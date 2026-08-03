"""Телеметрия: приём пользовательских событий для аналитики.

С 21.07.2026 — обязательная часть контракта: фронт шлёт события, без которых
аналитика посетителей пуста (B11). С 03.08.2026 действует словарь типов
(`schemas.EventType`) и белый список ключей `props`: эндпоинт открыт без
авторизации, поэтому и объём батча, и состав полей ограничены — см. §1/§10 ТЗ
и docs/analytics-privacy.md.

Приватность: ни IP, ни User-Agent запроса здесь не читаются и не пишутся —
ни в `events`, ни в логи.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crud
from .. import schemas as sch
from ..db import get_session

router = APIRouter(tags=["Телеметрия"])


@router.post(
    "/telemetry/events",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=sch.EventIngestResult,
    summary="Отправить события",
    description=(
        "Принимает батч событий (до "
        f"{sch.MAX_EVENTS_PER_BATCH} штук; больше — `422`).\n\n"
        "* событие с типом вне словаря **не роняет батч** — оно отбрасывается, "
        "остальные записываются; их число возвращается в `rejected`;\n"
        "* устаревший тип `audio_play` нормализуется в канонический `tts_play`;\n"
        "* в `props` сохраняются только ключи из контракта события, остальные "
        "отбрасываются; `props.text` длиннее "
        f"{sch.MAX_PROPS_TEXT_LEN} символов обрезается."
    ),
)
async def ingest_events(
    batch: sch.EventBatch, session: AsyncSession = Depends(get_session)
) -> sch.EventIngestResult:
    accepted, rejected = await crud.insert_events(session, batch)
    return sch.EventIngestResult(accepted=accepted, rejected=rejected)
