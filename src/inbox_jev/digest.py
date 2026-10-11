"""Daily digest per account, sent as a Slack DM (or printed)."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import CategoriesConfig
from .db import Message, Run, Unsubscribe, now

MAX_ITEMS = 15


def gmail_link(account: str, gmail_id: str) -> str:
    return f"https://mail.google.com/mail/u/?authuser={account}#all/{gmail_id}"


def _line(account: str, m: Message, suffix: str = "") -> str:
    subject = (m.subject or "(sin asunto)").replace("<", "‹").replace(">", "›")[:90]
    return f"• <{gmail_link(account, m.gmail_id)}|{subject}> — {m.sender}{suffix}"


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

    reply = [m for m in done if (m.needs_reply or 0) >= cfg.needs_reply.threshold]
    if reply:
        out.append(f"\n*⚡ Piden respuesta ({len(reply)})*")
        out += [_line(account, m) for m in reply[:MAX_ITEMS]]

    ok = [u for u in unsubs if u.status == "ok"]
    if ok:
        out.append(f"\n*🚫 Desuscripto ({len(ok)})*")
        out += [f"• {u.list_key} ({u.method})" for u in ok[:MAX_ITEMS]]
    pending = [u for u in unsubs if u.status == "manual_pending"]
    if pending:
        out.append(f"\n*🖐 Unsubscribe manual ({len(pending)})*")
        out += [
            f"• {u.list_key}" + (f" — <{u.target}|link>" if u.target else " — link en el cuerpo")
            for u in pending[:MAX_ITEMS]
        ]
    failed = [u for u in unsubs if u.status == "failed"]
    if failed:
        out.append(f"\n*❗ Unsubscribe fallido ({len(failed)})*")
        out += [
            f"• {u.list_key}: {(u.error or u.http_status or '')!s:.80}" for u in failed[:MAX_ITEMS]
        ]

    if low:
        out.append(
            f"\n*🤔 Baja confianza ({len(low)})* — quedaron en el Inbox con {cfg.review_label}"
        )
        out += [
            _line(account, m, f" → {m.category}? ({(m.confidence or 0):.0%})")
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
        out += [f"• {m.sender}: {m.category} → {m.corrected_category}" for m in learned[:MAX_ITEMS]]

    run_errors = [r.error for r in runs if r.error]
    if errors or run_errors:
        out.append(f"\n*⚠️ Errores*: {len(errors)} mails, {len(run_errors)} corridas fallidas")
        out += [f"• {e[:120]}" for e in run_errors[:3]]

    return "\n".join(out)


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
