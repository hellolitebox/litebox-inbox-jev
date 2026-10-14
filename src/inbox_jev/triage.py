"""One triage pass over an account's inbox."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Literal, Protocol, Self, cast, get_args

from sqlalchemy import Update, and_, or_, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.expression import Executable

from . import corrections, rules
from . import unsubscribe as unsub
from .classification import Classification
from .classifier import (
    Classifier,
    JevError,
    JevFailure,
    JevNotConfigured,
    build_state,
    describe_jev_error,
    failure_kind,
)
from .config import FREE_MAIL, Account, CategoriesConfig, Mode, Settings
from .db import (
    MAX_ERROR_ATTEMPTS,
    Message,
    MessageStatus,
    Run,
    SenderRule,
    SentDomainScan,
    Unsubscribe,
    now,
)
from .gmail import (
    DomainScan,
    DomainScans,
    DomainVerdict,
    Email,
    is_not_found,
    is_token_refresh_error,
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
from .signals import ADDRESS, Signals, domain_of, is_automated_address, resolve_relay
from .system_labels import INBOX, SPAM, STARRED, TRASH
from .unsubscribe import UnsubscribeStatus

log = logging.getLogger(__name__)

MAX_UNSUB_ATTEMPTS = 3
# An email whose processing fails is retried with exponential backoff (5, 10, 20… minutes after
# each failure) and left alone (status stays `error`) after db.MAX_ERROR_ATTEMPTS failed passes,
# about 10.5 hours after the first one, so an outage of a few hours doesn't make it give up.
ERROR_RETRY_BACKOFF = timedelta(minutes=5)
# A pass stops asking Jev after this many failed calls in a row (each one already retried by the
# SDK): Jev is down, and the rest of the emails that need it wait for the next pass instead of
# each spending a call, its retries and one of its own error attempts.
MAX_CONSECUTIVE_JEV_FAILURES = 3
# Jev refusing this many calls in a row the same way (same error type and HTTP status) is taken as
# a change on its side, not as bad emails: the pass stops asking Jev, and each refused email
# counts an ordinary failure (backoff) instead of giving up at once (ADR 0035).
MAX_IDENTICAL_REJECTIONS = 2


@dataclass
class RunCounts:
    processed: int = 0
    errors: int = 0
    jev_skipped: int = 0  # emails left for the next pass because it stopped asking Jev (logged)


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


class Classifies(Protocol):
    """Anything that answers for one email's Jev state."""

    def classify(self, state: dict) -> Classification: ...


class JevClient(Classifies, Protocol):
    """What triage needs from Jev: a `classifier.Classifier`, or a fake one in tests."""

    def close(self) -> None: ...


class GmailApi(Protocol):
    """What triage needs from Gmail: a `gmail.Gmail`, or `tests/fakes.FakeGmail` (mypy checks
    the fake against this)."""

    def list_inbox_ids(self, lookback: str, limit: int) -> list[str]: ...
    def get(self, message_id: str) -> Email: ...
    def get_headers(self, message_id: str, names: list[str]) -> Email: ...
    def get_label_ids(self, message_ids: Sequence[str]) -> dict[str, list[str] | None]: ...
    def labels(self) -> dict[str, str]: ...
    def ensure_labels(self, names: list[str]) -> dict[str, str]: ...
    def modify(self, message_id: str, add: list[str], remove: list[str]) -> None: ...
    def send(self, to: str, subject: str, body: str) -> None: ...
    def has_written_to(self, query: str, scans: DomainScans | None = None) -> bool: ...


# An account's address -> a Gmail client for it (`gmail.connect` in the CLI, a fake in tests).
GmailFactory = Callable[[str], GmailApi]


class JevUnavailable(Exception):
    """A pass stopped asking Jev (`JevGuard`); the message is the run's `runs.error`."""


class _JevStopped(Exception):
    """Raised instead of asking Jev once the pass stopped doing so: the email isn't to blame and
    is left as it is for the next pass.

    Except on the call that stopped the pass by being refused like the one before it
    (`MAX_IDENTICAL_REJECTIONS`): `refusal` is that refusal, and its email counts an ordinary
    failure for it (see _run_pass).
    """

    def __init__(self, refusal: JevError | None = None) -> None:
        super().__init__()  # no args: the refusal's text can echo the email
        self.refusal = refusal


