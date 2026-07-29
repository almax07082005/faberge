"""Озвучивание (Yandex SpeechKit) для кнопки «Прослушать»."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crud
from .. import schemas as sch
from ..db import get_session
from ..services import UpstreamError, tts

router = APIRouter(tags=["Озвучивание"])


@router.post("/speech", response_model=sch.SpeechResponse, summary="Синтез речи (Прослушать)")
async def synthesize_speech(req: sch.SpeechRequest, session: AsyncSession = Depends(get_session)) -> sch.SpeechResponse:
    text = req.text
    if not text and req.exhibit_id is not None:
        ex = await crud.get_exhibit_orm(session, req.exhibit_id)
        if ex is None:
            raise HTTPException(status_code=404, detail="Экспонат не найден.")
        # E15: озвучиваем spoken-версию (числа прописью в нужном падеже). Если её
        # нет (LLM не сгенерировал/не настроен) — исходное описание; числа в нём
        # приведёт к произносимому виду tts.prepare_for_tts (LLM + фолбэк на
        # детерминированный normalize_for_tts).
        text = ex.short_description_spoken or ex.short_description or ex.name
    if not text:
        raise HTTPException(status_code=400, detail="Укажите text или exhibit_id.")

    try:
        outcome = await tts.synthesize(text, req.voice.value, req.format.value, req.speed, req.emotion)
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=exc.message)

    return sch.SpeechResponse(
        audio_url=outcome.audio_url,
        format=sch.AudioFormat(outcome.fmt),
        voice=req.voice,
        duration_ms=outcome.duration_ms,
        characters=outcome.characters,
        cached=outcome.cached,
    )
