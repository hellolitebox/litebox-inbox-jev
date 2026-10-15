"""Daily digest per account, sent as a Slack DM (or printed).

It lists only what still needs the user: before listing an email it asks Gmail (read-only)
whether it is still in the inbox, whether the user has replied in its thread since and, for the
low-confidence count, whether it still has the review label.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol, TypedDict, TypeVar
from urllib.parse import quote

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Action, CategoriesConfig, DigestLanguage, Mode
from .db import MAX_ERROR_ATTEMPTS, Message, MessageStatus, Run, Unsubscribe, now
from .gmail import ThreadMessage
from .system_labels import INBOX, SENT
from .unsubscribe import UnsubscribeStatus, describe_http_status

log = logging.getLogger(__name__)

MAX_ITEMS = 15

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
    escaped with `esc` before they are formatted in, never here. Counts that run into the text
    are `PluralForms`, picked with `plural`.
    """

    header: str
    processed: PluralForms
    summary: str
    handled: PluralForms
    pending_check_failed: str
    nothing: str
    no_subject: str
    needs_reply: str
    unsubscribed: str
    unsubscribe_manual: str
    link: str
    link_in_body: str
    unsubscribe_failed: str
    low_confidence: str
    low_confidence_dry_run: str
    see_in_gmail: str
    learned: str
    errors: str
    error_emails: PluralForms
    failed_runs: PluralForms
    gave_up: str
    run_time: str
    run_times: str
    failed_run: str
    time_format: str


# One table per language; mypy checks that each has every key, and the tests that the
# placeholders match across languages. Add a language here and in `config.DigestLanguage`.
MESSAGES: dict[DigestLanguage, DigestMessages] = {
    DigestLanguage.EN: {
        "header": "*📬 Inbox Jev — {account}* (last {hours}h, mode: {modes})",
        "processed": {"one": "{n} processed", "other": "{n} processed"},
        "summary": "{processed} — {summary} · to review: {low}",
        "handled": {"one": "{n} already handled", "other": "{n} already handled"},
        "pending_check_failed": (
            "_Couldn't check Gmail for what is already handled, so everything is listed._"
        ),
        "nothing": "nothing",
        "no_subject": "(no subject)",
        "needs_reply": "\n*⚡ Needs reply ({n})*",
        "unsubscribed": "\n*🚫 Unsubscribed ({n})*",
        "unsubscribe_manual": "\n*🖐 Manual unsubscribe ({n})*",
        "link": "link",
        "link_in_body": "link in the body",
        "unsubscribe_failed": "\n*❗ Unsubscribe failed ({n})*",
        "low_confidence": (
            "\n*🤔 Low confidence: {n}* from the last {hours}h still in the inbox with {label}"
            " ({link})"
        ),
        "low_confidence_dry_run": (
            "\n*🤔 Low confidence: {n}* from the last {hours}h still in the inbox"
            " (dry run: not labeled)"
        ),
        "see_in_gmail": "see in Gmail",
        "learned": "\n*🧠 Corrections learned: {n}*",
        "errors": "\n*⚠️ Errors*: {emails} ({gave_up} not retried again), {failed_runs}",
        "error_emails": {"one": "{n} email", "other": "{n} emails"},
        "failed_runs": {"one": "{n} failed run", "other": "{n} failed runs"},
        "gave_up": (
            "_Not retried again after {attempts} attempts (`inbox-jev retry-errors` retries them):_"
        ),
        "run_time": "{last} UTC",
        "run_times": "{n} runs, last {last} UTC",
        "failed_run": "• Failed run ({times}): {error}",
        "time_format": "%m/%d %H:%M",
    },
    DigestLanguage.ES: {
        "header": "*📬 Inbox Jev — {account}* (últimas {hours}h, modo: {modes})",
        "processed": {"one": "{n} procesado", "other": "{n} procesados"},
        "summary": "{processed} — {summary} · a revisar: {low}",
        "handled": {"one": "{n} ya resuelto", "other": "{n} ya resueltos"},
        "pending_check_failed": (
            "_No se pudo consultar Gmail para ver qué ya está resuelto: se lista todo._"
        ),
        "nothing": "nada",
        "no_subject": "(sin asunto)",
        "needs_reply": "\n*⚡ Piden respuesta ({n})*",
        "unsubscribed": "\n*🚫 Desuscripto ({n})*",
        "unsubscribe_manual": "\n*🖐 Unsubscribe manual ({n})*",
        "link": "link",
        "link_in_body": "link en el cuerpo",
        "unsubscribe_failed": "\n*❗ Unsubscribe fallido ({n})*",
        "low_confidence": (
            "\n*🤔 Baja confianza: {n}* de las últimas {hours}h siguen en el Inbox con {label}"
            " ({link})"
        ),
        "low_confidence_dry_run": (
            "\n*🤔 Baja confianza: {n}* de las últimas {hours}h siguen en el Inbox"
            " (dry run: sin etiqueta)"
        ),
        "see_in_gmail": "ver en Gmail",
        "learned": "\n*🧠 Correcciones aprendidas: {n}*",
        "errors": "\n*⚠️ Errores*: {emails} ({gave_up} sin más reintentos), {failed_runs}",
        "error_emails": {"one": "{n} mail", "other": "{n} mails"},
        "failed_runs": {"one": "{n} corrida fallida", "other": "{n} corridas fallidas"},
        "gave_up": (
            "_Sin más reintentos tras {attempts} intentos "
            "(`inbox-jev retry-errors` los reintenta):_"
        ),
        "run_time": "{last} UTC",
        "run_times": "{n} corridas, última {last} UTC",
        "failed_run": "• Corrida fallida ({times}): {error}",
        "time_format": "%d/%m %H:%M",
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


def _link(url: str, label: str) -> str:
    """A Slack link. `|` ends the URL part of `<url|label>`, so it is percent-encoded."""
    return f"<{esc(url).replace('|', '%7C')}|{label}>"


def _line(t: DigestMessages, account: str, m: Message, suffix: str = "") -> str:
    subject = esc(m.subject or t["no_subject"], 90)
    return f"• {_link(gmail_link(account, m.gmail_id), subject)} — {esc(m.sender)}{suffix}"


def _needs_reply(cfg: CategoriesConfig, m: Message) -> bool:
    if m.decided_labels is None:  # rows stored before decided_labels existed
        return (m.needs_reply or 0) >= cfg.needs_reply.threshold
    return cfg.needs_reply.label in m.decided_labels


def build(
    session: Session,
    cfg: CategoriesConfig,
    account: str,
    hours: int = 24,
    *,
    language: DigestLanguage,
    gmail: ThreadReader | None,
) -> str:
    """The digest text for `account` over the last `hours`, in `language`.

    `language` has no default on purpose: the account's `digest_language` is the only default,
    so a caller can't silently render a different language than the account's.

    `gmail` is read to drop the emails already handled (`check_pending`); None (it couldn't be
    opened) or a failed read lists them all, with a line saying so. Neither has a default, so
    a caller can't skip the check by forgetting it.
    """
    t = MESSAGES[language]
    since = now() - timedelta(hours=hours)
    msgs = session.scalars(
        select(Message)
        .where(Message.account == account, Message.processed_at >= since)
        .order_by(Message.processed_at.desc())
    ).all()
    done = [m for m in msgs if m.status == MessageStatus.DONE]
    errors = [m for m in msgs if m.status == MessageStatus.ERROR]
    unsubs = session.scalars(
        select(Unsubscribe).where(Unsubscribe.account == account, Unsubscribe.attempted_at >= since)
    ).all()
    runs = session.scalars(select(Run).where(Run.account == account, Run.started_at >= since)).all()
    learned = session.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.account == account,
            Message.corrected_category.is_not(None),
            Message.correction_checked_at >= since,
        )
    )

    modes = sorted({m.mode for m in done})
    low = [m for m in done if m.low_confidence]
    kept = _kept_on_purpose(cfg, done)
    listed = {id(m) for _, msgs in kept for m in msgs}
    reply = [m for m in done if _needs_reply(cfg, m) and id(m) not in listed]
    pending = check_pending(
        gmail, [*(m for _, msgs in kept for m in msgs), *reply], low, cfg.review_label
    )

    out = [t["header"].format(account=account, hours=hours, modes=", ".join(modes) or "—")]
    out += _summary(t, cfg, done, low, pending)
    for label, msgs in kept:
        shown = pending.keep(msgs)
        out += _section(f"\n*🔔 {label} ({len(shown)})*", shown, lambda m: _line(t, account, m))
    reply = pending.keep(reply)
    out += _section(t["needs_reply"].format(n=len(reply)), reply, lambda m: _line(t, account, m))
    out += _unsubscribe_sections(t, unsubs)
    out += _low_confidence(t, cfg, account, hours, pending.keep_for_review(low))
    if learned:
        out.append(t["learned"].format(n=learned))
    out += _errors(t, account, errors, [r for r in runs if r.error])
    return "\n".join(out)


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


