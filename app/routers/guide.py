"""ИИ-гид: генерация рассказа (YandexGPT) и диалог с подсказками."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import crud
from .. import models as m
from .. import schemas as sch
from ..config import settings
from ..db import get_session
# `location_text` — общая формулировка расположения (её же печатает карточка
# предмета). Псевдоним не косметический: в `chat()` есть ЛОКАЛЬНАЯ переменная
# `location` (поле ответа гида), и под родным именем модуль оказался бы затенён
# во всей функции целиком — обращение к нему падало бы `UnboundLocalError` на
# каждом запросе `/guide/chat`, в том числе ДО строки её присваивания.
from ..services import UpstreamError, guide_intel, guide_mentions, guide_questions, llm, tts
from ..services import location as location_text

router = APIRouter(prefix="/guide", tags=["ИИ-гид"])

logger = logging.getLogger(__name__)

# Причины «вопрос остался без ответа», которые считаются ОТКАЗОМ гида, то есть
# поводом больше не предлагать эту формулировку (баг-репорт 31.08.2026, п. II-3).
# `error` (сбой LLM, ветка ниже пишет его перед 502) и `not_found` (навигация не
# нашла предмет) сюда не входят намеренно: это провал инфраструктуры и поиска, а
# не отсутствие ответа, и прятать из-за него нормальный вопрос нельзя. По той же
# логике не входит `llm_hedge` — содержательный ответ с оговоркой «этого точно не
# знаю» (см. ниже, где причина выбирается). Кортеж намеренно совпадает с выборкой
# `crud.exhibit_refused_questions`: сессионная и глобальная память об отказах
# должны прятать ОДНО И ТО ЖЕ, иначе они разъедутся по строгости.
_REFUSAL_REASONS = ("llm_refusal", "no_context")


# ── Вспомогательные (детерминированные ответы гида по каталогу) ───────────────
def _hall_phrase(hall: m.Hall, case: str = "nom") -> str:
    """«зал 4 «Синяя гостиная»» / «зале 4 «Синяя гостиная»» (для «в зале …»).

    У залов без номера («Вне постоянной экспозиции») `hall_number` пуст — «зал
    None» посетителю показывать нельзя (баг-репорт 28.07.2026, п.5).

    Тонкая обёртка над ORM-объектом: сама формулировка живёт в
    `app/services/location.py` — её же печатает карточка предмета, и разъезжаться
    этим двум фразам нельзя (баг-репорт 31.08.2026, п. I-2).
    """
    return location_text.hall_phrase(hall.hall_number, hall.name, case)


def _location_phrase(ex: m.Exhibit) -> str:
    """«в зале 4 «Синяя гостиная», витрина 5» — из привязки экспоната.

    Текст фразы — в `app/services/location.py` (общий с карточкой предмета);
    здесь остаётся только то, чего чистый модуль знать не может: привязан ли
    предмет к витрине и к залу вообще.
    """
    if ex.showcase is None:
        return ""
    hall = ex.showcase.hall
    return location_text.location_phrase(
        hall.hall_number if hall is not None else None,
        hall.name if hall is not None else None,
        # showcase_number = NULL — группа «не в витринах» (пустой квадрат в путеводителе).
        ex.showcase.showcase_number,
        case="prep",
        # Витрина-сирота (зала нет) начинает фразу сразу с витрины. Флагом, а не
        # выводом из полей: у зала без номера и без названия прежняя версия всё
        # равно печатала «в зале, …», и поведение мы здесь не меняем.
        has_hall=hall is not None,
    )


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

    `halls` приходит из crud.all_halls_ordered — служебные записи каталога туда
    уже не попадают.

    Считаем ТОЛЬКО пронумерованные залы: «Вне постоянной экспозиции» — не зал
    экспозиции, а группа для предметов вне неё, у него по требованию заказчика
    нет номера (баг-репорт 28.07.2026, п.5). Он есть в списке, но не в счётчике.

    С 31.08.2026 счётчик даёт 12, а не 11: музей отменил решение прятать зал №1
    «Парадная лестница» (п. I-1), и она считается наравне с остальными — ровно
    столько залов и в путеводителе музея. Это НЕ баг и не регресс: побочный эффект
    «гид говорит 11 залов» был зафиксирован в docs/staircase-hall-decision.md как
    следствие старого решения и снялся вместе с ним. Кода здесь править не
    пришлось — цифру задаёт состав выдачи, а не константа.
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


def _merge_referenced(
    context_exhibit: Optional[m.Exhibit],
    found: List[m.Exhibit],
    sources: Optional[Sequence[str]] = None,
) -> List[sch.ReferencedExhibit]:
    """Плашки экспонатов (B6): контекстный экспонат первым, затем найденные; без дублей, максимум 4.

    Контекстный экспонат (тот, у которого стоит посетитель) в блоке ВСЕГДА: он
    не проходит проверку упоминания и не может быть из блока вытеснен. Посетитель
    смотрит именно на него, и убирать плашку из-за того, что гид не назвал предмет
    по имени, нельзя. Проверка `guide_mentions` применяется ТОЛЬКО к `found` —
    инвариант держится тем, что контекст подмешивается уже ПОСЛЕ фильтрации, и без
    этой строчки следующая правка сломает его молча.

    `sources` — необязательный параллельный `found` список пометок «откуда взялась
    плашка» (`answer`/`question`) для `ReferencedExhibit.mentioned_in`; контекстный
    экспонат помечается `context`.
    """
    out: List[sch.ReferencedExhibit] = []
    seen: set[int] = set()
    pairs: List[Tuple[m.Exhibit, Optional[str]]] = []
    if context_exhibit is not None:
        pairs.append((context_exhibit, "context"))
    for i, e in enumerate(found):
        pairs.append((e, sources[i] if sources is not None and i < len(sources) else None))
    for e, source in pairs:
        if e.id in seen:
            continue
        seen.add(e.id)
        plaque = crud.to_referenced_exhibit(e)
        plaque.mentioned_in = source
        out.append(plaque)
        if len(out) >= 4:
            break
    return out


# ── Память диалога для блока подсказок (пп. II-2/II-3) ────────────────────────
# Память сессии — пары (формулировка, у какого экспоната её задали). `None` во
# втором элементе означает «вопрос не про предмет» (общий чат) либо «текущая
# реплика» — такие исключаются в любой ветке.
SessionMemory = List[Tuple[str, Optional[int]]]


async def _session_memory(
    session: AsyncSession, session_id: Optional[uuid.UUID], current_message: str = ""
) -> Tuple[SessionMemory, SessionMemory]:
    """(что уже спрашивали в этой сессии, на что гид уже отказался отвечать).

    Обе половины — из `guide_messages`, отдельным запросом, а НЕ из истории для
    промпта: та поднимается хвостом в `GUIDE_HISTORY_TURNS` реплик (по
    умолчанию 3), и на её основе из блока исчезали бы только три последних
    вопроса, а четвёртый по счёту предлагался бы заново.

    `current_message` — реплика, которую обрабатываем прямо сейчас. Её в БД ещё
    нет (`_add_messages` вызывается в самом конце), а именно она и есть жалоба
    п. II-2 со скриншота: посетитель спросил про скифские мотивы, получил ответ —
    и следующей подсказкой ему предлагают тот же вопрос другими словами.
    """
    current = (current_message or "").strip()
    asked: SessionMemory = [(current, None)] if current else []
    refused: SessionMemory = []
    if session_id is None:
        return asked, refused
    for content, answered, fail_reason, exhibit_id in await crud.session_asked_questions(session, session_id):
        text = (content or "").strip()
        if not text:
            continue
        asked.append((text, exhibit_id))
        if answered is False and fail_reason in _REFUSAL_REASONS:
            refused.append((text, exhibit_id))
    return asked, refused


def _scoped(memory: SessionMemory, exhibit_id: Optional[int] = None) -> List[str]:
    """Формулировки, относящиеся к этому предмету (и не привязанные ни к какому).

    Вопрос, заданный у соседней витрины, здесь исключать нельзя: у пасхальных
    яиц пулы подсказок совпадают дословно, и посетитель, идущий по залу, к
    третьему предмету остался бы без хороших подсказок.
    """
    return [text for text, ex_id in memory if ex_id is None or ex_id == exhibit_id]


async def _exhibit_refusals(session: AsyncSession, exhibit_id: Optional[int]) -> List[str]:
    """Вопросы, на которые гид уже отказался отвечать по ЭТОМУ экспонату — у всех.

    Память глобальная (решение Д8): музей описал системный сбой алгоритма, а не
    невезение одного посетителя, — значит вопрос, на который ответа нет, не
    должен предлагаться никому. Порог и срок давности — в настройках
    (`GUIDE_REFUSAL_MEMORY_MIN_COUNT`, `GUIDE_REFUSAL_MEMORY_DAYS`), выключается
    целиком `GUIDE_REFUSAL_MEMORY_ENABLED=false`.
    """
    if not settings.guide_refusal_memory_enabled or exhibit_id is None:
        return []
    return await crud.exhibit_refused_questions(
        session,
        exhibit_id,
        min_count=settings.guide_refusal_memory_min_count,
        days=settings.guide_refusal_memory_days,
    )


async def _exhibit_suggestions(
    session: AsyncSession,
    exhibit_dict: dict,
    max_questions: int,
    language: str,
    asked: SessionMemory = (),
    refused: SessionMemory = (),
) -> List[str]:
    """Подсказки по экспонату так, чтобы блок НЕ мог оказаться пустым (п. II-7).

    Память сессии сужается до этого предмета (`_scoped`) и дополняется
    глобальной памятью об отказах по нему же (решение Д8).

    Сбой генерации подсказок не должен ронять уже полученный ответ гида — это
    поведение было и раньше. Новое здесь то, что вместо пустого списка при сбое
    отдаётся детерминированный запас по карточке: `except UpstreamError:
    questions = []` был шестым, тихим сценарием исчезновения блока.
    """
    exhibit_id = exhibit_dict.get("id")
    asked_here = _scoped(asked, exhibit_id)
    refused_here = _scoped(refused, exhibit_id) + await _exhibit_refusals(session, exhibit_id)
    try:
        return await guide_questions.for_exhibit(
            session, exhibit_dict, max_questions, language, asked=asked_here, refused=refused_here
        )
    except UpstreamError:
        return guide_questions.select_questions(
            [], max_questions, exhibit=exhibit_dict, asked=asked_here, refused=refused_here
        )


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

    exhibit_dict = crud.exhibit_to_dict(ex)
    # Дословно из п. II-7: «надо возвращаться назад» — то есть на экран рассказа,
    # где подсказки показываются всегда. Если клиент передал session_id, здесь
    # действуют те же исключения, что и в диалоге: иначе, вернувшись, посетитель
    # увидит в блоке ровно тот вопрос, который только что задал и на который гид
    # уже отказался отвечать.
    asked, refused = await _session_memory(session, req.session_id)
    try:
        text, model = await llm.generate_story(exhibit_dict, req.style.value, req.language)
        # Вопросы-подсказки — из кэша (таблица exhibit_questions): они зависят
        # только от карточки, а раньше считались вторым вызовом LLM на каждый
        # рассказ (просьба заказчика 26.08.2026).
        questions = await _exhibit_suggestions(
            session, exhibit_dict, req.max_questions, req.language, asked, refused
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
        "по общим знаниям о музее, а не «в предоставленных материалах нет информации».\n\n"
        "Блок `suggested_questions` при `max_questions > 0` непустой в любой ветке: "
        "он не повторяет вопросы, уже заданные в этой сессии, и вопросы, на которые "
        "гид отказался отвечать по этому экспонату, а если после исключений пул "
        "кончился — подставляется детерминированный запас. Отдельный случай — "
        "уточнение по неуникальному номеру: там в поле лежат варианты «В зале 4 "
        "«Синяя гостиная», витрина 5», а не вопросы."
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
    # Диагностика к п. II-7 (решение Д7). Само поведение сброса НЕ меняем — оно
    # и есть фикс 28.07.2026, п.3, и вернуть «залипший» зал нельзя. Но самая
    # вероятная практическая причина жалобы «после ответа подсказки пропадают»
    # именно здесь: клиент сериализует тело целиком и всегда шлёт
    # `"context": null`, что по контракту означает ЯВНЫЙ СБРОС — со второй
    # реплики контекста экспоната нет, а значит нет и подсказок по нему. Отличить
    # это от нормального выхода в общий чат по коду невозможно, зато видно по
    # данным: контекст экспоната БЫЛ и пришёл пустой. Строка греппается в логах
    # функции; без неё мы не сможем НАЗВАТЬ музею причину.
    if reset_context and sess is not None and (sess.context or {}).get("exhibit_id") is not None:
        logger.warning(
            "guide_context_reset session_id=%s previous_exhibit_id=%s previous_hall_id=%s "
            "context_field_sent=%s reset_flag=%s",
            req.session_id,
            (sess.context or {}).get("exhibit_id"),
            (sess.context or {}).get("hall_id"),
            context_sent,
            req.reset_context,
        )
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
    context_hall: Optional[m.Hall] = None        # зал из контекста — нужен блоку подсказок (п. II-7)
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
                context_hall = hall
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

    # Память диалога для блока подсказок (пп. II-2/II-3). Отдельным запросом, а не
    # из `history` выше: там ровно GUIDE_HISTORY_TURNS последних реплик — сколько
    # уходит в промпт, — а исключать надо всё, что посетитель уже спрашивал.
    asked, refused = await _session_memory(session, sess.id, req.message)

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
            # П. II-7, сценарий 1: посетитель ввёл номер, получил описание
            # предмета — и оставался без единой подсказки, хотя контекст
            # экспоната здесь уже известен и кэш подсказок по нему, скорее
            # всего, прогрет.
            questions = await _exhibit_suggestions(
                session, crud.exhibit_to_dict(target), req.max_questions, req.language, asked, refused
            )
        else:
            # Неуникальный номер — уточняющий диалог (B9).
            answer = (
                f"Экспонат №{number} встречается в нескольких витринах. "
                "Уточните, пожалуйста, зал или витрину:"
            )
            referenced_exhibits = [crud.to_referenced_exhibit(e) for e in number_matches[:4]]
            # Единственное место, где в `suggested_questions` лежат НЕ вопросы, а
            # варианты уточнения («В зале 4 «Синяя гостиная», витрина 5»). Ни
            # фильтр запрещённых тем, ни дедупликация, ни исключения сюда не
            # применяются: это контракт уточняющего диалога B9, а не подсказки.
            questions = [_where_hint(e) for e in number_matches][:6]
    elif guide_intel.is_hall_listing(req.message):
        # B10 — список залов структурой (детерминированно, без риска галлюцинаций).
        halls = await crud.all_halls_ordered(session)
        referenced_halls = [crud.to_referenced_hall(h) for h in halls]
        answer = _describe_halls(halls)
        # П. II-7, сценарий 2: «какие есть залы» — ответ приходил вообще без
        # продолжения. Набор строится из уже загруженных залов, без LLM и без
        # кэша: платить за подсказки к списку залов не за что.
        questions = guide_questions.halls_overview_questions(
            [(h.hall_number, h.name) for h in halls], req.max_questions, exclude=_scoped(asked)
        )
    else:
        # Обычный диалог: LLM-ответ + retrieval-обвязка (B6/B7).
        try:
            answer = await llm.chat(grounding, history, req.message, req.language)
        except UpstreamError as exc:
            # Сбой LLM — тоже «вопрос без ответа»: записываем реплику с причиной
            # `error` до того, как отдать 502, иначе такие вопросы не попадут ни
            # в один отчёт и провал останется невидимым.
            await _persist_messages(
                session, sess, req.message, exc.message, context, answered=False, fail_reason="error"
            )
            raise HTTPException(status_code=502, detail=exc.message)
        # Подсказки к ответу. По экспонату — из кэша (26.08.2026); вне экспоната
        # их раньше не было вовсе, и это ровно то, на что жалуется п. II-7
        # («после ответа варианты вопросов уже не предлагаются»). Сценарии 3 и 4:
        # контекст только зала и общий чат без контекста получают
        # детерминированные наборы — без LLM, без кэша и без риска отказа.
        if exhibit_dict is not None:
            questions = await _exhibit_suggestions(
                session, exhibit_dict, req.max_questions, req.language, asked, refused
            )
        elif context_hall is not None:
            questions = guide_questions.hall_questions(req.max_questions, exclude=_scoped(asked))
        else:
            questions = guide_questions.museum_questions(req.max_questions, exclude=_scoped(asked))
        # B6 — экспонаты, о которых речь. Кандидатов берём с ЗАПАСОМ (окно, а не
        # четвёрка): полнотекстовый поиск ранжирует по ts_rank, а вес D у
        # raw_history поднимает длинные истории выше карточки, которую гид назвал
        # по имени, — упомянутый предмет в топ-4 просто не доезжал.
        found = await crud.search_exhibits_orm(
            session, f"{req.message} {answer}", limit=settings.guide_referenced_candidates
        )
        sources: Optional[List[str]] = None
        if settings.guide_referenced_require_mention:
            # Порога у полнотекстового поиска нет: на любой реплике он вернёт
            # столько строк, сколько попросили, и блок «Упомянуто в ответе»
            # добивался до четырёх предметами, которых в ответе нет (баг-репорт
            # 31.08.2026, п. II-1). Оставляем только те карточки, чьё название
            # (или номер в форме «№12») реально встречается в тексте.
            mentions = guide_mentions.mentioned_in_dialogue(
                answer, req.message, [(e.name, e.exhibit_number) for e in found]
            )
            found = [found[mention.index] for mention in mentions]
            sources = [mention.source for mention in mentions]
        referenced_exhibits = _merge_referenced(context_exhibit, found, sources)
        # B7 — навигационный вопрос: location целевого экспоната. `found` здесь
        # уже проверенный, поэтому «как пройти» больше не приводит посетителя к
        # витрине случайного экспоната; если не подтвердился никто, ветка честно
        # ставит answered=False/not_found — и метрика «вопросы без ответа» в
        # /admin/analytics/unanswered на релизе подрастёт скачком.
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
            # `answered=False` ставится по ШИРОКОМУ признаку: отчёт
            # /admin/analytics/unanswered показывает, где не хватает описаний, и
            # лишняя строка в нём стоит дёшево — её увидит сотрудник музея.
            #
            # А вот ПРИЧИНА разделена, и это не косметика. `llm_refusal` и
            # `no_context` — единственные две причины, которые попадают в выборку
            # `crud.exhibit_refused_questions`, то есть в ГЛОБАЛЬНУЮ память
            # отказов (решение Д8): такой вопрос перестаёт предлагаться ВСЕМ
            # посетителям этого экспоната на GUIDE_REFUSAL_MEMORY_DAYS дней.
            # Кормить эту память широким признаком нельзя: он ловит маркер где
            # угодно в ответе, а промпт диалога сам просит модель писать «этого
            # точно не знаю» одной фразой посреди содержательного ответа — мы
            # прятали бы нормальный вопрос за то поведение, которого добиваемся.
            #
            # Поэтому мягкая оговорка получает СВОЮ причину `llm_hedge`: в отчёт
            # она попадает (видно, что ответ был неполным), в память подсказок —
            # нет. Строгий предикат — guide_intel.is_hard_refusal.
            answered = False
            if guide_intel.is_hard_refusal(answer):
                # Отказ без справки — не хватило контекста; со справкой — модель
                # отказалась при наличии материалов.
                fail_reason = "no_context" if not grounding.strip() else "llm_refusal"
            else:
                fail_reason = "llm_hedge"

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
