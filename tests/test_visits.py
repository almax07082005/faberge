"""Юнит-тесты разбиения потока событий на визиты по таймауту (§5 ТЗ 03.08.2026).

Чистые функции, БД не нужна. Запуск:
    python -m pytest tests/test_visits.py    # если установлен pytest
    python tests/test_visits.py              # standalone (без зависимостей)
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import visits

BASE = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)


@dataclass
class Ev:
    """Минимальная замена строке из `events` (нужны только session_id и ts)."""

    session_id: str
    minutes: int
    type: str = "exhibit_view"

    @property
    def ts(self) -> datetime:
        return BASE + timedelta(minutes=self.minutes)


def test_single_visit_when_gaps_are_short():
    events = [Ev("s1", 0), Ev("s1", 5), Ev("s1", 20)]
    result = visits.split_visits(events)
    assert len(result) == 1
    assert len(result[0]) == 3


def test_four_hour_gap_splits_into_two_visits():
    """DoD §5: сессия с разрывом 4 часа — два визита, а не один на 4 часа."""
    events = [Ev("s1", 0), Ev("s1", 10), Ev("s1", 250), Ev("s1", 255)]
    result = visits.split_visits(events)
    assert len(result) == 2
    first, second = result
    assert (first[-1].ts - first[0].ts) == timedelta(minutes=10)
    assert (second[-1].ts - second[0].ts) == timedelta(minutes=5)


def test_gap_exactly_at_timeout_stays_one_visit():
    """Ровно 30 минут — ещё не разрыв, разрезаем при паузе БОЛЬШЕ таймаута."""
    assert len(visits.split_visits([Ev("s1", 0), Ev("s1", 30)], timeout_minutes=30)) == 1
    assert len(visits.split_visits([Ev("s1", 0), Ev("s1", 31)], timeout_minutes=30)) == 2


def test_custom_timeout():
    events = [Ev("s1", 0), Ev("s1", 10)]
    assert len(visits.split_visits(events, timeout_minutes=5)) == 2
    assert len(visits.split_visits(events, timeout_minutes=60)) == 1


def test_empty_and_single():
    assert visits.split_visits([]) == []
    assert len(visits.split_visits([Ev("s1", 0)])) == 1


def test_visits_by_session_splits_each_session():
    rows = [
        Ev("s1", 0), Ev("s1", 5),
        Ev("s1", 300),                       # тот же session_id, но спустя 5 часов
        Ev("s2", 0), Ev("s2", 1),
    ]
    result = list(visits.visits_by_session(rows))
    assert [sid for sid, _ in result] == ["s1", "s1", "s2"]
    assert [len(events) for _sid, events in result] == [2, 1, 2]


def test_duration_does_not_depend_on_session_end():
    """Длительность визита одинакова, дошёл `session_end` или нет."""
    with_end = [Ev("s1", 0), Ev("s1", 12), Ev("s1", 12, type="session_end")]
    without_end = [Ev("s1", 0), Ev("s1", 12)]
    duration = lambda events: (events[-1].ts - events[0].ts)  # noqa: E731
    assert duration(visits.split_visits(with_end)[0]) == duration(visits.split_visits(without_end)[0])


def test_visits_by_session_empty():
    assert list(visits.visits_by_session([])) == []


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
