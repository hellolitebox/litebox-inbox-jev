"""Daily digest per account, sent as a Slack DM (or printed).

It says first how many emails still need the user and lists them: before listing an email it
asks Gmail (read-only) whether it is still in the inbox, whether the user has replied in its
thread since and, for the To review count, whether it still has the review label. Then what Jev
did (a table of totals), unsubscribes, the corrections learned from the user and errors.

This module decides what the digest says (`build` returns a `slack.Digest`); `slack` lays it
out as Block Kit blocks or text and posts it.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import Protocol, TypedDict, TypeVar
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Account, Action, CategoriesConfig, DigestLanguage, Mode
from .db import MAX_ERROR_ATTEMPTS, Message, MessageStatus, Run, Unsubscribe, now
from .gmail import ThreadMessage
from .rules import decided_per_email
from .slack import Digest, Group, Totals, Wording
from .system_labels import INBOX, SENT
from .unsubscribe import UnsubscribeMethod, UnsubscribeStatus, describe_http_status

log = logging.getLogger(__name__)

MAX_ITEMS = 15
# The emails listed per "needs you" group: the digest is read on a phone, the rest is a link.
NEEDS_YOU_ITEMS = 10
MAX_SUBJECT = 60
MAX_SENDER = 40

_T = TypeVar("_T")


class ThreadReader(Protocol):
    """What the digest reads from Gmail: `gmail.Gmail.get_threads` and `labels` (read-only)."""

    def get_threads(
        self, thread_ids: Sequence[str]
    ) -> Mapping[str, Sequence[ThreadMessage] | None]: ...

    def labels(self) -> Mapping[str, str]: ...


class PluralForms(TypedDict):
    """A count's text for exactly one (`one`) and for any other number (`other`), with `{n}`.

    English and Spanish only use the singular for 1 (0 is plural in both); a language with more
    forms would need more keys here.
    """

    one: str
    other: str


class DigestMessages(TypedDict):
    """Every user-facing text of the digest, as `str.format` templates.

    Slack mrkdwn (`*bold*`, `_italic_`, emoji) is part of the template; values from an email are
    escaped with `esc` before they are formatted in, never here, and category labels shown as
    inline code with `code`. The digest's own parenthetical asides are italic, `_(…)_` (built
    with `aside` when their text isn't fixed). Counts that run into the text
    are `PluralForms`, picked with `plural`. `title`, the table's words and `notification` are
    plain text (a header, table cells, a push notification): no mrkdwn there.
    """

    title: str
    preview: str
    date: str
    weekdays: tuple[str, ...]  # Monday first, as `datetime.weekday`
    months: tuple[str, ...]  # January first
    context: str
    need_you: PluralForms
    inbox_zero: str
    processed_count: PluralForms
    just_now: str
    minutes_ago: str
    hours_ago: str
    days_ago: str
    totals_title: str
    table_category: str
    table_count: str
    to_review: str
    already_handled: str
    processed: str
    nothing_processed: str
    pending_check_failed: str
    no_subject: str
    needs_reply: str
    unsubscribed: str
    unsubscribe_manual: str
    method_one_click: str
    method_mailto: str
    method_manual: str
    link: str
    link_in_body: str
    unsubscribe_failed: str
    low_confidence: str
    low_confidence_dry_run: str
    review_in_gmail: str
    learned: str
    correction: str
    correction_accepted: str
    this_email_only: str
    more: str
    see_all: str
    errors: str
    error_emails: PluralForms
    failed_runs: PluralForms
    gave_up: str
    run_time: str
    run_times: str
    failed_run: str
    time_format: str
    footer: str


# One table per language; mypy checks that each has every key, and the tests that the
# placeholders match across languages. Add a language here and in `config.DigestLanguage`.
MESSAGES: dict[DigestLanguage, DigestMessages] = {
    DigestLanguage.EN: {
        "title": "📬 Inbox Jev · {date}",
        "preview": "[preview] ",
        "date": "{weekday}, {month} {day}",
        "weekdays": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
        "months": (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ),
        "context": "{account} · last {hours}h · mode: {modes}",
        "need_you": {"one": "{n} email needs you", "other": "{n} emails need you"},
        "inbox_zero": "Inbox zero — nothing needs you today",
        "processed_count": {"one": "{n} processed", "other": "{n} processed"},
        "just_now": "just now",
        "minutes_ago": "{n} min ago",
        "hours_ago": "{n}h ago",
        "days_ago": "{n}d ago",
        "totals_title": "*📊 What Jev did · last {hours}h*",
        "table_category": "Category",
        "table_count": "Emails",
        "to_review": "To review",
        "already_handled": "Already handled",
        "processed": "Processed",
        "nothing_processed": "_Nothing processed._",
        "pending_check_failed": (
            "_Couldn't check Gmail for what is already handled, so everything is listed._"
        ),
        "no_subject": "(no subject)",
        "needs_reply": "*⚡ Needs reply ({n})*",
        "unsubscribed": "*🚫 Unsubscribed ({n})*",
        "unsubscribe_manual": "*🖐 Manual unsubscribe ({n})*",
        "method_one_click": "one-click",
        "method_mailto": "email",
        "method_manual": "manual",
        "link": "link",
        "link_in_body": "link in the body",
        "unsubscribe_failed": "*❗ Unsubscribe failed ({n})*",
        "low_confidence": (
            "*🤔 To review: {n}* from the last {hours}h, still in the inbox with {label} · {link}"
        ),
        "low_confidence_dry_run": (
            "*🤔 To review: {n}* from the last {hours}h, still in the inbox"
            " _(dry run: not labeled)_"
        ),
        "review_in_gmail": "Review in Gmail",
        "learned": "*🧠 Corrections learned ({n})*",
        "correction": "• {sender}: {old} → {new}",
        "correction_accepted": "• {sender}: {new} _(suggestion accepted)_",
        "this_email_only": " _(this email only)_",
        "more": "+{n} more",
        "see_all": "see all in Gmail",
        "errors": "*⚠️ Errors*: {emails} _({gave_up} not retried again)_, {failed_runs}",
        "error_emails": {"one": "{n} email", "other": "{n} emails"},
        "failed_runs": {"one": "{n} failed run", "other": "{n} failed runs"},
        "gave_up": (
            "_Not retried again after {attempts} attempts (`inbox-jev retry-errors` retries them):_"
        ),
        "run_time": "{last} UTC",
        "run_times": "{n} runs, last {last} UTC",
        "failed_run": "• Failed run _({times})_: {error}",
        "time_format": "%m/%d %H:%M",
        "footer": "💡 Fix a label in Gmail and Inbox Jev learns it within the hour.",
    },
    DigestLanguage.ES: {
        "title": "📬 Inbox Jev · {date}",
        "preview": "[preview] ",
        "date": "{weekday} {day} {month}",
        "weekdays": ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"),
        "months": (
            "ene",
            "feb",
            "mar",
            "abr",
            "may",
            "jun",
            "jul",
            "ago",
            "sep",
            "oct",
            "nov",
            "dic",
        ),
        "context": "{account} · últimas {hours}h · modo: {modes}",
        "need_you": {"one": "{n} mail te necesita", "other": "{n} mails te necesitan"},
        "inbox_zero": "Inbox en cero — hoy nada te necesita",
        "processed_count": {"one": "{n} procesado", "other": "{n} procesados"},
        "just_now": "recién",
        "minutes_ago": "hace {n} min",
        "hours_ago": "hace {n} h",
        "days_ago": "hace {n} d",
        "totals_title": "*📊 Lo que hizo Jev · últimas {hours}h*",
        "table_category": "Categoría",
        "table_count": "Mails",
        "to_review": "A revisar",
        "already_handled": "Ya resueltos",
        "processed": "Procesados",
        "nothing_processed": "_Nada procesado._",
        "pending_check_failed": (
            "_No se pudo consultar Gmail para ver qué ya está resuelto: se lista todo._"
        ),
        "no_subject": "(sin asunto)",
        "needs_reply": "*⚡ Piden respuesta ({n})*",
        "unsubscribed": "*🚫 Desuscripto ({n})*",
        "unsubscribe_manual": "*🖐 Unsubscribe manual ({n})*",
        "method_one_click": "un clic",
        "method_mailto": "por mail",
        "method_manual": "manual",
        "link": "link",
        "link_in_body": "link en el cuerpo",
        "unsubscribe_failed": "*❗ Unsubscribe fallido ({n})*",
        "low_confidence": (
            "*🤔 A revisar: {n}* de las últimas {hours}h, siguen en el Inbox con {label} · {link}"
        ),
        "low_confidence_dry_run": (
            "*🤔 A revisar: {n}* de las últimas {hours}h, siguen en el Inbox"
            " _(dry run: sin etiqueta)_"
        ),
        "review_in_gmail": "Revisar en Gmail",
        "learned": "*🧠 Correcciones aprendidas ({n})*",
        "correction": "• {sender}: {old} → {new}",
        "correction_accepted": "• {sender}: {new} _(sugerencia aceptada)_",
        "this_email_only": " _(solo este mail)_",
        "more": "+{n} más",
        "see_all": "ver todos en Gmail",
        "errors": "*⚠️ Errores*: {emails} _({gave_up} sin más reintentos)_, {failed_runs}",
        "error_emails": {"one": "{n} mail", "other": "{n} mails"},
        "failed_runs": {"one": "{n} corrida fallida", "other": "{n} corridas fallidas"},
        "gave_up": (
            "_Sin más reintentos tras {attempts} intentos "
            "(`inbox-jev retry-errors` los reintenta):_"
        ),
        "run_time": "{last} UTC",
        "run_times": "{n} corridas, última {last} UTC",
        "failed_run": "• Corrida fallida _({times})_: {error}",
        "time_format": "%d/%m %H:%M",
        "footer": "💡 Corregí una etiqueta en Gmail e Inbox Jev lo aprende en menos de una hora.",
    },
}


def plural(forms: PluralForms, n: int) -> str:
    """`n` formatted into its singular form when it is exactly 1, else into the plural one."""
    return (forms["one"] if n == 1 else forms["other"]).format(n=n)


def gmail_link(account: str, gmail_id: str) -> str:
    return f"https://mail.google.com/mail/u/?authuser={account}#all/{gmail_id}"


def gmail_search_link(account: str, query: str) -> str:
    """A link to the Gmail search `query` in `account`'s mailbox."""
    mailbox = quote(account, safe="@")
    return f"https://mail.google.com/mail/u/{mailbox}/#search/{quote(query, safe='')}"


