"""SQLAlchemy models. Works with any DATABASE_URL (SQLite by default)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(320), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("account", "gmail_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(320), index=True)
    gmail_id: Mapped[str] = mapped_column(String(64))
    thread_id: Mapped[str] = mapped_column(String(64))
    sender: Mapped[str] = mapped_column(String(320), index=True)
    from_header: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="done")  # done | error
    error: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(16))  # jev | rule | allowlist
    jev_category: Mapped[str | None] = mapped_column(String(64))  # raw model answer
    category: Mapped[str | None] = mapped_column(String(64), index=True)  # after policy
    confidence: Mapped[float | None] = mapped_column(Float)
    probabilities: Mapped[dict | None] = mapped_column(JSON)
    needs_reply: Mapped[float | None] = mapped_column(Float)
    tags: Mapped[dict | None] = mapped_column(JSON)
    low_confidence: Mapped[bool] = mapped_column(Boolean, default=False)
    downgraded_to_spam: Mapped[bool] = mapped_column(Boolean, default=False)
    mode: Mapped[str] = mapped_column(String(16))
    labels_added: Mapped[list | None] = mapped_column(JSON)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    spammed: Mapped[bool] = mapped_column(Boolean, default=False)
    corrected_category: Mapped[str | None] = mapped_column(String(64))
    correction_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    restored: Mapped[bool] = mapped_column(Boolean, default=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class Unsubscribe(Base):
    __tablename__ = "unsubscribes"
    __table_args__ = (UniqueConstraint("account", "list_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(320), index=True)
    list_key: Mapped[str] = mapped_column(String(320))
    sender: Mapped[str] = mapped_column(String(320))
    gmail_id: Mapped[str] = mapped_column(String(64))
    method: Mapped[str] = mapped_column(String(16))  # one_click | mailto | manual
    target: Mapped[str | None] = mapped_column(Text)
    # ok | failed | manual_pending | skipped
    status: Mapped[str] = mapped_column(String(16))
    http_status: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class SenderRule(Base):
    __tablename__ = "sender_rules"
    __table_args__ = (UniqueConstraint("account", "sender_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(320), index=True)
    sender_key: Mapped[str] = mapped_column(String(320))  # address or "@domain"
    category: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16))  # manual | correction | auto
    hits: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


def make_engine(url: str) -> Engine:
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:"):
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return engine


def make_session_factory(url: str) -> sessionmaker:
    return sessionmaker(make_engine(url), expire_on_commit=False)
