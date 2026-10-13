"""Turn a classification into Gmail actions. Pure functions — no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlparse

from .classification import Classification
from .config import PUBLIC_GROUP_HOSTS, Account, Action, CategoriesConfig, Mode
from .signals import Signals, domain_of, on_domain
from .system_labels import INBOX, SPAM

ARCHIVE_ACTIONS = (Action.ARCHIVE, Action.ARCHIVE_UNSUBSCRIBE)
# previous_contact values meaning I have written to the sender or their domain.
KNOWN_CONTACT = ("wrote_to_sender", "wrote_to_their_domain")


class DecisionSource(StrEnum):
    """What decided an email (stored in `messages.source`; values are the stored strings)."""

    JEV = "jev"
    RULE = "rule"
    ALLOWLIST = "allowlist"


@dataclass
class Decision:
    category: str
    confidence: float
    source: DecisionSource
    low_confidence: bool = False
    # Historical name (kept to match the DB column): it now means "unsubscribe skipped:
    # unverified sender". Set only when the decision would otherwise unsubscribe; the email keeps
    # its archive_unsubscribe category and is archived only.
    downgraded_to_spam: bool = False
    add_labels: list[str] = field(default_factory=list)
    archive: bool = False
    spam: bool = False
    unsubscribe: bool = False
    # Archived because the archive categories together were confident, not the top one alone.
    archive_group: bool = False
    # Unsubscribe skipped because I wrote to the sender or their domain (the category's
    # unsubscribe_known_contacts is false), on a verified, non-group sender that would otherwise
    # have been unsubscribed. Same order on every path (unsubscribe_gate).
    known_contact_kept: bool = False


class Veto(StrEnum):
    """Why `unsubscribe_gate` refused an unsubscribe, in the order it checks."""

    NOT_UNSUBSCRIBING = "category doesn't unsubscribe"
    ALLOWLISTED = "allowlisted sender"
    UNVERIFIED = "unverified sender"
    GROUP = "group or own list"
    KNOWN_CONTACT = "known contact"


@dataclass(frozen=True)
class GateResult:
    """`unsubscribe_gate`'s answer.

    `veto` is the first check that refused the unsubscribe, or None when it may go ahead.
    `contact_unknown`: it may go ahead only because `previous_contact` is unknown (None) for a
    category that spares known contacts, so a sent-mail search could still veto it.
    """

    veto: Veto | None
    contact_unknown: bool = False

    @property
    def allowed(self) -> bool:
        return self.veto is None


@dataclass
class Plan:
    """What actually gets applied in the current mode."""

    add_labels: list[str]
    remove_labels: list[str]
    archived: bool
    spammed: bool
    unsubscribe: bool


def decide(
    cfg: CategoriesConfig,
    account: Account,
    sig: Signals,
    classification: Classification | None = None,
    rule_category: str | None = None,
    *,
    previous_contact: str | None = None,
) -> Decision:
    """Decide what happens to one email.

    `previous_contact` is triage.previous_contact's answer (None = unknown / not checked).
    A `rule_category` that isn't in `cfg` (removed from categories.yaml) is ignored, as if there
    were no rule. On the allowlist and rule paths a `classification` only adds the Needs Reply
    and tag labels: the category stays the allowlist's or the rule's.
    """
    if is_allowed(account, sig):
        decision = _apply(
            cfg, Decision(category="important", confidence=1.0, source=DecisionSource.ALLOWLIST)
        )
        return _add_flags(cfg, classification, decision) if classification else decision
    if rule_category in cfg.by_key:
        decision = _apply(
            cfg, Decision(category=rule_category, confidence=1.0, source=DecisionSource.RULE)
        )
        # A rule skips Jev, not the sender checks: manual and correction rules can point at
        # senders that never proved who they are.
        _check_unsubscribe(cfg, account, sig, decision, previous_contact)
        return _add_flags(cfg, classification, decision) if classification else decision
    if classification is None:
        raise ValueError("classification is required when no allowlist entry or rule matches")

    category = classification.category
    if category not in cfg.by_key:
        # Unknown answer (config changed under us): treat as uncertain.
        decision = Decision(
            category="important", confidence=0.0, source=DecisionSource.JEV, low_confidence=True
        )
        decision.add_labels.append(cfg.review_label)
        return _add_flags(cfg, classification, decision)

    decision = Decision(
        category=category, confidence=classification.confidence, source=DecisionSource.JEV
    )
    threshold = account.min_confidence.get(category, cfg.by_key[category].min_confidence)
    if decision.confidence >= threshold:
        _apply(cfg, decision)
        _check_unsubscribe(cfg, account, sig, decision, previous_contact)
    elif _archive_group_confident(cfg, account, sig, classification, threshold, previous_contact):
        # Jev is sure the email gets archived, just not which archive category fits best
        # (e.g. notification 0.55 / newsletter 0.45). Archive it under the top category, but
        # never unsubscribe: that still needs the category's own confidence.
        _apply(cfg, decision)
        decision.unsubscribe = False
        decision.archive_group = True
    else:
        decision.low_confidence = True
        decision.add_labels += [cfg.review_label, cfg.by_key[category].label]

    return _add_flags(cfg, classification, decision)


def _check_unsubscribe(
    cfg: CategoriesConfig,
    account: Account,
    sig: Signals,
    decision: Decision,
    previous_contact: str | None,
) -> None:
    """Apply `unsubscribe_gate` to a decision that would unsubscribe (Jev and rule paths).

    Each flag is set only when its check is what stopped the unsubscribe: `downgraded_to_spam`
    for an unverified sender (the email keeps its category and is only archived),
    `known_contact_kept` for a known contact. Unknown `previous_contact` doesn't stop it.
    """
    if not decision.unsubscribe:
        return
    veto = unsubscribe_gate(cfg, account, sig, decision.category, previous_contact).veto
    if veto is not None:
        decision.unsubscribe = False
        decision.downgraded_to_spam = veto == Veto.UNVERIFIED
        decision.known_contact_kept = veto == Veto.KNOWN_CONTACT


def unsubscribe_gate(
    cfg: CategoriesConfig,
    account: Account,
    sig: Signals,
    category: str,
    previous_contact: str | None,
) -> GateResult:
    """The sender checks every unsubscribe passes, in one order, on every path. Pure.

    1. The category's action is `archive_unsubscribe`.
    2. The sender isn't allowlisted (on `run` the allowlist decides `important` before this).
    3. Verified: DMARC pass and a usable List-Unsubscribe. Unsubscribing from a sender that
       can't prove who it is only confirms the address.
    4. Not a group, discussion list or own-domain list, whose List-Unsubscribe removes *me*.
    5. Not a known contact (`previous_contact` in KNOWN_CONTACT) for a category that spares
       them. Unknown (None) passes, with `contact_unknown` set: `run` goes ahead,
       `unsubscribe-retry` holds the row, and the rule path runs the sent-mail search first.

    Confidence, mode/`act_categories` (`acts_on`), once-per-list and the run cap are the
    callers': they depend on the path or on stored state.
    """
    if not unsubscribes(cfg, category):
        return GateResult(Veto.NOT_UNSUBSCRIBING)
    if is_allowed(account, sig):
        return GateResult(Veto.ALLOWLISTED)
    if not _verified(sig):
        return GateResult(Veto.UNVERIFIED)
    if _group_list(sig, account):
        return GateResult(Veto.GROUP)
    if spares_known_contacts(cfg, category):
        if previous_contact in KNOWN_CONTACT:
            return GateResult(Veto.KNOWN_CONTACT)
        return GateResult(None, contact_unknown=previous_contact is None)
    return GateResult(None)


def acts_on(mode: Mode, act_categories: list[str] | None, category: str) -> bool:
    """Whether a decision in `category` is acted on (archive, spam, unsubscribe) in `mode`.

    Only in `act`, and only for the account's `act_categories` when it sets them (pass
    `account.act_categories`). In `dry_run` and `label`, and for other categories, nothing
    destructive happens.
    """
    return mode == Mode.ACT and (act_categories is None or category in act_categories)


def flags_only(
    cfg: CategoriesConfig, account: Account, sig: Signals, rule_category: str | None
) -> bool:
    """The allowlist or a rule to a keep category decides: Jev is asked only for the flags.

    The email stays in the inbox either way, so Needs Reply and the tags are worth a Jev call.
    A rule to an archive or spam category skips Jev entirely (its email leaves the inbox and
    would never get Needs Reply).
    """
    if is_allowed(account, sig):
        return True
    category = cfg.by_key.get(rule_category) if rule_category else None
    return category is not None and category.action == Action.KEEP


def spares_known_contacts(cfg: CategoriesConfig, category: str) -> bool:
    """The category unsubscribes, but not from senders I wrote to (unsubscribe_known_contacts)."""
    return unsubscribes(cfg, category) and not cfg.by_key[category].unsubscribe_known_contacts


def unsubscribes(cfg: CategoriesConfig, category: str) -> bool:
    """The category's action is archive + unsubscribe (an unknown category doesn't)."""
    c = cfg.by_key.get(category)
    return c is not None and c.action == Action.ARCHIVE_UNSUBSCRIBE


def is_allowed(account: Account, sig: Signals) -> bool:
    """Allowlisted author, or a group listed by exact address in allow_senders.

    A group only counts through allow_senders: allowlisting your own domain (to trust
    colleagues) must not also trust every stranger who writes to hello@yourdomain.
    """
    relay = (sig.relayed_by or "").lower()
    return account.is_allowed(sig.sender) or bool(
        relay and relay in (s.lower() for s in account.allow_senders)
    )


def _verified(sig: Signals) -> bool:
    """Safe to unsubscribe: the sender passed DMARC and offers List-Unsubscribe."""
    return sig.dmarc_pass and sig.has_unsubscribe


def _target_host(uri: str) -> str:
    """Host of an https unsubscribe URL, or domain of a mailto address (lowercased)."""
    if uri.lower().startswith("mailto:"):
        address = uri[len("mailto:") :].split("?", 1)[0]
        return domain_of(address).strip().lower()
    return (urlparse(uri).hostname or "").lower()


def _group_list(sig: Signals, account: Account) -> bool:
    """Mail relayed by a discussion list or group (e.g. a pitch posted to hello@ or a community).

    Its List-Unsubscribe removes *me* from the group, not the original sender from anything,
    so it is never used: anything with a List-Post header (Google Groups, Mailman, groups.io,
    LISTSERV, Discourse), Google Groups unsubscribe targets, and lists or senders on my domain.
    """
    if sig.is_discussion_list:
        return True
    own = domain_of(account.email.lower())
    hosts = [_target_host(u) for u in (*sig.unsubscribe_https, *sig.unsubscribe_mailto)]
    if any(on_domain(h, g) for h in hosts for g in PUBLIC_GROUP_HOSTS):
        return True
    if sig.relayed_by or sig.group_author:
        return True  # Google Groups relay, trusted or not
    names = [sig.sender_domain, (sig.list_id or "").lower()]
    return any(on_domain(n, own) for n in names if n)


def _archive_group_confident(
    cfg: CategoriesConfig,
    account: Account,
    sig: Signals,
    classification: Classification,
    threshold: float,
    previous_contact: str | None = None,
) -> bool:
    """True when the archive categories together reach `archive_group_min_confidence`.

    Only applies when Jev's top answer is itself an archive category. Spam is never part of the
    group: moving to Spam always needs the spam category's own confidence. The group can only add
    confidence, never lower the bar:
    - the floor is at least the top category's own threshold (`threshold`, which already
      includes an account-level min_confidence), so a stricter setting is never bypassed;
    - a member whose threshold (account-level min_confidence, else the category's own) is
      above the floor doesn't count;
    - with `act_categories`, only categories the account acts on count;
    - an email Jev thinks expects a reply (needs_reply >= threshold) stays in Review. For a
      sender I have never written to (or when that's unknown) this only applies when the keep
      categories (important, action_required) together reach
      `archive_group_reply_keep_probability`: every pitch "expects a reply", so the reply
      signal only matters when the email might be from someone who actually needs one. For a
      known contact (I wrote to the sender or their domain) any reply signal blocks the group,
      unless the email can't be replied to (`Signals.no_reply`: a no-reply sender and no
      Reply-To elsewhere); then it is judged like a stranger's, by the keep categories alone.
    """
    floor = cfg.archive_group_min_confidence
    top = classification.category
    if floor is None or cfg.by_key[top].action not in ARCHIVE_ACTIONS:
        return False
    if classification.needs_reply >= cfg.needs_reply.threshold:
        if previous_contact in KNOWN_CONTACT and not sig.no_reply:
            return False
        keep = cfg.keep_probability(classification.probabilities)
        if keep >= cfg.archive_group_reply_keep_probability:
            return False
    floor = max(floor, threshold)
    acts = account.act_categories

    def counts(key: str) -> bool:
        if key not in cfg.by_key or cfg.by_key[key].action not in ARCHIVE_ACTIONS:
            return False
        if acts is not None and key not in acts:
            return False
        member = account.min_confidence.get(key, cfg.by_key[key].min_confidence)
        return key == top or member <= floor

    total = sum(p for key, p in classification.probabilities.items() if counts(key))
    return total >= floor


def _add_flags(
    cfg: CategoriesConfig, classification: Classification, decision: Decision
) -> Decision:
    # Cold outreach also "expects a reply": only flag emails that stay in the inbox
    # (kept categories, low-confidence guesses, unknown categories), never archived or spammed.
    if classification.needs_reply >= cfg.needs_reply.threshold and not (
        decision.archive or decision.spam
    ):
        decision.add_labels.append(cfg.needs_reply.label)
    for tag in cfg.tags:
        if classification.tags.get(tag.key, 0.0) >= tag.threshold:
            decision.add_labels.append(tag.label)
    return decision


def _apply(cfg: CategoriesConfig, decision: Decision) -> Decision:
    category = cfg.by_key[decision.category]
    decision.add_labels.append(category.label)
    decision.archive = category.action in ARCHIVE_ACTIONS
    decision.spam = category.action == Action.SPAM
    decision.unsubscribe = category.action == Action.ARCHIVE_UNSUBSCRIBE
    return decision


def plan(
    cfg: CategoriesConfig,
    decision: Decision,
    mode: Mode,
    act_categories: list[str] | None = None,
) -> Plan:
    if mode == Mode.ACT and not acts_on(mode, act_categories, decision.category):
        mode = Mode.LABEL
    if mode == Mode.DRY_RUN:
        return Plan([], [], archived=False, spammed=False, unsubscribe=False)
    labels = list(dict.fromkeys([*decision.add_labels, cfg.processed_label]))
    if mode == Mode.LABEL:
        return Plan(labels, [], archived=False, spammed=False, unsubscribe=False)
    remove = [INBOX] if (decision.archive or decision.spam) else []
    if decision.spam:
        labels.append(SPAM)
    return Plan(
        labels,
        remove,
        archived=decision.archive and not decision.spam,
        spammed=decision.spam,
        unsubscribe=decision.unsubscribe,
    )


def rule_keys(sender: str) -> list[str]:
    """Lookup order for sender rules: exact address, then @domain."""
    return [sender, f"@{domain_of(sender)}"]
