"""Per-sender memory: manual rules, rules learned from corrections, and automatic rules."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .classifier import Classification
from .config import Action, CategoriesConfig
from .db import SenderRule
from .policy import Decision, rule_keys


def lookup(session: Session, account: str, sender: str) -> SenderRule | None:
    for key in rule_keys(sender):
        rule = session.scalar(
            select(SenderRule).where(
                SenderRule.account == account,
                SenderRule.sender_key == key,
                SenderRule.active.is_(True),
            )
        )
        if rule:
            return rule
    return None


def set_rule(session: Session, account: str, sender_key: str, category: str, source: str) -> None:
    rule = session.scalar(
        select(SenderRule).where(SenderRule.account == account, SenderRule.sender_key == sender_key)
    )
    if rule is None:
        rule = SenderRule(account=account, sender_key=sender_key)
        session.add(rule)
    rule.category, rule.source, rule.active, rule.hits = category, source, True, 0


def record_auto(
    session: Session,
    cfg: CategoriesConfig,
    account: str,
    sender: str,
    classification: Classification,
    decision: Decision,
) -> None:
    """Count consistent high-confidence answers; activate after cfg.auto_rule.hits.

    Only touches automatic rules: manual and correction rules are never changed here.
    """
    is_keep = _is_keep(cfg, decision.category)
    other_keep = cfg.keep_probability(classification.probabilities, exclude=decision.category)
    keep_signal = other_keep >= cfg.auto_rule.max_keep_probability
    rule = session.scalar(
        select(SenderRule).where(SenderRule.account == account, SenderRule.sender_key == sender)
    )
    # The sender sends mail that may need to stay in the inbox (e.g. a "payment failed" from a
    # notifications address): restart a pending rule that would archive it.
    if (
        (is_keep or keep_signal)
        and rule is not None
        and rule.source == "auto"
        and not rule.active
        and not _is_keep(cfg, rule.category)
    ):
        rule.hits = 0
    # Unverified marketing never earns a rule: a rule skips the DMARC/List-Unsubscribe check.
    # A group archive never does either: the top category alone was below its threshold.
    if (
        decision.low_confidence
        or decision.downgraded_to_spam
        or decision.archive_group
        or keep_signal
        or classification.confidence < cfg.auto_rule.min_confidence
    ):
        return
    if rule is None:
        session.add(
            SenderRule(
                account=account,
                sender_key=sender,
                category=decision.category,
                source="auto",
                hits=1,
                active=cfg.auto_rule.hits <= 1,
            )
        )
        return
    if rule.source != "auto" or rule.active:
        return
    if rule.category == decision.category:
        rule.hits += 1
        rule.active = rule.hits >= cfg.auto_rule.hits
    else:
        rule.category, rule.hits = decision.category, 1


def _is_keep(cfg: CategoriesConfig, key: str) -> bool:
    return key in cfg.by_key and cfg.by_key[key].action == Action.KEEP
