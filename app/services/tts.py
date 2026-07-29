"""Синтез речи (Yandex SpeechKit + стаб).

Стаб генерирует настоящий (тихий) WAV нужной длительности и кладёт его в
media/tts/, чтобы кнопка «Прослушать» работала локально без облака. Реальный
SpeechKit вызывается при наличии ключа и отдаёт mp3/oggopus.
"""
from __future__ import annotations

import hashlib
import os
import wave
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

import httpx

from ..config import settings
from . import UpstreamError, llm, storage
from .text_normalize import has_numerals, normalize_for_tts

SPEECHKIT_URL = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
_CHARS_PER_SEC = 14.0  # грубая оценка темпа речи
_CONTENT_TYPE = {"mp3": "audio/mpeg", "oggopus": "audio/ogg", "wav": "audio/wav"}

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
    audio_format = "oggopus" if fmt == "oggopus" else ("lpcm" if fmt == "wav" else "mp3")
    # `emotion` в v1 REST несёт амплуа голоса; приводим к поддерживаемому, чтобы
    # тёплый дефолт (good) не ронял синтез на голосах, у которых его нет.
    role = _resolve_role(voice, emotion)
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
    headers = {"Authorization": f"Api-Key {settings.speechkit_api_key or settings.yandex_api_key}"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(SPEECHKIT_URL, headers=headers, data=data)
            resp.raise_for_status()
            audio_bytes = resp.content
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError("Сервис озвучивания временно недоступен.") from exc

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
    seconds = max(1.0, characters / _CHARS_PER_SEC)
    return SpeechOutcome(
        audio_url=audio_url,
        fmt=fmt,
        duration_ms=int(seconds * 1000),
        characters=characters,
        cached=False,
    )
