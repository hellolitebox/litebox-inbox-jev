"""One triage pass over an account's inbox."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from . import corrections, rules
from . import unsubscribe as unsub
from .classifier import Classifier, build_state
from .config import Account, CategoriesConfig, Mode, Settings
from .db import Message, Run, Unsubscribe, now
from .gmail import Email, Gmail, load_credentials
from .policy import decide, plan

log = logging.getLogger(__name__)

MAX_UNSUB_ATTEMPTS = 3


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
    ctx: Context, account: Account, mode_override: Mode | None = None, limit: int | None = None
) -> Run:
    mode = effective_mode(ctx.settings, account, mode_override)
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
                ctx.settings.lookback, limit or ctx.settings.max_messages_per_run
            )
            # Emails seen only in dry_run get processed again once labels/actions are enabled.
            done_query = select(Message.gmail_id).where(
                Message.account == account.email,
                Message.status == "done",
                Message.gmail_id.in_(ids),
            )
            if mode != Mode.DRY_RUN:
                done_query = done_query.where(Message.mode != Mode.DRY_RUN.value)
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
    sig = email.signals
    rule = rules.lookup(session, account.email, sig.sender)

    classification = None
    if not account.is_allowed(sig.sender) and rule is None:
        classification = ctx.classifier.classify(
            build_state(email, account.email, ctx.settings.body_chars)
        )
    decision = decide(ctx.cfg, account, sig, classification, rule.category if rule else None)
    actions = plan(ctx.cfg, decision, mode)

    gmail.modify(
        message_id,
        add=[label_ids.get(name, name) for name in actions.add_labels],
        remove=actions.remove_labels,
    )

    msg = _upsert_message(session, account.email, email)
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
    if actions.unsubscribe and unsub_budget[0] > 0 and _unsubscribe(session, gmail, account, email):
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
            "labels": actions.add_labels,
            "archived": actions.archived,
            "spammed": actions.spammed,
            "request_id": classification.request_id if classification else None,
        },
    )
    return msg


def _unsubscribe(session: Session, gmail: Gmail, account: Account, email: Email) -> bool:
    """Unsubscribe once per (account, list). Returns True if an attempt was made."""
    sig = email.signals
    row = session.scalar(
        select(Unsubscribe).where(
            Unsubscribe.account == account.email, Unsubscribe.list_key == sig.list_key
        )
    )
    if row and (row.status != "failed" or row.attempts >= MAX_UNSUB_ATTEMPTS):
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


def _upsert_message(session: Session, account: str, email: Email) -> Message:
    msg = session.scalar(
        select(Message).where(Message.account == account, Message.gmail_id == email.id)
    )
    if msg is None:
        msg = Message(account=account, gmail_id=email.id)
        session.add(msg)
    msg.thread_id = email.thread_id
    msg.sender = email.signals.sender
    msg.from_header = email.header("From")
    msg.subject = email.header("Subject")
    msg.received_at = datetime.fromtimestamp(email.internal_date_ms / 1000, UTC)
    return msg


def _record_error(
    session: Session, account: str, mode: Mode, message_id: str, exc: Exception
) -> None:
    msg = session.scalar(
        select(Message).where(Message.account == account, Message.gmail_id == message_id)
    )
    if msg is None:
        msg = Message(account=account, gmail_id=message_id, thread_id="", sender="")
        session.add(msg)
    msg.status, msg.mode = "error", mode.value
    msg.error = f"{type(exc).__name__}: {exc}"[:2000]
    msg.processed_at = now()
    session.commit()
