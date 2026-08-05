"""Юнит-тесты контракта телеметрии: словарь типов и белый список props (§1/§10).

Чистые функции, БД не нужна. Запуск:
    python -m pytest tests/test_event_contract.py    # если установлен pytest
    python tests/test_event_contract.py              # standalone (без зависимостей)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pydantic

from app import schemas as sch


def test_known_types_pass():
    for event_type in sch.EventType:
        assert sch.normalize_event(sch.Event(type=event_type.value)) is not None


def test_unknown_type_rejected():
    for bad in ("exhibitView", "hall-view", "", "   ", "drop table"):
        assert sch.normalize_event(sch.Event(type=bad)) is None


def test_audio_play_normalized_to_tts_play():
    """Совместимость: накопленные данные не теряются и не раздваиваются."""
    normalized = sch.normalize_event(sch.Event(type="audio_play", exhibit_id=7))
    assert normalized is not None
    assert normalized.type == sch.EventType.tts_play.value
    assert normalized.exhibit_id == 7


def test_type_is_trimmed():
    assert sch.normalize_event(sch.Event(type="  hall_view ")).type == "hall_view"


def test_props_whitelist_drops_unknown_keys():
    """DoD §10: событие с props.user_agent записывается БЕЗ этого ключа."""
    event = sch.Event(
        type="exhibit_view",
        props={"source": "hall", "user_agent": "Mozilla/5.0", "ip": "203.0.113.7", "geo": "59.9,30.3"},
    )
    props = sch.normalize_event(event).props
    assert props == {"source": "hall"}


def test_denied_keys_are_not_in_any_whitelist():
    allowed = set().union(*sch.EVENT_PROPS_ALLOWED.values())
    assert not (allowed & sch.EVENT_PROPS_DENIED)


def test_props_are_per_type():
    # `text` разрешён у chat_message, но не у hall_view.
    assert sch.normalize_event(sch.Event(type="chat_message", props={"text": "привет"})).props == {"text": "привет"}
    assert sch.normalize_event(sch.Event(type="hall_view", props={"text": "привет"})).props is None


def test_long_text_is_truncated_not_rejected():
    long_text = "я" * (sch.MAX_PROPS_TEXT_LEN + 500)
    props = sch.normalize_event(sch.Event(type="chat_message", props={"text": long_text})).props
    assert len(props["text"]) == sch.MAX_PROPS_TEXT_LEN


def test_recognition_props_kept():
    props = sch.normalize_event(
        sch.Event(type="recognition", props={
            "recognized": True, "confidence": 0.91, "fallback": False, "candidates_count": 3, "ip": "1.2.3.4",
        })
    ).props
    assert props == {"recognized": True, "confidence": 0.91, "fallback": False, "candidates_count": 3}


def test_recognition_retry_kept():
    """ТЗ 04.08.2026 §1: повторная съёмка после неудачи доезжает до БД."""
    props = sch.normalize_event(
        sch.Event(type="recognition", props={"recognized": False, "retry": True})
    ).props
    assert props == {"recognized": False, "retry": True}


def test_recognition_retry_does_not_open_the_whitelist():
    """`retry` разрешён только у recognition, прочие ключи по-прежнему отбрасываются."""
    props = sch.normalize_event(
        sch.Event(type="recognition", props={"retry": True, "attempt": 2, "user_agent": "curl"})
    ).props
    assert props == {"retry": True}
    assert sch.normalize_event(sch.Event(type="hall_view", props={"retry": True})).props is None


def test_batch_size_limit():
    ok = sch.EventBatch(events=[sch.Event(type="hall_view")] * sch.MAX_EVENTS_PER_BATCH)
    assert len(ok.events) == sch.MAX_EVENTS_PER_BATCH
    try:
        sch.EventBatch(events=[sch.Event(type="hall_view")] * (sch.MAX_EVENTS_PER_BATCH + 1))
    except pydantic.ValidationError:
        pass
    else:
        raise AssertionError("батч сверх лимита должен отклоняться (422)")


def test_empty_batch_rejected():
    try:
        sch.EventBatch(events=[])
    except pydantic.ValidationError:
        pass
    else:
        raise AssertionError("пустой батч должен отклоняться")


def test_mixed_batch_keeps_valid_events():
    """DoD §1: одна опечатка не стоит девяти корректных событий."""
    batch = sch.EventBatch(events=[
        sch.Event(type="app_open"),
        sch.Event(type="exhibitView"),      # опечатка
        sch.Event(type="hall_view", hall_id=3),
        sch.Event(type="audio_play", exhibit_id=1),
    ])
    normalized = [sch.normalize_event(e) for e in batch.events]
    assert sum(1 for e in normalized if e is not None) == 3
    assert sum(1 for e in normalized if e is None) == 1


if __name__ == "__main__":
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("—" * 40)
    print("все тесты пройдены" if not failures else f"провалено: {failures}")
    sys.exit(1 if failures else 0)
