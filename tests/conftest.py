import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from inbox_jev.classifier import Classification
from inbox_jev.config import Account, load_categories
from inbox_jev.signals import Signals

ROOT = Path(__file__).resolve().parents[1]

GMAIL_PASS = (
    "mx.google.com; dkim=pass header.i=@news.shop.com; spf=pass; dmarc=pass header.from=shop.com"
)


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
    base = dict(
        sender="promo@news.shop.com",
        sender_domain="news.shop.com",
        list_id="news.shop.com",
        unsubscribe_https=["https://shop.com/u/123"],
        unsubscribe_mailto=["mailto:unsub@shop.com?subject=unsubscribe"],
        one_click=True,
        dmarc_pass=True,
    )
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
