"""Schema upgrade for existing databases and log filtering."""

import logging

import pytest
from conftest import ROOT
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from inbox_jev import logs
from inbox_jev.config import load_categories
from inbox_jev.db import Base, Message, make_engine
from inbox_jev.digest import build


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
    sdk, httpx2 = logging.getLogger("typesafe_sdk"), logging.getLogger("httpx2")
    monkeypatch.setattr(sdk, "filters", list(sdk.filters))
    monkeypatch.setattr(sdk, "level", sdk.level)
    monkeypatch.setattr(httpx2, "level", httpx2.level)
    logs.setup("INFO")
    logs.setup("INFO")
    assert sdk.getEffectiveLevel() == logging.INFO
    assert sum(isinstance(f, logs.DropSdkSuccess) for f in sdk.filters) == 1
    assert httpx2.level == logging.WARNING
