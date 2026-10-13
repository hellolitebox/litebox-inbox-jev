"""Run-level failures (token, config), the per-email error retry cap and `run` across accounts."""

import json
from unittest import mock

import google.auth._exponential_backoff
import google.auth.transport
import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from test_triage import _age_errors, setup  # noqa: F401 (pytest fixture)
from typer.testing import CliRunner

from inbox_jev import cli, digest, triage
from inbox_jev import gmail as inbox_gmail
from inbox_jev.config import Mode
from inbox_jev.db import MAX_ERROR_ATTEMPTS, Message, Run, Unsubscribe
from inbox_jev.unsubscribe import UnsubscribeMethod, UnsubscribeStatus


@pytest.fixture
def triage_setup(setup):  # noqa: F811
    """tests/test_triage.py's fake Gmail, fake Jev and in-memory DB."""
    return setup


def _runs(ctx):
    with ctx.sessions() as s:
        rows = s.query(Run).order_by(Run.id).all()
        s.expunge_all()
        return rows


def _digest(ctx, account, hours=24):
    with ctx.sessions() as s:
        return digest.build(s, ctx.cfg, account.email, hours)


def test_expired_token_is_recorded_on_the_run_and_raised(triage_setup, monkeypatch):
    ctx, gmail, account = triage_setup

    def expired(tokens_dir, email):
        raise RefreshError(
            "invalid_grant: Token has been expired or revoked.",
            {"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
        )

    monkeypatch.setattr(triage, "load_credentials", expired)
    with pytest.raises(RefreshError):
        triage.run_account(ctx, account, Mode.ACT)
    [run] = _runs(ctx)
    assert run.finished_at is not None and (run.processed, run.errors) == (0, 0)
    assert run.error == (
        "Gmail token expired or revoked (invalid_grant: Token has been expired or revoked.): "
        "run `inbox-jev auth add me@acme.com`"
    )
    text = _digest(ctx, account)
    assert "1 corridas fallidas" in text and "run `inbox-jev auth add me@acme.com`" in text


class _TokenEndpoint(google.auth.transport.Request):
    """A fake transport: the token endpoint answers every request with `status` and `body`."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status, self.body, self.calls = status, body, 0

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        self.calls += 1
        response = mock.Mock(spec=google.auth.transport.Response)
        response.status, response.headers, response.data = self.status, {}, self.body
        return response


_HTML_503 = b"<html><body>503 Service Unavailable</body></html>"
_REFRESH_OUTCOMES = [
    pytest.param(
        503,
        _HTML_503,
        "Gmail token refresh failed (not invalid_grant); will retry next run, "
        "run `inbox-jev auth add me@acme.com` if it persists",
        id="5xx-html-not-retryable",
    ),
    pytest.param(
        503,
        json.dumps({"error": "server_error", "error_description": "<b>busy</b>"}).encode(),
        "Gmail token refresh failed (temporary); will retry next run",
        id="5xx-json-retryable",
    ),
    pytest.param(
        400,
        json.dumps(
            {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
        ).encode(),
        "Gmail token expired or revoked (invalid_grant: Token has been expired or revoked.): "
        "run `inbox-jev auth add me@acme.com`",
        id="400-invalid-grant",
    ),
]


def _refresh_error(status: int, body: bytes, monkeypatch) -> RefreshError:
    """The `RefreshError` google-auth itself raises when the token endpoint answers this way."""
    monkeypatch.setattr(google.auth._exponential_backoff.time, "sleep", lambda s: None)
    creds = Credentials(
        None,
        refresh_token="r",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="i",
        client_secret="s",
    )
    with pytest.raises(RefreshError) as info:
        creds.refresh(_TokenEndpoint(status, body))
    return info.value


@pytest.mark.parametrize(("status", "body", "expected"), _REFRESH_OUTCOMES)
def test_token_refresh_failure_outcomes_from_google_auth(
    triage_setup, monkeypatch, tmp_path, status, body, expected
):
    """The three outcomes of spec §1 / ADR 0017, through the real `Credentials.refresh`."""
    ctx, gmail, account = triage_setup
    token = tmp_path / "me@acme.com.json"
    token.write_text(
        json.dumps(
            {
                "refresh_token": "r",
                "client_id": "i",
                "client_secret": "s",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )
    monkeypatch.setattr(google.auth._exponential_backoff.time, "sleep", lambda s: None)
    monkeypatch.setattr(inbox_gmail, "Request", lambda: _TokenEndpoint(status, body))
    monkeypatch.setattr(
        triage, "load_credentials", lambda d, e: inbox_gmail.load_credentials(tmp_path, e)
    )
    with pytest.raises(RefreshError):
        triage.run_account(ctx, account, Mode.ACT)
    [run] = _runs(ctx)
    assert run.error == expected
    text = _digest(ctx, account)
    assert "<html>" not in run.error and "<html>" not in text
    assert "busy" not in run.error and "busy" not in text


def test_google_auth_marks_html_5xx_not_retryable_and_json_5xx_retryable(monkeypatch):
    """Pins the google-auth shapes the outcomes above rely on (user-credential refresh)."""
    html = _refresh_error(503, _HTML_503, monkeypatch)
    assert (html.retryable, html.args) == (False, (_HTML_503.decode(),))
    server = _refresh_error(503, b'{"error": "server_error"}', monkeypatch)
    assert server.retryable is True
    grant = _refresh_error(400, b'{"error": "invalid_grant"}', monkeypatch)
    assert grant.retryable is False and grant.args[0].startswith("invalid_grant")


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            RefreshError("invalid_grant: Token has been expired or revoked."),
            "Gmail token expired or revoked (invalid_grant: Token has been expired or revoked.): "
            "run `inbox-jev auth add me@acme.com`",
        ),
        (
            RefreshError('{"unexpected": "<body>"}', {"unexpected": "<body>"}),
            "Gmail token refresh failed (not invalid_grant); will retry next run, "
            "run `inbox-jev auth add me@acme.com` if it persists",
        ),
        (
            RefreshError("invalid_grant: x", retryable=True),
            "Gmail token refresh failed (temporary); will retry next run",
        ),
    ],
)
def test_describe_run_error_only_hints_auth_for_invalid_grant(exc, expected):
    assert triage.describe_run_error(exc, "me@acme.com") == expected


def test_transient_token_failure_mid_run_still_fails_the_run(triage_setup, monkeypatch):
    ctx, gmail, account = triage_setup
    real_get = gmail.get
    error = _refresh_error(503, _HTML_503, monkeypatch)

    def get(message_id):
        if message_id == "2":
            raise error
        return real_get(message_id)

    monkeypatch.setattr(gmail, "get", get)
    with pytest.raises(RefreshError):
        triage.run_account(ctx, account, Mode.ACT)
    [run] = _runs(ctx)
    assert (run.processed, run.errors) == (1, 0) and "not invalid_grant" in run.error
    assert "<html>" not in run.error
    with ctx.sessions() as s:
        assert s.query(Message).filter_by(gmail_id="2").count() == 0


def test_unknown_act_category_is_recorded_on_the_run(triage_setup):
    ctx, gmail, account = triage_setup
    account.act_categories = ["notifications"]
    with pytest.raises(ValueError):
        triage.run_account(ctx, account, Mode.ACT)
    [run] = _runs(ctx)
    assert run.finished_at is not None
    assert run.error == "ValueError: me@acme.com: unknown act_categories ['notifications']"
    assert gmail.modified == []


def test_digest_groups_identical_run_failures(triage_setup, monkeypatch):
    ctx, gmail, account = triage_setup

    def broken(tokens_dir, email):
        raise OSError("disk <full> & co")

    monkeypatch.setattr(triage, "load_credentials", broken)
    for _ in range(3):
        with pytest.raises(OSError):
            triage.run_account(ctx, account, Mode.ACT)
    text = _digest(ctx, account)
    assert "3 corridas fallidas" in text and "(3 corridas, última" in text
    assert text.count("OSError: disk &lt;full&gt; &amp; co") == 1


def test_token_dying_mid_run_fails_the_run_without_counting_against_emails(
    triage_setup, monkeypatch
):
    ctx, gmail, account = triage_setup
    real_get = gmail.get

    def get(message_id):
        if message_id == "2":
            raise RefreshError("invalid_grant: Token has been expired or revoked.")
        return real_get(message_id)

    monkeypatch.setattr(gmail, "get", get)
    with pytest.raises(RefreshError):
        triage.run_account(ctx, account, Mode.ACT)
    [run] = _runs(ctx)
    assert (run.processed, run.errors) == (1, 0) and "auth add" in run.error
    with ctx.sessions() as s:
        assert s.query(Message).filter_by(gmail_id="2").count() == 0


def test_run_counts_survive_a_failed_email(triage_setup):
    ctx, gmail, account = triage_setup
    ctx._classifier.answers.pop("50% off")  # email 2 fails between successful ones
    run = triage.run_account(ctx, account, Mode.ACT)
    [stored] = _runs(ctx)
    assert (run.processed, run.errors) == (stored.processed, stored.errors) == (3, 1)


def _attempts(ctx, gmail_id="1"):
    with ctx.sessions() as s:
        return s.query(Message).filter_by(gmail_id=gmail_id).one().error_attempts


def test_failed_email_backs_off_then_gives_up_and_shows_in_digest(triage_setup):
    ctx, gmail, account = triage_setup
    ctx._classifier.answers.pop("Proposal")

    triage.run_account(ctx, account, Mode.ACT)
    calls = ctx._classifier.calls
    triage.run_account(ctx, account, Mode.ACT)  # within the 5-minute backoff: not retried
    assert _attempts(ctx) == 1 and ctx._classifier.calls == calls

    for n in range(2, MAX_ERROR_ATTEMPTS + 1):
        _age_errors(ctx, 5 * 2 ** (n - 2) - 1)  # one minute short of this attempt's backoff
        triage.run_account(ctx, account, Mode.ACT)
        assert _attempts(ctx) == n - 1
        _age_errors(ctx, 1)
        triage.run_account(ctx, account, Mode.ACT)
        assert _attempts(ctx) == n

    _age_errors(ctx, 60 * 24)  # gave up: never retried again, however long it waits
    calls = ctx._classifier.calls
    run = triage.run_account(ctx, account, Mode.ACT)
    assert (run.processed, run.errors) == (0, 0) and ctx._classifier.calls == calls

    text = _digest(ctx, account, hours=48)
    assert "1 mails (1 sin más reintentos)" in text
    assert "Proposal" in text.split("Sin más reintentos")[1]


def test_success_resets_the_error_count(triage_setup):
    ctx, gmail, account = triage_setup
    answers = ctx._classifier.answers
    good = answers.pop("Proposal")
    triage.run_account(ctx, account, Mode.ACT)
    _age_errors(ctx, 5)
    triage.run_account(ctx, account, Mode.ACT)
    assert _attempts(ctx) == 2
    answers["Proposal"] = good
    _age_errors(ctx, 10)
    triage.run_account(ctx, account, Mode.ACT)
    assert _attempts(ctx) == 0


def test_retry_errors_resets_given_up_emails(triage_setup):
    ctx, gmail, account = triage_setup
    answers = ctx._classifier.answers
    good = answers.pop("Proposal")
    triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        s.query(Message).filter_by(gmail_id="1").one().error_attempts = MAX_ERROR_ATTEMPTS
        s.commit()
    _age_errors(ctx, 60 * 24)
    assert triage.run_account(ctx, account, Mode.ACT).processed == 0

    with ctx.sessions() as s:
        assert triage.reset_error_attempts(s, "other@acme.com") == 0
        assert triage.reset_error_attempts(s, account.email) == 1
    answers["Proposal"] = good
    assert triage.run_account(ctx, account, Mode.ACT).processed == 1


def test_cli_run_continues_after_a_failed_account(monkeypatch, tmp_path):
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text("accounts:\n  - email: a@acme.com\n  - email: b@acme.com\n")
    monkeypatch.setenv("ACCOUNTS_FILE", str(accounts))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'db.sqlite'}")
    seen = []

    def run_account(ctx, acc, *args):
        seen.append(acc.email)
        if acc.email == "a@acme.com":
            raise RefreshError("invalid_grant")

    monkeypatch.setattr(triage, "run_account", run_account)
    monkeypatch.setattr(cli.logs, "setup", lambda level: None)  # keep the test's logging as is
    result = CliRunner().invoke(cli.app, ["run"])
    assert seen == ["a@acme.com", "b@acme.com"]
    assert result.exit_code == 1


def test_digest_escapes_email_text_for_slack(triage_setup):
    ctx, gmail, account = triage_setup
    gmail.emails["1"].headers["subject"] = ["Q&A <!channel> <https://evil.example|click>"]
    gmail.emails["1"].headers["from"] = ["Eve <eve@client.com>"]
    ctx._classifier.answers["Q&A <!channel> <https://evil.example|click>"] = (
        ctx._classifier.answers["Proposal"]
    )
    triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        s.add(
            Unsubscribe(
                account=account.email,
                list_key="<!here>&list",
                sender="x@shop.com",
                gmail_id="2",
                method=UnsubscribeMethod.MANUAL,
                status=UnsubscribeStatus.MANUAL_PENDING,
                target="https://shop.com/u?a=1&b=<x>|y",
            )
        )
        s.commit()
    text = _digest(ctx, account)
    assert "Q&amp;A &lt;!channel&gt; &lt;https://evil.example|click&gt;" in text
    assert "<!channel>" not in text and "<!here>" not in text and "<https://evil" not in text
    assert "• &lt;!here&gt;&amp;list — <https://shop.com/u?a=1&amp;b=&lt;x&gt;%7Cy|link>" in text


def test_digest_escape_truncates_before_escaping():
    assert digest.esc("a&b<c>", 3) == "a&amp;b"
    assert digest.esc(None) == "None" and digest.esc(503) == "503"
