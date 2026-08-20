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
from ..config import settings
from ..db import get_session
from ..services import UpstreamError, guide_intel, llm, tts

router = APIRouter(prefix="/guide", tags=["ИИ-гид"])


# ── Вспомогательные (детерминированные ответы гида по каталогу) ───────────────
def _hall_phrase(hall: m.Hall, case: str = "nom") -> str:
    """«зал 3 «Синяя гостиная»» / «зале 3 «Синяя гостиная»» (для «в зале …»).

    У залов без номера («Вне постоянной экспозиции») `hall_number` пуст — «зал
    None» посетителю показывать нельзя (баг-репорт 28.07.2026, п.5).
    """
    word = "зале" if case == "prep" else "зал"
    name = f" «{hall.name}»" if hall.name else ""
    if hall.hall_number is None:
        return f"{word}{name}" if name else word
    return f"{word} {hall.hall_number}{name}"


def _location_phrase(ex: m.Exhibit) -> str:
    """«в зале 3 «Синяя гостиная», витрина 2» — из привязки экспоната."""
    if ex.showcase is None:
        return ""
    hall = ex.showcase.hall
    parts = []
    if hall is not None:
        parts.append("в " + _hall_phrase(hall, case="prep"))
    # showcase_number = NULL — группа «не в витринах» (пустой квадрат в путеводителе).
    parts.append(
        f"витрина {ex.showcase.showcase_number}" if ex.showcase.showcase_number is not None
        else "вне витрин"
    )
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


def _plural_halls(n: int) -> str:
    """«1 зал», «2 зала», «10 залов» — иначе гид отвечает «В музее 1 залов»."""
    if 11 <= n % 100 <= 14:
        return "залов"
    return {1: "зал", 2: "зала", 3: "зала", 4: "зала"}.get(n % 10, "залов")


def _describe_halls(halls: List[m.Hall]) -> str:
    """Детерминированный ответ со списком залов (B10).

    `halls` приходит из crud.all_halls_ordered — служебные записи (Парадная
    лестница) туда уже не попадают.

    Считаем ТОЛЬКО пронумерованные залы: «Вне постоянной экспозиции» — не зал
    экспозиции, а группа для предметов вне неё, у него по требованию заказчика
    нет номера (баг-репорт 28.07.2026, п.5). Он есть в списке, но не в счётчике.
    """
    if not halls:
        return "Пока в каталоге нет залов."
    numbered = [h for h in halls if h.hall_number is not None]
    unnumbered = [h for h in halls if h.hall_number is None]
    main = [h for h in numbered if not h.is_temporary]
    temp = [h for h in numbered if h.is_temporary]

    def _fmt(items: List[m.Hall]) -> str:
        return "; ".join(_hall_phrase(h) for h in items)

    lines = [f"В музее {len(numbered)} {_plural_halls(len(numbered))}."]
    if main:
        lines.append(f"Основная экспозиция: {_fmt(main)}.")
    if temp:
        lines.append(f"Временные выставки: {_fmt(temp)}.")
    if unnumbered:
        lines.append("Кроме того: " + "; ".join(h.name or "без названия" for h in unnumbered) + ".")
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


def _add_messages(
    session: AsyncSession,
    sess: m.GuideSession,
    question: str,
    answer: str,
    context: Optional[sch.GuideContext],
    answered: bool,
    fail_reason: Optional[str],
) -> None:
    """Положить в сессию пару «вопрос — ответ» с признаком ответа (§4).

    Признак и контекст пишутся на ОБЕ строки: отчёт `/admin/analytics/unanswered`
    читает реплики посетителя (`role='user'`) и не должен ради этого джойнить
    таблицу саму с собой.
    """
    common = dict(
        answered=answered,
        fail_reason=fail_reason,
        exhibit_id=context.exhibit_id if context else None,
        hall_id=context.hall_id if context else None,
    )
    session.add(m.GuideMessage(session_id=sess.id, role="user", content=question, **common))
    session.add(m.GuideMessage(session_id=sess.id, role="assistant", content=answer, **common))


async def _persist_messages(
    session: AsyncSession,
    sess: m.GuideSession,
    question: str,
    answer: str,
    context: Optional[sch.GuideContext],
    answered: bool,
    fail_reason: Optional[str],
) -> None:
    """То же, но с немедленной фиксацией — для ветки, которая дальше бросает 502."""
    _add_messages(session, sess, question, answer, context, answered, fail_reason)
    sess.last_activity = datetime.now(timezone.utc)
    await session.commit()


def _is_blank_context(context: Optional[sch.GuideContext]) -> bool:
    """Пустой контекст: `null` или объект, у которого не заполнено ни одно поле (`{}`)."""
    return context is None or all(v is None for v in context.model_dump().values())


