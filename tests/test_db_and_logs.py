"""Schema upgrade for existing databases, SQLite settings and log filtering."""

import contextlib
import logging

import httpx2
import pytest
from conftest import ROOT
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from typesafe_sdk import Noul, RetryPolicy, TypeSafeClient, TypeSafeError

from inbox_jev import logs
from inbox_jev.config import Mode, load_categories
from inbox_jev.db import SQLITE_BUSY_TIMEOUT_MS, Base, Message, MessageStatus, make_engine
from inbox_jev.digest import build
from inbox_jev.policy import DecisionSource
from inbox_jev.rules import RuleSource
from inbox_jev.unsubscribe import UnsubscribeMethod, UnsubscribeStatus


def test_sqlite_file_database_uses_wal_and_busy_timeout(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'app.db'}")
    for _ in range(2):  # every pooled connection, not just the first
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == SQLITE_BUSY_TIMEOUT_MS
        engine.dispose()


def test_in_memory_sqlite_still_works():
    engine = make_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "memory"


def test_make_engine_adds_error_attempts_to_existing_rows(tmp_path):
    """Deploy story for `messages.error_attempts`: old rows get 0, so they are retried as before."""
    url = f"sqlite:///{tmp_path / 'old.db'}"
    old = create_engine(url)
    Base.metadata.create_all(old)
    with old.begin() as conn:
        conn.execute(text("ALTER TABLE messages DROP COLUMN error_attempts"))
        conn.execute(
            text(
                "INSERT INTO messages (account, gmail_id, thread_id, sender, from_header, subject,"
                " status, mode, low_confidence, downgraded_to_spam, archived, spammed, restored,"
                " processed_at) VALUES ('me@acme.com', '1', '', '', '', '', 'error', 'act',"
                " 0, 0, 0, 0, 0, CURRENT_TIMESTAMP)"
            )
        )
    old.dispose()

    engine = make_engine(url)
    with Session(engine) as s:
        assert s.scalars(select(Message)).one().error_attempts == 0


