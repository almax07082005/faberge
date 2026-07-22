"""Юнит-тесты разбора намерения реплики гида (B7/B9/B10).

Чистые функции, БД не нужна. Запуск:
    python -m pytest tests/test_guide_intel.py      # если установлен pytest
    python tests/test_guide_intel.py                # standalone (без зависимостей)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import guide_intel as gi


def test_parse_exhibit_number_positive():
    assert gi.parse_exhibit_number("12") == "12"
    assert gi.parse_exhibit_number("№ 12") == "12"
    assert gi.parse_exhibit_number("номер 7") == "7"
    assert gi.parse_exhibit_number("экспонат 42") == "42"
    assert gi.parse_exhibit_number("  12а ") == "12а"
    assert gi.parse_exhibit_number("№3?") == "3"


def test_parse_exhibit_number_negative():
    assert gi.parse_exhibit_number("расскажи про яйцо") is None
    assert gi.parse_exhibit_number("что в зале 3 интересного") is None
    assert gi.parse_exhibit_number("") is None
    assert gi.parse_exhibit_number("яйцо 1885 года") is None


def test_is_navigational():
    assert gi.is_navigational("как найти это яйцо?")
    assert gi.is_navigational("В каком зале коронационное яйцо")
    assert gi.is_navigational("где находится Ротшильд")
    assert not gi.is_navigational("расскажи историю яйца")
    assert not gi.is_navigational("из чего оно сделано")


def test_is_hall_listing():
    assert gi.is_hall_listing("какие залы есть в музее?")
    assert gi.is_hall_listing("сколько залов")
    assert gi.is_hall_listing("перечисли залы")
    assert not gi.is_hall_listing("что в этом зале")
    assert not gi.is_hall_listing("расскажи про яйцо")


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
