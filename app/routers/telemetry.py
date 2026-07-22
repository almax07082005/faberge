"""Телеметрия: приём пользовательских событий для аналитики.

С 21.07.2026 — обязательная часть контракта: фронт шлёт события (app_open,
hall_view, exhibit_view, recognition, audio_play), без которых
GET /admin/analytics/engagement и /analytics/routes возвращают пустоту (B11).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crud
from .. import schemas as sch
from ..db import get_session

router = APIRouter(tags=["Телеметрия"])


@router.post("/telemetry/events", status_code=status.HTTP_202_ACCEPTED, summary="Отправить события")
async def ingest_events(batch: sch.EventBatch, session: AsyncSession = Depends(get_session)) -> dict:
    accepted = await crud.insert_events(session, batch)
    return {"accepted": accepted}
