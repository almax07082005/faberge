"""Проверка «этот экспонат действительно назван в реплике» (блок «Упомянуто в ответе»).

Зачем. Музей в баг-репорте 31.08.2026 (п. II-1) написал дословно: «в разделе
"Упомянуто в ответе" упоминаются предметы, которые не упомянуты. Нужен ли этот
раздел вообще в таком случае?». Раздел нужен — но только когда предмет
действительно назван; пустой блок фронт не рисует.

Откуда брался мусор. Плашки наполнялись первыми четырьмя строками
полнотекстового поиска (`crud.search_exhibits_orm`), а у него нет порога: в
`WHERE` стоит факт совпадения, `ts_rank` участвует только в `ORDER BY`. Плюс
tsquery там OR-овый («совпало ЛЮБОЕ значимое слово»), а на вход подавался вопрос
ВМЕСТЕ с целым ответом гида — сотня слов. В поисковый вектор входит `raw_history`
(вес D) — простыни на тысячи знаков с «императрица», «Санкт-Петербург»,
«золото», — поэтому длинная история почти всегда обгоняла карточку, названную по
имени. Отсюда обе половины дефекта на скриншотах музея: в блоке стояли чужие
предметы, а реально упомянутый в четвёрку не попадал.

Почему проверка упоминания, а не порог по `ts_rank`. `ts_rank` не нормирован —
он зависит от длины документа, длины запроса и числа совпавших лексем, поэтому
одна константа на коротком вопросе отсечёт всё, а на длинном ответе — ничего.
Калибровать её можно только по живым данным, а прод-БД нам недоступна. И главное:
порог не отличает «совпало по собственному имени предмета» от «совпало по пяти
общим словам в `raw_history`» — а музей жалуется ровно на второе. Проверка
«название карточки действительно встречается в тексте» отвечает на вопрос музея
буквально, детерминирована и тестируется без БД и сети.

Нормализация — только та, что уже есть в репозитории: `recognizer.normalize_name`
(NFKC, кавычки, ё→е, регистр — она же сшивает названия ML-индекса с каталогом) и
`question_cluster.normalize`/`stem` (пунктуация, пробелы, русские окончания).
Третьего нормализатора здесь сознательно не заводится.

Модуль чистый: ни БД, ни сети, ни ORM, ни промптов — на вход приходят пары
«название, номер экспоната», на выход уходят индексы этих пар.
"""
from __future__ import annotations

import re
from typing import Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

# `_STOPWORDS` берём у кластеризатора вопросов намеренно: это тот же самый список
# служебных слов русского языка, и держать рядом второй его экземпляр — верный
# способ развести их через полгода.
from .question_cluster import _STOPWORDS, normalize, stem
from .recognizer import normalize_name

# Кавычки, в которых каталог держит собственное имя предмета: «Пасхальное яйцо
# «Ландыши»». Апострофы (' и ’) сюда НЕ входят — они бывают внутри слова.
_QUOTE_CHARS = "«»„“”\""
_QUOTE_SPLIT_RE = re.compile("[" + re.escape(_QUOTE_CHARS) + "]")
# Конец «опознавательной головы» названия без кавычек: «Лауриц Туксен. Коронация
# Николая II и Александры Фёдоровны в Успенском соборе» → «Лауриц Туксен».
_HEAD_SPLIT_RE = re.compile(r"[.,(;:]")
_SPACES_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"\S+")
# Буква или цифра — левая граница прямого совпадения: «орден» не должен
# находиться внутри «порядок».
_WORD_CHAR = r"[0-9A-Za-zА-Яа-яЁё]"


class Mention(NamedTuple):
    """Подтверждённое упоминание карточки.

    :param index: позиция карточки во ВХОДНОЙ последовательности (ORM сюда не
        передаётся, поэтому вызывающий код сам достаёт объект по индексу).
    :param source: где названа — ``"answer"`` или ``"question"``. Нужен, чтобы
        фронт мог честно озаглавить блок: «Упомянуто в ответе» против
        «Вы спрашивали про».
    """

    index: int
    source: str


class _Hit(NamedTuple):
    """Совпадение внутри одного текста (внутренняя структура)."""

    index: int
    offset: int              # позиция в подготовленном тексте — для порядка чтения
    stems: frozenset         # стеммы ядра названия — для снятия неоднозначности
    by_number: bool          # совпало по номеру «№12» — такие не режем как неоднозначные


