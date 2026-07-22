"""Запросы к БД и сериализация ORM → Pydantic."""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import date
from typing import Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy import String, and_, cast, delete as sa_delete, func, or_, select, text, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from . import models as m
from . import schemas as sch


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
async def get_map(session: AsyncSession, is_temporary: Optional[bool] = None) -> sch.MapResponse:
    stmt = select(m.Hall).options(selectinload(m.Hall.showcases)).order_by(m.Hall.sort_order, m.Hall.hall_number)
    if is_temporary is not None:
        stmt = stmt.where(m.Hall.is_temporary == is_temporary)
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
                cover_image_url=h.cover_image_url, is_temporary=h.is_temporary, sort_order=h.sort_order,
                showcase_count=showcase_counts.get(h.id, 0),
                exhibit_count=exhibit_counts.get(h.id, 0), showcases=showcases,
            )
        )
    return sch.MapResponse(halls=map_halls)


async def list_halls(
    session: AsyncSession, limit: int, offset: int, is_temporary: Optional[bool] = None
) -> sch.HallListResponse:
    count_stmt = select(func.count(m.Hall.id))
    list_stmt = select(m.Hall).order_by(m.Hall.sort_order, m.Hall.hall_number)
    if is_temporary is not None:
        count_stmt = count_stmt.where(m.Hall.is_temporary == is_temporary)
        list_stmt = list_stmt.where(m.Hall.is_temporary == is_temporary)
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
        cover_image_url=hall.cover_image_url, is_temporary=hall.is_temporary, sort_order=hall.sort_order,
        showcase_count=showcase_counts.get(hall.id, 0),
        exhibit_count=exhibit_counts.get(hall.id, 0), showcases=showcases,
    )


async def hall_exists(session: AsyncSession, hall_id: int) -> bool:
    return (await session.execute(select(m.Hall.id).where(m.Hall.id == hall_id))).scalar_one_or_none() is not None


