"""Per-sender memory: manual rules, rules learned from corrections, and automatic rules."""

from __future__ import annotations

from collections.abc import Collection
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from .classification import Classification
from .config import Action, CategoriesConfig
from .db import SenderRule
from .policy import KNOWN_CONTACT, Decision, rule_keys, spares_known_contacts


class RuleSource(StrEnum):
    """Where a sender rule came from (stored in `sender_rules.source`)."""

    MANUAL = "manual"  # `inbox-jev rules set`
    CORRECTION = "correction"  # learned from a label the user changed in Gmail
    AUTO = "auto"  # learned from consistent, confident Jev answers (record_auto)


def lookup(session: Session, account: str, sender: str) -> SenderRule | None:
    """The first active rule for `sender` (exact address, then @domain), whatever its category.

    Deciding code uses `match`, which also checks the category still exists.
    """
    found = active(session, account, sender)
    return found[0] if found else None


def match(
    session: Session, account: str, sender: str, categories: Collection[str]
) -> tuple[SenderRule | None, SenderRule | None]:
    """The rule that decides for `sender`, as `(rule, stale)`.

    The first active rule decides (exact address, then @domain). If its category isn't one of
    `categories` (removed from categories.yaml), it comes back as `stale` and `rule` is None: the
    sender has no rule and Jev decides. It never falls through to the @domain rule: an exact rule
    is the user's exception to it, so applying the domain rule would undo that exception.
    """
    first = lookup(session, account, sender)
    if first is None or first.category in categories:
        return first, None
    return None, first


def active(session: Session, account: str, sender: str) -> list[SenderRule]:
    """Active rules matching `sender`, in lookup order (exact address, then @domain)."""
    keys = rule_keys(sender)
    found = session.scalars(
        select(SenderRule).where(
            SenderRule.account == account,
            SenderRule.sender_key.in_(keys),
            SenderRule.active.is_(True),
        )
    ).all()
    return sorted(found, key=lambda r: keys.index(r.sender_key))


def set_rule(
    session: Session, account: str, sender_key: str, category: str, source: RuleSource
) -> None:
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
    previous_contact: str | None = None,
    *,
    count: bool = True,
) -> None:
    """Count consistent high-confidence answers; activate after cfg.auto_rule.hits.

    Only for Jev decisions (`DecisionSource.JEV`) outside dry_run: the caller checks.
    `count=False` (an email whose answer was already learned from) adds no hit, creates no rule
    and switches no category: each email adds at most one hit. It still applies the resets: the
    keep one below, and a confident answer in another category than a pending auto rule's
    (Jev disagreeing with itself about one email: the sender isn't consistent).
    Only touches automatic rules: manual and correction rules are never changed here. An active
    auto rule whose category was removed from categories.yaml is replaced like a pending one
    (`match` sends its sender to Jev: auto rules are keyed on the exact address, so they come
    first).
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
        and rule.source == RuleSource.AUTO
        and not rule.active
        and not _is_keep(cfg, rule.category)
    ):
        rule.hits = 0
    # Unverified senders (downgraded_to_spam) can earn a rule: the rule path re-runs the
    # DMARC/List-Unsubscribe and group checks on every email, so it never unsubscribes them.
    # A group archive never earns one: the top category alone was below its threshold. Nor does
    # a known contact in a category that spares known contacts (cold_outreach), whether or not
    # an unsubscribe was in play (verified or not, group or not): a "pitch" from someone I wrote
    # to is where Jev is most likely wrong, so such senders stay on the Jev path, which sees the
    # relationship on every email.
    known_contact = previous_contact in KNOWN_CONTACT and spares_known_contacts(
        cfg, decision.category
    )
    if (
        decision.low_confidence
        or decision.archive_group
        or known_contact
        or keep_signal
        or classification.confidence < cfg.auto_rule.min_confidence
    ):
        return
    if rule is None:
        if not count:
            return
        session.add(
            SenderRule(
                account=account,
                sender_key=sender,
                category=decision.category,
                source=RuleSource.AUTO,
                hits=1,
                active=cfg.auto_rule.hits <= 1,
            )
        )
        return
    if rule.source != RuleSource.AUTO or (rule.active and rule.category in cfg.by_key):
        return
    if not count:
        if not rule.active and rule.category != decision.category:
            rule.hits = 0
        return
    if rule.category == decision.category:
        rule.hits += 1
        rule.active = rule.hits >= cfg.auto_rule.hits
    else:
        rule.category, rule.hits = decision.category, 1
        rule.active = cfg.auto_rule.hits <= 1


def _is_keep(cfg: CategoriesConfig, key: str) -> bool:
    return key in cfg.by_key and cfg.by_key[key].action == Action.KEEP
