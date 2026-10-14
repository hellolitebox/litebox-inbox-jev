import re
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest
from fakes import FakeClassifier, FakeGmail, email, gmail_factory
from sqlalchemy import event
from sqlalchemy.engine import Engine

from inbox_jev import triage
from inbox_jev.classification import Classification
from inbox_jev.config import Account, Settings, load_categories
from inbox_jev.db import open_sessions
from inbox_jev.signals import Signals

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg():
    return load_categories(ROOT / "categories.yaml")


@pytest.fixture
def account():
    return Account(
        email="me@acme.com",
        allow_domains=["acme.com"],
        allow_senders=["vip@partner.com"],
        group_domains=["acme.com"],
    )


def make_signals(**kw) -> Signals:
    base = {
        "sender": "promo@news.shop.com",
        "sender_domain": "news.shop.com",
        "list_id": "news.shop.com",
        "unsubscribe_https": ("https://shop.com/u/123",),
        "unsubscribe_mailto": ("mailto:unsub@shop.com?subject=unsubscribe",),
        "one_click": True,
        "dmarc_pass": True,
    }
    base.update(kw)
    return Signals(**base)


def make_classification(category="marketing", confidence=0.97, needs_reply=0.1, tags=None):
    return Classification(
        category=category,
        confidence=confidence,
        probabilities={category: confidence},
        needs_reply=needs_reply,
        tags=tags or {},
    )


@contextmanager
def selects_from(engine: Engine, table: str) -> Iterator[list[str]]:
    """Collect the SELECT statements on `table` that `engine` runs inside the block."""
    found: list[str] = []
    pattern = re.compile(rf"^SELECT\s.*\sFROM {table}\b", re.DOTALL)

    def record(conn, cursor, statement, parameters, context, executemany):
        if pattern.match(statement):
            found.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield found
    finally:
        event.remove(engine, "before_cursor_execute", record)


@pytest.fixture
def make_sessions():
    """`db.open_sessions` for a test: every engine it opened is disposed when the test ends."""
    with ExitStack() as stack:
        yield lambda url: stack.enter_context(open_sessions(url))


@pytest.fixture
def setup(cfg, account, make_sessions):
    """Four inbox emails on a fake Gmail, a fake Jev and an in-memory DB: (ctx, gmail, account)."""
    emails = [
        email(
            "1",
            "Client <c@client.com>",
            "Proposal",
        ),
        email(
            "2",
            "Shop <promo@shop.com>",
            "50% off",
            list_unsubscribe="<mailto:u@shop.com>",
        ),
        email("3", "Bank <x@acme.com>", "Internal"),  # allowlisted domain
        email("4", "Weird <w@weird.biz>", "Maybe promo"),
    ]
    answers = {
        "Proposal": make_classification("important", 0.9, needs_reply=0.9),
        "50% off": make_classification("marketing", 0.97),
        "Maybe promo": make_classification("marketing", 0.6),
        # Allowlisted: Jev is asked only for the flags; its category is ignored.
        "Internal": make_classification("notification", 0.99),
    }
    gmail = FakeGmail(emails)
    settings = Settings(_env_file=None, typesafe_api_key="x", database_url="sqlite:///:memory:")
    sessions = make_sessions(settings.database_url)
    ctx = triage.Context(
        settings, cfg, sessions, gmail_factory(gmail), classifier=FakeClassifier(answers)
    )
    return ctx, gmail, account