def _summary(
    t: DigestMessages,
    cfg: CategoriesConfig,
    done: list[Message],
    low: list[Message],
    pending: Pending,
) -> list[str]:
    """How many emails were processed, per category (low-confidence ones apart), and how many
    of the ones the digest would list were already handled (or that the check failed)."""
    counts = Counter(m.category for m in done if not m.low_confidence)
    summary = " · ".join(
        f"{cfg.by_key[k].label if k in cfg.by_key else k}: {n}" for k, n in counts.most_common()
    )
    processed = plural(t["processed"], len(done))
    line = t["summary"].format(processed=processed, summary=summary or t["nothing"], low=len(low))
    if pending.handled:
        line += " · " + plural(t["handled"], pending.handled)
    return [line, t["pending_check_failed"]] if pending.failed else [line]


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


def _unsubscribe_sections(t: DigestMessages, unsubs: Sequence[Unsubscribe]) -> list[str]:
    """Unsubscribed lists, the ones waiting for a manual unsubscribe, and the failed ones."""
    ok = [u for u in unsubs if u.status == UnsubscribeStatus.OK]
    pending = [u for u in unsubs if u.status == UnsubscribeStatus.MANUAL_PENDING]
    failed = [u for u in unsubs if u.status == UnsubscribeStatus.FAILED]
    return [
        *_section(
            t["unsubscribed"].format(n=len(ok)), ok, lambda u: f"• {esc(u.list_key)} ({u.method})"
        ),
        *_section(
            t["unsubscribe_manual"].format(n=len(pending)),
            pending,
            lambda u: (
                f"• {esc(u.list_key)} — "
                + (_link(u.target, t["link"]) if u.target else t["link_in_body"])
            ),
        ),
        *_section(
            t["unsubscribe_failed"].format(n=len(failed)),
            failed,
            lambda u: f"• {esc(u.list_key)}: {esc(_unsubscribe_error(u), 160)}",
        ),
    ]


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
        link = _link(gmail_search_link(account, query), t["see_in_gmail"])
        out.append(
            t["low_confidence"].format(
                n=labeled, hours=hours, label=esc(cfg.review_label), link=link
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


def send_slack(token: str, user_id: str, text: str) -> None:
    resp = httpx.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}"},
        json={"channel": user_id, "text": text, "unfurl_links": False, "unfurl_media": False},
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"Slack error: {body.get('error')}")