class JevGuard:
    """The Jev client of one account's pass: stops calling Jev once it is clearly unusable.

    A failure Jev will repeat for every email (`JevFailure.CONFIG`: key revoked, no access,
    unknown model) stops it at once, and the email that hit it is not blamed (`_JevStopped`).
    So do MAX_IDENTICAL_REJECTIONS refusals in a row with the same error type and HTTP status
    (`JevFailure.REJECTED`, e.g. two 422s): Jev refuses whatever it is sent, and the refused
    emails count an ordinary failure rather than giving up (`_JevStopped.refusal`). `_run_pass`
    holds an email's refusal back until the next call shows which case it is (`last_rejection`).
    Otherwise MAX_CONSECUTIVE_JEV_FAILURES failed calls in a row stop it; those failures are the
    emails' own (stored as `error`). A successful call resets both counts. Once stopped, every
    call raises `_JevStopped` without reaching Jev; `stopped` says why.
    """

    def __init__(
        self, client: Classifies, account: str, limit: int = MAX_CONSECUTIVE_JEV_FAILURES
    ) -> None:
        self._client, self._account, self._limit = client, account, limit
        self.consecutive_failures = 0
        self.stopped: str | None = None
        # The latest call's error when Jev refused it (REJECTED), else None; with the number of
        # identical refusals in a row it ends.
        self.last_rejection: JevError | None = None
        self._identical_rejections = 0

    def check(self) -> None:
        """Raise `_JevStopped` if the pass no longer asks Jev (before work only Jev needs)."""
        if self.stopped is not None:
            raise _JevStopped

    def classify(self, state: dict) -> Classification:
        self.check()
        try:
            result = self._client.classify(state)
        except JevError as exc:
            failure = exc
        else:
            self.consecutive_failures = 0
            self.last_rejection, self._identical_rejections = None, 0
            return result
        # Outside the `except`, so nothing raised here chains the Jev error's text (see _run_pass).
        self.consecutive_failures += 1
        kind = failure_kind(failure)
        self._track_rejection(failure if kind is JevFailure.REJECTED else None)
        what = describe_jev_error(failure, request_id=False)
        if kind is JevFailure.CONFIG:
            self._stop(
                f"Jev rejected the API key or model ({what}): check TYPESAFE_API_KEY and "
                "TYPESAFE_MODEL; emails that need Jev wait for the next run"
            )
            raise _JevStopped
        if self._identical_rejections >= MAX_IDENTICAL_REJECTIONS:
            self._stop(
                f"Jev refused {self._identical_rejections} requests in a row the same way "
                f"({what}): likely a change on Jev's side; emails that need Jev wait for the "
                "next run"
            )
            raise _JevStopped(refusal=failure)
        if self.consecutive_failures >= self._limit:
            self._stop(
                f"Jev unavailable: {self.consecutive_failures} calls in a row failed (last: "
                f"{what}); emails that need Jev wait for the next run"
            )
        raise failure

    @property
    def refusing_everything(self) -> bool:
        """Stopped because Jev refused MAX_IDENTICAL_REJECTIONS calls in a row the same way."""
        return self._identical_rejections >= MAX_IDENTICAL_REJECTIONS

    def _track_rejection(self, rejection: JevError | None) -> None:
        """Count `rejection` (None: the call failed another way) toward identical refusals."""
        if rejection is None:
            self._identical_rejections = 0
        elif self.last_rejection is not None and _rejection_key(rejection) == _rejection_key(
            self.last_rejection
        ):
            self._identical_rejections += 1
        else:
            self._identical_rejections = 1
        self.last_rejection = rejection

    def _stop(self, reason: str) -> None:
        self.stopped = reason
        self.last_rejection = None
        log.error("stopped asking Jev", extra={"account": self._account, "reason": reason})


def _rejection_key(exc: JevError) -> tuple[type, int | None]:
    """What makes two refusals "the same way": the SDK error type and the HTTP status."""
    return type(exc), getattr(exc, "status", None)


