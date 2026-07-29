"""Юнит-тесты нормализации текста для озвучки (баг-репорт 28.07.2026, п.2).

Числительные: «Пётр I» должен звучать как «Пётр Первый», а не «Пётр один».
Чистые функции, БД и сеть не нужны. Запуск:
    python -m pytest tests/test_text_normalize.py    # если установлен pytest
    python tests/test_text_normalize.py              # standalone (без зависимостей)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.text_normalize import (  # noqa: E402
    has_numerals,
    normalize_for_tts,
    ordinal_word,
    roman_to_arabic,
)


def test_bug_report_table():
    """Таблица «должно звучать так» из баг-репорта."""
    assert normalize_for_tts("Пётр I основал Санкт-Петербург") == "Пётр Первый основал Санкт-Петербург"
    assert normalize_for_tts("Александр III") == "Александр Третий"
    assert normalize_for_tts("при Николае II") == "при Николае Втором"
    assert normalize_for_tts("XIX век") == "девятнадцатый век"
    # Арабские числа читались верно и раньше — не регрессить.
    assert normalize_for_tts("в 1885 году") == "в 1885 году"


def test_name_ordinals_cases():
    """Падеж порядкового выводится из окончания имени."""
    assert normalize_for_tts("Николай II") == "Николай Второй"
    assert normalize_for_tts("подарок Николая II") == "подарок Николая Второго"
    assert normalize_for_tts("подарен Николаю II") == "подарен Николаю Второму"
    assert normalize_for_tts("заказано Александром III") == "заказано Александром Третьим"
    assert normalize_for_tts("о Петре I") == "о Петре Первом"
    # Женский род: «Екатерина Вторая», а не «Второй».
    assert normalize_for_tts("Екатерина II") == "Екатерина Вторая"
    assert normalize_for_tts("при Екатерине II") == "при Екатерине Второй"


def test_century_ordinals():
    assert normalize_for_tts("в XIX веке") == "в девятнадцатом веке"
    assert normalize_for_tts("конца XVIII века") == "конца восемнадцатого века"
    assert normalize_for_tts("XX век") == "двадцатый век"
    assert normalize_for_tts("XXI столетие") == "двадцать первое столетие"
    assert normalize_for_tts("в XVII столетии") == "в семнадцатом столетии"


def test_no_numbers_untouched():
    text = "Яйцо «Ландыши» украшено жемчугом и розовыми бриллиантами."
    assert normalize_for_tts(text) == text
    assert not has_numerals(text)


def test_no_regression_on_plain_romans():
    """Римские вне «именного»/«векового» контекста по-прежнему → арабские
    (лучше, чем чтение по буквам)."""
    assert normalize_for_tts("глава XIV") == "глава 14"
    assert roman_to_arabic("XIX") == "19"
    # Заглавное слово-не-имя перед римским: остаётся номером, а не порядковым.
    assert normalize_for_tts("Витрина X") == "Витрина 10"
    assert normalize_for_tts("Глава XIV") == "Глава 14"
    # Слова, не являющиеся римским числом целиком, не затрагиваются.
    assert normalize_for_tts("VIVA и MIXER") == "VIVA и MIXER"


def test_arabic_regnal_number():
    """«Пётр 1» (так пишут в чате) тоже должен звучать порядковым."""
    assert normalize_for_tts("Пётр 1") == "Пётр Первый"
    assert normalize_for_tts("расскажи про Петра 1") == "расскажи про Петра Первого"
    # Но не всё подряд после заглавного слова: номера залов/витрин не трогаем.
    assert normalize_for_tts("Зал 5") == "Зал 5"
    assert normalize_for_tts("Витрина 3") == "Витрина 3"


def test_ordinal_word():
    assert ordinal_word(1) == "первый"
    assert ordinal_word(2) == "второй"
    assert ordinal_word(3, "prep") == "третьем"
    assert ordinal_word(8, "nom", "n") == "восьмое"
    assert ordinal_word(21) == "двадцать первый"
    assert ordinal_word(19, "prep") == "девятнадцатом"
    assert ordinal_word(0) is None
    assert ordinal_word(100) is None


def test_has_numerals():
    assert has_numerals("в 1885 году")
    assert has_numerals("Пётр I")
    assert not has_numerals("яйцо Фаберже")
    assert not has_numerals("")
    assert not has_numerals(None)


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
