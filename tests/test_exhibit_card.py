"""Унифицированная карточка предмета (баг-репорт 31.08.2026, п. I-2).

Музей попросил сделать карточку «более понятной для пользователя и прогнозируемой» и
перечислил состав дословно: «название предмета, изображение, расположение (название и номер
зала, номер витрины), год создания, фирма и мастер, материалы, техники, описание». Ключевое
слово — ПРОГНОЗИРУЕМАЯ: набор и порядок полей всегда одинаковы, поля не пропадают, когда
пустые, а расположение приходит готовой строкой, а не собирается фронтом из трёх ручек.

Отсюда четыре группы проверок:

  • ПОРЯДОК и СОСТАВ полей — зафиксированы литеральными списками. Порядок объявления в
    Pydantic попадает и в JSON, и в openapi, то есть это контракт; тест ловит и случайную
    перестановку, и потерю поля при перестановке.
  • ПРОГНОЗИРУЕМОСТЬ — у пустейшего экспоната (без витрины, без мастера) в ответе те же
    ключи, `location` и `maker` — объекты, а не `null`.
  • ОДНА ФОРМУЛИРОВКА РАСПОЛОЖЕНИЯ — `crud.to_exhibit_location(ex).text_in` символ в символ
    равно тому, что печатает ИИ-гид (`guide._location_phrase`). Это антирегресс на «третью
    реализацию»: разъехавшиеся фразы про одно и то же место увидит музей, а не мы.
  • РАЗБОР «фирмы и мастера» — на реальных значениях прода, вместе с инвариантом «части —
    дословные подстроки исходной строки»: испортить данные разбор не может физически, но
    подменить слово-маркер («фабрику» на «фирму») — вполне, и этого делать нельзя.

Ни БД, ни сети: зал, витрина, экспонат и галерея — простые дата-классы с теми же полями,
что читают сериализаторы. Запуск:
    python -m pytest tests/test_exhibit_card.py
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import crud  # noqa: E402
from app import schemas as sch  # noqa: E402
from app.routers import guide  # noqa: E402
from app.services.catalog_line import parse_catalog_line, split_maker  # noqa: E402


# ── Минимальные заменители ORM ───────────────────────────────────────────────
@dataclass
class Hall:
    id: int = 4
    hall_number: Optional[int] = 4
    name: Optional[str] = "Синяя гостиная"
    is_temporary: bool = False


@dataclass
class Showcase:
    id: int = 32
    showcase_number: Optional[int] = 5
    name: Optional[str] = None
    hall: Optional[Hall] = field(default_factory=Hall)


@dataclass
class Image:
    id: int = 1
    url: str = "https://cdn.example/exhibits/lilies/01.jpg"
    alt: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    is_primary: bool = True


@dataclass
class Exhibit:
    id: int = 101
    exhibit_number: Optional[str] = "12"
    label_slug: Optional[str] = "faberge_egg_lilies"
    name: str = "Яйцо «Ландыши»"
    year_created: Optional[str] = "1898"
    origin_place: Optional[str] = "Санкт-Петербург"
    master_name: Optional[str] = "Фирма К. Фаберже, мастер М. Перхин"
    material: Optional[str] = "Золото, эмаль, жемчуг"
    techniques: Optional[str] = "эмаль по гильошированному фону, гравировка"
    short_description: Optional[str] = "Пасхальный подарок Николая II Александре Фёдоровне."
    image_url: Optional[str] = "https://cdn.example/exhibits/lilies/01.jpg"
    video_url: Optional[str] = None
    model_3d_url: Optional[str] = None
    model_3d_embed: Optional[str] = None
    audio_url: Optional[str] = None
    source_url: Optional[str] = None
    raw_history: Optional[str] = None
    short_description_spoken: Optional[str] = None
    updated_at: None = None
    showcase_id: Optional[int] = 32
    showcase: Optional[Showcase] = field(default_factory=Showcase)
    images: List[Image] = field(default_factory=lambda: [Image()])


def empty_exhibit() -> Exhibit:
    """Самая бедная карточка прода: без витрины, без мастера, без описания и фото.

    Экспонаты с `showcase_id IS NULL` на проде есть — ср. scripts/fix_showcase_orphans.py.
    """
    return Exhibit(
        year_created=None, origin_place=None, master_name=None, material=None, techniques=None,
        short_description=None, image_url=None, showcase_id=None, showcase=None, images=[],
    )


# Состав и порядок, которые музей задал 31.08.2026. Списки литеральные СОЗНАТЕЛЬНО:
# сгенерировать их из самой схемы значило бы проверять схему ею же.
EXHIBIT_FIELDS = [
    "id", "exhibit_number", "label_slug",
    "name",
    "image_url", "images",
    "location", "hall", "showcase",
    "year_created", "origin_place",
    "maker", "master_name",
    "material", "techniques",
    "short_description",
    "video_url", "model_3d_url", "model_3d_embed", "audio_url", "source_url",
]
SUMMARY_FIELDS = [
    "id", "exhibit_number", "label_slug",
    "name",
    "thumbnail_url",
    "location", "hall_id", "showcase_id", "showcase_number",
    "year_created",
    "maker", "master_name",
    "is_temporary",
]


# ── Порядок и состав полей ───────────────────────────────────────────────────
def test_exhibit_field_order_is_the_one_museum_asked_for():
    """Порядок объявления = порядок ключей в JSON и в openapi, значит это контракт."""
    assert list(sch.Exhibit.model_fields) == EXHIBIT_FIELDS


def test_summary_field_order_matches_the_card():
    """Плашка «зал/витрина» рисуется одинаково в каталоге и в карточке — и поля те же."""
    assert list(sch.ExhibitSummary.model_fields) == SUMMARY_FIELDS


def test_museum_order_puts_image_and_location_before_dating():
    """Дословно со скриншота: над названием — расположение, дата и место, фирма и мастер.

    Проверяем не только полное совпадение списка выше, но и сами отношения порядка —
    чтобы при следующей правке было видно, ЧТО именно защищается.
    """
    order = list(sch.Exhibit.model_fields)
    assert order.index("name") < order.index("image_url") < order.index("location")
    assert order.index("location") < order.index("year_created") < order.index("origin_place")
    assert order.index("origin_place") < order.index("maker") < order.index("material")
    assert order.index("material") < order.index("techniques") < order.index("short_description")


# ── Прогнозируемость: поля не пропадают ──────────────────────────────────────
def test_keys_do_not_disappear_on_an_empty_card():
    """Пустая карточка отдаёт ТЕ ЖЕ ключи — пустое приходит как null, а не отсутствием ключа."""
    full = crud.to_exhibit(Exhibit()).model_dump()
    empty = crud.to_exhibit(empty_exhibit()).model_dump()
    assert list(full) == EXHIBIT_FIELDS
    assert list(empty) == EXHIBIT_FIELDS
    assert empty["hall"] is None and empty["showcase"] is None      # legacy-поля честно null
    assert empty["origin_place"] is None and empty["master_name"] is None


def test_location_and_maker_are_always_objects():
    """`location`/`maker` пустеют ВНУТРЬ. Иначе фронт пишет три уровня проверок."""
    empty = crud.to_exhibit(empty_exhibit())
    assert isinstance(empty.location, sch.ExhibitLocation)
    assert isinstance(empty.maker, sch.ExhibitMaker)
    assert empty.model_dump()["location"] == {
        "hall_id": None, "hall_number": None, "hall_name": None,
        "showcase_id": None, "showcase_number": None, "showcase_name": None,
        "text": None, "text_in": None,
    }
    assert empty.model_dump()["maker"] == {"text": None, "firm": None, "master": None}


def test_bare_schema_instance_has_the_full_key_set():
    """Даже собранная руками схема отдаёт весь набор: ни одного exclude_none в проекте нет."""
    assert list(sch.Exhibit(id=1, name="x").model_dump()) == EXHIBIT_FIELDS
    assert list(sch.ExhibitSummary(id=1, name="x").model_dump()) == SUMMARY_FIELDS
    assert sch.Exhibit(id=1, name="x").model_dump()["location"]["text"] is None


def test_summary_keeps_the_full_key_set_too():
    full = crud.to_exhibit_summary(Exhibit()).model_dump()
    empty = crud.to_exhibit_summary(empty_exhibit()).model_dump()
    assert list(full) == list(empty) == SUMMARY_FIELDS
    assert full["location"]["text"] == "Зал 4 «Синяя гостиная», витрина 5"
    assert empty["location"]["text"] is None


# ── Обратная совместимость ───────────────────────────────────────────────────
def test_legacy_fields_are_still_there():
    """hall/showcase/master_name дублируют новые поля, но их нельзя убирать: клиенты в проде."""
    card = crud.to_exhibit(Exhibit())
    assert card.hall is not None and card.hall.hall_number == 4
    assert card.showcase is not None and card.showcase.showcase_number == 5
    assert card.master_name == "Фирма К. Фаберже, мастер М. Перхин"
    assert card.maker.text == card.master_name          # инвариант: maker.text = master_name
    summary = crud.to_exhibit_summary(Exhibit())
    assert (summary.hall_id, summary.showcase_id, summary.showcase_number) == (4, 32, 5)
    assert summary.master_name == summary.maker.text


def test_admin_view_still_carries_internal_fields():
    """Публичная карточка не обросла внутренними полями, админская их не потеряла."""
    admin = crud.to_exhibit(Exhibit(raw_history="Заказчик — Николай II."), admin=True)
    assert admin.raw_history == "Заказчик — Николай II."
    assert "short_description_spoken" in sch.ExhibitAdmin.model_fields
    assert "raw_history" not in sch.Exhibit.model_fields
    assert list(sch.Exhibit(id=1, name="x").model_dump()).count("raw_history") == 0


def test_showcase_name_reaches_the_card():
    """Название витрины раньше не доезжало вовсе — заполнять showcase_name было нечем."""
    ex = Exhibit(showcase=Showcase(name="Пасхальные подарки"))
    assert crud.to_exhibit(ex).showcase.name == "Пасхальные подарки"
    assert crud.to_exhibit_location(ex).showcase_name == "Пасхальные подарки"


# ── Расположение: одна формулировка на весь бэкенд ───────────────────────────
LOCATION_CASES = [
    # (экспонат, text, text_in)
    (Exhibit(), "Зал 4 «Синяя гостиная», витрина 5", "в зале 4 «Синяя гостиная», витрина 5"),
    (Exhibit(showcase=Showcase(showcase_number=None)),
     "Зал 4 «Синяя гостиная», вне витрин", "в зале 4 «Синяя гостиная», вне витрин"),
    # Зал без номера — «Вне постоянной экспозиции» (требование заказчика 28.07.2026, п.5):
    # «зал None» посетителю не показываем никогда.
    (Exhibit(showcase=Showcase(showcase_number=1,
                               hall=Hall(9, None, "Вне постоянной экспозиции"))),
     "Зал «Вне постоянной экспозиции», витрина 1", "в зале «Вне постоянной экспозиции», витрина 1"),
    # Витрина-сирота: зал не привязан — фраза начинается сразу с витрины.
    (Exhibit(showcase=Showcase(hall=None)), "Витрина 5", "витрина 5"),
]


def test_location_text_is_ready_to_show():
    for ex, text, text_in in LOCATION_CASES:
        where = crud.to_exhibit_location(ex)
        assert where.text == text
        assert where.text_in == text_in
    assert "None" not in " ".join(crud.to_exhibit_location(ex).text or "" for ex, _, _ in LOCATION_CASES)


def test_card_location_matches_what_the_guide_says():
    """Антирегресс на «третью реализацию»: карточка и гид — символ в символ одна фраза."""
    for ex, _, _ in LOCATION_CASES:
        assert crud.to_exhibit_location(ex).text_in == guide._location_phrase(ex)


def test_guide_location_carries_the_same_phrase():
    """Блок «где искать» (B7) отдаёт ту же строку — четвёртой склейки на фронте не будет."""
    ex = Exhibit()
    where = crud.to_location(ex)
    assert where is not None
    assert where.text == crud.to_exhibit_location(ex).text
    assert where.text_in == guide._location_phrase(ex)
    assert (where.hall_id, where.showcase_id) == (4, 32)
    # Семантику «None у непривязанного экспоната» менять нельзя: на неё смотрит ветка B7.
    assert crud.to_location(empty_exhibit()) is None


def test_location_structure_is_filled_for_links():
    where = crud.to_exhibit_location(Exhibit())
    assert (where.hall_id, where.hall_number, where.hall_name) == (4, 4, "Синяя гостиная")
    assert (where.showcase_id, where.showcase_number) == (32, 5)


# ── «Фирма и мастер» ─────────────────────────────────────────────────────────
MAKER_CASES = [
    # (master_name, firm, master) — значения с прода и из путеводителя.
    ("Фирма К. Фаберже, мастер М. Перхин", "Фирма К. Фаберже", "мастер М. Перхин"),
    ("Фирма И. Морозова, мастерская В. Иванова", "Фирма И. Морозова", "мастерская В. Иванова"),
    # Кавычки источника сохраняются (db/guide_fixes_20260812.json: «Фирма «К. Э. Болин»»).
    ("Фирма «К. Э. Болин», мастер Н. Черноков", "Фирма «К. Э. Болин»", "мастер Н. Черноков"),
    ("Николай Черноков", None, "Николай Черноков"),
    ("Фабрика Д. Шелапутина", "Фабрика Д. Шелапутина", None),
    ("Первая серебряная артель", "Первая серебряная артель", None),
    ("Генрих Семирадский (1843–1902)", None, "Генрих Семирадский (1843–1902)"),
    ("Национальная фарфоровая мануфактура. Автор модели Ж.-Б.-Г. Делуа (1848–1899)",
     "Национальная фарфоровая мануфактура", "Автор модели Ж.-Б.-Г. Делуа (1848–1899)"),
    # Несколько исполнителей после точки (id 528): режем один раз, хвост уходит целиком.
    ("Императорский фарфоровый завод. Исполнители росписи П. Столетов, В. Иванов",
     "Императорский фарфоровый завод", "Исполнители росписи П. Столетов, В. Иванов"),
    # В начале строки «Мастерская» — предприятие, а не исполнитель (id 633).
    ("Мастерская Дж. Бриджа", "Мастерская Дж. Бриджа", None),
    ("Неизвестный мастер", None, "Неизвестный мастер"),
]


def test_split_maker_on_real_values():
    for text, firm, master in MAKER_CASES:
        parts = split_maker(text)
        assert (parts.text, parts.firm, parts.master) == (text, firm, master), text


def test_split_maker_on_empty_input():
    for empty in (None, "", "   "):
        parts = split_maker(empty)
        assert (parts.text, parts.firm, parts.master) == (None, None, None)


def test_split_maker_never_rewrites_the_source():
    """Части — ДОСЛОВНЫЕ подстроки исходной строки, вместе со словом-маркером.

    «Фирма», «Фабрика», «мастерская» — формулировки самого путеводителя; подменять их ради
    единообразной вёрстки мы не вправе, а склеить обратно фронт должен уметь всегда.
    """
    for text, firm, master in MAKER_CASES:
        for part in (firm, master):
            if part is not None:
                assert part in text
        assert (firm or master) is not None                    # хоть одна часть заполнена
        head = firm if firm is not None else master
        assert text.startswith(head)                           # левая часть — начало строки


def test_maker_text_is_always_authoritative():
    """Разобрать не удалось — обе части null, но text заполнен: фронту всегда есть что рисовать."""
    weird = "Сделано по эскизу неизвестного лица, около 1900"
    parts = split_maker(weird)
    assert parts.text == weird
    assert parts.firm is None and parts.master == weird        # целиком в мастера, ничего не потеряно


def test_parse_catalog_line_master_name_is_unchanged():
    """Разрез — производный: сам разбор каталожной строки остался прежним байт в байт."""
    line = ("Санкт-Петербург, 1899–1903. Фирма К. Фаберже, мастер М. Перхин. "
            "Золото, серебро, сталь, сапфир; штамп, чеканка, гравировка, золочение")
    parsed = parse_catalog_line(line)
    assert parsed.master_name == "Фирма К. Фаберже, мастер М. Перхин"
    assert parsed.origin_place == "Санкт-Петербург"
    parts = split_maker(parsed.master_name)
    assert (parts.firm, parts.master) == ("Фирма К. Фаберже", "мастер М. Перхин")


def test_card_maker_is_split():
    card = crud.to_exhibit(Exhibit())
    assert card.maker.firm == "Фирма К. Фаберже"
    assert card.maker.master == "мастер М. Перхин"
    assert crud.to_exhibit_summary(Exhibit()).maker.firm == "Фирма К. Фаберже"


# ── Место создания (Д4: «Дата создания И МЕСТО») ─────────────────────────────
def test_origin_place_reaches_the_card():
    assert crud.to_exhibit(Exhibit()).origin_place == "Санкт-Петербург"
    assert crud.to_exhibit(Exhibit(origin_place=None)).origin_place is None
    # Колонка есть у модели и пишется через админку — иначе бэкфиллу некуда класть разбор.
    assert "origin_place" in sch.ExhibitCreate.model_fields
    assert "origin_place" in sch.ExhibitPatch.model_fields
    assert "origin_place" in sch.ExhibitUpdate.model_fields


def test_origin_place_is_not_in_the_list_response():
    """В списке места нет сознательно: музей просил его в карточке, а страница списка и без
    того подросла на location/maker."""
    assert "origin_place" not in sch.ExhibitSummary.model_fields


# ── Контракт в openapi.yaml ──────────────────────────────────────────────────
def _openapi() -> dict:
    import yaml                                     # PyYAML — зависимость проекта

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "openapi.yaml")
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_openapi_exhibit_matches_the_schema():
    """`required` и порядок свойств в спецификации — те же, что в Pydantic.

    Контракт ведётся в двух файлах, и разъезжаются они молча: у `app/schemas.py` есть
    тесты, у `openapi.yaml` — только глаза. Прогнозируемость обещана именно в спецификации
    («набор ключей постоянен»), поэтому обещание и проверяем.
    """
    schema = _openapi()["components"]["schemas"]["Exhibit"]
    assert list(schema["properties"]) == EXHIBIT_FIELDS
    assert sorted(schema["required"]) == sorted(EXHIBIT_FIELDS)


def test_openapi_exhibit_example_is_complete():
    """Пример карточки обязан проходить по собственному `required` — все 21 ключ.

    Неполный пример — это не косметика: линтер спецификации на нём падает, а фикстура,
    снятая с него, воспроизводит ровно ту непрогнозируемость («ключа просто нет»), ради
    ухода от которой правку и делали.
    """
    schema = _openapi()["components"]["schemas"]["Exhibit"]
    example = schema["example"]
    assert [key for key in schema["required"] if key not in example] == []
    assert [key for key in example if key not in schema["properties"]] == []
    assert list(example) == EXHIBIT_FIELDS          # порядок ключей — тоже часть обещания
    # Пустое в примере показано как null, а не пропуском ключа — иначе пример учит плохому.
    assert example["model_3d_embed"] is None and example["audio_url"] is None
    assert set(example["location"]) == set(_openapi()["components"]["schemas"]["ExhibitLocation"]["required"])
    assert set(example["maker"]) == set(_openapi()["components"]["schemas"]["ExhibitMaker"]["required"])


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