class Context:
    """What one CLI invocation shares across its accounts: settings, categories, DB, Gmail, Jev.

    `gmail` opens an account's Gmail client (`gmail.connect` in the CLI, a fake in tests).
    `classifier` injects the Jev client (tests pass a fake). Without one, a `Classifier` is
    created from the settings when a `run` pass starts, which fails it (`JevNotConfigured`)
    when TYPESAFE_API_KEY is missing; commands that never ask Jev don't need the key.
    Use it as a context manager (or call `close`) to close the Jev client; the caller that
    created `sessions` disposes of its engine.
    """

    def __init__(
        self,
        settings: Settings,
        cfg: CategoriesConfig,
        sessions: sessionmaker,
        gmail: GmailFactory,
        classifier: JevClient | None = None,
    ) -> None:
        self.settings = settings
        self.cfg = cfg
        self.sessions = sessions
        self.gmail = gmail
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

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


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
                    "jev_skipped": counts.jev_skipped,
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
    jev = JevGuard(ctx.classifier, account.email)  # no API key: fails before touching Gmail
    scans = DomainScanRows(session, account.email)  # a database without the upsert: likewise
    gmail = ctx.gmail(account.email)
    label_ids = gmail.ensure_labels(ctx.cfg.all_labels()) if mode != Mode.DRY_RUN else {}
    if mode != Mode.DRY_RUN:
        corrections.check(session, gmail, account.email, ctx.cfg)

    ids = gmail.list_inbox_ids(
        lookback or ctx.settings.lookback, limit or ctx.settings.max_messages_per_run
    )
    skip = _not_due(session, account.email, mode, ids)
    budget = UnsubscribeBudget(ctx.settings.max_unsubscribes_per_run)
    account_pass = AccountPass(ctx, session, gmail, account, mode, label_ids, budget, jev, scans)
    held: _HeldRejection | None = None  # an email Jev refused, until the next call says why
    for message_id in ids:
        if message_id in skip:
            continue
        # The failure is handled after the `try`, not in its `except`: an error raised while
        # handling it (the rollback, `_record_error`'s commit) would otherwise chain a Jev error,
        # whose text can echo the email, into the traceback the caller logs.
        failure: Exception | None = None
        try:
            process(account_pass, message_id)
        except Exception as exc:
            failure = exc
        else:
            counts.processed += 1
        if failure is not None:
            session.rollback()
        if held is not None and jev.last_rejection is not held.failure:
            _settle_rejection(account_pass, held, counts)  # a later Jev call says whose fault
            held = None
        if failure is None:
            continue
        if isinstance(failure, _JevStopped):
            if failure.refusal is None:  # needed Jev after the pass stopped asking it
                counts.jev_skipped += 1  # untouched
            else:  # refused like the email before it, which stopped the pass
                refusal = failure.refusal
                _fail(account_pass, message_id, refusal, account_pass.fetched, counts, alone=False)
            continue
        if is_token_refresh_error(failure):
            raise failure  # token refresh failed: fail the run, don't count it against the email
        rejection = jev.last_rejection
        if rejection is not None and failure is rejection:  # this email, or Jev refusing all?
            held = _HeldRejection(message_id, rejection, account_pass.fetched)
            continue
        _fail(account_pass, message_id, failure, account_pass.fetched, counts)
    if held is not None:  # no later call: Jev refused only this email as far as anyone knows
        _settle_rejection(account_pass, held, counts)
    if jev.stopped is not None:
        raise JevUnavailable(jev.stopped)


@dataclass(frozen=True)
class _HeldRejection:
    """An email whose Jev call was refused (`JevFailure.REJECTED`), not stored yet: if the next
    Jev call is refused the same way, Jev refuses everything and the email doesn't give up."""

    message_id: str
    failure: JevError
    email: Email | None  # as `process` read it, for the error row


def _settle_rejection(p: AccountPass, held: _HeldRejection, counts: RunCounts) -> None:
    """Store a held refusal once the next Jev call (or the pass's end) says whose fault it was.

    Jev refusing every call the same way (`JevGuard.refusing_everything`): an ordinary failure,
    retried after the backoff like a timeout, so two emails Jev really refuses can't stop every
    pass before the emails behind them. Otherwise it was this email: it gives up at once.
    """
    alone = not p.jev.refusing_everything
    _fail(p, held.message_id, held.failure, held.email, counts, alone=alone)


