"""One triage pass over an account's inbox."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cached_property
from types import TracebackType
from typing import Any, Literal, Protocol, Self, cast, get_args

from sqlalchemy import Update, and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from . import corrections, logs, rules
from . import unsubscribe as unsub
from .classification import Classification, PreviousContact
from .classifier import (
    Classifier,
    JevError,
    JevFailure,
    JevNotConfiguredError,
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
    Unsubscribe,
    now,
)
from .gmail import (
    Email,
    is_not_found,
    is_token_refresh_error,
    token_error_detail,
)
from .jev_guard import Classifies, FailedEmail, JevGuard, JevStoppedError, JevUnavailableError
from .policy import (
    Decision,
    DecisionSource,
    Plan,
    acts_on,
    decide,
    flags_only,
    is_allowed,
    plan,
    unsubscribe_gate,
)
from .scan_rows import SCAN_UPSERT_DIALECTS, DomainScanRows
from .sent_scan import DomainScans
from .signals import ADDRESS, Signals, domain_of, is_automated_address, resolve_relay
from .system_labels import INBOX, SPAM, STARRED
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
    def close(self) -> None: ...


# An account's address -> a Gmail client for it (`gmail.connect` in the CLI, a fake in tests).
# Whoever opens a client closes it when the account's work ends (`contextlib.closing`).
GmailFactory = Callable[[str], GmailApi]
# The irreversible unsubscribe request, given the sender's signals and a way to send mail:
# `unsubscribe.unsubscribe`, or a spy in tests.
Unsubscriber = Callable[[Signals, unsub.SendMail], unsub.Result]


class Context:
    """What one CLI invocation shares across its accounts: settings, categories, DB, Gmail, Jev.

    `gmail` opens an account's Gmail client (`gmail.connect` in the CLI, a fake in tests).
    `classifier` injects the Jev client (tests pass a fake). Without one, a `Classifier` is
    created from the settings when a `run` pass starts, which fails it (`JevNotConfiguredError`)
    when TYPESAFE_API_KEY is missing; commands that never ask Jev don't need the key.
    `unsubscriber` makes the unsubscribe requests of `run` and `unsubscribe-retry`, and
    `scan_dialects` are the databases the sent-mail scans are stored in (`DomainScanRows`);
    tests replace them.
    Use it as a context manager (or call `close`) to close the Jev client; the caller that
    created `sessions` disposes of its engine.
    """

    def __init__(
        self,
        settings: Settings,
        cfg: CategoriesConfig,
        sessions: sessionmaker[Session],
        gmail: GmailFactory,
        classifier: JevClient | None = None,
        *,
        unsubscriber: Unsubscriber = unsub.unsubscribe,
        scan_dialects: Collection[str] = SCAN_UPSERT_DIALECTS,
    ) -> None:
        self.settings = settings
        self.cfg = cfg
        self.sessions = sessions
        self.gmail = gmail
        self._classifier = classifier
        self.unsubscriber = unsubscriber
        self.scan_dialects = scan_dialects
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

    The row is committed first, so any failure (no TYPESAFE_API_KEY, an expired Gmail token,
    Gmail or DB errors) is stored in `runs.error` with `finished_at` set, logged with its
    traceback ("account run failed", tagged with the run's `run_id`), then re-raised for the
    caller, which doesn't log it again. The account is expected to have passed
    `config.validate_accounts` (the CLI checks the whole file before any run); an
    `act_categories` key that isn't a category would only match nothing, so that category is
    labeled and never acted on (fail closed).
    """
    mode = effective_mode(ctx.settings, account, mode_override)
    with ctx.sessions() as session:
        with _logged_failure(account.email):  # no run_id: the row couldn't be stored
            run = Run(account=account.email, mode=mode)
            session.add(run)
            session.commit()
        # Counted outside the ORM object: a rollback after a failed email would reset it.
        counts = RunCounts()
        # Every log line of the pass carries `run_id`, its failure's traceback included.
        with logs.run_context(run.id), _logged_failure(account.email):
            try:
                _run_pass(ctx, session, account, mode, limit, lookback, counts)
            except Exception as exc:
                session.rollback()
                run.error = describe_run_error(exc, account.email)
                raise
            finally:
                _finish_run(session, run, counts)
    return run


