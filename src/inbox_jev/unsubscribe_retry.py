"""`inbox-jev unsubscribe-retry`: failed unsubscribes retried through the checks `run` applies."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import corrections, rules
from .config import Account, CategoriesConfig, Mode
from .db import Message, Unsubscribe, now
from .gmail import Email, is_not_found
from .policy import (
    DecisionSource,
    GateResult,
    Veto,
    acts_on,
    category_threshold,
    unsubscribe_gate,
    unsubscribes,
)
from .scan_rows import DomainScanRows
from .signals import Signals
from .system_labels import SPAM, TRASH
from .triage import (
    MAX_UNSUB_ATTEMPTS,
    Context,
    GmailApi,
    UnsubscribeBudget,
    current_label_names,
    effective_mode,
    find_message,
    previous_contact,
    record_attempt,
    rule_for,
    signals_for,
    user_edit,
)
from .unsubscribe import UnsubscribeStatus


def retry_unsubscribes(
    ctx: Context, session: Session, gmail: GmailApi, account: Account
) -> list[tuple[str, str]]:
    """Retry `failed` unsubscribes with the checks `run` applies. Returns (list, outcome) pairs.

    Before any Gmail call: the stored email must exist and not be restored or corrected by the
    user, the mode must be act and the run cap (MAX_UNSUBSCRIBES_PER_RUN per call) not reached.
    After fetching the email: it must not be reported as spam or in the trash, show a correction
    in its live labels (Jev label changed, pulled back to the inbox) or any edit `run` leaves
    alone (`user_edit`: starred, a label of ours removed, another of our labels added). Its
    category is the current sender rule's, else the stored one, which must be an unsubscribing
    category in `act_categories`. A stored category that a rule decided needs that rule to still
    exist (`run` would ask Jev again otherwise); a stored Jev category needs its current
    confidence threshold (account override included) and must not be low-confidence. The sender
    (resolved, group author) must pass `policy.unsubscribe_gate` (not allowlisted, not in
    decide_each_email unless a manual rule decides, DMARC + List-Unsubscribe, not a group or own
    list) and, for categories that spare known contacts, the sent-mail search must say I never
    wrote to them.

    Rows that the user or the sender ruled out (restored, corrected, reported as spam, trashed,
    starred or relabeled, a rule to a keep category, allowlisted, in decide_each_email without a
    manual rule, group, unverified, known contact) become `skipped`, so `run` won't retry them
    either. Rows held back by settings or transient state (mode, act_categories, a removed rule,
    confidence, the cap, a failed sent-mail search, a deleted email, no stored message) are left
    as they are.

    Raises `NotImplementedError` before reading any row or calling Gmail on a database without
    the sent-mail scan upsert (`DomainScanRows`), as `run` does at the start of its pass.
    """
    # A database without the upsert: fails here.
    scans = DomainScanRows(session, account.email, dialects=ctx.scan_dialects)
    mode = effective_mode(ctx.settings, account, None)
    rows = session.scalars(
        select(Unsubscribe).where(
            Unsubscribe.account == account.email,
            Unsubscribe.status == UnsubscribeStatus.FAILED,
            Unsubscribe.attempts < MAX_UNSUB_ATTEMPTS,
        )
    ).all()
    if mode != Mode.ACT:
        return [(row.list_key, f"held: mode is {mode}") for row in rows]
    budget = UnsubscribeBudget(ctx.settings.max_unsubscribes_per_run)
    retry = _RetryPass(ctx, session, gmail, account, mode, budget, scans)
    out = [_retry_one(retry, row) for row in rows]
    session.commit()  # the sent-mail scans of rows held back (DomainScanRows)
    return out


@dataclass(frozen=True)
class _RetryPass:
    """One account's `unsubscribe-retry` call: what each of its rows is retried with."""

    ctx: Context
    session: Session
    gmail: GmailApi
    account: Account
    mode: Mode
    budget: UnsubscribeBudget  # the run cap, spent by each attempt
    scans: DomainScanRows  # the account's sent-mail domain scans, committed with the rows


def _retry_one(p: _RetryPass, row: Unsubscribe) -> tuple[str, str]:
    """One failed unsubscribe through `retry_unsubscribes`' checks, in order: (list, outcome)."""
    cfg, session, account = p.ctx.cfg, p.session, p.account
    msg = find_message(session, account.email, row.gmail_id)
    if msg is None:
        return row.list_key, "held: no stored message"
    if reason := _stored_user_decision(msg):
        return _skip(session, row, reason)
    if p.budget.exhausted:
        return row.list_key, "held: run cap reached"
    email = _fetch(p.gmail, row.gmail_id)
    if email is None:
        return row.list_key, "held: email deleted in Gmail"
    current = current_label_names(email, p.gmail.labels())
    sig = signals_for(email, account)
    rule = rule_for(p.ctx, account, rules.load(session, account.email, sig.sender))
    rule_category = rule.category if rule else None
    manual_rule = rules.is_manual(rule)
    if reason := _live_user_decision(cfg, msg, current, rule_category):
        return _skip(session, row, reason)
    category = rule_category or msg.category
    if (
        category is None
        or not unsubscribes(cfg, category)
        or not acts_on(p.mode, account.act_categories, category)
    ):
        return row.list_key, f"held: category {category} doesn't unsubscribe here"
    if rule_category is None and (held := _stale_decision(cfg, account, msg, category)):
        return row.list_key, f"held: {held}"
    gate = _sender_gate(p, sig, category, manual_rule=manual_rule)
    if gate is None:  # stricter than run: nothing in a manual retry is urgent
        return row.list_key, "held: sent-mail search failed"
    if gate.veto is not None:
        return _skip(session, row, _retry_skip_reason(gate.veto))
    return row.list_key, _unsubscribe_again(p, row, sig)


