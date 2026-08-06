"""Служебные эндпоинты (health-check)."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from .. import schemas as sch
from ..config import settings
from ..db import get_session

router = APIRouter(tags=["Система"])


@router.get("/health", response_model=sch.HealthStatus, summary="Health-check")
async def health(response: Response, session: AsyncSession = Depends(get_session)) -> sch.HealthStatus:
    deps = {}
    overall = "ok"
    try:
        await session.execute(text("SELECT 1"))
        deps["postgres"] = "up"
    except Exception:  # noqa: BLE001
        deps["postgres"] = "down"
        overall = "degraded"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    deps["object_storage"] = "up" if settings.storage_configured else "down"
    deps["yolo"] = "up" if settings.yolo_configured else "down"
    deps["yandexgpt"] = "up" if settings.llm_configured else "down"
    deps["speechkit"] = "up" if settings.tts_configured else "down"

    # Шрифт с кириллицей для PDF-выгрузки аналитики (п. 3 баг-репорта 06.08.2026:
    # «PDF не формируется»). Без TTF ручка /admin/analytics/export?format=pdf честно
    # отдаёт 503 — но заказчик узнавал об этом, только нажав кнопку. Пусть отсутствие
    # шрифта после деплоя видно сразу, до первой выгрузки.
    #
    # Импорт ЛЕНИВЫЙ, внутри функции: analytics_export специально не тянут на холодном
    # старте функции Yandex Cloud (см. его docstring), и health не должен это ломать.
    # Отсутствие шрифта — не отказ сервиса: overall остаётся "ok", 503 не отдаём.
    try:
        from ..services import analytics_export

        deps["pdf_font"] = "up" if analytics_export.font_available() else "down"
    except Exception:  # noqa: BLE001
        deps["pdf_font"] = "down"

    return sch.HealthStatus(
        status=overall, version=__version__, time=datetime.now(timezone.utc), dependencies=deps
    )
