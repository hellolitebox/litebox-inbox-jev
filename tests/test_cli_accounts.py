"""CLI commands across accounts: one failing account doesn't stop the others, and the command
exits 1; `run` needs TYPESAFE_API_KEY, the commands that never ask Jev don't."""

from pathlib import Path

import pytest
from fakes import FakeGmail
from typer.testing import CliRunner

from inbox_jev import cli, labels, triage
from inbox_jev.db import Run, open_sessions

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Two accounts (a fails where a test says so, b works), a file DB and no Jev key."""
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text(
        "accounts:\n"
        "  - email: a@acme.com\n    slack_user_id: UA\n"
        "  - email: b@acme.com\n    slack_user_id: UB\n"
    )
    db = f"sqlite:///{tmp_path / 'db.sqlite'}"
    monkeypatch.setenv("ACCOUNTS_FILE", str(accounts))
    monkeypatch.setenv("CATEGORIES_FILE", str(ROOT / "categories.yaml"))
    monkeypatch.setenv("DATABASE_URL", db)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    monkeypatch.setattr(cli.logs, "setup", lambda level: None)  # keep the test's logging as is
    return db


@pytest.fixture
def opened(monkeypatch):
    """`gmail.connect` failing for a@acme.com; the clients it opened for the others."""
    clients: list[FakeGmail] = []

    def connect(tokens_dir, email):
        if email == "a@acme.com":
            raise OSError("token file unreadable")
        clients.append(FakeGmail([]))
        return clients[-1]

    monkeypatch.setattr(cli.gmail_mod, "connect", connect)
    return clients


def test_digest_goes_on_after_a_failed_account(env, monkeypatch):
    sent = []

    def send_slack(token, user_id, text):
        if user_id == "UA":
            raise RuntimeError("Slack error: channel_not_found")
        sent.append(user_id)

    monkeypatch.setattr(cli.digest_mod, "send_slack", send_slack)
    result = CliRunner().invoke(cli.app, ["digest"])
    assert sent == ["UB"]
    assert result.exit_code == 1
    assert "a@acme.com: digest failed: RuntimeError: Slack error: channel_not_found" in (
        result.output
    )


def test_digest_needs_no_jev_key(env, monkeypatch):
    sent = []
    monkeypatch.setattr(cli.digest_mod, "send_slack", lambda t, u, text: sent.append(u))
    result = CliRunner().invoke(cli.app, ["digest"])
    assert (result.exit_code, sent) == (0, ["UA", "UB"])


def test_unsubscribe_retry_goes_on_after_a_failed_account_and_closes_jev(env, monkeypatch, opened):
    monkeypatch.setattr(triage, "retry_unsubscribes", lambda ctx, s, g, acc: [("news.x", "ok")])
    closed = []
    monkeypatch.setattr(triage.Context, "close", lambda self: closed.append(True))
    result = CliRunner().invoke(cli.app, ["unsubscribe-retry"])
    assert "b@acme.com news.x: ok" in result.output
    assert "a@acme.com: unsubscribe retry failed: OSError: token file unreadable" in result.output
    assert (result.exit_code, closed) == (1, [True])
    assert [g.closes for g in opened] == [1]  # b's Gmail client is closed once its work ends


def test_senders_backfill_goes_on_after_a_failed_account(env, monkeypatch, opened):
    monkeypatch.setattr(triage, "backfill_senders", lambda s, g, acc: (2, 1, 0))
    result = CliRunner().invoke(cli.app, ["senders", "backfill"])
    assert "b@acme.com: 2 group emails checked, 1 senders updated" in result.output
    assert "a@acme.com: senders backfill failed: OSError" in result.output
    assert result.exit_code == 1
    assert [g.closes for g in opened] == [1]


def test_run_without_jev_key_fails_every_account_before_gmail(env, monkeypatch):
    def no_gmail(tokens_dir, email):
        raise AssertionError("Gmail must not be touched")

    monkeypatch.setattr(cli.gmail_mod, "connect", no_gmail)
    result = CliRunner().invoke(cli.app, ["run"])
    assert result.exit_code == 1
    assert result.output.count("TYPESAFE_API_KEY is not set") == 2
    with open_sessions(env) as sessions, sessions() as s:
        runs = s.query(Run).order_by(Run.account).all()
        assert [(r.account, r.error) for r in runs] == [
            (acc, "TYPESAFE_API_KEY is not set: add it to .env; nothing was processed")
            for acc in ("a@acme.com", "b@acme.com")
        ]


def test_labels_rename_tells_refusals_from_failures(env, monkeypatch, caplog):
    """Only `LabelRenameRefused` is reported as refused; any other error (even a ValueError from
    a library) is a failure, logged with its traceback."""
    monkeypatch.setattr(cli.gmail_mod, "connect", lambda tokens_dir, email: FakeGmail([]))

    def rename(gmail, session, account, old, new):
        if account == "a@acme.com":
            raise labels.LabelRenameRefused("'INBOX' is a system label and can't be renamed")
        raise ValueError("unparseable Gmail answer")

    monkeypatch.setattr(labels, "rename", rename)
    result = CliRunner().invoke(cli.app, ["labels", "rename", "INBOX", "Jev/Inbox"])
    assert "a@acme.com: refused: 'INBOX' is a system label" in result.output
    assert "b@acme.com: label rename failed: ValueError: unparseable Gmail answer" in result.output
    assert result.exit_code == 1
    # A refusal is expected: printed, never logged as a failure with a traceback.
    assert [r.account for r in caplog.records if r.exc_info] == ["b@acme.com"]


@pytest.mark.parametrize(
    "command",
    [
        ["run"],
        ["run", "-a", "b@acme.com"],  # the whole file is checked, not only the named account
        ["digest", "--dry-print"],
        ["unsubscribe-retry"],
        ["senders", "backfill"],
        ["labels", "rename", "Jev/Old", "Jev/New"],
    ],
)
def test_accounts_not_matching_categories_fail_every_command_before_it_starts(
    env, monkeypatch, tmp_path, command
):
    (tmp_path / "accounts.yaml").write_text(
        "accounts:\n"
        "  - email: a@acme.com\n    act_categories: [notifications]\n"
        "  - email: b@acme.com\n    min_confidence: {markting: 0.9}\n"
    )
    connected = []
    monkeypatch.setattr(cli.gmail_mod, "connect", lambda d, email: connected.append(email))
    result = CliRunner().invoke(cli.app, command)
    assert result.exit_code == 2, result.output
    assert "a@acme.com: unknown act_categories ['notifications']" in result.output
    assert "b@acme.com: unknown min_confidence ['markting']" in result.output
    assert connected == []
    assert not (tmp_path / "db.sqlite").exists()  # the database isn't even opened
