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


def test_is_refusal_positive():
    """§4: отказ гида распознаётся по фразам-маркерам (для отчёта «вопросы без ответа»)."""
    assert gi.is_refusal("Не могу предоставить полный список экспонатов.")
    assert gi.is_refusal("К сожалению, у меня нет информации об этом предмете.")
    assert gi.is_refusal("В предоставленных материалах нет ответа на ваш вопрос.")
    assert gi.is_refusal("Не нашёл такой экспонат.")
    assert gi.is_refusal("Не нашел такой экспонат.")  # без «ё»
    assert gi.is_refusal("Уточните, что именно вас интересует.")


def test_is_refusal_negative():
    assert not gi.is_refusal("Яйцо «Коронационное» создано в 1897 году мастером Перхиным.")
    assert not gi.is_refusal("Оно находится в зале 3, витрина 2.")
    assert not gi.is_refusal("")


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
    # «экспонат12» без пробела НЕ должен разбираться как №2 (жадный \w съедал цифры).
    assert gi.parse_exhibit_number("экспонат12") is None


def test_is_navigational():
    assert gi.is_navigational("как найти это яйцо?")
    assert gi.is_navigational("В каком зале коронационное яйцо")
    assert gi.is_navigational("где находится Ротшильд")
    assert not gi.is_navigational("расскажи историю яйца")
    assert not gi.is_navigational("из чего оно сделано")
    # Провенанс/прошедшее время — не текущая навигация (иначе location противоречит ответу).
    assert not gi.is_navigational("где выставлялось это яйцо раньше")


def test_is_hall_listing():
    assert gi.is_hall_listing("какие залы есть в музее?")
    assert gi.is_hall_listing("какие есть залы")
    assert gi.is_hall_listing("сколько залов")
    assert gi.is_hall_listing("перечисли залы")
    assert gi.is_hall_listing("перечень залов")
    assert gi.is_hall_listing("список залов")
    assert gi.is_hall_listing("покажи залы")
    assert gi.is_hall_listing("все залы")
    assert gi.is_hall_listing("залы музея")
    assert not gi.is_hall_listing("что в этом зале")
    assert not gi.is_hall_listing("расскажи про яйцо")
    # Вопрос про ОДИН конкретный зал не должен триггерить дамп всех залов.
    assert not gi.is_hall_listing("что за зал 5?")


def test_is_hall_listing_with_fillers():
    """C25: слова-вставки между вопросительным словом и «залы» не должны ломать матч."""
    assert gi.is_hall_listing("А какие вообще есть залы")
    assert gi.is_hall_listing("сколько всего залов")
    assert gi.is_hall_listing("какие тут ещё залы")
    assert gi.is_hall_listing("сколько у вас залов?")
    assert gi.is_hall_listing("КАКИЕ ЗАЛЫ ЕСТЬ")  # регистр не важен


def test_is_hall_listing_not_hall_contents():
    """Вопрос про содержимое зала — не запрос перечня залов (иначе ложный дамп)."""
    assert not gi.is_hall_listing("какие экспонаты в зале 4")
    assert not gi.is_hall_listing("сколько экспонатов в зале")
    assert not gi.is_hall_listing("в каких залах есть яйца Фаберже")
    assert not gi.is_hall_listing("какие экспонаты вообще есть в зале 2")


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
