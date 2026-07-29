"""Нормализация текста перед отправкой в синтез речи.

Главная задача — числительные. SpeechKit читает «XIX век» по буквам
(«икс-и-икс»), а «Александр III» — как «Александр Ай-Ай-Ай». Прежняя версия
модуля лечила это заменой римских на арабские, но тем самым порождала баг из
баг-репорта: «Пётр I» → «Пётр 1» → синтезатор читает КОЛИЧЕСТВЕННЫМ числительным
«Пётр один» вместо «Пётр Первый», а «XIX век» → «19 век» → «девятнадцать век».

Поэтому римские раскрываются в ПОРЯДКОВЫЕ ЧИСЛИТЕЛЬНЫЕ СЛОВАМИ там, где по
контексту это порядковое:

  • после личного имени  — «Пётр I» → «Пётр Первый», «при Николае II» → «при
    Николае Втором» (падеж выводится из окончания имени);
  • перед словом «век» / «столетие» — «XIX век» → «девятнадцатый век»,
    «в XIX веке» → «в девятнадцатом веке».

Остальные римские (не в этих контекстах) по-прежнему становятся арабскими —
это лучше, чем чтение по буквам. Арабские числа не трогаем: «в 1885 году»
SpeechKit читает верно сам; исключение — номер монарха («Пётр 1»), который без
нас прочитался бы количественным.

Это ДЕТЕРМИНИРОВАННЫЙ минимум — фолбэк на случай, когда LLM (llm.to_spoken_text,
он ставит числа в нужный падеж куда точнее) не настроен или недоступен.
"""
from __future__ import annotations

import re
from typing import Optional

# ── Римские числа ────────────────────────────────────────────────────────────
# Канонический римский numeral (1..3999), только заглавные латинские буквы —
# именно так в текстах пишут века и порядковые имена монархов. Соседи слева и
# справа не должны быть буквами (ни латиница, ни кириллица), иначе зацепим часть
# слова вроде «VIVA» или «MIXER». Lookahead [IVXLCDM] отсекает пустые совпадения.
_ROMAN_CORE = r"M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"
_NOT_LETTER_BEFORE = r"(?<![A-Za-zА-Яа-яЁё])"
_NOT_LETTER_AFTER = r"(?![A-Za-zА-Яа-яЁё])"

_ROMAN_RE = re.compile(_NOT_LETTER_BEFORE + r"(?=[IVXLCDM])" + _ROMAN_CORE + _NOT_LETTER_AFTER)

_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def _roman_to_int(roman: str) -> int:
    total = 0
    prev = 0
    for ch in reversed(roman):
        value = _ROMAN_VALUES[ch]
        if value < prev:
            total -= value
        else:
            total += value
            prev = value
    return total


def roman_to_arabic(text: str) -> str:
    """Заменяет отдельные римские числа в тексте на арабские.

    Затрагивает только самостоятельные токены из заглавных латинских I/V/X/L/C/D/M,
    являющиеся корректным римским числом. Кириллический текст и обычные слова не
    трогает.
    """
    if not text:
        return text
    return _ROMAN_RE.sub(lambda m: str(_roman_to_int(m.group(0))), text)


# ── Порядковые числительные словами ──────────────────────────────────────────
# Основы порядковых 1..20 и 30 (номера монархов и века дальше не заходят).
_ORDINAL_STEMS = {
    1: "перв", 2: "втор", 3: "трет", 4: "четвёрт", 5: "пят", 6: "шест", 7: "седьм",
    8: "восьм", 9: "девят", 10: "десят", 11: "одиннадцат", 12: "двенадцат",
    13: "тринадцат", 14: "четырнадцат", 15: "пятнадцат", 16: "шестнадцат",
    17: "семнадцат", 18: "восемнадцат", 19: "девятнадцат", 20: "двадцат", 30: "тридцат",
}
# Числительные с ударным окончанием: «второй», «шестой», «седьмой», «восьмой»
# (остальные — «первый», «пятый», …). Различие только в им. п. мужского рода.
_STRESSED_NOMINATIVE = frozenset({2, 6, 7, 8})

