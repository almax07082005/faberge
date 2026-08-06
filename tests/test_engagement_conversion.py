"""Юнит-тесты базы конверсий визита (п.7 баг-репорта 06.08.2026).

Проверяется главное требование: числитель и знаменатель берутся из ОДНОЙ выборки
визитов, поэтому доля не может оказаться больше 100%. До правки числители
считались по всем визитам, а знаменателем брались визиты с `app_open` — и визит
с `chat_open` без `app_open` (данные до 04.08.2026, потерянное событие) завышал
конверсию.

Чистые функции, БД не нужна. Запуск:
    python -m pytest tests/test_engagement_conversion.py    # если установлен pytest
    python tests/test_engagement_conversion.py              # standalone (без зависимостей)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import visits


def visit(app_open: bool = False, chat: bool = False, question: bool = False) -> visits.VisitFlags:
    """Короткая запись одного визита: какие признаки в нём были."""
    return visits.VisitFlags(has_app_open=app_open, has_chat_open=chat, has_question=question)


def test_no_app_open_falls_back_to_all_visits():
    """Данные до 04.08.2026: `app_open` не приходил — базой становятся все визиты."""
    stats = visits.conversion_stats([visit(chat=True), visit(), visit(), visit()])
    assert stats.basis == visits.CONVERSION_BASIS_ALL_VISITS
    assert stats.denominator == 4
    assert stats.chat_numerator == 1
    assert stats.chat_rate == 0.25


def test_app_open_becomes_basis_and_visit_without_it_is_excluded():
    """Ключевой случай: визит с чатом, но без `app_open`, в числитель не идёт."""
    stats = visits.conversion_stats([
        visit(app_open=True, chat=True),
        visit(app_open=True),
        visit(chat=True),                 # старый визит: чат был, `app_open` не долетел
    ])
    assert stats.basis == visits.CONVERSION_BASIS_APP_OPEN
    assert stats.denominator == 2          # в базе только визиты с `app_open`
    assert stats.visits_with_chat == 2     # «всего» по-прежнему считает все визиты
    assert stats.chat_numerator == 1       # а в долю попал только визит из базы
    assert stats.chat_rate == 0.5          # прежняя формула дала бы 2/2 = 100%


def test_rate_never_exceeds_one_on_mixed_data():
    """Смесь «старые визиты без app_open + новые с ним» — доля обязана остаться ≤ 1.0.

    Именно на такой смеси прежняя формула и ломалась: 12 визитов с чатом делились
    на 2 визита с `app_open` и давали 600%.
    """
    flags = [visit(chat=True, question=True) for _ in range(10)]          # до 04.08.2026
    flags += [visit(app_open=True, chat=True, question=True) for _ in range(2)]  # после
    stats = visits.conversion_stats(flags)

    assert stats.basis == visits.CONVERSION_BASIS_APP_OPEN
    assert stats.visits_with_chat == 12 and stats.visits_with_questions == 12
    assert stats.chat_rate <= 1.0 and stats.question_rate <= 1.0
    assert stats.chat_rate == 1.0          # оба визита базы дошли до чата
    # Прежняя формула: числитель по всем визитам, знаменатель по app_open.
    assert stats.visits_with_chat / stats.denominator == 6.0


def test_numerator_is_always_subset_of_denominator():
    """Инвариант на разнородных наборах: числитель ≤ знаменателя, доля ≤ 1.0."""
    combos = [
        [],
        [visit()],
        [visit(chat=True)],
        [visit(app_open=True)],
        [visit(app_open=True, chat=True, question=True), visit(chat=True), visit(question=True)],
        [visit(app_open=True, question=True), visit(app_open=True), visit(), visit(chat=True)],
    ]
    for flags in combos:
        stats = visits.conversion_stats(flags)
        assert stats.chat_numerator <= stats.denominator, flags
        assert stats.question_numerator <= stats.denominator, flags
        assert 0.0 <= stats.chat_rate <= 1.0, flags
        assert 0.0 <= stats.question_rate <= 1.0, flags


def test_no_visits_gives_zeros_without_division_error():
    """Период без визитов — нули, а не ZeroDivisionError."""
    stats = visits.conversion_stats([])
    assert stats.total_visits == 0
    assert stats.denominator == 0
    assert stats.basis == visits.CONVERSION_BASIS_ALL_VISITS
    assert stats.chat_rate == 0.0 and stats.question_rate == 0.0


def test_denominator_matches_what_was_actually_divided_by():
    """`conversion_denominator` — ровно то число, на которое делили (иначе цифру не проверить)."""
    flags = [
        visit(app_open=True, chat=True, question=True),
        visit(app_open=True, chat=True),
        visit(app_open=True),
        visit(chat=True),
    ]
    stats = visits.conversion_stats(flags)
    assert stats.denominator == 3
    assert stats.chat_rate == round(stats.chat_numerator / stats.denominator, 4)
    assert stats.question_rate == round(stats.question_numerator / stats.denominator, 4)


def test_all_visits_basis_keeps_totals_and_numerators_equal():
    """При базе «все визиты» «всего» и числитель совпадают — расхождения быть не должно."""
    stats = visits.conversion_stats([visit(chat=True), visit(question=True), visit()])
    assert stats.basis == visits.CONVERSION_BASIS_ALL_VISITS
    assert stats.chat_numerator == stats.visits_with_chat
    assert stats.question_numerator == stats.visits_with_questions
    assert stats.denominator == stats.total_visits


def test_question_conversion_counts_visits_not_messages():
    """Конверсия в вопрос — доля ВИЗИТОВ с вопросом, а не число реплик."""
    stats = visits.conversion_stats([
        visit(app_open=True, question=True),   # хоть десять сообщений — визит один
        visit(app_open=True),
    ])
    assert stats.question_numerator == 1
    assert stats.question_rate == 0.5


def test_schema_exposes_basis_and_denominator():
    """Ручка отдаёт базу явно: заказчик должен видеть, от чего считалась доля."""
    from app import schemas as sch

    payload = sch.AnalyticsEngagement(
        total_visits=10,
        sessions_with_chat=5,
        sessions_with_app_open=8,
        conversion_basis="app_open",
        conversion_denominator=8,
        chat_conversion_numerator=4,
        chat_conversion_rate=0.5,
    ).model_dump(by_alias=True)
    assert payload["conversion_basis"] == "app_open"
    assert payload["conversion_denominator"] == 8
    assert payload["chat_conversion_numerator"] == 4


def test_schema_restores_basis_for_reports_cached_before_the_fix():
    """Кэш агрегатов живёт сутки: старый payload не должен отдавать ни знаменатель 0, ни долю >100%."""
    from app import schemas as sch

    # Запись старого формата: числитель посчитан по всем визитам, а сохранённая
    # доля — уже поделённая на визиты с app_open, то есть 120%.
    legacy = {"total_visits": 30, "sessions_with_app_open": 10, "sessions_with_chat": 12,
              "sessions_with_questions": 4, "chat_conversion_rate": 1.2}
    restored = sch.AnalyticsEngagement.model_validate(legacy)
    # Числители в такой записи — по всем визитам, значит и база у неё все визиты.
    assert restored.conversion_basis == "all_visits"
    assert restored.conversion_denominator == 30
    assert restored.chat_conversion_numerator == 12
    # Доля пересчитана от базы, а не взята из кэша: 12/30, а не сохранённые 1.2.
    assert restored.chat_conversion_rate == 0.4
    assert restored.question_conversion_rate == round(4 / 30, 4)
    assert restored.chat_conversion_rate <= 1.0

    legacy_without_app_open = {"total_visits": 30, "sessions_with_app_open": 0, "sessions_with_chat": 3}
    restored = sch.AnalyticsEngagement.model_validate(legacy_without_app_open)
    assert restored.conversion_basis == "all_visits"
    assert restored.conversion_denominator == 30

    # Пустой период старого формата не должен ронять валидацию делением на ноль.
    empty = sch.AnalyticsEngagement.model_validate({"total_visits": 0, "sessions_with_chat": 0})
    assert empty.conversion_denominator == 0
    assert empty.chat_conversion_rate == 0.0


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
