"""Конфигурация приложения (переменные окружения / .env)."""
from __future__ import annotations

import logging
import math
import ssl
from functools import lru_cache
from typing import Any, Dict, List, Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


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

    # ── Загрузка файлов ──────────────────────────────────────────────────────
    # П.10 баг-репорта 06.08.2026. Приложение обещало 10 МБ, но Yandex API Gateway
    # рубит запрос раньше:
    #   413 {"errorCode":413,"errorMessage":"request entity is larger than limits (3670016)",
    #        "errorType":"ProxyIntegrationError"}
    # и — что хуже — отвечает БЕЗ CORS-заголовков, поэтому в браузере это не 413,
    # а «Failed to fetch»: фронт не может сказать «фото слишком большое». Вывод:
    # наш собственный лимит обязан быть заведомо НИЖЕ платформенного — тогда 413
    # отдаёт FastAPI (с CORS) и посетитель видит внятный текст.
    #
    # 3 670 016 Б = 3.5 МиБ — НЕ наш выбор, а потолок платформы (столько же у
    # синхронного вызова Cloud Functions). Кодом он не поднимается: только правкой
    # конфигурации API Gateway / квот Yandex Cloud.
    gateway_max_request_bytes: int = 3_670_016
    # Лимит гейтвея меряется по ВСЕМУ запросу, а не по файлу: сюда входят
    # multipart-обвязка (boundary, Content-Disposition, поля hall_id/top_k) и
    # заголовки. Фактически это ~500 Б, но имя файла и лишние поля с фронта
    # непредсказуемы, а 64 КиБ запаса нам ничего не стоят.
    upload_request_overhead_bytes: int = 65_536
    # Откуда взялось 2.5 МБ. На пути файла ДВЕ стены, и обе упираются в одно и то
    # же число 3 670 016 Б:
    #   1) сам API Gateway — 413 «request entity is larger than limits (3670016)»;
    #   2) синхронный вызов Cloud Functions — столько же на payload вызова, а тело
    #      уезжает в функцию как base64 внутри JSON (index.py: isBase64Encoded →
    #      base64.b64decode), то есть +33% к размеру файла на этом участке.
    # Вторая стена ниже первой и обойти её нельзя, поэтому считаем по ней:
    #   3 670 016 × 3/4 = 2 752 512 Б   (base64 — 4 байта на каждые 3 исходных)
    #   2 752 512 − 65 536 = 2 686 976 Б ≈ 2.56 МиБ → округляем вниз до 2.5 МБ.
    # Два замера с прода (1.7 МБ → 200, 3.7 МБ → 413) стены между собой не
    # различают — но нам и не нужно: даже если 413 гейтвея меряет сырой HTTP-запрос,
    # файл крупнее ~2.6 МБ всё равно не пролезет дальше, в функцию. Ошибаться здесь
    # в оптимистичную сторону нельзя: это ровно тот баг п.10, который мы чиним.
    # Если понадобится отыграть мегабайт — бинарный поиск по POST /recognition
    # (2.6 / 2.8 / 3.0 МБ). Прошедшие 2.8 МБ означают, что base64-стены нет и
    # потолок можно поднять до ~3.4 МБ, поменяв только это число.
    max_upload_mb: float = 2.5

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
    # Версия REST API SpeechKit: "v3" (по умолчанию) или "v1".
    # v1 тарифицируется по символам, v3 — по запросам, поэтому короткие реплики
    # («Прослушать» на ответ гида) в v3 обходятся заметно дешевле. Значение "v1"
    # оставлено как аварийный откат: меняется одной переменной окружения, код
    # обоих путей живёт рядом (app/services/tts.py).
    speechkit_api_version: str = "v3"

    # ── Расход LLM ───────────────────────────────────────────────────────────
    # Модель для «переписать числа прописью» (llm.to_spoken_text). Задача
    # механическая — Pro на ней переплата, поэтому по умолчанию берётся lite:
    # если URI не задан явно, он выводится из YANDEXGPT_MODEL_URI заменой
    # yandexgpt → yandexgpt-lite (см. llm._lite_model_uri).
    yandexgpt_lite_model_uri: Optional[str] = None
    # Сколько последних реплик диалога уходит в промпт уточняющего вопроса.
    # Было 6; на 3 качество ответов не падает (гид отвечает на текущий вопрос,
    # а не пересказывает диалог), а входной контекст вдвое короче.
    guide_history_turns: int = 3
    # Потолок справки об экспонате/зале в промпте диалога, знаков. Полное
    # raw_history бывает в несколько тысяч знаков и целиком уезжает в каждый
    # уточняющий вопрос; для ответа хватает начала (обрезаем по границе фразы).
    guide_grounding_max_chars: int = 700
    # Целевой объём рассказа (POST /guide/story), знаков. Прежний рассказ на
    # ~2500 знаков посетитель дослушивал редко, а платили мы за каждый токен;
    # ориентир вдвое короче держится промптом, max_tokens — только страховка,
    # чтобы модель не обрывало на полуслове.
    guide_story_max_chars: int = 1200
    guide_story_max_tokens: int = 500
    # Писать в лог расход токенов на каждый вызов LLM (INFO, logger
    # app.services.llm): операция, модель, input/output/total. По этим строкам
    # считается стоимость ответа.
    llm_log_usage: bool = True

    # ── Аналитика посетителей ────────────────────────────────────────────────
    # Неактивность, после которой визит считается завершённым (требование
    # заказчика — 30 минут). Используется и при приёме `session_end` от фронта,
    # и как серверная страховка: поток событий сессии режется по разрыву,
    # даже если `session_end` не дошёл (app/services/visits.py).
    session_timeout_minutes: int = 30
    # Отчёты отдаются из кэша агрегатов (таблица analytics_reports); запись
    # старше этого срока пересчитывается на лету. По ТЗ реалтайм не требуется —
    # данные обновляются раз в сутки ночным джобом (scripts/rebuild_analytics.py).
    analytics_cache_ttl_minutes: int = 1440
    # Кластер вопросов с частотой не выше этого числа попадает в «редкие».
    analytics_rare_max_count: int = 2
    # Шрифт с кириллицей для PDF-выгрузки. Пусто — ищем по стандартным путям
    # системы (app/services/analytics_export.py). Без кириллического шрифта
    # ReportLab выдаёт лист с квадратами вместо текста.
    analytics_pdf_font_path: Optional[str] = None

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

    # ── Лимит загрузки: заявленный против платформенного (п.10) ──────────────
    @property
    def platform_max_upload_bytes(self) -> int:
        """Сколько байт файла физически проходит через API Gateway.

        Чисто производная величина — в env её задавать нечего, меняется только
        gateway_max_request_bytes (и то если Yandex поднимет квоту). Считаем по
        нижней из двух стен: не по 413 гейтвея, а по payload синхронного вызова
        функции, куда тело уезжает в base64 (+33%), минус запас на multipart.
        """
        payload = self.gateway_max_request_bytes * 3 // 4
        return max(payload - self.upload_request_overhead_bytes, 0)

    @property
    def max_upload_bytes(self) -> int:
        """Фактический лимит на файл — именно его проверяют ручки загрузки.

        min(заявленного, платформенного): если кто-то снова поставит
        MAX_UPLOAD_MB=10, приложение не начнёт обещать невозможное — оно зажмёт
        лимит и само отдаст 413 (с CORS), вместо того чтобы отдать запрос
        гейтвею и получить «Failed to fetch». Падать при старте из-за строчки в
        env для музейного PWA хуже, чем молча ужаться, — расхождение уезжает в
        лог WARNING (см. _check_upload_limit).
        """
        declared = int(self.max_upload_mb * 1024 * 1024)
        return min(declared, self.platform_max_upload_bytes)

    @property
    def max_upload_label(self) -> str:
        """Лимит для текстов ошибок и описаний ручек — «2,5 МБ».

        Считается от max_upload_bytes, а не от max_upload_mb: DoD п.10 —
        «заявленный лимит совпадает с фактическим», значит показывать число,
        которое мы сами уже зажали, нельзя.

        Округление — ВНИЗ, а не к ближайшему. При MAX_UPLOAD_MB=10 (значение,
        которое сейчас стоит в окружении прода) фактический лимит — 2.5625 МиБ,
        и округление к ближайшему давало «2,6 МБ»: приложение снова обещало
        больше, чем принимает, — тот же баг п.10, только на одну десятую.
        Посетитель, сжавший фото ровно под обещанные 2,6 МБ, получал бы 413.
        """
        tenths = math.floor(self.max_upload_bytes / (1024 * 1024) * 10) / 10
        text = f"{tenths:.0f}" if tenths.is_integer() else f"{tenths:.1f}"
        return f"{text.replace('.', ',')} МБ"

    @model_validator(mode="after")
    def _check_upload_limit(self) -> "Settings":
        """Проверка при старте: MAX_UPLOAD_MB не должен превышать потолок платформы.

        Не исключение, а WARNING + зажатие в max_upload_bytes: неверная строчка в
        env не должна ронять прод, но и тихо возвращать баг п.10 (обещаем больше,
        чем пропускает гейтвей) тоже нельзя.
        """
        if int(self.max_upload_mb * 1024 * 1024) > self.platform_max_upload_bytes:
            logger.warning(
                "MAX_UPLOAD_MB=%s превышает лимит API Gateway (%s Б на весь запрос, "
                "на файл остаётся %s Б) — фактически используется %s Б. Поднять потолок "
                "можно только в конфигурации API Gateway, не в коде.",
                self.max_upload_mb, self.gateway_max_request_bytes,
                self.platform_max_upload_bytes, self.max_upload_bytes,
            )
        return self

    # Флаги «настроен ли внешний сервис» — для health-check и выбора реализации.
    @property
    def llm_configured(self) -> bool:
        return bool(self.yandex_api_key and (self.yandexgpt_model_uri or self.yandex_folder_id))

    @property
    def tts_configured(self) -> bool:
        return bool(self.speechkit_api_key or self.yandex_api_key)

    @property
    def speechkit_v3(self) -> bool:
        """Синтезировать через API v3 (тарификация по запросам, а не по символам)."""
        return (self.speechkit_api_version or "v3").strip().lower() != "v1"

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