@contextmanager
def _logged_failure(account: str) -> Iterator[None]:
    """Log an exception leaving the block ("account run failed", with its traceback), then
    re-raise it: inside `logs.run_context`, the line carries the run's `run_id`."""
    try:
        yield
    except Exception:
        log.exception("account run failed", extra={"account": account})
        raise


def _finish_run(session: Session, run: Run, counts: RunCounts) -> None:
    """Store the pass's counts and end time on its row (committed) and log "run finished"."""
    run.processed, run.errors = counts.processed, counts.errors
    run.finished_at = now()
    session.add(run)
    session.commit()
    log.info(
        "run finished",
        extra={
            "account": run.account,
            "run_id": run.id,
            "mode": run.mode,
            "processed": run.processed,
            "errors": run.errors,
            "jev_skipped": counts.jev_skipped,
            "failed": run.error is not None,
        },
    )


def _run_pass(
    ctx: Context,
    session: Session,
    account: Account,
    mode: Mode,
    limit: int | None,
    lookback: str | None,
    counts: RunCounts,
) -> None:
    jev = JevGuard(ctx.classifier, account.email)  # no API key: fails before touching Gmail
    # A database without the upsert: likewise.
    scans = DomainScanRows(session, account.email, dialects=ctx.scan_dialects)
    with closing(ctx.gmail(account.email)) as gmail:
        label_ids = gmail.ensure_labels(ctx.cfg.all_labels()) if mode != Mode.DRY_RUN else {}
        if mode != Mode.DRY_RUN:
            corrections.check(session, gmail, account, ctx.cfg)

        ids = gmail.list_inbox_ids(
            lookback or ctx.settings.lookback, limit or ctx.settings.max_messages_per_run
        )
        skip = _not_due(session, account.email, mode, ids)
        budget = UnsubscribeBudget(ctx.settings.max_unsubscribes_per_run)
        account_pass = AccountPass(
            ctx, session, gmail, account, mode, label_ids, budget, jev, scans
        )
        try:
            _process_all(account_pass, [i for i in ids if i not in skip], counts)
        finally:
            counts.jev_skipped = jev.skipped
        if jev.stopped is not None:
            raise JevUnavailableError(jev.stopped)


def _process_all(p: AccountPass, ids: list[str], counts: RunCounts) -> None:
    """Process each email in turn (`_attempt`). A failure is stored as `JevGuard.blame` and
    `JevGuard.settle_held` say (`_fail`) and the pass goes on, except a Gmail token that can't
    refresh, which fails the run without counting against the email."""
    jev = p.jev
    for message_id in ids:
        failure = _attempt(p, message_id, counts)
        if (held := jev.settle_held()) is not None:  # a later Jev call says whose fault it was
            _fail(p, held, counts)
        if failure is None:
            continue
        if is_token_refresh_error(failure):
            raise failure
        if (failed := jev.blame(message_id, failure, p.fetched)) is not None:
            _fail(p, failed, counts)
    if (held := jev.finish()) is not None:
        _fail(p, held, counts)


def _attempt(p: AccountPass, message_id: str, counts: RunCounts) -> Exception | None:
    """Process one email (`process`): None when it succeeded, else what it raised, its changes
    rolled back.

    The failure is returned, not handled in the `except`: an error raised while handling it (the
    rollback, `_record_error`'s commit) would otherwise chain a Jev error, whose text can echo
    the email, into the traceback the caller logs.
    """
    try:
        process(p, message_id)
    except Exception as exc:
        failure = exc
    else:
        counts.processed += 1
        return None
    p.session.rollback()
    return failure


