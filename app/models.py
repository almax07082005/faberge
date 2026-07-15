"""ORM-модели SQLAlchemy 2.0 (соответствуют db/schema.sql)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime


class Base(DeclarativeBase):
    pass


class Hall(Base):
    __tablename__ = "halls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hall_number: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)
    level: Mapped[Optional[int]] = mapped_column(Integer)
    cover_image_url: Mapped[Optional[str]] = mapped_column(Text)
    is_temporary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    showcases: Mapped[List["Showcase"]] = relationship(
        back_populates="hall", cascade="all, delete-orphan", order_by="Showcase.showcase_number"
    )


class Showcase(Base):
    __tablename__ = "showcases"
    __table_args__ = (UniqueConstraint("hall_id", "showcase_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hall_id: Mapped[int] = mapped_column(ForeignKey("halls.id", ondelete="CASCADE"), nullable=False)
    showcase_number: Mapped[int] = mapped_column(Integer, nullable=False)
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
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    year_created: Mapped[Optional[int]] = mapped_column(Integer)
    master_name: Mapped[Optional[str]] = mapped_column(String(255))
    material: Mapped[Optional[str]] = mapped_column(String(255))
    short_description: Mapped[Optional[str]] = mapped_column(Text)
    short_description_spoken: Mapped[Optional[str]] = mapped_column(Text)
    raw_history: Mapped[Optional[str]] = mapped_column(Text)
    image_url: Mapped[Optional[str]] = mapped_column(Text)
    model_3d_url: Mapped[Optional[str]] = mapped_column(Text)
    model_3d_embed: Mapped[Optional[str]] = mapped_column(Text)
    audio_url: Mapped[Optional[str]] = mapped_column(Text)
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

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
    __table_args__ = (CheckConstraint("role IN ('user','assistant','system')", name="guide_messages_role_chk"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("guide_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped["GuideSession"] = relationship(back_populates="messages")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    exhibit_id: Mapped[Optional[int]] = mapped_column(Integer)
    hall_id: Mapped[Optional[int]] = mapped_column(Integer)
    label_slug: Mapped[Optional[str]] = mapped_column(String(100))
    props: Mapped[Optional[dict]] = mapped_column(JSONB)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
