"""FastAPI-приложение «ИИ-гид музея Фаберже» (PostgreSQL 17).

Запуск (локально):
    python3 -m pip install -r app/requirements.txt
    python scripts/init_db.py --seed        # применить схему + демо-данные
    uvicorn app.main:app --reload --port 8000

Документация:
    Swagger UI : http://localhost:8000/docs
    ReDoc      : http://localhost:8000/redoc
    OpenAPI    : http://localhost:8000/openapi.json
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import settings
from .routers import (
    admin,
    exhibits,
    guide,
    navigation,
    recognition,
    search,
    speech,
    system,
    telemetry,
)
from .services import UpstreamError

# ── Уровень логирования ──────────────────────────────────────────────────────
# Без этого INFO-строки приложения не видны в проде: рантайм Cloud Functions
# ставит на корневой логгер свой обработчик, а уровень остаётся WARNING —
# `logging.basicConfig` в таком случае молча ничего не делает (он уходит
# раньше, если обработчики уже есть). Проверено на проде 20.08.2026: строк
# `llm_usage`/`tts_request` в логах функции не было вовсе, хотя код их писал.
# Поэтому уровень выставляем явно, самому корневому логгеру.
_root_logger = logging.getLogger()
_root_logger.setLevel(getattr(logging, (settings.log_level or "INFO").upper(), logging.INFO))
if not _root_logger.handlers:
    logging.basicConfig(level=_root_logger.level)
# httpx на INFO печатает строку на каждый исходящий запрос («HTTP Request: POST
# … 200 OK») — дублирует наши собственные строки расхода, только без цифр.
logging.getLogger("httpx").setLevel(logging.WARNING)

DESCRIPTION = """
Backend мобильного web-приложения (PWA) **«ИИ-гид музея Фаберже»**.

Иерархия каталога: **зал → витрина → экспонат**. Ключевой сценарий —
сфотографировать экспонат (`/recognition` → YOLO), получить рассказ
(`/guide/story` → YandexGPT) с вопросами-подсказками и озвучку
(`/speech` → SpeechKit).

> Слой данных (навигация/каталог/поиск) — реальные запросы к PostgreSQL 17.
> Внешние сервисы (YOLO / YandexGPT / SpeechKit / Object Storage) работают в
> режиме-стабе, пока в окружении нет ключей Yandex Cloud (см. `.env.example`).
"""

tags_metadata = [
    {"name": "Система", "description": "Health-check и мониторинг."},
    {"name": "Карта и навигация", "description": "Карта музея и уровни зал → витрина → экспонат."},
    {"name": "Экспонаты", "description": "Карточка экспоната и «Другие экспонаты зала»."},
    {"name": "Поиск", "description": "Поиск по названиям залов и экспонатов."},
    {"name": "Распознавание", "description": "Приём фото и маршрутизация в YOLO."},
    {"name": "ИИ-гид", "description": "Рассказ (YandexGPT) и диалог с подсказками."},
    {"name": "Озвучивание", "description": "Синтез речи (SpeechKit)."},
    {"name": "Администрирование", "description": "[Вне MVP] CRUD и аналитика (Bearer)."},
    {"name": "Телеметрия", "description": "Приём событий для аналитики (обязательный контракт с 21.07.2026)."},
]

app = FastAPI(
    title="API ИИ-гида музея Фаберже",
    version=__version__,
    description=DESCRIPTION,
    openapi_tags=tags_metadata,
    contact={"name": "Команда ИИ-гида музея Фаберже", "url": "https://fabergemuseum.ru"},
    license_info={"name": "Proprietary — внутренний документ проекта"},
    servers=[
        {"url": "http://localhost:8000", "description": "Локальная разработка"},
        {"url": "https://{gatewayId}.apigw.yandexcloud.net", "description": "Production — Yandex API Gateway",
         "variables": {"gatewayId": {"default": "d5dexampleexample00"}}},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(UpstreamError)
async def _upstream_error_handler(request, exc: UpstreamError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": exc.message})


# Статика: локальное хранилище медиа (стаб Object Storage) + сгенерированное аудио.
os.makedirs(settings.media_dir, exist_ok=True)
app.mount("/media", StaticFiles(directory=settings.media_dir), name="media")

# Роутеры.
app.include_router(system.router)
app.include_router(navigation.router)
app.include_router(exhibits.router)
app.include_router(search.router)
app.include_router(recognition.router)
app.include_router(guide.router)
app.include_router(speech.router)
app.include_router(admin.auth_router)
app.include_router(admin.router)
app.include_router(telemetry.router)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")
