"""Юнит-тесты нормализации текста (баг-репорт 28.07.2026, п.2 и 06.08.2026, п.9).

Два независимых блока одного модуля:
  • озвучка — числительные: «Пётр I» должен звучать как «Пётр Первый», а не «Пётр один»;
  • типографика каталога — прямые кавычки → «ёлочки», «пресс- папье» → «пресс-папье».
Строки для второго блока взяты ИЗ ПРОДА (слепок каталога 06.08.2026, 1253 карточки):
на выдуманных примерах правило легко сделать красивым и нерабочим.

Чистые функции, БД и сеть не нужны. Запуск:
    python -m pytest tests/test_text_normalize.py    # если установлен pytest
    python tests/test_text_normalize.py              # standalone (без зависимостей)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.text_normalize import (  # noqa: E402
    CHANGE_HYPHEN,
    CHANGE_INVISIBLE,
    CHANGE_QUOTES,
    CHANGE_SPACES,
    analyze_typography,
    fix_typography,
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


# ── Типографика каталога (баг-репорт 06.08.2026, п.9) ────────────────────────
# Все «прод-» строки ниже — дословно с прода 06.08.2026 (id указан в комментарии),
# чтобы тест ловил ровно те дефекты, которые заказчик видит в приложении.

def test_typography_prod_quotes():
    """30 названий с прямыми кавычками при «ёлочках» в остальном каталоге."""
    assert fix_typography('Браслет с львиными головами из серии "Керченские древности"') == (
        "Браслет с львиными головами из серии «Керченские древности»"
    )  # id=25 — пример из самого баг-репорта
    assert fix_typography('Брошь "Божья коровка"') == "Брошь «Божья коровка»"  # id=30
    assert fix_typography('Рама с молитвой "Отче наш"') == "Рама с молитвой «Отче наш»"  # id=173
    # id=100: кавычки внутри названия с дефисом — дефис не должен пострадать.
    assert fix_typography('Мастер-монограммист "FG"') == "Мастер-монограммист «FG»"
    # id=83: закрывающая кавычка перед скобкой, а не перед пробелом.
    assert fix_typography('Коробочка с эмалевой миниатюрой "Попались" (по рисунку С. Соломко)') == (
        "Коробочка с эмалевой миниатюрой «Попались» (по рисунку С. Соломко)"
    )


def test_typography_prod_nested_quotes():
    """id=423: вложенная кавычка. Правило «первая открывающая, вторая закрывающая» тут врёт."""
    assert fix_typography(
        'Складень трёхстворчатый "Богоматерь Казанская (в возглавии "Спас Нерукотворный"), '
        'Великомученица Екатерина, Св. Алексий, человек Божий"'
    ) == (
        "Складень трёхстворчатый «Богоматерь Казанская (в возглавии „Спас Нерукотворный“), "
        "Великомученица Екатерина, Св. Алексий, человек Божий»"
    )


def test_typography_prod_hyphen():
    """Все три реальных названия с разорванным дефисом: id=358, id=359, id=901."""
    assert fix_typography(
        "Письменный прибор (поднос, чернильница, пресс- папье, марочница, коробочка для перьев, "
        "нож, ручка, держатель для карандаша)"
    ) == (
        "Письменный прибор (поднос, чернильница, пресс-папье, марочница, коробочка для перьев, "
        "нож, ручка, держатель для карандаша)"
    )
    assert fix_typography(
        "Письменный прибор (поднос, чернильница, пресс- папье, марочница, коробочка для перьев, "
        "нож, ручка, подставка для бумаги)"
    ) == (
        "Письменный прибор (поднос, чернильница, пресс-папье, марочница, коробочка для перьев, "
        "нож, ручка, подставка для бумаги)"
    )
    # id=901 — составное имя собственное: справа заглавная буква, а не строчная.
    assert fix_typography("Тарелки из серии с печатными видами Санкт- Петербурга и пригородов") == (
        "Тарелки из серии с печатными видами Санкт-Петербурга и пригородов"
    )


def test_typography_prod_invisible_mark():
    """id=81: невидимый U+200E внутри названия — карточка не находилась поиском."""
    result = analyze_typography("Колье-браслет из серии «‎Морозные узоры»")
    assert result.text == "Колье-браслет из серии «Морозные узоры»"
    assert result.changes == (CHANGE_INVISIBLE,)


def test_typography_unpaired_quote_untouched():
    """Непарную кавычку НЕ трогаем вовсе — строка уходит в «требует глаз»."""
    for text in ('Незакрытая "кавычка', 'закрывающая" одна', 'дюймы 5" трубы'):
        result = analyze_typography(text)
        assert result.text == text, text          # ни одного символа не поменяли
        assert result.changes == ()
        assert result.needs_review is True, text


def test_typography_dashes_not_touched():
    """Тире в перечислении и отдельно стоящий дефис — это не «разорванное слово»."""
    for text in (
        "поднос, чернильница — предметы одного набора",
        "что-то - и дальше",
        "Портсигар – подарок императрицы",
    ):
        assert fix_typography(text) == text, text
    # Уже стоящие «ёлочки» и „лапки“ не ломаем.
    assert fix_typography("Яйцо «Ландыши»") == "Яйцо «Ландыши»"
    assert fix_typography("Ковш «Гордая „малая“»") == "Ковш «Гордая „малая“»"


def test_typography_spaces():
    assert fix_typography("два  пробела   подряд") == "два пробела подряд"
    assert fix_typography("  хвост и края  ") == "хвост и края"
    # Перевод строки — структура текста описания, схлопывать его нельзя.
    assert fix_typography("первая строка  \nвторая  строка") == "первая строка\nвторая строка"


def test_typography_changes_report():
    """Скрипт сводит отчёт «кавычки: N, дефис: N, пробелы: N» по этим меткам."""
    result = analyze_typography('  Прибор "Малый" с пресс- папье  и хвостом  ')
    assert result.text == "Прибор «Малый» с пресс-папье и хвостом"
    assert set(result.changes) == {CHANGE_QUOTES, CHANGE_HYPHEN, CHANGE_SPACES}
    assert result.needs_review is False
    # Чистая строка — пустой список правок, скрипт её в план не возьмёт.
    clean = analyze_typography("Яйцо «Ландыши»")
    assert clean.changes == () and clean.text == "Яйцо «Ландыши»"


def test_typography_idempotent():
    """f(f(x)) == f(x): повторный прогон скрипта по каталогу находит ноль замен."""
    samples = [
        'Икона "Богоматерь Иверская"',                     # id=64
        "Тарелки из серии с печатными видами Санкт- Петербурга и пригородов",   # id=901
        "Колье-браслет из серии «‎Морозные узоры»",   # id=81
        '  Прибор "Малый" с пресс- папье  ',
        'Незакрытая "кавычка',
        "Яйцо «Ландыши»",
    ]
    for text in samples:
        once = fix_typography(text)
        assert fix_typography(once) == once, text


def test_typography_empty_and_none():
    assert fix_typography("") == ""
    assert fix_typography(None) is None
    assert analyze_typography(None).changes == ()
    assert analyze_typography(None).needs_review is False


def test_typography_and_tts_do_not_mix():
    """Два блока модуля независимы: типографика не трогает числа, озвучка — кавычки."""
    assert fix_typography("Пётр I основал Санкт-Петербург") == "Пётр I основал Санкт-Петербург"
    assert fix_typography("XIX век") == "XIX век"
    assert normalize_for_tts('Икона "Спас Нерукотворный"') == 'Икона "Спас Нерукотворный"'


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
