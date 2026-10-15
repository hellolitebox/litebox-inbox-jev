"""The digest as a Slack message: a Block Kit layout, its text version, and posting it.

`digest.build` decides what the digest says (`Digest`, every string already in the account's
language and escaped for mrkdwn); this module only lays it out, once as Block Kit `blocks` for
`chat.postMessage` and once as mrkdwn text, which `--dry-print` prints and which is sent instead
when Slack rejects the blocks, so a bad block never loses a digest.

Links are mrkdwn links, never url buttons: a url button still sends the app an interaction
payload, and an app without an interactivity Request URL (this one) shows an error on click.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

import httpx

log = logging.getLogger(__name__)

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
TIMEOUT_SECONDS = 20

# Block Kit limits (docs.slack.dev/reference/block-kit/blocks): blocks per message, characters
# of a header's plain text and of a section's mrkdwn text. Past them Slack answers
# `invalid_blocks`, so the layout caps itself first.
MAX_BLOCKS = 50
MAX_HEADER = 150
MAX_SECTION = 3000
# The table block cuts a long label with "…" past this, so the counts stay on a phone screen.
MAX_LABEL_WIDTH = 28
# Rows of a table block, its header and total included.
MAX_TABLE_ROWS = 100

# Slack's answers to blocks it can't take; the text version is sent instead.
INVALID_BLOCKS = frozenset({"invalid_blocks", "invalid_blocks_format"})

# A Block Kit object, as `chat.postMessage` takes it (JSON).
Block = dict[str, Any]

# The HTTP client `send_slack` posts with; a test gives `send_slack` its own.
client = partial(httpx.Client, timeout=TIMEOUT_SECONDS)


@dataclass(frozen=True)
class Group:
    """A titled list: one section block, one paragraph of the text version.

    `title` and `lines` are mrkdwn, already escaped. `hidden` items were left out (a cap) and
    are counted on a last line, with `link` (a Gmail search showing them all) when there is one.
    """

    title: str
    lines: Sequence[str]
    hidden: int = 0
    link: str | None = None


@dataclass(frozen=True)
class Totals:
    """The "what Jev did" table: a header row, `rows` (category label, count), the `review` row
    under them (not a label), the `total` row (their sum), and `after` it the rows that are not
    part of the sum. Category labels show as code, the other rows as plain text."""

    header: tuple[str, str]
    rows: Sequence[tuple[str, int]]
    total: tuple[str, int]
    after: Sequence[tuple[str, int]] = ()
    review: tuple[str, int] | None = None


@dataclass(frozen=True)
class Wording:
    """The layout's own words, in the digest's language: the "+N more" line (`more`, with
    `{n}`), the link on it (`see_all`) and the line for an empty table (`nothing`)."""

    more: str
    see_all: str
    nothing: str


@dataclass(frozen=True)
class Digest:
    """Everything the digest says, in order. Strings are mrkdwn and escaped, except `title`
    and `notification` (plain text) and the table's labels (raw text). `alerts` go first, right
    under the context: problems that stop inbox-jev soon, whatever else happened."""

    title: str
    context: str
    hero: str
    notification: str
    wording: Wording
    alerts: Sequence[str] = ()
    needs_you: Sequence[Group] = ()
    review: Sequence[str] = ()
    totals_title: str = ""
    totals: Totals | None = None
    notes: Sequence[str] = ()
    unsubscribes: Sequence[Group] = ()
    corrections: Group | None = None
    errors: Group | None = None
    footer: str = ""

    @property
    def _secondary(self) -> list[Group]:
        """The groups under the table: unsubscribes, corrections, errors."""
        tail = (self.corrections, self.errors)
        return [*self.unsubscribes, *(g for g in tail if g is not None)]

    @property
    def text(self) -> str:
        """The digest as one mrkdwn message: what `--dry-print` prints, and what is sent when
        Slack rejects the blocks."""
        parts = [f"*{self.title}*\n{self.context}", *self.alerts, self.hero]
        parts += [_group_text(g, self.wording) for g in self.needs_you]
        if self.review:
            parts.append("\n".join(self.review))
        table = _table_text(self.totals) if self.totals else self.wording.nothing
        parts.append("\n".join([self.totals_title, table, *self.notes]))
        parts += [_group_text(g, self.wording) for g in self._secondary]
        if self.footer:
            parts.append(f"_{self.footer}_")
        return "\n\n".join(parts)

    def blocks(self) -> list[Block]:
        """The digest as Block Kit blocks for `chat.postMessage`, at most MAX_BLOCKS."""
        blocks: list[Block] = [
            {"type": "header", "text": _plain(_cut(self.title, MAX_HEADER))},
            _context(self.context),
            *(_section(alert) for alert in self.alerts),
            _section(self.hero),
            *(_section(_fit(g, self.wording)) for g in self.needs_you),
        ]
        if self.review:
            blocks.append(_section("\n".join(self.review)))
        blocks += [{"type": "divider"}, _section(self.totals_title)]
        blocks.append(
            _table(self.totals, self.wording) if self.totals else _section(self.wording.nothing)
        )
        blocks += [_context(note) for note in self.notes]
        if self._secondary:
            blocks.append({"type": "divider"})
            blocks += [_section(_fit(g, self.wording)) for g in self._secondary]
        if self.footer:
            blocks += [{"type": "divider"}, _context(self.footer)]
        if len(blocks) > MAX_BLOCKS:  # only with very many keep categories: keep the top
            blocks = [*blocks[: MAX_BLOCKS - 1], _context("…")]
        return blocks


def _plain(text: str) -> Block:
    return {"type": "plain_text", "text": text, "emoji": True}


def _mrkdwn(text: str) -> Block:
    """A mrkdwn text object. `verbatim` keeps Slack from turning a plain `@here`, `@channel`,
    `#channel` or URL in it into a mention or link (it would by default, which `esc` can't stop);
    the digest's own `<url|label>` links still work."""
    return {"type": "mrkdwn", "text": text, "verbatim": True}


def _section(text: str) -> Block:
    return {"type": "section", "text": _mrkdwn(_cut(text, MAX_SECTION))}


def _context(text: str) -> Block:
    return {"type": "context", "elements": [_mrkdwn(text)]}


def _cut(text: str, limit: int) -> str:
    """`text` in at most `limit` characters, "…" last when it had to be cut."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _more(group: Group, hidden: int, wording: Wording) -> str:
    more = wording.more.format(n=hidden)
    return f"{more} · <{group.link}|{wording.see_all}>" if group.link else more


def _group_text(group: Group, wording: Wording) -> str:
    lines = [group.title, *group.lines]
    if group.hidden:
        lines.append(_more(group, group.hidden, wording))
    return "\n".join(lines)


def _fit(group: Group, wording: Wording) -> str:
    """`group` as one section's text: its lines while they fit in MAX_SECTION characters,
    the rest counted on the "+N more" line with the others left out."""
    lines = list(group.lines)
    while True:
        hidden = group.hidden + len(group.lines) - len(lines)
        tail = [_more(group, hidden, wording)] if hidden else []
        text = "\n".join([group.title, *lines, *tail])
        if len(text) <= MAX_SECTION or not lines:
            return text
        lines.pop()


def _cell_label(label: str) -> str:
    """A table label on one line, at most MAX_LABEL_WIDTH characters, never empty, a backtick
    shown as `'` (as in the mrkdwn code spans, and in the text version's code block, where it
    would close the block)."""
    return _cut(" ".join(label.split()).replace("`", "'"), MAX_LABEL_WIDTH) or "—"


def _raw(text: str) -> Block:
    return {"type": "raw_text", "text": text}


def _styled(text: str, style: str) -> Block:
    """A rich text cell showing `text` in one style (`bold`, `code`)."""
    words = {"type": "text", "text": text, "style": {style: True}}
    return {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [words]}]}


def _bold(text: str) -> Block:
    return _styled(text, "bold")


def _code(text: str) -> Block:
    """A category label's cell: a raw text cell can't be styled, so it is rich text in code."""
    return _styled(text, "code")


def _table(totals: Totals, wording: Wording) -> Block:
    """The native table block: labels left (category labels in code), counts right-aligned,
    the header row and the total in bold, the rows outside the sum under it. Raw text cells
    show their text as is: no mrkdwn, mention or link; rich text `text` elements don't parse
    mentions either. Past MAX_TABLE_ROWS the last category rows that fit become one "+N more"
    row with their sum, so the total still adds up."""
    labels = list(totals.rows)
    plain = [totals.review] if totals.review else []
    room = MAX_TABLE_ROWS - 2 - len(plain) - len(totals.after)  # the header and total rows
    if len(labels) > room:
        rest = labels[room - 1 :]
        labels = labels[: room - 1]
        plain.insert(0, (wording.more.format(n=len(rest)), sum(n for _, n in rest)))
    label, total = totals.total
    return {
        "type": "table",
        "column_settings": [{"is_wrapped": True}, {"align": "right"}],
        "rows": [
            [_bold(totals.header[0]), _bold(totals.header[1])],
            *([_code(_cell_label(label)), _raw(str(n))] for label, n in labels),
            *([_raw(_cell_label(label)), _raw(str(n))] for label, n in plain),
            [_bold(_cell_label(label)), _bold(str(total))],
            *([_raw(_cell_label(label)), _raw(str(n))] for label, n in totals.after),
        ],
    }


def _table_text(totals: Totals) -> str:
    """The table for the text version: monospaced in a code block, counts right-aligned and a
    rule above the total. A code block shows no mrkdwn but Slack still decodes `&amp;` there,
    so labels are escaped (`_cell_label` already made a backtick `'`)."""
    above = (*totals.rows, *([totals.review] if totals.review else []))
    cells = [(_cell_label(label), str(n)) for label, n in (*above, totals.total, *totals.after)]
    width = max(len(label) for label, _ in cells)
    digits = max(len(n) for _, n in cells)
    lines = [f"{label:<{width}}  {n:>{digits}}" for label, n in cells]
    lines.insert(len(above), "─" * (width + 2 + digits))
    body = "\n".join(lines).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"```\n{body}\n```"


class SlackError(RuntimeError):
    """Slack answered `ok: false`; `error` is its error code (e.g. `invalid_blocks`)."""

    def __init__(self, error: str, details: Sequence[str] = ()) -> None:
        super().__init__(f"Slack error: {error}")
        self.error = error
        self.details = list(details)


def _post(http: httpx.Client, token: str, payload: Block) -> None:
    resp = http.post(
        POST_MESSAGE_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={**payload, "unfurl_links": False, "unfurl_media": False},
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        details = (body.get("response_metadata") or {}).get("messages") or []
        raise SlackError(str(body.get("error")), [str(d) for d in details])


def send_slack(http: httpx.Client, token: str, user_id: str, digest: Digest) -> None:
    """Post `digest` to `user_id` as blocks, with its notification as the message text (what
    a push notification and a screen reader show). If Slack rejects the blocks, log a warning
    and post the text version instead; any other failure raises."""
    blocks = {"channel": user_id, "text": digest.notification, "blocks": digest.blocks()}
    try:
        _post(http, token, blocks)
    except SlackError as exc:
        if exc.error not in INVALID_BLOCKS:
            raise
        # Slack's details name the block and the rule it broke (a JSON pointer), not its text.
        log.warning(
            "digest blocks rejected by Slack: sending the text version",
            extra={"error": exc.error, "details": exc.details},
        )
        _post(http, token, {"channel": user_id, "text": digest.text})