@router.post(
    "/chat", response_model=sch.ChatResponse, summary="Диалог с ИИ-гидом",
    description=(
        "Диалог с гидом. Контекст (`context`) управляется явно:\n\n"
        "* поле `context` **не передано** — бэкенд подставит контекст, сохранённый "
        "в сессии (продолжение разговора об экспонате/зале);\n"
        "* `\"context\": {}` или `\"context\": null` — **сброс**: контекст сессии "
        "очищается, вопрос трактуется как общий (вход в общий чат с главного экрана);\n"
        "* `\"reset_context\": true` — тот же сброс, но без передачи самого поля;\n"
        "* заполненный `context` — заменяет контекст сессии целиком.\n\n"
        "Контекст зала — подсказка, а не рамка: если ответа в нём нет, гид отвечает "
        "по общим знаниям о музее, а не «в предоставленных материалах нет информации»."
    ),
)
async def chat(req: sch.ChatRequest, session: AsyncSession = Depends(get_session)) -> sch.ChatResponse:
    # Сессия диалога.
    sess: Optional[m.GuideSession] = None
    if req.session_id is not None:
        sess = await session.get(m.GuideSession, req.session_id)

    # Раньше `context=None` означало одновременно «не передавали» и «сбросьте», и
    # бэкенд всегда воскрешал сохранённый контекст. Из-за этого зал «залипал»:
    # посетитель выходил из Рыцарского зала в общий чат, а гид продолжал отвечать
    # «в материалах о Рыцарском зале нет…» (баг-репорт 28.07.2026, п.3). Теперь
    # «не передавали» отличается от «сбросьте» по model_fields_set.
    context_sent = "context" in req.model_fields_set
    reset_context = req.reset_context or (context_sent and _is_blank_context(req.context))
    context = None if reset_context else req.context
    if sess is None:
        sess = m.GuideSession(context=context.model_dump() if context else None)
        session.add(sess)
        await session.flush()
    elif reset_context:
        sess.context = None
    elif context is None and not context_sent and sess.context:
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
            # hall_id берём из ПРИСЛАННОГО контекста, не из прежнего состояния
            # сессии: при переходе к экспонату другого зала старый зал не должен
            # оставаться приклеенным (баг-репорт 28.07.2026, п.3).
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
        # Из БД поднимаем ровно столько последних реплик, сколько уйдёт в промпт
        # (GUIDE_HISTORY_TURNS): в модель всё равно попадает только хвост, а
        # тянуть весь диалог сессии — лишний трафик БД на каждом вопросе.
        turns = max(0, settings.guide_history_turns)
        rows = (
            await session.execute(
                select(m.GuideMessage)
                .where(m.GuideMessage.session_id == sess.id)
                .order_by(m.GuideMessage.id.desc())
                .limit(turns)
            )
        ).scalars().all()
        history = [(r.role, r.content) for r in reversed(rows)]

    # Ответ + структурированные данные (B6/B7/B10). Retrieval из каталога (B1) —
    # через crud.search_exhibits_orm / exhibits_by_number / all_halls_ordered.
    referenced_exhibits: List[sch.ReferencedExhibit] = []
    referenced_halls: List[sch.ReferencedHall] = []
    location: Optional[sch.GuideLocation] = None
    questions: List[str] = []

    # §4 — «смог ли гид ответить». Признак ставится ЗДЕСЬ, в момент генерации:
    # постфактум отличить содержательный ответ от вежливого отказа («не могу
    # предоставить полный список») по тексту в БД невозможно. По умолчанию ответ
    # считается содержательным — детерминированные ветки ниже его не меняют.
    answered = True
    fail_reason: Optional[str] = None

    # B9 — поиск по номеру. Реплика-номер неоднозначна (это может быть год «1885»
    # или количество), поэтому ветку берём ТОЛЬКО при реальном совпадении по номеру;
    # иначе (0 совпадений) проваливаемся в обычный диалог, а не в тупик «не нашёл».
    number = guide_intel.parse_exhibit_number(req.message)
    number_matches = await crud.exhibits_by_number(session, number) if number is not None else []

    if number_matches:
        if len(number_matches) == 1:
            target = number_matches[0]
            answer = _describe_exhibit(target)
            referenced_exhibits = [crud.to_referenced_exhibit(target)]
            location = crud.to_location(target)
            hall = target.showcase.hall if target.showcase else None
            context = sch.GuideContext(
                exhibit_id=target.id, label_slug=target.label_slug, hall_id=hall.id if hall else None
            )
        else:
            # Неуникальный номер — уточняющий диалог (B9).
            answer = (
                f"Экспонат №{number} встречается в нескольких витринах. "
                "Уточните, пожалуйста, зал или витрину:"
            )
            referenced_exhibits = [crud.to_referenced_exhibit(e) for e in number_matches[:4]]
            questions = [_where_hint(e) for e in number_matches][:6]
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
            # Сбой LLM — тоже «вопрос без ответа»: записываем реплику с причиной
            # `error` до того, как отдать 502, иначе такие вопросы не попадут ни
            # в один отчёт и провал останется невидимым.
            await _persist_messages(
                session, sess, req.message, exc.message, context, answered=False, fail_reason="error"
            )
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
            else:
                # Спросили «где найти», а экспонат в каталоге не нашёлся.
                answered, fail_reason = False, "not_found"
        if answered and guide_intel.is_refusal(answer):
            # Отказ без справки — не хватило контекста; со справкой — модель
            # отказалась при наличии материалов.
            answered = False
            fail_reason = "no_context" if not grounding.strip() else "llm_refusal"

    _add_messages(session, sess, req.message, answer, context, answered, fail_reason)
    sess.last_activity = datetime.now(timezone.utc)
    if context is not None:
        sess.context = context.model_dump()
    elif reset_context:
        sess.context = None  # сброс переживает и запись сессии, а не только этот ответ
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
