"""Разбор каталожной строки путеводителя (баг-репорт 12.08.2026, п.5).

Импорт залил каталожную строку указателя целиком в ``short_description``, а
``year_created``/``master_name``/``material`` оставил пустыми — у 1048 карточек из 1252.
Этот модуль разбирает такую строку обратно на поля. Ни БД, ни сети: на вход строка,
на выходе ``ParsedLine``; бэкфилл (разовый скрипт) только раскладывает результат по
колонкам и печатает отчёт.

Форма строки (74 % карточек): «Город, датировка. Фирма/мастер. Материалы; техники»,
плюс шесть вариаций — без мастера, без места, с преамбулой-провенансом («Подарок …»),
живопись «Автор (годы жизни). Датировка. Материалы», без «;» (материалы и техники через
запятую) и многочастная запись с метками («Живопись:», «Оклад:», «Футляр:»). Правила
выведены из 1048 реальных карточек прода, сверенных с путеводителем 2014 года; здесь они
повторены буквально. Ключевые решения и почему они именно такие:

* **«;» сильнее словаря.** Слева от точки с запятой — материалы, справа — техники, и
  лексикон эту позицию НЕ переопределяет. Иначе не решается «эмаль»: в «Бовенит, рубин,
  золото, стекло, эмаль, бумага; резьба, …» (id 1079, сверено с путеводителем) это
  материал, а в «Серебро; чеканка, эмаль, золочение» (id 524) — техника. Одна и та же
  лексема, решает только позиция. «Акварель» в путеводителе стоит справа от «;» во всех
  23 вхождениях — поэтому в ``material`` она попасть не может (это и есть смежный баг
  п.5: «Акварель» лежала материалом у карточек старого импорта).
* **Точка — не всегда конец сегмента.** «Мастерская Дж. Бриджа» (id 633) и «ок. 1700»
  (id 632) наивный сплит по «. » режет пополам. Инициалы (1–2 заглавные буквы) и
  сокращения из ``_ABBREVIATIONS`` границей не считаются.
* **Датировка — строкой, дословно.** ``year_created`` хранит датировку как в
  путеводителе («1880-е — первая половина 1890-х»), включая латинские и кириллические
  гомоглифы исходника: чинить их нужно для РАСПОЗНАВАНИЯ, а не для показа. Рядом
  ``year_lower`` — нижняя граница периода числом («конец XIX века» → ``None`` и
  ``precision='century'``): в колонку он больше не пишется (с 17.08.2026 колонка
  ``year_created`` — TEXT), но нужен классификации точности и пометкам разбора.
* **Молчаливая порча хуже пропуска.** Если в строке остался сегмент, который парсер не
  опознал, карточка получает ``status='skipped'`` и ВСЕ поля ``None`` — пусть лучше
  останется пустой и попадёт в отчёт, чем в ``master_name`` уедет половина датировки.
  Связная музейная проза (20 карточек, напр. id 8, 232) отсеивается ещё до разбора —
  ``looks_like_catalog_line``.

Что модуль сознательно НЕ делает: не ходит в БД, не решает, писать ли поле поверх
непустого (это дело бэкфилла — он дозаполняет только пустые), и не «чинит» содержательные
ошибки указателя. Перевёрнутый диапазон «1897−1809» (id 483) разбирается как есть с
пометкой в ``notes``: молча поменять годы местами — значит подделать источник.

Рядом живёт ``split_maker`` — разрез готового ``master_name`` на «фирму» и «мастера»
для карточки предмета (баг-репорт 31.08.2026, п. I-2). Он тут не ради разбора строки, а
ради словаря исполнителей: лексикон «Фирма | Фабрика | мастер | автор …» один на модуль,
и второй его копии в репозитории быть не должно. В колонки разрез не пишется никогда.

Разбор детерминирован: ``parse_catalog_line(x)`` от одной и той же строки всегда даёт
один и тот же результат, поэтому повторный прогон бэкфилла не находит изменений.

    from app.services.catalog_line import parse_catalog_line, looks_like_catalog_line

    line = parse_catalog_line(card["short_description"])
    if line.status != "skipped":
        patch = {"year_created": line.year_created, "material": line.material, ...}
        # line.year_created — строка датировки целиком («1899–1903», «конец XIX века»)
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

# ── Словари ──────────────────────────────────────────────────────────────────
# Оба списка выведены из данных, а не придуманы: техники — из правых частей всех
# 981 строк с «;», материалы — из левых. Держим их здесь, а не в JSON рядом с
# модулем, чтобы разбор работал в Cloud Function без чтения файлов с диска.

TECHNIQUES: frozenset = frozenset({
    "«пламенеющая» глазурь", "акварель", "амальгама", "бесцветная глазурь", "вальцовка",
    "витражная эмаль", "воронение", "выемчатая эмаль", "выпиловка", "гильошировка",
    "гравировка", "гравировка на металле и на кости", "гранение", "гризайль", "гуашь",
    "живописная эмаль", "живопись по металлу", "живопись по эмали", "зернь", "золочение",
    "инкрустация", "канфарение", "ковка", "литье", "матировка", "матовое крытье",
    "микромозаика", "мозаика", "мозаичный набор", "монтировка",
    "надглазурная монохромная роспись", "надглазурная печать",
    "надглазурная полихромная роспись", "надглазурная роспись",
    "надглазурная роспись эмалевыми красками", "надглазурное зеленое крытье",
    "надглазурное крытье", "надглазурное полихромное крытье", "низание",
    "низание жемчугом", "патинирование", "печать", "плетение", "подглазурная роспись",
    "подглазурное крытье кобальтом", "позолота", "полировка", "пунцирование", "резьба",
    "рельеф", "роспись золотом", "ручная лепка", "серебрение", "скань", "тиснение",
    "токарная работа", "токарно-давильные работы", "травление", "транспарантная эмаль",
    "флорентийская мозаика", "фото", "фотопечать", "цветная глазурь",
    "цветная глазурь «бычья кровь»", "цировка", "чеканка", "шитье", "штамп", "эмаль",
    "эмаль по гильошированному фону", "эмаль по гильошированному фону и гравировке",
    "эмаль по гильошировке", "эмаль по гравировке", "эмаль по золотой фольге",
    "эмаль по рельефу", "эмаль по скани", "эмаль по фольге",
})

# Семейства техник: указатель свободно порождает варианты («эмаль по рельефу и скани»,
# «надглазурное крытье кобальтом»), перечислять их поимённо бессмысленно.
TECHNIQUE_PREFIXES: Tuple[str, ...] = (
    "эмаль по", "надглазурн", "подглазурн", "роспись", "живопись по", "гравировка на",
    "токарно-давильн", "токарная", "мозаичн", "флорентийская", "цветная глазурь",
    "бесцветная глазурь", "матовое крытье", "ручная лепка", "низание", "«пламенеющ",
)

MATERIALS: frozenset = frozenset({
    "агат", "алебастр", "алмаз", "алмаз-«роза»", "алмазы", "алмазы-«розы»", "альмандин",
    "альмандины", "аметист", "бархат", "басма", "белила", "бирюза", "бисер", "бисквит",
    "бовенит", "бриллиант", "бриллианты", "бронза", "бумага", "гелиодор", "гелиотроп",
    "гипс", "горный хрусталь", "гранат", "гранаты", "графит", "декоративные камни",
    "декоративный камень", "демантоид", "дерево", "драгоценные и полудрагоценные камни",
    "драгоценные камни", "дуб", "жадеит", "жемчуг", "замша", "зеркало", "золото",
    "изумруд", "изумруды", "камень", "карандаш", "картон", "кварц", "керамика", "кожа",
    "коралл", "кость", "лазурит", "латунь", "левкас", "лунные камни", "лунный камень",
    "малахит", "маркетри", "масло", "медный сплав", "медь", "мел", "металл", "мех",
    "моховой агат", "мрамор", "муар", "недрагоценный металл", "нефрит", "оникс", "опал",
    "орех", "панцирь черепахи", "парча", "перламутр", "песчаник", "платина",
    "полудрагоценные камни", "полудрагоценный камень", "пробка", "репс", "рог", "родонит",
    "розовые и голубые сапфиры", "розовый сапфир", "рубин", "рубины", "сангина", "сапфир",
    "сапфиры", "свинцовое стекло", "сердолик", "серебро", "слоновая кость", "смальта",
    "спессартин", "сталь", "стекла", "стекло", "стразы", "темпера", "ткань", "тонировка",
    "тушь", "уголь", "фарфор", "фаянс", "халцедон", "халцедон («мекка»)", "халцедоны",
    "холст", "хризопразы", "хрусталь", "черепаха", "шелк", "шнур", "щетина", "янтарь",
    "яшма",
})

# Топонимы: на проде их 21 (лидируют Москва — 604 карточки и Санкт-Петербург — 390),
# остальные добавлены из путеводителя. Словарь НЕ обязателен для разбора — место берётся
# позиционно, слева от датировки, — но по нему видно, что в место уехало что-то чужое, и
# по нему же разбирается legacy-форма B, где запятая разделяет вообще всё.
PLACES: frozenset = frozenset({
    "санкт-петербург", "москва", "россия", "европа", "западная европа", "германия",
    "англия", "лондон", "париж", "севр", "палех", "мстёра", "австро-венгрия", "голландия",
    "амстердам", "швейцария", "женева", "ханау", "италия", "франция", "брянск", "казань",
    "одесса", "вена", "финляндия", "архангельское", "сольвычегодск", "поволжье", "киев",
    "ростов", "тула", "владимир", "великий устюг", "нижний новгород", "сергиев посад",
    "холуй", "гжель", "мейсен", "кострома", "рига", "варшава", "прибалтика",
    "московская губерния", "великобритания", "норвегия", "швеция", "дания", "бельгия",
})
_COUNTRIES: frozenset = frozenset({
    "россия", "франция", "англия", "германия", "швейцария", "италия", "голландия",
    "австро-венгрия", "великобритания", "норвегия", "швеция", "дания", "бельгия",
})


def _is_place(token: str) -> bool:
    return token.strip().lower().replace(" (?)", "") in PLACES


# Длина колонки ``exhibits.material`` — VARCHAR(255). При переполнении режем по границе
# токена и пишем пометку, а не падаем: потерять хвост списка материалов не так страшно,
# как уронить бэкфилл на 900-й карточке.
MATERIAL_MAX_LEN = 255

STATUS_PARSED = "parsed"      # разобрано целиком, ни одного неопознанного сегмента
STATUS_PARTIAL = "partial"    # поля получены, но часть строки не разобрана — в отчёт
STATUS_REPAIRED = "repaired"  # разобрано, но строку пришлось починить (см. _repair_glued)
STATUS_SKIPPED = "skipped"    # не разобрано: все поля None, карточку не трогаем

PRECISION_EXACT = "exact"
PRECISION_RANGE = "range"
PRECISION_RANGE_DECADE = "range_decade"
PRECISION_DECADE = "decade"
PRECISION_CIRCA = "circa"
PRECISION_BEFORE = "before"
PRECISION_AFTER = "after"
PRECISION_CENTURY = "century"


@dataclass(frozen=True)
class ParsedLine:
    """Результат разбора одной каталожной строки.

    ``year_created`` — датировка СТРОКОЙ, дословно как в путеводителе: ровно то, что
    пишется в одноимённую колонку (TEXT с 17.08.2026). ``year_lower`` — её нижняя
    граница числом; в колонки не пишется, но нужен классификации точности
    (``precision``) и пометкам разбора (перевёрнутый диапазон).

    ``origin_place`` — место создания дословно («Санкт-Петербург»). До 31.08.2026 колонки
    под него не было и оно уходило только в отчёт; теперь колонка есть (музей попросил
    показывать «дату создания И МЕСТО», п. I-2), заполняет её отдельный бэкфилл
    ``scripts/backfill_exhibit_origin_place.py``. ``provenance`` по-прежнему в колонки не
    пишется (такой колонки нет) — преамбула-посвящение нужна отчёту заказчику: по ней сразу
    видно глазом, ту ли строку разобрал парсер.

    ``notes`` — человекочитаемые пометки на русском: что починили, чего не поняли, где
    опечатка указателя. Это то, что бэкфилл кладёт в отчёт рядом с id карточки.
    """

    status: str
    year_created: Optional[str]
    year_lower: Optional[int]
    master_name: Optional[str]
    material: Optional[str]
    techniques: Optional[str]
    origin_place: Optional[str]
    provenance: Optional[str]
    precision: Optional[str]
    notes: Tuple[str, ...] = ()


# ── Предобработка ────────────────────────────────────────────────────────────
# Все виды тире эквивалентны: в диапазонах ЛЕТ на проде встречаются U+2013 (433 раза),
# U+2212 (249) и U+002D (1, id 635), между ЭПОХАМИ — U+2014 (33). Ни одно из них не несёт
# смысла сверх «от и до», поэтому для распознавания приводим к дефису.
_DASHES = "‐‑‒–—―−-"
_DASH_CLASS = "[" + _DASHES + "]"
_DASH_RE = re.compile(_DASH_CLASS)

_SPACE_RE = re.compile(r"[\s  ]+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,;.])")


def _prepare(text: Optional[str]) -> str:
    """NFC, схлопнутые пробелы, убранный пробел перед «,», «;», «.».

    Гомоглифы здесь НЕ чиним: латинская «e» в «1770-e» и кириллическая «Х» в «ХХ век»
    нужны в ``dating`` дословно — их лечит ``_repair_homoglyphs`` только внутри разбора
    датировки, где они мешают распознаванию.
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFC", text)
    normalized = _SPACE_RE.sub(" ", normalized)
    normalized = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", normalized)
    return normalized.strip()


