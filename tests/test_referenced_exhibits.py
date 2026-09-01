"""Блок «Упомянуто в ответе»: показываем только реально названные предметы (п. II-1, 31.08.2026).

Музей дословно: «В разделе "Упомянуто в ответе" в диалоге с AI-гидом упоминаются
предметы, которые не упомянуты. Нужен ли этот раздел вообще в таком случае?»
Наш ответ: раздел нужен, но только когда предмет действительно упомянут; пустой
блок фронт не рисует.

Раньше блок наполнялся первыми четырьмя строками `crud.search_exhibits_orm`, а у
неё нет порога релевантности — выборка непустая всегда, и четвёрка «добивалась»
чужими предметами. Ниже — дословные пары со скриншотов музея: ответ и те самые
плашки, которые к нему приезжали.

Ни БД, ни сети: `app/services/guide_mentions.py` — чистый модуль, на вход идут
пары «название, номер экспоната».

Запуск: python -m pytest tests/test_referenced_exhibits.py
"""
from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import schemas as sch
from app.routers import guide
from app.services import guide_mentions as gm


def _cards(*pairs: Tuple[str, Optional[str]]) -> List[Tuple[str, Optional[str]]]:
    """Кандидаты в блок: (название карточки, exhibit_number)."""
    return list(pairs)


# ── Скриншот 1: ответ про яйцо «Орден Святого Георгия» ───────────────────────
# Дословно из баг-репорта: на этот ответ в блоке стояли «Блюдо с монограммами»,
# «Бювар с монограммами» и «Лауриц Туксен. Коронация…» — ни один не упомянут.
ORDER_ANSWER = (
    'Яйцо "Орден Святого Георгия" вывезла из России в 1919 году императрица '
    "Мария Фёдоровна. Это единственное императорское пасхальное яйцо Фаберже, "
    "которое покинуло страну вместе с владелицей, а не было конфисковано."
)

# ── Скриншот 2: ответ про браслет со львиными головами ───────────────────────
# В блоке стояли «Набалдашник трости», «Набор из двенадцати…», «Ваза с
# изображением цветов».
BRACELET_ANSWER = (
    "Браслет со львиными головами создал мастер Эрик Коллин около 1880 года. "
    "Массивные львиные головы отсылают к древним скифским украшениям, найденным "
    "при раскопках в Крыму."
)


def test_order_of_st_george_does_not_pull_foreign_plaques():
    """Скриншот музея №1: ни одна из трёх чужих плашек в блок не попадает."""
    cards = _cards(
        ("Блюдо с монограммами", "24"),
        ("Бювар с монограммами", "25"),
        ("Лауриц Туксен. Коронация Николая II и Александры Фёдоровны в Успенском соборе", "8"),
    )
    assert gm.mentioned_indexes(ORDER_ANSWER, cards) == []


def test_order_of_st_george_keeps_the_exhibit_actually_named():
    """Обратная половина того же дефекта: упомянутый предмет из топ-4 ВЫТЕСНЯЛСЯ.

    Кавычки и слово «Пасхальное» в названии совпадению не мешают — их снимает
    нормализация, уже применяемая при сшивке названий (recognizer.normalize_name).
    """
    cards = _cards(
        ("Блюдо с монограммами", "24"),
        ("Пасхальное яйцо «Орден Святого Георгия»", "31"),
        ("Бювар с монограммами", "25"),
    )
    assert gm.mentioned_indexes(ORDER_ANSWER, cards) == [1]


def test_bracelet_does_not_pull_foreign_plaques():
    """Скриншот музея №2: набалдашник, набор из двенадцати и ваза — мимо."""
    cards = _cards(
        ("Набалдашник трости", "17"),
        ("Набор из двенадцати предметов", "18"),
        ("Ваза с изображением цветов", "19"),
    )
    assert gm.mentioned_indexes(BRACELET_ANSWER, cards) == []


def test_bracelet_keeps_the_bracelet():
    """Сам браслет остаётся: название без кавычек совпадает целиком."""
    cards = _cards(
        ("Набалдашник трости", "17"),
        ("Браслет со львиными головами", "20"),
        ("Ваза с изображением цветов", "19"),
    )
    assert gm.mentioned_indexes(BRACELET_ANSWER, cards) == [1]


# ── Положительные попадания ──────────────────────────────────────────────────
def test_direct_hit_ignores_quotes_and_case():
    """Гид говорит «яйцо "Ландыши"», в каталоге — «Пасхальное яйцо «Ландыши»»."""
    answer = 'Яйцо "Ландыши" Николай II подарил Александре Фёдоровне в 1898 году.'
    assert gm.mentioned_indexes(answer, _cards(("Пасхальное яйцо «Ландыши»", "3"))) == [0]


def test_morphological_hit_matches_russian_cases():
    """«о Коронационном яйце» → карточка «Пасхальное яйцо «Коронационное»»."""
    answer = "Сейчас речь о Коронационном яйце — самом известном в собрании."
    assert gm.mentioned_indexes(answer, _cards(("Пасхальное яйцо «Коронационное»", "12"))) == [0]