async def list_hall_showcases(session: AsyncSession, hall_id: int, limit: int, offset: int) -> sch.ShowcaseListResponse:
    total = (await session.execute(select(func.count(m.Showcase.id)).where(m.Showcase.hall_id == hall_id))).scalar_one()
    showcases = (
        (
            await session.execute(
                select(m.Showcase).where(m.Showcase.hall_id == hall_id).order_by(m.Showcase.showcase_number).limit(limit).offset(offset)
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
    list_stmt = select(m.Showcase).order_by(m.Showcase.hall_id, m.Showcase.showcase_number)
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
        .order_by(func.ts_rank(m.Hall.search_vector, tsq).desc(), m.Hall.hall_number)
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


async def all_halls_ordered(session: AsyncSession) -> List[m.Hall]:
    """Все залы в порядке каталога (B10): для ответа «какие залы есть»."""
    stmt = select(m.Hall).order_by(m.Hall.sort_order, m.Hall.hall_number)
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
    hall = m.Hall(
        hall_number=data.hall_number, name=data.name, description=data.description,
        level=data.level, is_temporary=data.is_temporary,
        # По умолчанию порядок = номер зала (новый зал встаёт в конец естественного
        # порядка); админ переставит через reorder / PATCH sort_order.
        sort_order=data.sort_order if data.sort_order is not None else data.hall_number,
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


# ── Телеметрия / аналитика ───────────────────────────────────────────────────
async def insert_events(session: AsyncSession, batch: sch.EventBatch) -> int:
    objs = [
        m.Event(
            session_id=batch.session_id, type=e.type, exhibit_id=e.exhibit_id, hall_id=e.hall_id,
            label_slug=e.label_slug, props=e.props, ts=e.ts,
        )
        for e in batch.events
    ]
    session.add_all(objs)
    await session.commit()
    return len(objs)


async def analytics_overview(session: AsyncSession, dfrom: Optional[date], dto: Optional[date]) -> sch.AnalyticsOverview:
    def _range(col):
        conds = []
        if dfrom is not None:
            conds.append(col >= dfrom)
        if dto is not None:
            conds.append(col < dto)
        return and_(*conds) if conds else None

    ev_range = _range(m.Event.ts)

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
    success = await _count(and_(m.Event.type == "recognition", cast(m.Event.props["recognized"].astext, String) == "true"))
    total_audio_plays = await _count(m.Event.type == "audio_play")

    msg_stmt = select(func.count()).select_from(m.GuideMessage).where(m.GuideMessage.role == "user")
    if dfrom is not None:
        msg_stmt = msg_stmt.where(m.GuideMessage.created_at >= dfrom)
    if dto is not None:
        msg_stmt = msg_stmt.where(m.GuideMessage.created_at < dto)
    total_chat_messages = (await session.execute(msg_stmt)).scalar_one()

    top_exhibits = await _top_items(session, m.Event.exhibit_id, "exhibit_view", m.Exhibit, ev_range)
    top_halls = await _top_items(session, m.Event.hall_id, "hall_view", m.Hall, ev_range)

    rate = round(success / total_recognitions, 4) if total_recognitions else 0.0
    return sch.AnalyticsOverview(
        from_=dfrom.isoformat() if dfrom else None,
        to=dto.isoformat() if dto else None,
        total_sessions=total_sessions,
        total_app_opens=total_app_opens,
        total_recognitions=total_recognitions,
        recognition_success_rate=rate,
        total_chat_messages=total_chat_messages,
        total_audio_plays=total_audio_plays,
        top_exhibits=top_exhibits,
        top_halls=top_halls,
    )


async def _top_items(session: AsyncSession, id_col, ev_type: str, entity, ev_range) -> List[sch.AnalyticsTopItem]:
    stmt = select(id_col, func.count().label("c")).select_from(m.Event).where(m.Event.type == ev_type, id_col.isnot(None))
    if ev_range is not None:
        stmt = stmt.where(ev_range)
    stmt = stmt.group_by(id_col).order_by(func.count().desc()).limit(5)
    rows = (await session.execute(stmt)).all()
    items: List[sch.AnalyticsTopItem] = []
    for ent_id, count in rows:
        name = (await session.execute(select(entity.name).where(entity.id == ent_id))).scalar_one_or_none()
        items.append(sch.AnalyticsTopItem(id=ent_id, name=name, count=count))
    return items


async def _hall_names(session: AsyncSession, hall_ids: Set[int]) -> Dict[int, Optional[str]]:
    if not hall_ids:
        return {}
    rows = await session.execute(select(m.Hall.id, m.Hall.name).where(m.Hall.id.in_(list(hall_ids))))
    return {hid: name for hid, name in rows.all()}


# ── C16: частые/редкие вопросы (агрегат по guide_messages) ────────────────────
async def analytics_questions(
    session: AsyncSession, dfrom: Optional[date], dto: Optional[date], limit: int = 20
) -> sch.AnalyticsQuestions:
    """Топ частых и редких вопросов посетителей.

    Источник — реальные реплики пользователя (``guide_messages.role='user'``).
    Формулировки нормализуем (trim + lower) и группируем, чтобы «Кто это?» и
    «кто это? » считались одним вопросом.
    """
    norm = func.lower(func.trim(m.GuideMessage.content))
    conds = [m.GuideMessage.role == "user", func.length(func.trim(m.GuideMessage.content)) > 0]
    if dfrom is not None:
        conds.append(m.GuideMessage.created_at >= dfrom)
    if dto is not None:
        conds.append(m.GuideMessage.created_at < dto)
    where = and_(*conds)

    grouped = select(norm.label("q"), func.count().label("c")).where(where).group_by(norm)
    freq_rows = (await session.execute(grouped.order_by(func.count().desc(), norm).limit(limit))).all()
    rare_rows = (await session.execute(grouped.order_by(func.count().asc(), norm).limit(limit))).all()

    total = (await session.execute(select(func.count()).select_from(m.GuideMessage).where(where))).scalar_one()
    unique = (await session.execute(select(func.count()).select_from(grouped.subquery()))).scalar_one()

    return sch.AnalyticsQuestions(
        from_=dfrom.isoformat() if dfrom else None,
        to=dto.isoformat() if dto else None,
        total_questions=total,
        unique_questions=unique,
        frequent=[sch.AnalyticsQuestionItem(question=q, count=c) for q, c in freq_rows],
        rare=[sch.AnalyticsQuestionItem(question=q, count=c) for q, c in rare_rows],
    )


# ── C17: длительность сессии (первое открытие → последнее взаимодействие) ─────
_DURATION_BUCKETS = [("0–1 мин", 0, 60), ("1–5 мин", 60, 300), ("5–15 мин", 300, 900), ("15+ мин", 900, None)]


async def analytics_engagement(
    session: AsyncSession, dfrom: Optional[date], dto: Optional[date]
) -> sch.AnalyticsEngagement:
    """Вовлечённость: время от первого события сессии до последнего.

    ``first_open`` = MIN(ts) по сессии (обычно событие ``app_open``),
    ``last_interaction`` = MAX(ts). Длительность = их разница. Считаем среднее,
    медиану, максимум и распределение по корзинам — без спец. событий, только по
    ``events`` (session_id + ts).
    """
    conds = [m.Event.session_id.isnot(None)]
    if dfrom is not None:
        conds.append(m.Event.ts >= dfrom)
    if dto is not None:
        conds.append(m.Event.ts < dto)
    rows = (
        await session.execute(
            select(
                m.Event.session_id,
                func.min(m.Event.ts),
                func.max(m.Event.ts),
                func.count(),
            )
            .where(and_(*conds))
            .group_by(m.Event.session_id)
        )
    ).all()

    durations: List[float] = []
    event_counts: List[int] = []
    for _sid, first, last, n in rows:
        durations.append((last - first).total_seconds())
        event_counts.append(n)

    total = len(rows)
    buckets = [
        sch.AnalyticsDurationBucket(
            label=label,
            count=sum(1 for d in durations if d >= lo and (hi is None or d < hi)),
        )
        for label, lo, hi in _DURATION_BUCKETS
    ]
    return sch.AnalyticsEngagement(
        from_=dfrom.isoformat() if dfrom else None,
        to=dto.isoformat() if dto else None,
        total_sessions=total,
        avg_duration_sec=round(statistics.fmean(durations), 1) if durations else 0.0,
        median_duration_sec=round(statistics.median(durations), 1) if durations else 0.0,
        max_duration_sec=round(max(durations), 1) if durations else 0.0,
        avg_events_per_session=round(statistics.fmean(event_counts), 2) if event_counts else 0.0,
        buckets=buckets,
    )


# ── C18: маршрут пользователя по залам (агрегат по hall_view) ─────────────────
async def analytics_routes(
    session: AsyncSession, dfrom: Optional[date], dto: Optional[date], limit: int = 10
) -> sch.AnalyticsRoutes:
    """Маршрут по залам: посещения, точки входа, переходы A→B, частые пути.

    Строим последовательность залов на сессию из событий ``hall_view`` по ``ts``,
    схлопывая подряд идущие повторы одного зала (переоткрытие карточек в том же
    зале не считаем переходом).
    """
    conds = [m.Event.type == "hall_view", m.Event.hall_id.isnot(None), m.Event.session_id.isnot(None)]
    if dfrom is not None:
        conds.append(m.Event.ts >= dfrom)
    if dto is not None:
        conds.append(m.Event.ts < dto)
    rows = (
        await session.execute(
            select(m.Event.session_id, m.Event.hall_id)
            .where(and_(*conds))
            .order_by(m.Event.session_id, m.Event.ts)
        )
    ).all()

    sequences: Dict[object, List[int]] = defaultdict(list)
    for sid, hid in rows:
        seq = sequences[sid]
        if not seq or seq[-1] != hid:  # схлопываем подряд идущие дубли
            seq.append(hid)

    visits: Counter = Counter()
    entries: Counter = Counter()
    transitions: Counter = Counter()
    paths: Counter = Counter()
    lengths: List[int] = []
    for seq in sequences.values():
        if not seq:
            continue
        lengths.append(len(seq))
        entries[seq[0]] += 1
        visits.update(seq)
        transitions.update(zip(seq, seq[1:]))
        paths[tuple(seq)] += 1

    all_ids: Set[int] = set(visits)
    for a, b in transitions:
        all_ids.add(a)
        all_ids.add(b)
    names = await _hall_names(session, all_ids)

    total_sessions = len(lengths)
    return sch.AnalyticsRoutes(
        from_=dfrom.isoformat() if dfrom else None,
        to=dto.isoformat() if dto else None,
        total_sessions_with_route=total_sessions,
        avg_halls_per_session=round(statistics.fmean(lengths), 2) if lengths else 0.0,
        top_hall_visits=[sch.AnalyticsRouteHall(id=h, name=names.get(h), count=c) for h, c in visits.most_common(limit)],
        top_entry_halls=[sch.AnalyticsRouteHall(id=h, name=names.get(h), count=c) for h, c in entries.most_common(limit)],
        top_transitions=[
            sch.AnalyticsRouteTransition(
                from_hall_id=a, from_hall_name=names.get(a), to_hall_id=b, to_hall_name=names.get(b), count=c
            )
            for (a, b), c in transitions.most_common(limit)
        ],
        top_paths=[
            sch.AnalyticsRoutePath(
                halls=[sch.AnalyticsRouteHall(id=h, name=names.get(h), count=visits.get(h, 0)) for h in path],
                count=c,
            )
            for path, c in paths.most_common(limit)
        ],
    )
