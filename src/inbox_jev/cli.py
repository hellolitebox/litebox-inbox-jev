"""Command line entrypoint: `inbox-jev --help`."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated

import typer
from sqlalchemy import func, select

from . import __version__, labels, logs, rules, triage
from . import digest as digest_mod
from . import gmail as gmail_mod
from .config import Account, Mode, Settings, check_lookback, load_accounts, load_categories
from .db import Message, MessageStatus, SenderRule, make_session_factory, now

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

# Set by the image (Dockerfile ARG GIT_SHA): the commit the running code was built from. Read
# here rather than through Settings so `--version` needs no configuration at all.
REVISION_ENV = "INBOX_JEV_REVISION"


def _version(value: bool) -> None:
    if not value:
        return
    revision = os.environ.get(REVISION_ENV)
    typer.echo(f"inbox-jev {__version__}" + (f" ({revision})" if revision else ""))
    raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version,
            is_eager=True,
            help="Print the release and the commit the image was built from, then exit.",
        ),
    ] = False,
) -> None:
    pass  # the app's help text is on `typer.Typer(help=…)` above; nothing runs before a command


def _settings(*, announce: bool = False) -> Settings:
    """Load settings and set up logging; `announce` logs the image's commit first.

    Only the timer-driven commands announce: their JSON logs then say which image produced them.
    Interactive commands print tables, and the line would land before them on stdout.
    """
    settings = Settings()
    logs.setup(settings.log_level)
    if announce:
        log.info("starting", extra={"revision": os.environ.get(REVISION_ENV, "unknown")})
    return settings


def _accounts(settings: Settings, only: str | None) -> list[Account]:
    accounts = load_accounts(settings.accounts_file)
    if only:
        accounts = [a for a in accounts if a.email.lower() == only.lower()]
        if not accounts:
            raise typer.BadParameter(f"{only} is not in {settings.accounts_file}")
    return accounts


def _each_account(accounts: list[Account], what: str, work: Callable[[Account], None]) -> None:
    """Do `work` for each account: one failing (token, Gmail, Slack, Jev, DB) doesn't stop the
    others. Each failure is logged and printed; the command exits 1 if any account failed."""
    failed = False
    for acc in accounts:
        try:
            work(acc)
        except Exception as exc:
            failed = True
            log.exception(f"{what} failed", extra={"account": acc.email})
            reason = triage.describe_run_error(exc, acc.email)
            typer.echo(f"{acc.email}: {what} failed: {reason}", err=True)
    if failed:
        raise typer.Exit(1)


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
    """Classify new inbox emails and apply labels/actions.

    Exits 1 when any account's pass failed (stored in `runs.error`, shown in the digest),
    including a missing TYPESAFE_API_KEY, which fails every pass before Gmail is touched.
    """
    s = _settings(announce=True)
    ctx = triage.Context(
        s, load_categories(s.categories_file), make_session_factory(s.database_url)
    )

    def run_one(acc: Account) -> None:
        triage.run_account(ctx, acc, mode, limit, lookback)

    try:
        _each_account(_accounts(s, account), "account run", run_one)
    finally:
        ctx.close()


@app.command()
def digest(
    account: AccountOpt = None,
    hours: int = 24,
    dry_print: Annotated[bool, typer.Option(help="Print instead of sending to Slack.")] = False,
) -> None:
    """Send the daily digest to each account's Slack user (exits 1 if any account failed)."""
    s = _settings(announce=not dry_print)  # the timer case announces; a preview doesn't
    cfg = load_categories(s.categories_file)
    sessions = make_session_factory(s.database_url)

    def send(acc: Account) -> None:
        with sessions() as session:
            text = digest_mod.build(session, cfg, acc.email, hours, language=acc.digest_language)
        if dry_print or not (s.slack_bot_token and acc.slack_user_id):
            typer.echo(text + "\n")
            return
        digest_mod.send_slack(s.slack_bot_token, acc.slack_user_id, text)
        log.info("digest sent", extra={"account": acc.email})

    _each_account(_accounts(s, account), "digest", send)


@app.command()
def restore(
    account: Annotated[str, typer.Option("--account", "-a")],
    since: Annotated[str, typer.Option(help="e.g. 12h, 3d")] = "1d",
    category: Annotated[str | None, typer.Option(help="Only this category.")] = None,
) -> None:
    """Undo: move processed emails back to the inbox and remove Jev labels."""
    s = _settings()
    window = _parse_since(since)
    gmail = gmail_mod.Gmail(gmail_mod.load_credentials(s.google_tokens_dir, account))
    with make_session_factory(s.database_url)() as session:
        result = triage.restore(session, gmail, account, window, category)
    deleted = f" Skipped {result.deleted} deleted in Gmail." if result.deleted else ""
    typer.echo(f"Restored {result.restored} emails.{deleted} Unsubscribes cannot be undone.")


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


@app.command("retry-errors")
def retry_errors(account: Annotated[str, typer.Option("--account", "-a")]) -> None:
    """Have the next run retry emails that failed, including those that stopped retrying."""
    s = _settings()
    with make_session_factory(s.database_url)() as session:
        count = triage.reset_error_attempts(session, account)
    typer.echo(
        f"Reset {count} failed emails. The next `run` retries those still inside its window: use "
        "`run --lookback` wide enough to reach the oldest one."
    )


@app.command("unsubscribe-retry")
def unsubscribe_retry(account: AccountOpt = None) -> None:
    """Retry failed unsubscribes (up to 3 attempts each) that still pass run's checks.

    One account failing doesn't stop the others; exits 1 if any failed.
    """
    s = _settings()
    cfg = load_categories(s.categories_file)
    ctx = triage.Context(s, cfg, make_session_factory(s.database_url))

    def retry(acc: Account) -> None:
        gmail = gmail_mod.Gmail(gmail_mod.load_credentials(s.google_tokens_dir, acc.email))
        with ctx.sessions() as session:
            for list_key, outcome in triage.retry_unsubscribes(ctx, session, gmail, acc):
                typer.echo(f"{acc.email} {list_key}: {outcome}")

    try:
        _each_account(_accounts(s, account), "unsubscribe retry", retry)
    finally:
        ctx.close()


@senders_app.command("backfill")
def senders_backfill(account: AccountOpt = None) -> None:
    """Store the author, not the group, as sender of group mail saved before that was done.

    Re-reads the headers of stored emails whose From has " via ". Safe to run again. Emails
    deleted in Gmail are skipped; any other Gmail error stops that account (the others go on)
    and the command exits 1.
    """
    s = _settings()
    sessions = make_session_factory(s.database_url)

    def backfill(acc: Account) -> None:
        gmail = gmail_mod.Gmail(gmail_mod.load_credentials(s.google_tokens_dir, acc.email))
        with sessions() as session:
            checked, updated, skipped = triage.backfill_senders(session, gmail, acc)
        typer.echo(
            f"{acc.email}: {checked} group emails checked, {updated} senders updated, "
            f"{skipped} skipped (deleted in Gmail)"
        )

    _each_account(_accounts(s, account), "senders backfill", backfill)


@app.command()
def stats(account: AccountOpt = None, days: int = 7) -> None:
    """Counts per category for the last N days."""
    s = _settings()
    sessions = make_session_factory(s.database_url)
    with sessions() as session:
        query = (
            select(Message.account, Message.category, Message.low_confidence, func.count())
            .where(
                Message.status == MessageStatus.DONE,
                Message.processed_at >= now() - timedelta(days=days),
            )
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
        rules.set_rule(session, account, sender.lower(), category, source=rules.RuleSource.MANUAL)
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
