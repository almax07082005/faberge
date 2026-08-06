"""Запросы к БД и сериализация ORM → Pydantic."""
from __future__ import annotations

import statistics
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy import (
    String,
    and_,
    cast,
    delete as sa_delete,
    func,
    nullslast,
    or_,
    select,
    text,
    update as sa_update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from . import models as m
from . import schemas as sch
from .config import settings
from .services import question_cluster, visits


# ── Сериализаторы ────────────────────────────────────────────────────────────
def to_hall(h: m.Hall, showcase_count: Optional[int] = None, exhibit_count: Optional[int] = None) -> sch.Hall:
    return sch.Hall(
        id=h.id,
        hall_number=h.hall_number,
        name=h.name,
        description=h.description,
        level=h.level,
        cover_image_url=h.cover_image_url,
        is_temporary=h.is_temporary,
        is_service=h.is_service,
        sort_order=h.sort_order,
        showcase_count=showcase_count,
        exhibit_count=exhibit_count,
    )


def to_hall_brief(h: m.Hall) -> sch.HallBrief:
    return sch.HallBrief(id=h.id, hall_number=h.hall_number, name=h.name)


def to_showcase(s: m.Showcase, exhibit_count: Optional[int] = None) -> sch.Showcase:
    return sch.Showcase(
        id=s.id, hall_id=s.hall_id, showcase_number=s.showcase_number, name=s.name, exhibit_count=exhibit_count
    )


def to_exhibit_summary(e: m.Exhibit) -> sch.ExhibitSummary:
    hall = e.showcase.hall if e.showcase else None
    return sch.ExhibitSummary(
        id=e.id,
        exhibit_number=e.exhibit_number,
        label_slug=e.label_slug,
        name=e.name,
        year_created=e.year_created,
        master_name=e.master_name,
        thumbnail_url=e.image_url,
        hall_id=hall.id if hall else None,
        showcase_id=e.showcase_id,
        showcase_number=e.showcase.showcase_number if e.showcase else None,
        is_temporary=hall.is_temporary if hall else None,
    )


def to_exhibit(e: m.Exhibit, admin: bool = False) -> sch.Exhibit:
    hall = to_hall_brief(e.showcase.hall) if e.showcase and e.showcase.hall else None
    showcase = sch.ShowcaseBrief(id=e.showcase.id, showcase_number=e.showcase.showcase_number) if e.showcase else None
    images = [
        sch.Image(id=i.id, url=i.url, alt=i.alt, width=i.width, height=i.height, is_primary=i.is_primary)
        for i in e.images
    ]
    cls = sch.ExhibitAdmin if admin else sch.Exhibit
    data = dict(
        id=e.id,
        exhibit_number=e.exhibit_number,
        label_slug=e.label_slug,
        name=e.name,
        year_created=e.year_created,
        master_name=e.master_name,
        material=e.material,
        short_description=e.short_description,
        image_url=e.image_url,
        images=images,
        video_url=e.video_url,
        model_3d_url=e.model_3d_url,
        model_3d_embed=e.model_3d_embed,
        audio_url=e.audio_url,
        source_url=e.source_url,
        hall=hall,
        showcase=showcase,
    )
    if admin:
        data["raw_history"] = e.raw_history
        data["short_description_spoken"] = e.short_description_spoken
    return cls(**data)


def exhibit_to_dict(e: m.Exhibit) -> Dict:
    return {
        "id": e.id,
        "exhibit_number": e.exhibit_number,
        "label_slug": e.label_slug,
        "name": e.name,
        "year_created": e.year_created,
        "master_name": e.master_name,
        "material": e.material,
        "short_description": e.short_description,
        "raw_history": e.raw_history,
    }


# ── Сериализаторы для ответов ИИ-гида (B6/B7/B10) ─────────────────────────────
def to_referenced_exhibit(e: m.Exhibit) -> sch.ReferencedExhibit:
    """Плашка экспоната для ответа гида (B6): id, название, миниатюра, где лежит."""
    hall = e.showcase.hall if e.showcase else None
    return sch.ReferencedExhibit(
        id=e.id,
        name=e.name,
        exhibit_number=e.exhibit_number,
        thumbnail_url=e.image_url,
        hall_number=hall.hall_number if hall else None,
        showcase_number=e.showcase.showcase_number if e.showcase else None,
    )


def to_referenced_hall(h: m.Hall) -> sch.ReferencedHall:
    return sch.ReferencedHall(id=h.id, hall_number=h.hall_number, name=h.name)


def to_location(e: m.Exhibit) -> Optional[sch.GuideLocation]:
    """Навигация «зал + витрина» (B7). None, если экспонат не привязан к витрине."""
    if e.showcase is None:
        return None
    hall = e.showcase.hall
    return sch.GuideLocation(
        hall_number=hall.hall_number if hall else None,
        hall_name=hall.name if hall else None,
        showcase_number=e.showcase.showcase_number,
    )


# ── Загрузчики с нужными relationship ────────────────────────────────────────
_EXHIBIT_FULL = (
    selectinload(m.Exhibit.showcase).selectinload(m.Showcase.hall),
    selectinload(m.Exhibit.images),
)
_EXHIBIT_SUMMARY = (selectinload(m.Exhibit.showcase).selectinload(m.Showcase.hall),)


# ── Счётчики ─────────────────────────────────────────────────────────────────
async def _hall_counts(session: AsyncSession, hall_ids: Sequence[int]) -> Tuple[Dict[int, int], Dict[int, int]]:
    if not hall_ids:
        return {}, {}
    sc_rows = await session.execute(
        select(m.Showcase.hall_id, func.count(m.Showcase.id)).where(m.Showcase.hall_id.in_(hall_ids)).group_by(m.Showcase.hall_id)
    )
    showcase_counts = {hid: cnt for hid, cnt in sc_rows.all()}
    ex_rows = await session.execute(
        select(m.Showcase.hall_id, func.count(m.Exhibit.id))
        .join(m.Exhibit, m.Exhibit.showcase_id == m.Showcase.id)
        .where(m.Showcase.hall_id.in_(hall_ids))
        .group_by(m.Showcase.hall_id)
    )
    exhibit_counts = {hid: cnt for hid, cnt in ex_rows.all()}
    return showcase_counts, exhibit_counts


async def _showcase_exhibit_counts(session: AsyncSession, showcase_ids: Sequence[int]) -> Dict[int, int]:
    if not showcase_ids:
        return {}
    rows = await session.execute(
        select(m.Exhibit.showcase_id, func.count(m.Exhibit.id))
        .where(m.Exhibit.showcase_id.in_(showcase_ids))
        .group_by(m.Exhibit.showcase_id)
    )
    return {sid: cnt for sid, cnt in rows.all()}


# ── Карта / навигация ────────────────────────────────────────────────────────
# Порядок залов: sort_order, затем номер. Залы без номера («Вне постоянной
# экспозиции») идут последними — nullslast. Служебные записи (Парадная лестница)
# в публичную выдачу не попадают: источник правды один, скрывать их на клиенте
# нельзя (баг-репорт 28.07.2026, п.5).
_HALL_ORDER = (m.Hall.sort_order, nullslast(m.Hall.hall_number.asc()))
# Витрины: по номеру, а группа «не в витринах» (showcase_number IS NULL) — последней.
_SHOWCASE_ORDER = nullslast(m.Showcase.showcase_number.asc())


def _hall_visibility(stmt, is_temporary: Optional[bool], include_service: bool):
    if is_temporary is not None:
        stmt = stmt.where(m.Hall.is_temporary == is_temporary)
    if not include_service:
        stmt = stmt.where(m.Hall.is_service.is_(False))
    return stmt


async def get_map(
    session: AsyncSession, is_temporary: Optional[bool] = None, include_service: bool = False
) -> sch.MapResponse:
    stmt = select(m.Hall).options(selectinload(m.Hall.showcases)).order_by(*_HALL_ORDER)
    stmt = _hall_visibility(stmt, is_temporary, include_service)
    halls = (await session.execute(stmt)).scalars().all()
    hall_ids = [h.id for h in halls]
    showcase_counts, exhibit_counts = await _hall_counts(session, hall_ids)
    all_showcase_ids = [s.id for h in halls for s in h.showcases]
    sc_ex_counts = await _showcase_exhibit_counts(session, all_showcase_ids)

    map_halls: List[sch.MapHall] = []
    for h in halls:
        showcases = [
            sch.Showcase(
                id=s.id, hall_id=s.hall_id, showcase_number=s.showcase_number, name=s.name,
                exhibit_count=sc_ex_counts.get(s.id, 0),
            )
            for s in h.showcases
        ]
        map_halls.append(
            sch.MapHall(
                id=h.id, hall_number=h.hall_number, name=h.name, description=h.description, level=h.level,
                cover_image_url=h.cover_image_url, is_temporary=h.is_temporary, is_service=h.is_service,
                sort_order=h.sort_order, showcase_count=showcase_counts.get(h.id, 0),
                exhibit_count=exhibit_counts.get(h.id, 0), showcases=showcases,
            )
        )
    return sch.MapResponse(halls=map_halls)


async def list_halls(
    session: AsyncSession, limit: int, offset: int, is_temporary: Optional[bool] = None,
    include_service: bool = False,
) -> sch.HallListResponse:
    count_stmt = _hall_visibility(select(func.count(m.Hall.id)), is_temporary, include_service)
    list_stmt = _hall_visibility(
        select(m.Hall).order_by(*_HALL_ORDER), is_temporary, include_service
    )
    total = (await session.execute(count_stmt)).scalar_one()
    halls = (await session.execute(list_stmt.limit(limit).offset(offset))).scalars().all()
    showcase_counts, exhibit_counts = await _hall_counts(session, [h.id for h in halls])
    items = [to_hall(h, showcase_counts.get(h.id, 0), exhibit_counts.get(h.id, 0)) for h in halls]
    return sch.HallListResponse(items=items, total=total, limit=limit, offset=offset)


async def get_hall(session: AsyncSession, hall_id: int) -> Optional[sch.HallDetail]:
    hall = (
        await session.execute(select(m.Hall).options(selectinload(m.Hall.showcases)).where(m.Hall.id == hall_id))
    ).scalar_one_or_none()
    if hall is None:
        return None
    showcase_counts, exhibit_counts = await _hall_counts(session, [hall.id])
    sc_ex_counts = await _showcase_exhibit_counts(session, [s.id for s in hall.showcases])
    showcases = [to_showcase(s, sc_ex_counts.get(s.id, 0)) for s in hall.showcases]
    return sch.HallDetail(
        id=hall.id, hall_number=hall.hall_number, name=hall.name, description=hall.description, level=hall.level,
        cover_image_url=hall.cover_image_url, is_temporary=hall.is_temporary, is_service=hall.is_service,
        sort_order=hall.sort_order, showcase_count=showcase_counts.get(hall.id, 0),
        exhibit_count=exhibit_counts.get(hall.id, 0), showcases=showcases,
    )


async def hall_exists(session: AsyncSession, hall_id: int) -> bool:
    return (await session.execute(select(m.Hall.id).where(m.Hall.id == hall_id))).scalar_one_or_none() is not None


async def list_hall_showcases(session: AsyncSession, hall_id: int, limit: int, offset: int) -> sch.ShowcaseListResponse:
    total = (await session.execute(select(func.count(m.Showcase.id)).where(m.Showcase.hall_id == hall_id))).scalar_one()
    showcases = (
        (
            await session.execute(
                select(m.Showcase).where(m.Showcase.hall_id == hall_id).order_by(_SHOWCASE_ORDER).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    counts = await _showcase_exhibit_counts(session, [s.id for s in showcases])
    items = [to_showcase(s, counts.get(s.id, 0)) for s in showcases]
    return sch.ShowcaseListResponse(items=items, total=total, limit=limit, offset=offset)


async def list_showcases(
    session: AsyncSession, limit: int, offset: int, hall_id: Optional[int] = None
) -> sch.ShowcaseListResponse:
    flt = (m.Showcase.hall_id == hall_id) if hall_id is not None else None
    count_stmt = select(func.count(m.Showcase.id))
    list_stmt = select(m.Showcase).order_by(m.Showcase.hall_id, _SHOWCASE_ORDER)
    if flt is not None:
        count_stmt = count_stmt.where(flt)
        list_stmt = list_stmt.where(flt)
    total = (await session.execute(count_stmt)).scalar_one()
    showcases = (await session.execute(list_stmt.limit(limit).offset(offset))).scalars().all()
    counts = await _showcase_exhibit_counts(session, [s.id for s in showcases])
    items = [to_showcase(s, counts.get(s.id, 0)) for s in showcases]
    return sch.ShowcaseListResponse(items=items, total=total, limit=limit, offset=offset)


async def get_showcase(session: AsyncSession, showcase_id: int) -> Optional[sch.ShowcaseDetail]:
    s = (
        await session.execute(
            select(m.Showcase)
            .options(selectinload(m.Showcase.hall), selectinload(m.Showcase.exhibits).selectinload(m.Exhibit.showcase))
            .where(m.Showcase.id == showcase_id)
        )
    ).scalar_one_or_none()
    if s is None:
        return None
    counts = await _showcase_exhibit_counts(session, [s.id])
    exhibits = [to_exhibit_summary(e) for e in s.exhibits]
    return sch.ShowcaseDetail(
        id=s.id, hall_id=s.hall_id, showcase_number=s.showcase_number, name=s.name,
        exhibit_count=counts.get(s.id, 0), hall=to_hall_brief(s.hall) if s.hall else None, exhibits=exhibits,
    )


async def showcase_exists(session: AsyncSession, showcase_id: int) -> bool:
    return (await session.execute(select(m.Showcase.id).where(m.Showcase.id == showcase_id))).scalar_one_or_none() is not None


# ── Экспонаты ────────────────────────────────────────────────────────────────
async def _exhibits_page(session: AsyncSession, base_filter, limit: int, offset: int) -> sch.ExhibitListResponse:
    total = (await session.execute(select(func.count()).select_from(select(m.Exhibit.id).where(base_filter).subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                select(m.Exhibit).options(*_EXHIBIT_SUMMARY).where(base_filter).order_by(m.Exhibit.id).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return sch.ExhibitListResponse(items=[to_exhibit_summary(e) for e in rows], total=total, limit=limit, offset=offset)


async def list_hall_exhibits(session: AsyncSession, hall_id: int, limit: int, offset: int) -> sch.ExhibitListResponse:
    flt = m.Exhibit.showcase_id.in_(select(m.Showcase.id).where(m.Showcase.hall_id == hall_id))
    return await _exhibits_page(session, flt, limit, offset)


async def list_showcase_exhibits(session: AsyncSession, showcase_id: int, limit: int, offset: int) -> sch.ExhibitListResponse:
    return await _exhibits_page(session, m.Exhibit.showcase_id == showcase_id, limit, offset)


async def list_exhibits(
    session: AsyncSession, limit: int, offset: int, hall_id: Optional[int], showcase_id: Optional[int],
    label_slug: Optional[str], q: Optional[str], is_temporary: Optional[bool] = None,
) -> sch.ExhibitListResponse:
    conds = []
    if hall_id is not None:
        conds.append(m.Exhibit.showcase_id.in_(select(m.Showcase.id).where(m.Showcase.hall_id == hall_id)))
    if showcase_id is not None:
        conds.append(m.Exhibit.showcase_id == showcase_id)
    if label_slug:
        conds.append(m.Exhibit.label_slug == label_slug)
    if q:
        conds.append(m.Exhibit.name.ilike(f"%{q}%"))
    if is_temporary is not None:
        # Временность наследуется от зала: экспонат «временный», если его витрина
        # принадлежит залу временной выставки (showcase → hall.is_temporary).
        conds.append(
            m.Exhibit.showcase_id.in_(
                select(m.Showcase.id)
                .join(m.Hall, m.Showcase.hall_id == m.Hall.id)
                .where(m.Hall.is_temporary == is_temporary)
            )
        )
    flt = and_(*conds) if conds else (m.Exhibit.id == m.Exhibit.id)
    return await _exhibits_page(session, flt, limit, offset)


async def get_exhibit_orm(session: AsyncSession, exhibit_id: int) -> Optional[m.Exhibit]:
    return (
        await session.execute(select(m.Exhibit).options(*_EXHIBIT_FULL).where(m.Exhibit.id == exhibit_id))
    ).scalar_one_or_none()


async def get_exhibit_by_slug_orm(session: AsyncSession, label_slug: str) -> Optional[m.Exhibit]:
    return (
        await session.execute(select(m.Exhibit).options(*_EXHIBIT_FULL).where(m.Exhibit.label_slug == label_slug))
    ).scalar_one_or_none()


async def list_related(session: AsyncSession, exhibit_id: int, limit: int) -> Optional[sch.ExhibitListResponse]:
    hall_id = (
        await session.execute(
            select(m.Showcase.hall_id).join(m.Exhibit, m.Exhibit.showcase_id == m.Showcase.id).where(m.Exhibit.id == exhibit_id)
        )
    ).scalar_one_or_none()
    if hall_id is None:
        # экспонат не найден или не привязан к витрине/залу
        exists = (await session.execute(select(m.Exhibit.id).where(m.Exhibit.id == exhibit_id))).scalar_one_or_none()
        if exists is None:
            return None
        return sch.ExhibitListResponse(items=[], total=0, limit=limit, offset=0)
    flt = and_(
        m.Exhibit.id != exhibit_id,
        m.Exhibit.showcase_id.in_(select(m.Showcase.id).where(m.Showcase.hall_id == hall_id)),
    )
    return await _exhibits_page(session, flt, limit, 0)


async def all_label_slugs(session: AsyncSession) -> List[str]:
    rows = await session.execute(select(m.Exhibit.label_slug).where(m.Exhibit.label_slug.isnot(None)).order_by(m.Exhibit.id))
    return [r[0] for r in rows.all()]


async def names_by_slugs(session: AsyncSession, slugs: Sequence[str]) -> Dict[str, str]:
    if not slugs:
        return {}
    rows = await session.execute(select(m.Exhibit.label_slug, m.Exhibit.name).where(m.Exhibit.label_slug.in_(list(slugs))))
    return {slug: name for slug, name in rows.all()}


async def slug_by_name(session: AsyncSession) -> Dict[str, str]:
    """Карта ``name → label_slug`` для сшивки с внешним ML-поиском по фото: сервис
    распознавания ключует предметы по названию (title), а наш каталог — по
    label_slug. Имена не уникальны (напр. «Портсигар» ×12) — берём экспонат с
    наименьшим id детерминированно (первый по order_by id)."""
    rows = await session.execute(
        select(m.Exhibit.name, m.Exhibit.label_slug)
        .where(m.Exhibit.label_slug.isnot(None))
        .order_by(m.Exhibit.id)
    )
    mapping: Dict[str, str] = {}
    for name, slug in rows.all():
        if name and name not in mapping:
            mapping[name] = slug
    return mapping


# ── Поиск и retrieval (B1/B8/C27) ─────────────────────────────────────────────
# Слой поиска по каталогу, доступный и из GET /search, и из ИИ-гида (retrieval).
# Каждый запрос — одно условие ``search_vector @@ q OR <ILIKE>``:
#   • FTS по взвешенному tsvector даёт ранжирование (ts_rank) и морфологию русского;
#   • ILIKE-подстрока добирает частичные совпадения (напр. «коронац» без полного
#     слова), которые FTS без префиксного поиска не находит.
# Требует применённой миграции (колонки search_vector / exhibit_number). Это не
# «мягкая» зависимость: exhibit_number/video_url — обычные колонки, которые ORM
# селектит в КАЖДОМ запросе к экспонатам, поэтому без миграции падает весь модуль
# экспонатов, а не только поиск (мигрируем ДО деплоя — см. db/migrations).
def _exhibit_ilike(q: str):
    like = f"%{q}%"
    return or_(
        m.Exhibit.name.ilike(like),
        m.Exhibit.master_name.ilike(like),
        m.Exhibit.short_description.ilike(like),
        m.Exhibit.raw_history.ilike(like),
        m.Exhibit.exhibit_number.ilike(like),
    )


def _hall_ilike(q: str):
    like = f"%{q}%"
    return or_(m.Hall.name.ilike(like), m.Hall.description.ilike(like))


def _ru_tsquery(q: str):
    """OR-tsquery из свободного текста запроса.

    ``plainto_tsquery`` соединяет слова через AND, поэтому целая фраза («расскажи
    про коронационное яйцо») почти никогда не матчится. Для retrieval нужна
    OR-семантика: совпало любое значимое слово, а ``ts_rank`` поднимает документы
    с бОльшим числом совпадений. Превращаем '&' в '|' на уровне текста tsquery
    (безопасно: plainto_tsquery не создаёт фраз/операторов, только '&').
    Один объект переиспользуется в WHERE и ORDER BY одного запроса.
    """
    return text("replace(plainto_tsquery('russian', :ftsq)::text, ' & ', ' | ')::tsquery").bindparams(ftsq=q)


async def search_exhibits_orm(session: AsyncSession, q: str, limit: int) -> List[m.Exhibit]:
    """ORM-экспонаты по запросу: FTS-ранжирование + ILIKE-подстрока. Загружает зал/витрину."""
    tsq = _ru_tsquery(q)
    stmt = (
        select(m.Exhibit)
        .options(*_EXHIBIT_SUMMARY)
        .where(or_(m.Exhibit.search_vector.op("@@")(tsq), _exhibit_ilike(q)))
        .order_by(func.ts_rank(m.Exhibit.search_vector, tsq).desc(), m.Exhibit.id)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def search_halls_orm(session: AsyncSession, q: str, limit: int) -> List[m.Hall]:
    tsq = _ru_tsquery(q)
    stmt = (
        select(m.Hall)
        .where(or_(m.Hall.search_vector.op("@@")(tsq), _hall_ilike(q)))
        .order_by(func.ts_rank(m.Hall.search_vector, tsq).desc(), nullslast(m.Hall.hall_number.asc()))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def search(session: AsyncSession, q: str, limit: int) -> sch.SearchResponse:
    halls = await search_halls_orm(session, q, limit)
    exhibits = await search_exhibits_orm(session, q, limit)
    hall_items = [to_hall(h) for h in halls]
    exhibit_items = [to_exhibit_summary(e) for e in exhibits]
    return sch.SearchResponse(query=q, halls=hall_items, exhibits=exhibit_items, total=len(hall_items) + len(exhibit_items))


# ── Retrieval для ИИ-гида (B1) ────────────────────────────────────────────────
# Единый слой доступа гида к каталогу: поиск экспонатов/залов и точечные выборки
# (по номеру — B9, весь список залов — B10). Возвращает ORM-объекты, которые роутер
# гида сериализует в referenced_exhibits / referenced_halls / location.
async def exhibits_by_number(session: AsyncSession, number: str) -> List[m.Exhibit]:
    """Экспонаты с данным номером по путеводителю (B9). Номер не уникален — их может
    быть несколько (тогда гид уточняет зал/витрину)."""
    num = number.strip()
    stmt = (
        select(m.Exhibit)
        .options(*_EXHIBIT_SUMMARY)
        .where(func.lower(m.Exhibit.exhibit_number) == num.lower())
        .order_by(m.Exhibit.showcase_id, m.Exhibit.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def all_halls_ordered(session: AsyncSession, include_service: bool = False) -> List[m.Hall]:
    """Залы в порядке каталога (B10): для ответа «какие залы есть».

    Служебные записи (Парадная лестница) по умолчанию исключены — гид не должен
    называть их посетителю, а «В музее N залов» обязан сойтись с GET /halls.
    """
    stmt = _hall_visibility(select(m.Hall).order_by(*_HALL_ORDER), None, include_service)
    return list((await session.execute(stmt)).scalars().all())


async def candidates_by_slugs(session: AsyncSession, slugs: Sequence[str]) -> Dict[str, Tuple[int, Optional[str]]]:
    """slug → (exhibit_id, thumbnail_url) для карточек кандидатов распознавания (B5)."""
    if not slugs:
        return {}
    rows = await session.execute(
        select(m.Exhibit.label_slug, m.Exhibit.id, m.Exhibit.image_url).where(m.Exhibit.label_slug.in_(list(slugs)))
    )
    return {slug: (ex_id, image_url) for slug, ex_id, image_url in rows.all()}


# ── Администрирование (CRUD) ─────────────────────────────────────────────────
async def create_hall(session: AsyncSession, data: sch.HallCreate) -> sch.HallDetail:
    if data.sort_order is not None:
        sort_order = data.sort_order
    elif data.hall_number is not None:
        # По умолчанию порядок = номер зала (новый зал встаёт в конец естественного
        # порядка); админ переставит через reorder / PATCH sort_order.
        sort_order = data.hall_number
    else:
        # Зал без номера — в самый конец каталога.
        sort_order = ((await session.execute(select(func.max(m.Hall.sort_order)))).scalar() or 0) + 1
    hall = m.Hall(
        hall_number=data.hall_number, name=data.name, description=data.description,
        level=data.level, is_temporary=data.is_temporary, is_service=data.is_service,
        sort_order=sort_order,
    )
    session.add(hall)
    await session.commit()
    result = await get_hall(session, hall.id)
    assert result is not None
    return result


def _reorder_slots(current: Dict[int, int], hall_ids: List[int]) -> Dict[int, int]:
    """Раздать «слоты» (отсортированные текущие sort_order) залам в новом порядке.

    Чистая функция — вынесена для тестируемости. ``current`` — {hall_id: sort_order}
    только для переставляемых залов; ``hall_ids`` — желаемый порядок. Возвращает
    {hall_id: new_sort_order}.
    """
    slots = sorted(current.values())
    return {hid: slot for hid, slot in zip(hall_ids, slots)}


async def reorder_halls(session: AsyncSession, hall_ids: List[int]) -> None:
    """Переставить залы (C11), сохраняя их текущие позиции (slot-preserving).

    Берём переданные залы, их нынешние ``sort_order`` сортируем по возрастанию —
    это «слоты». Раздаём слоты залам в порядке ``hall_ids``. Так перестановка
    подсписка (напр. только основной экспозиции) не задевает позиции залов вне
    запроса. Бросает ``ValueError`` при дубликатах или неизвестных id.
    """
    if len(set(hall_ids)) != len(hall_ids):
        raise ValueError("Список hall_ids содержит дубликаты.")
    rows = (
        await session.execute(select(m.Hall.id, m.Hall.sort_order).where(m.Hall.id.in_(hall_ids)))
    ).all()
    current = {hid: so for hid, so in rows}
    missing = [hid for hid in hall_ids if hid not in current]
    if missing:
        raise ValueError(f"Залы не найдены: {missing}")
    for hid, new_order in _reorder_slots(current, hall_ids).items():
        if current[hid] != new_order:
            await session.execute(
                sa_update(m.Hall).where(m.Hall.id == hid).values(sort_order=new_order)
            )
    await session.commit()


async def get_hall_orm(session: AsyncSession, hall_id: int) -> Optional[m.Hall]:
    return (await session.execute(select(m.Hall).where(m.Hall.id == hall_id))).scalar_one_or_none()


async def patch_hall(session: AsyncSession, hall: m.Hall, data: sch.HallPatch) -> sch.HallDetail:
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(hall, field, value)
    await session.commit()
    result = await get_hall(session, hall.id)
    assert result is not None
    return result


async def set_hall_cover(session: AsyncSession, hall: m.Hall, cover_image_url: str) -> sch.HallDetail:
    hall.cover_image_url = cover_image_url
    await session.commit()
    result = await get_hall(session, hall.id)
    assert result is not None
    return result


async def create_showcase(session: AsyncSession, data: sch.ShowcaseCreate) -> sch.ShowcaseDetail:
    sc = m.Showcase(hall_id=data.hall_id, showcase_number=data.showcase_number, name=data.name)
    session.add(sc)
    await session.commit()
    result = await get_showcase(session, sc.id)
    assert result is not None
    return result


async def get_showcase_orm(session: AsyncSession, showcase_id: int) -> Optional[m.Showcase]:
    return (await session.execute(select(m.Showcase).where(m.Showcase.id == showcase_id))).scalar_one_or_none()


async def patch_showcase(session: AsyncSession, sc: m.Showcase, data: sch.ShowcasePatch) -> sch.ShowcaseDetail:
    """Частичное обновление витрины (B2). Уникальность (hall_id, showcase_number)
    проверяет БД — IntegrityError ловит роутер и отдаёт 409."""
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(sc, field, value)
    await session.commit()
    result = await get_showcase(session, sc.id)
    assert result is not None
    return result


# ── Удаление залов / витрин (каскад + очистка медиа) ─────────────────────────
async def count_hall_showcases(session: AsyncSession, hall_id: int) -> int:
    return (await session.execute(select(func.count(m.Showcase.id)).where(m.Showcase.hall_id == hall_id))).scalar_one()


async def count_showcase_exhibits(session: AsyncSession, showcase_id: int) -> int:
    return (await session.execute(select(func.count(m.Exhibit.id)).where(m.Exhibit.showcase_id == showcase_id))).scalar_one()


async def _exhibit_image_urls(session: AsyncSession, exhibit_ids_stmt, extra: Sequence[Optional[str]] = ()) -> List[str]:
    """URL всех медиа (первичное фото + галерея + озвучка) для набора экспонатов (+ доп. URL, напр. обложка зала).

    ``exhibit_ids_stmt`` — SELECT id экспонатов (подзапрос). Порядок URL не важен —
    ``storage.delete_many`` сам дедуплицирует и пропускает пустые/внешние ссылки
    (напр. ``model_3d_url`` на Koinovo — внешний, его не трогаем).
    """
    urls: List[str] = [u for u in extra if u]
    # image_url + audio_url (предсинтезированная озвучка живёт в нашем бакете, см. tts.synthesize).
    primary = await session.execute(select(m.Exhibit.image_url, m.Exhibit.audio_url).where(m.Exhibit.id.in_(exhibit_ids_stmt)))
    for image_url, audio_url in primary.all():
        if image_url:
            urls.append(image_url)
        if audio_url:
            urls.append(audio_url)
    gallery = await session.execute(select(m.ExhibitImage.url).where(m.ExhibitImage.exhibit_id.in_(exhibit_ids_stmt)))
    urls += [r[0] for r in gallery.all()]
    return urls


async def collect_hall_image_urls(session: AsyncSession, hall_id: int) -> List[str]:
    """Обложка зала + все изображения экспонатов во всех витринах зала — для очистки хранилища."""
    cover = (await session.execute(select(m.Hall.cover_image_url).where(m.Hall.id == hall_id))).scalar_one_or_none()
    ex_ids = select(m.Exhibit.id).join(m.Showcase, m.Exhibit.showcase_id == m.Showcase.id).where(m.Showcase.hall_id == hall_id)
    return await _exhibit_image_urls(session, ex_ids, extra=[cover])


async def collect_showcase_image_urls(session: AsyncSession, showcase_id: int) -> List[str]:
    """Все изображения экспонатов витрины — для очистки хранилища."""
    ex_ids = select(m.Exhibit.id).where(m.Exhibit.showcase_id == showcase_id)
    return await _exhibit_image_urls(session, ex_ids)


async def delete_hall(session: AsyncSession, hall_id: int) -> None:
    # Core-DELETE: полагаемся на FK ON DELETE CASCADE (витрины → экспонаты → фото),
    # чтобы не тянуть весь граф в ORM и не ловить async lazy-load.
    await session.execute(sa_delete(m.Hall).where(m.Hall.id == hall_id))
    await session.commit()


async def delete_showcase(session: AsyncSession, showcase_id: int) -> None:
    await session.execute(sa_delete(m.Showcase).where(m.Showcase.id == showcase_id))
    await session.commit()


async def create_exhibit(session: AsyncSession, data: sch.ExhibitCreate) -> m.Exhibit:
    ex = m.Exhibit(**data.model_dump())
    session.add(ex)
    await session.commit()
    return await get_exhibit_orm(session, ex.id)  # type: ignore[return-value]


async def replace_exhibit(session: AsyncSession, ex: m.Exhibit, data: sch.ExhibitUpdate) -> m.Exhibit:
    for field, value in data.model_dump().items():
        setattr(ex, field, value)
    await session.commit()
    return await get_exhibit_orm(session, ex.id)  # type: ignore[return-value]


async def patch_exhibit(session: AsyncSession, ex: m.Exhibit, data: sch.ExhibitPatch) -> m.Exhibit:
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(ex, field, value)
    await session.commit()
    return await get_exhibit_orm(session, ex.id)  # type: ignore[return-value]


async def delete_exhibit(session: AsyncSession, ex: m.Exhibit) -> None:
    await session.delete(ex)
    await session.commit()


def collect_image_urls(ex: m.Exhibit) -> List[str]:
    """Все URL медиа экспоната (первичное фото + галерея + озвучка) — для очистки хранилища.

    Требует, чтобы коллекция ``ex.images`` была уже загружена (см. ``_EXHIBIT_FULL``).
    ``model_3d_url`` не включаем — это внешняя ссылка на Koinovo, не наш объект.
    """
    urls = [img.url for img in ex.images]
    if ex.image_url:
        urls.append(ex.image_url)
    if ex.audio_url:
        urls.append(ex.audio_url)
    return urls


async def get_exhibit_image(session: AsyncSession, exhibit_id: int, image_id: int) -> Optional[m.ExhibitImage]:
    return (
        await session.execute(
            select(m.ExhibitImage).where(
                m.ExhibitImage.id == image_id, m.ExhibitImage.exhibit_id == exhibit_id
            )
        )
    ).scalar_one_or_none()


async def delete_exhibit_image(session: AsyncSession, img: m.ExhibitImage) -> None:
    # Если удаляем первичное изображение — снимаем ссылку с exhibits.image_url.
    if img.is_primary:
        ex = await session.get(m.Exhibit, img.exhibit_id)
        if ex is not None and ex.image_url == img.url:
            ex.image_url = None
    await session.delete(img)
    await session.commit()


async def list_exhibit_images(session: AsyncSession, exhibit_id: int) -> List[m.ExhibitImage]:
    rows = await session.execute(
        select(m.ExhibitImage).where(m.ExhibitImage.exhibit_id == exhibit_id).order_by(m.ExhibitImage.position, m.ExhibitImage.id)
    )
    return list(rows.scalars().all())


async def add_exhibit_image(session: AsyncSession, exhibit_id: int, url: str, is_primary: bool) -> m.ExhibitImage:
    if is_primary:
        ex = await session.get(m.Exhibit, exhibit_id)
        if ex is not None:
            ex.image_url = url
    pos = (await session.execute(select(func.coalesce(func.max(m.ExhibitImage.position), -1)).where(m.ExhibitImage.exhibit_id == exhibit_id))).scalar_one() + 1
    img = m.ExhibitImage(exhibit_id=exhibit_id, url=url, is_primary=is_primary, position=pos)
    session.add(img)
    await session.commit()
    await session.refresh(img)
    return img


# ── Телеметрия ───────────────────────────────────────────────────────────────
async def insert_events(session: AsyncSession, batch: sch.EventBatch) -> Tuple[int, int]:
    """Записать батч событий. Возвращает (принято, отброшено).

    Событие с типом вне словаря (§1) отбрасывается поштучно: фронт шлёт пачкой,
    и одна опечатка не должна стоить девяти корректных событий. `props`
    фильтруется по белому списку контракта (§10), `audio_play` нормализуется в
    канонический `tts_play`.
    """
    objs: List[m.Event] = []
    rejected = 0
    for raw in batch.events:
        ev = sch.normalize_event(raw)
        if ev is None:
            rejected += 1
            continue
        fields = dict(
            session_id=batch.session_id,
            type=ev.type,
            exhibit_id=ev.exhibit_id,
            hall_id=ev.hall_id,
            showcase_id=ev.showcase_id,
            label_slug=ev.label_slug,
            device_id=ev.device_id or batch.device_id,
            props=ev.props,
        )
        # ts проставляет фронт в момент действия; если не прислали — ставит БД
        # (server_default). Явный None записал бы NULL в NOT NULL-колонку.
        if ev.ts is not None:
            fields["ts"] = ev.ts
        objs.append(m.Event(**fields))
    if objs:
        session.add_all(objs)
        await session.commit()
    return len(objs), rejected


# ── Аналитика: общее ─────────────────────────────────────────────────────────
# Граница `to` ВКЛЮЧАЮЩАЯ: «с 1 по 31 июля» в интерфейсе означает, что события
# 31 июля попадают в выборку. Раньше фильтр был `col < to`, и последний день
# периода молча терялся (§2).
def _range_conds(col, dfrom: Optional[date], dto: Optional[date]) -> List:
    conds = []
    if dfrom is not None:
        conds.append(col >= dfrom)
    if dto is not None:
        conds.append(col < dto + timedelta(days=1))
    return conds


def _range_filter(col, dfrom: Optional[date], dto: Optional[date]):
    conds = _range_conds(col, dfrom, dto)
    return and_(*conds) if conds else None


def _iso(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value else None


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


# Тип события «прослушать»: канонический `tts_play` плюс исторический `audio_play`,
# который фронт слал до 03.08.2026 (нормализуется на приёме, но в накопленных
# данных остаётся как есть).
_TTS_TYPES = ("tts_play", "audio_play")


def _prop_bool(props: Optional[dict], key: str) -> bool:
    """Значение `props[key]` как bool: фронт может прислать и true, и \"true\"."""
    if not props:
        return False
    value = props.get(key)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def _prop_float(props: Optional[dict], key: str) -> Optional[float]:
    if not props:
        return None
    try:
        return float(props[key])
    except (KeyError, TypeError, ValueError):
        return None


async def _top_items(
    session: AsyncSession, id_col, ev_type: str, entity, ev_range, limit: int = 5
) -> List[sch.AnalyticsTopItem]:
    """Топ сущностей по событиям типа `ev_type` (имя берётся джойном, не запросом на строку)."""
    stmt = (
        select(id_col, entity.name, func.count().label("c"))
        .select_from(m.Event)
        .join(entity, entity.id == id_col)
        .where(m.Event.type == ev_type, id_col.isnot(None))
    )
    if ev_range is not None:
        stmt = stmt.where(ev_range)
    stmt = stmt.group_by(id_col, entity.name).order_by(func.count().desc(), id_col).limit(limit)
    rows = (await session.execute(stmt)).all()
    return [sch.AnalyticsTopItem(id=ent_id, name=name, count=count) for ent_id, name, count in rows]


async def _hall_names(session: AsyncSession, hall_ids: Set[int]) -> Dict[int, Optional[str]]:
    if not hall_ids:
        return {}
    rows = await session.execute(select(m.Hall.id, m.Hall.name).where(m.Hall.id.in_(list(hall_ids))))
    return {hid: name for hid, name in rows.all()}


async def _exhibit_names(session: AsyncSession, exhibit_ids: Set[int]) -> Dict[int, Optional[str]]:
    if not exhibit_ids:
        return {}
    rows = await session.execute(
        select(m.Exhibit.id, m.Exhibit.name).where(m.Exhibit.id.in_(list(exhibit_ids)))
    )
    return {eid: name for eid, name in rows.all()}


# ── Аналитика: сводка ────────────────────────────────────────────────────────
async def analytics_overview(
    session: AsyncSession, dfrom: Optional[date], dto: Optional[date], limit: int = 5
) -> sch.AnalyticsOverview:
    ev_range = _range_filter(m.Event.ts, dfrom, dto)

    async def _count(flt):
        stmt = select(func.count()).select_from(m.Event)
        if ev_range is not None:
            stmt = stmt.where(ev_range)
        if flt is not None:
            stmt = stmt.where(flt)
        return (await session.execute(stmt)).scalar_one()

    total_sessions_stmt = select(func.count(func.distinct(m.Event.session_id))).select_from(m.Event)
    if ev_range is not None:
        total_sessions_stmt = total_sessions_stmt.where(ev_range)
    total_sessions = (await session.execute(total_sessions_stmt)).scalar_one()

    total_app_opens = await _count(m.Event.type == "app_open")
    total_recognitions = await _count(m.Event.type == "recognition")
    success = await _count(
        and_(m.Event.type == "recognition", cast(m.Event.props["recognized"].astext, String) == "true")
    )
    total_audio_plays = await _count(m.Event.type.in_(_TTS_TYPES))

    msg_stmt = select(func.count()).select_from(m.GuideMessage).where(m.GuideMessage.role == "user")
    for cond in _range_conds(m.GuideMessage.created_at, dfrom, dto):
        msg_stmt = msg_stmt.where(cond)
    total_chat_messages = (await session.execute(msg_stmt)).scalar_one()

    top_exhibits = await _top_items(session, m.Event.exhibit_id, "exhibit_view", m.Exhibit, ev_range, limit)
    top_halls = await _top_items(session, m.Event.hall_id, "hall_view", m.Hall, ev_range, limit)

    return sch.AnalyticsOverview(
        from_=_iso(dfrom),
        to=_iso(dto),
        total_sessions=total_sessions,
        total_app_opens=total_app_opens,
        total_recognitions=total_recognitions,
        recognition_success_rate=_rate(success, total_recognitions),
        total_chat_messages=total_chat_messages,
        total_audio_plays=total_audio_plays,
        top_exhibits=top_exhibits,
        top_halls=top_halls,
    )


# ── Аналитика: частые/редкие вопросы (C16 + §3) ──────────────────────────────
# Сколько различных формулировок тянуть в кластеризатор. Кластеризация — O(n·k)
# по формулировкам, поэтому берём самые частые. Если формулировок больше, срез
# виден по `unique_questions` (полное число) против `total_clusters`.
_QUESTION_FETCH_LIMIT = 5000


async def analytics_questions(
    session: AsyncSession, dfrom: Optional[date], dto: Optional[date], limit: int = 20
) -> sch.AnalyticsQuestions:
    """Топ частых и редких вопросов посетителей, сгруппированных ПО СМЫСЛУ.

    Источник — реплики пользователя (`guide_messages.role='user'`). Группировка
    по точному тексту (как было до §3) на живых данных вырождается: «Сколько
    стоит яйцо?» и «какая цена яйца» — два вопроса с count=1. Смысловые группы
    строит app/services/question_cluster.py.
    """
    trimmed = func.trim(m.GuideMessage.content)
    conds = [m.GuideMessage.role == "user", func.length(trimmed) > 0]
    conds += _range_conds(m.GuideMessage.created_at, dfrom, dto)
    where = and_(*conds)

    grouped = select(trimmed.label("q"), func.count().label("c")).where(where).group_by(trimmed)
    rows = (
        await session.execute(grouped.order_by(func.count().desc(), trimmed).limit(_QUESTION_FETCH_LIMIT))
    ).all()

    total = (await session.execute(select(func.count()).select_from(m.GuideMessage).where(where))).scalar_one()
    unique = (await session.execute(select(func.count()).select_from(grouped.subquery()))).scalar_one()

    clusters = question_cluster.cluster_questions([(q, c) for q, c in rows])
    frequent = clusters[:limit]
    frequent_ids = {id(c) for c in frequent}
    # «Редкие» — кластеры, встретившиеся не чаще порога, и НЕ попавшие в частые.
    # Раньше rare был тем же запросом с ORDER BY ... ASC и при малом объёме
    # данных дублировал frequent слово в слово.
    rare = [c for c in clusters if c.count <= settings.analytics_rare_max_count and id(c) not in frequent_ids]
    rare.sort(key=lambda c: (c.count, c.question))

    def _item(cluster: question_cluster.QuestionCluster) -> sch.AnalyticsQuestionItem:
        return sch.AnalyticsQuestionItem(
            question=cluster.question, count=cluster.count, variants=cluster.variants
        )

    return sch.AnalyticsQuestions(
        from_=_iso(dfrom),
        to=_iso(dto),
        total_questions=total,
        unique_questions=unique,
        total_clusters=len(clusters),
        frequent=[_item(c) for c in frequent],
        rare=[_item(c) for c in rare[:limit]],
    )


# ── Аналитика: вопросы без ответа гида (§4) ──────────────────────────────────
async def analytics_unanswered(
    session: AsyncSession, dfrom: Optional[date], dto: Optional[date], limit: int = 20
) -> sch.AnalyticsUnanswered:
    """Вопросы, на которые гид не смог ответить — подсказка, чего не хватает в описаниях.

    Признак `answered` проставляется в момент генерации ответа (app/routers/guide.py);
    у сообщений, накопленных до миграции 2026-08-03, он NULL — такие в расчёт
    доли не входят и считаются отдельно (`unclassified`).
    """
    trimmed = func.trim(m.GuideMessage.content)
    base = [m.GuideMessage.role == "user"] + _range_conds(m.GuideMessage.created_at, dfrom, dto)

    async def _count(extra) -> int:
        stmt = select(func.count()).select_from(m.GuideMessage).where(and_(*base, extra))
        return (await session.execute(stmt)).scalar_one()

    total_unanswered = await _count(m.GuideMessage.answered.is_(False))
    total_answered = await _count(m.GuideMessage.answered.is_(True))
    unclassified = await _count(m.GuideMessage.answered.is_(None))

    rows = (
        await session.execute(
            select(
                trimmed.label("q"),
                m.GuideMessage.fail_reason,
                m.GuideMessage.exhibit_id,
                func.count().label("c"),
            )
            .where(and_(*base, m.GuideMessage.answered.is_(False), func.length(trimmed) > 0))
            .group_by(trimmed, m.GuideMessage.fail_reason, m.GuideMessage.exhibit_id)
            .order_by(func.count().desc())
            .limit(_QUESTION_FETCH_LIMIT)
        )
    ).all()

    # Кластеризатор группирует формулировки, а привязки (причина отказа, экспонат)
    # едут рядом как «полезная нагрузка» — их смысл ему знать не нужно.
    texts: List[Tuple[str, int]] = []
    payloads: List[List[Tuple[str, object]]] = []
    reason_totals: Counter = Counter()
    for question, reason, exhibit_id, count in rows:
        texts.append((question, count))
        tokens: List[Tuple[str, object]] = []
        if reason:
            tokens += [("reason", reason)] * count
            reason_totals[reason] += count
        if exhibit_id is not None:
            tokens += [("exhibit", exhibit_id)] * count
        payloads.append(tokens)

    clusters = question_cluster.cluster_questions(texts, payloads)[:limit]

    exhibit_ids = {
        value for cluster in clusters for kind, value in cluster.payload if kind == "exhibit"
    }
    names = await _exhibit_names(session, exhibit_ids)

    items: List[sch.AnalyticsUnansweredItem] = []
    for cluster in clusters:
        reasons = {value: n for (kind, value), n in cluster.payload.items() if kind == "reason"}
        exhibits = Counter({value: n for (kind, value), n in cluster.payload.items() if kind == "exhibit"})
        items.append(
            sch.AnalyticsUnansweredItem(
                question=cluster.question,
                count=cluster.count,
                variants=cluster.variants,
                fail_reasons=reasons,
                exhibits=[
                    sch.AnalyticsTopItem(id=eid, name=names.get(eid), count=n)
                    for eid, n in exhibits.most_common(5)
                ],
            )
        )

    classified = total_unanswered + total_answered
    return sch.AnalyticsUnanswered(
        from_=_iso(dfrom),
        to=_iso(dto),
        total_unanswered=total_unanswered,
        total_answered=total_answered,
        unanswered_rate=_rate(total_unanswered, classified),
        unclassified=unclassified,
        fail_reasons=dict(reason_totals),
        items=items,
    )


# ── Аналитика: вовлечённость и метрики визита (C17 + §5/§6) ──────────────────
_DURATION_BUCKETS = [("0–1 мин", 0, 60), ("1–5 мин", 60, 300), ("5–15 мин", 300, 900), ("15+ мин", 900, None)]


async def _visit_rows(session: AsyncSession, dfrom: Optional[date], dto: Optional[date]) -> List:
    """События периода, упорядоченные по (session_id, ts) — вход для разбиения на визиты.

    Один индексный проход по диапазону `ts` (idx_events_ts) на весь отчёт.
    Выборка целиком поднимается в память: разбиение на визиты, маршруты и разбор
    поведения после неудачного распознавания — последовательные задачи, которые
    в GROUP BY не выражаются. На накопленных за сезон данных отчёт считается не
    при каждом открытии дашборда, а ночным джобом (§12), эндпоинт читает кэш.
    """
    conds = [m.Event.session_id.isnot(None)] + _range_conds(m.Event.ts, dfrom, dto)
    return list(
        (
            await session.execute(
                select(
                    m.Event.session_id,
                    m.Event.ts,
                    m.Event.type,
                    m.Event.exhibit_id,
                    m.Event.hall_id,
                    m.Event.props,
                )
                .where(and_(*conds))
                .order_by(m.Event.session_id, m.Event.ts)
            )
        ).all()
    )


async def analytics_engagement(
    session: AsyncSession, dfrom: Optional[date], dto: Optional[date]
) -> sch.AnalyticsEngagement:
    """Вовлечённость: длительность ВИЗИТА и что посетитель успел за него сделать.

    Длительность считается по визитам, а не по сессии целиком: поток событий
    режется по неактивности дольше SESSION_TIMEOUT_MINUTES (§5), поэтому
    вкладка, открытая утром и вернувшаяся к жизни через четыре часа, даёт два
    визита, а не один на четыре часа.

    Конверсии считаются от базы, которую возвращает `visits.conversion_stats`:
    числитель и знаменатель берутся из одной и той же выборки визитов (п.7
    баг-репорта 06.08.2026), поэтому доля не может оказаться больше 100%.
    """
    rows = await _visit_rows(session, dfrom, dto)

    durations: List[float] = []
    event_counts: List[int] = []
    exhibit_counts: List[int] = []
    question_counts: List[int] = []
    sessions: Set = set()
    # Признаки визитов для конверсий — считаются в одном проходе, а сама база
    # (все визиты или только с `app_open`) выбирается уже после цикла.
    flags: List[visits.VisitFlags] = []

    for session_id, events in visits.visits_by_session(rows):
        sessions.add(session_id)
        durations.append((events[-1].ts - events[0].ts).total_seconds())
        event_counts.append(len(events))
        exhibit_counts.append(
            len({e.exhibit_id for e in events if e.type == "exhibit_view" and e.exhibit_id is not None})
        )
        questions = sum(1 for e in events if e.type == "chat_message")
        question_counts.append(questions)
        types = {e.type for e in events}
        flags.append(
            visits.VisitFlags(
                has_app_open="app_open" in types,
                has_chat_open="chat_open" in types,
                has_question=bool(questions),
            )
        )

    total_visits = len(durations)
    # База конверсий: визиты с `app_open`, если он в периоде вообще приходил
    # (фронт шлёт с 04.08.2026), иначе — все визиты. Числитель считается ВНУТРИ
    # базы, поэтому доли до и после 04.08.2026 сравнивать между собой нельзя:
    # у них разный знаменатель. Что именно взято за базу и сколько там визитов,
    # ручка теперь отдаёт явно (`conversion_basis`, `conversion_denominator`) —
    # чтобы вопрос «не завышена ли конверсия» проверялся по числам, а не на слово.
    conversions = visits.conversion_stats(flags)

    buckets = [
        sch.AnalyticsDurationBucket(
            label=label, count=sum(1 for d in durations if d >= lo and (hi is None or d < hi))
        )
        for label, lo, hi in _DURATION_BUCKETS
    ]
    return sch.AnalyticsEngagement(
        from_=_iso(dfrom),
        to=_iso(dto),
        total_sessions=len(sessions),
        total_visits=total_visits,
        avg_duration_sec=round(statistics.fmean(durations), 1) if durations else 0.0,
        median_duration_sec=round(statistics.median(durations), 1) if durations else 0.0,
        max_duration_sec=round(max(durations), 1) if durations else 0.0,
        avg_events_per_session=round(statistics.fmean(event_counts), 2) if event_counts else 0.0,
        avg_exhibits_per_session=round(statistics.fmean(exhibit_counts), 2) if exhibit_counts else 0.0,
        avg_questions_per_session=round(statistics.fmean(question_counts), 2) if question_counts else 0.0,
        # «Всего» — по всем визитам периода: на этих полях уже завязан дашборд,
        # их смысл не меняем. Для доли рядом едут числители, согласованные с базой.
        sessions_with_chat=conversions.visits_with_chat,
        sessions_with_questions=conversions.visits_with_questions,
        sessions_with_app_open=conversions.visits_with_app_open,
        conversion_basis=conversions.basis,
        conversion_denominator=conversions.denominator,
        chat_conversion_numerator=conversions.chat_numerator,
        question_conversion_numerator=conversions.question_numerator,
        chat_conversion_rate=conversions.chat_rate,
        question_conversion_rate=conversions.question_rate,
        buckets=buckets,
    )


# ── Аналитика: маршрут, точки выхода, повторные визиты (C18 + §7) ────────────
_SESSIONS_PER_DEVICE_BUCKETS = [("1 визит", 1, 1), ("2 визита", 2, 2), ("3+ визита", 3, None)]


async def _device_stats(session: AsyncSession, dfrom: Optional[date], dto: Optional[date]) -> Tuple[int, int, float, List[sch.AnalyticsSessionsPerDeviceBucket]]:
    """Повторные визиты по анонимному device_id.

    Сессии без `device_id` (данные до 03.08.2026, приватный режим браузера)
    расчёт не ломают — каждая считается отдельным «одиночным» устройством.
    """
    conds = [m.Event.session_id.isnot(None)] + _range_conds(m.Event.ts, dfrom, dto)
    rows = (
        await session.execute(
            select(m.Event.device_id, func.count(func.distinct(m.Event.session_id)))
            .where(and_(*conds, m.Event.device_id.isnot(None)))
            .group_by(m.Event.device_id)
        )
    ).all()
    counts = [n for _device, n in rows]

    anonymous = (
        await session.execute(
            select(func.count(func.distinct(m.Event.session_id))).where(
                and_(*conds, m.Event.device_id.is_(None))
            )
        )
    ).scalar_one()
    counts += [1] * anonymous

    total_devices = len(counts)
    returning = sum(1 for n in counts if n >= 2)
    avg = round(statistics.fmean(counts), 2) if counts else 0.0
    hist = [
        sch.AnalyticsSessionsPerDeviceBucket(
            label=label, devices=sum(1 for n in counts if n >= lo and (hi is None or n <= hi))
        )
        for label, lo, hi in _SESSIONS_PER_DEVICE_BUCKETS
    ]
    return total_devices, returning, avg, hist


async def analytics_routes(
    session: AsyncSession, dfrom: Optional[date], dto: Optional[date], limit: int = 10
) -> sch.AnalyticsRoutes:
    """Маршрут по залам: посещения, входы, переходы A→B, пути, точки выхода, повторные визиты.

    Последовательность залов строится из `hall_view` внутри одного визита (§5),
    подряд идущие повторы одного зала схлопываются — переоткрытие карточек в том
    же зале переходом не считается.
    """
    rows = await _visit_rows(session, dfrom, dto)

    visits_count = 0
    visit_lengths: List[int] = []
    hall_visits: Counter = Counter()
    entries: Counter = Counter()
    exits: Counter = Counter()
    transitions: Counter = Counter()
    paths: Counter = Counter()
    exit_screens: Counter = Counter()

    for _session_id, events in visits.visits_by_session(rows):
        visits_count += 1
        # Экран выхода — тип последнего СОДЕРЖАТЕЛЬНОГО события: `session_end`
        # сам по себе ничего не говорит о том, откуда посетитель ушёл.
        meaningful = [e for e in events if e.type != "session_end"]
        exit_screens[(meaningful or events)[-1].type] += 1

        sequence: List[int] = []
        for event in events:
            if event.type != "hall_view" or event.hall_id is None:
                continue
            if not sequence or sequence[-1] != event.hall_id:
                sequence.append(event.hall_id)
        if not sequence:
            continue
        visit_lengths.append(len(sequence))
        entries[sequence[0]] += 1
        exits[sequence[-1]] += 1
        hall_visits.update(sequence)
        transitions.update(zip(sequence, sequence[1:]))
        paths[tuple(sequence)] += 1

    all_ids: Set[int] = set(hall_visits) | set(exits)
    for a, b in transitions:
        all_ids.update((a, b))
    names = await _hall_names(session, all_ids)
    total_devices, returning, avg_per_device, hist = await _device_stats(session, dfrom, dto)

    def _halls(counter: Counter) -> List[sch.AnalyticsRouteHall]:
        return [
            sch.AnalyticsRouteHall(id=h, name=names.get(h), count=c) for h, c in counter.most_common(limit)
        ]

    return sch.AnalyticsRoutes(
        from_=_iso(dfrom),
        to=_iso(dto),
        total_sessions_with_route=len(visit_lengths),
        avg_halls_per_session=round(statistics.fmean(visit_lengths), 2) if visit_lengths else 0.0,
        top_hall_visits=_halls(hall_visits),
        top_entry_halls=_halls(entries),
        top_exit_halls=_halls(exits),
        top_exit_screens=[
            sch.AnalyticsTopItem(name=screen, count=c) for screen, c in exit_screens.most_common(limit)
        ],
        top_transitions=[
            sch.AnalyticsRouteTransition(
                from_hall_id=a, from_hall_name=names.get(a), to_hall_id=b, to_hall_name=names.get(b), count=c
            )
            for (a, b), c in transitions.most_common(limit)
        ],
        top_paths=[
            sch.AnalyticsRoutePath(
                halls=[sch.AnalyticsRouteHall(id=h, name=names.get(h), count=hall_visits.get(h, 0)) for h in path],
                count=c,
            )
            for path, c in paths.most_common(limit)
        ],
        total_devices=total_devices,
        returning_devices=returning,
        avg_sessions_per_device=avg_per_device,
        sessions_per_device_hist=hist,
    )


# ── Аналитика: статистика по экспонатам (§8) ─────────────────────────────────
async def analytics_exhibits(
    session: AsyncSession,
    dfrom: Optional[date],
    dto: Optional[date],
    limit: int = 20,
    order: str = "views",
) -> sch.AnalyticsExhibits:
    """Просмотры, вопросы, озвучки и распознавания по каждому экспонату.

    Список строится ОТ каталога (`exhibits LEFT JOIN events`): экспонат, которого
    никто не открывал, в `events` просто отсутствует, а заказчику нужны именно
    такие «мёртвые» карточки. Фильтр периода стоит в условии JOIN, а не в WHERE —
    иначе LEFT JOIN схлопнулся бы во внутренний и нули опять пропали бы.
    """
    join_conds = [m.Event.exhibit_id == m.Exhibit.id] + _range_conds(m.Event.ts, dfrom, dto)
    views = func.count().filter(m.Event.type == "exhibit_view")
    questions = func.count().filter(m.Event.type == "chat_message")
    tts_plays = func.count().filter(m.Event.type.in_(_TTS_TYPES))
    recognitions = func.count().filter(m.Event.type == "recognition")

    stmt = (
        select(
            m.Exhibit.id,
            m.Exhibit.name,
            m.Hall.hall_number,
            views.label("views"),
            questions.label("questions"),
            tts_plays.label("tts_plays"),
            recognitions.label("recognitions"),
        )
        .select_from(m.Exhibit)
        .outerjoin(m.Showcase, m.Showcase.id == m.Exhibit.showcase_id)
        .outerjoin(m.Hall, m.Hall.id == m.Showcase.hall_id)
        .outerjoin(m.Event, and_(*join_conds))
        .group_by(m.Exhibit.id, m.Exhibit.name, m.Hall.hall_number)
    )
    if order == "questions":
        stmt = stmt.order_by(questions.desc(), views.desc(), m.Exhibit.id)
    elif order == "asc":  # «мёртвые» карточки — от наименее просматриваемых
        stmt = stmt.order_by(views.asc(), questions.asc(), m.Exhibit.id)
    else:
        stmt = stmt.order_by(views.desc(), questions.desc(), m.Exhibit.id)
    rows = (await session.execute(stmt.limit(limit))).all()

    total_exhibits = (await session.execute(select(func.count()).select_from(m.Exhibit))).scalar_one()
    # Считаем от каталога (NOT EXISTS), а не «всего минус просмотренные»: в events
    # могут остаться id уже удалённых экспонатов, и разность занизила бы число.
    seen = (
        select(m.Event.id)
        .where(and_(m.Event.type == "exhibit_view", m.Event.exhibit_id == m.Exhibit.id,
                    *_range_conds(m.Event.ts, dfrom, dto)))
        .exists()
    )
    never_viewed = (
        await session.execute(select(func.count()).select_from(m.Exhibit).where(~seen))
    ).scalar_one()

    return sch.AnalyticsExhibits(
        from_=_iso(dfrom),
        to=_iso(dto),
        order=order,
        total_exhibits=total_exhibits,
        never_viewed=never_viewed,
        items=[
            sch.AnalyticsExhibitRow(
                id=eid, name=name, hall_number=hall_number,
                views=v, questions=q, tts_plays=t, recognitions=r,
            )
            for eid, name, hall_number, v, q, t, r in rows
        ],
    )


# ── Аналитика: качество распознавания (§9) ───────────────────────────────────
async def analytics_recognition(
    session: AsyncSession, dfrom: Optional[date], dto: Optional[date]
) -> sch.AnalyticsRecognition:
    """Успешность распознавания, фолбэк с топ-3 и что посетитель делает после неудачи.

    Всё считается ОДНИМ проходом по событиям визита (§5): для каждого события
    `recognition` смотрим, что было в этом же визите дальше — до следующей
    попытки распознавания. Отдельных запросов на событие нет.
    """
    rows = await _visit_rows(session, dfrom, dto)

    total = success = fallback_shown = fallback_converted = 0
    abandoned = retried = 0
    confidences: List[float] = []

    for _session_id, events in visits.visits_by_session(rows):
        for index, event in enumerate(events):
            if event.type != "recognition":
                continue
            total += 1
            confidence = _prop_float(event.props, "confidence")
            if confidence is not None:
                confidences.append(confidence)
            recognized = _prop_bool(event.props, "recognized")
            fallback = _prop_bool(event.props, "fallback")
            if recognized:
                success += 1

            # Хвост визита до следующей попытки распознавания.
            tail: List = []
            next_recognition = False
            for following in events[index + 1:]:
                if following.type == "recognition":
                    next_recognition = True
                    break
                tail.append(following)

            if fallback:
                fallback_shown += 1
                # «Сконвертировался» — открыл карточку из показанных кандидатов.
                # Кандидатов в событии нет, поэтому опираемся на источник открытия.
                if any(
                    e.type == "exhibit_view" and (e.props or {}).get("source") == "recognition" for e in tail
                ):
                    fallback_converted += 1

            if not recognized:
                if next_recognition:
                    retried += 1
                # «Ушёл» — после неудачи в визите не было ничего, кроме session_end.
                # Повторная съёмка и открытие экспоната руками уходом не считаются.
                elif not [e for e in tail if e.type != "session_end"]:
                    abandoned += 1

    failed = total - success
    return sch.AnalyticsRecognition(
        from_=_iso(dfrom),
        to=_iso(dto),
        total=total,
        success=success,
        success_rate=_rate(success, total),
        fallback_shown=fallback_shown,
        fallback_rate=_rate(fallback_shown, total),
        fallback_converted=fallback_converted,
        fallback_conversion_rate=_rate(fallback_converted, fallback_shown),
        failed=failed,
        abandoned_after_fail=abandoned,
        abandonment_rate=_rate(abandoned, failed),
        retry_after_fail=retried,
        avg_confidence=round(statistics.fmean(confidences), 4) if confidences else 0.0,
    )


# ── Аналитика: ночной пересчёт агрегатов и кэш отчётов (§12) ─────────────────
# По ТЗ реалтайм не требуется — данные обновляются раз в сутки. Отчёты считаются
# джобом (scripts/rebuild_analytics.py) и складываются в analytics_reports;
# эндпоинт отдаёт готовый payload и `updated_at`, чтобы музей видел «данные на
# 03.08.2026 04:00», а не думал, что дашборд сломался.
#
# Кластеризация вопросов и разбор последовательностей событий в плоскую суточную
# схему не ложатся, поэтому отчёт кэшируется целиком; плоские суточные счётчики
# (события по типам, сессии, просмотры) живут отдельно в analytics_daily.
def _period_key(dfrom: Optional[date], dto: Optional[date], variant: str = "") -> str:
    """Ключ периода: '<from>:<to>[:<параметры>]' с пустыми частями для открытых границ."""
    key = f"{_iso(dfrom) or ''}:{_iso(dto) or ''}"
    return f"{key}:{variant}" if variant else key


async def _store_report(
    session: AsyncSession, report: str, key: str, dfrom: Optional[date], dto: Optional[date], result
) -> None:
    payload = result.model_dump(mode="json", by_alias=True)
    stmt = pg_insert(m.AnalyticsReport).values(
        report=report, period_key=key, period_from=dfrom, period_to=dto,
        payload=payload, updated_at=result.updated_at,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[m.AnalyticsReport.report, m.AnalyticsReport.period_key],
        set_={"payload": stmt.excluded.payload, "updated_at": stmt.excluded.updated_at,
              "period_from": stmt.excluded.period_from, "period_to": stmt.excluded.period_to},
    )
    await session.execute(stmt)
    await session.commit()


async def cached_report(
    session: AsyncSession,
    report: str,
    dfrom: Optional[date],
    dto: Optional[date],
    model_cls,
    builder,
    variant: str = "",
    force: bool = False,
):
    """Отдать отчёт из кэша агрегатов; при промахе — посчитать и запомнить.

    Запись старше ANALYTICS_CACHE_TTL_MINUTES считается устаревшей и
    пересчитывается. `variant` разводит наборы параметров (limit/order), чтобы
    отчёт с `limit=50` не подменялся кэшем от `limit=20`.
    """
    key = _period_key(dfrom, dto, variant)
    now = datetime.now(timezone.utc)
    if not force:
        row = (
            await session.execute(
                select(m.AnalyticsReport).where(
                    m.AnalyticsReport.report == report, m.AnalyticsReport.period_key == key
                )
            )
        ).scalar_one_or_none()
        ttl = timedelta(minutes=settings.analytics_cache_ttl_minutes)
        if row is not None and now - row.updated_at <= ttl:
            return model_cls.model_validate(row.payload)

    result = await builder()
    result.updated_at = now
    await _store_report(session, report, key, dfrom, dto, result)
    return result


# Отчёты, которые прогревает ночной джоб (с параметрами по умолчанию).
REPORTS = ("overview", "questions", "unanswered", "engagement", "routes", "exhibits", "recognition")


async def build_report(
    session: AsyncSession, report: str, dfrom: Optional[date], dto: Optional[date], force: bool = False
):
    """Отчёт по имени с параметрами по умолчанию: из кэша, либо пересчёт при `force`."""
    builders = {
        "overview": (sch.AnalyticsOverview, lambda: analytics_overview(session, dfrom, dto)),
        "questions": (sch.AnalyticsQuestions, lambda: analytics_questions(session, dfrom, dto)),
        "unanswered": (sch.AnalyticsUnanswered, lambda: analytics_unanswered(session, dfrom, dto)),
        "engagement": (sch.AnalyticsEngagement, lambda: analytics_engagement(session, dfrom, dto)),
        "routes": (sch.AnalyticsRoutes, lambda: analytics_routes(session, dfrom, dto)),
        "exhibits": (sch.AnalyticsExhibits, lambda: analytics_exhibits(session, dfrom, dto)),
        "recognition": (sch.AnalyticsRecognition, lambda: analytics_recognition(session, dfrom, dto)),
    }
    model_cls, builder = builders[report]
    return await cached_report(session, report, dfrom, dto, model_cls, builder, force=force)


# Плоские суточные метрики. Каждая — отдельный GROUP BY по дню; дороже одного
# запроса, но читаемо и не требует ручной раскладки CASE-ов.
async def rebuild_daily(session: AsyncSession, dfrom: Optional[date], dto: Optional[date]) -> Tuple[int, int]:
    """Пересчитать analytics_daily за период. Идемпотентно: строки за пересчитанные дни удаляются и пишутся заново."""
    day = func.date(m.Event.ts).label("d")
    ev_conds = _range_conds(m.Event.ts, dfrom, dto)
    values: List[dict] = []

    def _add(rows, metric: str, with_dimension: bool = False) -> None:
        for row in rows:
            if with_dimension:
                d, dim, value = row
                if dim is None:
                    continue
                values.append({
                    "date": d, "metric": metric, "dimension_key": str(dim),
                    "dimension_id": dim if isinstance(dim, int) else None, "value": float(value),
                })
            else:
                d, value = row
                values.append({"date": d, "metric": metric, "dimension_key": "",
                               "dimension_id": None, "value": float(value)})

    base = select(day, func.count()).group_by(day)
    if ev_conds:
        base = base.where(and_(*ev_conds))
    _add((await session.execute(base)).all(), "events_total")

    by_type = select(day, m.Event.type, func.count()).group_by(day, m.Event.type)
    if ev_conds:
        by_type = by_type.where(and_(*ev_conds))
    _add((await session.execute(by_type)).all(), "events_by_type", with_dimension=True)

    sessions_stmt = select(day, func.count(func.distinct(m.Event.session_id))).group_by(day)
    if ev_conds:
        sessions_stmt = sessions_stmt.where(and_(*ev_conds))
    _add((await session.execute(sessions_stmt)).all(), "sessions")

    views_stmt = (
        select(day, m.Event.exhibit_id, func.count())
        .where(and_(m.Event.type == "exhibit_view", m.Event.exhibit_id.isnot(None), *ev_conds))
        .group_by(day, m.Event.exhibit_id)
    )
    _add((await session.execute(views_stmt)).all(), "exhibit_views", with_dimension=True)

    halls_stmt = (
        select(day, m.Event.hall_id, func.count())
        .where(and_(m.Event.type == "hall_view", m.Event.hall_id.isnot(None), *ev_conds))
        .group_by(day, m.Event.hall_id)
    )
    _add((await session.execute(halls_stmt)).all(), "hall_views", with_dimension=True)

    success_stmt = (
        select(day, func.count())
        .where(and_(m.Event.type == "recognition",
                    cast(m.Event.props["recognized"].astext, String) == "true", *ev_conds))
        .group_by(day)
    )
    _add((await session.execute(success_stmt)).all(), "recognition_success")

    msg_day = func.date(m.GuideMessage.created_at).label("d")
    msg_conds = [m.GuideMessage.role == "user"] + _range_conds(m.GuideMessage.created_at, dfrom, dto)
    msg_stmt = select(msg_day, func.count()).where(and_(*msg_conds)).group_by(msg_day)
    _add((await session.execute(msg_stmt)).all(), "chat_messages")

    unanswered_stmt = (
        select(msg_day, func.count())
        .where(and_(*msg_conds, m.GuideMessage.answered.is_(False)))
        .group_by(msg_day)
    )
    _add((await session.execute(unanswered_stmt)).all(), "chat_messages_unanswered")

    days = {row["date"] for row in values}
    if days:
        await session.execute(sa_delete(m.AnalyticsDaily).where(m.AnalyticsDaily.date.in_(list(days))))
        # Одна дата+метрика+измерение может прийти из разных запросов только при
        # совпадении ключа — на всякий случай схлопываем, чтобы не словить
        # нарушение первичного ключа на пакетной вставке.
        unique = {(row["date"], row["metric"], row["dimension_key"]): row for row in values}
        await session.execute(pg_insert(m.AnalyticsDaily), list(unique.values()))
        await session.commit()
        return len(days), len(unique)
    await session.commit()
    return 0, 0


async def rebuild_analytics(
    session: AsyncSession, dfrom: Optional[date] = None, dto: Optional[date] = None
) -> sch.AnalyticsRebuildResult:
    """Пересчитать суточный срез и прогреть кэш всех отчётов за период."""
    days, rows = await rebuild_daily(session, dfrom, dto)
    for report in REPORTS:
        await build_report(session, report, dfrom, dto, force=True)
    return sch.AnalyticsRebuildResult(
        rebuilt_reports=list(REPORTS),
        daily_days=days,
        daily_rows=rows,
        updated_at=datetime.now(timezone.utc),
    )


async def analytics_daily_series(
    session: AsyncSession, dfrom: Optional[date], dto: Optional[date], metric: Optional[str] = None
) -> sch.AnalyticsDailySeries:
    """Суточный ряд из analytics_daily — то, что заполняет ночной джоб."""
    conds = _range_conds(m.AnalyticsDaily.date, dfrom, dto)
    if metric:
        conds.append(m.AnalyticsDaily.metric == metric)
    stmt = select(m.AnalyticsDaily)
    if conds:
        stmt = stmt.where(and_(*conds))
    rows = (
        await session.execute(
            stmt.order_by(m.AnalyticsDaily.date, m.AnalyticsDaily.metric, m.AnalyticsDaily.dimension_key)
        )
    ).scalars().all()
    updated = max((r.updated_at for r in rows), default=None)
    return sch.AnalyticsDailySeries(
        from_=_iso(dfrom),
        to=_iso(dto),
        updated_at=updated,
        points=[
            sch.AnalyticsDailyPoint(
                date=r.date.isoformat(), metric=r.metric, dimension_key=r.dimension_key,
                dimension_id=r.dimension_id, value=r.value,
            )
            for r in rows
        ],
    )