# Латиница, притворяющаяся кириллицей (и наоборот). Внутри слова из кириллицы латинская
# «T» в «Tретья четверть» (id 1229) ломает любое правило по словам, а латинская «e» в
# «1770-e» (id 656) — распознавание десятилетия.
_LAT_TO_CYR: Dict[str, str] = {
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М", "O": "О",
    "P": "Р", "T": "Т", "X": "Х", "a": "а", "c": "с", "e": "е", "o": "о", "p": "р",
    "x": "х", "y": "у",
}
_CYR_LETTER = "[А-Яа-яЁё]"
_MIXED_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]{2,}")
_LATIN_DECADE_RE = re.compile(r"(?<=\d)(" + _DASH_CLASS + r")e\b")
# Кириллическая «Х» в римском числе перед «век»/«в.»: «ХХ век» (7 карточек), «ХVII».
_CYR_ROMAN_RE = re.compile(r"\b[XIVLCDMХх]{1,7}\b(?=\s*(?:век|в\.|вв))", re.IGNORECASE)


def _repair_homoglyphs(text: str) -> str:
    """Починить гомоглифы ТОЛЬКО ради распознавания датировки.

    Три реальных случая с прода: «1770-e» с латинской «e» (id 656), «Tретья» с латинской
    «T» (id 1229), «ХХ век»/«ХVII» с кириллической «Х» (id 1151, 1185, 1187, 652).
    Замены посимвольные и односимвольные — длина строки не меняется, поэтому вызывать
    функцию можно на любом куске текста, не сбивая offsets.
    """
    fixed = _LATIN_DECADE_RE.sub(r"\1е", text)

    def fix_word(match: "re.Match[str]") -> str:
        word = match.group(0)
        has_cyr = any("А" <= ch <= "я" or ch in "Ёё" for ch in word)
        has_lat = any(ch.isascii() and ch.isalpha() for ch in word)
        if not (has_cyr and has_lat):
            return word
        return "".join(_LAT_TO_CYR.get(ch, ch) if ch.isascii() else ch for ch in word)

    fixed = _MIXED_WORD_RE.sub(fix_word, fixed)
    # Римское число целиком из кириллицы («ХХ») смешанным словом не считается — правим
    # отдельно и только там, где справа стоит «век».
    return _CYR_ROMAN_RE.sub(lambda m: m.group(0).replace("Х", "X").replace("х", "x"), fixed)


