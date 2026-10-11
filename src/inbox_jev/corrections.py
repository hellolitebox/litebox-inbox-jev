"""Detect manual fixes in Gmail (changed Jev label, email pulled back to the inbox) and learn."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import rules
from .config import CategoriesConfig
from .db import Message, now
from .gmail import Gmail

log = logging.getLogger(__name__)

WINDOW = timedelta(days=7)
RECHECK_EVERY = timedelta(hours=1)
MAX_PER_RUN = 100


def detect(msg: Message, current_labels: set[str], cfg: CategoriesConfig) -> str | None:
    """Return the category the user corrected this email to, or None if untouched."""
    by_label = {c.label: c.key for c in cfg.categories}
    current = [by_label[name] for name in current_labels if name in by_label]
    if len(current) == 1:
        if current[0] != msg.category:
            return current[0]
        if msg.low_confidence and cfg.review_label not in current_labels:
            return current[0]  # user accepted the suggested category
    if (msg.archived or msg.spammed) and "INBOX" in current_labels:
        return "important"  # user pulled it back
    return None


def check(session: Session, gmail: Gmail, account: str, cfg: CategoriesConfig) -> int:
    cutoff = now()
    candidates = session.scalars(
        select(Message)
        .where(
            Message.account == account,
            Message.status == "done",
            Message.mode != "dry_run",
            Message.restored.is_(False),
            Message.corrected_category.is_(None),
            Message.processed_at >= cutoff - WINDOW,
            or_(
                Message.correction_checked_at.is_(None),
                Message.correction_checked_at < cutoff - RECHECK_EVERY,
            ),
        )
        .order_by(Message.processed_at.desc())
        .limit(MAX_PER_RUN)
    ).all()

    names = {v: k for k, v in gmail.labels().items()}
    corrected = 0
    for msg in candidates:
        label_ids = gmail.get_label_ids(msg.gmail_id)
        msg.correction_checked_at = cutoff
        if label_ids is None:
            continue
        category = detect(msg, {names.get(i, i) for i in label_ids}, cfg)
        if category:
            msg.corrected_category = category
            rules.set_rule(session, account, msg.sender, category, source="correction")
            corrected += 1
            log.info(
                "correction learned",
                extra={
                    "account": account,
                    "sender": msg.sender,
                    "from": msg.category,
                    "to": category,
                },
            )
    session.commit()
    return corrected
