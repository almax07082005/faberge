"""Экспонаты: карточка и «Другие экспонаты зала»."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crud
from .. import schemas as sch
from ..db import get_session
from ..dependencies import Pagination, pagination

router = APIRouter(tags=["Экспонаты"])


@router.get("/exhibits", response_model=sch.ExhibitListResponse, summary="Список / фильтрация экспонатов")
async def list_exhibits(
    p: Pagination = Depends(pagination),
    hall_id: Optional[int] = Query(None, ge=1),
    showcase_id: Optional[int] = Query(None, ge=1),
    label_slug: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    is_temporary: Optional[bool] = Query(
        None, description="Фильтр по типу зала экспоната: true — временная выставка, false — основная экспозиция."
    ),
    session: AsyncSession = Depends(get_session),
) -> sch.ExhibitListResponse:
    return await crud.list_exhibits(session, p.limit, p.offset, hall_id, showcase_id, label_slug, q, is_temporary)


@router.get("/exhibits/by-slug/{label_slug}", response_model=sch.Exhibit, summary="Экспонат по label_slug")
async def get_exhibit_by_slug(label_slug: str = Path(...), session: AsyncSession = Depends(get_session)) -> sch.Exhibit:
    ex = await crud.get_exhibit_by_slug_orm(session, label_slug)
    if ex is None:
        raise HTTPException(status_code=404, detail="Экспонат не найден.")
    return crud.to_exhibit(ex)


@router.get("/exhibits/{exhibit_id}", response_model=sch.Exhibit, summary="Карточка экспоната")
async def get_exhibit(exhibit_id: int = Path(ge=1), session: AsyncSession = Depends(get_session)) -> sch.Exhibit:
    ex = await crud.get_exhibit_orm(session, exhibit_id)
    if ex is None:
        raise HTTPException(status_code=404, detail="Экспонат не найден.")
    return crud.to_exhibit(ex)


@router.get("/exhibits/{exhibit_id}/related", response_model=sch.ExhibitListResponse, summary="Другие экспонаты зала")
async def list_related(
    exhibit_id: int = Path(ge=1),
    limit: int = Query(12, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
) -> sch.ExhibitListResponse:
    result = await crud.list_related(session, exhibit_id, limit)
    if result is None:
        raise HTTPException(status_code=404, detail="Экспонат не найден.")
    return result