def label_query(label: str) -> str:
    """The Gmail search term for a label: Gmail writes `Jev/Review` as `label:jev-review`."""
    return "label:" + re.sub(r"[\s/]+", "-", label.strip().lower())


def esc(text: object, limit: int | None = None) -> str:
    """Escape text for Slack mrkdwn (&, <, >), truncated to `limit` characters first.

    Everything from an email (subject, sender, list id, URLs, errors) goes through this, so it
    can't open a link, mention (`<!channel>`) or break the message's own links.
    """
    raw = str(text)[:limit] if limit is not None else str(text)
    return raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def code(text: str) -> str:
    """`text` as inline code in mrkdwn: escaped, and a backtick in it, which would close the
    code span, becomes `'`. Nothing inside a code span is formatted, so an underscore or asterisk
    in it can't open an italic or bold. Used for category labels (from categories.yaml) and for
    a correction's sender, which comes from an email."""
    return "`" + esc(text.replace("`", "'")) + "`"


def aside(text: str) -> str:
    """The digest's own parenthetical note in italic mrkdwn, `_(text)_`: escaped, and an
    underscore in it, which would end the italic early, becomes `-`. Text from an email is never
    an aside: it stays as it is, escaped."""
    return "_(" + esc(text.replace("_", "-")) + ")_"


