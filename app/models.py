"""ORM-модели SQLAlchemy 2.0 (соответствуют db/schema.sql)."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime


class Base(DeclarativeBase):
    pass


# Полнотекстовый вектор для поиска (B8/C27): русская конфигурация, взвешенная по
# важности поля. Выражения ДОЛЖНЫ совпадать с db/schema.sql (GENERATED-колонка),
# иначе поиск и индекс разойдутся. Все функции immutable → допустимо в GENERATED.
_HALL_TSV = (
    "setweight(to_tsvector('russian', coalesce(name,'')), 'A') || "
    "setweight(to_tsvector('russian', coalesce(description,'')), 'C')"
)
_EXHIBIT_TSV = (
    "setweight(to_tsvector('russian', coalesce(name,'')), 'A') || "
    "setweight(to_tsvector('russian', "
    "coalesce(master_name,'') || ' ' || coalesce(exhibit_number,'')), 'B') || "
    "setweight(to_tsvector('russian', coalesce(short_description,'')), 'C') || "
    "setweight(to_tsvector('russian', coalesce(raw_history,'')), 'D')"
)


class Hall(Base):
    __tablename__ = "halls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Номер зала на плане. NULL — у зала номера нет: «Вне постоянной экспозиции»
    # заказчик просил показывать без номера (баг-репорт 28.07.2026, п.5).
    hall_number: Mapped[Optional[int]] = mapped_column(Integer, unique=True)
    name: Mapped[Optional[str]] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)
    level: Mapped[Optional[int]] = mapped_column(Integer)
    cover_image_url: Mapped[Optional[str]] = mapped_column(Text)
    is_temporary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Служебная запись каталога (технические залы, вспомогательные пространства):
    # хранится в каталоге, но не показывается посетителю — ни в GET /halls, ни на
    # карте, ни в ответах гида.
    # «Парадная лестница» была такой записью с 29.07 по 31.08.2026 (п.5 баг-репорта
    # 28.07.2026); музей это решение отменил, зал снова публичный — разбор и отмена
    # в docs/staircase-hall-decision.md. Механизм остаётся: сегодня служебных
    # записей в каталоге нет ни одной, но для будущих он нужен.
    is_service: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Полнотекстовый вектор (B8): БД генерирует его сама (GENERATED ALWAYS ... STORED),
    # поэтому колонка read-only (Computed) и отложенная (deferred — не тянем tsvector в
    # обычные SELECT). Используется в crud.search / retrieval для ранжирования.
    search_vector: Mapped[Optional[str]] = mapped_column(
        TSVECTOR, Computed(_HALL_TSV, persisted=True), nullable=True, deferred=True
    )

    showcases: Mapped[List["Showcase"]] = relationship(
        back_populates="hall", cascade="all, delete-orphan", order_by="Showcase.showcase_number"
    )


class Showcase(Base):
    __tablename__ = "showcases"
    __table_args__ = (UniqueConstraint("hall_id", "showcase_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hall_id: Mapped[int] = mapped_column(ForeignKey("halls.id", ondelete="CASCADE"), nullable=False)
    # NULL — группа «не в витринах» (в путеводителе она отмечена пустым квадратом):
    # экспонаты зала, стоящие вне витрин. Такая группа в зале одна — гарантируется
    # частичным уникальным индексом (см. db/schema.sql).
    showcase_number: Mapped[Optional[int]] = mapped_column(Integer)
    name: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    hall: Mapped["Hall"] = relationship(back_populates="showcases")
    exhibits: Mapped[List["Exhibit"]] = relationship(
        back_populates="showcase", cascade="all, delete-orphan", order_by="Exhibit.id"
    )


class Exhibit(Base):
    __tablename__ = "exhibits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    showcase_id: Mapped[Optional[int]] = mapped_column(ForeignKey("showcases.id", ondelete="CASCADE"))
    label_slug: Mapped[Optional[str]] = mapped_column(String(100), unique=True)
    # Номер экспоната по путеводителю музея (B3): показывается перед названием в
    # каталоге. Строка (а не INT), т.к. в путеводителе встречаются номера вида «12а».
    exhibit_number: Mapped[Optional[str]] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Датировка строкой, дословно как в путеводителе: «1899–1903», «1880-е»,
    # «конец XIX — начало XX века». До 17.08.2026 поле было INT (нижняя граница
    # диапазона) и жило в паре с dating; теперь поле датировки одно (таска 17.08.2026).
    year_created: Mapped[Optional[str]] = mapped_column(Text)
    # Место создания дословно как в путеводителе: «Санкт-Петербург», «Швейцария,
    # Женева», «Московская губерния, Дмитровский уезд, село Горбуново». Колонку
    # завели 31.08.2026: музей просит показывать «дату создания И МЕСТО» рядом, а
    # парсер каталожной строки место извлекал давно (catalog_line.ParsedLine.
    # origin_place) — складывать его было некуда, и оно уходило только в отчёт.
    origin_place: Mapped[Optional[str]] = mapped_column(Text)
    master_name: Mapped[Optional[str]] = mapped_column(String(255))
    material: Mapped[Optional[str]] = mapped_column(String(255))
    # Техники из хвоста каталожной строки (после «;») — отдельно от материалов.
    techniques: Mapped[Optional[str]] = mapped_column(Text)
    short_description: Mapped[Optional[str]] = mapped_column(Text)
    short_description_spoken: Mapped[Optional[str]] = mapped_column(Text)
    raw_history: Mapped[Optional[str]] = mapped_column(Text)
    image_url: Mapped[Optional[str]] = mapped_column(Text)
    video_url: Mapped[Optional[str]] = mapped_column(Text)  # видео экспоната (B4/C22)
    model_3d_url: Mapped[Optional[str]] = mapped_column(Text)
    model_3d_embed: Mapped[Optional[str]] = mapped_column(Text)
    audio_url: Mapped[Optional[str]] = mapped_column(Text)
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Полнотекстовый вектор (B8) — см. пояснение у Hall.search_vector.
    search_vector: Mapped[Optional[str]] = mapped_column(
        TSVECTOR, Computed(_EXHIBIT_TSV, persisted=True), nullable=True, deferred=True
    )

    showcase: Mapped[Optional["Showcase"]] = relationship(back_populates="exhibits")
    images: Mapped[List["ExhibitImage"]] = relationship(
        back_populates="exhibit", cascade="all, delete-orphan", order_by="ExhibitImage.position"
    )


class ExhibitImage(Base):
    __tablename__ = "exhibit_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exhibit_id: Mapped[int] = mapped_column(ForeignKey("exhibits.id", ondelete="CASCADE"), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    alt: Mapped[Optional[str]] = mapped_column(String(255))
    width: Mapped[Optional[int]] = mapped_column(Integer)
    height: Mapped[Optional[int]] = mapped_column(Integer)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    exhibit: Mapped["Exhibit"] = relationship(back_populates="images")


class ExhibitQuestions(Base):
    """Кэш вопросов-подсказок ИИ-гида по экспонату (просьба заказчика 26.08.2026).

    Вопросы зависят только от карточки — ни от посетителя, ни от истории диалога,
    — а считались отдельным вызовом LLM на каждый рассказ и каждую реплику.
    Свежесть определяется ``source_hash`` (текст промпта + язык), а не временем:
    описание правят редко, а TTL вернул бы часть расхода обратно.
    """

    __tablename__ = "exhibit_questions"

    exhibit_id: Mapped[int] = mapped_column(
        ForeignKey("exhibits.id", ondelete="CASCADE"), primary_key=True
    )
    language: Mapped[str] = mapped_column(String(8), primary_key=True, default="ru")
    # Пул вопросов по порядку показа; наружу отдаётся срез под max_questions запроса.
    questions: Mapped[list] = mapped_column(JSONB, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GuideSession(Base):
    __tablename__ = "guide_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    context: Mapped[Optional[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_activity: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    messages: Mapped[List["GuideMessage"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="GuideMessage.id"
    )


class GuideMessage(Base):
    __tablename__ = "guide_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user','assistant','system')", name="guide_messages_role_chk"),
        CheckConstraint(
            "fail_reason IS NULL OR fail_reason IN "
            "('no_context','llm_refusal','llm_hedge','not_found','error')",
            name="guide_messages_fail_reason_chk",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("guide_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Смог ли гид ответить (§4 ТЗ 03.08.2026). Проставляется в момент генерации
    # ответа в app/routers/guide.py — постфактум отличить содержательный ответ от
    # отказа невозможно. NULL = признак не проставлен (сообщения до миграции
    # 2026-08-03; бэкфилл — scripts/backfill_unanswered.py).
    # Пишется на ОБЕ строки пары (вопрос и ответ): отчёт «вопросы без ответа»
    # читает role='user' без self-join.
    answered: Mapped[Optional[bool]] = mapped_column(Boolean)
    # Причина неудачи. Две из пяти — 'llm_refusal' и 'no_context' — кормят не
    # только отчёт, но и глобальную память отказов (`crud.exhibit_refused_questions`,
    # решение Д8): вопрос с такой причиной перестаёт предлагаться ВСЕМ посетителям
    # экспоната. Поэтому содержательный ответ с оговоркой «этого точно не знаю»
    # получает отдельную причину 'llm_hedge' — она в ту выборку не входит.
    fail_reason: Mapped[Optional[str]] = mapped_column(String(32))
    # Контекст вопроса — у какого экспоната/зала спрашивали (для привязки отчёта).
    exhibit_id: Mapped[Optional[int]] = mapped_column(Integer)
    hall_id: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped["GuideSession"] = relationship(back_populates="messages")


class Event(Base):
    """Событие телеметрии. Персональных данных не содержит (docs/analytics-privacy.md)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    exhibit_id: Mapped[Optional[int]] = mapped_column(Integer)
    hall_id: Mapped[Optional[int]] = mapped_column(Integer)
    showcase_id: Mapped[Optional[int]] = mapped_column(Integer)
    label_slug: Mapped[Optional[str]] = mapped_column(String(100))
    # Анонимный постоянный ID устройства (localStorage на фронте) — только для
    # метрики повторных визитов (§7): session_id живёт в sessionStorage, поэтому
    # новая вкладка = новый «человек». Ни с чем персональным не связывается.
    device_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    props: Mapped[Optional[dict]] = mapped_column(JSONB)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AnalyticsDaily(Base):
    """Плоский суточный срез агрегатов (§12).

    ``dimension_key`` = '' — метрика без разреза (NULL в составном PK недопустим).
    Джоб пересчёта идемпотентен: повторный запуск за дату перезаписывает строки.
    """

    __tablename__ = "analytics_daily"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    metric: Mapped[str] = mapped_column(String(64), primary_key=True)
    dimension_key: Mapped[str] = mapped_column(String(128), primary_key=True, default="")
    dimension_id: Mapped[Optional[int]] = mapped_column(Integer)
    value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AnalyticsReport(Base):
    """Кэш готового отчёта за период (§12).

    Отчёты с кластеризацией вопросов и разбором последовательностей событий в
    плоскую суточную схему не ложатся — ТЗ прямо разрешает под них отдельное
    хранилище. ``period_key`` = '<from>:<to>' с пустыми частями для открытых
    границ ('2026-07-01:2026-07-31', ':2026-07-31', ':').
    """

    __tablename__ = "analytics_reports"

    report: Mapped[str] = mapped_column(String(32), primary_key=True)
    period_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    period_from: Mapped[Optional[date]] = mapped_column(Date)
    period_to: Mapped[Optional[date]] = mapped_column(Date)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
