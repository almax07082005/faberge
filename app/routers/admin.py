"""Администрирование [вне MVP]: CRUD экспонатов, медиа, аналитика."""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, Query, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crud
from .. import schemas as sch
from ..config import settings
from ..db import get_session
from ..dependencies import require_admin
from ..services import UpstreamError, llm, storage

# Логин выдаёт токен, поэтому НЕ должен сам требовать токен — отдельный роутер без require_admin.
auth_router = APIRouter(prefix="/admin", tags=["Администрирование"])
router = APIRouter(prefix="/admin", tags=["Администрирование"], dependencies=[Depends(require_admin)])

_ALLOWED_IMG = {"image/jpeg", "image/png", "image/webp"}


@auth_router.post(
    "/login", response_model=sch.LoginResponse,
    summary="Логин администратора (логин/пароль → Bearer-токен)",
    description=(
        "Проверяет логин/пароль (`ADMIN_USERNAME` / `ADMIN_PASSWORD`) и возвращает "
        "Bearer-токен администратора. Полученный `access_token` нужно слать в заголовке "
        "`Authorization: Bearer <access_token>` ко всем `/admin/**`. Токен статический "
        "(не истекает) — соответствует MVP с единственным администратором."
    ),
)
async def login(data: sch.LoginRequest) -> sch.LoginResponse:
    if data.username != settings.admin_username or data.password != settings.admin_password:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль.")
    return sch.LoginResponse(access_token=settings.admin_api_token, token_type="bearer")


async def _read_validated_image(file: UploadFile) -> bytes:
    """Прочитать загруженный файл с проверкой типа и размера (общая для фото и обложки)."""
    if file.content_type not in _ALLOWED_IMG:
        raise HTTPException(status_code=415, detail="Поддерживаются только JPEG, PNG и WebP.")
    data = await file.read()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Размер файла превышает {settings.max_upload_mb} МБ.")
    return data


async def _autofill_spoken(data: sch.BaseModel) -> None:
    """E15: заполнить `short_description_spoken` из `short_description` через LLM.

    Правила:
      • если админ передал `short_description_spoken` явно — уважаем как ручное
        переопределение и не трогаем;
      • пересчитываем только когда в запросе меняется `short_description`;
      • если LLM недоступен/не настроен — `to_spoken_text` вернёт None, и озвучка
        уедет по фолбэку (исходное описание + детерминированная нормализация).
    """
    fields = data.model_fields_set
    if "short_description_spoken" in fields or "short_description" not in fields:
        return
    spoken = await llm.to_spoken_text(getattr(data, "short_description", None))
    data.short_description_spoken = spoken
    data.model_fields_set.add("short_description_spoken")


@router.post("/halls", response_model=sch.HallDetail, status_code=201, summary="[Вне MVP] Создать зал")
async def create_hall(data: sch.HallCreate, session: AsyncSession = Depends(get_session)) -> sch.HallDetail:
    try:
        return await crud.create_hall(session, data)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Зал с таким номером уже существует.")


@router.patch("/halls/{hall_id}", response_model=sch.HallDetail, summary="[Вне MVP] Частично обновить зал")
async def patch_hall(
    data: sch.HallPatch, hall_id: int = Path(ge=1), session: AsyncSession = Depends(get_session)
) -> sch.HallDetail:
    hall = await crud.get_hall_orm(session, hall_id)
    if hall is None:
        raise HTTPException(status_code=404, detail="Зал не найден.")
    try:
        return await crud.patch_hall(session, hall, data)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Зал с таким номером уже существует.")


