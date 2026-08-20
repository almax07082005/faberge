"""Синтез речи (Yandex SpeechKit + стаб).

Стаб генерирует настоящий (тихий) WAV нужной длительности и кладёт его в
media/tts/, чтобы кнопка «Прослушать» работала локально без облака. Реальный
SpeechKit вызывается при наличии ключа и отдаёт mp3/oggopus.

Версий API две. По умолчанию — v3 (`/tts/v3/utteranceSynthesis`): она
тарифицируется по запросам, а не по символам, и возвращает аудио потоком
JSON-кусков с таймингами. v1 (`/speech/v1/tts:synthesize`) остаётся рабочей и
включается SPEECHKIT_API_VERSION=v1 — это откат на случай, если с v3 что-то
пойдёт не так на проде.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import wave
from collections import OrderedDict
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple

import httpx

from ..config import settings
from . import UpstreamError, llm, storage
from .text_normalize import has_numerals, normalize_for_tts

logger = logging.getLogger(__name__)

# API v1 тарифицируется по СИМВОЛАМ, v3 — по ЗАПРОСАМ. У нас запросы короткие
# (реплика гида, подпись экспоната), поэтому по умолчанию идёт v3; v1 остаётся
# рабочим путём и включается SPEECHKIT_API_VERSION=v1 (аварийный откат).
SPEECHKIT_URL = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
SPEECHKIT_V3_URL = "https://tts.api.cloud.yandex.net/tts/v3/utteranceSynthesis"
_CHARS_PER_SEC = 14.0  # грубая оценка темпа речи (фолбэк, если v3 не вернул тайминги)
_CONTENT_TYPE = {"mp3": "audio/mpeg", "oggopus": "audio/ogg", "wav": "audio/wav"}
# Контейнер аудио в v3 задаётся перечислением, а не строкой формата из v1.
_V3_CONTAINER = {"mp3": "MP3", "oggopus": "OGG_OPUS", "wav": "WAV"}

# Поддерживаемые амплуа (роли) по голосам. В v1 REST параметр называется
# `emotion`, но принимает именно значения амплуа из «Списка голосов» SpeechKit.
# Нейтральное амплуа звучит «по-роботски»; тёплое (good/friendly) — человечнее.
# Если запросить амплуа, которого у голоса нет, SpeechKit отвечает ошибкой,
# поэтому здесь же — карта для безопасного фолбэка.
VOICE_ROLES = {
    "alena": {"neutral", "good"},
    "filipp": {"neutral"},
    "ermil": {"neutral", "good"},
    "jane": {"neutral", "good", "evil"},
    "omazh": {"neutral", "evil"},
    "zahar": {"neutral", "good"},
    "dasha": {"neutral", "good", "friendly"},
    "lera": {"neutral", "friendly"},
    "marina": {"neutral", "whisper", "friendly"},
    "alexander": {"neutral", "good"},
    "kirill": {"neutral", "strict", "good"},
}
# Приоритет «живости» при фолбэке: тёплое → дружелюбное → нейтральное.
_WARM_PRIORITY = ("good", "friendly", "neutral")


def _resolve_role(voice: str, requested: str) -> str:
    """Подбирает ближайшее поддерживаемое голосом амплуа к запрошенному."""
    supported = VOICE_ROLES.get(voice, {"neutral"})
    if requested in supported:
        return requested
    for role in _WARM_PRIORITY:
        if role in supported:
            return role
    return "neutral"


def _cache_key(voice: str, text: str, role: str, speed: float, fmt: str) -> str:
    raw = f"{voice}:{role}:{speed}:{fmt}:{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ── Числительные прописью перед синтезом ─────────────────────────────────────
# Баг-репорт 28.07.2026, п.2: «Пётр I» уходил в синтез как «Пётр 1» и звучал
# «Пётр один». Правильную форму («Пётр Первый», «в девятнадцатом веке») даёт
# llm.to_spoken_text — тот же инструмент, что готовит short_description_spoken
# у экспонатов (E15). Здесь он подключён и к произвольному тексту (кнопка
# «Прослушать» в чате), а детерминированный normalize_for_tts остаётся фолбэком.
#
# Стоимость: LLM зовём только когда в тексте реально есть числа, и кэшируем
# результат по хэшу исходного текста — повторные «Прослушать» на том же ответе
# гида и типовые фразы не оплачиваются заново. Кэш процессный (LRU): при
# рестарте/масштабировании просто прогревается заново.
_SPOKEN_CACHE: "OrderedDict[str, str]" = OrderedDict()
_SPOKEN_CACHE_MAX = 512
# Защита от «разговорчивой» модели: если LLM вернул явно не переписанный текст
# (пусто или втрое длиннее исходного), берём детерминированный вариант.
_SPOKEN_MAX_GROWTH = 3.0


async def prepare_for_tts(text: str) -> str:
    """Подготовить произвольный текст к синтезу: числа — прописью, в нужном падеже."""
    if not text or not has_numerals(text):
        return text
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cached = _SPOKEN_CACHE.get(key)
    if cached is not None:
        _SPOKEN_CACHE.move_to_end(key)
        return cached
    spoken = None
    if settings.tts_spoken_via_llm:
        # to_spoken_text сам возвращает None, если LLM не настроен или недоступен.
        spoken = await llm.to_spoken_text(text)
        if spoken and len(spoken) > len(text) * _SPOKEN_MAX_GROWTH:
            spoken = None
    result = spoken or normalize_for_tts(text)
    _SPOKEN_CACHE[key] = result
    if len(_SPOKEN_CACHE) > _SPOKEN_CACHE_MAX:
        _SPOKEN_CACHE.popitem(last=False)
    return result


@dataclass
class SpeechOutcome:
    audio_url: str
    fmt: str
    duration_ms: int
    characters: int
    cached: bool


async def synthesize(
    text: str,
    voice: str = "alena",
    fmt: str = "mp3",
    speed: float = 1.0,
    emotion: str = "good",
) -> SpeechOutcome:
    # Числа — прописью в нужном падеже («Пётр I» → «Пётр Первый», «XIX век» →
    # «девятнадцатый век»). Делаем ДО подсчёта символов и кэш-ключа — иначе на
    # старые записи кэша отдавалась бы прежняя (неправильная) озвучка.
    text = await prepare_for_tts(text)
    characters = len(text)
    if settings.tts_configured:
        return await _synthesize_yandex(text, voice, fmt, speed, emotion, characters)
    return _synthesize_stub(text, voice, characters)


def _synthesize_stub(text: str, voice: str, characters: int) -> SpeechOutcome:
    seconds = max(1.0, min(characters / _CHARS_PER_SEC, 12.0))  # ограничим файл 12 сек
    key = _cache_key(voice, text, "stub", 1.0, "wav")
    rel = f"tts/{voice}_{key}.wav"
    out_dir = os.path.join(settings.media_dir, "tts")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(settings.media_dir, rel)
    cached = os.path.exists(path)
    if not cached:
        _write_silent_wav(path, seconds)
    return SpeechOutcome(
        audio_url=f"{settings.public_base_url}/media/{rel}",
        fmt="wav",
        duration_ms=int(seconds * 1000),
        characters=characters,
        cached=cached,
    )


def _write_silent_wav(path: str, seconds: float, rate: int = 8000) -> None:
    frames = int(seconds * rate)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * frames)


async def _synthesize_yandex(
    text: str, voice: str, fmt: str, speed: float, emotion: str, characters: int
) -> SpeechOutcome:
    # `emotion` несёт амплуа голоса (в v1 параметр так и называется, в v3 это
    # hint `role`); приводим к поддерживаемому, чтобы тёплый дефолт (good) не
    # ронял синтез на голосах, у которых его нет.
    role = _resolve_role(voice, emotion)
    duration_ms: Optional[int] = None
    if settings.speechkit_v3:
        audio_bytes, duration_ms = await _fetch_v3(text, voice, fmt, speed, role)
    else:
        audio_bytes = await _fetch_v1(text, voice, fmt, speed, role)
    if duration_ms is None:
        duration_ms = int(max(1.0, characters / _CHARS_PER_SEC) * 1000)

    # Кэшируем результат в Object Storage в проде (ссылка переживает смену
    # экземпляра функции); локально, без бакета, — в media/. Ключ включает
    # амплуа/скорость/формат, иначе смена настроек отдавала бы старый файл.
    key = _cache_key(voice, text, role, speed, fmt)
    rel = f"tts/{voice}_{key}.{fmt}"
    if settings.storage_configured:
        stored = await storage.save_bytes(
            audio_bytes, rel, _CONTENT_TYPE.get(fmt, "application/octet-stream")
        )
        audio_url = stored.url
    else:
        out_dir = os.path.join(settings.media_dir, "tts")
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(settings.media_dir, rel), "wb") as fh:
            fh.write(audio_bytes)
        audio_url = f"{settings.public_base_url}/media/{rel}"
    # Строка расхода на синтез. В v3 тарификация по ЗАПРОСАМ, поэтому в логе
    # важно и число символов (чтобы видеть, что мы не шлём простыни), и сам факт
    # запроса: одна строка = один платный вызов.
    logger.info(
        "tts_request api=%s voice=%s role=%s fmt=%s chars=%s duration_ms=%s bytes=%s",
        "v3" if settings.speechkit_v3 else "v1",
        voice, role, fmt, characters, duration_ms, len(audio_bytes),
    )
    return SpeechOutcome(
        audio_url=audio_url,
        fmt=fmt,
        duration_ms=duration_ms,
        characters=characters,
        cached=False,
    )


def _auth_headers(with_folder: bool = True) -> dict:
    """Заголовки запроса. В v1 каталог передаётся полем folderId в теле, в v3 —
    заголовком x-folder-id, поэтому заголовок ставим только там, где он нужен."""
    headers = {"Authorization": f"Api-Key {settings.speechkit_api_key or settings.yandex_api_key}"}
    if with_folder and settings.yandex_folder_id:
        headers["x-folder-id"] = settings.yandex_folder_id
    return headers


async def _fetch_v1(text: str, voice: str, fmt: str, speed: float, role: str) -> bytes:
    """Синтез через API v1 (тарификация по символам). Аварийный путь."""
    audio_format = "oggopus" if fmt == "oggopus" else ("lpcm" if fmt == "wav" else "mp3")
    data = {
        "text": text,
        "voice": voice,
        "emotion": role,
        "speed": str(speed),
        "format": audio_format,
        "lang": "ru-RU",
    }
    # lpcm/oggopus отдаём в 48 кГц — иначе wav скатывается к «телефонному»,
    # роботному звучанию. Для mp3 частота фиксирована и параметр игнорируется.
    if audio_format in ("lpcm", "oggopus"):
        data["sampleRateHertz"] = "48000"
    if settings.yandex_folder_id:
        data["folderId"] = settings.yandex_folder_id
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(SPEECHKIT_URL, headers=_auth_headers(with_folder=False), data=data)
            resp.raise_for_status()
            return resp.content
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError("Сервис озвучивания временно недоступен.") from exc


def _v3_payload(text: str, voice: str, fmt: str, speed: float, role: str) -> dict:
    """Тело запроса v3.

    Отличия от v1, из-за которых понадобился отдельный путь: параметры голоса
    переехали в список `hints` (по одному полю в элементе — в proto это oneof),
    формат задаётся перечислением контейнера, а не строкой, и добавилась
    нормализация громкости (LUFS — рекомендация SpeechKit, ровнее по громкости
    между репликами).
    """
    hints: List[dict] = [{"voice": voice}, {"role": role}]
    if speed and speed != 1.0:
        hints.append({"speed": float(speed)})
    return {
        "text": text,
        "hints": hints,
        "outputAudioSpec": {"containerAudio": {"containerAudioType": _V3_CONTAINER.get(fmt, "MP3")}},
        "loudnessNormalizationType": "LUFS",
    }


def _iter_v3_messages(body: str) -> Iterator[dict]:
    """Сообщения ответа v3: по одному JSON на строку либо один цельный JSON.

    Обычный ответ — поток строк, но короткий синтез умещается в одно сообщение,
    и посредники (API Gateway) отдают его как обычный JSON-объект или массив.
    Разбираем оба вида, иначе аудио «пропадёт» на ровном месте.
    """
    body = (body or "").strip()
    if not body:
        return
    try:
        whole = json.loads(body)
    except json.JSONDecodeError:
        pass
    else:
        for message in (whole if isinstance(whole, list) else [whole]):
            if isinstance(message, dict):
                yield message
        return
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            yield message


def _parse_v3_stream(body: str) -> Tuple[bytes, Optional[int]]:
    """Собрать аудио из потокового ответа v3.

    v3 отвечает не одним JSON, а ПОТОКОМ объектов `{"result": {...}}` — по
    одному на кусок аудио; куски идут подряд и склеиваются в готовый файл.
    Тайминги (`startMs`/`lengthMs`) приходят там же — берём длительность из
    них, а не из оценки по числу символов.
    """
    chunks: List[bytes] = []
    duration_ms = 0
    for message in _iter_v3_messages(body):
        result = message.get("result") if isinstance(message, dict) else None
        if not isinstance(result, dict):
            continue
        data = (result.get("audioChunk") or {}).get("data")
        if data:
            chunks.append(base64.b64decode(data))
        # int64 в JSON приходит строкой ("lengthMs": "1234").
        try:
            end = int(result.get("startMs") or 0) + int(result.get("lengthMs") or 0)
            duration_ms = max(duration_ms, end)
        except (TypeError, ValueError):
            pass
    return b"".join(chunks), (duration_ms or None)


async def _fetch_v3(
    text: str, voice: str, fmt: str, speed: float, role: str
) -> Tuple[bytes, Optional[int]]:
    """Синтез через API v3 (тарификация по запросам)."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                SPEECHKIT_V3_URL, headers=_auth_headers(), json=_v3_payload(text, voice, fmt, speed, role)
            )
            resp.raise_for_status()
            audio_bytes, duration_ms = _parse_v3_stream(resp.text)
    except UpstreamError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError("Сервис озвучивания временно недоступен.") from exc
    if not audio_bytes:
        # 200 без единого audioChunk — ошибка формата запроса или пустой ответ.
        # Молча отдавать «файл» на 0 байт нельзя: фронт покажет битый плеер.
        logger.warning("speechkit v3: пустой ответ, voice=%s fmt=%s chars=%s", voice, fmt, len(text))
        raise UpstreamError("Сервис озвучивания временно недоступен.")
    return audio_bytes, duration_ms
