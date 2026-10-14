"""CLI commands that only read or reset stored state: `stats`, `rules list`, `reprocess`,
`retry-errors`, plus `auth add` (its browser OAuth flow replaced at the `gmail` boundary)."""

from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from inbox_jev import cli
from inbox_jev.db import Message, MessageStatus, SenderRule, now

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(monkeypatch, tmp_path, make_sessions):
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("CATEGORIES_FILE", str(ROOT / "categories.yaml"))
    monkeypatch.setattr(cli.logs, "setup", lambda level: None)  # keep the test's logging as is
    return make_sessions(url)


def _invoke(*args: str):
    return CliRunner().invoke(cli.app, list(args))


def _message(gmail_id: str, account: str = "me@acme.com", **fields) -> Message:
    defaults = {"thread_id": f"t{gmail_id}", "sender": "a@b.com", "mode": "act"}
    return Message(account=account, gmail_id=gmail_id, **(defaults | fields))


def _rule(sender_key: str, account: str = "me@acme.com", **fields) -> SenderRule:
    defaults = {"category": "marketing", "source": "manual", "active": True}
    return SenderRule(account=account, sender_key=sender_key, **(defaults | fields))


def _lines(output: str) -> list[list[str]]:
    return [line.split() for line in output.splitlines()]


def test_stats_counts_done_emails_per_category_in_the_window(db):
    old = now() - timedelta(days=10)
    with db() as s:
        s.add_all(
            [
                _message("1", category="marketing"),
                _message("2", category="marketing"),
                _message("3", category="marketing", low_confidence=True),
                _message("4", category=None),
                _message("5", category="marketing", processed_at=old),  # outside --days 7
                _message("6", category="marketing", status=MessageStatus.ERROR),  # not done
                _message("7", account="you@acme.com", category="important"),
            ]
        )
        s.commit()
    result = _invoke("stats")
    assert result.exit_code == 0, result.output
    lines = _lines(result.output)
    assert lines[0] == ["me@acme.com", "marketing", "2"]  # per account, the biggest count first
    assert sorted(lines[1:3]) == [  # tied counts: any order
        ["me@acme.com", "-", "1"],
        ["me@acme.com", "marketing", "(low", "conf)", "1"],
    ]
    assert lines[3:] == [["you@acme.com", "important", "1"]]
    wider = _invoke("stats", "--account", "me@acme.com", "--days", "30")
    assert wider.exit_code == 0, wider.output
    assert ["me@acme.com", "marketing", "3"] in _lines(wider.output)
    assert "you@acme.com" not in wider.output


def test_rules_list_shows_active_rules_and_with_all_those_still_learning(db):
    with db() as s:
        s.add_all(
            [
                _rule("ana@example.com"),
                _rule("@news.com", source="auto", active=False, hits=2),
                _rule("@other.com", account="you@acme.com", category="important"),
            ]
        )
        s.commit()
    result = _invoke("rules", "list")
    assert result.exit_code == 0, result.output
    assert _lines(result.output) == [
        ["me@acme.com", "ana@example.com", "marketing", "manual", "active"],
        ["you@acme.com", "@other.com", "important", "manual", "active"],
    ]
    learning = _invoke("rules", "list", "--account", "me@acme.com", "--all")
    assert learning.exit_code == 0, learning.output
    assert _lines(learning.output) == [
        ["me@acme.com", "@news.com", "marketing", "auto", "learning", "2"],
        ["me@acme.com", "ana@example.com", "marketing", "manual", "active"],
    ]


def test_reprocess_marks_the_category_and_reports_the_count(db):
    with db() as s:
        s.add_all([_message("1", category="marketing"), _message("2", category="important")])
        s.commit()
    result = _invoke("reprocess", "-a", "me@acme.com", "--category", "marketing")
    assert result.exit_code == 0, result.output
    assert "Marked 1 emails for reprocessing" in result.output
    with db() as s:
        assert {m.gmail_id: m.mode for m in s.query(Message)} == {"1": "label", "2": "act"}


def test_retry_errors_resets_the_accounts_failed_emails(db):
    failed = {"status": MessageStatus.ERROR, "error_attempts": 3}
    with db() as s:
        s.add_all([_message("1", **failed), _message("2", account="you@acme.com", **failed)])
        s.commit()
    result = _invoke("retry-errors", "-a", "me@acme.com")
    assert result.exit_code == 0, result.output
    assert "Reset 1 failed emails" in result.output
    with db() as s:
        attempts = {(m.account, m.gmail_id): m.error_attempts for m in s.query(Message)}
    assert attempts == {("me@acme.com", "1"): 0, ("you@acme.com", "2"): 3}


def test_auth_add_authorizes_the_account_with_the_configured_paths(monkeypatch, tmp_path):
    secrets, tokens = tmp_path / "client_secret.json", tmp_path / "tokens"
    monkeypatch.setenv("GOOGLE_CLIENT_SECRETS_FILE", str(secrets))
    monkeypatch.setenv("GOOGLE_TOKENS_DIR", str(tokens))
    monkeypatch.setattr(cli.logs, "setup", lambda level: None)
    calls = []

    def authorize(client_secrets: Path, tokens_dir: Path, email: str) -> Path:
        calls.append((client_secrets, tokens_dir, email))
        return tokens_dir / f"{email}.json"

    monkeypatch.setattr(cli.gmail_mod, "authorize", authorize)  # the browser OAuth flow
    result = _invoke("auth", "add", "me@acme.com")
    assert result.exit_code == 0, result.output
    assert calls == [(secrets, tokens, "me@acme.com")]
    assert f"Saved token to {tokens / 'me@acme.com.json'}" in result.output
