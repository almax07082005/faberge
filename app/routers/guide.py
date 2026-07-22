"""ИИ-гид: генерация рассказа (YandexGPT) и диалог с подсказками."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crud
from .. import models as m
from .. import schemas as sch
from ..db import get_session
from ..services import UpstreamError, guide_intel, llm, tts

router = APIRouter(prefix="/guide", tags=["ИИ-гид"])


# ── Вспомогательные (детерминированные ответы гида по каталогу) ───────────────
def _location_phrase(ex: m.Exhibit) -> str:
    """«в зале 3 «Синяя гостиная», витрина 2» — из привязки экспоната."""
    if ex.showcase is None:
        return ""
    hall = ex.showcase.hall
    parts = []
    if hall is not None:
        parts.append(f"в зале {hall.hall_number}" + (f" «{hall.name}»" if hall.name else ""))
    parts.append(f"витрина {ex.showcase.showcase_number}")
    return ", ".join(parts)


def _describe_exhibit(ex: m.Exhibit) -> str:
    """Детерминированный ответ по одному экспонату (B9, единственное совпадение)."""
    num = f"№{ex.exhibit_number} " if ex.exhibit_number else ""
    text = f"{num}«{ex.name}»"
    if ex.short_description:
        text += f" — {ex.short_description}"
    where = _location_phrase(ex)
    if where:
        text += f" Найти его можно {where}."
    return text


def _describe_halls(halls: List[m.Hall]) -> str:
    """Детерминированный ответ со списком залов (B10)."""
    if not halls:
        return "Пока в каталоге нет залов."
    main = [h for h in halls if not h.is_temporary]
    temp = [h for h in halls if h.is_temporary]

    def _fmt(items: List[m.Hall]) -> str:
        return "; ".join(f"зал {h.hall_number}" + (f" «{h.name}»" if h.name else "") for h in items)

    lines = [f"В музее {len(halls)} залов."]
    if main:
        lines.append(f"Основная экспозиция: {_fmt(main)}.")
    if temp:
        lines.append(f"Временные выставки: {_fmt(temp)}.")
    return " ".join(lines)


def _where_hint(ex: m.Exhibit) -> str:
    where = _location_phrase(ex)
    return where[0].upper() + where[1:] if where else ex.name


def _merge_referenced(context_exhibit: Optional[m.Exhibit], found: List[m.Exhibit]) -> List[sch.ReferencedExhibit]:
    """Плашки экспонатов (B6): контекстный экспонат первым, затем найденные; без дублей, максимум 4."""
    out: List[sch.ReferencedExhibit] = []
    seen: set[int] = set()
    for e in ([context_exhibit] if context_exhibit is not None else []) + list(found):
        if e.id in seen:
            continue
        seen.add(e.id)
        out.append(crud.to_referenced_exhibit(e))
        if len(out) >= 4:
            break
    return out


@router.post("/story", response_model=sch.StoryResponse, summary="Сгенерировать рассказ об экспонате")
async def generate_story(req: sch.StoryRequest, session: AsyncSession = Depends(get_session)) -> sch.StoryResponse:
    if req.exhibit_id is None and not req.label_slug:
        raise HTTPException(status_code=400, detail="Укажите exhibit_id или label_slug.")

    ex = None
    if req.exhibit_id is not None:
        ex = await crud.get_exhibit_orm(session, req.exhibit_id)
    elif req.label_slug:
        ex = await crud.get_exhibit_by_slug_orm(session, req.label_slug)
    if ex is None:
        raise HTTPException(status_code=404, detail="Экспонат не найден.")

    try:
        text, questions, model = await llm.generate_story(
            crud.exhibit_to_dict(ex), req.style.value, req.language, req.max_questions
        )
        audio_url = None
        if req.include_audio:
            audio_url = (await tts.synthesize(text)).audio_url
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=exc.message)

    return sch.StoryResponse(
        exhibit_id=ex.id,
        label_slug=ex.label_slug,
        style=req.style,
        text=text,
        suggested_questions=questions,
        audio_url=audio_url,
        model=model,
        generated_at=datetime.now(timezone.utc),
    )


@router.post("/chat", response_model=sch.ChatResponse, summary="Диалог с ИИ-гидом")
async def chat(req: sch.ChatRequest, session: AsyncSession = Depends(get_session)) -> sch.ChatResponse:
    # Сессия диалога.
    sess: Optional[m.GuideSession] = None
    if req.session_id is not None:
        sess = await session.get(m.GuideSession, req.session_id)
    context = req.context
    if sess is None:
        sess = m.GuideSession(context=context.model_dump() if context else None)
        session.add(sess)
        await session.flush()
    elif context is None and sess.context:
        context = sch.GuideContext(**sess.context)

    # Контекст-обоснование для модели.
    grounding = ""
    exhibit_dict = None
    context_exhibit: Optional[m.Exhibit] = None  # ORM-экспонат из контекста (для плашки/location)
    if context is not None:
        ex = None
        if context.exhibit_id is not None:
            ex = await crud.get_exhibit_orm(session, context.exhibit_id)
        elif context.label_slug:
            ex = await crud.get_exhibit_by_slug_orm(session, context.label_slug)
        if ex is not None:
            context_exhibit = ex
            exhibit_dict = crud.exhibit_to_dict(ex)
            grounding = " ".join(p for p in (ex.short_description, ex.raw_history) if p)
            context = sch.GuideContext(exhibit_id=ex.id, label_slug=ex.label_slug, hall_id=context.hall_id)
        elif context.hall_id is not None:
            hall = await session.get(m.Hall, context.hall_id)
            if hall is not None:
                grounding = hall.description or hall.name or ""

    # История диалога.
    history: List[Tuple[str, str]] = []
    if req.history:
        history = [(msg.role, msg.content) for msg in req.history]
    else:
        rows = (
            await session.execute(
                select(m.GuideMessage).where(m.GuideMessage.session_id == sess.id).order_by(m.GuideMessage.id)
            )
        ).scalars().all()
        history = [(r.role, r.content) for r in rows]

    # Ответ + структурированные данные (B6/B7/B10). Retrieval из каталога (B1) —
    # через crud.search_exhibits_orm / exhibits_by_number / all_halls_ordered.
    referenced_exhibits: List[sch.ReferencedExhibit] = []
    referenced_halls: List[sch.ReferencedHall] = []
    location: Optional[sch.GuideLocation] = None
    questions: List[str] = []

    number = guide_intel.parse_exhibit_number(req.message)

    if number is not None:
        # B9 — поиск по номеру экспоната + уточняющий диалог для неуникальных номеров.
        matches = await crud.exhibits_by_number(session, number)
        if len(matches) == 1:
            target = matches[0]
            answer = _describe_exhibit(target)
            referenced_exhibits = [crud.to_referenced_exhibit(target)]
            location = crud.to_location(target)
            hall = target.showcase.hall if target.showcase else None
            context = sch.GuideContext(
                exhibit_id=target.id, label_slug=target.label_slug, hall_id=hall.id if hall else None
            )
        elif len(matches) > 1:
            answer = (
                f"Экспонат №{number} встречается в нескольких витринах. "
                "Уточните, пожалуйста, зал или витрину:"
            )
            referenced_exhibits = [crud.to_referenced_exhibit(e) for e in matches[:4]]
            questions = [_where_hint(e) for e in matches][:6]
        else:
            answer = (
                f"Экспонат с номером {number} я не нашёл. "
                "Проверьте номер или назовите экспонат словами."
            )
    elif guide_intel.is_hall_listing(req.message):
        # B10 — список залов структурой (детерминированно, без риска галлюцинаций).
        halls = await crud.all_halls_ordered(session)
        referenced_halls = [crud.to_referenced_hall(h) for h in halls]
        answer = _describe_halls(halls)
    else:
        # Обычный диалог: LLM-ответ + retrieval-обвязка (B6/B7).
        try:
            answer, questions = await llm.chat(
                grounding, history, req.message, req.language, req.max_questions, exhibit_dict
            )
        except UpstreamError as exc:
            raise HTTPException(status_code=502, detail=exc.message)
        # B6 — экспонаты, о которых речь: retrieval по вопросу + ответу.
        found = await crud.search_exhibits_orm(session, f"{req.message} {answer}", limit=4)
        referenced_exhibits = _merge_referenced(context_exhibit, found)
        # B7 — навигационный вопрос: location целевого экспоната.
        if guide_intel.is_navigational(req.message):
            target = context_exhibit or (found[0] if found else None)
            if target is not None:
                location = crud.to_location(target)
                if not referenced_exhibits:
                    referenced_exhibits = [crud.to_referenced_exhibit(target)]

    session.add(m.GuideMessage(session_id=sess.id, role="user", content=req.message))
    session.add(m.GuideMessage(session_id=sess.id, role="assistant", content=answer))
    sess.last_activity = datetime.now(timezone.utc)
    if context is not None:
        sess.context = context.model_dump()
    await session.commit()

    return sch.ChatResponse(
        session_id=sess.id,
        answer=answer,
        suggested_questions=questions,
        context=context,
        referenced_exhibits=referenced_exhibits,
        referenced_halls=referenced_halls,
        location=location,
    )
