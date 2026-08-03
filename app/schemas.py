"""Pydantic-схемы запросов/ответов (зеркало openapi.yaml)."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ── Система ──────────────────────────────────────────────────────────────────
class HealthStatus(BaseModel):
    status: str = "ok"
    version: str
    time: Optional[datetime] = None
    dependencies: Dict[str, str] = Field(default_factory=dict)


# ── Залы ─────────────────────────────────────────────────────────────────────
class Hall(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    # null — у зала нет номера («Вне постоянной экспозиции»): подпись строится
    # только из названия, без «зал № …».
    hall_number: Optional[int] = None
    name: Optional[str] = None
    description: Optional[str] = None
    level: Optional[int] = None
    cover_image_url: Optional[str] = None
    is_temporary: bool = False            # зал временной выставки (vs основная экспозиция)
    is_service: bool = False              # служебная запись: в публичной выдаче не появляется
    sort_order: int = 0                   # порядок вывода в каталоге/админке (drag-n-drop, C11)
    showcase_count: Optional[int] = None
    exhibit_count: Optional[int] = None


class HallBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    hall_number: Optional[int] = None
    name: Optional[str] = None


class Showcase(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    hall_id: int
    # null — группа «не в витринах» (в путеводителе — пустой квадрат): экспонаты
    # зала, стоящие вне витрин. В зале такая группа одна и выводится последней.
    showcase_number: Optional[int] = None
    name: Optional[str] = None
    exhibit_count: Optional[int] = None


class ShowcaseBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    showcase_number: Optional[int] = None


class HallDetail(Hall):
    showcases: Optional[List[Showcase]] = None


class MapHall(Hall):
    showcases: List[Showcase] = Field(default_factory=list)


class MapResponse(BaseModel):
    halls: List[MapHall]


class HallListResponse(BaseModel):
    items: List[Hall]
    total: int
    limit: int
    offset: int


class ShowcaseDetail(Showcase):
    hall: Optional[HallBrief] = None
    exhibits: Optional[List["ExhibitSummary"]] = None


class ShowcaseListResponse(BaseModel):
    items: List[Showcase]
    total: int
    limit: int
    offset: int


# ── Экспонаты ────────────────────────────────────────────────────────────────
class Image(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int                              # идентификатор для DELETE /admin/exhibits/{id}/media/{image_id}
    url: str
    alt: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    is_primary: bool = False             # главная фотография экспоната (= exhibits.image_url)


class ExhibitSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    exhibit_number: Optional[str] = None  # номер по путеводителю музея (B3): перед названием
    label_slug: Optional[str] = None
    name: str
    year_created: Optional[int] = None
    master_name: Optional[str] = None
    thumbnail_url: Optional[str] = None
    hall_id: Optional[int] = None
    showcase_id: Optional[int] = None
    # Номер витрины (null — экспонат вне витрин). Отдаём прямо в списке, чтобы
    # фронт собирал группировку «витрина → её экспонаты» из одного ответа
    # GET /halls/{id}/exhibits, без запроса на каждую витрину.
    showcase_number: Optional[int] = None
    is_temporary: Optional[bool] = None  # унаследовано от зала: экспонат временной выставки


class Exhibit(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    exhibit_number: Optional[str] = None  # номер по путеводителю музея (B3)
    label_slug: Optional[str] = None
    name: str
    year_created: Optional[int] = None
    master_name: Optional[str] = None
    material: Optional[str] = None
    short_description: Optional[str] = None
    image_url: Optional[str] = None
    images: List[Image] = Field(default_factory=list)
    video_url: Optional[str] = None       # видео экспоната (B4/C22)
    model_3d_url: Optional[str] = None
    model_3d_embed: Optional[str] = None
    audio_url: Optional[str] = None
    source_url: Optional[str] = None
    hall: Optional[HallBrief] = None
    showcase: Optional[ShowcaseBrief] = None


class ExhibitAdmin(Exhibit):
    raw_history: Optional[str] = None
    # Версия short_description для озвучки: числа прописью в нужном падеже (E15).
    # Автогенерируется LLM при сохранении описания; админ может переопределить вручную.
    short_description_spoken: Optional[str] = None


class ExhibitListResponse(BaseModel):
    items: List[ExhibitSummary]
    total: int
    limit: int
    offset: int


# ── Поиск ────────────────────────────────────────────────────────────────────
class SearchResponse(BaseModel):
    query: str
    halls: List[Hall]
    exhibits: List[ExhibitSummary]
    total: int


# ── Распознавание ────────────────────────────────────────────────────────────
class RecognitionCandidate(BaseModel):
    label_slug: str
    name: Optional[str] = None
    confidence: float
    exhibit_id: Optional[int] = None      # id карточки экспоната для перехода (B5/E19)
    thumbnail_url: Optional[str] = None   # миниатюра кандидата (= exhibits.image_url) (B5/E19)


class RecognitionResponse(BaseModel):
    recognized: bool
    label_slug: Optional[str] = None
    confidence: Optional[float] = None
    exhibit: Optional[Exhibit] = None
    candidates: List[RecognitionCandidate] = Field(default_factory=list)
    request_id: Optional[str] = None
    processing_ms: Optional[int] = None


# ── ИИ-гид ───────────────────────────────────────────────────────────────────
class GuideStyle(str, enum.Enum):
    engaging = "engaging"
    historical = "historical"
    short = "short"
    kids = "kids"
    expert = "expert"


class GuideContext(BaseModel):
    exhibit_id: Optional[int] = None
    label_slug: Optional[str] = None
    hall_id: Optional[int] = None


class StoryRequest(BaseModel):
    exhibit_id: Optional[int] = None
    label_slug: Optional[str] = None
    style: GuideStyle = GuideStyle.engaging
    language: str = "ru"
    include_audio: bool = False
    max_questions: int = Field(default=4, ge=0, le=6)


class StoryResponse(BaseModel):
    exhibit_id: Optional[int] = None
    label_slug: Optional[str] = None
    style: GuideStyle = GuideStyle.engaging
    text: str
    suggested_questions: List[str] = Field(default_factory=list)
    audio_url: Optional[str] = None
    model: Optional[str] = None
    generated_at: Optional[datetime] = None


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    session_id: Optional[uuid.UUID] = None
    # Контракт контекста (баг-репорт 28.07.2026, п.3):
    #   • поле не передано вовсе   — бэкенд подставит контекст, сохранённый в сессии;
    #   • `"context": {}` / `null` — явный СБРОС: контекст сессии очищается,
    #                                вопрос трактуется как общий;
    #   • заполненный объект       — заменяет контекст сессии целиком (старый
    #                                hall_id не «доклеивается»).
    context: Optional[GuideContext] = None
    message: str = Field(min_length=1)
    history: Optional[List[ChatMessage]] = None
    language: str = "ru"
    max_questions: int = Field(default=3, ge=0, le=6)
    # Явный сброс контекста без передачи самого поля `context` — для входа в общий
    # чат с главного экрана, когда клиент переиспользует прежний `session_id`.
    reset_context: bool = False


# Плашка экспоната в ответе гида (B6): фронт рисует компонент exhibit-plaque.
class ReferencedExhibit(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    exhibit_number: Optional[str] = None
    thumbnail_url: Optional[str] = None
    hall_number: Optional[int] = None
    showcase_number: Optional[int] = None


# Структурированный зал в ответе гида (B10): «какие залы есть».
class ReferencedHall(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    hall_number: Optional[int] = None
    name: Optional[str] = None


# Навигационный ответ «зал + витрина» (B7): «как найти экспонат».
class GuideLocation(BaseModel):
    hall_number: Optional[int] = None
    hall_name: Optional[str] = None
    showcase_number: Optional[int] = None


class ChatResponse(BaseModel):
    session_id: uuid.UUID
    answer: str
    suggested_questions: List[str] = Field(default_factory=list)
    context: Optional[GuideContext] = None
    # Экспонаты, на которые ссылается ответ (B6): плашки-ссылки на карточки.
    referenced_exhibits: List[ReferencedExhibit] = Field(default_factory=list)
    # Залы, если вопрос про список/навигацию по залам (B10).
    referenced_halls: List[ReferencedHall] = Field(default_factory=list)
    # Где искать экспонат, если вопрос навигационный (B7).
    location: Optional[GuideLocation] = None


# ── Озвучивание ──────────────────────────────────────────────────────────────
class SpeechVoice(str, enum.Enum):
    alena = "alena"
    filipp = "filipp"
    jane = "jane"
    omazh = "omazh"
    zahar = "zahar"
    ermil = "ermil"


class AudioFormat(str, enum.Enum):
    mp3 = "mp3"
    oggopus = "oggopus"
    wav = "wav"


class SpeechRequest(BaseModel):
    text: Optional[str] = None
    exhibit_id: Optional[int] = None
    voice: SpeechVoice = SpeechVoice.alena
    format: AudioFormat = AudioFormat.mp3
    speed: float = Field(default=1.0, ge=0.1, le=3.0)
    emotion: str = "neutral"


class SpeechResponse(BaseModel):
    audio_url: str
    format: AudioFormat
    voice: SpeechVoice
    duration_ms: Optional[int] = None
    characters: Optional[int] = None
    cached: Optional[bool] = None


# ── Администрирование ────────────────────────────────────────────────────────
class HallCreate(BaseModel):
    # null — зал без номера («Вне постоянной экспозиции»).
    hall_number: Optional[int] = None
    name: Optional[str] = None
    description: Optional[str] = None
    level: Optional[int] = None
    is_temporary: bool = False
    is_service: bool = False              # служебная запись: скрыта из публичной выдачи
    # Если не указан — бэкенд ставит hall_number, а залу без номера даёт конец списка.
    sort_order: Optional[int] = None


class HallPatch(BaseModel):
    # hall_number допускает явный null — так зал становится «без номера»
    # (заказчик: «ЗАЛ № 99 — Вне постоянной экспозиции» показывать без номера).
    hall_number: Optional[int] = None
    name: Optional[str] = None
    description: Optional[str] = None
    level: Optional[int] = None
    cover_image_url: Optional[str] = None
    is_temporary: Optional[bool] = None
    is_service: Optional[bool] = None
    sort_order: Optional[int] = None

    @model_validator(mode="after")
    def _reject_null_required(self) -> "HallPatch":
        # Не-nullable в БД поля нельзя обнулять явным null (иначе IntegrityError → 409).
        for field in ("is_temporary", "is_service", "sort_order"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"Поле '{field}' не может быть null.")
        return self


class HallReorderRequest(BaseModel):
    # Список id залов в желаемом порядке (drag-n-drop). Залы переставляются в
    # рамках своих текущих позиций (slot-preserving): бэкенд берёт их нынешние
    # sort_order, сортирует и раздаёт в новом порядке. Поэтому можно переставлять
    # как весь список сразу, так и подсписок одной группы (основная/временная) —
    # позиции залов вне запроса не затрагиваются.
    hall_ids: List[int] = Field(min_length=1)


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ShowcaseCreate(BaseModel):
    hall_id: int
    # null — группа «не в витринах» (в путеводителе — пустой квадрат). В зале
    # допустима одна такая группа: повтор → 409.
    showcase_number: Optional[int] = None
    name: Optional[str] = None


class ShowcasePatch(BaseModel):
    # Частичное обновление витрины (B2/E14). Фронт (updateShowcase в lib/api/admin.ts)
    # шлёт любой поднабор из {hall_id, showcase_number, name}.
    hall_id: Optional[int] = None
    showcase_number: Optional[int] = None
    name: Optional[str] = None

    @model_validator(mode="after")
    def _reject_null_required(self) -> "ShowcasePatch":
        # hall_id — NOT NULL в БД: явный null это ошибка ввода (422), а не «поле не
        # передано». Иначе IntegrityError маппился бы в неверный 409.
        # name и showcase_number допускают null (в БД nullable).
        if "hall_id" in self.model_fields_set and self.hall_id is None:
            raise ValueError("Поле 'hall_id' не может быть null.")
        return self


class ExhibitCreate(BaseModel):
    showcase_id: int
    exhibit_number: Optional[str] = None  # номер по путеводителю музея (B3)
    label_slug: Optional[str] = None
    name: str
    year_created: Optional[int] = None
    master_name: Optional[str] = None
    material: Optional[str] = None
    short_description: Optional[str] = None
    # Ручное переопределение озвучки (числа прописью). Если не передан —
    # бэкенд сгенерирует его из short_description через LLM (E15).
    short_description_spoken: Optional[str] = None
    raw_history: Optional[str] = None
    image_url: Optional[str] = None
    video_url: Optional[str] = None       # видео экспоната (B4/C22)
    model_3d_url: Optional[str] = None


class ExhibitUpdate(ExhibitCreate):
    pass


class ExhibitPatch(BaseModel):
    showcase_id: Optional[int] = None
    exhibit_number: Optional[str] = None
    label_slug: Optional[str] = None
    name: Optional[str] = None
    year_created: Optional[int] = None
    master_name: Optional[str] = None
    material: Optional[str] = None
    short_description: Optional[str] = None
    short_description_spoken: Optional[str] = None
    raw_history: Optional[str] = None
    image_url: Optional[str] = None
    video_url: Optional[str] = None
    model_3d_url: Optional[str] = None


class MediaUploadResponse(BaseModel):
    image_id: int                        # id созданной записи галереи (для DELETE .../media/{image_id})
    image_url: str
    thumbnail_url: Optional[str] = None  # сейчас совпадает с image_url — отдельная миниатюра не генерируется
    object_key: str


class AnalyticsTopItem(BaseModel):
    # id = null у элементов, у которых нет сущности в каталоге — например, у
    # «экранов выхода» (там значимо только имя типа события).
    id: Optional[int] = None
    name: Optional[str] = None
    count: int


class AnalyticsOverview(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    # Время последнего пересчёта агрегатов (§12). Фронт показывает «данные на …»,
    # чтобы отсутствие сегодняшних цифр не выглядело поломкой дашборда.
    updated_at: Optional[datetime] = None
    total_sessions: int = 0
    total_app_opens: int = 0
    total_recognitions: int = 0
    recognition_success_rate: float = 0.0
    total_chat_messages: int = 0
    total_audio_plays: int = 0
    top_exhibits: List[AnalyticsTopItem] = Field(default_factory=list)
    top_halls: List[AnalyticsTopItem] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


# ── Аналитика: частые/редкие вопросы (C16 + §3 ТЗ 03.08.2026) ────────────────
class AnalyticsQuestionItem(BaseModel):
    question: str                     # представитель кластера — самая частая формулировка
    count: int                        # суммарно по кластеру
    # Другие формулировки того же смысла (до 5). Музею важно видеть, КАК спрашивают.
    variants: List[str] = Field(default_factory=list)


class AnalyticsQuestions(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    updated_at: Optional[datetime] = None
    total_questions: int = 0          # всего реплик пользователя (role='user')
    unique_questions: int = 0         # различных формулировок (после нормализации)
    total_clusters: int = 0           # смысловых групп вопросов
    frequent: List[AnalyticsQuestionItem] = Field(default_factory=list)  # топ по убыванию
    # Редкие — кластеры, встретившиеся не чаще RARE_MAX_COUNT раз; с `frequent` не пересекаются.
    rare: List[AnalyticsQuestionItem] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


# ── Аналитика: вопросы без ответа гида (§4) ──────────────────────────────────
class AnalyticsUnansweredItem(BaseModel):
    question: str                     # представитель кластера
    count: int
    variants: List[str] = Field(default_factory=list)
    # Сколько раз каждая причина: no_context | llm_refusal | not_found | error.
    fail_reasons: Dict[str, int] = Field(default_factory=dict)
    # Экспонаты, у карточек которых задавали эти вопросы — там и не хватает описания.
    exhibits: List[AnalyticsTopItem] = Field(default_factory=list)


class AnalyticsUnanswered(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    updated_at: Optional[datetime] = None
    total_unanswered: int = 0
    total_answered: int = 0
    # Доля неотвеченных среди вопросов с проставленным признаком (answered IS NOT NULL).
    unanswered_rate: float = 0.0
    # Вопросы без признака (накоплены до миграции 2026-08-03) — в расчёт доли не входят.
    unclassified: int = 0
    fail_reasons: Dict[str, int] = Field(default_factory=dict)
    items: List[AnalyticsUnansweredItem] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


# ── Аналитика: вовлечённость / длительность сессии (C17) ─────────────────────
class AnalyticsDurationBucket(BaseModel):
    label: str                        # напр. "0–1 мин", "1–5 мин"
    count: int


class AnalyticsEngagement(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    updated_at: Optional[datetime] = None
    # Число ВИЗИТОВ: поток событий сессии режется по неактивности дольше
    # SESSION_TIMEOUT_MINUTES (§5), поэтому визитов может быть больше, чем сессий.
    total_sessions: int = 0
    total_visits: int = 0
    avg_duration_sec: float = 0.0     # среднее от первого события визита до последнего
    median_duration_sec: float = 0.0
    max_duration_sec: float = 0.0
    avg_events_per_session: float = 0.0
    # §6 — что именно посетитель успел сделать за визит.
    avg_exhibits_per_session: float = 0.0   # уникальных exhibit_view
    avg_questions_per_session: float = 0.0  # chat_message
    sessions_with_chat: int = 0             # визитов хотя бы с одним chat_open
    sessions_with_questions: int = 0        # визитов хотя бы с одним chat_message
    # Знаменатель конверсий — визиты с app_open. Если app_open не приходил ни разу
    # (фронт ещё не шлёт), знаменателем становится общее число визитов.
    sessions_with_app_open: int = 0
    chat_conversion_rate: float = 0.0
    question_conversion_rate: float = 0.0
    buckets: List[AnalyticsDurationBucket] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


# ── Аналитика: маршрут пользователя по залам (C18) ───────────────────────────
class AnalyticsRouteHall(BaseModel):
    id: int
    name: Optional[str] = None
    count: int


class AnalyticsRouteTransition(BaseModel):
    from_hall_id: int
    from_hall_name: Optional[str] = None
    to_hall_id: int
    to_hall_name: Optional[str] = None
    count: int


class AnalyticsRoutePath(BaseModel):
    halls: List[AnalyticsRouteHall] = Field(default_factory=list)  # последовательность залов
    count: int


class AnalyticsSessionsPerDeviceBucket(BaseModel):
    label: str                        # «1 визит», «2 визита», «3+ визита»
    devices: int


class AnalyticsRoutes(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    updated_at: Optional[datetime] = None
    total_sessions_with_route: int = 0
    avg_halls_per_session: float = 0.0
    top_hall_visits: List[AnalyticsRouteHall] = Field(default_factory=list)
    top_entry_halls: List[AnalyticsRouteHall] = Field(default_factory=list)     # с какого зала начинают
    top_transitions: List[AnalyticsRouteTransition] = Field(default_factory=list)  # переходы A→B
    top_paths: List[AnalyticsRoutePath] = Field(default_factory=list)           # частые полные маршруты
    # §7 — где отваливаются.
    top_exit_halls: List[AnalyticsRouteHall] = Field(default_factory=list)      # последний hall_view визита
    top_exit_screens: List[AnalyticsTopItem] = Field(default_factory=list)      # тип последнего события визита
    # §7 — повторные визиты по анонимному device_id. Сессии без device_id
    # (старые данные, приватный режим) считаются одиночными устройствами.
    total_devices: int = 0
    returning_devices: int = 0                # устройств с ≥2 сессиями
    avg_sessions_per_device: float = 0.0
    sessions_per_device_hist: List[AnalyticsSessionsPerDeviceBucket] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


# ── Аналитика: статистика по экспонатам (§8) ─────────────────────────────────
class AnalyticsExhibitRow(BaseModel):
    id: int
    name: Optional[str] = None
    hall_number: Optional[int] = None
    views: int = 0
    questions: int = 0                # chat_message с этим exhibit_id
    tts_plays: int = 0
    recognitions: int = 0


class AnalyticsExhibits(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    updated_at: Optional[datetime] = None
    order: str = "views"              # views | questions | asc
    total_exhibits: int = 0           # всего экспонатов в каталоге
    never_viewed: int = 0             # ни одного exhibit_view за период
    items: List[AnalyticsExhibitRow] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


# ── Аналитика: качество распознавания (§9) ───────────────────────────────────
class AnalyticsRecognition(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    updated_at: Optional[datetime] = None
    total: int = 0
    success: int = 0
    success_rate: float = 0.0
    fallback_shown: int = 0           # props.fallback == true (показан топ-3)
    fallback_rate: float = 0.0
    fallback_converted: int = 0       # после фолбэка открыли карточку из кандидатов
    fallback_conversion_rate: float = 0.0
    failed: int = 0
    abandoned_after_fail: int = 0     # после неуспеха в визите не было содержательных событий
    abandonment_rate: float = 0.0
    retry_after_fail: int = 0         # повторная съёмка после неуспеха
    avg_confidence: float = 0.0       # среднее props.confidence по событиям, где оно есть

    model_config = ConfigDict(populate_by_name=True)


# ── Аналитика: суточный срез (§12) ───────────────────────────────────────────
class AnalyticsDailyPoint(BaseModel):
    date: str                         # ISO-дата
    metric: str
    dimension_key: str = ""
    dimension_id: Optional[int] = None
    value: float = 0.0


class AnalyticsDailySeries(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    updated_at: Optional[datetime] = None
    points: List[AnalyticsDailyPoint] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


class AnalyticsRebuildResult(BaseModel):
    rebuilt_reports: List[str] = Field(default_factory=list)
    daily_days: int = 0               # за сколько суток пересчитан плоский срез
    daily_rows: int = 0
    updated_at: datetime


# ── Телеметрия ───────────────────────────────────────────────────────────────
class EventType(str, enum.Enum):
    """Словарь типов событий — единственный источник правды контракта телеметрии.

    До 03.08.2026 ``type`` был свободной строкой: опечатка на фронте
    (``exhibitView``, ``hall-view``) молча писалась в БД и навсегда портила
    агрегаты — события по требованию заказчика не удаляются.
    """

    app_open = "app_open"
    hall_view = "hall_view"
    showcase_view = "showcase_view"
    exhibit_view = "exhibit_view"
    recognition = "recognition"
    chat_open = "chat_open"
    chat_message = "chat_message"
    tts_play = "tts_play"
    search_query = "search_query"
    session_end = "session_end"


# Совместимость: фронт исторически шлёт `audio_play` — это то же самое, что
# `tts_play`. Канонический тип — `tts_play`, псевдоним нормализуется на входе,
# чтобы уже накопленные данные не потерялись и не раздвоились в отчётах.
EVENT_TYPE_ALIASES: Dict[str, EventType] = {"audio_play": EventType.tts_play}

# Белый список ключей `props` по типам события (§10). Всё, чего нет в контракте,
# отбрасывается на приёме: props — свободный JSONB, и без фильтра фронт (или кто
# угодно, эндпоинт без авторизации) может положить туда user_agent, url с
# параметрами, координаты — то есть персональные данные, которых мы не храним.
EVENT_PROPS_ALLOWED: Dict[EventType, frozenset] = {
    EventType.app_open: frozenset({"entry", "qr_id"}),
    EventType.hall_view: frozenset(),
    EventType.showcase_view: frozenset(),
    EventType.exhibit_view: frozenset({"source"}),
    EventType.recognition: frozenset({"recognized", "confidence", "fallback", "candidates_count"}),
    EventType.chat_open: frozenset(),
    EventType.chat_message: frozenset({"text"}),
    EventType.tts_play: frozenset(),
    EventType.search_query: frozenset({"text", "results_count"}),
    EventType.session_end: frozenset({"reason", "last_screen"}),
}

# Ключи, которые нельзя принимать ни при каких условиях. Формально избыточно
# (работает белый список), но перечислены явно — это то, что показывают
# заказчику в docs/analytics-privacy.md.
EVENT_PROPS_DENIED = frozenset({"user_agent", "ip", "email", "phone", "url", "referrer", "geo"})

MAX_EVENTS_PER_BATCH = 50   # эндпоинт без авторизации: ограничиваем размер записи
MAX_PROPS_TEXT_LEN = 500    # текст вопроса/запроса длиннее — обрезается, не отклоняется


class Event(BaseModel):
    """Одно событие телеметрии.

    ``type`` объявлен строкой, а не ``EventType``, СОЗНАТЕЛЬНО: фронт шлёт события
    пачкой, и одна опечатка не должна ронять валидацией весь батч (потерять
    девять корректных событий из-за десятого). Допустимые значения проверяет
    ``normalize_event`` — неизвестные попадают в ``rejected`` ответа
    ``POST /telemetry/events``, остальные записываются.
    """

    type: str = Field(
        description=(
            "Тип события. Допустимо: " + ", ".join(t.value for t in EventType)
            + " (плюс устаревший псевдоним audio_play → tts_play). "
            "Событие с неизвестным типом отбрасывается, батч не отклоняется."
        ),
        examples=["exhibit_view"],
    )
    exhibit_id: Optional[int] = None
    hall_id: Optional[int] = None
    showcase_id: Optional[int] = None
    label_slug: Optional[str] = None
    # Анонимный ID устройства; если не задан на событии — берётся из батча.
    device_id: Optional[uuid.UUID] = None
    # Момент ДЕЙСТВИЯ (проставляет фронт), а не момент отправки батча.
    ts: Optional[datetime] = None
    props: Optional[Dict[str, Any]] = None


class EventBatch(BaseModel):
    session_id: Optional[uuid.UUID] = None
    device_id: Optional[uuid.UUID] = None   # значение по умолчанию для событий батча
    events: List[Event] = Field(min_length=1, max_length=MAX_EVENTS_PER_BATCH)


class EventIngestResult(BaseModel):
    accepted: int = 0
    rejected: int = 0                       # события с неизвестным `type`


def clean_event_props(event_type: EventType, props: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Оставить только ключи из контракта события и подрезать длинные тексты (§1/§10)."""
    if not props:
        return None
    allowed = EVENT_PROPS_ALLOWED.get(event_type, frozenset())
    clean: Dict[str, Any] = {}
    for key, value in props.items():
        if key not in allowed:
            continue  # в т.ч. всё из EVENT_PROPS_DENIED — его нет ни в одном белом списке
        if key == "text" and isinstance(value, str):
            value = value[:MAX_PROPS_TEXT_LEN]
        clean[key] = value
    return clean or None


def normalize_event(ev: Event) -> Optional[Event]:
    """Привести событие к контракту телеметрии.

    Возвращает копию с каноническим ``type`` и отфильтрованным ``props`` либо
    ``None``, если тип неизвестен — тогда событие отбрасывается, а остальные из
    того же батча записываются.
    """
    raw = (ev.type or "").strip()
    event_type = EVENT_TYPE_ALIASES.get(raw)
    if event_type is None:
        try:
            event_type = EventType(raw)
        except ValueError:
            return None
    return ev.model_copy(update={"type": event_type.value, "props": clean_event_props(event_type, ev.props)})


# ── Ошибки ───────────────────────────────────────────────────────────────────
class Error(BaseModel):
    detail: str


# Разрешаем отложенные ссылки (ShowcaseDetail -> ExhibitSummary).
ShowcaseDetail.model_rebuild()
