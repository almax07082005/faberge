"""PUT/PATCH админки не должны затирать поля, которых не было в теле (баг-репорт 31.08.2026, IV-1).

Музей: «Попробовали внести информацию по техникам, после чего из карточки пропало
изображение и описание». Причина — `crud.replace_exhibit` брал `data.model_dump()` без
`exclude_unset`: незаполненные поля схемы приезжали как `None` и стирали содержимое БД.
Потеря была молчаливой, и сколько карточек она задела на проде — вопрос сухого прогона
`scripts/restore_wiped_cards_20260831.py`, а не этих тестов (см. docs/task-2026-08-31-admin-put-data-loss.md).

Что здесь проверяется:
  • регрессия «Ренессанса»: PUT телом {showcase_id, name, techniques} не трогает
    image_url / material / short_description / raw_history;
  • очистка поля осталась возможной — явный `null` в теле по-прежнему стирает;
  • старая семантика достижима, но только по явному `?full_replace=true`;
  • потеря больше не молчит: `_warn_on_wipe` пишет предупреждение в лог;
  • `image_url` восстанавливается из галереи (`is_primary`), потому что он — зеркало
    строки `exhibit_images`, а не самостоятельное поле, — но ТОЛЬКО когда поля не было
    в теле: явный `{"image_url": null}` обязан очистить его и на PUT, и на PATCH;
  • остальные пути записи каталога (PATCH экспоната, PATCH зала, PATCH витрины,
    PUT /admin/halls/reorder) тем же способом НЕ затирают — сторожа против повторения;
  • `model_dump()` против `model_dump(exclude_unset=True)` на `ExhibitUpdate` — тест-объяснение
    ровно той разницы, которая стоила музею данных.

Ни БД, ни сети: сессия — заглушка с no-op commit, `crud.get_exhibit_orm` подменён фейковым
ORM-объектом, `llm.to_spoken_text` — стабом (иначе E15 полез бы в LLM за озвучкой).

Запуск: python -m pytest tests/test_admin_put_no_wipe.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import crud, schemas as sch  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.services import llm  # noqa: E402


# ── Фейковый ORM ────────────────────────────────────────────────────────────────────────────
@dataclass
class FakeHall:
    id: int = 3
    hall_number: Optional[int] = 3
    name: Optional[str] = "Синяя гостиная"


@dataclass
class FakeShowcase:
    id: int = 2
    hall_id: int = 3
    showcase_number: Optional[int] = 2
    name: Optional[str] = None
    hall: Optional[FakeHall] = field(default_factory=FakeHall)


@dataclass
class FakeImage:
    id: int = 80
    url: str = "https://cdn.example/exhibits/renessans/01.jpg"
    alt: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    is_primary: bool = True


def make_exhibit(**overrides) -> "FakeExhibit":
    """Карточка «Ренессанса» до правки — со всем, что музей потерял 31.08."""
    exhibit = FakeExhibit()
    for key, value in overrides.items():
        setattr(exhibit, key, value)
    return exhibit


@dataclass
class FakeExhibit:
    id: int = 7
    showcase_id: Optional[int] = 2
    exhibit_number: Optional[str] = "1"
    label_slug: Optional[str] = "faberge_pasxalnoe_yajczo_shkatulka_renessans"
    name: str = "Пасхальное яйцо-шкатулка «Ренессанс»"
    year_created: Optional[str] = "1894"
    origin_place: Optional[str] = "Санкт-Петербург"  # колонка заведена 31.08.2026 (п. I-2)
    master_name: Optional[str] = "Фирма К. Фаберже, мастер М. Перхин"
    material: Optional[str] = "Золото, агат, рубин"
    techniques: Optional[str] = "литьё, чеканка"
    short_description: Optional[str] = "Последний пасхальный подарок Александра III."
    short_description_spoken: Optional[str] = "Последний пасхальный подарок Александра Третьего."
    raw_history: Optional[str] = "Заказчик — Александр III. Получатель — Мария Фёдоровна."
    image_url: Optional[str] = "https://cdn.example/exhibits/renessans/01.jpg"
    video_url: Optional[str] = None
    model_3d_url: Optional[str] = None
    model_3d_embed: Optional[str] = None
    audio_url: Optional[str] = None
    source_url: Optional[str] = None
    updated_at: Optional[datetime] = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    images: List[FakeImage] = field(default_factory=lambda: [FakeImage()])
    showcase: Optional[FakeShowcase] = field(default_factory=FakeShowcase)


class FakeSession:
    """Ровно то, что зовут replace_exhibit/patch_exhibit: commit без БД."""

    async def commit(self) -> None:
        return None


@pytest.fixture
def client(monkeypatch):
    """TestClient с фейковой карточкой; возвращает (client, exhibit) — состояние проверяем по exhibit."""
    exhibit = make_exhibit()

    async def get_exhibit_orm(session, exhibit_id):
        return exhibit if exhibit_id == exhibit.id else None

    async def to_spoken_text(text):
        return "озвучка"

    async def fake_session():
        yield FakeSession()

    monkeypatch.setattr(crud, "get_exhibit_orm", get_exhibit_orm)
    monkeypatch.setattr(llm, "to_spoken_text", to_spoken_text)
    app.dependency_overrides[get_session] = fake_session
    test_client = TestClient(app, raise_server_exceptions=False)
    test_client.headers.update({"Authorization": f"Bearer {settings.admin_api_token}"})
    try:
        yield test_client, exhibit
    finally:
        app.dependency_overrides.clear()


# ── PUT: сердце задачи ──────────────────────────────────────────────────────────────────────
def test_put_keeps_fields_absent_from_body(client):
    """Регрессия «Ренессанса»: правка техник не должна уносить фото, описание и материалы."""
    test_client, exhibit = client
    response = test_client.put(
        "/admin/exhibits/7",
        json={"showcase_id": 2, "name": exhibit.name, "techniques": "литьё, чеканка, гравировка"},
    )
    assert response.status_code == 200
    assert exhibit.techniques == "литьё, чеканка, гравировка"          # прислали — записалось
    assert exhibit.image_url == "https://cdn.example/exhibits/renessans/01.jpg"
    assert exhibit.material == "Золото, агат, рубин"
    assert exhibit.short_description == "Последний пасхальный подарок Александра III."
    assert exhibit.raw_history.startswith("Заказчик")
    assert exhibit.short_description_spoken == "Последний пасхальный подарок Александра Третьего."
    assert response.json()["image_url"] == exhibit.image_url


def test_put_with_explicit_null_still_clears(client):
    """Очистка поля осталась возможной — но теперь это осознанный явный null."""
    test_client, exhibit = client
    response = test_client.put(
        "/admin/exhibits/7", json={"showcase_id": 2, "name": exhibit.name, "material": None},
    )
    assert response.status_code == 200
    assert exhibit.material is None
    assert exhibit.short_description == "Последний пасхальный подарок Александра III."  # соседи целы


def test_put_full_replace_restores_old_semantics(client):
    """Полная замена никуда не делась — она просто перестала быть значением по умолчанию."""
    test_client, exhibit = client
    response = test_client.put(
        "/admin/exhibits/7?full_replace=true",
        json={"showcase_id": 2, "name": exhibit.name, "techniques": "литьё"},
    )
    assert response.status_code == 200
    assert exhibit.material is None
    assert exhibit.short_description is None
    assert exhibit.raw_history is None
    # Исключение одно, и оно не про семантику PUT: image_url — зеркало галереи, и
    # рассогласовать его с `exhibit_images` не может даже полная замена. Гард смотрит
    # на ТЕЛО запроса (`model_fields_set`), а не на дамп: при full_replace в дампе есть
    # все поля схемы, и по нему «прислали image_url» от «подставил дамп» не отличить.
    assert exhibit.image_url == "https://cdn.example/exhibits/renessans/01.jpg"


def test_full_replace_with_explicit_null_image_url_still_clears(client):
    """…но и при полной замене явно присланный `null` фото снимает: тело важнее дампа."""
    test_client, exhibit = client
    response = test_client.put(
        "/admin/exhibits/7?full_replace=true",
        json={"showcase_id": 2, "name": exhibit.name, "image_url": None},
    )
    assert response.status_code == 200
    assert exhibit.image_url is None


def test_put_restores_image_url_from_gallery(client):
    """`image_url` — зеркало строки галереи: пустым при целой галерее он остаться не может.

    Тело БЕЗ `image_url` — то самое постороннее сохранение, которым карточку и испортили:
    первая же правка возвращает ей фото. Тело С `image_url` — другой случай, он ниже.
    """
    test_client, exhibit = client
    exhibit.image_url = None                              # состояние испорченной карточки
    response = test_client.put(
        "/admin/exhibits/7", json={"showcase_id": 2, "name": exhibit.name},
    )
    assert response.status_code == 200
    assert exhibit.image_url == "https://cdn.example/exhibits/renessans/01.jpg"
    assert response.json()["image_url"] == exhibit.image_url


def test_put_with_explicit_null_image_url_clears_it(client):
    """Явный `null` стирает `image_url` — ровно то, что обещают описание ручки и openapi.

    Лечение из галереи не должно отменять ОСОЗНАННУЮ очистку: иначе контракт «чтобы
    очистить поле, пришлите `null`» врал бы про одно из полей, а поведение зависело бы
    от данных (при двух строках с `is_primary` очистка проходила, при одной — нет).
    """
    test_client, exhibit = client
    assert exhibit.image_url                              # исходно фото есть
    response = test_client.put(
        "/admin/exhibits/7", json={"showcase_id": 2, "name": exhibit.name, "image_url": None},
    )
    assert response.status_code == 200
    assert exhibit.image_url is None
    assert response.json()["image_url"] is None
    assert exhibit.images and exhibit.images[0].is_primary   # галерея цела — снимали только главное


def test_patch_with_explicit_null_image_url_clears_it(client):
    """У PATCH это ещё и НЕ РЕГРЕСС: снять главное фото, оставив снимок в галерее, — рабочая операция.

    Ручка была исправна и в жалобе музея (п. IV-1) не фигурировала вовсе; отобрать у
    админки эту операцию «заодно с починкой PUT» было бы не починкой, а новой поломкой.
    """
    test_client, exhibit = client
    response = test_client.patch("/admin/exhibits/7", json={"image_url": None})
    assert response.status_code == 200
    assert exhibit.image_url is None
    assert response.json()["image_url"] is None


def test_patch_restores_image_url_when_field_is_absent(client):
    """А вот правка соседнего поля пустой `image_url` при целой галерее оставить не может."""
    test_client, exhibit = client
    exhibit.image_url = None
    response = test_client.patch("/admin/exhibits/7", json={"techniques": "гравировка"})
    assert response.status_code == 200
    assert exhibit.image_url == "https://cdn.example/exhibits/renessans/01.jpg"


def test_restore_is_skipped_only_for_the_field_that_came_in_the_body():
    """Гард смотрит на ИМЯ поля в теле, а не на его значение и не на соседей."""
    exhibit = make_exhibit(image_url=None)
    assert crud._restore_primary_image(exhibit, {"image_url"}) is False
    assert exhibit.image_url is None                       # прислали явно — уважаем
    assert crud._restore_primary_image(exhibit, {"techniques", "material"}) is True
    assert exhibit.image_url == "https://cdn.example/exhibits/renessans/01.jpg"


def test_image_url_not_restored_when_gallery_is_ambiguous(client):
    """Двух первичных фото быть не должно; какое лежало в image_url — не угадываем."""
    test_client, exhibit = client
    exhibit.image_url = None
    exhibit.images = [FakeImage(id=1, url="a.jpg"), FakeImage(id=2, url="b.jpg")]
    response = test_client.put("/admin/exhibits/7", json={"showcase_id": 2, "name": exhibit.name})
    assert response.status_code == 200
    assert exhibit.image_url is None


def test_restore_primary_image_is_a_no_op_when_url_is_intact():
    """Живой `image_url` не переписываем галереей: главным могли сделать другое фото вручную."""
    exhibit = make_exhibit(image_url="https://cdn.example/выбрано-вручную.jpg")
    assert crud._restore_primary_image(exhibit) is False
    assert exhibit.image_url == "https://cdn.example/выбрано-вручную.jpg"


def test_restore_primary_image_reports_what_it_did():
    exhibit = make_exhibit(image_url=None)
    assert crud._restore_primary_image(exhibit) is True
    assert crud._restore_primary_image(exhibit) is False       # второй раз восстанавливать нечего


# ── Предупреждение в лог ────────────────────────────────────────────────────────────────────
def test_warn_on_wipe_logs_non_empty_to_empty(caplog):
    """Ровно этой строки не хватило, чтобы заметить потерю месяц назад."""
    exhibit = make_exhibit()
    with caplog.at_level(logging.WARNING, logger="app.crud"):
        wiped = crud._warn_on_wipe(exhibit, {"short_description": None, "material": "   "}, "PUT")
    assert set(wiped) == {"short_description", "material"}
    assert "exhibit_update" in caplog.text and "id=7" in caplog.text
    assert "short_description" in caplog.text


def test_warn_on_wipe_silent_when_nothing_lost(caplog):
    """Пустое → пустое и непустое → другое непустое молчат: лог не должен стать шумом."""
    exhibit = make_exhibit(video_url=None)
    with caplog.at_level(logging.WARNING, logger="app.crud"):
        wiped = crud._warn_on_wipe(
            exhibit, {"video_url": None, "material": "Золото"}, "PATCH",
        )
    assert wiped == []
    assert "exhibit_update" not in caplog.text


def test_put_warns_in_log(client, caplog):
    """Явная очистка тоже пишется в лог — иначе следующая потеря снова пройдёт незамеченной."""
    test_client, exhibit = client
    with caplog.at_level(logging.WARNING, logger="app.crud"):
        test_client.put("/admin/exhibits/7", json={"showcase_id": 2, "name": exhibit.name, "material": None})
    assert "exhibit_update: PUT id=7" in caplog.text
    assert "material" in caplog.text


# ── Остальные пути записи каталога: сторожа ─────────────────────────────────────────────────
def test_patch_exhibit_does_not_wipe(client):
    """PATCH был корректен и таким и остаётся — фиксируем, чтобы не сломать заодно."""
    test_client, exhibit = client
    response = test_client.patch("/admin/exhibits/7", json={"techniques": "гравировка"})
    assert response.status_code == 200
    assert exhibit.techniques == "гравировка"
    assert exhibit.material == "Золото, агат, рубин"
    assert exhibit.short_description == "Последний пасхальный подарок Александра III."
    assert exhibit.image_url == "https://cdn.example/exhibits/renessans/01.jpg"


@dataclass
class FakeHallOrm:
    id: int = 3
    hall_number: Optional[int] = 3
    name: Optional[str] = "Рыцарский зал"
    description: Optional[str] = "Зал доспехов и оружия."
    level: Optional[int] = 2
    cover_image_url: Optional[str] = "https://cdn.example/halls/knight.jpg"
    is_temporary: bool = False
    is_service: bool = False
    sort_order: int = 2


def test_patch_hall_keeps_absent_fields(monkeypatch):
    """У зала обложке восстанавливаться не из чего (галереи-дубля нет) — тем важнее сторож."""
    hall = FakeHallOrm()

    async def get_hall(session, hall_id):
        return sch.HallDetail(id=hall.id, name=hall.name, showcases=[])

    monkeypatch.setattr(crud, "get_hall", get_hall)
    asyncio.run(crud.patch_hall(FakeSession(), hall, sch.HallPatch(name="Рыцарский зал (новый)")))
    assert hall.name == "Рыцарский зал (новый)"
    assert hall.description == "Зал доспехов и оружия."
    assert hall.cover_image_url == "https://cdn.example/halls/knight.jpg"


@dataclass
class FakeShowcaseOrm:
    id: int = 2
    hall_id: int = 3
    showcase_number: Optional[int] = 2
    name: Optional[str] = "Императорские яйца"


def test_patch_showcase_keeps_absent_fields(monkeypatch):
    """Витрину правят только PATCH-ем, и он тоже пишет ровно присланное."""
    showcase = FakeShowcaseOrm()

    async def get_showcase(session, showcase_id):
        return sch.ShowcaseDetail(id=showcase.id, hall_id=showcase.hall_id, exhibits=[])

    monkeypatch.setattr(crud, "get_showcase", get_showcase)
    asyncio.run(crud.patch_showcase(FakeSession(), showcase, sch.ShowcasePatch(showcase_number=5)))
    assert showcase.showcase_number == 5
    assert showcase.name == "Императорские яйца"          # имя не присылали — оно и не тронуто


def test_hall_reorder_writes_no_card_fields():
    """Вторая ручка PUT в проекте — перестановка залов: полей карточки она не пишет вовсе."""
    assert set(sch.HallReorderRequest(hall_ids=[3, 1, 2]).model_dump()) == {"hall_ids"}


# ── Тест-объяснение: где именно терялись данные ─────────────────────────────────────────────
def test_exhibit_update_dump_difference():
    """`model_dump()` отдаёт ВСЕ поля схемы, `exclude_unset` — только присланные два.

    Число полей сверяем со схемой, а не с константой: 31.08.2026 к ним добавилось
    пятнадцатое (`origin_place`, п. I-2), и суть теста — «дамп без exclude_unset
    отдаёт весь набор целиком», а не конкретная цифра.
    """
    payload = sch.ExhibitUpdate(showcase_id=2, name="Яйцо «Ренессанс»")
    assert set(payload.model_dump(exclude_unset=True)) == {"showcase_id", "name"}
    dumped = payload.model_dump()
    assert len(dumped) == len(sch.ExhibitUpdate.model_fields) >= 15
    # Ровно эти ключи и приезжали в setattr как None, стирая содержимое БД.
    for lost in ("image_url", "short_description", "material", "raw_history"):
        assert lost in dumped and dumped[lost] is None


def test_updated_at_only_in_admin_view():
    """Время правки нужно для разбора потерь — но публичный контракт не расширяем."""
    exhibit = make_exhibit()
    assert crud.to_exhibit(exhibit, admin=True).updated_at == exhibit.updated_at
    assert not hasattr(crud.to_exhibit(exhibit), "updated_at")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
