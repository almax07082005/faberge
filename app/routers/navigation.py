"""Карта и навигация: зал → витрина → экспонат."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crud
from .. import schemas as sch
from ..db import get_session
from ..dependencies import Pagination, pagination

router = APIRouter(tags=["Карта и навигация"])


_INCLUDE_SERVICE_DESC = (
    "Включить служебные записи каталога (`is_service`) — технические залы и "
    "вспомогательные пространства. "
    "По умолчанию `false` — посетителю они не показываются. `true` нужен админке, "
    "чтобы такие записи оставались управляемыми."
)


@router.get("/map", response_model=sch.MapResponse, summary="Интерактивная карта музея")
async def get_map(
    is_temporary: Optional[bool] = Query(
        None, description="Фильтр по типу зала: true — временные выставки, false — основная экспозиция."
    ),
    include_service: bool = Query(False, description=_INCLUDE_SERVICE_DESC),
    session: AsyncSession = Depends(get_session),
) -> sch.MapResponse:
    return await crud.get_map(session, is_temporary, include_service)


@router.get("/halls", response_model=sch.HallListResponse, summary="Список залов")
async def list_halls(
    p: Pagination = Depends(pagination),
    is_temporary: Optional[bool] = Query(
        None, description="Фильтр по типу зала: true — временные выставки, false — основная экспозиция."
    ),
    include_service: bool = Query(False, description=_INCLUDE_SERVICE_DESC),
    session: AsyncSession = Depends(get_session),
) -> sch.HallListResponse:
    return await crud.list_halls(session, p.limit, p.offset, is_temporary, include_service)


@router.get("/halls/{hall_id}", response_model=sch.HallDetail, summary="Получить зал")
async def get_hall(hall_id: int = Path(ge=1), session: AsyncSession = Depends(get_session)) -> sch.HallDetail:
    hall = await crud.get_hall(session, hall_id)
    if hall is None:
        raise HTTPException(status_code=404, detail="Зал не найден.")
    return hall


@router.get("/halls/{hall_id}/showcases", response_model=sch.ShowcaseListResponse, summary="Витрины зала")
async def list_hall_showcases(
    hall_id: int = Path(ge=1), p: Pagination = Depends(pagination), session: AsyncSession = Depends(get_session)
) -> sch.ShowcaseListResponse:
    if not await crud.hall_exists(session, hall_id):
        raise HTTPException(status_code=404, detail="Зал не найден.")
    return await crud.list_hall_showcases(session, hall_id, p.limit, p.offset)


@router.get("/halls/{hall_id}/exhibits", response_model=sch.ExhibitListResponse, summary="Экспонаты зала")
async def list_hall_exhibits(
    hall_id: int = Path(ge=1), p: Pagination = Depends(pagination), session: AsyncSession = Depends(get_session)
) -> sch.ExhibitListResponse:
    if not await crud.hall_exists(session, hall_id):
        raise HTTPException(status_code=404, detail="Зал не найден.")
    return await crud.list_hall_exhibits(session, hall_id, p.limit, p.offset)


@router.get("/showcases", response_model=sch.ShowcaseListResponse, summary="Список витрин")
async def list_showcases(
    p: Pagination = Depends(pagination),
    hall_id: Optional[int] = Query(None, ge=1, description="Фильтр по идентификатору зала."),
    session: AsyncSession = Depends(get_session),
) -> sch.ShowcaseListResponse:
    return await crud.list_showcases(session, p.limit, p.offset, hall_id)


@router.get("/showcases/{showcase_id}", response_model=sch.ShowcaseDetail, summary="Получить витрину")
async def get_showcase(showcase_id: int = Path(ge=1), session: AsyncSession = Depends(get_session)) -> sch.ShowcaseDetail:
    showcase = await crud.get_showcase(session, showcase_id)
    if showcase is None:
        raise HTTPException(status_code=404, detail="Витрина не найдена.")
    return showcase


@router.get("/showcases/{showcase_id}/exhibits", response_model=sch.ExhibitListResponse, summary="Экспонаты витрины")
async def list_showcase_exhibits(
    showcase_id: int = Path(ge=1), p: Pagination = Depends(pagination), session: AsyncSession = Depends(get_session)
) -> sch.ExhibitListResponse:
    if not await crud.showcase_exists(session, showcase_id):
        raise HTTPException(status_code=404, detail="Витрина не найдена.")
    return await crud.list_showcase_exhibits(session, showcase_id, p.limit, p.offset)