@router.put(
    "/halls/reorder", response_model=sch.HallListResponse,
    summary="Изменить порядок залов (drag-n-drop)",
    description=(
        "Переставляет залы (C11). В теле — `hall_ids` в желаемом порядке. Залы "
        "переставляются в рамках своих текущих позиций (можно слать как весь "
        "список, так и подсписок одной группы — «Основная экспозиция» или "
        "«Временная выставка»). Возвращает залы в новом порядке. Порядок также "
        "можно менять точечно через `PATCH /admin/halls/{id}` (поле `sort_order`)."
    ),
)
async def reorder_halls(
    data: sch.HallReorderRequest, session: AsyncSession = Depends(get_session)
) -> sch.HallListResponse:
    try:
        await crud.reorder_halls(session, data.hall_ids)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return await crud.list_halls(session, limit=1000, offset=0)


@router.post(
    "/halls/{hall_id}/cover", response_model=sch.HallDetail, status_code=201,
    summary="Загрузить обложку зала",
    description=(
        "Загрузка обложки зала (`multipart/form-data`, поле `file`). Лимиты: "
        f"размер ≤ {settings.max_upload_mb} МБ, форматы JPEG / PNG / WebP. "
        "URL объекта записывается в `cover_image_url` зала; отдельная миниатюра не генерируется."
    ),
)
async def upload_hall_cover(
    hall_id: int = Path(ge=1),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> sch.HallDetail:
    hall = await crud.get_hall_orm(session, hall_id)
    if hall is None:
        raise HTTPException(status_code=404, detail="Зал не найден.")
    data = await _read_validated_image(file)
    old_cover = hall.cover_image_url
    try:
        stored = await storage.save_image(data, file.filename or "cover", file.content_type, prefix="halls")
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=exc.message)
    result = await crud.set_hall_cover(session, hall, stored.url)
    # Старую обложку убираем из хранилища (best-effort), если её заменили на новую.
    if old_cover and old_cover != stored.url:
        await storage.delete_many([old_cover])
    return result


@router.delete(
    "/halls/{hall_id}", status_code=204, summary="[Вне MVP] Удалить зал",
    description=(
        "Удаляет зал. По умолчанию, если в зале есть витрины, возвращает `409` — "
        "сначала опустошите зал или передайте `?force=true`. При `force=true` "
        "каскадно удаляются витрины → экспонаты → их фото (включая объекты в "
        "Object Storage) и обложка зала."
    ),
)
async def delete_hall(
    hall_id: int = Path(ge=1),
    force: bool = Query(False, description="Каскадно удалить витрины, экспонаты и их медиа."),
    session: AsyncSession = Depends(get_session),
) -> None:
    hall = await crud.get_hall_orm(session, hall_id)
    if hall is None:
        raise HTTPException(status_code=404, detail="Зал не найден.")
    showcase_count = await crud.count_hall_showcases(session, hall_id)
    if showcase_count > 0 and not force:
        raise HTTPException(
            status_code=409,
            detail=f"Зал не пуст: витрин — {showcase_count}. Передайте ?force=true для каскадного удаления.",
        )
    # URL медиа собираем ДО удаления строк (после каскада их уже не достать из БД).
    image_urls = await crud.collect_hall_image_urls(session, hall_id)
    await crud.delete_hall(session, hall_id)
    # Чистим объекты хранилища после успешного удаления из БД (best-effort).
    await storage.delete_many(image_urls)


@router.post("/showcases", response_model=sch.ShowcaseDetail, status_code=201, summary="[Вне MVP] Создать витрину")
async def create_showcase(data: sch.ShowcaseCreate, session: AsyncSession = Depends(get_session)) -> sch.ShowcaseDetail:
    if not await crud.hall_exists(session, data.hall_id):
        raise HTTPException(status_code=404, detail="Зал не найден.")
    try:
        return await crud.create_showcase(session, data)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Витрина с таким номером уже существует в этом зале.")