# ── Разбиение на именованные части ───────────────────────────────────────────
# 159 карточек из 1048 разбиты на части: «Живопись: … Оклад: … Футляр: …». Частоты меток
# на проде: Живопись 122, Оклад 107, Футляр 31, Оправа 14, Корпус яйца 3, остальные по 1.
PART_LABELS: Tuple[str, ...] = (
    "Живопись", "Оклад", "Оправа", "Риза", "Рама", "Футляр", "Венец и цата",
    "Корпус яйца", "Механизм", "Накладной знак", "Стекло", "Портсигар", "Брошь", "Кулон",
    "Настольные часы", "Шкатулка", "Чернильный набор", "Карандаш в футляре", "Печать",
    "Серебро",
)
# Части-«обёртки»: футляр, оклад и рама — это НЕ сам предмет. Материалы футляра
# («металл, дерево, бархат, шелк») в ``material`` предмета попасть не должны, поэтому в
# плоские поля берётся первая ПРЕДМЕТНАЯ часть, а не первая по порядку.
# «Накладной знак» в этот список НЕ входит: на проде он ровно один (id 538) и означает не
# отдельный предмет со своими материалами, а ВСТАВНУЮ ДАТИРОВКУ посреди основной строки
# («Санкт-Петербург, 1880-е … Накладной знак: начало XX века. Фирма К. Фаберже, мастер
# М. Перхин. Золото, …») — мастер и материалы за ней принадлежат самому портсигару.
CONTAINER_LABELS: frozenset = frozenset({
    "Футляр", "Оклад", "Оправа", "Риза", "Рама", "Венец и цата", "Стекло", "Механизм",
})
MAIN_PART_LABEL = "<основная>"  # часть до первой метки — так её называет отчёт

_PART_RE = re.compile(
    r"(?:^|(?<=\. ))(" + "|".join(re.escape(label) for label in PART_LABELS) + r"):\s"
)


def _split_parts(text: str) -> List[Tuple[Optional[str], str]]:
    """Разложить строку на (метка, текст части). Текст до первой метки — часть без метки."""
    marks = [(m.start(), m.group(1), m.end()) for m in _PART_RE.finditer(text)]
    if not marks:
        return [(None, text)]
    parts: List[Tuple[Optional[str], str]] = []
    if marks[0][0] > 0:
        head = text[: marks[0][0]].strip().rstrip(".").strip()
        if head:
            parts.append((None, head))
    for index, (_, label, end) in enumerate(marks):
        stop = marks[index + 1][0] if index + 1 < len(marks) else len(text)
        body = text[end:stop].strip().rstrip(".").strip()
        parts.append((label, body))
    return parts


# ── Разбиение части на сегменты ──────────────────────────────────────────────
# Сокращения, после которых точка не заканчивает предложение. «ок.» здесь ключевое:
# без него «Голландия, Амстердам, ок. 1700» рвётся посреди датировки (id 632).
_ABBREVIATIONS: frozenset = frozenset({
    "ок", "г", "гг", "в", "вв", "св", "им", "стр", "руб", "тыс",
})
_DOT_RE = re.compile(r"\.\s+")
_WORD_BEFORE_DOT_RE = re.compile(r"([A-Za-zА-Яа-яЁё\-]+)$")
_REGNAL_RE = re.compile(r"[IVX]{1,2}")            # «Николая II», «Георгу I», «Людовика X»


def _is_sentence_boundary(text: str, dot_index: int) -> bool:
    """Точка на позиции ``dot_index`` — конец сегмента или часть сокращения/инициала?

    Инициал — 1–2 буквы, первая заглавная: «К. Фаберже», «Дж. Бриджа», «Ж.-Б.-Г. Делуа»
    (у последнего проверяется хвост «Г»). Сокращения — по списку выше.
    """
    head = text[:dot_index]
    # Точка внутри незакрытых кавычек — не граница: «Мастер-монограммист «I. N.»» (id 1243),
    # там это инициалы монограммы, а не два предложения.
    if head.count("«") + head.count("„") > head.count("»") + head.count("“"):
        return False
    match = _WORD_BEFORE_DOT_RE.search(head)
    if not match:
        return True
    word = match.group(1).split("-")[-1]          # «Ж.-Б.-Г» → «Г»
    if not word:
        return True
    if word.lower() in _ABBREVIATIONS:
        return False
    # Римское число перед точкой — номер монарха («королю Георгу I. Санкт-Петербург»,
    # id 547), и точка после него ЗАКАНЧИВАЕТ предложение. Настоящие инициалы в указателе
    # кириллические («К. Фаберже»), а латинские монограммы стоят в кавычках («FG») и уже
    # отсеяны проверкой выше — путаницы не возникает.
    if _REGNAL_RE.fullmatch(word):
        return True
    return not (len(word) <= 2 and word[0].isupper())


def _split_segments(text: str) -> List[str]:
    """Часть → сегменты по «. » с защитой инициалов и сокращений."""
    body = text.strip()
    while body.endswith("."):
        body = body[:-1].rstrip()
    segments: List[str] = []
    start = 0
    for match in _DOT_RE.finditer(body):
        if not _is_sentence_boundary(body, match.start()):
            continue
        chunk = body[start:match.start()].strip()
        if chunk:
            segments.append(chunk)
        start = match.end()
    tail = body[start:].strip()
    if tail:
        segments.append(tail)
    return segments


