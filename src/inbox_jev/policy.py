"""Turn a classification into Gmail actions. Pure functions — no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field

from .classifier import Classification
from .config import Account, Action, CategoriesConfig, Mode
from .signals import Signals


@dataclass
class Decision:
    category: str
    confidence: float
    source: str  # jev | rule | allowlist
    low_confidence: bool = False
    downgraded_to_spam: bool = False
    add_labels: list[str] = field(default_factory=list)
    archive: bool = False
    spam: bool = False
    unsubscribe: bool = False


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
) -> Decision:
    if account.is_allowed(sig.sender):
        return _apply(cfg, Decision(category="important", confidence=1.0, source="allowlist"))
    if rule_category and rule_category in cfg.by_key:
        return _apply(cfg, Decision(category=rule_category, confidence=1.0, source="rule"))
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
    if cfg.by_key[category].action == Action.ARCHIVE_UNSUBSCRIBE and not (
        sig.dmarc_pass and sig.has_unsubscribe
    ):
        spam = next((c for c in cfg.categories if c.action == Action.SPAM), None)
        if spam:
            decision.category = spam.key
            decision.downgraded_to_spam = True

    threshold = account.min_confidence.get(
        decision.category, cfg.by_key[decision.category].min_confidence
    )
    if decision.confidence < threshold:
        decision.low_confidence = True
        decision.add_labels += [cfg.review_label, cfg.by_key[decision.category].label]
    else:
        _apply(cfg, decision)

    return _add_flags(cfg, classification, decision)


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
    decision.archive = category.action in (Action.ARCHIVE, Action.ARCHIVE_UNSUBSCRIBE)
    decision.spam = category.action == Action.SPAM
    decision.unsubscribe = category.action == Action.ARCHIVE_UNSUBSCRIBE
    return decision


def plan(cfg: CategoriesConfig, decision: Decision, mode: Mode) -> Plan:
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
