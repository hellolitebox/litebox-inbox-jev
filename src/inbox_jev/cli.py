"""Command line entrypoint: `inbox-jev --help`."""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Annotated

import typer
from sqlalchemy import func, select

from . import digest as digest_mod
from . import gmail as gmail_mod
from . import labels, logs, rules, triage
from . import unsubscribe as unsub
from .config import Account, Mode, Settings, check_lookback, load_accounts, load_categories
from .db import Message, SenderRule, Unsubscribe, make_session_factory, now

app = typer.Typer(no_args_is_help=True, help="Triage Gmail with TypeSafe Jev.")
auth_app = typer.Typer(no_args_is_help=True, help="Manage Gmail OAuth tokens.")
rules_app = typer.Typer(no_args_is_help=True, help="Per-sender rules (skip Jev).")
labels_app = typer.Typer(no_args_is_help=True, help="Manage Jev labels in Gmail.")
senders_app = typer.Typer(no_args_is_help=True, help="Maintain stored senders.")
app.add_typer(auth_app, name="auth")
app.add_typer(rules_app, name="rules")
app.add_typer(labels_app, name="labels")
app.add_typer(senders_app, name="senders")

log = logging.getLogger("inbox_jev")

AccountOpt = Annotated[str | None, typer.Option("--account", "-a", help="Only this account.")]


def _settings() -> Settings:
    settings = Settings()
    logs.setup(settings.log_level)
    return settings


def _accounts(settings: Settings, only: str | None) -> list[Account]:
    accounts = load_accounts(settings.accounts_file)
    if only:
        accounts = [a for a in accounts if a.email.lower() == only.lower()]
        if not accounts:
            raise typer.BadParameter(f"{only} is not in {settings.accounts_file}")
    return accounts


