"""Turn a classification into Gmail actions. Pure functions — no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from .classifier import Classification
from .config import Account, Action, CategoriesConfig, Mode
from .signals import Signals

ARCHIVE_ACTIONS = (Action.ARCHIVE, Action.ARCHIVE_UNSUBSCRIBE)
# previous_contact values meaning I have written to the sender or their domain.
KNOWN_CONTACT = ("wrote_to_sender", "wrote_to_their_domain")


@dataclass
class Decision:
    category: str
    confidence: float
    source: str  # jev | rule | allowlist
    low_confidence: bool = False
    # Historical name (kept to match the DB column): it now means "unsubscribe skipped:
    # unverified sender". The email keeps its archive_unsubscribe category and is archived only.
    downgraded_to_spam: bool = False
    add_labels: list[str] = field(default_factory=list)
    archive: bool = False
    spam: bool = False
    unsubscribe: bool = False
    # Archived because the archive categories together were confident, not the top one alone.
    archive_group: bool = False
    # Unsubscribe skipped because I wrote to the sender or their domain (the category's
    # unsubscribe_known_contacts is false). Applies on the Jev and rule paths.
    known_contact_kept: bool = False


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
    """`previous_contact` is triage.previous_contact's answer (None = unknown / not checked)."""
    if is_allowed(account, sig):
        return _apply(cfg, Decision(category="important", confidence=1.0, source="allowlist"))
    if rule_category and rule_category in cfg.by_key:
        decision = _apply(cfg, Decision(category=rule_category, confidence=1.0, source="rule"))
        # A rule skips Jev, not the sender checks: manual and correction rules can point at
        # senders that never proved who they are.
        if decision.unsubscribe and not _verified(sig):
            decision.unsubscribe, decision.downgraded_to_spam = False, True
        if _group_list(sig, account):
            decision.unsubscribe = False
        if decision.unsubscribe and previous_contact in KNOWN_CONTACT:
            decision.known_contact_kept = spares_known_contacts(cfg, rule_category)
            decision.unsubscribe = not decision.known_contact_kept
        return decision
    if classification is None:
        raise ValueError("classification is required when no allowlist entry or rule matches")

    category = classification.category
    if category not in cfg.by_key:
        # Unknown answer (config changed under us): treat as uncertain.
        decision = Decision(category="important", confidence=0.0, source="jev", low_confidence=True)
        decision.add_labels.append(cfg.review_label)
        return _add_flags(cfg, classification, decision)

    decision = Decision(category=category, confidence=classification.confidence, source="jev")

    # Never unsubscribe from something that can't prove who it is: it confirms the address.
    # The email keeps its category (unverified isn't fraud) and is only archived, never spammed.
    unverified = cfg.by_key[category].action == Action.ARCHIVE_UNSUBSCRIBE and not _verified(sig)
    decision.downgraded_to_spam = unverified

    threshold = account.min_confidence.get(category, cfg.by_key[category].min_confidence)
    if decision.confidence >= threshold:
        _apply(cfg, decision)
        if decision.unsubscribe and previous_contact in KNOWN_CONTACT:
            decision.known_contact_kept = spares_known_contacts(cfg, category)
        if unverified or decision.known_contact_kept or _group_list(sig, account):
            decision.unsubscribe = False
    elif _archive_group_confident(cfg, account, classification, threshold, previous_contact):
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


def spares_known_contacts(cfg: CategoriesConfig, category: str) -> bool:
    """The category unsubscribes, but not from senders I wrote to (unsubscribe_known_contacts)."""
    c = cfg.by_key.get(category)
    return (
        c is not None
        and c.action == Action.ARCHIVE_UNSUBSCRIBE
        and not c.unsubscribe_known_contacts
    )


def can_unsubscribe(sig: Signals, account: Account) -> bool:
    """The sender checks every unsubscribe needs: verified and not a group or own list."""
    return _verified(sig) and not _group_list(sig, account)


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


GROUP_HOSTS = ("googlegroups.com", "groups.google.com")


def _target_host(uri: str) -> str:
    """Host of an https unsubscribe URL, or domain of a mailto address (lowercased)."""
    if uri.lower().startswith("mailto:"):
        address = uri[len("mailto:") :].split("?", 1)[0]
        return address.rsplit("@", 1)[-1].strip().lower()
    return (urlparse(uri).hostname or "").lower()


def _on_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _group_list(sig: Signals, account: Account) -> bool:
    """Mail relayed by a discussion list or group (e.g. a pitch posted to hello@ or a community).

    Its List-Unsubscribe removes *me* from the group, not the original sender from anything,
    so it is never used: anything with a List-Post header (Google Groups, Mailman, groups.io,
    LISTSERV, Discourse), Google Groups unsubscribe targets, and lists or senders on my domain.
    """
    if sig.is_discussion_list:
        return True
    own = account.email.lower().rsplit("@", 1)[-1]
    hosts = [_target_host(u) for u in (*sig.unsubscribe_https, *sig.unsubscribe_mailto)]
    if any(_on_domain(h, g) for h in hosts for g in GROUP_HOSTS):
        return True
    if sig.relayed_by or sig.group_author:
        return True  # Google Groups relay, trusted or not
    names = [sig.sender_domain, (sig.list_id or "").lower()]
    return any(_on_domain(n, own) for n in names if n)


def _archive_group_confident(
    cfg: CategoriesConfig,
    account: Account,
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
    - a member whose account-level min_confidence is above the floor doesn't count;
    - with `act_categories`, only categories the account acts on count;
    - an email Jev thinks expects a reply (needs_reply >= threshold) stays in Review. For a
      sender I have never written to (or when that's unknown) this only applies when the keep
      categories (important, action_required) together reach
      `archive_group_reply_keep_probability`: every pitch "expects a reply", so the reply
      signal only matters when the email might be from someone who actually needs one. For a
      known contact (I wrote to the sender or their domain) any reply signal blocks the group.
    """
    floor = cfg.archive_group_min_confidence
    top = classification.category
    if floor is None or cfg.by_key[top].action not in ARCHIVE_ACTIONS:
        return False
    if classification.needs_reply >= cfg.needs_reply.threshold:
        if previous_contact in KNOWN_CONTACT:
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
        return key == top or account.min_confidence.get(key, 0.0) <= floor

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
    if mode == Mode.ACT and act_categories is not None and decision.category not in act_categories:
        mode = Mode.LABEL
    if mode == Mode.DRY_RUN:
        return Plan([], [], archived=False, spammed=False, unsubscribe=False)
    labels = list(dict.fromkeys([*decision.add_labels, cfg.processed_label]))
    if mode == Mode.LABEL:
        return Plan(labels, [], archived=False, spammed=False, unsubscribe=False)
    remove = ["INBOX"] if (decision.archive or decision.spam) else []
    if decision.spam:
        labels.append("SPAM")
    return Plan(
        labels,
        remove,
        archived=decision.archive and not decision.spam,
        spammed=decision.spam,
        unsubscribe=decision.unsubscribe,
    )


def rule_keys(sender: str) -> list[str]:
    """Lookup order for sender rules: exact address, then @domain."""
    domain = sender.rsplit("@", 1)[-1]
    return [sender, f"@{domain}"]
