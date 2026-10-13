"""One triage pass over an account's inbox."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from sqlalchemy import Update, and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from . import corrections, rules
from . import unsubscribe as unsub
from .classifier import Classification, Classifier, JevError, build_state
from .config import FREE_MAIL, Account, CategoriesConfig, Mode, Settings
from .db import MAX_ERROR_ATTEMPTS, Message, MessageStatus, Run, SenderRule, Unsubscribe, now
from .gmail import (
    Email,
    Gmail,
    is_not_found,
    is_token_refresh_error,
    load_credentials,
    token_error_detail,
)
from .policy import (
    Decision,
    DecisionSource,
    Plan,
    Veto,
    acts_on,
    decide,
    flags_only,
    is_allowed,
    plan,
    unsubscribe_gate,
    unsubscribes,
)
from .signals import ADDRESS, Signals, domain_of, resolve_relay
from .system_labels import INBOX, SPAM, STARRED, TRASH
from .unsubscribe import UnsubscribeStatus

log = logging.getLogger(__name__)

MAX_UNSUB_ATTEMPTS = 3
# An email whose processing fails is retried with exponential backoff (5, 10, 20… minutes after
# each failure) and left alone (status stays `error`) after db.MAX_ERROR_ATTEMPTS failed passes,
# about 10.5 hours after the first one, so an outage of a few hours doesn't make it give up.
ERROR_RETRY_BACKOFF = timedelta(minutes=5)


@dataclass
class RunCounts:
    processed: int = 0
    errors: int = 0


@dataclass
class UnsubscribeBudget:
    """Unsubscribe attempts left in one `run` or `unsubscribe-retry` call (the run cap)."""

    remaining: int

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    def spend(self) -> None:
        """Count one attempt."""
        self.remaining -= 1


class JevClient(Protocol):
    """What triage needs from Jev: a `classifier.Classifier`, or a fake one in tests."""

    def classify(self, state: dict) -> Classification: ...
    def close(self) -> None: ...


class Context:
    """What one CLI invocation shares across its accounts: settings, categories, DB, Jev.

    `classifier` injects the Jev client (tests pass a fake). Without one, a `Classifier` is
    created from the settings the first time Jev is asked.
    """

    def __init__(
        self,
        settings: Settings,
        cfg: CategoriesConfig,
        sessions: sessionmaker,
        classifier: JevClient | None = None,
    ) -> None:
        self.settings = settings
        self.cfg = cfg
        self.sessions = sessions
        self._classifier = classifier
        # (account, sender_key) of rules to removed categories already logged in this invocation.
        self.stale_rules_logged: set[tuple[str, str]] = set()

    @property
    def classifier(self) -> JevClient:
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
        run = Run(account=account.email, mode=mode)
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
                    "mode": mode,
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
    skip = _not_due(session, account.email, mode, ids)
    budget = UnsubscribeBudget(ctx.settings.max_unsubscribes_per_run)
    for message_id in ids:
        if message_id in skip:
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


# The modes a pass in each mode redoes: an email still in the inbox that was seen in a weaker mode
# (dry_run, or label when running act) is processed once more, so switching modes also covers it.
_WEAKER_MODES: dict[Mode, list[Mode]] = {
    Mode.DRY_RUN: [],
    Mode.LABEL: [Mode.DRY_RUN],
    Mode.ACT: [Mode.DRY_RUN, Mode.LABEL],
}


def _not_due(session: Session, account: str, mode: Mode, ids: list[str]) -> set[str]:
    """Which of the inbox `ids` a pass in `mode` leaves alone.

    Rows done in `mode` or a stronger one (see _WEAKER_MODES), restored rows (the user explicitly
    undid them: never touched again) and errors still waiting for their backoff (`_retry_due`).
    """
    done_query = select(Message.gmail_id).where(
        Message.account == account,
        Message.gmail_id.in_(ids),
        or_(
            Message.restored.is_(True),
            and_(
                Message.status == MessageStatus.DONE,
                Message.mode.not_in(_WEAKER_MODES[mode]),
            ),
        ),
    )
    done = set(session.scalars(done_query))
    errored = select(Message.gmail_id, Message.error_attempts, Message.processed_at).where(
        Message.account == account,
        Message.gmail_id.in_(ids),
        Message.status == MessageStatus.ERROR,
        Message.restored.is_(False),
    )
    started = now()
    done.update(
        gmail_id
        for gmail_id, attempts, last in session.execute(errored)
        if not _retry_due(attempts, last, started)
    )
    return done


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
        Message.status == MessageStatus.DONE,
        Message.mode == Mode.ACT,
        Message.category == category,
        Message.archived.is_(False),
        Message.spammed.is_(False),
        Message.restored.is_(False),
    )
    if low_confidence_only:
        query = query.where(Message.low_confidence.is_(True))
    count = _update_count(session, query.values(mode=Mode.LABEL))
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
        Message.status == MessageStatus.DONE,
        Message.mode != Mode.DRY_RUN,
        Message.restored.is_(False),
        Message.processed_at >= now() - since,
    )
    if category:
        query = query.where(Message.category == category)
    result = RestoreResult()
    for msg in session.scalars(query).all():
        remove = [ids[n] for n in (msg.labels_added or []) if n in ids and n != SPAM]
        add = []
        if msg.archived or msg.spammed:
            add.append(INBOX)
        if msg.spammed:
            remove.append(SPAM)
        try:
            gmail.modify(msg.gmail_id, add=add, remove=remove)
        except Exception as exc:
            if not is_not_found(exc):
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
    query = update(Message).where(Message.account == account, Message.status == MessageStatus.ERROR)
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
    unsub_budget: UnsubscribeBudget,
) -> Message:
    """Decide one inbox email, apply the decision in Gmail and store it (committed).

    The steps: hand off an email the user edited since an earlier pass (`_user_touched`);
    gather the sender rule, Jev's answer and the sent-mail search (`_gather_inputs`); decide
    and plan (`policy`, pure); apply the labels in Gmail and store the outcome; learn an auto
    rule; unsubscribe within the run cap.
    """
    email = gmail.get(message_id)
    sig = signals_for(email, account)
    prev = _find_message(session, account.email, message_id)
    if prev is not None:
        prev.sender = sig.sender  # rows stored before group authors were recovered
        if mode != Mode.DRY_RUN and _user_touched(
            ctx.cfg, session, account, prev, email, label_ids
        ):
            return _leave_alone(session, prev, mode)

    list_row = _ListRow(session, account.email, sig.list_key)
    inputs = _gather_inputs(ctx, session, gmail, account, mode, email, sig, list_row)
    decision = decide(
        ctx.cfg,
        account,
        sig,
        inputs.classification,
        inputs.rule_category,
        previous_contact=inputs.contact,
    )
    actions = plan(ctx.cfg, decision, mode, account.act_categories)

    msg = _upsert_message(session, account.email, email, sig, prev)
    _apply_labels(gmail, message_id, msg, label_ids, actions)
    _store_outcome(msg, mode, inputs.classification, decision, actions)
    _learn_auto_rule(ctx, session, account, mode, sig, inputs, decision)
    if (
        actions.unsubscribe
        and not unsub_budget.exhausted
        and _unsubscribe(session, gmail, account, email, sig, list_row)
    ):
        unsub_budget.spend()

    session.commit()
    _log_processed(account, message_id, sig, inputs, decision, actions)
    return msg


@dataclass(frozen=True)
class _Inputs:
    """What `policy.decide` gets for one email besides its signals."""

    rule: SenderRule | None
    classification: Classification | None  # None: Jev not asked, or its flags-only call failed
    contact: str | None  # previous_contact's answer; None: not checked, or the search failed

    @property
    def rule_category(self) -> str | None:
        return self.rule.category if self.rule else None


class _ListRow:
    """An email's row in the Unsubscribe table, read at most once while the email is processed.

    Both the rule path's sent-mail check (`_rule_needs_contact`) and the unsubscribe step
    (`_unsubscribe`) need it; nothing writes it in between, so they share one read.
    """

    def __init__(self, session: Session, account: str, list_key: str) -> None:
        self._session, self._account, self._list_key = session, account, list_key
        self._read = False
        self._row: Unsubscribe | None = None

    def get(self) -> Unsubscribe | None:
        if not self._read:
            self._row = _unsub_row(self._session, self._account, self._list_key)
            self._read = True
        return self._row


def _leave_alone(session: Session, msg: Message, mode: Mode) -> Message:
    """An email the user edited: leave Gmail and labels_added as they are, done in `mode`.

    Marking it done in this mode is what keeps the next passes from revisiting it.
    """
    msg.status, msg.error, msg.mode = MessageStatus.DONE, None, mode
    msg.error_attempts = 0
    session.commit()
    return msg


def _gather_inputs(
    ctx: Context,
    session: Session,
    gmail: Gmail,
    account: Account,
    mode: Mode,
    email: Email,
    sig: Signals,
    list_row: _ListRow,
) -> _Inputs:
    """The sender rule, Jev's answer and the sent-mail search, each only where it can matter.

    No allowlist entry and no rule: Jev decides, told whether I wrote to the sender. The
    allowlist or a rule to a keep category (`policy.flags_only`): Jev only for the flags, no
    search. Any other rule: the search only if it could stop an unsubscribe
    (`_rule_needs_contact`).
    """
    rule = rule_for(ctx, session, account, sig.sender)
    if not is_allowed(account, sig) and rule is None:
        contact = previous_contact(gmail, account.email, sig.sender)
        classification = ctx.classifier.classify(
            build_state(email, account.email, ctx.settings.body_chars, contact, sig)
        )
        return _Inputs(rule, classification, contact)
    if flags_only(ctx.cfg, account, sig, rule.category if rule else None):
        flags = flags_classification(ctx.classifier, email, account, ctx.settings.body_chars, sig)
        return _Inputs(rule, flags, None)
    if rule is not None and _rule_needs_contact(
        ctx.cfg, account, sig, rule.category, mode, list_row
    ):
        return _Inputs(rule, None, previous_contact(gmail, account.email, sig.sender))
    return _Inputs(rule, None, None)


def _apply_labels(
    gmail: Gmail, message_id: str, msg: Message, label_ids: dict[str, str], actions: Plan
) -> None:
    """Apply the plan in Gmail (labels, INBOX, SPAM), from `msg`'s state before this pass.

    Reprocessing (e.g. label -> act) also drops labels of the previous pass that no longer apply.
    """
    stale = [
        label_ids[name]
        for name in (msg.labels_added or [])
        # SPAM is a defensive guard: only act rows carry it and those are never reprocessed,
        # but if one ever were, removing SPAM here would silently un-spam the email.
        if name not in actions.add_labels and name in label_ids and name != SPAM
    ]
    gmail.modify(
        message_id,
        add=[label_ids.get(name, name) for name in actions.add_labels],
        remove=actions.remove_labels + stale,
    )


def _store_outcome(
    msg: Message,
    mode: Mode,
    classification: Classification | None,
    decision: Decision,
    actions: Plan,
) -> None:
    """Record on the email's row what was decided and what was applied (not committed)."""
    msg.status, msg.error, msg.error_attempts = MessageStatus.DONE, None, 0
    msg.source = decision.source
    msg.jev_category = classification.category if classification else None
    msg.category = decision.category
    msg.confidence = decision.confidence
    msg.probabilities = classification.probabilities if classification else None
    msg.needs_reply = classification.needs_reply if classification else None
    msg.tags = classification.tags if classification else None
    msg.low_confidence = decision.low_confidence
    msg.downgraded_to_spam = decision.downgraded_to_spam
    msg.mode = mode
    msg.labels_added = actions.add_labels
    msg.decided_labels = decision.add_labels
    msg.archived, msg.spammed = actions.archived, actions.spammed
    msg.processed_at = now()


