"""Detect manual fixes in Gmail (changed Jev label, email pulled back to the inbox) and learn."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import rules
from .config import Account, CategoriesConfig, Mode
from .db import Message, MessageStatus, now
from .system_labels import INBOX

log = logging.getLogger(__name__)

WINDOW = timedelta(days=7)
RECHECK_EVERY = timedelta(hours=1)
MAX_PER_RUN = 100


class LabelReader(Protocol):
    """What `check` reads from Gmail: a `gmail.Gmail`, or a fake in tests."""

    def labels(self) -> dict[str, str]: ...
    def get_label_ids(self, message_ids: Sequence[str]) -> dict[str, list[str] | None]: ...


def detect(msg: Message, current_labels: set[str], cfg: CategoriesConfig) -> str | None:
    """Return the category the user corrected this email to, or None if untouched."""
    current = [cfg.by_label[name] for name in current_labels if name in cfg.by_label]
    if len(current) == 1:
        if current[0] != msg.category:
            return current[0]
        if msg.low_confidence and cfg.review_label not in current_labels:
            return current[0]  # user accepted the suggested category
    if (msg.archived or msg.spammed) and INBOX in current_labels:
        return "important"  # user pulled it back
    return None


def check(session: Session, gmail: LabelReader, account: Account, cfg: CategoriesConfig) -> int:
    """Record the corrections the user made in Gmail to recently processed emails (`record`).
    Returns how many were found."""
    cutoff = now()
    candidates = session.scalars(
        select(Message)
        .where(
            Message.account == account.email,
            Message.status == MessageStatus.DONE,
            Message.mode != Mode.DRY_RUN,
            Message.restored.is_(False),
            Message.corrected_category.is_(None),
            Message.processed_at >= cutoff - WINDOW,
            or_(
                Message.correction_checked_at.is_(None),
                Message.correction_checked_at < cutoff - RECHECK_EVERY,
            ),
        )
        # Never-checked rows first, then the longest unchecked, so with more than MAX_PER_RUN
        # candidates the older ones still get their turn instead of the newest hogging the cap.
        .order_by(
            Message.correction_checked_at.asc().nulls_first(),
            Message.processed_at.desc(),
        )
        .limit(MAX_PER_RUN)
    ).all()

    names = {v: k for k, v in gmail.labels().items()}
    # One batched Gmail call for all candidates. An error other than a 404 is raised before any
    # row changes: the run fails and every candidate is checked again by the next one.
    current = gmail.get_label_ids([msg.gmail_id for msg in candidates])
    corrected = 0
    for msg in candidates:
        label_ids = current[msg.gmail_id]
        msg.correction_checked_at = cutoff
        if label_ids is None:
            continue
        category = detect(msg, {names.get(i, i) for i in label_ids}, cfg)
        if category:
            record(session, account, msg, category)
            corrected += 1
    session.commit()
    return corrected


def record(session: Session, account: Account, msg: Message, category: str) -> None:
    """Store a detected correction and turn it into a sender rule.

    For an email decided on its own (`rules.decided_per_email`: its sender is in the account's
    decide_each_email, or it was sent as a mailing list's own address, `msg.sender_is_list`)
    the correction is stored on the email only: it decides that email if it is processed again
    (`triage.process`), not the sender's next ones. A row stored before `sender_is_list`
    existed (None) counts as not a list's own address.
    """
    msg.corrected_category = category
    learned = not rules.decided_per_email(
        account, msg.sender, sender_is_list=bool(msg.sender_is_list)
    )
    if learned:
        rules.set_rule(
            session, account.email, msg.sender, category, source=rules.RuleSource.CORRECTION
        )
    log.info(
        "correction learned" if learned else "correction recorded: email decided on its own",
        extra={
            "account": account.email,
            "sender": msg.sender,
            "from": msg.category,
            "to": category,
        },
    )
