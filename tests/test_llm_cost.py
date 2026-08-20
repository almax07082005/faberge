"""Юнит-тесты оптимизации расхода LLM и SpeechKit (задача 19.08.2026).

Пять мер, каждая проверяется по тому, что реально уходит в облако (перехват
httpx), а не по коду глазами:
  1. SpeechKit v3 — тело запроса и разбор потокового ответа; v1 остаётся
     рабочим путём отката (SPEECHKIT_API_VERSION=v1);
  2. числа прописью считает lite-модель;
  3. контекст уточняющего вопроса урезан: 3 реплики истории и справка по лимиту;
  4. рассказ ограничен по объёму — промптом и страховочным maxTokens;
  5. расход пишется в лог (llm_usage / tts_request).

Сеть и БД не нужны — httpx подменяется. Запуск:
    python -m pytest tests/test_llm_cost.py
    python tests/test_llm_cost.py            # standalone (без зависимостей)
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.services import UpstreamError, llm, tts  # noqa: E402


# ── Обвязка: подменённый httpx и настройки ──────────────────────────────────
class FakeResponse:
    def __init__(self, payload=None, text: str = "", content: bytes = b"") -> None:
        self._payload = payload
        self.text = text if text or payload is None else json.dumps(payload)
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class FakeClient:
    """AsyncClient на один запрос: складывает вызов в calls и отдаёт заготовку."""

    def __init__(self, calls: list, response: FakeResponse) -> None:
        self._calls = calls
        self._response = response

    def __call__(self, *args, **kwargs):  # httpx.AsyncClient(timeout=...)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None, data=None):
        self._calls.append({"url": url, "headers": headers or {}, "json": json, "data": data})
        return self._response


class FakeHttpx:
    def __init__(self, calls: list, response: FakeResponse) -> None:
        self.AsyncClient = FakeClient(calls, response)


@contextlib.contextmanager
def patched(module, response: FakeResponse, **overrides):
    """Подменить httpx в модуле и временно переопределить настройки."""
    calls: list = []
    original_httpx = module.httpx
    saved = {k: getattr(settings, k) for k in overrides}
    module.httpx = FakeHttpx(calls, response)
    for key, value in overrides.items():
        setattr(settings, key, value)
    try:
        yield calls
    finally:
        module.httpx = original_httpx
        for key, value in saved.items():
            setattr(settings, key, value)


def gpt_response(text: str = "ответ", input_tokens: int = 412, output_tokens: int = 180) -> FakeResponse:
    return FakeResponse(
        {
            "result": {
                "alternatives": [{"message": {"role": "assistant", "text": text}, "status": "FINAL"}],
                "usage": {
                    "inputTextTokens": str(input_tokens),
                    "completionTokens": str(output_tokens),
                    "totalTokens": str(input_tokens + output_tokens),
                },
                "modelVersion": "23.10.2024",
            }
        }
    )


LLM_ENV = {"yandex_api_key": "test-key", "yandex_folder_id": "test-folder",
           "yandexgpt_model_uri": "gpt://test-folder/yandexgpt/latest"}


# ── 2. Lite-модель для механической правки ──────────────────────────────────
def test_lite_model_uri_derived_from_main():
    """Без явного URI lite выводится из основного заменой сегмента модели."""
    with patched(llm, gpt_response(), **LLM_ENV, yandexgpt_lite_model_uri=None):
        assert llm._model_uri() == "gpt://test-folder/yandexgpt/latest"
        assert llm._model_uri(lite=True) == "gpt://test-folder/yandexgpt-lite/latest"


def test_lite_model_uri_explicit_wins():
    with patched(llm, gpt_response(), **LLM_ENV,
                 yandexgpt_lite_model_uri="gpt://other/yandexgpt-lite/rc"):
        assert llm._model_uri(lite=True) == "gpt://other/yandexgpt-lite/rc"


def test_lite_model_uri_keeps_finetuned_model():
    """У дообученной модели (ds://) подменять нечего — запрос не должен уйти в никуда."""
    env = dict(LLM_ENV, yandexgpt_model_uri="ds://b1gfine/tuned")
    with patched(llm, gpt_response(), **env, yandexgpt_lite_model_uri=None):
        assert llm._model_uri(lite=True) == "ds://b1gfine/tuned"


def test_spoken_text_goes_to_lite():
    """to_spoken_text — механическая правка, идёт на lite, не на основную модель."""
    with patched(llm, gpt_response("Пётр Первый"), **LLM_ENV, yandexgpt_lite_model_uri=None) as calls:
        result = asyncio.run(llm.to_spoken_text("Пётр I"))
    assert result == "Пётр Первый"
    assert calls[0]["json"]["modelUri"] == "gpt://test-folder/yandexgpt-lite/latest"


# ── 3. Короткий контекст уточняющего вопроса ────────────────────────────────
def test_chat_keeps_only_last_turns():
    history = [("user" if i % 2 == 0 else "assistant", f"реплика {i}") for i in range(10)]
    with patched(llm, gpt_response(), **LLM_ENV, guide_history_turns=3) as calls:
        asyncio.run(llm._yandexgpt_chat("", history, "а это что?", "ru"))
    prompt = calls[0]["json"]["messages"][1]["text"]
    assert "реплика 9" in prompt and "реплика 7" in prompt
    assert "реплика 6" not in prompt          # шестая с конца отрезана
    assert "реплика 0" not in prompt


def test_chat_shortens_grounding():
    grounding = " ".join(f"Фраза номер {i}." for i in range(1, 200))
    with patched(llm, gpt_response(), **LLM_ENV, guide_grounding_max_chars=700) as calls:
        asyncio.run(llm._yandexgpt_chat(grounding, [], "а это что?", "ru"))
    prompt = calls[0]["json"]["messages"][1]["text"]
    assert "Фраза номер 1." in prompt
    assert "Фраза номер 199." not in prompt
    # Справка обрезана по границе фразы, а не на полуслове.
    sent = prompt.split("Справка о текущем месте посетителя (может быть не связана с вопросом): ")[1]
    sent = sent.split("\nВопрос посетителя:")[0]
    assert len(sent) <= 700 and sent.endswith(".")


def test_source_urls_never_reach_the_model():
    """Ссылка из справки читается моделью как факт — вырезаем её до промпта.

    Живой случай с прода: у портсигара id=144 весь raw_history заканчивался
    «Источник: https://…/portcigar-dly-cera-doycona», и рассказ выходил про
    несуществующего «царя Дойкона», вычитанного из slug'а URL.
    """
    raw = ("Справочно — Место создания: Санкт-Петербург; Техника: Чеканка; "
           "Источник: https://fabergemuseum.ru/kollekczii/portcigar-dly-cera-doycona.")
    assert llm._strip_sources(raw) == "Справочно — Место создания: Санкт-Петербург; Техника: Чеканка"
    assert llm._strip_sources("Текст без ссылок.") == "Текст без ссылок."
    assert llm._strip_sources(None) == ""
    with patched(llm, gpt_response("рассказ"), **LLM_ENV) as calls:
        asyncio.run(llm._yandexgpt_story({"name": "Портсигар", "raw_history": raw}, "engaging", "ru"))
    assert "http" not in calls[0]["json"]["messages"][1]["text"]
    with patched(llm, gpt_response(), **LLM_ENV) as calls:
        asyncio.run(llm._yandexgpt_chat(raw, [], "что это?", "ru"))
    assert "http" not in calls[0]["json"]["messages"][1]["text"]


def test_shorten_cuts_on_sentence_boundary():
    assert llm._shorten("Короткий текст.", 100) == "Короткий текст."
    assert llm._shorten("Первое предложение. Второе предложение.", 25) == "Первое предложение."
    # Границы фразы нет — режем по слову и помечаем многоточием.
    assert llm._shorten("слово " * 40, 30).endswith("…")
    assert llm._shorten("", 10) == ""


# ── 4. Рассказ вдвое короче ─────────────────────────────────────────────────
def test_story_prompt_carries_length_budget():
    exhibit = {"name": "Яйцо", "raw_history": "История предмета", "year_created": "1885"}
    with patched(llm, gpt_response("рассказ"), **LLM_ENV,
                 guide_story_max_chars=1200, guide_story_max_tokens=500) as calls:
        asyncio.run(llm._yandexgpt_story(exhibit, "engaging", "ru"))
    body = calls[0]["json"]
    assert "не больше 1200 знаков" in body["messages"][1]["text"]
    # maxTokens — страховка над целью, а не сама цель: обрезка рвала бы фразу.
    assert body["completionOptions"]["maxTokens"] == "500"


# ── 5. Расход в логах ───────────────────────────────────────────────────────
class Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextlib.contextmanager
def collected(logger: logging.Logger):
    handler = Collector()
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


def test_usage_is_logged():
    with collected(llm.logger) as messages:
        with patched(llm, gpt_response(input_tokens=412, output_tokens=180), **LLM_ENV,
                     llm_log_usage=True):
            asyncio.run(llm._yandexgpt_chat("", [], "вопрос", "ru"))
    line = next(m for m in messages if m.startswith("llm_usage"))
    assert "operation=chat" in line
    assert "input_tokens=412" in line and "output_tokens=180" in line and "total_tokens=592" in line
    assert "model=gpt://test-folder/yandexgpt/latest" in line


def test_usage_log_can_be_switched_off():
    with collected(llm.logger) as messages:
        with patched(llm, gpt_response(), **LLM_ENV, llm_log_usage=False):
            asyncio.run(llm._yandexgpt_chat("", [], "вопрос", "ru"))
    assert not [m for m in messages if m.startswith("llm_usage")]


def test_usage_survives_missing_fields():
    """Ответ без usage не должен ронять запрос — расход просто неизвестен."""
    response = FakeResponse({"result": {"alternatives": [{"message": {"text": "ок"}}]}})
    with collected(llm.logger) as messages:
        with patched(llm, response, **LLM_ENV, llm_log_usage=True):
            assert asyncio.run(llm._yandexgpt_chat("", [], "вопрос", "ru")) == "ок"
    assert "input_tokens=None" in next(m for m in messages if m.startswith("llm_usage"))


def test_app_raises_root_log_level():
    """Без явного уровня INFO-строки расхода не доходят до логов Cloud Functions.

    Рантайм вешает на корневой логгер свой обработчик и оставляет уровень
    WARNING; `logging.basicConfig` в этом случае молча ничего не делает.
    Проверено на проде 20.08.2026 — llm_usage не было в логах вовсе.
    """
    import app.main  # noqa: F401  (импорт сам настраивает логирование)

    assert logging.getLogger().level <= logging.INFO
    assert logging.getLogger("httpx").level == logging.WARNING


# ── 1. SpeechKit v3 ─────────────────────────────────────────────────────────
def v3_stream(*chunks: bytes, start_ms: int = 0, length_ms: int = 1000) -> str:
    lines = []
    for index, chunk in enumerate(chunks):
        lines.append(json.dumps({
            "result": {
                "audioChunk": {"data": base64.b64encode(chunk).decode("ascii")},
                "startMs": str(start_ms + index * length_ms),
                "lengthMs": str(length_ms),
            }
        }))
    return "\n".join(lines)


def test_v3_payload_shape():
    payload = tts._v3_payload("текст", "alena", "mp3", 1.2, "good")
    assert payload["text"] == "текст"
    assert {"voice": "alena"} in payload["hints"] and {"role": "good"} in payload["hints"]
    assert {"speed": 1.2} in payload["hints"]
    assert payload["outputAudioSpec"] == {"containerAudio": {"containerAudioType": "MP3"}}
    assert payload["loudnessNormalizationType"] == "LUFS"
    # speed=1.0 — значение по умолчанию, в запрос не кладём.
    assert all("speed" not in hint for hint in tts._v3_payload("т", "alena", "oggopus", 1.0, "good")["hints"])
    assert tts._v3_payload("т", "alena", "oggopus", 1.0, "good")["outputAudioSpec"] == {
        "containerAudio": {"containerAudioType": "OGG_OPUS"}
    }


def test_v3_stream_is_concatenated_with_timings():
    audio, duration_ms = tts._parse_v3_stream(v3_stream(b"\x01\x02", b"\x03", length_ms=1500))
    assert audio == b"\x01\x02\x03"
    assert duration_ms == 3000                      # startMs 1500 + lengthMs 1500
    assert tts._parse_v3_stream("") == (b"", None)
    # Мусорная строка в потоке не должна ронять сборку.
    assert tts._parse_v3_stream("не json\n" + v3_stream(b"\x09"))[0] == b"\x09"


def test_v3_accepts_single_json_and_array():
    """Короткий синтез приходит одним объектом (или массивом) вместо потока строк."""
    single = json.dumps({"result": {"audioChunk": {"data": base64.b64encode(b"one").decode()},
                                    "startMs": "0", "lengthMs": "500"}})
    assert tts._parse_v3_stream(single) == (b"one", 500)
    array = json.dumps([
        {"result": {"audioChunk": {"data": base64.b64encode(b"a").decode()}, "startMs": "0", "lengthMs": "100"}},
        {"result": {"audioChunk": {"data": base64.b64encode(b"b").decode()}, "startMs": "100", "lengthMs": "100"}},
    ])
    assert tts._parse_v3_stream(array) == (b"ab", 200)


def test_v3_is_the_default_path():
    response = FakeResponse(text=v3_stream(b"audio-bytes", length_ms=2000))
    with patched(tts, response, speechkit_api_version="v3", yandex_api_key="k",
                 speechkit_api_key="sk", yandex_folder_id="test-folder",
                 object_storage_bucket=None, media_dir=tempfile.mkdtemp()) as calls:
        outcome = asyncio.run(tts._synthesize_yandex("текст", "alena", "mp3", 1.0, "good", 6))
    assert calls[0]["url"] == tts.SPEECHKIT_V3_URL
    assert calls[0]["headers"]["x-folder-id"] == "test-folder"
    assert calls[0]["data"] is None                 # v3 — JSON, а не form-data
    assert outcome.duration_ms == 2000              # длительность из таймингов, не из оценки
    assert outcome.fmt == "mp3"


def test_v1_remains_available_as_rollback():
    response = FakeResponse(content=b"mp3-bytes")
    with patched(tts, response, speechkit_api_version="v1", yandex_api_key="k",
                 speechkit_api_key="sk", yandex_folder_id="test-folder",
                 object_storage_bucket=None, media_dir=tempfile.mkdtemp()) as calls:
        outcome = asyncio.run(tts._synthesize_yandex("текст", "alena", "mp3", 1.0, "good", 6))
    assert calls[0]["url"] == tts.SPEECHKIT_URL
    assert calls[0]["data"]["format"] == "mp3"      # v1 — form-data
    assert outcome.duration_ms > 0                  # оценка по числу символов


def test_v3_empty_answer_is_an_error_not_a_broken_file():
    with patched(tts, FakeResponse(text=""), speechkit_api_version="v3", yandex_api_key="k",
                 speechkit_api_key="sk", object_storage_bucket=None):
        try:
            asyncio.run(tts._fetch_v3("текст", "alena", "mp3", 1.0, "good"))
        except UpstreamError:
            pass
        else:
            raise AssertionError("пустой ответ v3 должен давать UpstreamError")


def test_tts_request_is_logged():
    response = FakeResponse(text=v3_stream(b"audio", length_ms=1200))
    with collected(tts.logger) as messages:
        with patched(tts, response, speechkit_api_version="v3", yandex_api_key="k",
                     speechkit_api_key="sk", object_storage_bucket=None,
                     media_dir=tempfile.mkdtemp()):
            asyncio.run(tts._synthesize_yandex("текст", "alena", "mp3", 1.0, "good", 5))
    line = next(m for m in messages if m.startswith("tts_request"))
    assert "api=v3" in line and "chars=5" in line and "duration_ms=1200" in line


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
