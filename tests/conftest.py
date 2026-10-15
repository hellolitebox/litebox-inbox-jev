import json
import re
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest
from fakes import FakeClassifier, FakeGmail, email, gmail_factory
from sqlalchemy import event
from sqlalchemy.engine import Engine

from inbox_jev import slack, triage
from inbox_jev.classification import Classification
from inbox_jev.config import Account, DigestLanguage, Settings, load_categories
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


def in_language(account: Account, language: DigestLanguage) -> Account:
    """`account` with its digest in `language` (`digest.build` renders the account's own)."""
    return account.model_copy(update={"digest_language": language})


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


# The fields Block Kit documents for each block and object this layout uses
# (docs.slack.dev/reference/block-kit/blocks): anything else is a typo Slack would reject.
BLOCK_FIELDS = {
    "header": {"type", "text"},
    "context": {"type", "elements"},
    "section": {"type", "text"},
    "divider": {"type"},
    "table": {"type", "rows", "column_settings"},
}
CELL_TYPES = {"raw_text", "rich_text"}


def assert_mrkdwn(obj: slack.Block) -> None:
    """A mrkdwn text object, `verbatim` so Slack doesn't turn a plain `@here`, `@channel` or
    `#channel` taken from an email into a mention or a channel link."""
    assert obj["type"] == "mrkdwn" and obj["text"], obj
    assert obj.get("verbatim") is True, obj


# The styles a rich text `text` element takes (docs.slack.dev/reference/block-kit/blocks/
# rich-text-block): a raw_text cell has none, so a styled cell is rich text.
TEXT_STYLES = {"bold", "italic", "strike", "code"}


def cell_text(cell: slack.Block) -> str:
    """The text a table cell shows."""
    if cell["type"] == "raw_text":
        return str(cell["text"])
    return "".join(e["text"] for section in cell["elements"] for e in section["elements"])


def cell_style(cell: slack.Block) -> str | None:
    """A table cell's one text style, None for raw text."""
    if cell["type"] == "raw_text":
        return None
    (words,) = cell["elements"][0]["elements"]
    (style,) = words["style"]
    return str(style)


def assert_valid_cell(cell: slack.Block) -> None:
    """A table cell: raw text, or rich text of one section of non-empty, styled `text`s."""
    assert cell["type"] in CELL_TYPES, cell
    if cell["type"] == "raw_text":
        assert set(cell) == {"type", "text"} and cell["text"], cell
        return
    (section,) = cell["elements"]
    assert section["type"] == "rich_text_section", cell
    for element in section["elements"]:
        assert element["type"] == "text" and element["text"], cell
        style = element.get("style", {})
        assert set(style) <= TEXT_STYLES and all(v is True for v in style.values()), cell


def assert_valid_block_kit(blocks: list[slack.Block]) -> None:
    """`blocks` has Block Kit's shape and stays inside its documented limits."""
    json.dumps(blocks)  # plain JSON
    assert 0 < len(blocks) <= slack.MAX_BLOCKS
    assert sum(b["type"] == "table" for b in blocks) <= 1
    for block in blocks:
        kind = block["type"]
        assert set(block) <= BLOCK_FIELDS[kind], block
        if kind == "header":
            assert block["text"]["type"] == "plain_text"
            assert 0 < len(block["text"]["text"]) <= slack.MAX_HEADER
        elif kind == "section":
            assert_mrkdwn(block["text"])
            assert len(block["text"]["text"]) <= slack.MAX_SECTION
        elif kind == "context":
            assert 0 < len(block["elements"]) <= 10
            for element in block["elements"]:
                assert_mrkdwn(element)
        elif kind == "table":
            assert len(block["rows"]) <= slack.MAX_TABLE_ROWS == 100
            assert len(block["column_settings"]) <= 20
            for row in block["rows"]:
                assert 0 < len(row) <= 20
                for cell in row:
                    assert_valid_cell(cell)
            # "A single table's character count across all cells cannot exceed 10,000": the
            # cells' text, not their JSON (a styled rich text cell is ~120 characters of JSON).
            assert sum(len(cell_text(c)) for row in block["rows"] for c in row) <= 10_000
