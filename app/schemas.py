"""Pydantic-схемы запросов/ответов (зеркало openapi.yaml)."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


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
    label_slug: Optional[str] = None
    name: str
    year_created: Optional[int] = None
    master_name: Optional[str] = None
    material: Optional[str] = None
    short_description: Optional[str] = None
    image_url: Optional[str] = None
    images: List[Image] = Field(default_factory=list)
    model_3d_url: Optional[str] = None
    model_3d_embed: Optional[str] = None
    audio_url: Optional[str] = None
    source_url: Optional[str] = None
    hall: Optional[HallBrief] = None
    showcase: Optional[ShowcaseBrief] = None


class ExhibitAdmin(Exhibit):
    raw_history: Optional[str] = None


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


class ChatResponse(BaseModel):
    session_id: uuid.UUID
    answer: str
    suggested_questions: List[str] = Field(default_factory=list)
    context: Optional[GuideContext] = None


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


class HallPatch(BaseModel):
    hall_number: Optional[int] = None
    name: Optional[str] = None
    description: Optional[str] = None
    level: Optional[int] = None
    cover_image_url: Optional[str] = None
    is_temporary: Optional[bool] = None


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


class ExhibitCreate(BaseModel):
    showcase_id: int
    label_slug: Optional[str] = None
    name: str
    year_created: Optional[int] = None
    master_name: Optional[str] = None
    material: Optional[str] = None
    short_description: Optional[str] = None
    raw_history: Optional[str] = None
    image_url: Optional[str] = None
    model_3d_url: Optional[str] = None


class ExhibitUpdate(ExhibitCreate):
    pass


class ExhibitPatch(BaseModel):
    showcase_id: Optional[int] = None
    label_slug: Optional[str] = None
    name: Optional[str] = None
    year_created: Optional[int] = None
    master_name: Optional[str] = None
    material: Optional[str] = None
    short_description: Optional[str] = None
    raw_history: Optional[str] = None
    image_url: Optional[str] = None
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
    total_recognitions: int = 0
    recognition_success_rate: float = 0.0
    total_chat_messages: int = 0
    total_audio_plays: int = 0
    top_exhibits: List[AnalyticsTopItem] = Field(default_factory=list)
    top_halls: List[AnalyticsTopItem] = Field(default_factory=list)

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
