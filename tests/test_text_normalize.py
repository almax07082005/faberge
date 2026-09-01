"""Юнит-тесты текстовых функций (баг-репорты 28.07.2026 п.2, 06.08.2026 п.9, 31.08.2026 п. I-3).

Три независимых блока одного модуля:
  • озвучка — числительные: «Пётр I» должен звучать как «Пётр Первый», а не «Пётр один»;
  • типографика каталога — прямые кавычки → «ёлочки», «пресс- папье» → «пресс-папье»;
  • обрезка по границе предложения — общая для промпта диалога гида и превью
    описания зала (переезд из `llm._shorten` 31.08.2026).
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
    shorten_to_sentence,
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


# ── Обрезка по границе предложения (31.08.2026, п. I-3) ──────────────────────
def test_shorten_cuts_on_sentence_boundary():
    """Режем по концу фразы, а не по символу — на всех четырёх границах."""
    assert shorten_to_sentence("Первое предложение. Второе предложение.", 25) == "Первое предложение."
    assert shorten_to_sentence("Какая красота! А вот и второе предложение.", 25) == "Какая красота!"
    assert shorten_to_sentence("Что здесь спрятано? Внутри яйца сюрприз.", 25) == "Что здесь спрятано?"
    assert shorten_to_sentence("Материал: золото; техника — гильоше и эмаль по золоту.", 30) == (
        "Материал: золото;"
    )


def test_shorten_falls_back_to_word_when_boundary_is_too_early():
    """Граница фразы у самого начала — режем по слову и честно ставим «…».

    Это и есть смысл min_ratio: для промпта гида полфразы контекста хуже, чем
    оборванное слово, а для превью зала — наоборот, поэтому порог параметр.
    """
    text = "Совсем коротко. " + "длинное продолжение " * 5
    at_half = shorten_to_sentence(text, 60)
    assert at_half.endswith("…")
    # Ничего не дописано: превью — честный префикс исходного текста плюс «…».
    assert text.startswith(at_half[:-1]) and len(at_half) <= 61
    # Тот же текст и тот же лимит, но порог ниже — теперь короткая фраза годится.
    assert shorten_to_sentence(text, 60, min_ratio=1 / 5) == "Совсем коротко."


def test_shorten_default_ratio_repeats_previous_prompt_behaviour():
    """Дефолт 0.5 — ровно прежний порог `cut >= limit // 2` из `llm._shorten`.

    Обрезка справки уходит в оплачиваемый промпт диалога, и переезд функции не
    должен был изменить ни одного знака в нём.
    """
    for limit in range(1, 1000):
        assert int(limit * 0.5) == limit // 2, limit


def test_shorten_short_text_is_returned_whole():
    assert shorten_to_sentence("Короткий текст.", 100) == "Короткий текст."
    # Вход стрипается: описание с хвостовым пробелом не должно считаться длиннее.
    assert shorten_to_sentence("  Короткий текст.  ", 100) == "Короткий текст."


def test_shorten_empty_text():
    assert shorten_to_sentence("", 10) == ""
    assert shorten_to_sentence(None, 10) == ""
    assert shorten_to_sentence("   ", 10) == ""


def test_shorten_zero_limit_disables_cutting():
    """`limit <= 0` — обрезка выключена: текст отдаётся целиком, без «…»."""
    text = "Первое предложение. Второе предложение."
    assert shorten_to_sentence(text, 0) == text
    assert shorten_to_sentence(text, -100) == text


def test_llm_shorten_is_the_same_function():
    """`llm._shorten` остался алиасом — иначе разъедутся промпт и превью зала."""
    try:
        from app.services import llm
    except ImportError:  # standalone-прогон без httpx — проверять нечего
        return
    assert llm._shorten is shorten_to_sentence


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