def _fail(
    p: AccountPass,
    message_id: str,
    failure: Exception,
    email: Email | None,
    counts: RunCounts,
    *,
    alone: bool = True,
) -> None:
    """Store `message_id` as failed (`_record_error`, committed) and log it."""
    counts.errors += 1
    _record_error(p, message_id, failure, email, alone=alone)
    account = p.account.email
    if isinstance(failure, JevError):  # its text can echo the email: type and status only
        log.warning(
            "message failed",
            extra={"account": account, "id": message_id, "error": describe_jev_error(failure)},
        )
    else:
        log.error("message failed", extra={"account": account, "id": message_id}, exc_info=failure)


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
    a transient one can carry the token endpoint's raw response body. Nor a Jev error's text,
    which can echo the email (`describe_jev_error`).
    """
    if isinstance(exc, JevUnavailable):
        return str(exc)
    if isinstance(exc, JevNotConfigured):
        return f"{exc}; nothing was processed"
    if isinstance(exc, JevError):
        return describe_jev_error(exc, request_id=False)
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
    gmail: GmailApi,
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


@dataclass
class AccountPass:
    """One account's `run` pass: what each of its emails is processed with (`process`)."""

    ctx: Context
    session: Session
    gmail: GmailApi
    account: Account
    mode: Mode
    label_ids: dict[str, str]  # Gmail label name -> id; empty in dry_run
    unsub_budget: UnsubscribeBudget
    jev: JevGuard
    scans: DomainScanRows  # the account's sent-mail domain scans, saved with each email's commit
    # The email `process` is on, once read from Gmail: what its error row stores if it fails.
    fetched: Email | None = None


# The stages of `process` timed on the "message processed" log line, in milliseconds: reading
# the email from Gmail; the sender rule and the sent-mail search; Jev (state included); applying
# the decision (labels, DB row, auto rule, unsubscribe, commit).
Stage = Literal["fetch", "lookup", "jev", "apply"]


class _StageTimer:
    """Milliseconds spent in each `Stage` while one email is processed. A stage entered more
    than once adds up; one never entered stays 0."""

    def __init__(self) -> None:
        self._ms: dict[Stage, float] = dict.fromkeys(get_args(Stage), 0.0)

    @contextmanager
    def stage(self, name: Stage) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self._ms[name] += (time.perf_counter() - start) * 1000

    def as_ms(self) -> dict[Stage, int]:
        return {name: round(ms) for name, ms in self._ms.items()}