def _tokens(text: str) -> List[str]:
    return [token.strip() for token in text.split(",") if token.strip()]


# ── Материалы и техники ──────────────────────────────────────────────────────
_TECH_BY_LENGTH: Tuple[str, ...] = tuple(sorted(TECHNIQUES, key=len, reverse=True))
_DIGIT_RE = re.compile(r"\d")


def _is_known_technique(token: str) -> bool:
    low = token.strip().lower()
    return low in TECHNIQUES or low.startswith(TECHNIQUE_PREFIXES)


def _repair_glued(token: str) -> Optional[List[str]]:
    """Расклеить слипшиеся техники: «эмаль по гильошированному фонуинкрустация» → две.

    В указателе местами потеряна запятая («токарно-давильные работы эмаль по скани»,
    id 1048, 1336) или её не было вовсе (id 653 — слова слиплись без пробела). Режем
    только если ОБЕ половины — известные леммы: иначе «эмаль по чему-то новому» тоже
    развалилось бы пополам. Возвращает ``None``, если токен не расклеивается.
    """
    low = token.strip().lower()
    if low in TECHNIQUES:
        return None
    for lemma in _TECH_BY_LENGTH:
        if not low.startswith(lemma) or len(low) == len(lemma):
            continue
        rest = low[len(lemma):].lstrip(" ,")
        if not rest:
            continue
        if rest in TECHNIQUES:
            return [lemma, rest]
        deeper = _repair_glued(rest)
        if deeper:
            return [lemma, *deeper]
    return None


def _looks_like_token(token: str, first: bool, limit_words: int) -> bool:
    """Общая проверка «это вообще элемент перечисления, а не кусок фразы?».

    Цифр в материалах и техниках не бывает, заглавных внутри токена — тоже (кроме первой
    буквы сегмента, он же начало предложения). Именно это отсекает id 462, где точек между
    сегментами нет вовсе и в «материалы» иначе уехало бы «1890-е Фирма Г. Грачёва Серебро».
    """
    body = token.strip()
    if not body or len(body) > 64:
        return False
    if _DIGIT_RE.search(body):
        return False
    if len(body.split()) > limit_words:
        return False
    tail = body[1:] if first else body
    return not any(ch.isupper() for ch in tail)


def _is_material_like(token: str, first: bool) -> bool:
    return token.strip().lower() in MATERIALS or _looks_like_token(token, first, 5)


def _is_technique_like(token: str, first: bool) -> bool:
    return _is_known_technique(token) or _looks_like_token(token, first, 6)


def _is_place_like(token: str) -> bool:
    """Токен похож на место? Правило регистра тут не работает — топонимы бывают и с
    заглавными внутри («Санкт-Петербург»), и со строчной в начале («село Горбуново»),
    поэтому проверяем только длину и отсутствие цифр (цифры значат, что в кусок затесалась
    датировка или сломанная строка без точек-разделителей)."""
    body = token.strip()
    return bool(body) and len(body) <= 64 and len(body.split()) <= 6 and not _DIGIT_RE.search(body)


@dataclass(frozen=True)
class _Stuff:
    """Разобранный сегмент материалов/техник."""

    materials: Tuple[str, ...]
    techniques: Tuple[str, ...]
    notes: Tuple[str, ...] = ()
    repaired: bool = False


def _parse_stuff(segment: str) -> Optional[_Stuff]:
    """Сегмент «Материалы; техники» → списки. ``None``, если это не он.

    Правило ЖЁСТКО ПОЗИЦИОННОЕ (спека п.4.1): при наличии «;» словарь не может передвинуть
    токен через точку с запятой. Без «;» (16 карточек, форма A5) делим по словарю: токен —
    техника, если он в ``TECHNIQUES``, иначе материал.
    """
    if ";" not in segment:
        tokens = _tokens(segment)
        if not tokens:
            return None
        if not all(token.lower() in TECHNIQUES or token.lower() in MATERIALS for token in tokens):
            return None       # незнакомый токен — сегмент не наш, спека п.4.2
        materials = [t for t in tokens if t.lower() not in TECHNIQUES]
        techniques = [t for t in tokens if t.lower() in TECHNIQUES]
        return _Stuff(tuple(materials), tuple(techniques))

    chunks = segment.split(";")
    materials = _tokens(chunks[0])
    notes: List[str] = []
    repaired = False

    # Вторая «;» в одном сегменте (id 1022): «Серебро; чеканка, …, полудрагоценные камни;
    # зернь, …». Материал, уехавший в хвост техник, возвращаем на место — но только тот,
    # что действительно есть в словаре материалов.
    techniques: List[str] = []
    for index, chunk in enumerate(chunks[1:]):
        for token in _tokens(chunk):
            if index < len(chunks) - 2 and token.lower() in MATERIALS:
                materials.append(token)
                notes.append(
                    f"вторая «;» в сегменте: «{token.lower()}» возвращён(ы) в материалы"
                )
                repaired = True
                continue
            techniques.append(token)
    if len(chunks) > 2 and not repaired:
        notes.append("вторая «;» в сегменте — техники слиты в один список")
        repaired = True

    if not all(_is_material_like(token, i == 0) for i, token in enumerate(materials)):
        return None
    if not any(token.lower() in MATERIALS for token in materials):
        return None           # ни одного знакомого материала слева — это не наш сегмент

    fixed: List[str] = []
    for token in techniques:
        parts = _repair_glued(token)
        if parts:
            notes.append(f"починена склейка техник: «{token}» → «{', '.join(parts)}»")
            repaired = True
            fixed.extend(parts)
            continue
        fixed.append(token)
    if not all(_is_technique_like(token, i == 0) for i, token in enumerate(fixed)):
        return None
    return _Stuff(tuple(materials), tuple(fixed), tuple(notes), repaired)


# ── Нормализация значений ────────────────────────────────────────────────────
_QUOTE_PAIR_RE = re.compile(r"\"([^\"]*)\"")
_ROSE_RE = re.compile(r"\s*" + _DASH_CLASS + r"\s*(?=[«\"])")


def _normalize_material(token: str) -> str:
    """«алмазы -"розы"» → «Алмазы-«розы»».

    Регистр на проде — Каждый Токен С Заглавной (проверено на 167 непустых значениях
    ``material``, токенов со строчной нет), кавычки внутри токена — ёлочки, как в самом
    указателе; варианты «Алмазы -"розы"» (29 значений) и «Алмазы-"розы"» (23) — следы
    старого импорта.
    """
    body = _SPACE_RE.sub(" ", token.strip())
    body = _QUOTE_PAIR_RE.sub(lambda m: f"«{m.group(1)}»", body)
    body = _ROSE_RE.sub("-", body)
    return body[:1].upper() + body[1:] if body else body


