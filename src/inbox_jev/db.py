"""SQLAlchemy models. Works with any DATABASE_URL (SQLite by default).

String columns that hold a StrEnum's values (commented with the enum, e.g. `mode`): assign the
members (a StrEnum is a str, so the stored text is the value) and compare against them; rows read
back are plain strings.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    ColumnDefault,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    literal,
    text,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

# Failed passes on one email before triage stops retrying it (see triage.ERROR_RETRY_BACKOFF).
MAX_ERROR_ATTEMPTS = 8


def now() -> datetime:
    return datetime.now(UTC)


class MessageStatus(StrEnum):
    """`messages.status`: the last pass over the email succeeded or failed."""

    DONE = "done"
    ERROR = "error"


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(320), index=True)
    mode: Mapped[str] = mapped_column(String(16))  # config.Mode
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
    # The iTIP method of its calendar part (`Email.calendar_method`: "REQUEST", "REPLY",
    # "CANCEL"…), None for any other email. The digest leaves calendar messages out of its lists
    # (ADR 0049). Rows stored before the column existed (2026-10-15) are None, read as "not a
    # calendar message": they are listed as before.
    calendar_method: Mapped[str | None] = mapped_column(String(32))
    # The sender was the list's own address (`signals.Signals.sender_is_list`): a correction on
    # it creates no sender rule (`rules.decided_per_email`, ADR 0051). Rows stored before the
    # column existed (2026-10-15) are None, read as "not a list's own address" until the email
    # is processed again.
    sender_is_list: Mapped[bool | None] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(16), default=MessageStatus.DONE)
    error: Mapped[str | None] = mapped_column(Text)
    # Consecutive failed passes; reset on success. Retries stop at MAX_ERROR_ATTEMPTS.
    # server_default too, so inserts that don't know the column (older code after a rollback,
    # raw SQL) still work on databases created by this version.
    error_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    source: Mapped[str | None] = mapped_column(String(16))  # policy.DecisionSource
    jev_category: Mapped[str | None] = mapped_column(String(64))  # raw model answer
    category: Mapped[str | None] = mapped_column(String(64), index=True)  # after policy
    confidence: Mapped[float | None] = mapped_column(Float)
    probabilities: Mapped[dict[str, float] | None] = mapped_column(JSON)
    needs_reply: Mapped[float | None] = mapped_column(Float)
    tags: Mapped[dict[str, float] | None] = mapped_column(JSON)
    low_confidence: Mapped[bool] = mapped_column(Boolean, default=False)
    # policy.Decision.unsubscribe_skipped_unverified, under its historical column name (kept to
    # avoid a migration): unsubscribe skipped, unverified sender; archived, not spammed.
    downgraded_to_spam: Mapped[bool] = mapped_column(Boolean, default=False)
    mode: Mapped[str] = mapped_column(String(16))  # config.Mode
    labels_added: Mapped[list[str] | None] = mapped_column(JSON)  # applied in Gmail
    decided_labels: Mapped[list[str] | None] = mapped_column(JSON)  # policy output, any mode
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
    method: Mapped[str] = mapped_column(String(16))  # unsubscribe.UnsubscribeMethod
    target: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))  # unsubscribe.UnsubscribeStatus
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
    source: Mapped[str] = mapped_column(String(16))  # rules.RuleSource
    hits: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class SentDomainScan(Base):
    """An account's sent-mail scan of one domain (`sent_scan.DomainScan`), kept between runs so a
    domain written to only at machine addresses is read in full a few emails per run."""

    __tablename__ = "sent_domain_scans"
    __table_args__ = (UniqueConstraint("account", "domain"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(320), index=True)
    domain: Mapped[str] = mapped_column(String(255))
    rules: Mapped[str] = mapped_column(String(64))  # sent_scan.scan_rules() when it started
    verdict: Mapped[str] = mapped_column(String(16))  # sent_scan.DomainVerdict
    verdict_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    newest_s: Mapped[int | None] = mapped_column(BigInteger)  # Unix seconds
    newest_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    oldest_s: Mapped[int | None] = mapped_column(BigInteger)
    oldest_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    reached_end: Mapped[bool] = mapped_column(Boolean, default=False)
    read: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


# How long a SQLite connection waits for another process's write lock before failing with
# "database is locked" (e.g. a manual command while the timer's run is writing).
SQLITE_BUSY_TIMEOUT_MS = 30_000


def _sqlite_pragmas(dbapi_conn: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    """WAL lets readers (digest, viewer) work while a run writes; busy_timeout waits for locks."""
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode = WAL")  # in-memory databases stay "memory"
    finally:
        cursor.close()


def make_engine(url: str) -> Engine:
    parsed = make_url(url)
    sqlite = parsed.get_backend_name() == "sqlite"
    if sqlite and parsed.database not in (None, "", ":memory:"):
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url)
    try:
        if sqlite:
            event.listen(engine, "connect", _sqlite_pragmas)
        Base.metadata.create_all(engine)
        _add_missing_columns(engine)
    except BaseException:
        engine.dispose()  # a failed upgrade must not leave its connections open
        raise
    return engine


def _add_missing_columns(engine: Engine) -> None:
    """Poor man's migration: create_all doesn't add new columns to existing tables.

    Nullable columns are added as-is. NOT NULL columns are added with ``DEFAULT <literal>``
    when the model declares a scalar default; otherwise startup fails with a clear error,
    since existing rows can't be backfilled. Anything more involved needs a real migration
    tool.

    Limitations: indexes and constraints declared on a new column (``index=True``,
    ``unique=True``) are not created. If two processes upgrade the same database at once,
    the loser's ALTER fails with "duplicate column"; that is tolerated by re-inspecting.
    """
    preparer = engine.dialect.identifier_preparer
    for table in Base.metadata.sorted_tables:
        existing = {c["name"] for c in inspect(engine).get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            col_type = col.type.compile(dialect=engine.dialect)
            ddl = (
                f"ALTER TABLE {preparer.format_table(table)} "
                f"ADD COLUMN {preparer.format_column(col)} {col_type}"
            )
            if not col.nullable:
                default_gen = col.default
                if not isinstance(default_gen, ColumnDefault) or not default_gen.is_scalar:
                    raise RuntimeError(
                        f"{table.name}.{col.name} is NOT NULL without a scalar default and is "
                        "missing from the database; migrate it manually"
                    )
                default = literal(default_gen.arg, col.type).compile(
                    dialect=engine.dialect, compile_kwargs={"literal_binds": True}
                )
                ddl += f" NOT NULL DEFAULT {default}"
            try:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
            except DBAPIError:
                # Another process may have added it first.
                if col.name not in {c["name"] for c in inspect(engine).get_columns(table.name)}:
                    raise


@contextmanager
def open_sessions(url: str) -> Iterator[sessionmaker[Session]]:
    """A session factory on `make_engine(url)`, whose engine is disposed on exit.

    Disposing closes the pooled connections, which would otherwise stay open until the
    interpreter exits (SQLite warns `ResourceWarning: unclosed database` on Python 3.13+).
    """
    engine = make_engine(url)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()