def _learn_auto_rule(
    ctx: Context,
    session: Session,
    account: Account,
    mode: Mode,
    sig: Signals,
    inputs: _Inputs,
    decision: Decision,
) -> None:
    """Count Jev's decision toward an auto rule for the sender (`rules.record_auto`).

    Only Jev's own decisions count, and not in dry_run: a rule learned there would apply
    without Jev (and act) as soon as the mode is label or act.
    """
    classification = inputs.classification
    if classification and decision.source == DecisionSource.JEV and mode != Mode.DRY_RUN:
        rules.record_auto(
            session, ctx.cfg, account.email, sig.sender, classification, decision, inputs.contact
        )


def _log_processed(
    account: Account,
    message_id: str,
    sig: Signals,
    inputs: _Inputs,
    decision: Decision,
    actions: Plan,
) -> None:
    classification = inputs.classification
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
            "previous_contact": inputs.contact,
            "labels": actions.add_labels,
            "archived": actions.archived,
            "spammed": actions.spammed,
            "request_id": classification.request_id if classification else None,
        },
    )


def flags_classification(
    classifier: JevClient, email: Email, account: Account, body_chars: int, sig: Signals
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
    return resolve_relay(email.signals, account.group_domains)


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
        except Exception as exc:
            if not is_not_found(exc):
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
    if not ADDRESS.match(sender):
        return None  # odd characters would change the Gmail search query
    domain = domain_of(sender)
    try:
        if gmail.has_written_to(sender):
            return "wrote_to_sender"
        own = domain_of(account.lower())
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
    current = _current_label_names(email, label_ids)
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


def _current_label_names(email: Email, label_ids: dict[str, str]) -> set[str]:
    """The email's current Gmail labels by name, from `label_ids` (name -> id).

    An id missing from `label_ids` is kept as is: system labels' ids are their names.
    """
    names = {v: k for k, v in label_ids.items()}
    return {names.get(i, i) for i in email.label_ids}


def _user_edit(
    cfg: CategoriesConfig, msg: Message, current: set[str]
) -> tuple[str, list[str]] | None:
    """How the user edited an email we labeled, from its current label names, or None. Pure.

    `starred` (a strong "keep" signal, even after a correction), else nothing once the email
    has a correction, else `removed` (a label of ours is gone) or `added` (one of our labels that
    the last pass didn't set). Returns (reason, labels involved).
    """
    if STARRED in current:
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
    Unsubscribe table. Read-only: `scripts/dry_classify.py` asks it too.
    """
    list_row = _ListRow(session, account.email, sig.list_key)
    return _rule_needs_contact(cfg, account, sig, category, mode, list_row)


def _rule_needs_contact(
    cfg: CategoriesConfig,
    account: Account,
    sig: Signals,
    category: str,
    mode: Mode,
    list_row: _ListRow,
) -> bool:
    """`rule_needs_contact` on the email's `list_row`, which `process` reuses to unsubscribe."""
    if not acts_on(mode, account.act_categories, category):
        return False
    gate = unsubscribe_gate(cfg, account, sig, category, previous_contact=None)
    if not (gate.allowed and gate.contact_unknown):
        return False
    return not _unsub_settled(list_row.get())


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
            Unsubscribe.status == UnsubscribeStatus.FAILED,
            Unsubscribe.attempts < MAX_UNSUB_ATTEMPTS,
        )
    ).all()
    if mode != Mode.ACT:
        return [(row.list_key, f"held: mode is {mode}") for row in rows]
    budget = UnsubscribeBudget(ctx.settings.max_unsubscribes_per_run)
    out: list[tuple[str, str]] = []
    for row in rows:
        out.append(_retry_one(ctx, session, gmail, account, mode, row, budget))
    return out


