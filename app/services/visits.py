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

Здесь же живёт расчёт конверсий визита (п.7 баг-репорта 06.08.2026) — он тоже
про «что случилось внутри визита» и тоже должен проверяться без БД.

Функции чистые (БД не нужна) — на них есть юнит-тесты tests/test_visits.py и
tests/test_engagement_conversion.py.
"""
from __future__ import annotations

from dataclasses import dataclass
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


# ── Конверсии визита: числитель и знаменатель из одной выборки (п.7 БР 06.08.2026) ──
#
# Было: числители (визиты с `chat_open`, визиты с вопросом) считались по ВСЕМ визитам,
# а знаменателем брались визиты с `app_open` — `denominator = with_app_open or total`.
# Фронт шлёт `app_open` с 04.08.2026, то есть знаменатель уже переключился на новую
# базу, а числитель остался старым. Любой визит с `chat_open`, но без `app_open`
# (данные до 04.08, потерянное событие, вход по прямой ссылке на экспонат) завышал
# долю — вплоть до значений больше 100%. Именно это и был вопрос заказчика
# «не занижена/не завышена ли конверсия».
#
# Стало: база выбирается один раз, и числитель считается ТОЛЬКО внутри неё. Доля
# по построению не может превысить 1.0, потому что числитель — подмножество базы.
CONVERSION_BASIS_APP_OPEN = "app_open"
CONVERSION_BASIS_ALL_VISITS = "all_visits"


@dataclass(frozen=True)
class VisitFlags:
    """Признаки одного визита, от которых зависят конверсии.

    Сознательно не строка событий, а три булевых флага: расчёт базы не должен
    зависеть ни от модели `Event`, ни от БД — тогда его видно целиком в юнит-тесте.
    """

    has_app_open: bool = False      # в визите был `app_open` (фронт шлёт с 04.08.2026)
    has_chat_open: bool = False     # был `chat_open` — «дошёл до диалога с гидом»
    has_question: bool = False      # был хотя бы один `chat_message`


@dataclass(frozen=True)
class ConversionStats:
    """Счётчики конверсий с явно названной базой.

    Пары «всего» и «в базе» разведены намеренно: `visits_with_chat` — это визитов
    с `chat_open` ВСЕГО (на этом поле уже завязан фронт, менять его смысл нельзя),
    а `chat_numerator` — только те из них, что попали в базу, и именно он делится
    на `denominator`. При базе `all_visits` пары совпадают.
    """

    total_visits: int
    visits_with_app_open: int
    visits_with_chat: int
    visits_with_questions: int
    basis: str                      # CONVERSION_BASIS_APP_OPEN | CONVERSION_BASIS_ALL_VISITS
    denominator: int
    chat_numerator: int
    question_numerator: int
    chat_rate: float
    question_rate: float


def _share(part: int, whole: int) -> float:
    """Доля с тем же округлением, что и остальные доли отчётов (`crud._rate`).

    Пустая база — 0.0, а не деление на ноль: период без визитов это нормальный
    ответ отчёта, а не ошибка.
    """
    return round(part / whole, 4) if whole else 0.0


def conversion_stats(flags: Sequence[VisitFlags]) -> ConversionStats:
    """Посчитать конверсии визита от согласованной базы.

    База — визиты с `app_open`, если такие события в периоде вообще есть; иначе
    (данные до 04.08.2026, когда фронт `app_open` ещё не слал) базой становятся все
    визиты, чтобы метрика не обнулилась при живом трафике.

    ВАЖНО для сравнения периодов: при смене базы доли до и после 04.08.2026
    несопоставимы — «все визиты» это более широкий знаменатель, чем «визиты с
    app_open», поэтому после переключения доля обычно подрастает. Отдавать
    заказчику нужно вместе с `basis`/`denominator`, а не одним числом.
    """
    total = len(flags)
    with_app_open = sum(1 for f in flags if f.has_app_open)
    with_chat = sum(1 for f in flags if f.has_chat_open)
    with_questions = sum(1 for f in flags if f.has_question)

    if with_app_open:
        basis = CONVERSION_BASIS_APP_OPEN
        in_basis: Sequence[VisitFlags] = [f for f in flags if f.has_app_open]
    else:
        basis = CONVERSION_BASIS_ALL_VISITS
        in_basis = flags

    denominator = len(in_basis)
    chat_numerator = sum(1 for f in in_basis if f.has_chat_open)
    question_numerator = sum(1 for f in in_basis if f.has_question)

    return ConversionStats(
        total_visits=total,
        visits_with_app_open=with_app_open,
        visits_with_chat=with_chat,
        visits_with_questions=with_questions,
        basis=basis,
        denominator=denominator,
        chat_numerator=chat_numerator,
        question_numerator=question_numerator,
        chat_rate=_share(chat_numerator, denominator),
        question_rate=_share(question_numerator, denominator),
    )
