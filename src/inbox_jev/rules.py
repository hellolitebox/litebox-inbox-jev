"""Per-sender memory: manual rules, rules learned from corrections, and automatic rules."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from .classification import Classification, PreviousContact
from .config import Account, Action, CategoriesConfig
from .db import SenderRule
from .policy import KNOWN_CONTACT, Decision, rule_keys, spares_known_contacts


class RuleSource(StrEnum):
    """Where a sender rule came from (stored in `sender_rules.source`)."""

    MANUAL = "manual"  # `inbox-jev rules set`
    CORRECTION = "correction"  # learned from a label the user changed in Gmail
    AUTO = "auto"  # learned from consistent, confident Jev answers (record_auto)


def decided_per_email(account: Account, sender: str, *, sender_is_list: bool) -> bool:
    """Whether an email from `sender` is decided on its own: no auto rule is learned from it, a
    correction on it creates no sender rule, and only a manual rule decides it.

    True for a sender in the account's decide_each_email (`Account.decides_each_email`, ADR
    0045), and for an email sent as a mailing list's own address (`sender_is_list`:
    `Signals.sender_is_list`, or the stored `Message.sender_is_list`; ADR 0051), whose address
    carries every member's mail.
    """
    return sender_is_list or account.decides_each_email(sender)


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

    def deciding(self, account: Account, *, sender_is_list: bool) -> SenderRules:
        """The rules that may decide for the sender in `account`: all of them, or only the
        manual ones when the email is decided on its own (`decided_per_email`: a sender in
        decide_each_email, or an email sent as a list's own address, `sender_is_list`). A rule
        learned from some of such a sender's emails (auto or correction) says nothing about the
        next one; a manual rule is the user's own choice.
        """
        if not decided_per_email(account, self.sender, sender_is_list=sender_is_list):
            return self
        return SenderRules(self.sender, tuple(r for r in self.rows if is_manual(r)))

    def match(self, categories: Collection[str]) -> tuple[SenderRule | None, SenderRule | None]:
        """The rule that decides for the sender, as `(rule, stale)`.

        The first active rule decides (exact address, then @domain). If its category isn't one
        of `categories` (removed from categories.yaml), it comes back as `stale` and `rule` is
        None: the sender has no rule and Jev decides. It never falls through to the @domain
        rule: an exact rule is the user's exception to it, so applying the domain rule would undo
        that exception. Deciding code calls it on `deciding(…)`, never on all the rules.
        """
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


def is_manual(rule: SenderRule | None) -> bool:
    """`rule` is a manual one (`rules set`): for a sender in decide_each_email, the only kind
    that may decide its emails (`SenderRules.deciding`) and unsubscribe it
    (`policy.unsubscribe_gate`)."""
    return rule is not None and rule.source == RuleSource.MANUAL


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
    calendar: bool = False,
) -> None:
    """Count consistent high-confidence answers; activate after cfg.auto_rule.hits.

    Only for Jev decisions (`DecisionSource.JEV`) outside dry_run: the caller checks.
    `count=False` (an email whose answer was already learned from) adds no hit, creates no rule
    and switches no category: each email adds at most one hit. It still applies the resets: the
    keep one below, and a confident answer in another category than a pending auto rule's
    (Jev disagreeing with itself about one email: the sender isn't consistent).
    Only touches automatic rules: manual and correction rules are never changed here. An active
    auto rule whose category was removed from categories.yaml is replaced like a pending one
    (`SenderRules.match` sends its sender to Jev: auto rules are keyed on the exact address, so
    they come first).
    `known` is the sender's rules already read for this email (`load`); without it they are
    read here. `calendar`: the email is a calendar message (`Email.calendar_method`: an
    invitation, a response to one, a cancellation), which never counts (`_counts_toward_rule`).
    """
    other_keep = cfg.keep_probability(classification.probabilities, exclude=decision.category)
    keep_signal = other_keep >= cfg.auto_rule.max_keep_probability
    rule = (known if known is not None else load(session, account, sender)).exact
    if rule is not None and (_is_keep(cfg, decision.category) or keep_signal):
        _reset_pending_archive_rule(cfg, rule)
    if not _counts_toward_rule(
        cfg, classification, decision, previous_contact, keep_signal=keep_signal, calendar=calendar
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
    calendar: bool,
) -> bool:
    """Whether Jev's answer can count toward an auto rule: confident (not low-confidence, at
    least `auto_rule.min_confidence`), no keep category likely enough (`keep_signal`) and not a
    calendar message (`calendar`).

    A calendar message (an invitation, a response to one, a cancellation: ADR 0047) says nothing
    about the sender's other mail, and Google Calendar and Outlook send a response from the
    responder's own address: three "Accepted:" answers would otherwise teach an archiving rule on
    a person. It adds no hit, creates no rule and switches no pending rule's category; the keep
    reset in `record_auto` still applies to it, since a reset only sends more mail to Jev.

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
        or calendar
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