@router.patch(
    "/showcases/{showcase_id}", response_model=sch.ShowcaseDetail,
    summary="[Вне MVP] Частично обновить витрину",
    description=(
        "Частичное обновление витрины (E14): любой поднабор `{hall_id, showcase_number, "
        "name}`. Перенос в другой зал (`hall_id`) и смена `showcase_number` учитывают "
        "уникальность `(hall_id, showcase_number)` — при конфликте `409`."
    ),
)
async def patch_showcase(
    data: sch.ShowcasePatch, showcase_id: int = Path(ge=1), session: AsyncSession = Depends(get_session)
) -> sch.ShowcaseDetail:
    sc = await crud.get_showcase_orm(session, showcase_id)
    if sc is None:
        raise HTTPException(status_code=404, detail="Витрина не найдена.")
    if data.hall_id is not None and not await crud.hall_exists(session, data.hall_id):
        raise HTTPException(status_code=404, detail="Зал не найден.")
    try:
        return await crud.patch_showcase(session, sc, data)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Витрина с таким номером уже существует в этом зале.")


@router.delete(
    "/showcases/{showcase_id}", status_code=204, summary="[Вне MVP] Удалить витрину",
    description=(
        "Удаляет витрину. По умолчанию, если в витрине есть экспонаты, возвращает "
        "`409` — сначала опустошите витрину или передайте `?force=true`. При "
        "`force=true` каскадно удаляются экспонаты и их фото (включая объекты в "
        "Object Storage)."
    ),
)
async def delete_showcase(
    showcase_id: int = Path(ge=1),
    force: bool = Query(False, description="Каскадно удалить экспонаты и их медиа."),
    session: AsyncSession = Depends(get_session),
) -> None:
    sc = await crud.get_showcase_orm(session, showcase_id)
    if sc is None:
        raise HTTPException(status_code=404, detail="Витрина не найдена.")
    exhibit_count = await crud.count_showcase_exhibits(session, showcase_id)
    if exhibit_count > 0 and not force:
        raise HTTPException(
            status_code=409,
            detail=f"Витрина не пуста: экспонатов — {exhibit_count}. Передайте ?force=true для каскадного удаления.",
        )
    image_urls = await crud.collect_showcase_image_urls(session, showcase_id)
    await crud.delete_showcase(session, showcase_id)
    await storage.delete_many(image_urls)


@router.post("/exhibits", response_model=sch.ExhibitAdmin, status_code=201, summary="[Вне MVP] Создать экспонат")
async def create_exhibit(data: sch.ExhibitCreate, session: AsyncSession = Depends(get_session)) -> sch.ExhibitAdmin:
    if not await crud.showcase_exists(session, data.showcase_id):
        raise HTTPException(status_code=404, detail="Витрина не найдена.")
    await _autofill_spoken(data)
    try:
        ex = await crud.create_exhibit(session, data)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Экспонат с таким label_slug уже существует.")
    return crud.to_exhibit(ex, admin=True)


@router.get(
    "/exhibits/{exhibit_id}", response_model=sch.ExhibitAdmin,
    summary="[Вне MVP] Карточка экспоната для админки",
    description=(
        "Полная карточка экспоната, включая внутреннее поле `raw_history` "
        "(факты для LLM), которое не отдаётся публичным `GET /exhibits/{id}`. "
        "Нужна админке для просмотра/редактирования полного описания."
    ),
)
async def get_exhibit_admin(
    exhibit_id: int = Path(ge=1), session: AsyncSession = Depends(get_session)
) -> sch.ExhibitAdmin:
    ex = await crud.get_exhibit_orm(session, exhibit_id)
    if ex is None:
        raise HTTPException(status_code=404, detail="Экспонат не найден.")
    return crud.to_exhibit(ex, admin=True)


@router.put("/exhibits/{exhibit_id}", response_model=sch.ExhibitAdmin, summary="[Вне MVP] Полностью обновить экспонат")
async def update_exhibit(
    data: sch.ExhibitUpdate, exhibit_id: int = Path(ge=1), session: AsyncSession = Depends(get_session)
) -> sch.ExhibitAdmin:
    ex = await crud.get_exhibit_orm(session, exhibit_id)
    if ex is None:
        raise HTTPException(status_code=404, detail="Экспонат не найден.")
    await _autofill_spoken(data)
    try:
        ex = await crud.replace_exhibit(session, ex, data)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Нарушение уникальности label_slug.")
    return crud.to_exhibit(ex, admin=True)