def _join_materials(tokens: Sequence[str]) -> Tuple[Optional[str], Tuple[str, ...]]:
    """Материалы одной строкой + пометки. Длиннее 255 — режем по границе токена."""
    values = [_normalize_material(token) for token in tokens if token.strip()]
    if not values:
        return None, ()
    joined = ", ".join(values)
    if len(joined) <= MATERIAL_MAX_LEN:
        return joined, ()
    kept: List[str] = []
    for value in values:
        candidate = ", ".join([*kept, value])
        if len(candidate) > MATERIAL_MAX_LEN:
            break
        kept.append(value)
    if not kept:                                  # один токен длиннее колонки — режем жёстко
        return joined[:MATERIAL_MAX_LEN], (
            f"material длиннее {MATERIAL_MAX_LEN} символов — усечён посимвольно",
        )
    dropped = len(values) - len(kept)
    return ", ".join(kept), (
        f"material длиннее {MATERIAL_MAX_LEN} символов — усечён по границе токена, "
        f"отброшено материалов: {dropped}",
    )


def _join_techniques(tokens: Sequence[str]) -> Optional[str]:
    """Техники — всегда строчными, в порядке указателя."""
    values = [_SPACE_RE.sub(" ", token.strip()).lower() for token in tokens if token.strip()]
    return ", ".join(values) if values else None


# ── Датировка ────────────────────────────────────────────────────────────────
_YEAR_RE = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")
_DATE_START_RE = re.compile(
    r"^(?:\d{4}"
    r"|[XIVLCDM]{1,7}\s*(?:век|в\.)"
    r"|начал|конец|конца|середин|перв|втор|треть|четверт|последн|половин|рубеж"
    r"|около|ок\.|не позднее|не ранее|до\s+\d|после\s+\d)",
    re.IGNORECASE,
)
_DATE_HINT_RE = re.compile(
    r"\d{4}|[XIVLCDM]{1,7}\s*(?:век|в\.)|век|столет|половин|четверт|треть|рубеж",
    re.IGNORECASE,
)
_DECADE_RE = re.compile(r"\d{3}0\s*-\s*[ех]\b")
_UNCERTAIN_RE = re.compile(r"\(\?\)")


_ROMAN_START_RE = re.compile(r"^[XIVLCDM]{1,7}\b")


def _starts_dating(token: str) -> bool:
    """Токен ОТКРЫВАЕТ датировку? По нему режется «место, датировка».

    Отдельная ветка для римского числа в начале токена: «XVII век» ловится и первым
    правилом, а «XIX — начало XX века» (id 851) — только этой, потому что слово «век»
    в нём стоит через тире, а не сразу за числом.
    """
    fixed = _repair_homoglyphs(token.strip())
    if _DATE_START_RE.match(fixed):
        return True
    return bool(_ROMAN_START_RE.match(fixed) and _DATE_HINT_RE.search(fixed))


def _has_date_hint(token: str) -> bool:
    """В токене есть что-то от даты? Хвост датировки («, антикварная реставрация XIX века»)
    обязан такой признак иметь — иначе это уже не датировка, а неопознанный сегмент."""
    return bool(_DATE_HINT_RE.search(_repair_homoglyphs(token.strip())))


def _parse_dating(text: str) -> Tuple[Optional[int], str, Tuple[str, ...]]:
    """Датировка → (нижняя граница, точность, пометки).

    ``year_lower`` — ВСЕГДА нижняя граница периода, то есть ПЕРВЫЙ год строки: «1899–1903»
    → 1899, «1880-е — первая половина 1890-х» → 1880, «не позднее 1899» → 1899. Датировки
    без арабского года («конец XIX века», «рубеж XIX–XX веков») дают ``None``: век в число
    превращать нельзя, «конец XIX века» стал бы 1801.
    """
    fixed = _repair_homoglyphs(text.strip())
    flat = _DASH_RE.sub("-", fixed).lower()
    notes: List[str] = []
    if _UNCERTAIN_RE.search(flat):
        notes.append(f"датировка с оговоркой «(?)»: «{text.strip()}» — проверить по путеводителю")

    years = [int(value) for value in _YEAR_RE.findall(flat)]
    decades = _DECADE_RE.findall(flat)
    if not years:
        return None, PRECISION_CENTURY, tuple(notes)

    year = years[0]
    if len(years) >= 2 and years[1] < years[0]:
        notes.append(
            f"перевёрнутый диапазон «{text.strip()}» — опечатка указателя, "
            f"взята левая граница {year}"
        )

    if len(decades) >= 2:
        precision = PRECISION_RANGE_DECADE
    elif decades and len(years) >= 2:
        precision = PRECISION_RANGE_DECADE                      # «1880–1890-е»
    elif decades:
        precision = PRECISION_DECADE
    elif re.match(r"^(около|ок\.)\s*\d{4}", flat):
        precision = PRECISION_CIRCA
    elif re.match(r"^(не позднее|до)\s+\d{4}", flat):
        precision = PRECISION_BEFORE
    elif re.match(r"^(после|не ранее)\s+\d{4}", flat):
        precision = PRECISION_AFTER
    elif len(years) >= 2:
        precision = PRECISION_RANGE
    elif re.fullmatch(r"\d{4}(\s*\(\?\))?", flat):
        precision = PRECISION_EXACT
    else:
        precision = PRECISION_EXACT
        notes.append(f"датировка нестандартного вида: «{text.strip()}» — взят год {year}")
    return year, precision, tuple(notes)


# ── Сегмент авторства ────────────────────────────────────────────────────────
# 200+ различных значений, но все начинаются с фиксированного набора слов.
#
# Словарь ОДИН, но с 31.08.2026 он поделён надвое, и деление содержательное:
# «Фирма», «Фабрика», «Завод» называют ПРЕДПРИЯТИЕ, «мастер», «автор»,
# «миниатюрист» — ИСПОЛНИТЕЛЯ. Пока сегмент разбирался целиком, разница ничего не
# решала; она понадобилась, когда музей попросил показывать «фирму и мастера»
# раздельно (баг-репорт 31.08.2026, п. I-2) — см. `split_maker` ниже. Второй копии
# словаря заводить нельзя, поэтому распознаватель сегмента `_MAKER_RE`
# по-прежнему один и собирается из этих же двух половин: их объединение дословно
# равно прежнему списку, и порядок альтернатив на результат не влияет (в
# `_is_maker` от регулярки нужен только факт совпадения).
_FIRM_WORDS: Tuple[str, ...] = (
    r"Фирма", r"Фабрика", r"Мастерская", r"Живописная мастерская", r"Завод",
    r"Императорск\w+", r"Национальн\w+", r"Частн\w+", r"Серебряное заведение",
    r"Торгово-промышленное заведение", r"Товарищество", r"Монета",
    # Числительные — ради «Первая серебряная артель»; без слова «артель» они
    # отсекаются в `_is_maker`/`_is_firm` (см. `_NUMERAL_RE`).
    r"Перв\w+", r"Втор\w+", r"Треть\w+", r"Четверт\w+", r"Пят\w+", r"Шест\w+",
    r"Седьм\w+", r"Восьм\w+", r"Девят\w+", r"Десят\w+", r"Одиннадцат\w+",
    r"Двенадцат\w+", r"Тринадцат\w+", r"Четырнадцат\w+", r"Пятнадцат\w+",
    r"Шестнадцат\w+", r"Семнадцат\w+", r"Восемнадцат\w+", r"Девятнадцат\w+",
    r"Двадцат\w+",
)
_EXECUTOR_WORDS: Tuple[str, ...] = (
    r"Мастер", r"Мастер-монограммист", r"Неизвестн\w+", r"Автор", r"Иконописец",
    r"Исполнител\w*", r"Художник\w*", r"Миниатюрист", r"Скульптор\w*", r"Медальер",
    r"Гравер", r"Резчик", r"Живописец",
)
_MAKER_RE = re.compile(r"^(" + "|".join((*_FIRM_WORDS, *_EXECUTOR_WORDS)) + r")\b")
# Числительное засчитывается за автора ТОЛЬКО вместе со словом «артель» — иначе
# «Первая половина XIX века» уехала бы в мастера.
_NUMERAL_RE = re.compile(
    r"^(Перв|Втор|Треть|Четверт|Пят|Шест|Седьм|Восьм|Девят|Десят|Одиннадцат|Двенадцат"
    r"|Тринадцат|Четырнадцат|Пятнадцат|Шестнадцат|Семнадцат|Восемнадцат|Девятнадцат|Двадцат)"
)
# Форма A7 (живопись, 21 карточка): «Генрих Семирадский (1843–1902)» — автор без ключевого
# слова, зато с годами жизни в скобках.
_AUTHOR_LIFE_RE = re.compile(
    r"^[А-ЯЁ][а-яё\-]+(?:\s+[А-ЯЁ][а-яё\-\.]+){1,3}\s*\((?:\d{4}|\?)\s*"
    + _DASH_CLASS + r"\s*(?:\d{4}|\?)\)$"
)
_PROVENANCE_RE = re.compile(
    r"^(Подарок|Подарен|Подарена|Подарены|Поднесен|Поднесено|Принадлеж|С памятными"
    r"|Приобрет|Вручен|Заказан)"
)


