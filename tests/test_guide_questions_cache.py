"""Юнит-тесты кэша вопросов-подсказок (просьба заказчика 26.08.2026).

Проверяем то, ради чего кэш заводился, и то, чем за него платим:
  1. повторное обращение к экспонату НЕ идёт в LLM — вопросы приходят из БД;
  2. правка описания в админке инвалидирует запись сама (по хэшу текста);
  3. в кэше лежит пул, наружу уходит срез — /guide/chat с max_questions=3 не
     выбивает запись, сделанную /guide/story для 4;
  4. сбой LLM не роняет рассказ, если есть чем ответить (устаревший набор);
  5. рассказ и реплика диалога больше НЕ тянут второй вызов LLM за вопросами.

БД и сеть не нужны: слой хранения (`crud`) и вызов модели (`llm`) подменяются.
Запуск:
    python -m pytest tests/test_guide_questions_cache.py
    python tests/test_guide_questions_cache.py       # standalone
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.services import UpstreamError, guide_questions, llm  # noqa: E402


EXHIBIT = {
    "id": 144,
    "name": "Портсигар",
    "label_slug": "faberge_egg_hen",
    "short_description": "Портсигар фирмы Фаберже.",
    "raw_history": "Подарен Николаем II. Источник: https://fabergemuseum.ru/portcigar",
}


class FakeRow:
    """Строка exhibit_questions."""

    def __init__(self, questions, source_hash, model="gpt://test/yandexgpt/latest"):
        self.questions = list(questions)
        self.source_hash = source_hash
        self.model = model


class FakeExhibit:
    """ORM-экспонат в объёме, который читает crud.exhibit_to_dict."""

    def __init__(self, exhibit: dict) -> None:
        for field in (
            "id", "exhibit_number", "label_slug", "name", "year_created", "master_name",
            "material", "techniques", "short_description", "raw_history",
        ):
            setattr(self, field, exhibit.get(field))


@contextlib.contextmanager
def wired(row=None, generated=None, error=False, **overrides):
    """Подменить хранилище и LLM. Возвращает журнал вызовов."""
    calls = {"llm": [], "saved": [], "read": []}
    saved_settings = {k: getattr(settings, k) for k in overrides}
    original = {
        "get": guide_questions.crud.get_exhibit_questions,
        "save": guide_questions.crud.save_exhibit_questions,
        "llm": guide_questions.llm.suggested_questions,
    }

    async def fake_get(session, exhibit_id, language="ru"):
        calls["read"].append((exhibit_id, language))
        return row

    async def fake_save(session, exhibit_id, language, questions, source_hash, model=None):
        calls["saved"].append(
            {"exhibit_id": exhibit_id, "language": language, "questions": list(questions),
             "source_hash": source_hash, "model": model}
        )

    async def fake_llm(exhibit, max_questions, language="ru"):
        calls["llm"].append({"max_questions": max_questions, "language": language})
        if error:
            raise UpstreamError("Сервис генерации текста временно недоступен.")
        pool = generated if generated is not None else [f"Вопрос {i}?" for i in range(1, 7)]
        return list(pool)[:max_questions], "gpt://test/yandexgpt/latest"

    guide_questions.crud.get_exhibit_questions = fake_get
    guide_questions.crud.save_exhibit_questions = fake_save
    guide_questions.llm.suggested_questions = fake_llm
    for key, value in overrides.items():
        setattr(settings, key, value)
    try:
        yield calls
    finally:
        guide_questions.crud.get_exhibit_questions = original["get"]
        guide_questions.crud.save_exhibit_questions = original["save"]
        guide_questions.llm.suggested_questions = original["llm"]
        for key, value in saved_settings.items():
            setattr(settings, key, value)


def fresh_row(exhibit=EXHIBIT, questions=None, language="ru"):
    pool = questions if questions is not None else [f"Вопрос {i}?" for i in range(1, 7)]
    return FakeRow(pool, guide_questions.fingerprint(exhibit, language))


# ── 1. Попадание в кэш: LLM не зовём ────────────────────────────────────────
def test_cached_questions_skip_the_model():
    with wired(row=fresh_row()) as calls:
        questions = asyncio.run(guide_questions.for_exhibit(None, EXHIBIT, 4))
    assert calls["llm"] == [], "свежая запись не должна ходить в LLM"
    assert questions == ["Вопрос 1?", "Вопрос 2?", "Вопрос 3?", "Вопрос 4?"]


def test_pool_is_sliced_to_the_request():
    """В записи 6 вопросов: story просит 4, chat — 3, и оба обходятся без модели."""
    row = fresh_row()
    with wired(row=row) as calls:
        story = asyncio.run(guide_questions.for_exhibit(None, EXHIBIT, 4))
        chat = asyncio.run(guide_questions.for_exhibit(None, EXHIBIT, 3))
    assert len(story) == 4 and len(chat) == 3
    assert calls["llm"] == []


def test_zero_questions_costs_nothing():
    with wired(row=None) as calls:
        assert asyncio.run(guide_questions.for_exhibit(None, EXHIBIT, 0)) == []
    assert calls["llm"] == [] and calls["read"] == []


# ── 2. Промах: генерируем пул и сохраняем ───────────────────────────────────
def test_miss_generates_pool_and_stores_it():
    with wired(row=None, guide_questions_cache_size=6) as calls:
        questions = asyncio.run(guide_questions.for_exhibit(None, EXHIBIT, 4))
    assert calls["llm"][0]["max_questions"] == 6, "у модели просим пул, а не ровно max_questions"
    assert len(questions) == 4, "наружу — срез под запрос"
    stored = calls["saved"][0]
    assert stored["exhibit_id"] == 144 and len(stored["questions"]) == 6
    assert stored["source_hash"] == guide_questions.fingerprint(EXHIBIT)


def test_request_above_pool_size_still_honoured():
    """max_questions=6 при GUIDE_QUESTIONS_CACHE_SIZE=3 — просим 6, а не 3."""
    with wired(row=None, guide_questions_cache_size=3) as calls:
        questions = asyncio.run(guide_questions.for_exhibit(None, EXHIBIT, 6))
    assert calls["llm"][0]["max_questions"] == 6
    assert len(questions) == 6


def test_short_cached_pool_is_served_as_is():
    """max_questions — потолок, а не требование: короткий набор отдаём, не доплачивая.

    Модель регулярно возвращает меньше, чем просили. Если считать такую запись
    несвежей, каждый запрос будет заново платить за набор, который не вырастет.
    """
    with wired(row=fresh_row(questions=["Вопрос 1?", "Вопрос 2?"])) as calls:
        questions = asyncio.run(guide_questions.for_exhibit(None, EXHIBIT, 4))
    assert calls["llm"] == [], "короткий, но актуальный набор не повод звать LLM"
    assert questions == ["Вопрос 1?", "Вопрос 2?"]


# ── 3. Инвалидация: правка описания ─────────────────────────────────────────
def test_edited_description_invalidates_the_row():
    row = fresh_row()
    edited = dict(EXHIBIT, raw_history="Подарен Александру III. Совсем другая история.")
    with wired(row=row) as calls:
        asyncio.run(guide_questions.for_exhibit(None, edited, 4))
    assert calls["llm"], "текст карточки изменился — вопросы должны перегенерироваться"
    assert calls["saved"][0]["source_hash"] == guide_questions.fingerprint(edited)


def test_source_link_noise_does_not_invalidate():
    """Ссылка-источник в промпт не уходит (правка 20.08.2026) — и на хэш влиять не должна.

    Хэш считается по тексту ПОСЛЕ чистки: оборот «. Источник: <url>» вырезается
    целиком вместе с разделителем, поэтому эталон — фраза без точки на конце.
    """
    without_link = dict(EXHIBIT, raw_history="Подарен Николаем II")
    assert guide_questions.fingerprint(EXHIBIT) == guide_questions.fingerprint(without_link)


def test_language_is_part_of_the_key():
    assert guide_questions.fingerprint(EXHIBIT, "ru") != guide_questions.fingerprint(EXHIBIT, "en")


def test_force_regenerates_fresh_row():
    with wired(row=fresh_row()) as calls:
        asyncio.run(guide_questions.for_exhibit(None, EXHIBIT, 4, force=True))
    assert calls["llm"], "force обязан игнорировать свежую запись"


# ── 4. Сбои и выключенный кэш ───────────────────────────────────────────────
def test_llm_failure_falls_back_to_stale_row():
    stale = FakeRow(["Старый вопрос 1?", "Старый вопрос 2?"], "устаревший-хэш")
    with wired(row=stale, error=True):
        questions = asyncio.run(guide_questions.for_exhibit(None, EXHIBIT, 4))
    assert questions == ["Старый вопрос 1?", "Старый вопрос 2?"], "лучше устаревшие вопросы, чем 502 на рассказ"


def test_llm_failure_without_cache_still_raises():
    with wired(row=None, error=True):
        try:
            asyncio.run(guide_questions.for_exhibit(None, EXHIBIT, 4))
        except UpstreamError:
            return
    raise AssertionError("без записи в кэше сбой LLM должен пробрасываться, как и до кэша")


def test_disabled_cache_never_touches_the_table():
    with wired(row=fresh_row(), guide_questions_cache_enabled=False) as calls:
        questions = asyncio.run(guide_questions.for_exhibit(None, EXHIBIT, 3))
    assert calls["read"] == [] and calls["saved"] == [], "выключенный кэш не ходит в БД"
    assert calls["llm"] and len(questions) == 3


def test_exhibit_without_id_is_not_cached():
    """Экспонат без id (кэшировать не по чему) — прежнее поведение, прямой вызов."""
    with wired(row=fresh_row()) as calls:
        asyncio.run(guide_questions.for_exhibit(None, {"name": "Без id"}, 3))
    assert calls["read"] == [] and calls["saved"] == []
    assert calls["llm"]


# ── 5. Прогрев каталога ─────────────────────────────────────────────────────
def test_warm_skips_fresh_cards():
    with wired(row=None) as calls:
        outcome, _ = asyncio.run(
            guide_questions.warm_exhibit(None, FakeExhibit(EXHIBIT), fresh_row())
        )
    assert outcome == "cached" and calls["llm"] == [], "повторный прогрев не должен жечь токены"


def test_warm_dry_run_costs_nothing():
    with wired(row=None) as calls:
        outcome, _ = asyncio.run(
            guide_questions.warm_exhibit(None, FakeExhibit(EXHIBIT), None, dry_run=True)
        )
    assert outcome == "planned" and calls["llm"] == [] and calls["saved"] == []


def test_warm_generates_and_stores():
    with wired(row=None, guide_questions_cache_size=6) as calls:
        outcome, questions = asyncio.run(
            guide_questions.warm_exhibit(None, FakeExhibit(EXHIBIT), None)
        )
    assert outcome == "generated" and len(questions) == 6
    assert calls["saved"][0]["exhibit_id"] == 144


def test_warm_survives_llm_failure():
    with wired(row=None, error=True) as calls:
        outcome, _ = asyncio.run(guide_questions.warm_exhibit(None, FakeExhibit(EXHIBIT), None))
    assert outcome == "failed" and calls["saved"] == [], "битая карточка не должна ронять прогон"


# ── 6. Рассказ и диалог больше не платят за вопросы ─────────────────────────
def _counting_complete(calls):
    async def fake(system, user, temperature=0.6, max_tokens=800, operation="complete", lite=False):
        calls.append(operation)
        return "текст"
    return fake


@contextlib.contextmanager
def llm_calls():
    calls: list[str] = []
    original = llm._yandexgpt_complete
    saved = {k: getattr(settings, k) for k in ("yandex_api_key", "yandex_folder_id", "yandexgpt_model_uri")}
    llm._yandexgpt_complete = _counting_complete(calls)
    settings.yandex_api_key = "test-key"
    settings.yandex_folder_id = "test-folder"
    settings.yandexgpt_model_uri = "gpt://test-folder/yandexgpt/latest"
    try:
        yield calls
    finally:
        llm._yandexgpt_complete = original
        for key, value in saved.items():
            setattr(settings, key, value)


def test_story_is_a_single_llm_call():
    with llm_calls() as calls:
        text, model = asyncio.run(llm.generate_story(EXHIBIT, "engaging", "ru"))
    assert calls == ["story"], "рассказ больше не тянет за собой вызов questions"
    assert text and model.endswith("yandexgpt/latest")


def test_chat_is_a_single_llm_call():
    with llm_calls() as calls:
        answer = asyncio.run(llm.chat("справка", [], "вопрос", "ru"))
    assert calls == ["chat"], "реплика диалога больше не тянет за собой вызов questions"
    assert answer == "текст"


def test_questions_source_matches_the_prompt():
    """Хэш кэша считается по тому же тексту, что уходит в промпт (без ссылок-источников)."""
    source = llm.questions_source(EXHIBIT)
    assert "http" not in source and "Источник" not in source
    assert source.startswith("Подарен Николаем II")


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