@router.patch("/exhibits/{exhibit_id}", response_model=sch.ExhibitAdmin, summary="[Вне MVP] Частично обновить экспонат")
async def patch_exhibit(
    data: sch.ExhibitPatch, exhibit_id: int = Path(ge=1), session: AsyncSession = Depends(get_session)
) -> sch.ExhibitAdmin:
    ex = await crud.get_exhibit_orm(session, exhibit_id)
    if ex is None:
        raise HTTPException(status_code=404, detail="Экспонат не найден.")
    await _autofill_spoken(data)
    try:
        ex = await crud.patch_exhibit(session, ex, data)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Нарушение уникальности label_slug.")
    return crud.to_exhibit(ex, admin=True)


@router.delete("/exhibits/{exhibit_id}", status_code=204, summary="[Вне MVP] Удалить экспонат")
async def delete_exhibit(exhibit_id: int = Path(ge=1), session: AsyncSession = Depends(get_session)) -> None:
    ex = await crud.get_exhibit_orm(session, exhibit_id)
    if ex is None:
        raise HTTPException(status_code=404, detail="Экспонат не найден.")
    image_urls = crud.collect_image_urls(ex)
    await crud.delete_exhibit(session, ex)
    # Чистим объекты из хранилища после успешного удаления из БД (best-effort).
    await storage.delete_many(image_urls)


@router.post(
    "/exhibits/{exhibit_id}/spoken/regenerate", response_model=sch.ExhibitAdmin,
    summary="Перегенерировать озвучку описания (числа прописью)",
    description=(
        "Заново генерирует `short_description_spoken` из текущего `short_description` "
        "через LLM (E15): римские/арабские числа → слова в нужном падеже "
        "(«Александр III» → «Александр Третий»). Используется, если админ правил "
        "описание в обход авто-генерации или хочет пересобрать озвучку. "
        "Требует настроенного LLM (иначе `503`)."
    ),
)
async def regenerate_spoken(
    exhibit_id: int = Path(ge=1), session: AsyncSession = Depends(get_session)
) -> sch.ExhibitAdmin:
    ex = await crud.get_exhibit_orm(session, exhibit_id)
    if ex is None:
        raise HTTPException(status_code=404, detail="Экспонат не найден.")
    if not settings.llm_configured:
        raise HTTPException(status_code=503, detail="LLM не настроен — озвучку прописью сгенерировать нельзя.")
    spoken = await llm.to_spoken_text(ex.short_description)
    ex = await crud.patch_exhibit(session, ex, sch.ExhibitPatch(short_description_spoken=spoken))
    return crud.to_exhibit(ex, admin=True)


@router.get(
    "/exhibits/{exhibit_id}/media", response_model=list[sch.Image],
    summary="Список фото экспоната (галерея)",
    description="Возвращает галерею экспоната с `id` и `is_primary` для каждого фото (для удаления / выбора главной).",
)
async def list_media(
    exhibit_id: int = Path(ge=1), session: AsyncSession = Depends(get_session)
) -> list[sch.Image]:
    if not await _exhibit_exists(session, exhibit_id):
        raise HTTPException(status_code=404, detail="Экспонат не найден.")
    images = await crud.list_exhibit_images(session, exhibit_id)
    return [sch.Image.model_validate(i) for i in images]