def test_unquoted_name_must_match_in_full():
    """Название без кавычек совпадает целиком — одного родового слова мало.

    Ровно это правило и не пускает «Блюдо с монограммами» в ответ про яйцо:
    «блюдо» без «монограмм» плашки не даёт.
    """
    cards = _cards(("Блюдо с монограммами", "24"))
    assert gm.mentioned_indexes("На витрине лежит блюдо с монограммами.", cards) == [0]
    assert gm.mentioned_indexes("На витрине лежит старинное серебряное блюдо.", cards) == []


# ── Номер экспоната ──────────────────────────────────────────────────────────
def test_number_hit_only_in_explicit_form():
    """«№12» — номер; «в 1912 году» — не номер.

    Голое число номером не считаем осознанно: ответ гида полон годов и количеств.
    """
    cards = _cards(("Портсигар", "12"))
    assert gm.mentioned_indexes("Ищите его под №12, в дальней витрине.", cards) == [0]
    assert gm.mentioned_indexes("Портсигар номер 12 стоит рядом.", cards) == [0]
    assert gm.mentioned_indexes("Изготовлено в 1912 году в Санкт-Петербурге.", cards) == []


def test_number_hit_survives_ambiguity_cut():
    """Совпадение по номеру однозначно — правило поглощения его не режет.

    Двух одноимённых «Портсигаров» по названию мы бы выбросили (см. ниже), но
    номер называет ровно один из них.
    """
    cards = _cards(("Портсигар", "12"), ("Портсигар", "44"))
    assert gm.mentioned_indexes("Этот портсигар — №44.", cards) == [1]


# ── Снятие неоднозначности ───────────────────────────────────────────────────
def test_duplicate_names_are_dropped_entirely():
    """Дубли имён в каталоге реальны и массовы: «Портсигаров» в каталоге 162 (01.09.2026).

    Гид не сказал, какой именно, — показывать наугад хуже, чем не показывать.
    """
    cards = _cards(("Портсигар", "12"), ("Портсигар", "44"))
    assert gm.mentioned_indexes("Перед вами портсигар фирмы Фаберже.", cards) == []


def test_shorter_name_absorbed_by_longer():
    """«Ваза» уходит, если рядом совпала «Ваза с изображением цветов»."""
    cards = _cards(("Ваза", "7"), ("Ваза с изображением цветов", "19"))
    answer = "Ваза с изображением цветов стоит в витрине 4."
    assert gm.mentioned_indexes(answer, cards) == [1]


# ── Порядок и границы ────────────────────────────────────────────────────────
def test_order_follows_the_text_not_the_candidates():
    """Блок читается как «упомянуто в ответе» — значит, и порядок читательский."""
    cards = _cards(("Блюдо с монограммами", "24"), ("Браслет со львиными головами", "20"))
    answer = (
        "Сначала посмотрите на браслет со львиными головами, "
        "а затем на блюдо с монограммами."
    )
    assert gm.mentioned_indexes(answer, cards) == [1, 0]


def test_degenerate_input_returns_empty_without_raising():
    """Пустые кандидаты, пробельный текст, название из одних кавычек, номер None."""
    assert gm.mentioned_indexes("Любой текст", []) == []
    assert gm.mentioned_indexes("   ", _cards(("Ваза", "7"))) == []
    assert gm.mentioned_indexes("", _cards(("Ваза", None))) == []
    assert gm.mentioned_indexes("Экспонат №12 в витрине", _cards(("«»", None))) == []
    assert gm.mentioned_indexes("Ваза стоит тут", _cards(("", None))) == []


def test_match_does_not_start_inside_another_word():
    """Прямое совпадение проверяет левую границу слова: «порядок» — не «Орден»."""
    assert gm.mentioned_indexes("Порядок работ был такой.", _cards(("Орден", "9"))) == []


def test_hyphen_is_treated_as_a_space():
    """«Пресс-папье» и «пресс папье» пишут по-разному чаще, чем хотелось бы."""
    cards = _cards(("Пресс-папье с видом Петербурга", "14"))
    assert gm.mentioned_indexes("Пресс папье с видом Петербурга стоит рядом.", cards) == [0]


def test_name_core_extracts_the_recognisable_part():
    """Ядро названия: кавычки важнее всего, иначе — голова до первой точки/запятой."""
    assert gm.name_core("Пасхальное яйцо «Ландыши»") == "Ландыши"
    assert gm.name_core('Яйцо "Орден Святого Георгия"') == "Орден Святого Георгия"
    assert gm.name_core(
        "Лауриц Туксен. Коронация Николая II и Александры Фёдоровны в Успенском соборе"
    ) == "Лауриц Туксен"
    assert gm.name_core("Блюдо с монограммами") == "Блюдо с монограммами"
    assert gm.name_core("") == ""