def process(p: AccountPass, message_id: str) -> Message:
    """Decide one inbox email, apply the decision in Gmail and store it (committed).

    The steps: learn a correction the user made in Gmail since an earlier pass
    (`_learn_correction`) and hand off an email the user edited (`_user_edited`); gather the
    sender rule, Jev's answer and the sent-mail search (`_gather_inputs`); decide and plan
    (`policy`, pure); apply the labels in Gmail and store the outcome; learn an auto rule;
    unsubscribe within the run cap. Raises `_JevStopped`, before changing anything, when the
    email needs Jev's decision and `p.jev` has stopped.
    """
    ctx, session, account, mode = p.ctx, p.session, p.account, p.mode
    timer = _StageTimer()
    p.fetched = None
    with timer.stage("fetch"):
        email = p.gmail.get(message_id)
    p.fetched = email
    sig = signals_for(email, account)
    prev = _find_message(session, account.email, message_id)
    counted_before = _counted_toward_auto_rule(prev)
    if prev is not None:
        prev.sender = sig.sender  # rows stored before group authors were recovered
        # In dry_run, or before any label of ours is in Gmail (labels_added), nothing to compare.
        if mode != Mode.DRY_RUN and prev.labels_added:
            current = _current_label_names(email, p.label_ids)
            _learn_correction(ctx.cfg, session, account, prev, current)
            if _user_edited(ctx.cfg, account, prev, current):
                return _leave_alone(session, prev, mode)

    list_row = _ListRow(session, account.email, sig.list_key)
    inputs = _gather_inputs(p, email, sig, list_row, timer)
    decision = decide(
        ctx.cfg,
        account,
        sig,
        inputs.classification,
        inputs.rule_category,
        previous_contact=inputs.contact,
    )
    actions = plan(ctx.cfg, decision, mode, account.act_categories)

    with timer.stage("apply"):
        msg = _upsert_message(session, account.email, email, sig, prev)
        _apply_labels(p.gmail, message_id, msg, p.label_ids, actions)
        _store_outcome(msg, mode, inputs.classification, decision, actions)
        _learn_auto_rule(p, sig, inputs, decision, counted_before)
        if (
            actions.unsubscribe
            and not p.unsub_budget.exhausted
            and _unsubscribe(session, p.gmail, account, email, sig, list_row)
        ):
            p.unsub_budget.spend()
        session.commit()
    _log_processed(account, message_id, sig, inputs, decision, actions, timer)
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
    p: AccountPass, email: Email, sig: Signals, list_row: _ListRow, timer: _StageTimer
) -> _Inputs:
    """The sender rule, Jev's answer and the sent-mail search, each only where it can matter.

    No allowlist entry and no rule: Jev decides, told whether I wrote to the sender (neither is
    asked once `p.jev` has stopped: `_JevStopped`). The allowlist or a rule to a keep category
    (`policy.flags_only`): Jev only for the flags, no search. Any other rule: the search only if
    it could stop an unsubscribe (`_rule_needs_contact`).
    """
    ctx, account, body_chars = p.ctx, p.account, p.ctx.settings.body_chars
    with timer.stage("lookup"):
        rule = rule_for(ctx, p.session, account, sig.sender)
    if not is_allowed(account, sig) and rule is None:
        p.jev.check()
        with timer.stage("lookup"):
            contact = previous_contact(p.gmail, account.email, sig.sender, p.scans)
        with timer.stage("jev"):
            classification = p.jev.classify(
                build_state(email, account.email, body_chars, contact, sig)
            )
        return _Inputs(rule, classification, contact)
    if flags_only(ctx.cfg, account, sig, rule.category if rule else None):
        with timer.stage("jev"):
            flags = flags_classification(p.jev, email, account, body_chars, sig)
        return _Inputs(rule, flags, None)
    if rule is not None:
        with timer.stage("lookup"):
            if _rule_needs_contact(ctx.cfg, account, sig, rule.category, p.mode, list_row):
                contact = previous_contact(p.gmail, account.email, sig.sender, p.scans)
                return _Inputs(rule, None, contact)
    return _Inputs(rule, None, None)


def _apply_labels(
    gmail: GmailApi, message_id: str, msg: Message, label_ids: dict[str, str], actions: Plan
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
    p: AccountPass, sig: Signals, inputs: _Inputs, decision: Decision, counted_before: bool
) -> None:
    """Count Jev's decision toward an auto rule for the sender (`rules.record_auto`).

    Only Jev's own decisions count, and not in dry_run: a rule learned there would apply
    without Jev (and act) as soon as the mode is label or act. An email Jev's answer was already
    learned from (`counted_before`: processed again in a stronger mode, or after `reprocess`)
    adds no hit: an auto rule needs that many different emails. Its new answer can still reset a
    pending rule.
    """
    classification = inputs.classification
    if classification and decision.source == DecisionSource.JEV and p.mode != Mode.DRY_RUN:
        rules.record_auto(
            p.session,
            p.ctx.cfg,
            p.account.email,
            sig.sender,
            classification,
            decision,
            inputs.contact,
            count=not counted_before,
        )


def _counted_toward_auto_rule(row: Message | None) -> bool:
    """Whether an earlier pass already offered Jev's answer on this email to `rules.record_auto`:
    a Jev decision stored in label or act (dry_run learns nothing). Read before this pass
    overwrites the row."""
    return row is not None and row.source == DecisionSource.JEV and row.mode != Mode.DRY_RUN


def _log_processed(
    account: Account,
    message_id: str,
    sig: Signals,
    inputs: _Inputs,
    decision: Decision,
    actions: Plan,
    timer: _StageTimer,
) -> None:
    """The "message processed" line: what was decided and applied, and how long each stage took
    (`timings_ms`). Metadata only, never the email's content."""
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
            "timings_ms": timer.as_ms(),
        },
    )