@router.post(
    "/exhibits/{exhibit_id}/media", response_model=sch.MediaUploadResponse, status_code=201,
    summary="Загрузить фото экспоната",
    description=(
        "Загрузка фото экспоната (`multipart/form-data`: поле `file`, опционально `is_primary`). "
        f"Лимиты: размер ≤ {settings.max_upload_mb} МБ, форматы JPEG / PNG / WebP. "
        "Возвращает `image_id` (для последующего `DELETE .../media/{image_id}`). "
        "`thumbnail_url` сейчас совпадает с `image_url` — отдельная миниатюра не генерируется. "
        "При `is_primary=true` фото становится главным (`exhibits.image_url`)."
    ),
)
async def upload_media(
    exhibit_id: int = Path(ge=1),
    file: UploadFile = File(...),
    is_primary: bool = Form(False),
    session: AsyncSession = Depends(get_session),
) -> sch.MediaUploadResponse:
    if not await _exhibit_exists(session, exhibit_id):
        raise HTTPException(status_code=404, detail="Экспонат не найден.")
    data = await _read_validated_image(file)
    try:
        stored = await storage.save_image(data, file.filename or "image", file.content_type)
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=exc.message)
    img = await crud.add_exhibit_image(session, exhibit_id, stored.url, is_primary)
    return sch.MediaUploadResponse(
        image_id=img.id, image_url=stored.url, thumbnail_url=stored.thumbnail_url, object_key=stored.object_key
    )


@router.delete(
    "/exhibits/{exhibit_id}/media/{image_id}", status_code=204,
    summary="Удалить фото экспоната",
)
async def delete_media(
    exhibit_id: int = Path(ge=1),
    image_id: int = Path(ge=1),
    session: AsyncSession = Depends(get_session),
) -> None:
    img = await crud.get_exhibit_image(session, exhibit_id, image_id)
    if img is None:
        raise HTTPException(status_code=404, detail="Изображение не найдено.")
    url = img.url
    await crud.delete_exhibit_image(session, img)
    await storage.delete_many([url])


@router.get("/analytics/overview", response_model=sch.AnalyticsOverview, summary="[Вне MVP] Сводная аналитика")
async def analytics_overview(
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = Query(None),
    session: AsyncSession = Depends(get_session),
) -> sch.AnalyticsOverview:
    return await crud.analytics_overview(session, from_, to)


@router.get(
    "/analytics/questions", response_model=sch.AnalyticsQuestions,
    summary="Аналитика: частые и редкие вопросы",
    description=(
        "Агрегирует реплики посетителей (`guide_messages`, role=user): топ частых и "
        "редких формулировок (нормализация trim+lower), всего/уникальных вопросов. "
        "Опциональные `from`/`to` (даты, `to` не включительно), `limit` — размер топов."
    ),
)
async def analytics_questions(
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> sch.AnalyticsQuestions:
    return await crud.analytics_questions(session, from_, to, limit)


@router.get(
    "/analytics/engagement", response_model=sch.AnalyticsEngagement,
    summary="Аналитика: длительность сессии (первое открытие → последнее действие)",
    description=(
        "Время от первого события сессии до последнего (по `events`): среднее, "
        "медиана, максимум, среднее число событий на сессию и распределение по "
        "корзинам длительности. Опциональные `from`/`to`."
    ),
)
async def analytics_engagement(
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = Query(None),
    session: AsyncSession = Depends(get_session),
) -> sch.AnalyticsEngagement:
    return await crud.analytics_engagement(session, from_, to)


@router.get(
    "/analytics/routes", response_model=sch.AnalyticsRoutes,
    summary="Аналитика: маршрут пользователя по залам",
    description=(
        "Строит маршруты по залам из событий `hall_view`: посещения залов, точки "
        "входа, переходы A→B и частые полные пути; среднее число залов на сессию. "
        "Опциональные `from`/`to`, `limit` — размер топов."
    ),
)
async def analytics_routes(
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
) -> sch.AnalyticsRoutes:
    return await crud.analytics_routes(session, from_, to, limit)


async def _exhibit_exists(session: AsyncSession, exhibit_id: int) -> bool:
    return (await crud.get_exhibit_orm(session, exhibit_id)) is not None