# ── Двухступенчатость: ответ важнее вопроса ──────────────────────────────────
def test_fallback_to_question_keeps_navigation_working():
    """«Где найти яйцо „Ландыши“» → «в зале 4, витрина 5»: названия в ответе нет.

    Без отката к вопросу навигационная ветка B7 осталась бы без плашки, а
    посетитель — без ссылки на карточку. Источник виден по `Mention.source`.
    """
    cards = _cards(("Пасхальное яйцо «Ландыши»", "3"))
    mentions = gm.mentioned_in_dialogue(
        "Оно находится в зале 4, витрина 5.", "Где найти яйцо «Ландыши»?", cards
    )
    assert mentions == [gm.Mention(0, "question")]


def test_answer_wins_over_question():
    """Блок называется «Упомянуто в ОТВЕТЕ»: если ответ кого-то назвал, вопрос не смотрим."""
    cards = _cards(("Пасхальное яйцо «Ландыши»", "3"), ("Пасхальное яйцо «Коронационное»", "12"))
    mentions = gm.mentioned_in_dialogue(
        "Речь о Коронационном яйце — оно крупнее.", "А что за яйцо «Ландыши»?", cards
    )
    assert mentions == [gm.Mention(1, "answer")]


def test_no_mentions_at_all_gives_empty_block():
    """Общий вопрос без предметов — пустой блок, и это законное состояние."""
    cards = _cards(("Блюдо с монограммами", "24"), ("Ваза с изображением цветов", "19"))
    assert gm.mentioned_in_dialogue(ORDER_ANSWER, "А когда открылся музей?", cards) == []


# ── Инвариант: контекстный экспонат в блоке всегда ───────────────────────────
@dataclass
class _StubExhibit:
    """Минимальный ORM-подобный экспонат: `crud.to_referenced_exhibit` больше ничего не спрашивает."""

    id: int
    name: str
    exhibit_number: Optional[str] = None
    image_url: Optional[str] = None
    showcase: Optional[object] = None


def test_context_exhibit_always_stays_in_the_block():
    """Посетитель стоит у предмета — плашка обязана быть, даже если гид его не назвал."""
    context = _StubExhibit(id=101, name="Пасхальное яйцо «Коронационное»", exhibit_number="12")
    out = guide._merge_referenced(context, [], [])
    assert [p.id for p in out] == [101]
    assert out[0].mentioned_in == "context"


def test_context_exhibit_is_first_and_not_duplicated():
    """Контекст идёт первым и не дублируется, если он же подтвердился проверкой."""
    context = _StubExhibit(id=101, name="Пасхальное яйцо «Коронационное»", exhibit_number="12")
    other = _StubExhibit(id=7, name="Браслет со львиными головами", exhibit_number="20")
    out = guide._merge_referenced(context, [context, other], ["answer", "answer"])
    assert [p.id for p in out] == [101, 7]
    assert out[0].mentioned_in == "context"
    assert out[1].mentioned_in == "answer"


def test_merge_referenced_caps_at_four():
    """Ограничение «не больше четырёх плашек» сохраняется."""
    found = [_StubExhibit(id=i, name=f"Экспонат {i}") for i in range(1, 8)]
    assert len(guide._merge_referenced(None, found, ["answer"] * 7)) == 4


# ── Контракт пустого блока ───────────────────────────────────────────────────
def test_empty_block_is_a_list_not_null():
    """Пустой блок — `[]`, никогда `null`: фронту нечего защищать от undefined."""
    response = sch.ChatResponse(session_id=uuid.uuid4(), answer="Музей открылся в 2013 году.")
    assert response.referenced_exhibits == []
    assert response.referenced_halls == []
    payload = response.model_dump()
    assert payload["referenced_exhibits"] == []
    assert payload["referenced_halls"] == []


def test_story_response_has_no_referenced_block_at_all():
    """`POST /guide/story` тем же дефектом не болеет — сторож на будущее.

    В `StoryResponse` полей `referenced_*` нет вовсе, retrieval в этой ручке не
    вызывается. Если блок туда когда-нибудь добавят, тест напомнит, что его надо
    наполнять через `guide_mentions`, а не «первыми N результатами поиска».
    """
    fields = set(sch.StoryResponse.model_fields)
    assert not [f for f in fields if f.startswith("referenced")]


def test_rollback_switch_and_candidate_window_are_configurable():
    """Откат без релиза и размер окна кандидатов — настройками, как у кэша подсказок."""
    from app.config import settings

    assert settings.guide_referenced_require_mention is True
    # Четырёх кандидатов не хватало: длинные raw_history вытесняли названную карточку.
    assert settings.guide_referenced_candidates > 4


def test_referenced_exhibit_keeps_all_legacy_fields():
    """Обратная совместимость: старые поля плашки на месте, `mentioned_in` только добавлено."""
    plaque = sch.ReferencedExhibit(id=1, name="Ваза")
    payload = plaque.model_dump()
    for key in ("id", "name", "exhibit_number", "thumbnail_url", "hall_number", "showcase_number"):
        assert key in payload
    assert payload["mentioned_in"] is None
