"""Daily digest per account, sent as a Slack DM (or printed)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import TypedDict, TypeVar

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Action, CategoriesConfig, DigestLanguage
from .db import MAX_ERROR_ATTEMPTS, Message, MessageStatus, Run, Unsubscribe, now
from .unsubscribe import UnsubscribeStatus

MAX_ITEMS = 15

_T = TypeVar("_T")


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
    nothing: str
    no_subject: str
    needs_reply: str
    unsubscribed: str
    unsubscribe_manual: str
    link: str
    link_in_body: str
    unsubscribe_failed: str
    low_confidence: str
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
        "nothing": "nothing",
        "no_subject": "(no subject)",
        "needs_reply": "\n*⚡ Needs reply ({n})*",
        "unsubscribed": "\n*🚫 Unsubscribed ({n})*",
        "unsubscribe_manual": "\n*🖐 Manual unsubscribe ({n})*",
        "link": "link",
        "link_in_body": "link in the body",
        "unsubscribe_failed": "\n*❗ Unsubscribe failed ({n})*",
        "low_confidence": "\n*🤔 Low confidence ({n})* — left in the inbox with {label}",
        "learned": "\n*🧠 Corrections learned ({n})*",
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
        "nothing": "nada",
        "no_subject": "(sin asunto)",
        "needs_reply": "\n*⚡ Piden respuesta ({n})*",
        "unsubscribed": "\n*🚫 Desuscripto ({n})*",
        "unsubscribe_manual": "\n*🖐 Unsubscribe manual ({n})*",
        "link": "link",
        "link_in_body": "link en el cuerpo",
        "unsubscribe_failed": "\n*❗ Unsubscribe fallido ({n})*",
        "low_confidence": "\n*🤔 Baja confianza ({n})* — quedaron en el Inbox con {label}",
        "learned": "\n*🧠 Correcciones aprendidas ({n})*",
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
) -> str:
    """The digest text for `account` over the last `hours`, in `language`.

    `language` has no default on purpose: the account's `digest_language` is the only default,
    so a caller can't silently render a different language than the account's.
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
    learned = session.scalars(
        select(Message).where(
            Message.account == account,
            Message.corrected_category.is_not(None),
            Message.correction_checked_at >= since,
        )
    ).all()

    modes = sorted({m.mode for m in done})
    low = [m for m in done if m.low_confidence]
    kept = _kept_on_purpose(cfg, done)
    listed = {id(m) for _, msgs in kept for m in msgs}
    reply = [m for m in done if _needs_reply(cfg, m) and id(m) not in listed]
    out = [t["header"].format(account=account, hours=hours, modes=", ".join(modes) or "—")]
    out += _summary(t, cfg, done, low)
    for label, msgs in kept:
        out += _section(f"\n*🔔 {label} ({len(msgs)})*", msgs, lambda m: _line(t, account, m))
    out += _section(t["needs_reply"].format(n=len(reply)), reply, lambda m: _line(t, account, m))
    out += _unsubscribe_sections(t, unsubs)
    out += _low_confidence(t, cfg, account, low)
    out += _section(
        t["learned"].format(n=len(learned)),
        learned,
        lambda m: f"• {esc(m.sender)}: {esc(m.category)} → {esc(m.corrected_category)}",
    )
    out += _errors(t, account, errors, [r for r in runs if r.error])
    return "\n".join(out)


def _section(title: str, items: Sequence[_T], line: Callable[[_T], str]) -> list[str]:
    """`title` and one `line` per item (at most MAX_ITEMS), or nothing when there are none."""
    return [title, *(line(item) for item in items[:MAX_ITEMS])] if items else []


def _summary(
    t: DigestMessages, cfg: CategoriesConfig, done: list[Message], low: list[Message]
) -> list[str]:
    """How many emails were processed, per category (low-confidence ones apart)."""
    counts = Counter(m.category for m in done if not m.low_confidence)
    summary = " · ".join(
        f"{cfg.by_key[k].label if k in cfg.by_key else k}: {n}" for k, n in counts.most_common()
    )
    processed = plural(t["processed"], len(done))
    return [t["summary"].format(processed=processed, summary=summary or t["nothing"], low=len(low))]


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
            lambda u: f"• {esc(u.list_key)}: {esc(u.error or u.http_status or '', 80)}",
        ),
    ]


def _low_confidence(
    t: DigestMessages, cfg: CategoriesConfig, account: str, low: list[Message]
) -> list[str]:
    """Emails left in the inbox with the review label, with Jev's guess and its confidence."""
    return _section(
        t["low_confidence"].format(n=len(low), label=cfg.review_label),
        low,
        lambda m: _line(t, account, m, f" → {esc(m.category)}? ({(m.confidence or 0):.0%})"),
    )


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