# ── Нормализация ─────────────────────────────────────────────────────────────
def _prepare(text: str) -> str:
    """Единый вид текста и названия: композиция двух ГОТОВЫХ нормализаций.

    Сначала `recognizer.normalize_name` (NFKC, кавычки → пробел, ё→е, регистр),
    затем `question_cluster.normalize` (пунктуация → пробел, схлопывание
    пробелов). Дефис приравниваем к пробелу — «пресс-папье» в ответе гида и в
    карточке пишется по-разному чаще, чем хотелось бы.

    Важно, что подготовка ОДНА и та же для текста и для названия: и прямое
    совпадение, и морфологическое считают позиции в одной и той же строке,
    поэтому порядок плашек получается общий и сравнимый.
    """
    flat = normalize(normalize_name(text or "")).replace("-", " ")
    return _SPACES_RE.sub(" ", flat).strip()


def name_core(name: str) -> str:
    """«Опознавательное ядро» названия — то, чем предмет реально называют.

    Требовать совпадения названия целиком нельзя: гид говорит «яйцо „Ландыши“»,
    а в каталоге лежит «Пасхальное яйцо «Ландыши»». Поэтому:

      1. если в названии есть часть в кавычках — берём её («Ландыши»);
      2. иначе — голову до первой `.`/`,`/`(`/`;`/`:` («Лауриц Туксен. Коронация
         Николая II…» → «Лауриц Туксен»).

    Название без кавычек и без пунктуации («Блюдо с монограммами») остаётся
    целиком — и совпасть должно тоже целиком.
    """
    text = (name or "").strip()
    if not text:
        return ""
    parts = _QUOTE_SPLIT_RE.split(text)
    if len(parts) >= 3 and parts[1].strip():
        return parts[1].strip()
    head = _HEAD_SPLIT_RE.split(text)[0].strip()
    return head or text


def _significant_tokens(prepared: str) -> List[Tuple[str, int]]:
    """Значимые слова подготовленного текста и их позиции.

    Служебные слова («с», «из», «в») выбрасываем: иначе «Блюдо с монограммами»
    требовало бы предлога, а он в живой речи гида легко теряется.
    """
    out: List[Tuple[str, int]] = []
    for match in _TOKEN_RE.finditer(prepared):
        token = match.group(0)
        if len(token) < 2 or token in _STOPWORDS:
            continue
        out.append((token, match.start()))
    return out


def _stem_offsets(prepared: str) -> Dict[str, int]:
    """Стем → позиция ПЕРВОГО вхождения. Стем — `question_cluster.stem`."""
    out: Dict[str, int] = {}
    for token, pos in _significant_tokens(prepared):
        key = stem(token)
        if key and key not in out:
            out[key] = pos
    return out


def _core_stems(prepared_core: str) -> Set[str]:
    """Множество стеммов ядра названия."""
    return {stem(token) for token, _ in _significant_tokens(prepared_core) if stem(token)}


# ── Отдельные проверки ───────────────────────────────────────────────────────
def _direct_offset(prepared_core: str, prepared_text: str) -> Optional[int]:
    """Прямое совпадение: ядро названия как подстрока текста, с левой границей слова."""
    if not prepared_core or not prepared_text:
        return None
    match = re.search(r"(?<!" + _WORD_CHAR + ")" + re.escape(prepared_core), prepared_text)
    return match.start() if match else None


def _morphological_offset(stems: Set[str], stem_offsets: Dict[str, int]) -> Optional[int]:
    """Морфологическое совпадение: ВСЕ стеммы ядра есть в тексте.

    «о Коронационном яйце» → стем `коронационн` совпадает с ядром
    «Коронационное». Требование «все стеммы» — то, что не пускает в блок «Блюдо
    с монограммами» на ответе про яйцо: одного «блюда» мало, нужны и
    «монограммы».

    Позиция — там, где название СОБРАЛОСЬ целиком (максимум по стеммам), а не
    там, где мелькнуло первое общее слово.
    """
    if not stems or not stems <= set(stem_offsets):
        return None
    return max(stem_offsets[s] for s in stems)


def _number_offset(number: Optional[str], raw_text: str, prepared_text: str) -> Optional[int]:
    """Совпадение по номеру — только в явной форме «№12» / «номер 12».

    Голое число номером не считаем осознанно: ответ гида полон годов и
    количеств («в 1912 году»), и `guide_intel.parse_exhibit_number` про эту же
    неоднозначность уже предупреждает.

    Сам знак «№» ищем в СЫРОМ тексте: обе наши нормализации его теряют —
    `question_cluster.normalize` сносит как пунктуацию, а `normalize_name` через
    NFKC раскладывает «№» в «No». Позиция при этом берётся из подготовленного
    текста, чтобы остаться в общей шкале с остальными проверками.
    """
    num = (number or "").strip()
    if not num or not raw_text:
        return None
    pattern = r"(?:№|\bномер)\s*0*" + re.escape(num) + r"(?![0-9A-Za-zА-Яа-яЁё])"
    if not re.search(pattern, raw_text, re.IGNORECASE):
        return None
    prepared_num = _prepare(num)
    if prepared_num:
        found = re.search(
            r"(?<!" + _WORD_CHAR + ")" + re.escape(prepared_num) + r"(?!" + _WORD_CHAR + ")",
            prepared_text,
        )
        if found:
            return found.start()
    return 0