def _is_maker(segment: str) -> bool:
    if not _MAKER_RE.match(segment):
        return False
    if _NUMERAL_RE.match(segment) and "артель" not in segment.lower():
        return False
    return True


# ── «Фирма и мастер» раздельно (баг-репорт 31.08.2026, п. I-2) ────────────────
# Слово-маркер исполнителя в ХВОСТЕ строки — то, по чему видно, что справа от
# разделителя начался человек: «Фирма К. Фаберже, мастер М. Перхин».
# «Мастерская» добавлена сюда отдельно, потому что позиция меняет её смысл: в
# начале строки это предприятие («Мастерская Дж. Бриджа», id 633), а после
# запятой — исполнитель («Фирма И. Морозова, мастерская В. Иванова»).
# Регистр не важен: после «, » маркер строчный, после «. » — прописной.
_EXECUTOR_TAIL_RE = re.compile(
    r"^(" + "|".join((*_EXECUTOR_WORDS, r"Мастерская", r"Живописная мастерская")) + r")\b",
    re.IGNORECASE,
)
_FIRM_HEAD_RE = re.compile(r"^(" + "|".join(_FIRM_WORDS) + r")\b")
# Разделители «предприятие | исполнитель» — те же, что режут сегменты каталожной
# строки: запятая («Фирма К. Фаберже, мастер М. Перхин») и точка
# («Императорский фарфоровый завод. Исполнители росписи П. Столетов, В. Иванов», id 528).
_MAKER_SEPARATORS = (", ", ". ")


@dataclass(frozen=True)
class MakerParts:
    """Строка авторства, разобранная на предприятие и исполнителя.

    ``text`` — исходная строка дословно и ВСЕГДА авторитетна; ``firm``/``master`` —
    её подстроки, разбор эвристический. Не разобралось — обе части ``None``, а
    ``text`` заполнен: показать посетителю всегда есть что.
    """

    text: Optional[str]
    firm: Optional[str]
    master: Optional[str]


def _is_firm(segment: str) -> bool:
    """Сегмент начинается со слова, называющего ПРЕДПРИЯТИЕ, а не человека."""
    if not _FIRM_HEAD_RE.match(segment):
        return False
    # Тот же оберег, что и в `_is_maker`: «Первая половина XIX века» — не артель.
    if _NUMERAL_RE.match(segment) and "артель" not in segment.lower():
        return False
    return True


def split_maker(master_name: Optional[str]) -> MakerParts:
    """Разрезать ``master_name`` на «фирму» и «мастера» — БЕЗ записи в БД.

    Музей просит показывать фирму и мастера отдельными строками карточки
    (баг-репорт 31.08.2026, п. I-2), а в базе они лежат в одном поле: «Фирма
    К. Фаберже, мастер М. Перхин». Разбивка здесь ПРОИЗВОДНАЯ и считается при
    сериализации ответа — отдельных колонок мы сознательно не заводим:

    * форму записи фирм музей оставил за собой (``db/guide_fixes_20260812.json``:
      «Решение о форме записи фабрик — за музеем»), и записать в колонку наше
      предположение значило бы выдать его за факт музея;
    * два поля на одну сущность расползаются при первой же ручной правке в
      админке — ровно тем доводом в ``docs/task-2026-08-17-year-created-string.md``
      убили колонку-дубль ``dating``.

    Правила выведены из тех же 200+ значений прода, что и остальной модуль:

    1. Пусто или одни пробелы → все три поля ``None``.
    2. Режем по ПЕРВОМУ разделителю («, » или «. »), справа от которого стоит
       слово-маркер исполнителя, а слева — название предприятия. Левая часть →
       ``firm``, правая → ``master``.
    3. Разделителя нет: строка целиком уходит в ``firm``, если начинается со
       слова-предприятия («Фабрика Д. Шелапутина», «Первая серебряная артель»),
       иначе в ``master`` («Николай Черноков», «Генрих Семирадский (1843–1902)»).
    4. Части возвращаются ДОСЛОВНО, вместе со словом-маркером и кавычками
       источника («Фирма «К. Э. Болин»»): «фирма», «фабрика» и «мастерская» — это
       формулировки самого путеводителя, и подменять их ради красивой вёрстки мы
       не вправе. Инвариант: ``firm`` и ``master``, если они не ``None``, —
       подстроки ``text``.
    5. Левая часть, не похожая на предприятие, разбивку отменяет: строка целиком
       остаётся мастером. Это тот же принцип, что и во всём модуле — молча
       испортить хуже, чем не разобрать.

    Функция чистая и детерминированная; ``parse_catalog_line`` она не меняет,
    поэтому вывод бэкфилла остаётся прежним байт в байт.
    """
    text = (master_name or "").strip()
    if not text:
        return MakerParts(None, None, None)

    for index in range(len(text) - 1):
        if text[index:index + 2] not in _MAKER_SEPARATORS:
            continue
        head, tail = text[:index].strip(), text[index + 2:].strip()
        if head and tail and _EXECUTOR_TAIL_RE.match(tail) and _is_firm(head):
            return MakerParts(text, head, tail)

    if _is_firm(text):
        return MakerParts(text, text, None)
    return MakerParts(text, None, text)


