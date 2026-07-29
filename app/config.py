"""Конфигурация приложения (переменные окружения / .env)."""
from __future__ import annotations

import ssl
from functools import lru_cache
from typing import Any, Dict, List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    # ── База данных ──────────────────────────────────────────────────────────
    # Для Yandex Managed PostgreSQL:
    #   postgresql+asyncpg://<user>:<pwd>@<host>:6432/<db>
    #   + DB_SSL_ROOT_CERT=~/.postgresql/root.crt  (TLS обязателен)
    database_url: str = "postgresql+asyncpg://faberge:faberge@localhost:5432/faberge"
    db_ssl_root_cert: Optional[str] = None
    sql_echo: bool = False

    # ── Приложение ───────────────────────────────────────────────────────────
    public_base_url: str = "http://localhost:8000"
    cors_origins: str = "*"                       # список через запятую
    admin_api_token: str = "dev-admin-token"      # Bearer для /admin/**
    admin_username: str = "admin"                 # логин для POST /admin/login
    admin_password: str = "admin"                 # пароль для POST /admin/login (переопределите в проде!)
    media_dir: str = "media"                      # локальное хранилище (стаб Object Storage)
    max_upload_mb: int = 10

    # ── Распознавание по фото ────────────────────────────────────────────────
    recognition_confidence_threshold: float = 0.6
    recognition_timeout_sec: float = 25.0         # таймаут запроса к ML-сервису поиска
    # Порог нечёткой сшивки названия из ML-индекса с названием в каталоге
    # (0..1, difflib.SequenceMatcher). Ниже — предсказание отбрасывается и
    # логируется. 0 отключает нечёткое сопоставление (только точное).
    recognition_name_match_cutoff: float = 0.86

    # ── Озвучивание ──────────────────────────────────────────────────────────
    # Прогонять произвольный текст через LLM, чтобы числа звучали порядковыми
    # («Пётр Первый», а не «Пётр один»). Выключение оставляет детерминированную
    # нормализацию (app/services/text_normalize.py) — дешевле, но грубее.
    tts_spoken_via_llm: bool = True

    # ── Yandex Cloud (опционально; без ключей сервисы работают в режиме-стабе) ─
    yandex_api_key: Optional[str] = None
    yandex_folder_id: Optional[str] = None
    yandexgpt_model_uri: Optional[str] = None     # gpt://<folder>/yandexgpt/latest
    yolo_endpoint: Optional[str] = None           # HTTP-эндпоинт развёрнутой YOLO
    speechkit_api_key: Optional[str] = None
    object_storage_bucket: Optional[str] = None
    object_storage_endpoint: str = "https://storage.yandexcloud.net"
    object_storage_public_base: Optional[str] = None  # CDN-домен раздачи

    # ── Производные значения ─────────────────────────────────────────────────
    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def ssl_context(self) -> Optional[ssl.SSLContext]:
        if not self.db_ssl_root_cert:
            return None
        ctx = ssl.create_default_context(cafile=self.db_ssl_root_cert)
        return ctx

    def db_connect_args(self) -> Dict[str, Any]:
        ctx = self.ssl_context()
        return {"ssl": ctx} if ctx is not None else {}

    # Флаги «настроен ли внешний сервис» — для health-check и выбора реализации.
    @property
    def llm_configured(self) -> bool:
        return bool(self.yandex_api_key and (self.yandexgpt_model_uri or self.yandex_folder_id))

    @property
    def tts_configured(self) -> bool:
        return bool(self.speechkit_api_key or self.yandex_api_key)

    @property
    def yolo_configured(self) -> bool:
        return bool(self.yolo_endpoint)

    @property
    def storage_configured(self) -> bool:
        return bool(self.object_storage_bucket)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