def _fail(p: AccountPass, failed: FailedEmail, counts: RunCounts) -> None:
    """Store a failed email (`_record_error`, committed) and log it."""
    counts.errors += 1
    message_id, failure = failed.message_id, failed.failure
    _record_error(p, message_id, failure, failed.email, alone=failed.alone)
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
    backoff: timedelta = ERROR_RETRY_BACKOFF * 2 ** max(attempts - 1, 0)
    return at >= last_failure + backoff


def describe_run_error(exc: BaseException, account: str) -> str:
    """The `runs.error` text: the exception, or what to do when the Gmail token can't refresh.

    Never includes a token refresh failure's own text unless it is the `invalid_grant` summary:
    a transient one can carry the token endpoint's raw response body. Nor a Jev error's text,
    which can echo the email (`describe_jev_error`).
    """
    if isinstance(exc, JevUnavailableError):
        return str(exc)
    if isinstance(exc, JevNotConfiguredError):
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
    *,
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

    @cached_property
    def sources(self) -> InputSources:
        """Where `gather_inputs` reads this pass's emails' inputs: Jev through the pass's guard."""
        return InputSources(
            self.ctx.cfg,
            self.session,
            self.gmail,
            self.jev,
            self.account,
            self.mode,
            self.ctx.settings.body_chars,
            scans=self.scans,
            guard=self.jev,
        )


# The stages of `process` timed on the "message processed" log line, in milliseconds: reading
# the email from Gmail; the sender rule and the sent-mail search; Jev (state included); applying
# the decision (labels, DB row, auto rule, unsubscribe, commit).
Stage = Literal["fetch", "lookup", "jev", "apply"]