def test_make_engine_adds_missing_columns(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    old = create_engine(url)
    Base.metadata.create_all(old)
    with old.begin() as conn:
        conn.execute(text("ALTER TABLE messages DROP COLUMN decided_labels"))
        conn.execute(
            text(
                "INSERT INTO messages (account, gmail_id, thread_id, sender, from_header, subject,"
                " status, mode, needs_reply, low_confidence, downgraded_to_spam, archived,"
                " spammed, restored, processed_at)"
                " VALUES ('me@acme.com', '1', 't1', 'c@client.com', '', 'Old proposal', 'done',"
                " 'act', 0.9, 0, 0, 0, 0, 0, CURRENT_TIMESTAMP)"
            )
        )
    old.dispose()

    engine = make_engine(url)
    make_engine(url)  # idempotent
    with Session(engine) as s:
        msg = s.scalars(select(Message)).one()
        assert msg.decided_labels is None
        # Pre-column rows fall back to the needs_reply score in the digest.
        out = build(s, load_categories(ROOT / "categories.yaml"), "me@acme.com", hours=10_000)
    assert "Piden respuesta (1)" in out and "Old proposal" in out


def test_make_engine_adds_not_null_column_with_default(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    old = create_engine(url)
    Base.metadata.create_all(old)
    with old.begin() as conn:
        conn.execute(text("ALTER TABLE messages DROP COLUMN restored"))
        conn.execute(
            text(
                "INSERT INTO messages (account, gmail_id, thread_id, sender, from_header, subject,"
                " status, mode, low_confidence, downgraded_to_spam, archived, spammed,"
                " processed_at) VALUES ('me@acme.com', '1', 't1', 'c@client.com', '', 's',"
                " 'done', 'act', 0, 0, 0, 0, CURRENT_TIMESTAMP)"
            )
        )
    old.dispose()

    engine = make_engine(url)
    make_engine(url)  # idempotent
    with Session(engine) as s:
        assert s.scalars(select(Message)).one().restored is False


def test_make_engine_rejects_not_null_column_without_default(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    old = create_engine(url)
    Base.metadata.create_all(old)
    with old.begin() as conn:
        conn.execute(text("ALTER TABLE messages DROP COLUMN restored"))
    old.dispose()

    # Simulate a NOT NULL column whose default isn't a scalar (e.g. a callable).
    col = Base.metadata.tables["messages"].c.restored
    monkeypatch.setattr(col, "default", None)
    with pytest.raises(RuntimeError, match="messages.restored is NOT NULL"):
        make_engine(url)


def _record(msg, *args, level=logging.INFO):
    return logging.LogRecord("typesafe_sdk", level, __file__, 1, msg, args, None)


def test_sdk_filter_drops_only_success_lines():
    f = logs.DropSdkSuccess()
    ok = "%s %s <- %s in %.0fms (request %s)"
    assert not f.filter(_record(ok, "POST", "https://x", 200, 12.0, "req_1"))
    assert f.filter(_record(ok, "POST", "https://x", 503, 12.0, "req_1"))
    assert f.filter(_record("%s %s retry %s", "POST", "https://x", 1))
    assert f.filter(_record("%s %s <- %s", "POST", "https://x", "ConnectError"))


def test_setup_keeps_sdk_at_info_with_filter(monkeypatch):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", list(root.handlers))
    monkeypatch.setattr(root, "level", root.level)
    sdk, httpx2_log = logging.getLogger("typesafe_sdk"), logging.getLogger("httpx2")
    monkeypatch.setattr(sdk, "filters", list(sdk.filters))
    monkeypatch.setattr(sdk, "level", sdk.level)
    monkeypatch.setattr(httpx2_log, "level", httpx2_log.level)
    logs.setup("INFO")
    logs.setup("INFO")
    assert sdk.getEffectiveLevel() == logging.INFO
    assert sum(isinstance(f, logs.DropSdkSuccess) for f in sdk.filters) == 1
    assert httpx2_log.level == logging.WARNING


def test_sdk_filter_matches_the_installed_sdk_log_records(caplog, monkeypatch):
    """Send real requests through typesafe_sdk (mock transport, no network) and filter its logs.

    `DropSdkSuccess` matches the SDK's internal "<- status in Nms" format; if an SDK update
    changes it, this fails instead of the success lines silently flooding the journal.
    """
    sdk = logging.getLogger("typesafe_sdk")
    monkeypatch.setattr(sdk, "filters", [])  # let caplog see what the SDK emits
    statuses = iter([200, 503])

    def handler(request):
        return httpx2.Response(next(statuses), json={}, headers={"x-typesafe-request-id": "r1"})

    client = TypeSafeClient(
        api_key="test-key",
        model="test-model",
        transport=httpx2.MockTransport(handler),
        retry=RetryPolicy(max_retries=0),
    )
    with client, caplog.at_level(logging.INFO, logger="typesafe_sdk"):
        for _ in range(2):
            with contextlib.suppress(TypeSafeError):  # `{}` isn't a valid answer; only logs matter
                client.system_one({"subject": "hi"}, {"q": Noul(instructions="Is it?")})

    replies = [r for r in caplog.records if r.name == "typesafe_sdk" and " <- " in r.getMessage()]
    assert [r.args[2] for r in replies] == [200, 503]
    f = logs.DropSdkSuccess()
    assert [f.filter(r) for r in replies] == [False, True]  # success dropped, error kept


@pytest.mark.parametrize(
    "enum, stored",
    [
        (MessageStatus, {"done", "error"}),
        (DecisionSource, {"jev", "rule", "allowlist"}),
        (UnsubscribeMethod, {"one_click", "mailto", "manual"}),
        (UnsubscribeStatus, {"ok", "failed", "manual_pending", "skipped"}),
        (RuleSource, {"manual", "correction", "auto"}),
        (Mode, {"dry_run", "label", "act"}),
    ],
)
def test_stored_enum_values_never_change(enum, stored):
    """These strings are in deployed databases: renaming a value needs a data migration."""
    assert {member.value for member in enum} == stored


def test_enum_members_are_stored_as_their_plain_value():
    engine = make_engine("sqlite:///:memory:")
    with Session(engine) as session:
        session.add(
            Message(
                account="me@x.com",
                gmail_id="1",
                thread_id="t1",
                sender="a@b.com",
                status=MessageStatus.ERROR,
                source=DecisionSource.ALLOWLIST,
                mode=Mode.DRY_RUN,
            )
        )
        session.commit()
        raw = session.execute(text("SELECT status, source, mode FROM messages")).one()
    assert tuple(raw) == ("error", "allowlist", "dry_run")
    assert all(type(value) is str for value in raw)