def _retry_one(
    ctx: Context,
    session: Session,
    gmail: Gmail,
    account: Account,
    mode: Mode,
    row: Unsubscribe,
    budget: UnsubscribeBudget,
) -> tuple[str, str]:
    """One failed unsubscribe through `retry_unsubscribes`' checks, in order: (list, outcome)."""
    msg = _find_message(session, account.email, row.gmail_id)
    if msg is None:
        return row.list_key, "held: no stored message"
    if reason := _stored_user_decision(msg):
        return _skip(session, row, reason)
    if budget.exhausted:
        return row.list_key, "held: run cap reached"
    try:
        email = gmail.get(row.gmail_id)
    except Exception as exc:
        if is_not_found(exc):
            return row.list_key, "held: email deleted in Gmail"
        raise
    current = _current_label_names(email, gmail.labels())
    sig = signals_for(email, account)
    rule = rule_for(ctx, session, account, sig.sender)
    rule_category = rule.category if rule else None
    if reason := _live_user_decision(ctx.cfg, msg, current, rule_category):
        return _skip(session, row, reason)
    category = rule_category or msg.category
    if (
        category is None
        or not unsubscribes(ctx.cfg, category)
        or not acts_on(mode, account.act_categories, category)
    ):
        return row.list_key, f"held: category {category} doesn't unsubscribe here"
    if rule_category is None and (held := _stale_decision(ctx.cfg, account, msg, category)):
        return row.list_key, f"held: {held}"
    gate = unsubscribe_gate(ctx.cfg, account, sig, category, previous_contact=None)
    if gate.allowed and gate.contact_unknown:
        contact = previous_contact(gmail, account.email, sig.sender)
        if contact is None:  # stricter than run: nothing in a manual retry is urgent
            return row.list_key, "held: sent-mail search failed"
        gate = unsubscribe_gate(ctx.cfg, account, sig, category, contact)
    if gate.veto is not None:
        return _skip(session, row, _RETRY_SKIP_REASONS[gate.veto])
    result = unsub.unsubscribe(sig, send_mail=gmail.send)
    record_attempt(row, result)
    session.commit()
    budget.spend()
    return row.list_key, result.status


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
    (`corrections.detect`), any edit `run` leaves alone (`_user_edit`), or a sender rule to a
    category that doesn't unsubscribe.
    """
    if SPAM in current:
        return "reported as spam"
    if TRASH in current:
        return "in the trash"
    if corrected := corrections.detect(msg, current, cfg):
        return f"corrected in Gmail to {corrected}"
    if edit := _user_edit(cfg, msg, current):
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
    row.status, row.error, row.attempted_at = UnsubscribeStatus.SKIPPED, f"retry: {reason}", now()
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
    return row is not None and (
        row.status != UnsubscribeStatus.FAILED or row.attempts >= MAX_UNSUB_ATTEMPTS
    )


def _unsubscribe(
    session: Session,
    gmail: Gmail,
    account: Account,
    email: Email,
    sig: Signals,
    list_row: _ListRow,
) -> bool:
    """Unsubscribe once per (account, list). Returns True if an attempt was made."""
    row = list_row.get()
    if _unsub_settled(row):
        return False

    # Write the email's row (taking SQLite's write lock) before the irreversible request, so a
    # locked DB fails the email before it unsubscribes rather than rolling back the attempt's row.
    session.flush()
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


def _find_message(session: Session, account: str, gmail_id: str) -> Message | None:
    return session.scalar(
        select(Message).where(Message.account == account, Message.gmail_id == gmail_id)
    )


def _upsert_message(
    session: Session, account: str, email: Email, sig: Signals, msg: Message | None
) -> Message:
    """Store the email's metadata on its row `msg` (already looked up), or on a new row."""
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
    msg.status, msg.mode = MessageStatus.ERROR, mode
    msg.error = f"{type(exc).__name__}: {exc}"[:2000]
    msg.error_attempts = (msg.error_attempts or 0) + 1
    msg.processed_at = now()
    session.commit()
    if msg.error_attempts >= MAX_ERROR_ATTEMPTS:
        log.warning(
            "message gave up",
            extra={"account": account, "id": message_id, "attempts": msg.error_attempts},
        )
