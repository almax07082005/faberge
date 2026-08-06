"""Юнит-тесты подсчёта `retry_after_fail` (ТЗ 04.08.2026 §1).

Повторная съёмка после неудачи считается по явному `props.retry` от фронта;
для визитов, записанных до правки контракта (пометки нет), остаётся эвристика
«за неудачей в том же визите пошла ещё одна попытка».

БД не нужна: подменяем `crud._visit_rows` на готовый список строк. Запуск:
    python -m pytest tests/test_recognition_retry.py
    python tests/test_recognition_retry.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import crud

BASE = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)


@dataclass
class Row:
    """Минимальная замена строке из `events` — те же поля, что выбирает _visit_rows."""

    type: str
    minutes: int
    session_id: str = "s1"
    exhibit_id: Optional[int] = None
    hall_id: Optional[int] = None
    props: Optional[dict] = field(default=None)

    @property
    def ts(self) -> datetime:
        return BASE + timedelta(minutes=self.minutes)


def _report(rows):
    """Прогнать analytics_recognition на подставленных строках."""
    original = crud._visit_rows

    async def fake_visit_rows(session, dfrom, dto):
        return rows

    crud._visit_rows = fake_visit_rows
    try:
        return asyncio.run(crud.analytics_recognition(None, None, None))
    finally:
        crud._visit_rows = original


def test_explicit_retry_is_counted():
    """Фронт прислал `retry: true` — попытка учтена как повторная съёмка."""
    report = _report([
        Row("recognition", 0, props={"recognized": False}),
        Row("recognition", 1, props={"recognized": True, "retry": True}),
    ])
    assert report.total == 2
    assert report.success == 1
    assert report.retry_after_fail == 1


def test_explicit_retry_false_is_not_counted():
    """Съёмка другого экспоната после успеха — `retry: false`, не повтор."""
    report = _report([
        Row("recognition", 0, props={"recognized": True, "retry": False}),
        Row("recognition", 1, props={"recognized": True, "retry": False}),
    ])
    assert report.retry_after_fail == 0


def test_explicit_flag_wins_over_heuristic():
    """Явный признак не складывается с эвристикой: неудача + повтор = 1, не 2."""
    report = _report([
        Row("recognition", 0, props={"recognized": False, "retry": False}),
        Row("recognition", 1, props={"recognized": False, "retry": True}),
        Row("recognition", 2, props={"recognized": True, "retry": True}),
    ])
    assert report.total == 3
    assert report.retry_after_fail == 2


def test_heuristic_still_works_for_old_events():
    """У событий до 04.08.2026 `props.retry` нет — считаем по порядку событий."""
    report = _report([
        Row("recognition", 0, props={"recognized": False}),
        Row("recognition", 1, props={"recognized": True}),
    ])
    assert report.retry_after_fail == 1


def test_retry_is_not_abandonment():
    """Повтор после неудачи уходом не считается (§9)."""
    report = _report([
        Row("recognition", 0, props={"recognized": False, "retry": False}),
        Row("recognition", 1, props={"recognized": False, "retry": True}),
        Row("session_end", 2, props={"reason": "timeout"}),
    ])
    assert report.retry_after_fail == 1
    assert report.abandoned_after_fail == 1  # ушёл после ВТОРОЙ неудачи, не после первой


def test_visits_are_independent():
    """Разрыв больше таймаута — два визита; пометка одного не влияет на другой."""
    report = _report([
        Row("recognition", 0, session_id="s1", props={"recognized": False}),
        Row("recognition", 1, session_id="s1", props={"recognized": True}),
        Row("recognition", 0, session_id="s2", props={"recognized": False, "retry": False}),
        Row("recognition", 1, session_id="s2", props={"recognized": True, "retry": True}),
    ])
    assert report.total == 4
    assert report.retry_after_fail == 2  # по одному повтору в каждом визите


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
