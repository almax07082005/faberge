"""Кэш вопросов-подсказок на экспонаты (просьба заказчика 26.08.2026).

Зачем
    Вопросы под рассказом («Кому подарили это яйцо?») считает отдельный вызов
    YandexGPT — в логах расхода это `operation=questions`. Он шёл ВТОРЫМ рядом с
    каждым рассказом (`POST /guide/story`) и рядом с каждой репликой диалога об
    экспонате (`POST /guide/chat`), то есть на один ход диалога приходилось два
    вызова LLM. При этом результат зависит ТОЛЬКО от карточки экспоната: ни от
    посетителя, ни от истории диалога, ни от заданного вопроса. Один экспонат за
    день открывают десятки раз — и каждый раз мы платили за один и тот же текст.

Как устроено
    Таблица `exhibit_questions`: (экспонат, язык) → пул вопросов. Ключ свежести
    — `source_hash`: sha256 языка и текста, который уходит в промпт
    (`llm.questions_source` — тот же самый, что и в генерации). Музей поправил
    описание — хэш разошёлся — запись перегенерируется при первом обращении.
    Ручной инвалидации нет и не нужно.

Чего здесь нет намеренно
    • TTL. «Протухание по времени» вернуло бы часть расхода за то, что и так не
      меняется: описания правят редко, а изменение ловится хэшем.
    • Кэша по (экспонат, max_questions). В записи лежит ПУЛ
      (`GUIDE_QUESTIONS_CACHE_SIZE`), наружу отдаётся срез: иначе диалог с его
      `max_questions=3` выбивал бы запись, сделанную рассказом для 4.
    • Кэша вопросов вне экспоната. В общем чате (без контекста экспоната)
      подсказок не было и раньше — кэшировать нечего.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Dict, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from .. import crud
from .. import models as m
from ..config import settings
from . import UpstreamError, llm

logger = logging.getLogger(__name__)


def fingerprint(exhibit: Dict, language: str = "ru") -> str:
    """Хэш текста, из которого генерируются вопросы (+ язык) — ключ свежести записи."""
    source = llm.questions_source(exhibit)
    return hashlib.sha256(f"{language}\n{source}".encode("utf-8")).hexdigest()


def _pool_size(max_questions: int) -> int:
    """Сколько вопросов просить у модели: пул из настроек, но не меньше запрошенного."""
    return max(max_questions, settings.guide_questions_cache_size)


def is_fresh(row: Optional[m.ExhibitQuestions], exhibit: Dict, language: str = "ru") -> bool:
    """Годится ли запись кэша: непустая и по тому же исходному тексту.

    Длину набора НЕ проверяем, и это важно: `max_questions` — потолок, а не
    требование. Модель регулярно отдаёт меньше, чем просили (а стаб на карточке
    без мастера — всего три вопроса), и правило «в записи должно лежать ровно
    столько, сколько попросили» означало бы вечный промах: каждый запрос заново
    платит за набор, который заведомо не вырастет. Если пул нужно расширить
    (подняли GUIDE_QUESTIONS_CACHE_SIZE) — это разовый `--force`, а не налог на
    каждое обращение.
    """
    if row is None or not row.questions:
        return False
    return row.source_hash == fingerprint(exhibit, language)


async def for_exhibit(
    session: AsyncSession,
    exhibit: Dict,
    max_questions: int,
    language: str = "ru",
    force: bool = False,
) -> List[str]:
    """Вопросы-подсказки по экспонату: из кэша, при промахе — LLM и запись в кэш.

    `exhibit` — словарь `crud.exhibit_to_dict`. Без `id` (или при выключенном
    кэше) работает как раньше: прямой вызов LLM без похода в БД.
    """
    if max_questions <= 0:
        return []
    exhibit_id = exhibit.get("id")
    if not settings.guide_questions_cache_enabled or exhibit_id is None:
        questions, _ = await llm.suggested_questions(exhibit, max_questions, language)
        return questions[:max_questions]

    row = await crud.get_exhibit_questions(session, exhibit_id, language)
    if not force and is_fresh(row, exhibit, language):
        return list(row.questions)[:max_questions]

    try:
        questions, model = await llm.suggested_questions(exhibit, _pool_size(max_questions), language)
    except UpstreamError:
        # Подсказки — не сам ответ гида: если модель сейчас недоступна, показать
        # прошлый (пусть и устаревший) набор честнее, чем уронить рассказ в 502.
        # Когда прошлого набора нет — ведём себя как до кэша и пробрасываем сбой.
        if row is not None and row.questions:
            logger.warning("guide_questions: LLM недоступен, отдаём устаревший кэш exhibit_id=%s", exhibit_id)
            return list(row.questions)[:max_questions]
        raise

    if questions:
        await crud.save_exhibit_questions(
            session, exhibit_id, language, questions, fingerprint(exhibit, language), model
        )
    return questions[:max_questions]


async def warm_exhibit(
    session: AsyncSession,
    ex: m.Exhibit,
    row: Optional[m.ExhibitQuestions],
    language: str = "ru",
    force: bool = False,
    dry_run: bool = False,
) -> Tuple[str, List[str]]:
    """Прогреть одну карточку. Возвращает (итог, вопросы).

    Итог: `cached` — запись уже свежая, LLM не звали; `generated` — сгенерировали
    и записали; `planned` — сухой прогон, генерации не было; `failed` — LLM
    отказал (прогрев продолжается со следующей карточки, см. скрипт).
    """
    exhibit = crud.exhibit_to_dict(ex)
    want = _pool_size(0)
    if not force and is_fresh(row, exhibit, language):
        return "cached", list(row.questions)
    if dry_run:
        return "planned", []
    try:
        questions, model = await llm.suggested_questions(exhibit, want, language)
    except UpstreamError as exc:
        logger.warning("guide_questions: прогрев exhibit_id=%s не удался: %s", ex.id, exc.message)
        return "failed", []
    if not questions:
        return "failed", []
    await crud.save_exhibit_questions(
        session, ex.id, language, questions, fingerprint(exhibit, language), model
    )
    return "generated", questions
