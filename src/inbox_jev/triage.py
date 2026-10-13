"""One triage pass over an account's inbox."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import Update, and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from . import corrections, rules
from . import unsubscribe as unsub
from .classifier import Classification, Classifier, JevError, build_state
from .config import FREE_MAIL, Account, CategoriesConfig, Mode, Settings
from .db import MAX_ERROR_ATTEMPTS, Message, Run, SenderRule, Unsubscribe, now
from .gmail import Email, Gmail, is_token_refresh_error, load_credentials, token_error_detail
from .policy import (
    Veto,
    acts_on,
    decide,
    flags_only,
    is_allowed,
    plan,
    unsubscribe_gate,
    unsubscribes,
)
from .signals import Signals, resolve_relay

log = logging.getLogger(__name__)

MAX_UNSUB_ATTEMPTS = 3
# An email whose processing fails is retried with exponential backoff (5, 10, 20… minutes after
# each failure) and left alone (status stays `error`) after db.MAX_ERROR_ATTEMPTS failed passes,
# about 10.5 hours after the first one, so an outage of a few hours doesn't make it give up.
ERROR_RETRY_BACKOFF = timedelta(minutes=5)
SAFE_ADDRESS = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")


@dataclass
class RunCounts:
    processed: int = 0
    errors: int = 0


@dataclass
class Context:
    settings: Settings
    cfg: CategoriesConfig
    sessions: sessionmaker
    _classifier: Classifier | None = None
    # (account, sender_key) of rules to removed categories already logged in this invocation.
    stale_rules_logged: set[tuple[str, str]] = field(default_factory=set)

    @property
    def classifier(self) -> Classifier:
        if self._classifier is None:
            s = self.settings
            self._classifier = Classifier(s.typesafe_api_key, s.typesafe_model, self.cfg)
        return self._classifier

    def close(self) -> None:
        if self._classifier:
            self._classifier.close()


def effective_mode(settings: Settings, account: Account, override: Mode | None) -> Mode:
    return override or account.mode or settings.mode


def run_account(
    ctx: Context,
    account: Account,
    mode_override: Mode | None = None,
    limit: int | None = None,
    lookback: str | None = None,
) -> Run:
    """One pass over an account's inbox, recorded as a `Run` row.

    The row is committed first, so any failure (bad config, an expired Gmail token, Gmail or DB
    errors) is stored in `runs.error` with `finished_at` set, then re-raised for the caller.
    """
    mode = effective_mode(ctx.settings, account, mode_override)
    with ctx.sessions() as session:
        run = Run(account=account.email, mode=mode.value)
        session.add(run)
        session.commit()
        # Counted outside the ORM object: a rollback after a failed email would reset it.
        counts = RunCounts()
        try:
            _run_pass(ctx, session, account, mode, limit, lookback, counts)
        except Exception as exc:
            session.rollback()
            run.error = describe_run_error(exc, account.email)
            raise
        finally:
            run.processed, run.errors = counts.processed, counts.errors
            run.finished_at = now()
            session.add(run)
            session.commit()
            log.info(
                "run finished",
                extra={
                    "account": account.email,
                    "mode": mode.value,
                    "processed": run.processed,
                    "errors": run.errors,
                    "failed": run.error is not None,
                },
            )
    return run


def _run_pass(
    ctx: Context,
    session: Session,
    account: Account,
    mode: Mode,
    limit: int | None,
    lookback: str | None,
    counts: RunCounts,
) -> None:
    unknown = set(account.act_categories or []) - set(ctx.cfg.by_key)
    if unknown:
        raise ValueError(f"{account.email}: unknown act_categories {sorted(unknown)}")
    gmail = Gmail(load_credentials(ctx.settings.google_tokens_dir, account.email))
    label_ids = gmail.ensure_labels(ctx.cfg.all_labels()) if mode != Mode.DRY_RUN else {}
    if mode != Mode.DRY_RUN:
        corrections.check(session, gmail, account.email, ctx.cfg)

    ids = gmail.list_inbox_ids(
        lookback or ctx.settings.lookback, limit or ctx.settings.max_messages_per_run
    )
    # Emails still in the inbox that were seen in a weaker mode (dry_run, or label when running
    # act) get processed once more, so switching modes also covers them. Restored rows are never
    # touched again: the user explicitly undid them. Errors wait for their backoff (`_retry_due`).
    weaker = {
        Mode.DRY_RUN: [],
        Mode.LABEL: [Mode.DRY_RUN],
        Mode.ACT: [Mode.DRY_RUN, Mode.LABEL],
    }
    done_query = select(Message.gmail_id).where(
        Message.account == account.email,
        Message.gmail_id.in_(ids),
        or_(
            Message.restored.is_(True),
            and_(
                Message.status == "done",
                Message.mode.not_in([m.value for m in weaker[mode]]),
            ),
        ),
    )
    done = set(session.scalars(done_query))
    errored = select(Message.gmail_id, Message.error_attempts, Message.processed_at).where(
        Message.account == account.email,
        Message.gmail_id.in_(ids),
        Message.status == "error",
        Message.restored.is_(False),
    )
    started = now()
    done.update(
        gmail_id
        for gmail_id, attempts, last in session.execute(errored)
        if not _retry_due(attempts, last, started)
    )
    budget = [ctx.settings.max_unsubscribes_per_run]
    for message_id in ids:
        if message_id in done:
            continue
        try:
            process(ctx, session, gmail, account, mode, label_ids, message_id, budget)
            counts.processed += 1
        except Exception as exc:
            session.rollback()
            if is_token_refresh_error(exc):
                raise  # token refresh failed: fail the run, don't count it against the email
            counts.errors += 1
            _record_error(session, account.email, mode, message_id, exc)
            log.exception("message failed", extra={"account": account.email, "id": message_id})


def _retry_due(attempts: int, last_failure: datetime, at: datetime) -> bool:
    """Whether an email that failed `attempts` times, last at `last_failure`, is retried at `at`."""
    if attempts >= MAX_ERROR_ATTEMPTS:
        return False
    if last_failure.tzinfo is None:  # SQLite returns naive datetimes (stored as UTC)
        last_failure = last_failure.replace(tzinfo=UTC)
    return at >= last_failure + ERROR_RETRY_BACKOFF * 2 ** max(attempts - 1, 0)


def describe_run_error(exc: BaseException, account: str) -> str:
    """The `runs.error` text: the exception, or what to do when the Gmail token can't refresh.

    Never includes a token refresh failure's own text unless it is the `invalid_grant` summary:
    a transient one can carry the token endpoint's raw response body.
    """
    detail = token_error_detail(exc)
    if detail is not None:
        return (
            f"Gmail token expired or revoked ({detail[:200]}): run `inbox-jev auth add {account}`"
        )
    if is_token_refresh_error(exc):
        if getattr(exc, "retryable", False):
            return "Gmail token refresh failed (temporary); will retry next run"
        return (
            "Gmail token refresh failed (not invalid_grant); will retry next run, "
            f"run `inbox-jev auth add {account}` if it persists"
        )
    return f"{type(exc).__name__}: {exc}"[:2000]


def mark_for_reprocess(
    session: Session,
    cfg: CategoriesConfig,
    account: str,
    category: str,
    low_confidence_only: bool = False,
) -> int:
    """Mark act-mode rows still in the inbox as `label`, so the next `act` run redoes them.

    Only rows Jev left in the inbox (not archived, spammed or restored) qualify. The next run
    takes them through the normal path (user-edit checks, stale label removal), but only if
    they are within its `--lookback` window. Returns how many rows were marked.
    """
    if category not in cfg.by_key:
        raise ValueError(f"unknown category {category!r}; one of {', '.join(cfg.by_key)}")
    query = update(Message).where(
        Message.account == account,
        Message.status == "done",
        Message.mode == Mode.ACT.value,
        Message.category == category,
        Message.archived.is_(False),
        Message.spammed.is_(False),
        Message.restored.is_(False),
    )
    if low_confidence_only:
        query = query.where(Message.low_confidence.is_(True))
    count = _update_count(session, query.values(mode=Mode.LABEL.value))
    session.commit()
    return count


@dataclass
class RestoreResult:
    restored: int = 0
    deleted: int = 0  # gone from Gmail (HTTP 404): left as they are


def restore(
    session: Session,
    gmail: Gmail,
    account: str,
    since: timedelta,
    category: str | None = None,
) -> RestoreResult:
    """Undo label/act passes processed in the last `since`: `inbox-jev restore`.

    For each `done` row not processed in `dry_run` (which changed nothing in Gmail) and not
    already restored, optionally only in `category`: archived or spammed emails go back to
    INBOX, the labels we added and SPAM are removed, and the row is marked `restored`, so `run`
    never processes it again, it is never learned as a correction and `unsubscribe-retry` skips
    its failed unsubscribe. Each email is committed on its own. Unsubscribes can't be undone.

    An email Gmail no longer has (HTTP 404) is skipped and its row left unchanged, so one
    deleted email doesn't stop the rest from being restored; any other Gmail error is raised.
    """
    ids = gmail.labels()
    query = select(Message).where(
        Message.account == account,
        Message.status == "done",
        Message.mode != Mode.DRY_RUN.value,
        Message.restored.is_(False),
        Message.processed_at >= now() - since,
    )
    if category:
        query = query.where(Message.category == category)
    result = RestoreResult()
    for msg in session.scalars(query).all():
        remove = [ids[n] for n in (msg.labels_added or []) if n in ids and n != "SPAM"]
        add = []
        if msg.archived or msg.spammed:
            add.append("INBOX")
        if msg.spammed:
            remove.append("SPAM")
        try:
            gmail.modify(msg.gmail_id, add=add, remove=remove)
        except Exception as exc:  # googleapiclient.errors.HttpError 404
            if not _not_found(exc):
                raise
            log.warning("restore: email deleted", extra={"account": account, "id": msg.gmail_id})
            result.deleted += 1
            continue
        msg.restored = True
        session.commit()
        result.restored += 1
    return result


def _update_count(session: Session, statement: Update) -> int:
    """Rows an UPDATE touched. `Session.execute` types any statement as `Result`; the
    cursor of a DML statement is a `CursorResult`, which has `rowcount`."""
    return cast(CursorResult[Any], session.execute(statement)).rowcount


def reset_error_attempts(session: Session, account: str) -> int:
    """Have the next run retry every email stored as `error`, including those that gave up.

    Only emails still within that run's `--lookback` window are retried. Returns how many rows
    were reset.
    """
    query = update(Message).where(Message.account == account, Message.status == "error")
    count = _update_count(session, query.values(error_attempts=0))
    session.commit()
    return count


def process(
    ctx: Context,
    session: Session,
    gmail: Gmail,
    account: Account,
    mode: Mode,
    label_ids: dict[str, str],
    message_id: str,
    unsub_budget: list[int],
) -> Message:
    email = gmail.get(message_id)
    sig = signals_for(email, account)
    prev = _find_message(session, account.email, message_id)
    if prev is not None:
        prev.sender = sig.sender  # rows stored before group authors were recovered
    if (
        prev is not None
        and mode != Mode.DRY_RUN
        and _user_touched(ctx.cfg, session, account, prev, email, label_ids)
    ):
        # Leave Gmail and labels_added as they are; just don't revisit it in this mode.
        prev.status, prev.error, prev.mode = "done", None, mode.value
        prev.error_attempts = 0
        session.commit()
        return prev
    rule = rule_for(ctx, session, account, sig.sender)

    classification, contact = None, None
    if not is_allowed(account, sig) and rule is None:
        contact = previous_contact(gmail, account.email, sig.sender)
        classification = ctx.classifier.classify(
            build_state(email, account.email, ctx.settings.body_chars, contact, sig)
        )
    elif flags_only(ctx.cfg, account, sig, rule.category if rule else None):
        classification = flags_classification(
            ctx.classifier, email, account, ctx.settings.body_chars, sig
        )
    elif rule and rule_needs_contact(ctx.cfg, session, account, sig, rule.category, mode):
        contact = previous_contact(gmail, account.email, sig.sender)
    decision = decide(
        ctx.cfg,
        account,
        sig,
        classification,
        rule.category if rule else None,
        previous_contact=contact,
    )
    actions = plan(ctx.cfg, decision, mode, account.act_categories)

    msg = _upsert_message(session, account.email, email, sig)
    # Reprocessing (e.g. label -> act): drop labels from the previous pass that no longer apply.
    stale = [
        label_ids[name]
        for name in (msg.labels_added or [])
        # "SPAM" is a defensive guard: only act rows carry it and those are never reprocessed,
        # but if one ever were, removing SPAM here would silently un-spam the email.
        if name not in actions.add_labels and name in label_ids and name != "SPAM"
    ]
    gmail.modify(
        message_id,
        add=[label_ids.get(name, name) for name in actions.add_labels],
        remove=actions.remove_labels + stale,
    )

    msg.status, msg.error, msg.error_attempts = "done", None, 0
    msg.source = decision.source
    msg.jev_category = classification.category if classification else None
    msg.category = decision.category
    msg.confidence = decision.confidence
    msg.probabilities = classification.probabilities if classification else None
    msg.needs_reply = classification.needs_reply if classification else None
    msg.tags = classification.tags if classification else None
    msg.low_confidence = decision.low_confidence
    msg.downgraded_to_spam = decision.downgraded_to_spam
    msg.mode = mode.value
    msg.labels_added = actions.add_labels
    msg.decided_labels = decision.add_labels
    msg.archived, msg.spammed = actions.archived, actions.spammed
    msg.processed_at = now()

    # Only Jev's own decisions count, and not in dry_run: a rule learned there would apply
    # without Jev (and act) as soon as the mode is label or act.
    if classification and decision.source == "jev" and mode != Mode.DRY_RUN:
        rules.record_auto(
            session, ctx.cfg, account.email, sig.sender, classification, decision, contact
        )
    if (
        actions.unsubscribe
        and unsub_budget[0] > 0
        and _unsubscribe(session, gmail, account, email, sig)
    ):
        unsub_budget[0] -= 1

    session.commit()
    log.info(
        "message processed",
        extra={
            "account": account.email,
            "id": message_id,
            "sender": sig.sender,
            "category": decision.category,
            "confidence": round(decision.confidence, 3),
            "source": decision.source,
            "low_confidence": decision.low_confidence,
            "archive_group": decision.archive_group,
            "previous_contact": contact,
            "labels": actions.add_labels,
            "archived": actions.archived,
            "spammed": actions.spammed,
            "request_id": classification.request_id if classification else None,
        },
    )
    return msg


def flags_classification(
    classifier: Classifier, email: Email, account: Account, body_chars: int, sig: Signals
) -> Classification | None:
    """Jev's answer for an email kept by the allowlist or a keep rule, or None if the call fails.

    Jev only adds Needs Reply and the tags there, so no sent-mail search: its answer can't
    change a keep decision. A failed call doesn't fail the email: it is decided without Jev's
    answer (labeled by the allowlist or the rule, without the flags) and a warning is logged.
    """
    try:
        return classifier.classify(build_state(email, account.email, body_chars, None, sig))
    except JevError as exc:
        log.warning(
            "flags-only Jev call failed",
            extra={"account": account.email, "id": email.id, "error_type": type(exc).__name__},
        )
        return None


def rule_for(ctx: Context, session: Session, account: Account, sender: str) -> SenderRule | None:
    """The sender's rule (`rules.match`): None when its first rule points to a removed category.

    Such a rule makes Jev decide (never the @domain rule behind it) and is logged once per
    invocation, so the user can `rules delete` it or restore the category. It stays in the DB:
    the category may come back, and a manual or correction rule can't be relearned.
    """
    rule, stale = rules.match(session, account.email, sender, ctx.cfg.by_key)
    if stale is not None and (account.email, stale.sender_key) not in ctx.stale_rules_logged:
        ctx.stale_rules_logged.add((account.email, stale.sender_key))
        log.warning(
            "sender rule ignored: unknown category",
            extra={
                "account": account.email,
                "sender_key": stale.sender_key,
                "category": stale.category,
                "rule_source": stale.source,
            },
        )
    return rule


def signals_for(email: Email, account: Account) -> Signals:
    """The email's signals with the group author as sender when the group is trusted.

    Use this, not `email.signals`, wherever the sender drives a decision, a rule or the DB.
    """
    return resolve_relay(email.signals, account.relay_domains)


# Headers signals_for needs to recover a group's author (see signals.original_sender).
_SENDER_HEADERS = ["From", "Authentication-Results", "Mailing-list", "X-Original-Sender"]


def backfill_senders(session: Session, gmail: Gmail, account: Account) -> tuple[int, int, int]:
    """Re-read headers of stored group mail ("… via …" in From) and store its author as sender.

    For rows saved before group authors were recovered, so corrections on them create rules on
    the author, not the group. Idempotent. Skips only emails Gmail no longer has (HTTP 404); any
    other error (auth, quota, 5xx) is raised. Returns (rows checked, updated, skipped).
    """
    rows = session.scalars(
        select(Message).where(
            Message.account == account.email, Message.from_header.contains(" via ")
        )
    ).all()
    updated = skipped = 0
    for msg in rows:
        try:
            email = gmail.get_headers(msg.gmail_id, _SENDER_HEADERS)
        except Exception as exc:  # googleapiclient.errors.HttpError 404
            if not _not_found(exc):
                raise
            log.warning("backfill: email deleted", extra={"account": account.email})
            skipped += 1
            continue
        sender = signals_for(email, account).sender
        if sender and sender != msg.sender:
            msg.sender = sender
            updated += 1
    session.commit()
    return len(rows), updated, skipped


def previous_contact(gmail: Gmail, account: str, sender: str) -> str | None:
    """wrote_to_sender | wrote_to_their_domain | never, or None if it can't be checked.

    The domain check is skipped for free mail providers (a gmail.com sender says nothing about
    who else I wrote to) and for my own domain (colleagues are allowlisted, if at all).
    """
    sender = sender.lower()
    if not SAFE_ADDRESS.match(sender):
        return None  # odd characters would change the Gmail search query
    domain = sender.rsplit("@", 1)[-1]
    try:
        if gmail.has_written_to(sender):
            return "wrote_to_sender"
        own = account.lower().rsplit("@", 1)[-1]
        if domain not in FREE_MAIL and domain != own and gmail.has_written_to(domain):
            return "wrote_to_their_domain"
    except Exception:
        log.warning("previous contact check failed", extra={"account": account}, exc_info=True)
        return None
    return "never"


def _user_touched(
    cfg: CategoriesConfig,
    session: Session,
    account: Account,
    msg: Message,
    email: Email,
    label_ids: dict[str, str],
) -> bool:
    """Check an email labeled by an earlier pass against its current Gmail labels.

    A changed category label is recorded as a correction (the sender rule then decides the
    category). Otherwise, hands off (returns True) if `_user_edit` finds an edit.
    """
    if not msg.labels_added:
        return False  # nothing of ours in Gmail yet (dry_run)
    names = {v: k for k, v in label_ids.items()}
    current = {names.get(i, i) for i in email.label_ids}
    if msg.corrected_category is None:
        category = corrections.detect(msg, current, cfg)
        if category:
            corrections.record(session, account.email, msg, category)
    edit = _user_edit(cfg, msg, current)
    if edit is None:
        return False
    reason, labels = edit
    log.info(
        "left alone: user edited the email",
        extra={"account": account.email, "id": msg.gmail_id, "reason": reason, "labels": labels},
    )
    return True


def _user_edit(
    cfg: CategoriesConfig, msg: Message, current: set[str]
) -> tuple[str, list[str]] | None:
    """How the user edited an email we labeled, from its current label names, or None. Pure.

    `starred` (a strong "keep" signal, even after a correction), else nothing once the email
    has a correction, else `removed` (a label of ours is gone) or `added` (one of our labels that
    the last pass didn't set). Returns (reason, labels involved).
    """
    if "STARRED" in current:
        return "starred", []
    if msg.corrected_category is not None:
        return None
    labels_added = msg.labels_added or []
    if removed := [n for n in labels_added if n != cfg.processed_label and n not in current]:
        return "removed", removed
    managed = set(cfg.all_labels()) - {cfg.processed_label}
    if added := sorted((current & managed) - set(labels_added)):
        return "added", added
    return None


def rule_needs_contact(
    cfg: CategoriesConfig,
    session: Session,
    account: Account,
    sig: Signals,
    category: str,
    mode: Mode,
) -> bool:
    """A rule hit that would unsubscribe unless I wrote to the sender: worth the Gmail searches.

    Only when the category is acted on (`policy.acts_on`), `policy.unsubscribe_gate` lets it
    through with only the sent-mail search left to decide (a category that spares known
    contacts, a verified non-group sender), and the list isn't already settled in the
    Unsubscribe table.
    """
    if not acts_on(mode, account.act_categories, category):
        return False
    gate = unsubscribe_gate(cfg, account, sig, category, previous_contact=None)
    if not (gate.allowed and gate.contact_unknown):
        return False
    return not _unsub_settled(_unsub_row(session, account.email, sig.list_key))


def retry_unsubscribes(
    ctx: Context, session: Session, gmail: Gmail, account: Account
) -> list[tuple[str, str]]:
    """Retry `failed` unsubscribes with the checks `run` applies. Returns (list, outcome) pairs.

    Before any Gmail call: the stored email must exist and not be restored or corrected by the
    user, the mode must be act and the run cap (MAX_UNSUBSCRIBES_PER_RUN per call) not reached.
    After fetching the email: it must not be reported as spam or in the trash, show a correction
    in its live labels (Jev label changed, pulled back to the inbox) or any edit `run` leaves
    alone (`_user_edit`: starred, a label of ours removed, another of our labels added). Its
    category is the current sender rule's, else the stored one, which must be an unsubscribing
    category in `act_categories`. A stored category that a rule decided needs that rule to still
    exist (`run` would ask Jev again otherwise); a stored Jev category needs its current
    confidence threshold (account override included) and must not be low-confidence. The sender
    (resolved, group author) must pass `policy.unsubscribe_gate` (not allowlisted, DMARC +
    List-Unsubscribe, not a group or own list) and, for categories that spare known contacts,
    the sent-mail search must say I never wrote to them.

    Rows that the user or the sender ruled out (restored, corrected, reported as spam, trashed,
    starred or relabeled, a rule to a keep category, allowlisted, group, unverified, known
    contact) become `skipped`, so `run` won't retry them either. Rows held back by settings or
    transient state (mode, act_categories, a removed rule, confidence, the cap, a failed sent-mail
    search, a deleted email, no stored message) are left as they are.
    """
    mode = effective_mode(ctx.settings, account, None)
    rows = session.scalars(
        select(Unsubscribe).where(
            Unsubscribe.account == account.email,
            Unsubscribe.status == "failed",
            Unsubscribe.attempts < MAX_UNSUB_ATTEMPTS,
        )
    ).all()
    if mode != Mode.ACT:
        return [(row.list_key, f"held: mode is {mode.value}") for row in rows]
    out: list[tuple[str, str]] = []
    budget = ctx.settings.max_unsubscribes_per_run
    names: dict[str, str] | None = None
    for row in rows:
        msg = _find_message(session, account.email, row.gmail_id)
        if msg is None:
            out.append((row.list_key, "held: no stored message"))
            continue
        reason = None
        if msg.restored:
            reason = "restored by the user"
        elif msg.corrected_category is not None:
            reason = f"corrected to {msg.corrected_category}"
        if reason:
            out.append(_skip(session, row, reason))
            continue
        if budget <= 0:
            out.append((row.list_key, "held: run cap reached"))
            continue
        try:
            email = gmail.get(row.gmail_id)
        except Exception as exc:
            if _not_found(exc):
                out.append((row.list_key, "held: email deleted in Gmail"))
                continue
            raise
        if names is None:
            names = {v: k for k, v in gmail.labels().items()}
        current = {names.get(i, i) for i in email.label_ids}
        sig = signals_for(email, account)
        rule = rule_for(ctx, session, account, sig.sender)
        rule_category = rule.category if rule else None
        category = rule_category or msg.category
        if "SPAM" in current:
            reason = "reported as spam"  # unsubscribing would confirm the address
        elif "TRASH" in current:
            reason = "in the trash"
        elif corrected := corrections.detect(msg, current, ctx.cfg):
            reason = f"corrected in Gmail to {corrected}"
        elif edit := _user_edit(ctx.cfg, msg, current):
            what, labels = edit
            reason = f"label {what} in Gmail: {', '.join(labels)}" if labels else what
        elif rule_category and not unsubscribes(ctx.cfg, rule_category):
            reason = f"sender rule says {rule_category}"
        if reason:
            out.append(_skip(session, row, reason))
            continue
        if (
            category is None
            or not unsubscribes(ctx.cfg, category)
            or not acts_on(mode, account.act_categories, category)
        ):
            out.append((row.list_key, f"held: category {category} doesn't unsubscribe here"))
            continue
        held = None
        if rule_category is None and msg.source != "jev":
            held = "rule since removed"  # run would ask Jev again; not worth it for a retry
        elif (
            rule_category is None
            and msg.source == "jev"
            and not _confident(ctx.cfg, account, msg, category)
        ):
            held = f"confidence {msg.confidence} below the {category} threshold"
        if held:
            out.append((row.list_key, f"held: {held}"))
            continue
        gate = unsubscribe_gate(ctx.cfg, account, sig, category, previous_contact=None)
        if gate.allowed and gate.contact_unknown:
            contact = previous_contact(gmail, account.email, sig.sender)
            if contact is None:  # stricter than run: nothing in a manual retry is urgent
                out.append((row.list_key, "held: sent-mail search failed"))
                continue
            gate = unsubscribe_gate(ctx.cfg, account, sig, category, contact)
        if gate.veto is not None:
            out.append(_skip(session, row, _RETRY_SKIP_REASONS[gate.veto]))
            continue
        result = unsub.unsubscribe(sig, send_mail=gmail.send)
        record_attempt(row, result)
        session.commit()
        budget -= 1
        out.append((row.list_key, result.status))
    return out


# The `skipped: …` reasons unsubscribe-retry stores for each gate veto. NOT_UNSUBSCRIBING never
# gets here: the category is checked (and held) first.
_RETRY_SKIP_REASONS = {
    Veto.NOT_UNSUBSCRIBING: "category doesn't unsubscribe",
    Veto.ALLOWLISTED: "allowlisted sender",
    Veto.UNVERIFIED: "unverified sender, group or own list",
    Veto.GROUP: "unverified sender, group or own list",
    Veto.KNOWN_CONTACT: "known contact",
}


def _skip(session: Session, row: Unsubscribe, reason: str) -> tuple[str, str]:
    row.status, row.error, row.attempted_at = "skipped", f"retry: {reason}", now()
    session.commit()
    return row.list_key, f"skipped: {reason}"


def _confident(cfg: CategoriesConfig, account: Account, msg: Message, category: str) -> bool:
    """Jev's stored answer still clears the category's current threshold, as `decide` requires."""
    threshold = account.min_confidence.get(category, cfg.by_key[category].min_confidence)
    return not msg.low_confidence and msg.confidence is not None and msg.confidence >= threshold


def _unsub_row(session: Session, account: str, list_key: str) -> Unsubscribe | None:
    return session.scalar(
        select(Unsubscribe).where(Unsubscribe.account == account, Unsubscribe.list_key == list_key)
    )


def _unsub_settled(row: Unsubscribe | None) -> bool:
    """No further attempt will be made: done (ok, or any non-failed status) or out of retries."""
    return row is not None and (row.status != "failed" or row.attempts >= MAX_UNSUB_ATTEMPTS)


def _unsubscribe(
    session: Session, gmail: Gmail, account: Account, email: Email, sig: Signals
) -> bool:
    """Unsubscribe once per (account, list). Returns True if an attempt was made."""
    row = _unsub_row(session, account.email, sig.list_key)
    if _unsub_settled(row):
        return False

    result = unsub.unsubscribe(sig, send_mail=gmail.send)
    if row is None:
        row = Unsubscribe(account=account.email, list_key=sig.list_key, attempts=0)
        session.add(row)
    row.sender, row.gmail_id = sig.sender, email.id
    record_attempt(row, result)
    return True


def record_attempt(row: Unsubscribe, result: unsub.Result) -> None:
    """Store an unsubscribe attempt's result on its row (not committed) and log it.

    The one place `run` and `unsubscribe-retry` update a row after an attempt, so the attempt
    count that settles a list (MAX_UNSUB_ATTEMPTS) moves the same way on both.
    """
    row.method, row.status, row.target = result.method, result.status, result.target
    row.http_status, row.error = result.http_status, result.error
    row.attempts = (row.attempts or 0) + 1
    row.attempted_at = now()
    log.info(
        "unsubscribe",
        extra={
            "account": row.account,
            "list": row.list_key,
            "method": result.method,
            "status": result.status,
        },
    )


def _not_found(exc: BaseException) -> bool:
    """A Gmail API error saying the email no longer exists (HttpError with status 404)."""
    return getattr(getattr(exc, "resp", None), "status", None) == 404


def _find_message(session: Session, account: str, gmail_id: str) -> Message | None:
    return session.scalar(
        select(Message).where(Message.account == account, Message.gmail_id == gmail_id)
    )


def _upsert_message(session: Session, account: str, email: Email, sig: Signals) -> Message:
    msg = _find_message(session, account, email.id)
    if msg is None:
        msg = Message(account=account, gmail_id=email.id)
        session.add(msg)
    msg.thread_id = email.thread_id
    msg.sender = sig.sender
    msg.from_header = email.header("From")
    msg.subject = email.header("Subject")
    msg.received_at = datetime.fromtimestamp(email.internal_date_ms / 1000, UTC)
    return msg


def _record_error(
    session: Session, account: str, mode: Mode, message_id: str, exc: Exception
) -> None:
    msg = _find_message(session, account, message_id)
    if msg is None:
        msg = Message(account=account, gmail_id=message_id, thread_id="", sender="")
        session.add(msg)
    msg.status, msg.mode = "error", mode.value
    msg.error = f"{type(exc).__name__}: {exc}"[:2000]
    msg.error_attempts = (msg.error_attempts or 0) + 1
    msg.processed_at = now()
    session.commit()
    if msg.error_attempts >= MAX_ERROR_ATTEMPTS:
        log.warning(
            "message gave up",
            extra={"account": account, "id": message_id, "attempts": msg.error_attempts},
        )
