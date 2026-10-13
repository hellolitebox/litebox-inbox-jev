"""Daily digest per account, sent as a Slack DM (or printed)."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Action, CategoriesConfig
from .db import MAX_ERROR_ATTEMPTS, Message, Run, Unsubscribe, now

MAX_ITEMS = 15


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


def _line(account: str, m: Message, suffix: str = "") -> str:
    subject = esc(m.subject or "(sin asunto)", 90)
    return f"• {_link(gmail_link(account, m.gmail_id), subject)} — {esc(m.sender)}{suffix}"


def _needs_reply(cfg: CategoriesConfig, m: Message) -> bool:
    if m.decided_labels is None:  # rows stored before decided_labels existed
        return (m.needs_reply or 0) >= cfg.needs_reply.threshold
    return cfg.needs_reply.label in m.decided_labels


def build(session: Session, cfg: CategoriesConfig, account: str, hours: int = 24) -> str:
    since = now() - timedelta(hours=hours)
    msgs = session.scalars(
        select(Message)
        .where(Message.account == account, Message.processed_at >= since)
        .order_by(Message.processed_at.desc())
    ).all()
    done = [m for m in msgs if m.status == "done"]
    errors = [m for m in msgs if m.status == "error"]
    unsubs = session.scalars(
        select(Unsubscribe).where(Unsubscribe.account == account, Unsubscribe.attempted_at >= since)
    ).all()
    runs = session.scalars(select(Run).where(Run.account == account, Run.started_at >= since)).all()

    modes = sorted({m.mode for m in done})
    out = [f"*📬 Inbox Jev — {account}* (últimas {hours}h, modo: {', '.join(modes) or '—'})"]

    counts = Counter(m.category for m in done if not m.low_confidence)
    labels = {c.key: c.label for c in cfg.categories}
    summary = " · ".join(f"{labels.get(k, k)}: {n}" for k, n in counts.most_common())
    low = [m for m in done if m.low_confidence]
    out.append(f"{len(done)} procesados — {summary or 'nada'} · a revisar: {len(low)}")

    # Categories that stay in the inbox on purpose (besides "important") get their own section,
    # e.g. automated emails that need an action. Emails listed there aren't repeated below.
    listed: set[int] = set()
    for cat in cfg.categories:
        if cat.action != Action.KEEP or cat.key == "important":
            continue
        kept = [m for m in done if m.category == cat.key and not m.low_confidence]
        if kept:
            out.append(f"\n*🔔 {cat.label} ({len(kept)})*")
            out += [_line(account, m) for m in kept[:MAX_ITEMS]]
            listed.update(id(m) for m in kept)

    reply = [m for m in done if _needs_reply(cfg, m) and id(m) not in listed]
    if reply:
        out.append(f"\n*⚡ Piden respuesta ({len(reply)})*")
        out += [_line(account, m) for m in reply[:MAX_ITEMS]]

    ok = [u for u in unsubs if u.status == "ok"]
    if ok:
        out.append(f"\n*🚫 Desuscripto ({len(ok)})*")
        out += [f"• {esc(u.list_key)} ({u.method})" for u in ok[:MAX_ITEMS]]
    pending = [u for u in unsubs if u.status == "manual_pending"]
    if pending:
        out.append(f"\n*🖐 Unsubscribe manual ({len(pending)})*")
        out += [
            f"• {esc(u.list_key)} — "
            + (_link(u.target, "link") if u.target else "link en el cuerpo")
            for u in pending[:MAX_ITEMS]
        ]
    failed = [u for u in unsubs if u.status == "failed"]
    if failed:
        out.append(f"\n*❗ Unsubscribe fallido ({len(failed)})*")
        out += [
            f"• {esc(u.list_key)}: {esc(u.error or u.http_status or '', 80)}"
            for u in failed[:MAX_ITEMS]
        ]

    if low:
        out.append(
            f"\n*🤔 Baja confianza ({len(low)})* — quedaron en el Inbox con {cfg.review_label}"
        )
        out += [
            _line(account, m, f" → {esc(m.category)}? ({(m.confidence or 0):.0%})")
            for m in low[:MAX_ITEMS]
        ]

    learned = session.scalars(
        select(Message).where(
            Message.account == account,
            Message.corrected_category.is_not(None),
            Message.correction_checked_at >= since,
        )
    ).all()
    if learned:
        out.append(f"\n*🧠 Correcciones aprendidas ({len(learned)})*")
        out += [
            f"• {esc(m.sender)}: {esc(m.category)} → {esc(m.corrected_category)}"
            for m in learned[:MAX_ITEMS]
        ]

    failed_runs = [r for r in runs if r.error]
    if errors or failed_runs:
        gave_up = [m for m in errors if (m.error_attempts or 0) >= MAX_ERROR_ATTEMPTS]
        out.append(
            f"\n*⚠️ Errores*: {len(errors)} mails ({len(gave_up)} sin más reintentos), "
            f"{len(failed_runs)} corridas fallidas"
        )
        out += _failed_runs(failed_runs)
        if gave_up:
            out.append(
                f"_Sin más reintentos tras {MAX_ERROR_ATTEMPTS} intentos "
                "(`inbox-jev retry-errors` los reintenta):_"
            )
            out += [_line(account, m, f": {esc(m.error or '', 80)}") for m in gave_up[:MAX_ITEMS]]

    return "\n".join(out)


def _failed_runs(runs: list[Run]) -> list[str]:
    """One line per distinct run error (a dead token fails every run the same way)."""
    by_error: dict[str, list[Run]] = {}
    for r in sorted(runs, key=lambda r: r.started_at):
        by_error.setdefault(r.error or "", []).append(r)
    lines = []
    for error, group in by_error.items():
        last = group[-1].started_at.strftime("%d/%m %H:%M")
        times = f"{len(group)} corridas, última {last} UTC" if len(group) > 1 else f"{last} UTC"
        lines.append(f"• Corrida fallida ({times}): {esc(error, 300)}")
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
