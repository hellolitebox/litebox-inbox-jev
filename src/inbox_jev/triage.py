"""One triage pass over an account's inbox."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from . import corrections, rules
from . import unsubscribe as unsub
from .classifier import Classifier, build_state
from .config import FREE_MAIL, Account, CategoriesConfig, Mode, Settings
from .db import Message, Run, Unsubscribe, now
from .gmail import Email, Gmail, load_credentials
from .policy import can_unsubscribe, decide, is_allowed, plan, spares_known_contacts
from .signals import Signals, resolve_relay

log = logging.getLogger(__name__)

MAX_UNSUB_ATTEMPTS = 3
SAFE_ADDRESS = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")


@dataclass
class Context:
    settings: Settings
    cfg: CategoriesConfig
    sessions: sessionmaker
    _classifier: Classifier | None = None

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
    mode = effective_mode(ctx.settings, account, mode_override)
    unknown = set(account.act_categories or []) - set(ctx.cfg.by_key)
    if unknown:
        raise ValueError(f"{account.email}: unknown act_categories {sorted(unknown)}")
    gmail = Gmail(load_credentials(ctx.settings.google_tokens_dir, account.email))
    label_ids = gmail.ensure_labels(ctx.cfg.all_labels()) if mode != Mode.DRY_RUN else {}

    with ctx.sessions() as session:
        run = Run(account=account.email, mode=mode.value)
        session.add(run)
        session.commit()
        try:
            if mode != Mode.DRY_RUN:
                corrections.check(session, gmail, account.email, ctx.cfg)

            ids = gmail.list_inbox_ids(
                lookback or ctx.settings.lookback, limit or ctx.settings.max_messages_per_run
            )
            # Emails still in the inbox that were seen in a weaker mode (dry_run, or label when
            # running act) get processed once more, so switching modes also covers them.
            # Restored rows are never touched again: the user explicitly undid them.
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
            budget = [ctx.settings.max_unsubscribes_per_run]
            for message_id in ids:
                if message_id in done:
                    continue
                try:
                    process(ctx, session, gmail, account, mode, label_ids, message_id, budget)
                    run.processed += 1
                except Exception as exc:
                    session.rollback()
                    run.errors += 1
                    _record_error(session, account.email, mode, message_id, exc)
                    log.exception(
                        "message failed", extra={"account": account.email, "id": message_id}
                    )
        except Exception as exc:
            run.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
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
                },
            )
    return run


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
    count = session.execute(query.values(mode=Mode.LABEL.value)).rowcount
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
    if mode != Mode.DRY_RUN and _user_touched(ctx.cfg, session, account, prev, email, label_ids):
        # Leave Gmail and labels_added as they are; just don't revisit it in this mode.
        prev.status, prev.error, prev.mode = "done", None, mode.value
        session.commit()
        return prev
    rule = rules.lookup(session, account.email, sig.sender)

    classification, contact = None, None
    if is_allowed(account, sig):
        pass
    elif rule is None:
        contact = previous_contact(gmail, account.email, sig.sender)
        classification = ctx.classifier.classify(
            build_state(email, account.email, ctx.settings.body_chars, contact, sig)
        )
    elif _rule_needs_contact(ctx.cfg, session, account, sig, rule.category, mode):
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

    msg.status, msg.error = "done", None
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

    if classification:
        rules.record_auto(session, ctx.cfg, account.email, sig.sender, classification, decision)
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
            if getattr(getattr(exc, "resp", None), "status", None) != 404:
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
    msg: Message | None,
    email: Email,
    label_ids: dict[str, str],
) -> bool:
    """Check an email labeled by an earlier pass against its current Gmail labels.

    A changed category label is recorded as a correction (the sender rule then decides the
    category). Otherwise, hands off (returns True) if the user starred the email, removed a label
    of ours, or added one of our labels that the last pass didn't set.
    """
    if msg is None or not msg.labels_added:
        return False  # new, or nothing of ours in Gmail yet (dry_run)
    names = {v: k for k, v in label_ids.items()}
    current = {names.get(i, i) for i in email.label_ids}
    if msg.corrected_category is None:
        category = corrections.detect(msg, current, cfg)
        if category:
            corrections.record(session, account.email, msg, category)
    reason, labels = None, []
    if "STARRED" in current:
        reason = "starred"  # a strong "keep" signal, even after a correction
    elif msg.corrected_category is not None:
        return False
    elif removed := [n for n in msg.labels_added if n != cfg.processed_label and n not in current]:
        reason, labels = "removed", removed
    else:
        managed = set(cfg.all_labels()) - {cfg.processed_label}
        if added := sorted((current & managed) - set(msg.labels_added)):
            reason, labels = "added", added
    if reason is None:
        return False
    log.info(
        "left alone: user edited the email",
        extra={"account": account.email, "id": msg.gmail_id, "reason": reason, "labels": labels},
    )
    return True


def _rule_needs_contact(
    cfg: CategoriesConfig,
    session: Session,
    account: Account,
    sig: Signals,
    category: str,
    mode: Mode,
) -> bool:
    """A rule hit that would unsubscribe unless I wrote to the sender: worth the Gmail searches.

    Only in act mode for categories in act_categories (as `plan` decides), for categories that
    spare known contacts, senders that pass the other unsubscribe checks, and lists not already
    settled in the Unsubscribe table.
    """
    acts = mode == Mode.ACT and (
        account.act_categories is None or category in account.act_categories
    )
    if not (acts and spares_known_contacts(cfg, category) and can_unsubscribe(sig, account)):
        return False
    return not _unsub_settled(_unsub_row(session, account.email, sig.list_key))


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
    row.method, row.status, row.target = result.method, result.status, result.target
    row.http_status, row.error = result.http_status, result.error
    row.attempts += 1
    row.attempted_at = now()
    log.info(
        "unsubscribe",
        extra={
            "account": account.email,
            "list": sig.list_key,
            "method": result.method,
            "status": result.status,
        },
    )
    return True


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
    msg.processed_at = now()
    session.commit()