# ── Разбор одной части ───────────────────────────────────────────────────────
@dataclass
class _Part:
    """Разбор одной части карточки. ``leftover`` — сегменты, которые парсер не опознал."""

    label: Optional[str]
    place: Optional[str] = None
    dating: Optional[str] = None
    year: Optional[int] = None
    precision: Optional[str] = None
    master: Optional[str] = None
    materials: Tuple[str, ...] = ()
    techniques: Tuple[str, ...] = ()
    provenance: Optional[str] = None
    leftover: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()
    repaired: bool = False

    def is_empty(self) -> bool:
        return not (self.place or self.dating or self.master or self.materials or self.techniques)


def _split_place_and_dating(segments: Sequence[str], part: _Part) -> None:
    """Оставшиеся слева сегменты → место + датировка.

    Режем по запятым (и по границам сегментов: id 622 «Санкт-Петербург. 1903–1914») и
    берём позицию ПОСЛЕДНЕГО токена, ОТКРЫВАЮЩЕГО датировку. Так корректно берутся и
    «Швейцария, Женева», и «Московская губерния, Дмитровский уезд, село Горбуново».
    Хвост после датировки обязан сам содержать признак даты — «антикварная реставрация
    XIX века» его имеет (id 1185), а «Я. Роохальс, И. Хоогебоом» (id 632) нет, и часть
    честно уходит в неразобранные.
    """
    tokens: List[Tuple[str, str]] = []            # (разделитель перед токеном, текст)
    for segment in segments:
        for token_index, token in enumerate(_tokens(segment)):
            separator = "" if not tokens else (", " if token_index else ". ")
            tokens.append((separator, token))
    if not tokens:
        return

    start = None
    for index in range(len(tokens) - 1, -1, -1):
        if _starts_dating(tokens[index][1]):
            start = index
            break

    if start is None:
        if all(_is_place_like(token) for _, token in tokens):
            part.place = "".join(sep + token for sep, token in tokens)
        else:
            part.leftover = (*part.leftover, *(token for _, token in tokens))
        return

    tail = tokens[start + 1:]
    if not all(_has_date_hint(token) for _, token in tail):
        part.leftover = (*part.leftover, *(token for _, token in tokens))
        return

    head = tokens[:start]
    if head and not all(_is_place_like(token) for _, token in head):
        part.leftover = (*part.leftover, *(token for _, token in head))
        return
    if head:
        part.place = "".join(sep + token for sep, token in head).lstrip(", ").lstrip(". ")
        # Топоним не из словаря — не повод бросать строку (в указателе есть и «село
        # Горбуново», и «Дмитровский уезд»), но повод показать его заказчику: так в отчёт
        # попал «Эскиз» из «Карл Брюллов (1799–1852). Эскиз. 1850» (id 1120).
        for _, token in head:
            if not _is_place(token):
                part.notes = (*part.notes, f"место «{token}» не из словаря топонимов — проверить")
    dating = "".join(sep + token for sep, token in tokens[start:]).lstrip(", ").lstrip(". ")
    part.dating = dating
    part.year, part.precision, notes = _parse_dating(dating)
    part.notes = (*part.notes, *notes)


def _parse_part(label: Optional[str], body: str, allow_provenance: bool) -> _Part:
    part = _Part(label=label)
    segments = _split_segments(body)
    if not segments:
        return part

    # Преамбула-провенанс (22 карточки): «Подарок императора Николая II сэру А. Доусону.»
    # В колонку не пишется, но без её отделения место уехало бы в «Подарок императора…».
    if allow_provenance:
        preamble: List[str] = []
        while segments and _PROVENANCE_RE.match(segments[0]):
            preamble.append(segments.pop(0))
        if preamble:
            part.provenance = ". ".join(preamble) + "."
    if not segments:
        return part

    # Форма A7: автор с годами жизни идёт ПЕРВЫМ сегментом и ключевого слова не имеет.
    if _AUTHOR_LIFE_RE.match(segments[0]):
        part.master = segments.pop(0)
    if not segments:
        return part

    stuff_index = None
    stuff = None
    for index in range(len(segments) - 1, -1, -1):
        found = _parse_stuff(segments[index])
        if found is not None:
            stuff_index, stuff = index, found
            break
    if stuff is not None and stuff_index is not None:
        part.materials = stuff.materials
        part.techniques = stuff.techniques
        part.notes = (*part.notes, *stuff.notes)
        part.repaired = stuff.repaired
        # Сегменты ПОСЛЕ материалов — неопознанный хвост (id 475: список названий
        # медальонов после «Гипс, тонировка»).
        part.leftover = (*part.leftover, *segments[stuff_index + 1:])
        segments = segments[:stuff_index]

    # Сегмент авторства — соседний слева; их может быть несколько подряд
    # (id 528: «Императорский фарфоровый завод. Исполнители росписи П. Столетов, …»).
    makers: List[str] = []
    while segments and _is_maker(segments[-1]):
        makers.insert(0, segments.pop())
    if makers:
        joined = ". ".join(makers)
        part.master = joined if part.master is None else f"{part.master}. {joined}"

    _split_place_and_dating(segments, part)
    return part


# ── Публичный интерфейс ──────────────────────────────────────────────────────
def _skipped(*notes: str) -> ParsedLine:
    """Ничего не разобрали: ВСЕ поля None. Карточку бэкфилл не трогает вовсе."""
    return ParsedLine(
        status=STATUS_SKIPPED, year_created=None, year_lower=None, master_name=None,
        material=None, techniques=None, origin_place=None, provenance=None,
        precision=None, notes=tuple(notes),
    )


# Форма B — legacy-импорт (8 карточек: 26, 95, 133, 163, 173, 182, 209, 423, 426):
# ««Название». Фирма/мастер: X, Город[, Страна][, Год], Мат1, Мат2.» Разбирается отдельной
# грамматикой: запятая тут разделяет ВСЁ подряд, и общие правила на ней не работают.
# Кавычка названия жадная: внутри названия бывают вложенные кавычки («Ковш с эмалевой
# миниатюрой "Гордая" (по рисунку С. Соломко)», id 95) — закрывающая та, за которой «. ».
_LEGACY_RE = re.compile(r"^[«\"](?P<title>.+)[»\"]\.\s+(?P<rest>.*)$", re.DOTALL)
_LEGACY_MAKER_RE = re.compile(r"^Фирма/мастер:\s*", re.IGNORECASE)