def _lookback_option(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return check_lookback(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _parse_since(value: str) -> timedelta:
    match = re.fullmatch(r"(\d+)([hd])", value.strip())
    if not match:
        raise typer.BadParameter("use e.g. 12h or 3d")
    n, unit = int(match.group(1)), match.group(2)
    return timedelta(hours=n) if unit == "h" else timedelta(days=n)


@auth_app.command("add")
def auth_add(email: str) -> None:
    """Open the browser to authorize EMAIL and store its refresh token."""
    s = _settings()
    path = gmail_mod.authorize(s.google_client_secrets_file, s.google_tokens_dir, email)
    typer.echo(f"Saved token to {path}")


@app.command()
def run(
    account: AccountOpt = None,
    mode: Annotated[Mode | None, typer.Option(help="Override MODE for this run.")] = None,
    limit: Annotated[int | None, typer.Option(help="Max inbox messages to look at.")] = None,
    lookback: Annotated[
        str | None,
        typer.Option(
            help="Override LOOKBACK, e.g. 30d to clean up the backlog.",
            callback=_lookback_option,
        ),
    ] = None,
) -> None:
    """Classify new inbox emails and apply labels/actions."""
    s = _settings()
    ctx = triage.Context(
        s, load_categories(s.categories_file), make_session_factory(s.database_url)
    )
    failed = False
    try:
        for acc in _accounts(s, account):
            try:
                triage.run_account(ctx, acc, mode, limit, lookback)
            except Exception:
                failed = True
                log.exception("account run failed", extra={"account": acc.email})
    finally:
        ctx.close()
    if failed:
        raise typer.Exit(1)


@app.command()
def digest(
    account: AccountOpt = None,
    hours: int = 24,
    dry_print: Annotated[bool, typer.Option(help="Print instead of sending to Slack.")] = False,
) -> None:
    """Send the daily digest to each account's Slack user."""
    s = _settings()
    cfg = load_categories(s.categories_file)
    sessions = make_session_factory(s.database_url)
    for acc in _accounts(s, account):
        with sessions() as session:
            text = digest_mod.build(session, cfg, acc.email, hours)
        if dry_print or not (s.slack_bot_token and acc.slack_user_id):
            typer.echo(text + "\n")
            continue
        digest_mod.send_slack(s.slack_bot_token, acc.slack_user_id, text)
        log.info("digest sent", extra={"account": acc.email})


@app.command()
def restore(
    account: Annotated[str, typer.Option("--account", "-a")],
    since: Annotated[str, typer.Option(help="e.g. 12h, 3d")] = "1d",
    category: Annotated[str | None, typer.Option(help="Only this category.")] = None,
) -> None:
    """Undo: move processed emails back to the inbox and remove Jev labels."""
    s = _settings()
    sessions = make_session_factory(s.database_url)
    gmail = gmail_mod.Gmail(gmail_mod.load_credentials(s.google_tokens_dir, account))
    ids = gmail.labels()
    query = select(Message).where(
        Message.account == account,
        Message.status == "done",
        Message.mode != Mode.DRY_RUN.value,
        Message.restored.is_(False),
        Message.processed_at >= now() - _parse_since(since),
    )
    if category:
        query = query.where(Message.category == category)
    count = 0
    with sessions() as session:
        for msg in session.scalars(query):
            remove = [ids[n] for n in (msg.labels_added or []) if n in ids and n != "SPAM"]
            add = []
            if msg.archived or msg.spammed:
                add.append("INBOX")
            if msg.spammed:
                remove.append("SPAM")
            gmail.modify(msg.gmail_id, add=add, remove=remove)
            msg.restored = True
            session.commit()
            count += 1
    typer.echo(f"Restored {count} emails. Unsubscribes cannot be undone.")


@app.command()
def reprocess(
    account: Annotated[str, typer.Option("--account", "-a")],
    category: Annotated[str, typer.Option(help="Category key from categories.yaml.")],
    low_confidence_only: Annotated[
        bool, typer.Option("--low-confidence-only", help="Only low-confidence rows.")
    ] = False,
) -> None:
    """Have the next act run redo CATEGORY emails Jev left in the inbox."""
    s = _settings()
    cfg = load_categories(s.categories_file)
    with make_session_factory(s.database_url)() as session:
        try:
            count = triage.mark_for_reprocess(session, cfg, account, category, low_confidence_only)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        f"Marked {count} emails for reprocessing. The next `run` in act mode redoes them, but "
        "only those inside its window: use `run --lookback` wide enough to reach the oldest one."
    )


@app.command("unsubscribe-retry")
def unsubscribe_retry(account: AccountOpt = None) -> None:
    """Retry failed unsubscribes (up to 3 attempts each).

    Doesn't re-check previous_contact: each row passed it when created; `run` re-checks.
    """
    s = _settings()
    sessions = make_session_factory(s.database_url)
    for acc in _accounts(s, account):
        gmail = gmail_mod.Gmail(gmail_mod.load_credentials(s.google_tokens_dir, acc.email))
        with sessions() as session:
            rows = session.scalars(
                select(Unsubscribe).where(
                    Unsubscribe.account == acc.email,
                    Unsubscribe.status == "failed",
                    Unsubscribe.attempts < triage.MAX_UNSUB_ATTEMPTS,
                )
            ).all()
            for row in rows:
                sig = triage.signals_for(gmail.get(row.gmail_id), acc)
                result = unsub.unsubscribe(sig, send_mail=gmail.send)
                row.method, row.status, row.target = result.method, result.status, result.target
                row.http_status, row.error = result.http_status, result.error
                row.attempts += 1
                row.attempted_at = now()
                session.commit()
                typer.echo(f"{row.list_key}: {result.status}")


@senders_app.command("backfill")
def senders_backfill(account: AccountOpt = None) -> None:
    """Store the author, not the group, as sender of group mail saved before that was done.

    Re-reads the headers of stored emails whose From has " via ". Safe to run again. Emails
    deleted in Gmail are skipped; any other Gmail error stops the command.
    """
    s = _settings()
    sessions = make_session_factory(s.database_url)
    for acc in _accounts(s, account):
        gmail = gmail_mod.Gmail(gmail_mod.load_credentials(s.google_tokens_dir, acc.email))
        with sessions() as session:
            checked, updated, skipped = triage.backfill_senders(session, gmail, acc)
        typer.echo(
            f"{acc.email}: {checked} group emails checked, {updated} senders updated, "
            f"{skipped} skipped (deleted in Gmail)"
        )


@app.command()
def stats(account: AccountOpt = None, days: int = 7) -> None:
    """Counts per category for the last N days."""
    s = _settings()
    sessions = make_session_factory(s.database_url)
    with sessions() as session:
        query = (
            select(Message.account, Message.category, Message.low_confidence, func.count())
            .where(Message.status == "done", Message.processed_at >= now() - timedelta(days=days))
            .group_by(Message.account, Message.category, Message.low_confidence)
            .order_by(Message.account, func.count().desc())
        )
        if account:
            query = query.where(Message.account == account)
        for acc, cat, low, n in session.execute(query):
            typer.echo(f"{acc:40} {cat or '-':16} {'(low conf)' if low else '':11} {n}")


@rules_app.command("list")
def rules_list(account: AccountOpt = None, all_rules: bool = typer.Option(False, "--all")) -> None:
    """Show active rules (--all includes auto rules still learning)."""
    s = _settings()
    with make_session_factory(s.database_url)() as session:
        query = select(SenderRule).order_by(SenderRule.account, SenderRule.sender_key)
        if account:
            query = query.where(SenderRule.account == account)
        if not all_rules:
            query = query.where(SenderRule.active.is_(True))
        for r in session.scalars(query):
            state = "active" if r.active else f"learning {r.hits}"
            typer.echo(f"{r.account:32} {r.sender_key:40} {r.category:16} {r.source:10} {state}")


@rules_app.command("set")
def rules_set(
    account: Annotated[str, typer.Option("--account", "-a")],
    sender: Annotated[str, typer.Argument(help="Address or @domain.")],
    category: str,
) -> None:
    """Always classify SENDER as CATEGORY."""
    s = _settings()
    cfg = load_categories(s.categories_file)
    if category not in cfg.by_key:
        raise typer.BadParameter(f"unknown category; one of {', '.join(cfg.by_key)}")
    with make_session_factory(s.database_url)() as session:
        rules.set_rule(session, account, sender.lower(), category, source="manual")
        session.commit()
    typer.echo(f"{sender} → {category}")


@rules_app.command("delete")
def rules_delete(
    account: Annotated[str, typer.Option("--account", "-a")],
    sender: Annotated[str, typer.Argument(help="Address or @domain.")],
) -> None:
    """Remove the rule for SENDER (Jev decides again)."""
    s = _settings()
    with make_session_factory(s.database_url)() as session:
        rule = session.scalar(
            select(SenderRule).where(
                SenderRule.account == account, SenderRule.sender_key == sender.lower()
            )
        )
        if rule is None:
            raise typer.BadParameter("no such rule")
        session.delete(rule)
        session.commit()
    typer.echo(f"Deleted rule for {sender}")


@labels_app.command("rename")
def labels_rename(old: str, new: str, account: AccountOpt = None) -> None:
    """Rename label OLD to NEW in Gmail (emails keep it) and in the DB. Update
    categories.yaml to NEW as well, and run this before the next triage pass, or that pass
    creates NEW as a separate, empty label."""
    s = _settings()
    if new not in load_categories(s.categories_file).all_labels():
        typer.echo(f"warning: {new!r} is not a label in {s.categories_file}", err=True)
    sessions = make_session_factory(s.database_url)
    failed = False
    for acc in _accounts(s, account):
        # One account failing (refusal, missing token, Gmail error) doesn't stop the others.
        try:
            gmail = gmail_mod.Gmail(gmail_mod.load_credentials(s.google_tokens_dir, acc.email))
            with sessions() as session:
                outcome = labels.rename(gmail, session, acc.email, old, new)
        except ValueError as exc:
            failed = True
            typer.echo(f"{acc.email}: refused: {exc}", err=True)
            continue
        except Exception as exc:
            failed = True
            log.exception("label rename failed", extra={"account": acc.email})
            typer.echo(f"{acc.email}: failed: {exc}", err=True)
            continue
        typer.echo(f"{acc.email}: {old} -> {new}: {outcome}")
    if failed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