class StageTimer:
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
    (`_learn_correction`) and hand off an email the user edited (`_user_edited`); read the
    sender's rules once (`rules.load`: the rule that decides, `rule_for`, and the row an auto
    rule is learned on; for a sender in decide_each_email, the email's own correction decides
    first, `_own_correction`); gather Jev's answer and the sent-mail search (`gather_inputs`);
    decide and plan (`policy`, pure); apply the labels in Gmail and store the outcome; learn an
    auto rule; unsubscribe within the run cap. Raises `JevStoppedError`, before changing anything,
    when the email needs Jev's decision and `p.jev` has stopped.
    """
    ctx, session, account, mode = p.ctx, p.session, p.account, p.mode
    timer = StageTimer()
    p.fetched = None
    with timer.stage("fetch"):
        email = p.gmail.get(message_id)
    p.fetched = email
    sig = signals_for(email, account)
    prev = find_message(session, account.email, message_id)
    counted_before = _counted_toward_auto_rule(prev)
    if prev is not None:
        prev.sender = sig.sender  # rows stored before group authors were recovered
        # In dry_run, or before any label of ours is in Gmail (labels_added), nothing to compare.
        if mode != Mode.DRY_RUN and prev.labels_added:
            current = current_label_names(email, p.label_ids)
            _learn_correction(ctx.cfg, session, account, prev, current)
            if _user_edited(ctx.cfg, account, prev, current):
                return _leave_alone(session, prev, mode)

    with timer.stage("lookup"):
        known = rules.load(session, account.email, sig.sender)
        rule = rule_for(ctx, account, known)
    own_correction = _own_correction(ctx.cfg, account, sig, prev)
    rule_category = own_correction or (rule.category if rule else None)
    manual_rule = own_correction is None and rules.is_manual(rule)
    list_row = ListRow(session, account.email, sig.list_key)
    inputs = gather_inputs(
        p.sources,
        email,
        sig,
        rule_category,
        manual_rule=manual_rule,
        list_row=list_row,
        timer=timer,
    )
    decision = decide(
        ctx.cfg,
        account,
        sig,
        inputs.classification,
        inputs.rule_category,
        previous_contact=inputs.contact,
        manual_rule=manual_rule,
    )
    actions = plan(ctx.cfg, decision, mode, account.act_categories)

    with timer.stage("apply"):
        msg = _upsert_message(session, account.email, email, sig, prev)
        _apply_labels(p.gmail, message_id, msg, p.label_ids, actions)
        _store_outcome(msg, mode, inputs.classification, decision, actions)
        _learn_auto_rule(p, sig, inputs, decision, known, counted_before=counted_before)
        if (
            actions.unsubscribe
            and not p.unsub_budget.exhausted
            and _unsubscribe(p, email, sig, list_row)
        ):
            p.unsub_budget.spend()
        session.commit()
    _log_processed(account, message_id, sig, inputs, decision, actions, timer)
    return msg


@dataclass(frozen=True)
class InputSources:
    """What `gather_inputs` reads one account's inputs from.

    `run` builds it from its pass (`AccountPass.sources`: Jev through the pass's `JevGuard`, also
    given as `guard`, and the sent-mail scans in the DB); `scripts/dry_classify.py` from its
    read-only clients (the bare `Classifier`, no `guard`, no `scans`: the domain search's
    progress lasts as long as `gmail`).
    """

    cfg: CategoriesConfig
    session: Session
    gmail: GmailApi
    classifier: Classifies
    account: Account
    mode: Mode  # the rule path's sent-mail search depends on it
    body_chars: int
    scans: DomainScans | None = None
    # The pass's guard when `classifier` is one: once it has stopped asking Jev, an email Jev
    # would decide gets no sent-mail search either (`JevStoppedError`).
    guard: JevGuard | None = None


@dataclass(frozen=True)
class Inputs:
    """What `policy.decide` gets for one email besides its signals (`gather_inputs`)."""

    rule_category: str | None  # the category a rule decides, None: no rule
    classification: Classification | None  # None: Jev not asked, or its flags-only call failed
    # previous_contact's answer; None: not checked, or the search failed
    contact: PreviousContact | None


class ListRow:
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


def gather_inputs(
    src: InputSources,
    email: Email,
    sig: Signals,
    rule_category: str | None,
    *,
    manual_rule: bool = False,
    list_row: ListRow | None = None,
    timer: StageTimer | None = None,
) -> Inputs:
    """Jev's answer and the sent-mail search for an email a rule decides as `rule_category`
    (None: no rule; `manual_rule`: it is a manual rule), each only where it can matter. `run`
    and `scripts/dry_classify.py` both call it, so the script makes the same calls as a pass.

    No allowlist entry and no rule: Jev decides, told whether I wrote to the sender (neither is
    asked once a pass's `JevGuard` has stopped: `JevStoppedError`). The allowlist or a rule to a
    keep category (`policy.flags_only`): Jev only for the flags, no search. Any other rule: the
    search only if it could stop an unsubscribe (`_rule_needs_contact`, on `list_row`, which
    `process` reuses to unsubscribe; read here when not given). `timer` times the stages.
    Raises `JevError` when Jev decides the email and the call fails.
    """
    account, gmail = src.account, src.gmail
    timer = timer or StageTimer()
    if not is_allowed(account, sig) and rule_category is None:
        if src.guard is not None:
            src.guard.check()  # no sent-mail search for an email Jev won't decide
        with timer.stage("lookup"):
            contact = previous_contact(gmail, account.email, sig.sender, src.scans)
        with timer.stage("jev"):
            classification = src.classifier.classify(
                build_state(email, account.email, src.body_chars, contact, sig)
            )
        return Inputs(rule_category, classification, contact)
    if flags_only(src.cfg, account, sig, rule_category):
        with timer.stage("jev"):
            flags = flags_classification(src.classifier, email, account, src.body_chars, sig)
        return Inputs(rule_category, flags, None)
    if rule_category is not None:
        list_row = list_row or ListRow(src.session, account.email, sig.list_key)
        with timer.stage("lookup"):
            if _rule_needs_contact(
                src.cfg, account, sig, rule_category, src.mode, list_row, manual_rule=manual_rule
            ):
                contact = previous_contact(gmail, account.email, sig.sender, src.scans)
                return Inputs(rule_category, None, contact)
    return Inputs(rule_category, None, None)


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
    msg.downgraded_to_spam = decision.unsubscribe_skipped_unverified
    msg.mode = mode
    msg.labels_added = actions.add_labels
    msg.decided_labels = decision.add_labels
    msg.archived, msg.spammed = actions.archived, actions.spammed
    msg.processed_at = now()


def _learn_auto_rule(
    p: AccountPass,
    sig: Signals,
    inputs: Inputs,
    decision: Decision,
    known: rules.SenderRules,
    *,
    counted_before: bool,
) -> None:
    """Count Jev's decision toward an auto rule for the sender (`rules.record_auto`).

    Only Jev's own decisions count, not in dry_run and not for a sender the account decides
    email by email (decide_each_email: never learned, any pending rule left as it is). A rule
    learned in dry_run would apply
    without Jev (and act) as soon as the mode is label or act. An email Jev's answer was already
    learned from (`counted_before`: processed again in a stronger mode, or after `reprocess`)
    adds no hit: an auto rule needs that many different emails. Its new answer can still reset a
    pending rule. `known` is the sender's rules `process` read for this email.
    """
    classification = inputs.classification
    if (
        classification
        and decision.source == DecisionSource.JEV
        and p.mode != Mode.DRY_RUN
        and not p.account.decides_each_email(sig.sender)
    ):
        rules.record_auto(
            p.session,
            p.ctx.cfg,
            p.account.email,
            sig.sender,
            classification,
            decision,
            inputs.contact,
            count=not counted_before,
            known=known,
        )


def _own_correction(
    cfg: CategoriesConfig, account: Account, sig: Signals, prev: Message | None
) -> str | None:
    """The category the user corrected this email to, when its sender is in decide_each_email.

    Such a correction is learned as no sender rule (`corrections.record`), so it decides this
    email alone when it is processed again (a stronger mode, or `reprocess`), as a correction
    rule would. None for other senders (their correction rule decides) and for a category since
    removed from categories.yaml.
    """
    if prev is None or not account.decides_each_email(sig.sender):
        return None
    category = prev.corrected_category
    return category if category in cfg.by_key else None


def _counted_toward_auto_rule(row: Message | None) -> bool:
    """Whether an earlier pass already offered Jev's answer on this email to `rules.record_auto`:
    a Jev decision stored in label or act (dry_run learns nothing). Read before this pass
    overwrites the row."""
    return row is not None and row.source == DecisionSource.JEV and row.mode != Mode.DRY_RUN


def _log_processed(
    account: Account,
    message_id: str,
    sig: Signals,
    inputs: Inputs,
    decision: Decision,
    actions: Plan,
    timer: StageTimer,
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
    except JevStoppedError:
        return None  # the run's error and the "stopped asking Jev" log line say why
    except JevError as exc:
        failure = exc
    # Outside the `except`, so nothing raised here chains the Jev error's text (see _attempt).
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


def rule_for(ctx: Context, account: Account, known: rules.SenderRules) -> SenderRule | None:
    """The sender's rule (`rules.SenderRules.match`), from their rules `known` (`rules.load`)
    that may decide in `account` (`rules.SenderRules.deciding`: only a manual one for a sender
    in decide_each_email): None when its first rule points to a removed category.

    Such a rule makes Jev decide (never the @domain rule behind it) and is logged once per
    invocation, so the user can `rules delete` it or restore the category. It stays in the DB:
    the category may come back, and a manual or correction rule can't be relearned.
    """
    rule, stale = known.deciding(account).match(ctx.cfg.by_key)
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

    Use this, not `email.extract_signals()`, wherever the sender drives a decision, a rule or
    the DB.
    """
    return resolve_relay(email.extract_signals(), account.group_domains)


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


def previous_contact(
    gmail: GmailApi, account: str, sender: str, scans: DomainScans | None = None
) -> PreviousContact | None:
    """Whether I wrote to `sender` or to someone at their domain, or None if it can't be checked.

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
            return PreviousContact.WROTE_TO_SENDER
        own = domain_of(account.lower())
        if domain not in FREE_MAIL and domain != own and gmail.has_written_to(domain, scans):
            return PreviousContact.WROTE_TO_THEIR_DOMAIN
    except (SQLAlchemyError, NotImplementedError):
        # Saving the domain scan failed (e.g. the database is locked, or has no upsert): not an
        # unknown contact, which `run` may unsubscribe; the caller fails before any request.
        raise
    except Exception:
        log.warning("previous contact check failed", extra={"account": account}, exc_info=True)
        return None
    return PreviousContact.NEVER


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
        corrections.record(session, account, msg, category)


def _user_edited(cfg: CategoriesConfig, account: Account, msg: Message, current: set[str]) -> bool:
    """Whether the user edited an email labeled by an earlier pass (`user_edit`), logged.

    Read-only: `process` records any correction first (`_learn_correction`), so a corrected
    email is only handed off when starred.
    """
    edit = user_edit(cfg, msg, current)
    if edit is None:
        return False
    reason, labels = edit
    log.info(
        "left alone: user edited the email",
        extra={"account": account.email, "id": msg.gmail_id, "reason": reason, "labels": labels},
    )
    return True


def current_label_names(email: Email, label_ids: dict[str, str]) -> set[str]:
    """The email's current Gmail labels by name, from `label_ids` (name -> id).

    An id missing from `label_ids` is kept as is: system labels' ids are their names.
    """
    names = {v: k for k, v in label_ids.items()}
    return {names.get(i, i) for i in email.label_ids}


def user_edit(
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
    if added := sorted((current & cfg.managed_labels) - set(labels_added)):
        return "added", added
    return None


def _rule_needs_contact(
    cfg: CategoriesConfig,
    account: Account,
    sig: Signals,
    category: str,
    mode: Mode,
    list_row: ListRow,
    *,
    manual_rule: bool,
) -> bool:
    """A rule hit that would unsubscribe unless I wrote to the sender: worth the Gmail searches.

    Only when the category is acted on (`policy.acts_on`), `policy.unsubscribe_gate` lets it
    through with only the sent-mail search left to decide (a category that spares known
    contacts, a verified non-group sender), and the list isn't already settled in the
    Unsubscribe table (`list_row`). Read-only.
    """
    if not acts_on(mode, account.act_categories, category):
        return False
    gate = unsubscribe_gate(
        cfg, account, sig, category, previous_contact=None, manual_rule=manual_rule
    )
    if not (gate.allowed and gate.contact_unknown):
        return False
    return not _unsub_settled(list_row.get())


def _unsub_row(session: Session, account: str, list_key: str) -> Unsubscribe | None:
    return session.scalar(
        select(Unsubscribe).where(Unsubscribe.account == account, Unsubscribe.list_key == list_key)
    )


def _unsub_settled(row: Unsubscribe | None) -> bool:
    """No further attempt will be made: done (ok, or any non-failed status) or out of retries."""
    return row is not None and (
        row.status != UnsubscribeStatus.FAILED or row.attempts >= MAX_UNSUB_ATTEMPTS
    )


def _unsubscribe(p: AccountPass, email: Email, sig: Signals, list_row: ListRow) -> bool:
    """Unsubscribe once per (account, list). Returns True if an attempt was made."""
    row = list_row.get()
    if _unsub_settled(row):
        return False

    # Write the email's row (taking SQLite's write lock) before the irreversible request, so a
    # locked DB fails the email before it unsubscribes rather than rolling back the attempt's row.
    # There is always a write: `_store_outcome` has just set the row's `processed_at`.
    p.session.flush()
    result = p.ctx.unsubscriber(sig, p.gmail.send)
    if row is None:
        row = Unsubscribe(account=p.account.email, list_key=sig.list_key, attempts=0)
        p.session.add(row)
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


def find_message(session: Session, account: str, gmail_id: str) -> Message | None:
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
    msg = find_message(session, account.email, message_id)
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