def _url(url: str) -> str:
    """`url` for a Slack link: escaped, and `|`, which ends the URL part of `<url|label>`,
    percent-encoded."""
    return esc(url).replace("|", "%7C")


def _link(url: str, label: str) -> str:
    """A Slack link to `url` showing `label` (mrkdwn, already escaped)."""
    return f"<{_url(url)}|{label}>"


def _line(t: DigestMessages, account: str, m: Message, suffix: str = "") -> str:
    subject = esc(m.subject or t["no_subject"], 90)
    return f"• {_link(gmail_link(account, m.gmail_id), subject)} — {esc(m.sender)}{suffix}"


def _is_calendar(m: Message) -> bool:
    """Whether `m` is a calendar message (an invitation, update, cancellation or response:
    `Message.calendar_method` is set). The digest counts it but never lists it: it stays in the
    inbox, labeled, and the calendar already shows it (ADR 0049). A row stored before the column
    existed has None and is listed as before."""
    return m.calendar_method is not None


def _needs_reply(cfg: CategoriesConfig, m: Message) -> bool:
    if m.decided_labels is None:  # rows stored before decided_labels existed
        return (m.needs_reply or 0) >= cfg.needs_reply.threshold
    return cfg.needs_reply.label in m.decided_labels


def build(
    session: Session,
    cfg: CategoriesConfig,
    account: Account,
    hours: int = 24,
    *,
    gmail: ThreadReader | None,
    preview: bool = False,
) -> Digest:
    """The digest for `account` over the last `hours`, in its `digest_language`.

    `gmail` is read to drop the emails already handled (`check_pending`); None (it couldn't be
    opened) or a failed read lists them all, with a line saying so. It has no default, so a
    caller can't skip the check by forgetting it. `preview` marks the title, for a test DM.
    """
    t = MESSAGES[account.digest_language]
    email = account.email
    at = now()
    since = at - timedelta(hours=hours)
    msgs = session.scalars(
        select(Message)
        .where(Message.account == email, Message.processed_at >= since)
        .order_by(Message.processed_at.desc())
    ).all()
    done = [m for m in msgs if m.status == MessageStatus.DONE]
    errors = [m for m in msgs if m.status == MessageStatus.ERROR]
    unsubs = session.scalars(
        select(Unsubscribe).where(Unsubscribe.account == email, Unsubscribe.attempted_at >= since)
    ).all()
    runs = session.scalars(select(Run).where(Run.account == email, Run.started_at >= since)).all()
    corrected = session.scalars(
        select(Message)
        .where(
            Message.account == email,
            Message.corrected_category.is_not(None),
            Message.correction_checked_at >= since,
        )
        .order_by(Message.correction_checked_at.desc(), Message.id.desc())
    ).all()

    modes = sorted({m.mode for m in done})
    low = [m for m in done if m.low_confidence]
    # Calendar messages count in the "What Jev did" table but are never listed (ADR 0049).
    listable = [m for m in done if not _is_calendar(m)]
    low_listed = [m for m in low if not _is_calendar(m)]
    kept = _kept_on_purpose(cfg, listable)
    listed = {id(m) for _, msgs in kept for m in msgs}
    reply = [m for m in listable if _needs_reply(cfg, m) and id(m) not in listed]
    pending = check_pending(
        gmail, [*(m for _, msgs in kept for m in msgs), *reply], low_listed, cfg.review_label
    )

    needs_you = []
    for label, kept_msgs in kept:
        if shown := pending.keep(kept_msgs):
            title = f"*🔔 {code(label)} ({len(shown)})*"
            needs_you.append(_needs_you(t, email, title, label, shown, at))
    if shown := pending.keep(reply):
        title = t["needs_reply"].format(n=len(shown))
        needs_you.append(_needs_you(t, email, title, cfg.needs_reply.label, shown, at))
    for_review = pending.keep_for_review(low_listed)
    waiting = sum(len(g.lines) + g.hidden for g in needs_you) + len(for_review)
    headline = plural(t["need_you"], waiting) if waiting else t["inbox_zero"]
    date = t["date"].format(
        weekday=t["weekdays"][at.weekday()], day=at.day, month=t["months"][at.month - 1]
    )
    mark = t["preview"] if preview else ""
    error_lines = _errors(t, email, errors, [r for r in runs if r.error])
    return Digest(
        title=mark + t["title"].format(date=date),
        context=t["context"].format(account=esc(email), hours=hours, modes=", ".join(modes) or "—"),
        hero=f"*{headline}*" if waiting else f"*{headline}* ✨",
        notification=f"{mark}{headline} · {plural(t['processed_count'], len(done))}",
        wording=Wording(more=t["more"], see_all=t["see_all"], nothing=t["nothing_processed"]),
        needs_you=needs_you,
        review=_low_confidence(t, cfg, email, hours, for_review),
        totals_title=t["totals_title"].format(hours=hours),
        totals=_totals(t, cfg, done, low, pending),
        notes=[t["pending_check_failed"]] if pending.failed else [],
        unsubscribes=_unsubscribe_sections(t, unsubs),
        corrections=_corrections(t, cfg, account, corrected),
        errors=Group(error_lines[0], error_lines[1:]) if error_lines else None,
        footer=t["footer"],
    )