def _fetch(gmail: GmailApi, message_id: str) -> Email | None:
    """The stored email as Gmail has it now, or None when it was deleted (HTTP 404)."""
    try:
        return gmail.get(message_id)
    except Exception as exc:
        if is_not_found(exc):
            return None
        raise


def _sender_gate(
    p: _RetryPass, sig: Signals, category: str, *, manual_rule: bool
) -> GateResult | None:
    """`policy.unsubscribe_gate` for the sender, with the sent-mail search when only it is left
    to decide; None when that search failed. `manual_rule`: `category` is a manual rule's."""
    cfg, account = p.ctx.cfg, p.account
    gate = unsubscribe_gate(cfg, account, sig, category, None, manual_rule=manual_rule)
    if not (gate.allowed and gate.contact_unknown):
        return gate
    contact = previous_contact(p.gmail, account.email, sig.sender, p.scans)
    if contact is None:
        return None
    return unsubscribe_gate(cfg, account, sig, category, contact, manual_rule=manual_rule)


def _unsubscribe_again(p: _RetryPass, row: Unsubscribe, sig: Signals) -> str:
    """Unsubscribe from the row's list again, store the attempt (committed) and spend the cap.
    Returns the attempt's status."""
    # Take SQLite's write lock before the irreversible request, so a locked DB (e.g. `run`
    # holding it) fails this row before it unsubscribes rather than rolling back the attempt's
    # row after. The flush needs a write to take the lock: the row may have nothing pending
    # (no sent-mail search, a cached verdict, or nothing new to save), so stamp the attempt's
    # time now; `record_attempt` sets it again after the request.
    row.attempted_at = now()
    p.session.flush()
    result = p.ctx.unsubscriber(sig, p.gmail.send)
    record_attempt(row, result)
    p.session.commit()
    p.budget.spend()
    return result.status


def _stored_user_decision(msg: Message) -> str | None:
    """Why the stored row rules a retry out (restored or corrected by the user), or None."""
    if msg.restored:
        return "restored by the user"
    if msg.corrected_category is not None:
        return f"corrected to {msg.corrected_category}"
    return None


def _live_user_decision(
    cfg: CategoriesConfig, msg: Message, current: set[str], rule_category: str | None
) -> str | None:
    """Why the email's live labels or the current sender rule rule a retry out, or None.

    Reported as spam (unsubscribing would confirm the address), in the trash, a correction
    (`corrections.detect`), any edit `run` leaves alone (`user_edit`), or a sender rule to a
    category that doesn't unsubscribe.
    """
    if SPAM in current:
        return "reported as spam"
    if TRASH in current:
        return "in the trash"
    if corrected := corrections.detect(msg, current, cfg):
        return f"corrected in Gmail to {corrected}"
    if edit := user_edit(cfg, msg, current):
        what, labels = edit
        return f"label {what} in Gmail: {', '.join(labels)}" if labels else what
    if rule_category and not unsubscribes(cfg, rule_category):
        return f"sender rule says {rule_category}"
    return None


def _stale_decision(
    cfg: CategoriesConfig, account: Account, msg: Message, category: str
) -> str | None:
    """Why the stored decision, with no sender rule now, no longer backs a retry, or None.

    A stored rule decision needs that rule to still exist (`run` would ask Jev again; not worth
    it for a retry); a stored Jev decision must still clear the category's current threshold.
    """
    if msg.source != DecisionSource.JEV:
        return "rule since removed"
    if not _confident(cfg, account, msg, category):
        return f"confidence {msg.confidence} below the {category} threshold"
    return None


# The `skipped: …` reason unsubscribe-retry stores for a gate veto is the veto's own text, except
# the unverified and group vetoes, which share one. NOT_UNSUBSCRIBING never gets here: the
# category is checked (and held) first.
_RETRY_SKIP_REASON_OVERRIDES = {
    Veto.UNVERIFIED: "unverified sender, group or own list",
    Veto.GROUP: "unverified sender, group or own list",
}


def _retry_skip_reason(veto: Veto) -> str:
    return _RETRY_SKIP_REASON_OVERRIDES.get(veto, veto.value)


def _skip(session: Session, row: Unsubscribe, reason: str) -> tuple[str, str]:
    row.status, row.error, row.attempted_at = UnsubscribeStatus.SKIPPED, f"retry: {reason}", now()
    session.commit()
    return row.list_key, f"skipped: {reason}"


def _confident(cfg: CategoriesConfig, account: Account, msg: Message, category: str) -> bool:
    """Jev's stored answer still clears the category's current threshold, as `decide` requires."""
    threshold = category_threshold(cfg, account, category)
    return not msg.low_confidence and msg.confidence is not None and msg.confidence >= threshold
