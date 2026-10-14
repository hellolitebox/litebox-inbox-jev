"""Schema upgrade for existing databases, SQLite settings and log filtering."""

import contextlib
import json
import logging
import sys
from unittest import mock

import httpx2
import pytest
from conftest import ROOT
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from typesafe_sdk import Noul, RetryPolicy, TypeSafeClient, TypeSafeError

from inbox_jev import db, logs
from inbox_jev.config import DigestLanguage, LogLevel, Mode, load_categories
from inbox_jev.db import SQLITE_BUSY_TIMEOUT_MS, Base, Message, MessageStatus, make_engine
from inbox_jev.digest import build
from inbox_jev.policy import DecisionSource
from inbox_jev.rules import RuleSource
from inbox_jev.unsubscribe import UnsubscribeMethod, UnsubscribeStatus


@pytest.fixture
def engines():
    """`make_engine` for a test: every engine it made is disposed when the test ends."""
    made = []

    def make(url):
        made.append(make_engine(url))
        return made[-1]

    yield make
    for engine in made:
        engine.dispose()


def test_sqlite_file_database_uses_wal_and_busy_timeout(tmp_path, engines):
    engine = engines(f"sqlite:///{tmp_path / 'app.db'}")
    for _ in range(2):  # every pooled connection, not just the first
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == SQLITE_BUSY_TIMEOUT_MS
        engine.dispose()


def test_in_memory_sqlite_still_works(engines):
    engine = engines("sqlite:///:memory:")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "memory"


def test_make_engine_adds_error_attempts_to_existing_rows(tmp_path, engines):
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

    engine = engines(url)
    with Session(engine) as s:
        assert s.scalars(select(Message)).one().error_attempts == 0


def test_make_engine_adds_missing_columns(tmp_path, engines):
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

    engine = engines(url)
    engines(url)  # idempotent
    with Session(engine) as s:
        msg = s.scalars(select(Message)).one()
        assert msg.decided_labels is None
        # Pre-column rows fall back to the needs_reply score in the digest.
        cfg = load_categories(ROOT / "categories.yaml")
        out = build(s, cfg, "me@acme.com", hours=10_000, language=DigestLanguage.ES)
    assert "Piden respuesta (1)" in out and "Old proposal" in out


def test_make_engine_adds_not_null_column_with_default(tmp_path, engines):
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

    engine = engines(url)
    engines(url)  # idempotent
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
    with pytest.raises(RuntimeError, match=r"messages\.restored is NOT NULL"):
        make_engine(url)


def test_make_engine_disposes_the_engine_when_the_upgrade_fails(tmp_path, monkeypatch):
    """A failed `_add_missing_columns` must not leave the engine's connections open."""
    made = []
    real_create_engine = db.create_engine

    def create(url):
        engine = real_create_engine(url)
        monkeypatch.setattr(engine, "dispose", mock.Mock(wraps=engine.dispose))
        made.append(engine)
        return engine

    def fail(engine):
        raise RuntimeError("upgrade failed")

    monkeypatch.setattr(db, "create_engine", create)
    monkeypatch.setattr(db, "_add_missing_columns", fail)
    with pytest.raises(RuntimeError, match="upgrade failed"):
        make_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    assert len(made) == 1
    made[0].dispose.assert_called_once_with()


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
    logs.setup(LogLevel.INFO)
    logs.setup(LogLevel.INFO)
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


def test_enum_members_are_stored_as_their_plain_value(engines):
    engine = engines("sqlite:///:memory:")
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


def test_run_context_tags_records_inside_it_and_an_explicit_run_id_wins():
    f = logs.RunIdFilter()
    outside = _record("before")
    assert f.filter(outside) and not hasattr(outside, "run_id")
    with logs.run_context(7):
        inside, explicit = _record("during"), _record("explicit")
        explicit.run_id = 3
        assert f.filter(inside) and f.filter(explicit)
        with logs.run_context(8):  # nested: the inner run, then back to the outer one
            nested = _record("nested")
            f.filter(nested)
        again = _record("again")
        f.filter(again)
    after = _record("after")
    f.filter(after)
    assert (inside.run_id, explicit.run_id, nested.run_id, again.run_id) == (7, 3, 8, 7)
    assert not hasattr(after, "run_id")


def test_setup_writes_the_run_id_in_the_json_line(monkeypatch, capsys):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", list(root.handlers))
    monkeypatch.setattr(root, "level", root.level)
    sdk = logging.getLogger("typesafe_sdk")
    monkeypatch.setattr(sdk, "filters", list(sdk.filters))
    monkeypatch.setattr(sdk, "level", sdk.level)
    logs.setup(LogLevel.INFO)
    with logs.run_context(42):
        logging.getLogger("inbox_jev.test").info("message processed", extra={"id": "m1"})
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert (line["msg"], line["id"], line["run_id"]) == ("message processed", "m1", 42)


class _MultiLineError(Exception):
    """An exception whose message ends with a line that looks like a type (`Subject: …`)."""


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ValueError("bad\nSubject: Your invoice"), "ValueError"),
        (_MultiLineError("x\nSubject: Your invoice"), f"{__name__}._MultiLineError"),
        (httpx2.ConnectError("boom\nSubject: x"), "httpx2.ConnectError"),
    ],
)
def test_json_formatter_writes_the_exact_exception_type(exc, expected):
    """`exc_type` is the class, not parsed from the traceback's last line (which, for a
    multi-line message, is the end of the message)."""
    try:
        raise exc
    except Exception:
        record = logging.getLogger("inbox_jev.test").makeRecord(
            "inbox_jev.test", logging.ERROR, __file__, 1, "run failed", (), sys.exc_info()
        )
    line = json.loads(logs.JsonFormatter().format(record))
    assert line["exc_type"] == expected
    assert "Subject" in line["exc"]  # the full traceback stays in the journal


def test_json_formatter_without_an_exception_has_no_exc_type():
    line = json.loads(logs.JsonFormatter().format(_record("fine")))
    assert "exc" not in line
    assert "exc_type" not in line