# Падежи, которыми оперируем: nom (им.), gen (род./вин. одуш.), dat (дат.),
# ins (тв.), prep (пред.). Женский род сворачивается в три формы: nom / acc / obl.
_ENDINGS = {
    ("m", "nom"): "ый", ("m", "gen"): "ого", ("m", "dat"): "ому", ("m", "ins"): "ым", ("m", "prep"): "ом",
    ("n", "nom"): "ое", ("n", "gen"): "ого", ("n", "dat"): "ому", ("n", "ins"): "ым", ("n", "prep"): "ом",
    ("f", "nom"): "ая", ("f", "acc"): "ую", ("f", "obl"): "ой",
}
# «Третий» — мягкое склонение, отдельная таблица.
_THIRD = {
    ("m", "nom"): "третий", ("m", "gen"): "третьего", ("m", "dat"): "третьему",
    ("m", "ins"): "третьим", ("m", "prep"): "третьем",
    ("n", "nom"): "третье", ("n", "gen"): "третьего", ("n", "dat"): "третьему",
    ("n", "ins"): "третьим", ("n", "prep"): "третьем",
    ("f", "nom"): "третья", ("f", "acc"): "третью", ("f", "obl"): "третьей",
}


def ordinal_word(value: int, case: str = "nom", gender: str = "m") -> Optional[str]:
    """Порядковое числительное словами: (2, 'prep', 'm') → «втором».

    Возвращает ``None`` для чисел вне поддерживаемого диапазона (1..39) — тогда
    вызывающий код оставляет число как есть (или переводит в арабское).
    """
    if value < 1 or value > 39:
        return None
    prefix = ""
    if value > 20 and value % 10:  # 21..29, 31..39 — склоняется только последнее слово
        tens, value = value - value % 10, value % 10
        prefix = ("двадцать " if tens == 20 else "тридцать ")
    stem = _ORDINAL_STEMS.get(value)
    if stem is None:
        return None
    if value == 3:
        form = _THIRD.get((gender, case))
        return prefix + form if form else None
    ending = _ENDINGS.get((gender, case))
    if ending is None:
        return None
    if case == "nom" and gender == "m" and value in _STRESSED_NOMINATIVE:
        ending = "ой"
    return prefix + stem + ending


# ── Контекст 1: век / столетие ───────────────────────────────────────────────
# Падеж (и род) порядкового берём из формы самого слова: «в XIX веке» → «в
# девятнадцатом веке», «конца XVIII века» → «конца восемнадцатого века».
# Формы множественного числа («веков») намеренно не поддержаны — там нужно
# порядковое во мн. ч.; такие римские просто станут арабскими, как раньше.
_CENTURY_FORMS = {
    "век": ("nom", "m"), "века": ("gen", "m"), "веку": ("dat", "m"),
    "веком": ("ins", "m"), "веке": ("prep", "m"),
    "столетие": ("nom", "n"), "столетия": ("gen", "n"), "столетию": ("dat", "n"),
    "столетием": ("ins", "n"), "столетии": ("prep", "n"),
}
_CENTURY_RE = re.compile(
    _NOT_LETTER_BEFORE + r"(?=[IVXLCDM])(" + _ROMAN_CORE + r")" + _NOT_LETTER_AFTER
    + r"(\s+)([А-Яа-яЁё]+)"
)


def _sub_centuries(text: str) -> str:
    def repl(m: re.Match) -> str:
        forms = _CENTURY_FORMS.get(m.group(3).lower())
        if forms is None:
            return m.group(0)
        word = ordinal_word(_roman_to_int(m.group(1)), *forms)
        return m.group(0) if word is None else f"{word}{m.group(2)}{m.group(3)}"

    return _CENTURY_RE.sub(repl, text)


# ── Контекст 2: номер при личном имени ───────────────────────────────────────
# Имена, при которых число — это порядковый номер правителя. Список нужен, чтобы
# (а) не превращать в порядковое всё подряд после заглавного слова, когда номер
# записан арабскими («Зал 5» должен остаться «Зал 5»), и (б) определить род:
# «Екатерина II» → «Екатерина Вторая», а не «Второй».
_FEMININE_NAME_STEMS = (
    "екатерин", "елизавет", "анн", "мари", "виктори", "ольг", "софь", "софи",
    "изабелл", "маргарит",
)
_MONARCH_NAME_STEMS = _FEMININE_NAME_STEMS + (
    "пётр", "петр", "александр", "никола", "павел", "павл", "иван", "алексе",
    "фёдор", "федор", "михаил", "константин", "борис", "дмитри", "димитри",
    "людовик", "карл", "генрих", "георг", "эдуард", "вильгельм", "наполеон",
    "фридрих", "филипп", "ричард", "яков", "густав", "кристиан", "христиан",
)