def flags_classification(
    classifier: Classifies, email: Email, account: Account, body_chars: int, sig: Signals
) -> Classification | None:
    """Jev's answer for an email kept by the allowlist or a keep rule, or None if the call fails.

    Jev only adds Needs Reply and the tags there, so no sent-mail search: its answer can't
    change a keep decision. A failed call doesn't fail the email: it is decided without Jev's
    answer (labeled by the allowlist or the rule, without the flags) and a warning is logged.
    Once the pass has stopped asking Jev (`JevGuard`), it is decided that way without a call.
    """
    try:
        return classifier.classify(build_state(email, account.email, body_chars, None, sig))
    except _JevStopped:
        return None  # the run's error and the "stopped asking Jev" log line say why
    except JevError as exc:
        failure = exc
    # Outside the `except`, so nothing raised here chains the Jev error's text (see _run_pass).
    log.warning(
        "flags-only Jev call failed",
        extra={
            "account": account.email,
            "id": email.id,
            "error_type": type(failure).__name__,
            "error": describe_jev_error(failure),
        },
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


def backfill_senders(session: Session, gmail: GmailApi, account: Account) -> tuple[int, int, int]:
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


class DomainScanRows:
    """An account's sent-mail domain scans (`gmail.DomainScans`) in `sent_domain_scans`.

    On the caller's session and never committed here: a scan is saved with the email it was
    made for (a rolled-back email loses it, and the next check reads those emails again). Its
    own session would wait on the pass's write lock in SQLite.

    `save` is an upsert, so a row another process inserted since `load` (`run` and
    `unsubscribe-retry` checking the same new domain) can't fail the commit with a unique
    conflict: the last scan saved wins, except over a `person` made under the same rules, which
    stays (it is final). Either scan is gap-free on its own, so the next check goes on from it.
    """

    def __init__(self, session: Session, account: str) -> None:
        # Checked here, not at the first save: `previous_contact` must not reach a domain
        # verdict it then can't store (its caller would see an unknown contact, no veto).
        self._dialect = session.get_bind().dialect.name
        _require_scan_upsert(self._dialect)
        self._session, self._account = session, account

    def load(self, domain: str) -> DomainScan | None:
        # populate_existing: `save` writes with Core, so a row already in the session is stale.
        row = self._session.scalar(
            select(SentDomainScan)
            .where(SentDomainScan.account == self._account, SentDomainScan.domain == domain)
            .execution_options(populate_existing=True)
        )
        if row is None:
            return None
        return DomainScan(
            domain=row.domain,
            rules=row.rules,
            verdict=DomainVerdict(row.verdict),
            verdict_at=_utc(row.verdict_at),
            newest_s=row.newest_s,
            newest_ids=list(row.newest_ids or []),
            oldest_s=row.oldest_s,
            oldest_ids=list(row.oldest_ids or []),
            reached_end=row.reached_end,
            read=row.read,
        )

    def save(self, scan: DomainScan) -> None:
        self._session.execute(scan_upsert(self._dialect, self._account, scan))


# The databases with the ON CONFLICT upsert `scan_upsert` writes: the ones the app documents.
_SCAN_UPSERT_DIALECTS = ("sqlite", "postgresql")


def _require_scan_upsert(dialect: str) -> None:
    """Raise NotImplementedError on a database `scan_upsert` has no statement for."""
    if dialect not in _SCAN_UPSERT_DIALECTS:
        raise NotImplementedError(f"no upsert for sent_domain_scans on {dialect}")


def scan_upsert(dialect: str, account: str, scan: DomainScan) -> Executable:
    """The statement `DomainScanRows.save` runs: insert the scan, or update the account's row
    for its domain unless that row is a `person` made under the same rules. ON CONFLICT exists
    in the databases the app documents, SQLite and Postgres."""
    _require_scan_upsert(dialect)
    insert: sqlite.Insert | postgresql.Insert
    if dialect == "sqlite":
        insert = sqlite.insert(SentDomainScan)
    else:
        insert = postgresql.insert(SentDomainScan)
    values = {
        "rules": scan.rules,
        "verdict": scan.verdict.value,
        "verdict_at": scan.verdict_at,
        "newest_s": scan.newest_s,
        "newest_ids": list(scan.newest_ids),
        "oldest_s": scan.oldest_s,
        "oldest_ids": list(scan.oldest_ids),
        "reached_end": scan.reached_end,
        "read": scan.read,
        "updated_at": now(),
    }
    insert = insert.values(account=account, domain=scan.domain, **values)
    stored = SentDomainScan
    return insert.on_conflict_do_update(
        index_elements=[stored.account, stored.domain],
        set_=values,
        where=or_(
            stored.verdict != DomainVerdict.PERSON.value, stored.rules != insert.excluded.rules
        ),
    )


def _utc(at: datetime | None) -> datetime | None:
    """`at` as an aware UTC datetime: SQLite returns naive ones (stored as UTC)."""
    return at.replace(tzinfo=UTC) if at is not None and at.tzinfo is None else at


def previous_contact(
    gmail: GmailApi, account: str, sender: str, scans: DomainScans | None = None
) -> str | None:
    """wrote_to_sender | wrote_to_their_domain | never, or None if it can't be checked.

    Writing to a machine address isn't knowing someone: an automated sender
    (`signals.is_automated_address`, e.g. a no-reply or bounce address) is never searched for
    itself, the address search only counts emails that went to the sender's own address, and
    the domain search (`Gmail.has_written_to`) ignores emails sent only to machine addresses on
    the domain. The domain check is skipped for free mail providers (a gmail.com sender says
    nothing about who else I wrote to) and for my own domain (colleagues are allowlisted, if at
    all). `scans` keeps the domain search's progress between runs (`DomainScanRows`); without
    it, it lasts as long as `gmail`. A database error saving it (or a database it can't be
    saved in) is raised, not `None`.
    """
    sender = sender.lower()
    if not ADDRESS.match(sender):
        return None  # odd characters would change the Gmail search query
    domain = domain_of(sender)
    try:
        if not is_automated_address(sender) and gmail.has_written_to(sender):
            return "wrote_to_sender"
        own = domain_of(account.lower())
        if domain not in FREE_MAIL and domain != own and gmail.has_written_to(domain, scans):
            return "wrote_to_their_domain"
    except (SQLAlchemyError, NotImplementedError):
        # Saving the domain scan failed (e.g. the database is locked, or has no upsert): not an
        # unknown contact, which `run` may unsubscribe; the caller fails before any request.
        raise
    except Exception:
        log.warning("previous contact check failed", extra={"account": account}, exc_info=True)
        return None
    return "never"


def _learn_correction(
    cfg: CategoriesConfig, session: Session, account: Account, msg: Message, current: set[str]
) -> None:
    """Record a changed category label on an email labeled by an earlier pass as a correction
    (`corrections.record`: the sender rule then decides the category). Not committed.

    Only the first correction counts: an email already corrected is left as it is.
    """
    if msg.corrected_category is not None:
        return
    category = corrections.detect(msg, current, cfg)
    if category:
        corrections.record(session, account.email, msg, category)


def _user_edited(cfg: CategoriesConfig, account: Account, msg: Message, current: set[str]) -> bool:
    """Whether the user edited an email labeled by an earlier pass (`_user_edit`), logged.

    Read-only: `process` records any correction first (`_learn_correction`), so a corrected
    email is only handed off when starred.
    """
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
    ctx: Context, session: Session, gmail: GmailApi, account: Account
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

    Raises `NotImplementedError` before reading any row or calling Gmail on a database without
    the sent-mail scan upsert (`DomainScanRows`), as `run` does at the start of its pass.
    """
    scans = DomainScanRows(session, account.email)  # a database without the upsert: fails here
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
        out.append(_retry_one(ctx, session, gmail, account, mode, row, budget, scans))
    session.commit()  # the sent-mail scans of rows held back (DomainScanRows)
    return out


def _retry_one(
    ctx: Context,
    session: Session,
    gmail: GmailApi,
    account: Account,
    mode: Mode,
    row: Unsubscribe,
    budget: UnsubscribeBudget,
    scans: DomainScanRows,
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
        contact = previous_contact(gmail, account.email, sig.sender, scans)
        if contact is None:  # stricter than run: nothing in a manual retry is urgent
            return row.list_key, "held: sent-mail search failed"
        gate = unsubscribe_gate(ctx.cfg, account, sig, category, contact)
    if gate.veto is not None:
        return _skip(session, row, _RETRY_SKIP_REASONS[gate.veto])
    # Take SQLite's write lock before the irreversible request, so a locked DB (e.g. `run`
    # holding it) fails this row before it unsubscribes rather than rolling back the attempt's
    # row after. The flush needs a write to take the lock: the row may have nothing pending
    # (no sent-mail search, a cached verdict, or nothing new to save), so stamp the attempt's
    # time now; `record_attempt` sets it again after the request.
    row.attempted_at = now()
    session.flush()
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
    gmail: GmailApi,
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
    # There is always a write: `_store_outcome` has just set the row's `processed_at`.
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
    _store_metadata(msg, email, sig)
    return msg


def _store_metadata(msg: Message, email: Email, sig: Signals) -> None:
    """The email's thread, sender (resolved: `signals_for`), From, Subject and date on `msg`."""
    msg.thread_id = email.thread_id
    msg.sender = sig.sender
    msg.from_header = email.header("From")
    msg.subject = email.header("Subject")
    msg.received_at = datetime.fromtimestamp(email.internal_date_ms / 1000, UTC)


def _record_error(
    p: AccountPass, message_id: str, exc: Exception, email: Email | None, *, alone: bool = True
) -> None:
    """Store a failed pass over `message_id` on its row (`status = error`, one more attempt).

    `email` is the email as `process` read it, or None when reading it failed. The row then gets
    its From, Subject, sender, date and thread (`_failed_email_metadata`), so the digest can name
    it; failing to get them never stops the failure from being stored.
    A Jev refusal (`JevFailure.REJECTED`) gives up at once when Jev refused this email `alone`;
    when it refused every call the same way (ADR 0035) it is an ordinary failure, with backoff.
    """
    session, account, mode = p.session, p.account, p.mode
    msg = _find_message(session, account.email, message_id)
    if msg is None:
        msg = Message(account=account.email, gmail_id=message_id, thread_id="", sender="")
        session.add(msg)
    _failed_email_metadata(p, msg, email, exc)
    msg.status, msg.mode = MessageStatus.ERROR, mode
    msg.error_attempts = (msg.error_attempts or 0) + 1
    if isinstance(exc, JevError):
        msg.error = describe_jev_error(exc)  # never the SDK's text: it can echo the email
        if alone and failure_kind(exc) is JevFailure.REJECTED:
            msg.error_attempts = MAX_ERROR_ATTEMPTS  # Jev refuses this email: retrying won't help
    else:
        msg.error = f"{type(exc).__name__}: {exc}"[:2000]
    msg.processed_at = now()
    session.commit()
    if msg.error_attempts >= MAX_ERROR_ATTEMPTS:
        log.warning(
            "message gave up",
            extra={"account": account.email, "id": message_id, "attempts": msg.error_attempts},
        )


# What a failed email's row needs when reading the email failed: From and Subject for the digest,
# and the headers `signals_for` resolves the sender from. No body (format=metadata).
_ERROR_ROW_HEADERS = ["Subject", *_SENDER_HEADERS]


def _failed_email_metadata(
    p: AccountPass, msg: Message, email: Email | None, failure: Exception
) -> None:
    """Store a failed email's metadata on its row `msg` (not committed), best effort.

    From the email `process` read; when reading it failed (`failure`), from one metadata-only
    read (`gmail.get_headers`, headers only, no retries), unless the row already has its From or
    Subject from an earlier pass or Gmail said the email is gone (404: that read would fail too).
    Any error doing so is logged (type only) and the row is left without them: it must never
    stop the failure from being stored.
    """
    try:
        if email is None:
            if msg.from_header or msg.subject or is_not_found(failure):
                return
            email = p.gmail.get_headers(msg.gmail_id, _ERROR_ROW_HEADERS)
        _store_metadata(msg, email, signals_for(email, p.account))
    except Exception as exc:
        log.warning(
            "failed email: metadata unavailable",
            extra={
                "account": p.account.email,
                "id": msg.gmail_id,
                "error_type": type(exc).__name__,
            },
        )
