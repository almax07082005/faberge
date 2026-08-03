"""Разбиение потока событий сессии на визиты по таймауту неактивности (§5 ТЗ 03.08.2026).

Зачем. По требованию заказчика визит завершается `session_end` после 30 минут
неактивности, и от этого считается длительность посещения. Прежний расчёт брал
`MAX(ts) - MIN(ts)` по `session_id` — пока фронт не шлёт `session_end`, это
работает, но даёт дыру: посетитель, открывший приложение утром и вернувшийся к
той же вкладке через четыре часа, превращается в один сплошной визит на четыре
часа и портит среднее по музею.

Поэтому длительность считается не по сессии, а по ВИЗИТАМ: поток событий сессии
режется там, где между соседними событиями прошло больше `SESSION_TIMEOUT_MINUTES`.
Это серверная страховка — она не зависит от того, дошёл ли `session_end` (вкладку
на телефоне могли просто убить), и работает одинаково для старых и новых данных.

Функции чистые (БД не нужна) — на них есть юнит-тест tests/test_visits.py.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable, Iterator, List, Optional, Sequence, Tuple

from ..config import settings


def _ts_of(item: Any):
    return item.ts


def _session_of(item: Any):
    return item.session_id


def split_visits(
    events: Sequence[Any],
    timeout_minutes: Optional[int] = None,
    ts_of: Callable[[Any], Any] = _ts_of,
) -> List[List[Any]]:
    """Разрезать хронологически упорядоченные события ОДНОЙ сессии на визиты.

    Визит обрывается, когда пауза до следующего события больше таймаута:
    сессия с разрывом в 4 часа даёт два визита, а не один на 4 часа.

    :param events: события одной сессии, упорядоченные по времени.
    :param timeout_minutes: таймаут неактивности; по умолчанию —
        ``SESSION_TIMEOUT_MINUTES`` из конфигурации (30 минут).
    """
    if not events:
        return []
    minutes = settings.session_timeout_minutes if timeout_minutes is None else timeout_minutes
    gap = timedelta(minutes=minutes)

    visits: List[List[Any]] = [[events[0]]]
    for prev, current in zip(events, events[1:]):
        if ts_of(current) - ts_of(prev) > gap:
            visits.append([current])
        else:
            visits[-1].append(current)
    return visits


def visits_by_session(
    rows: Sequence[Any],
    timeout_minutes: Optional[int] = None,
    session_of: Callable[[Any], Any] = _session_of,
    ts_of: Callable[[Any], Any] = _ts_of,
) -> Iterator[Tuple[Any, List[Any]]]:
    """Пройти строки, упорядоченные по (session_id, ts), и выдать визиты.

    Один проход без словаря на всю выборку: строки уже сгруппированы БД, поэтому
    достаточно накапливать текущую сессию и отдавать её визиты при смене ключа.

    :return: пары «session_id, события визита».
    """
    current_session: Any = None
    buffer: List[Any] = []

    def flush() -> Iterator[Tuple[Any, List[Any]]]:
        for visit in split_visits(buffer, timeout_minutes, ts_of):
            yield current_session, visit

    for row in rows:
        session_id = session_of(row)
        if buffer and session_id != current_session:
            yield from flush()
            buffer = []
        current_session = session_id
        buffer.append(row)
    if buffer:
        yield from flush()
