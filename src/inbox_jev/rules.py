"""Per-sender memory: manual rules, rules learned from corrections, and automatic rules."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from .classification import Classification, PreviousContact
from .config import Action, CategoriesConfig
from .db import SenderRule
from .policy import KNOWN_CONTACT, Decision, rule_keys, spares_known_contacts


class RuleSource(StrEnum):
    """Where a sender rule came from (stored in `sender_rules.source`)."""

    MANUAL = "manual"  # `inbox-jev rules set`
    CORRECTION = "correction"  # learned from a label the user changed in Gmail
    AUTO = "auto"  # learned from consistent, confident Jev answers (record_auto)


@dataclass(frozen=True)
class SenderRules:
    """Every rule keyed on one sender (exact address, then @domain), active or not.

    Read once per email (`load`): `match` decides with the active ones, and `record_auto` gets
    the exact-address row (`exact`) it may update, active or not, without reading it again.
    """

    sender: str
    rows: tuple[SenderRule, ...]  # in lookup order: exact address, then @domain

    @property
    def active(self) -> list[SenderRule]:
        """The active rules, in lookup order."""
        return [r for r in self.rows if r.active]

    @property
    def exact(self) -> SenderRule | None:
        """The rule keyed on the exact address, active or not (auto rules are keyed so)."""
        return next((r for r in self.rows if r.sender_key == self.sender), None)

    def match(self, categories: Collection[str]) -> tuple[SenderRule | None, SenderRule | None]:
        """The rule that decides for the sender, as `(rule, stale)` (see the module's `match`)."""
        found = self.active
        first = found[0] if found else None
        if first is None or first.category in categories:
            return first, None
        return None, first


def load(session: Session, account: str, sender: str) -> SenderRules:
    """Every rule matching `sender` (`SenderRules`), in one query."""
    keys = rule_keys(sender)
    found = session.scalars(
        select(SenderRule).where(SenderRule.account == account, SenderRule.sender_key.in_(keys))
    ).all()
    return SenderRules(sender, tuple(sorted(found, key=lambda r: keys.index(r.sender_key))))


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
    return load(session, account, sender).match(categories)


def active(session: Session, account: str, sender: str) -> list[SenderRule]:
    """Active rules matching `sender`, in lookup order (exact address, then @domain)."""
    return load(session, account, sender).active


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
    previous_contact: PreviousContact | None = None,
    *,
    count: bool = True,
    known: SenderRules | None = None,
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
    `known` is the sender's rules already read for this email (`load`); without it they are
    read here.
    """
    other_keep = cfg.keep_probability(classification.probabilities, exclude=decision.category)
    keep_signal = other_keep >= cfg.auto_rule.max_keep_probability
    rule = (known if known is not None else load(session, account, sender)).exact
    if rule is not None and (_is_keep(cfg, decision.category) or keep_signal):
        _reset_pending_archive_rule(cfg, rule)
    if not _counts_toward_rule(
        cfg, classification, decision, previous_contact, keep_signal=keep_signal
    ):
        return
    if rule is None:
        if count:
            session.add(_new_auto_rule(cfg, account, sender, decision.category))
        return
    _count_hit(cfg, rule, decision.category, count=count)


def _reset_pending_archive_rule(cfg: CategoriesConfig, rule: SenderRule) -> None:
    """The sender sends mail that may need to stay in the inbox (a keep decision, or a keep
    category likely enough, e.g. a "payment failed" from a notifications address): restart a
    pending auto rule that would archive it."""
    if rule.source == RuleSource.AUTO and not rule.active and not _is_keep(cfg, rule.category):
        rule.hits = 0


def _counts_toward_rule(
    cfg: CategoriesConfig,
    classification: Classification,
    decision: Decision,
    previous_contact: PreviousContact | None,
    *,
    keep_signal: bool,
) -> bool:
    """Whether Jev's answer can count toward an auto rule: confident (not low-confidence, at
    least `auto_rule.min_confidence`) and no keep category likely enough (`keep_signal`).

    Unverified senders (unsubscribe_skipped_unverified) can earn a rule: the rule path re-runs
    the DMARC/List-Unsubscribe and group checks on every email, so it never unsubscribes them.
    A group archive never earns one: the top category alone was below its threshold. Nor does
    a known contact in a category that spares known contacts (cold_outreach), whether or not
    an unsubscribe was in play (verified or not, group or not): a "pitch" from someone I wrote
    to is where Jev is most likely wrong, so such senders stay on the Jev path, which sees the
    relationship on every email.
    """
    known_contact = previous_contact in KNOWN_CONTACT and spares_known_contacts(
        cfg, decision.category
    )
    return not (
        decision.low_confidence
        or decision.archive_group
        or known_contact
        or keep_signal
        or classification.confidence < cfg.auto_rule.min_confidence
    )


def _new_auto_rule(cfg: CategoriesConfig, account: str, sender: str, category: str) -> SenderRule:
    """A sender's first counted answer: a pending auto rule with one hit (active at once when
    `auto_rule.hits` is 1)."""
    return SenderRule(
        account=account,
        sender_key=sender,
        category=category,
        source=RuleSource.AUTO,
        hits=1,
        active=cfg.auto_rule.hits <= 1,
    )


def _count_hit(cfg: CategoriesConfig, rule: SenderRule, category: str, *, count: bool) -> None:
    """Count a confident answer in `category` on the sender's exact rule `rule`.

    Only a pending auto rule, or an active one whose category was removed, changes: manual and
    correction rules never do. The same category adds a hit (active at `auto_rule.hits`);
    another one starts the rule over in it. With `count=False` (an answer already learned from)
    nothing is added, but a pending rule in another category still starts over at 0 hits.
    """
    if rule.source != RuleSource.AUTO or (rule.active and rule.category in cfg.by_key):
        return
    if not count:
        if not rule.active and rule.category != category:
            rule.hits = 0
        return
    if rule.category == category:
        rule.hits += 1
        rule.active = rule.hits >= cfg.auto_rule.hits
    else:
        rule.category, rule.hits = category, 1
        rule.active = cfg.auto_rule.hits <= 1


def _is_keep(cfg: CategoriesConfig, key: str) -> bool:
    return key in cfg.by_key and cfg.by_key[key].action == Action.KEEP