def _needs_you(
    t: DigestMessages,
    account: str,
    title: str,
    label: str,
    msgs: Sequence[Message],
    at: datetime,
) -> Group:
    """One "needs you" group: its first NEEDS_YOU_ITEMS emails, the rest counted with a link to
    `label`'s emails still in the inbox."""
    return Group(
        title,
        [_item(t, account, m, at) for m in msgs[:NEEDS_YOU_ITEMS]],
        hidden=max(0, len(msgs) - NEEDS_YOU_ITEMS),
        link=_url(gmail_search_link(account, f"{label_query(label)} in:inbox")),
    )


def _item(t: DigestMessages, account: str, m: Message, at: datetime) -> str:
    """An email to act on: its subject in bold, linking to it; under it who sent it and when."""
    subject = esc(_shorten(m.subject or t["no_subject"], MAX_SUBJECT))
    who = esc(_shorten(parseaddr(m.from_header or "")[0].strip() or m.sender, MAX_SENDER))
    link = _link(gmail_link(account, m.gmail_id), subject)
    return f"• *{link}*\n      {who} · {_ago(t, m.received_at or m.processed_at, at)}"


def _shorten(text: str, limit: int) -> str:
    """`text` on one line, in at most `limit` characters, "…" last when it had to be cut."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _ago(t: DigestMessages, when: datetime, at: datetime) -> str:
    """How long before `at` `when` was, in minutes, hours (under two days) or days."""
    if when.tzinfo is None:  # SQLite gives back naive datetimes, stored in UTC
        when = when.replace(tzinfo=UTC)
    minutes = max(0, int((at - when).total_seconds() // 60))
    if minutes < 1:
        return t["just_now"]
    if minutes < 60:
        return t["minutes_ago"].format(n=minutes)
    if minutes < 48 * 60:
        return t["hours_ago"].format(n=minutes // 60)
    return t["days_ago"].format(n=minutes // (24 * 60))


@dataclass(frozen=True)
class Pending:
    """Which of the checked emails still need the user (`check_pending`).

    `gmail_ids` are the ones still pending in their thread (`is_pending`), what the listed
    sections keep; `review_ids` the low-confidence ones still left for review
    (`is_left_for_review`), what the Low-confidence line counts. Both are None when the check
    couldn't be made: then every email counts as pending.
    """

    gmail_ids: frozenset[str] | None
    review_ids: frozenset[str] | None
    handled: int = 0

    @property
    def failed(self) -> bool:
        return self.gmail_ids is None

    def keep(self, msgs: Sequence[Message]) -> list[Message]:
        """`msgs` without the ones already handled (all of them when the check failed)."""
        return _keep(msgs, self.gmail_ids)

    def keep_for_review(self, low: Sequence[Message]) -> list[Message]:
        """The low-confidence `low` still left for review (all of them when the check failed)."""
        return _keep(low, self.review_ids)


def _keep(msgs: Sequence[Message], ids: frozenset[str] | None) -> list[Message]:
    return list(msgs) if ids is None else [m for m in msgs if m.gmail_id in ids]


def check_pending(
    gmail: ThreadReader | None,
    listed: Sequence[Message],
    low: Sequence[Message],
    review_label: str,
) -> Pending:
    """Which of the emails the sections would list (`listed`) and of the low-confidence ones
    the Low-confidence line would count (`low`) are still pending, from one batched read of
    their threads (and one read of the label list, for `review_label`'s id, when a low-confidence
    email should have it).

    The review label only matters for the Low-confidence line: an email also listed elsewhere
    (Needs reply) stays there until it leaves the inbox or is replied to. An email counts as
    handled only when no section that would show it still does.

    A failed read (token, Gmail API) doesn't fail the digest: it is logged and every email
    counts as pending (`Pending.failed`), so the digest lists them as before.
    """
    unique = {m.gmail_id: m for m in (*listed, *low)}
    if not unique:
        return Pending(frozenset(), frozenset())
    if gmail is None:
        return Pending(None, None)
    try:
        threads = gmail.get_threads(sorted({m.thread_id for m in unique.values()}))
        review_id = (
            gmail.labels().get(review_label) if any(_labeled_for_review(m) for m in low) else None
        )
    except Exception as exc:  # any failure only costs the check, never the digest
        log.warning(
            "digest pending check failed: listing every email",
            exc_info=True,
            extra={"emails": len(unique), "error_type": type(exc).__name__},
        )
        return Pending(None, None)
    ids = frozenset(i for i, m in unique.items() if is_pending(m, threads.get(m.thread_id)))
    review_ids = frozenset(
        m.gmail_id
        for m in low
        if is_left_for_review(m, threads.get(m.thread_id), review_label_id=review_id)
    )
    shown = (ids & {m.gmail_id for m in listed}) | review_ids
    return Pending(ids, review_ids, handled=len(unique) - len(shown))


def _own(m: Message, thread: Sequence[ThreadMessage] | None) -> ThreadMessage | None:
    """`m` itself in its `thread`, or None when it was deleted."""
    return next((x for x in thread or () if x.id == m.gmail_id), None)


def is_pending(m: Message, thread: Sequence[ThreadMessage] | None) -> bool:
    """Whether `m` still needs the user: it is still in the inbox and no message of its thread
    that I sent is dated after it. A deleted email or thread is not pending."""
    own = _own(m, thread)
    if own is None or INBOX not in own.label_ids:
        return False
    return not any(
        SENT in x.label_ids and x.internal_date_ms > own.internal_date_ms for x in thread or ()
    )


def is_left_for_review(
    m: Message, thread: Sequence[ThreadMessage] | None, *, review_label_id: str | None
) -> bool:
    """Whether the low-confidence `m` is still left for review: it is pending (`is_pending`)
    and, if it was labeled for review (`_labeled_for_review`), it still has that label
    (`review_label_id`, None when the label doesn't exist): removing it accepts or corrects the
    suggestion. It says nothing about a reply, so only the Low-confidence line uses it."""
    if not is_pending(m, thread):
        return False
    own = _own(m, thread)
    return not _labeled_for_review(m) or (
        own is not None and review_label_id is not None and review_label_id in own.label_ids
    )


def _section(title: str, items: Sequence[_T], line: Callable[[_T], str]) -> list[str]:
    """`title` and one `line` per item (at most MAX_ITEMS), or nothing when there are none."""
    return [title, *(line(item) for item in items[:MAX_ITEMS])] if items else []


def _label(cfg: CategoriesConfig, key: str | None) -> str:
    """The category `key`'s label, or the key itself for one no longer in categories.yaml."""
    category = cfg.by_key.get(key) if key is not None else None
    return category.label if category is not None else str(key)


def _totals(
    t: DigestMessages,
    cfg: CategoriesConfig,
    done: list[Message],
    low: list[Message],
    pending: Pending,
) -> Totals | None:
    """The "what Jev did" table: emails processed per category (most first, ties by label;
    low-confidence ones apart as To review), how many of the ones the digest would list were
    already handled (under the total, not part of it). None when nothing was processed."""
    if not done:
        return None
    counts = Counter(_label(cfg, m.category) for m in done if not m.low_confidence)
    rows = sorted(counts.items(), key=lambda row: (-row[1], row[0]))
    review = (t["to_review"], len(low)) if low else None
    # Not part of the total: it counts emails the digest would have listed, not processed ones.
    after = [(t["already_handled"], pending.handled)] if pending.handled else []
    header = (t["table_category"], t["table_count"])
    return Totals(header, rows, (t["processed"], len(done)), after, review=review)


def _kept_on_purpose(cfg: CategoriesConfig, done: list[Message]) -> list[tuple[str, list[Message]]]:
    """(label, emails) of each category that stays in the inbox on purpose besides "important",
    e.g. automated emails that need an action: each gets its own section, and the emails listed
    there aren't repeated under Needs Reply."""
    kept = []
    for cat in cfg.categories:
        if cat.action != Action.KEEP or cat.key == "important":
            continue
        msgs = [m for m in done if m.category == cat.key and not m.low_confidence]
        if msgs:
            kept.append((cat.label, msgs))
    return kept


def _corrections(
    t: DigestMessages, cfg: CategoriesConfig, account: Account, corrected: Sequence[Message]
) -> Group | None:
    """The corrections picked up in the period, newest first: sender, the category it had and
    the one the user gave it (or that the user accepted the suggestion). One on an email
    decided on its own (`rules.decided_per_email`: a sender in decide_each_email, or an email
    sent as a mailing list's own address) made no sender rule (`corrections.record`), which the
    line says. At most MAX_ITEMS, then how many more; None when there are none.

    The sender is code, like the labels: an underscore at the start of an address
    (`_bounce@x.example`) would otherwise open an italic that the line's aside closes."""

    def line(m: Message) -> str:
        sender = code(m.sender)
        new = code(_label(cfg, m.corrected_category))
        if m.corrected_category == m.category:
            text = t["correction_accepted"].format(sender=sender, new=new)
        else:
            old = code(_label(cfg, m.category))
            text = t["correction"].format(sender=sender, old=old, new=new)
        per_email = decided_per_email(account, m.sender, sender_is_list=bool(m.sender_is_list))
        return text + (t["this_email_only"] if per_email else "")

    return _group(t["learned"], corrected, line)


def _unsubscribe_sections(t: DigestMessages, unsubs: Sequence[Unsubscribe]) -> list[Group]:
    """Unsubscribed lists, the ones waiting for a manual unsubscribe, and the failed ones: a
    group each, when there are any, of at most MAX_ITEMS lines, then how many more."""
    ok = [u for u in unsubs if u.status == UnsubscribeStatus.OK]
    pending = [u for u in unsubs if u.status == UnsubscribeStatus.MANUAL_PENDING]
    failed = [u for u in unsubs if u.status == UnsubscribeStatus.FAILED]

    def manual(u: Unsubscribe) -> str:
        return f"• {esc(u.list_key)} — " + (
            _link(u.target, t["link"]) if u.target else t["link_in_body"]
        )

    groups = [
        _group(t["unsubscribed"], ok, lambda u: f"• {esc(u.list_key)} {aside(_method(t, u))}"),
        _group(t["unsubscribe_manual"], pending, manual),
        _group(
            t["unsubscribe_failed"],
            failed,
            lambda u: f"• {esc(u.list_key)}: {esc(_unsubscribe_error(u), 160)}",
        ),
    ]
    return [g for g in groups if g is not None]


def _method(t: DigestMessages, u: Unsubscribe) -> str:
    """How `u` unsubscribed, in words (`one-click`, not the stored `one_click`); a method this
    version doesn't know shows as stored."""
    match u.method:
        case UnsubscribeMethod.ONE_CLICK:
            return t["method_one_click"]
        case UnsubscribeMethod.MAILTO:
            return t["method_mailto"]
        case UnsubscribeMethod.MANUAL:
            return t["method_manual"]
    return u.method


def _group(title: str, items: Sequence[_T], line: Callable[[_T], str]) -> Group | None:
    """`title` (with `{n}`, the count) over one `line` per item, at most MAX_ITEMS, then how
    many more; None when there are none."""
    if not items:
        return None
    return Group(
        title.format(n=len(items)),
        [line(item) for item in items[:MAX_ITEMS]],
        hidden=max(0, len(items) - MAX_ITEMS),
    )


def _unsubscribe_error(u: Unsubscribe) -> str:
    """A failed unsubscribe's reason for the digest: its error, or `HTTP <status> <reason>` when
    the row has a status but its error doesn't start with one. Rows stored before the error was
    `unsubscribe.describe_http_failure` hold the raw response body, sender-controlled text (a
    JSON object, an HTML page), so it is not shown."""
    if u.http_status is None:
        return u.error or ""
    status = describe_http_status(u.http_status)
    return u.error if u.error and u.error.startswith(status) else status


def _labeled_for_review(m: Message) -> bool:
    """Whether the pass that decided `m` gave it the review label: a low-confidence email in a
    mode that labels (`dry_run` changes nothing in Gmail)."""
    return bool(m.low_confidence) and m.mode != Mode.DRY_RUN


def _low_confidence(
    t: DigestMessages, cfg: CategoriesConfig, account: str, hours: int, low: list[Message]
) -> list[str]:
    """How many pending low-confidence emails of the last `hours` still have the review label,
    with a link to that Gmail search; and, apart, how many `dry_run` left unlabeled (no link:
    the search would not find them)."""
    labeled = sum(1 for m in low if _labeled_for_review(m))
    out = []
    if labeled:
        query = f"{label_query(cfg.review_label)} in:inbox"
        link = _link(gmail_search_link(account, query), t["review_in_gmail"])
        out.append(
            t["low_confidence"].format(
                n=labeled, hours=hours, label=code(cfg.review_label), link=link
            )
        )
    if unlabeled := len(low) - labeled:
        out.append(t["low_confidence_dry_run"].format(n=unlabeled, hours=hours))
    return out


def _errors(
    t: DigestMessages, account: str, errors: list[Message], failed_runs: list[Run]
) -> list[str]:
    """Failed emails and runs: one line per distinct run error, then the emails that gave up."""
    if not (errors or failed_runs):
        return []
    gave_up = [m for m in errors if (m.error_attempts or 0) >= MAX_ERROR_ATTEMPTS]
    out = [
        t["errors"].format(
            emails=plural(t["error_emails"], len(errors)),
            gave_up=len(gave_up),
            failed_runs=plural(t["failed_runs"], len(failed_runs)),
        ),
        *_failed_runs(t, failed_runs),
    ]
    out += _section(
        t["gave_up"].format(attempts=MAX_ERROR_ATTEMPTS),
        gave_up,
        lambda m: _line(t, account, m, f": {esc(m.error or '', 80)}"),
    )
    return out


def _failed_runs(t: DigestMessages, runs: list[Run]) -> list[str]:
    """One line per distinct run error (a dead token fails every run the same way)."""
    by_error: dict[str, list[Run]] = {}
    for r in sorted(runs, key=lambda r: r.started_at):
        by_error.setdefault(r.error or "", []).append(r)
    lines = []
    for error, group in by_error.items():
        last = group[-1].started_at.strftime(t["time_format"])
        times = (
            t["run_times"].format(n=len(group), last=last)
            if len(group) > 1
            else t["run_time"].format(last=last)
        )
        lines.append(t["failed_run"].format(times=times, error=esc(error, 300)))
    return lines[:MAX_ITEMS]
