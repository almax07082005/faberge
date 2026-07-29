"""Распознавание экспоната по фото (YOLO)."""
from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crud
from .. import schemas as sch
from ..config import settings
from ..db import get_session
from ..services import UpstreamError, recognizer

router = APIRouter(tags=["Распознавание"])
logger = logging.getLogger(__name__)

_ALLOWED = {"image/jpeg", "image/png", "image/webp"}


@router.post("/recognition", response_model=sch.RecognitionResponse, summary="Распознать экспонат по фото")
async def recognize_exhibit(
    file: UploadFile = File(..., description="Фото экспоната (JPEG/PNG/WebP)."),
    hall_id: Optional[int] = Form(None),
    top_k: int = Form(3, ge=1, le=10),
    session: AsyncSession = Depends(get_session),
) -> sch.RecognitionResponse:
    if file.content_type not in _ALLOWED:
        raise HTTPException(status_code=415, detail="Поддерживаются только JPEG, PNG и WebP.")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Пустой файл изображения.")
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Размер файла превышает {settings.max_upload_mb} МБ.")

    known = await crud.all_label_slugs(session)
    # Реальный ML-сервис возвращает названия (title); карта имя→slug нужна только
    # в реал-режиме — в стабе лишний запрос не делаем.
    name_to_slug = await crud.slug_by_name(session) if settings.yolo_configured else {}
    t0 = time.monotonic()
    try:
        outcome = await recognizer.recognize(data, known, hall_id, top_k, name_to_slug=name_to_slug)
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=exc.message)
    processing_ms = int((time.monotonic() - t0) * 1000)

    # E19: пустой список кандидатов — это для посетителя глухая ошибка «не удалось
    # распознать». Если модель что-то нашла, но название не сшилось с каталогом,
    # добираем кандидатов полнотекстовым поиском по этим названиям — фронт покажет
    # топ-3 «возможно, это». Уверенность берём модели: она про фото, а не про то,
    # насколько наш поиск угадал карточку.
    if not outcome.candidates and outcome.unmatched:
        query = " ".join(title for title, _ in outcome.unmatched if title).strip()
        found = await crud.search_exhibits_orm(session, query, limit=top_k) if query else []
        fallback_conf = outcome.unmatched[0][1]
        outcome.candidates = [(e.label_slug, fallback_conf) for e in found if e.label_slug]
        logger.info(
            "recognition: кандидаты добраны поиском по каталогу для %r → %s",
            query, [slug for slug, _ in outcome.candidates],
        )

    exhibit = None
    if outcome.recognized and outcome.label_slug:
        ex = await crud.get_exhibit_by_slug_orm(session, outcome.label_slug)
        exhibit = crud.to_exhibit(ex) if ex else None

    slugs = [slug for slug, _ in outcome.candidates]
    names = await crud.names_by_slugs(session, slugs)
    # B5/E19: к каждому кандидату — id карточки и миниатюра, чтобы фронт вёл на
    # карточку экспоната (а не в чат) и показывал фото.
    briefs = await crud.candidates_by_slugs(session, slugs)
    candidates = [
        sch.RecognitionCandidate(
            label_slug=slug,
            name=names.get(slug),
            confidence=conf,
            exhibit_id=briefs.get(slug, (None, None))[0],
            thumbnail_url=briefs.get(slug, (None, None))[1],
        )
        for slug, conf in outcome.candidates
    ]

    return sch.RecognitionResponse(
        recognized=bool(exhibit) if outcome.recognized else False,
        label_slug=outcome.label_slug if exhibit else None,
        confidence=outcome.confidence,
        exhibit=exhibit,
        candidates=candidates,
        request_id=str(uuid.uuid4()),
        processing_ms=processing_ms,
    )
