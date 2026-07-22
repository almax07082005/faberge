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
    hall_number: int
    name: Optional[str] = None
    description: Optional[str] = None
    level: Optional[int] = None
    cover_image_url: Optional[str] = None
    is_temporary: bool = False            # зал временной выставки (vs основная экспозиция)
    sort_order: int = 0                   # порядок вывода в каталоге/админке (drag-n-drop, C11)
    showcase_count: Optional[int] = None
    exhibit_count: Optional[int] = None


class HallBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    hall_number: int
    name: Optional[str] = None


class Showcase(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    hall_id: int
    showcase_number: int
    name: Optional[str] = None
    exhibit_count: Optional[int] = None


class ShowcaseBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    showcase_number: int


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
    context: Optional[GuideContext] = None
    message: str = Field(min_length=1)
    history: Optional[List[ChatMessage]] = None
    language: str = "ru"
    max_questions: int = Field(default=3, ge=0, le=6)


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
    hall_number: int
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
    hall_number: int
    name: Optional[str] = None
    description: Optional[str] = None
    level: Optional[int] = None
    is_temporary: bool = False
    sort_order: Optional[int] = None      # если не указан — бэкенд ставит hall_number


class HallPatch(BaseModel):
    hall_number: Optional[int] = None
    name: Optional[str] = None
    description: Optional[str] = None
    level: Optional[int] = None
    cover_image_url: Optional[str] = None
    is_temporary: Optional[bool] = None
    sort_order: Optional[int] = None

    @model_validator(mode="after")
    def _reject_null_required(self) -> "HallPatch":
        # Не-nullable в БД поля нельзя обнулять явным null (иначе IntegrityError → 409).
        for field in ("hall_number", "is_temporary", "sort_order"):
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
    showcase_number: int
    name: Optional[str] = None


class ShowcasePatch(BaseModel):
    # Частичное обновление витрины (B2/E14). Фронт (updateShowcase в lib/api/admin.ts)
    # шлёт любой поднабор из {hall_id, showcase_number, name}.
    hall_id: Optional[int] = None
    showcase_number: Optional[int] = None
    name: Optional[str] = None

    @model_validator(mode="after")
    def _reject_null_required(self) -> "ShowcasePatch":
        # hall_id и showcase_number — NOT NULL в БД: явный null это ошибка ввода (422),
        # а не «поле не передано». Иначе IntegrityError маппился бы в неверный 409.
        # name допускает null (в БД nullable).
        for field in ("hall_id", "showcase_number"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"Поле '{field}' не может быть null.")
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
    id: int
    name: Optional[str] = None
    count: int


class AnalyticsOverview(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    total_sessions: int = 0
    total_app_opens: int = 0
    total_recognitions: int = 0
    recognition_success_rate: float = 0.0
    total_chat_messages: int = 0
    total_audio_plays: int = 0
    top_exhibits: List[AnalyticsTopItem] = Field(default_factory=list)
    top_halls: List[AnalyticsTopItem] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


# ── Аналитика: частые/редкие вопросы (C16) ───────────────────────────────────
class AnalyticsQuestionItem(BaseModel):
    question: str
    count: int


class AnalyticsQuestions(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    total_questions: int = 0          # всего реплик пользователя (role='user')
    unique_questions: int = 0         # различных формулировок (после нормализации)
    frequent: List[AnalyticsQuestionItem] = Field(default_factory=list)  # топ по убыванию
    rare: List[AnalyticsQuestionItem] = Field(default_factory=list)      # редкие (по возрастанию)

    model_config = ConfigDict(populate_by_name=True)


# ── Аналитика: вовлечённость / длительность сессии (C17) ─────────────────────
class AnalyticsDurationBucket(BaseModel):
    label: str                        # напр. "0–1 мин", "1–5 мин"
    count: int


class AnalyticsEngagement(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    total_sessions: int = 0
    avg_duration_sec: float = 0.0     # среднее от первого открытия до последнего действия
    median_duration_sec: float = 0.0
    max_duration_sec: float = 0.0
    avg_events_per_session: float = 0.0
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


class AnalyticsRoutes(BaseModel):
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    total_sessions_with_route: int = 0
    avg_halls_per_session: float = 0.0
    top_hall_visits: List[AnalyticsRouteHall] = Field(default_factory=list)
    top_entry_halls: List[AnalyticsRouteHall] = Field(default_factory=list)     # с какого зала начинают
    top_transitions: List[AnalyticsRouteTransition] = Field(default_factory=list)  # переходы A→B
    top_paths: List[AnalyticsRoutePath] = Field(default_factory=list)           # частые полные маршруты

    model_config = ConfigDict(populate_by_name=True)


# ── Телеметрия ───────────────────────────────────────────────────────────────
class Event(BaseModel):
    type: str
    exhibit_id: Optional[int] = None
    hall_id: Optional[int] = None
    label_slug: Optional[str] = None
    ts: Optional[datetime] = None
    props: Optional[Dict[str, Any]] = None


class EventBatch(BaseModel):
    session_id: Optional[uuid.UUID] = None
    events: List[Event] = Field(min_length=1)


# ── Ошибки ───────────────────────────────────────────────────────────────────
class Error(BaseModel):
    detail: str


# Разрешаем отложенные ссылки (ShowcaseDetail -> ExhibitSummary).
ShowcaseDetail.model_rebuild()