def _parse_legacy(text: str) -> Optional[ParsedLine]:
    """Разбор формы B. ``None``, если строка под неё не подходит."""
    match = _LEGACY_RE.match(text)
    if not match:
        return None
    rest = match.group("rest").strip().rstrip(".")
    if not rest:
        return None
    master: Optional[str] = None
    labeled = bool(_LEGACY_MAKER_RE.match(rest))
    if labeled:
        rest = _LEGACY_MAKER_RE.sub("", rest, count=1)

    tokens = _tokens(rest)
    if not tokens:
        return None
    if labeled:
        master = tokens.pop(0)

    # Порядок жёсткий: [мастер] [места] [год] [материалы]. Года у трёх карточек нет вовсе
    # (id 95, 133, 173) — это не повод бросать строку, дозаполним чем есть.
    places: List[str] = []
    while tokens and _is_place(tokens[0]):
        places.append(tokens.pop(0))
    year_token: Optional[str] = None
    if tokens and re.fullmatch(r"\d{4}", tokens[0]):
        year_token = tokens.pop(0)

    place = next((value for value in places if value.lower() not in _COUNTRIES), None)
    if place is None and places:
        place = places[0]

    if not all(_is_material_like(token, True) or _is_known_technique(token) for token in tokens):
        return None                                # порядок сломан — не наша грамматика
    materials = [token for token in tokens if not _is_known_technique(token)]
    techniques = [token for token in tokens if _is_known_technique(token)]
    if not (master or place or year_token or materials):
        return None
    material, notes = _join_materials(materials)
    year, precision, date_notes = (None, None, ())
    if year_token is not None:
        year, precision, date_notes = _parse_dating(year_token)
    return ParsedLine(
        status=STATUS_PARSED,
        year_created=year_token,
        year_lower=year,
        master_name=master,
        material=material,
        techniques=_join_techniques(techniques),
        origin_place=place,
        provenance=None,
        precision=precision,
        notes=("форма B (legacy-импорт старого парсера)", *notes, *date_notes),
    )


def _continuation_of(primary: _Part, parts: Sequence[_Part]) -> Optional[_Part]:
    """Часть, которая на самом деле ПРОДОЛЖАЕТ основную, а не описывает свой предмет.

    Единственный случай на проде — вставная датировка id 538: «… Накладной знак: начало
    XX века. Фирма К. Фаберже, мастер М. Перхин. Золото, …». Метка относится только к
    датировке, а мастер и материалы за ней — уже про сам портсигар. Признак: у основной
    части нет материалов вовсе, а следующая часть — не футляр и не оклад (у тех материалы
    СВОИ, их присваивать предмету нельзя).
    """
    if primary.materials or primary.dating is None:
        return None
    index = list(parts).index(primary)
    for part in list(parts)[index + 1:]:
        if (part.label or MAIN_PART_LABEL) in CONTAINER_LABELS:
            return None
        if part.materials and not part.leftover:
            return part
    return None


def _has_technique_tail(segment: str) -> bool:
    """Справа от «;» стоят как минимум две известные техники?

    Ослабленный признак каталожной строки — для тех, что разобрать нельзя, но и прозой
    они не являются (id 462: «Санкт-Петербург, 1890-е Фирма Г. Грачёва Серебро, сталь,
    шнур; штамп, гравировка, живописная эмаль, …» — потеряны все точки-разделители).
    """
    if ";" not in segment:
        return False
    tail = _tokens(segment.split(";", 1)[1])
    return len(tail) >= 2 and all(token.lower() in TECHNIQUES for token in tail)


def looks_like_catalog_line(text: Optional[str]) -> bool:
    """Это каталожная строка указателя, а не связный музейный текст?

    Отсеивает 20 карточек-нарративов (id 8, 15, 232 и др.), которые парсить нельзя вовсе:
    у них нет ни перечисления материалов, ни автора с годами жизни. Наивное правило «есть
    «;» → каталожная строка» на них ломается — «;» встречается и внутри прозы (id 5, 11).

    True не обещает, что строка разберётся: id 462 и 632 — каталожные, но битые, и
    ``parse_catalog_line`` вернёт по ним ``skipped``.
    """
    prepared = _prepare(text)
    if not prepared:
        return False
    if _parse_legacy(prepared) is not None:
        return True
    for _, body in _split_parts(prepared):
        segments = _split_segments(body)
        if not segments:
            continue
        if _AUTHOR_LIFE_RE.match(segments[0]):
            return True
        for segment in segments:
            if _parse_stuff(segment) is not None or _has_technique_tail(segment):
                return True
    return False


def parse_catalog_line(text: Optional[str]) -> ParsedLine:
    """Разобрать каталожную строку путеводителя.

    В плоские поля идёт ПЕРВАЯ ПРЕДМЕТНАЯ часть многочастной карточки (метка не из
    ``CONTAINER_LABELS``), иначе первая вообще: материалы футляра и оклада — не материалы
    предмета. Остальные части в поля не попадают, но их метки перечислены в ``notes``,
    чтобы заказчик видел, что карточка многочастная.

    Если разобрать не удалось — ``status='skipped'`` и все поля ``None``: молча испортить
    карточку хуже, чем пропустить её и показать в отчёте.
    """
    prepared = _prepare(text)
    if not prepared:
        return _skipped("пустая строка")
    if not looks_like_catalog_line(prepared):
        return _skipped("не каталожная строка: связный музейный текст — разбирать нельзя")

    legacy = _parse_legacy(prepared)
    if legacy is not None:
        return legacy

    raw_parts = _split_parts(prepared)
    parts = [
        _parse_part(label, body, allow_provenance=(index == 0 and label is None))
        for index, (label, body) in enumerate(raw_parts)
    ]
    parts = [part for part in parts if not (part.is_empty() and not part.leftover)]
    if not parts:
        return _skipped("строка не разобрана: не найдено ни материалов, ни датировки")

    primary = next(
        (part for part in parts if (part.label or MAIN_PART_LABEL) not in CONTAINER_LABELS),
        parts[0],
    )
    donor = _continuation_of(primary, parts)
    if donor is not None:
        primary.materials = donor.materials
        primary.techniques = donor.techniques
        primary.repaired = primary.repaired or donor.repaired
        primary.master = primary.master or donor.master
        primary.notes = (*primary.notes, *donor.notes, (
            f"вставная датировка «{donor.label}: {donor.dating}» — мастер и материалы "
            f"после неё отнесены к самому предмету"
        ))
    if primary.leftover or primary.is_empty():
        unknown = "; ".join(primary.leftover) if primary.leftover else prepared
        return _skipped(f"строка не разобрана: неопознанный сегмент «{unknown[:120]}»")

    notes: List[str] = list(primary.notes)
    if len(parts) > 1:
        labels = ", ".join(part.label or MAIN_PART_LABEL for part in parts)
        chosen = primary.label or MAIN_PART_LABEL
        notes.append(f"многочастная карточка (части: {labels}); в поля взята часть «{chosen}»")

    broken = [part for part in parts if part is not primary and part.leftover]
    for part in broken:
        label = part.label or MAIN_PART_LABEL
        notes.append(f"часть «{label}» не разобрана: «{'; '.join(part.leftover)[:120]}»")
    for part in parts:
        if part is not primary and part is not donor and part.notes:
            notes.extend(f"часть «{part.label or MAIN_PART_LABEL}»: {note}" for note in part.notes)

    material, material_notes = _join_materials(primary.materials)
    notes.extend(material_notes)

    if broken:
        status = STATUS_PARTIAL
    elif primary.repaired:
        status = STATUS_REPAIRED
    else:
        status = STATUS_PARSED
    return ParsedLine(
        status=status,
        year_created=primary.dating,
        year_lower=primary.year,
        master_name=primary.master,
        material=material,
        techniques=_join_techniques(primary.techniques),
        origin_place=primary.place,
        provenance=primary.provenance,
        precision=primary.precision,
        notes=tuple(notes),
    )