# Заглавные слова, после которых число — это НОМЕР, а не порядковый при имени:
# «Витрина X» должно остаться «Витрина 10», а не стать «Витрина Десятый».
_NON_NAME_WORDS = frozenset({
    "зал", "зале", "зала", "витрина", "витрине", "витрины", "глава", "главе", "главы",
    "том", "томе", "тома", "часть", "части", "раздел", "разделе", "рис", "рисунок",
    "таблица", "таблице", "этаж", "этаже", "стенд", "стенде", "экспонат", "экспоната",
})


def _name_gender(name_low: str) -> str:
    return "f" if name_low.startswith(_FEMININE_NAME_STEMS) else "m"


def _case_from_name(name_low: str, gender: str) -> str:
    """Падеж порядкового выводим из окончания имени: «при Николае» → пред. падеж.

    Грубая, но устойчивая эвристика по склонению личных имён. Для мужских имён
    порядок проверок важен: «Николаем» — тв. п. (-ем), а не пред. (-е).
    """
    if gender == "f":
        if name_low.endswith("у"):                       # «Екатерину» — вин. п.
            return "acc"
        if name_low.endswith(("ы", "и", "е", "ой", "ей")):  # род./дат./пред./тв.
            return "obl"
        return "nom"
    if name_low.endswith(("ом", "ем", "ым")):            # «Петром», «Николаем»
        return "ins"
    if name_low.endswith(("у", "ю")):                    # «Петру», «Николаю»
        return "dat"
    if name_low.endswith(("а", "я")):                    # «Петра», «Николая»
        return "gen"
    if name_low.endswith("е"):                           # «Петре», «Николае»
        return "prep"
    return "nom"                                          # «Пётр», «Николай»


def _ordinal_after_name(name: str, value: int) -> Optional[str]:
    low = name.lower()
    if low in _NON_NAME_WORDS:
        return None
    gender = _name_gender(low)
    word = ordinal_word(value, _case_from_name(low, gender), gender)
    return word[0].upper() + word[1:] if word else None


# Заглавное кириллическое слово + римское число: «Пётр I», «Александр III».
# Значение ограничиваем 30, чтобы «Витрина D» не стала «Витрина Пятисотой».
_NAME_ROMAN_RE = re.compile(
    r"([А-ЯЁ][а-яё]+)(\s+)(?=[IVXLCDM])(" + _ROMAN_CORE + r")" + _NOT_LETTER_AFTER
)
# Имя монарха + арабское число: «Пётр 1» (так пишут посетители в чате).
# Только для известных имён — иначе испортили бы «Зал 5» и «Витрина 3».
_NAME_DIGIT_RE = re.compile(r"([А-ЯЁ][а-яё]+)(\s+)(\d{1,2})(?!\d)" + _NOT_LETTER_AFTER)


def _sub_name_numerals(text: str) -> str:
    def repl_roman(m: re.Match) -> str:
        value = _roman_to_int(m.group(3))
        if value > 30:
            return m.group(0)
        word = _ordinal_after_name(m.group(1), value)
        return m.group(0) if word is None else f"{m.group(1)}{m.group(2)}{word}"

    def repl_digit(m: re.Match) -> str:
        if not m.group(1).lower().startswith(_MONARCH_NAME_STEMS):
            return m.group(0)
        word = _ordinal_after_name(m.group(1), int(m.group(3)))
        return m.group(0) if word is None else f"{m.group(1)}{m.group(2)}{word}"

    return _NAME_DIGIT_RE.sub(repl_digit, _NAME_ROMAN_RE.sub(repl_roman, text))


# ── Публичный интерфейс ──────────────────────────────────────────────────────
_HAS_NUMERALS_RE = re.compile(r"\d|" + _ROMAN_RE.pattern)


def has_numerals(text: Optional[str]) -> bool:
    """Есть ли в тексте числа (арабские или римские)?

    Нужен, чтобы не звать LLM-нормализацию на каждое «Прослушать»: текст без
    чисел синтезатор прочитает верно и без переписывания (см. tts.prepare_for_tts).
    """
    return bool(text) and _HAS_NUMERALS_RE.search(text) is not None  # type: ignore[arg-type]


def normalize_for_tts(text: str) -> str:
    """Полная детерминированная подготовка текста к озвучке.

    Порядок важен: сначала контексты, где число — порядковое (век, имя правителя),
    затем всё оставшееся римское → арабское.
    """
    if not text:
        return text
    text = _sub_centuries(text)
    text = _sub_name_numerals(text)
    return roman_to_arabic(text)