# ── Снятие неоднозначности ───────────────────────────────────────────────────
def _drop_ambiguous(hits: List[_Hit]) -> List[_Hit]:
    """Убрать совпадения, которые ни на что конкретное не указывают.

    Без рукописного словаря «родовых» слов, по одному правилу поглощения:

    * ядро одной совпавшей карточки — СТРОГОЕ подмножество ядра другой
      совпавшей: «Ваза» уходит, если рядом совпала «Ваза с изображением
      цветов» — гид назвал вторую;
    * ядра РАВНЫ: уходят обе. Дубли имён в каталоге реальны и массовы: карточек
      с названием ровно «Портсигар» на снимке 01.09.2026 — 162. Гид не сказал,
      какой именно, и показывать наугад хуже, чем не показывать.
      Не путать с «двенадцатью Портсигарами» из `crud.slug_by_name`: там речь про
      карту распознавания, которая строится ТОЛЬКО по карточкам с `label_slug`,
      а сюда кандидаты приходят полнотекстовым поиском по всему каталогу.

    Совпадения по НОМЕРУ сами не режутся (номер однозначен), но СРАВНИВАТЬ с
    ними надо: на «этот портсигар — №44» карточка «Портсигар» №12 совпала бы по
    родовому слову и встала рядом с той единственной, которую гид назвал точно.
    """
    drop: Set[int] = set()
    for a in hits:
        if a.by_number or not a.stems:
            continue
        for b in hits:
            if a.index == b.index or not b.stems:
                continue
            # Равенство ядер разбирается симметрично: каждая из карточек
            # выбывает на своей итерации внешнего цикла (кроме совпавших по
            # номеру — те не выбывают вовсе).
            if a.stems == b.stems or a.stems < b.stems:
                drop.add(a.index)
                break
    return [h for h in hits if h.index not in drop]


# ── Публичный интерфейс ──────────────────────────────────────────────────────
def mentioned_indexes(text: str, cards: Sequence[Tuple[str, Optional[str]]]) -> List[int]:
    """Индексы карточек `(название, номер)`, реально названных в `text`.

    Порядок результата — порядок ЧТЕНИЯ (по позиции первого упоминания), а не
    порядок кандидатов: блок называется «Упомянуто в ответе», и читаться он
    должен так же, как ответ.
    """
    prepared_text = _prepare(text)
    raw_text = text or ""
    stem_offsets = _stem_offsets(prepared_text)

    hits: List[_Hit] = []
    for i, (name, number) in enumerate(cards):
        # Ядро считаем и для совпавших по номеру: без него правило поглощения не
        # увидит, что «Портсигар» №12 — то же родовое слово, что и названный №44.
        core = _prepare(name_core(name or ""))
        stems = frozenset(_core_stems(core)) if core else frozenset()

        offset = _number_offset(number, raw_text, prepared_text)
        if offset is not None:
            hits.append(_Hit(i, offset, stems, True))
            continue
        if not prepared_text or not core:
            # Пустое или чисто служебное название (««»», «б/н») опознать нечем —
            # такая карточка в блок не попадает никогда.
            continue
        offset = _direct_offset(core, prepared_text)
        if offset is None:
            offset = _morphological_offset(stems, stem_offsets)
        if offset is None:
            continue
        hits.append(_Hit(i, offset, stems, False))

    survivors = _drop_ambiguous(hits)
    survivors.sort(key=lambda h: (h.offset, h.index))
    return [h.index for h in survivors]


def mentioned_in_dialogue(
    answer: str, question: str, cards: Sequence[Tuple[str, Optional[str]]]
) -> List[Mention]:
    """Двухступенчато: сначала по ОТВЕТУ, и только если он никого не назвал — по вопросу.

    Ответ первичен — блок так и называется. Но откатываться к вопросу
    обязательно: на «где найти яйцо „Ландыши“» гид отвечает «в зале 4, витрина
    5», названия в ответе нет вовсе, а плашка там законна и нужна (на ней держится
    навигационная ветка B7). Откуда взялась плашка, видно по `Mention.source`.
    """
    hits = mentioned_indexes(answer, cards)
    if hits:
        return [Mention(i, "answer") for i in hits]
    return [Mention(i, "question") for i in mentioned_indexes(question, cards)]
