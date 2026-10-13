"""Jev failures in a pass: what each SDK error means (`failure_kind`), when a pass stops asking
Jev (`JevGuard`), a missing API key, and that no Jev error text (which can echo the email) is
stored or logged."""

import json
import logging

import httpx2
import pytest
from fakes import JevFails, email, jev_http_error
from sqlalchemy.exc import OperationalError
from typesafe_sdk import (
    Choice,
    RetryPolicy,
    TypeSafeAPIConnectionError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeBadRequestError,
    TypeSafeClient,
    TypeSafeError,
    TypeSafeInternalServerError,
    TypeSafeNotFoundError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
    TypeSafeUnprocessableEntityError,
)

from inbox_jev import logs, rules, triage
from inbox_jev.classifier import (
    Classifier,
    JevFailure,
    JevNotConfigured,
    describe_jev_error,
    failure_kind,
)
from inbox_jev.config import Mode, Settings
from inbox_jev.db import MAX_ERROR_ATTEMPTS, Message, MessageStatus, Run, now
from inbox_jev.logs import JsonFormatter
from inbox_jev.rules import RuleSource

BODY = "Wire 9.800 USD to account ES12-SECRET before Friday"


def _sdk_classifier(cfg, handler) -> Classifier:
    """The real Classifier, its HTTP calls answered by `handler` (no network, no SDK retries)."""
    clf = Classifier("key", "jev-latest", cfg)
    clf._client.close()
    clf._client = TypeSafeClient(
        api_key="key",
        model="jev-latest",
        transport=httpx2.MockTransport(handler),
        retry=RetryPolicy(max_retries=0),
    )
    return clf


def _echoing(status: int):
    """A Jev answering `status` with a validation message that quotes the email's body."""

    def handler(request):
        state = json.loads(request.content)["state"]
        return httpx2.Response(
            status,
            json={"detail": f"state.body is not valid: {state['body']!r}"},
            headers={"x-typesafe-request-id": "req-422"},
        )

    return handler


def _rows(ctx):
    with ctx.sessions() as s:
        rows = {m.gmail_id: m for m in s.query(Message)}
        s.expunge_all()
        return rows


def _run(ctx):
    with ctx.sessions() as s:
        return s.query(Run).order_by(Run.id.desc()).first()


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (jev_http_error(TypeSafeAuthenticationError, 401), JevFailure.CONFIG),
        (jev_http_error(TypeSafePermissionDeniedError, 403), JevFailure.CONFIG),
        (jev_http_error(TypeSafeNotFoundError, 404), JevFailure.CONFIG),
        (jev_http_error(TypeSafeBadRequestError, 400), JevFailure.REJECTED),
        (jev_http_error(TypeSafeUnprocessableEntityError, 422), JevFailure.REJECTED),
        (jev_http_error(TypeSafeRateLimitError, 429), JevFailure.TRANSIENT),
        (jev_http_error(TypeSafeInternalServerError, 503), JevFailure.TRANSIENT),
        (TypeSafeAPITimeoutError(30.0), JevFailure.TRANSIENT),
        (TypeSafeAPIConnectionError("Connection error: refused"), JevFailure.TRANSIENT),
        (TypeSafeError("anything else"), JevFailure.TRANSIENT),
    ],
)
def test_failure_kind(error, kind):
    assert failure_kind(error) is kind


def test_sdk_error_text_echoes_the_body_but_the_description_does_not(cfg):
    """Pins why `describe_jev_error` exists: the SDK's str() quotes the response body."""
    clf = _sdk_classifier(cfg, _echoing(422))
    with pytest.raises(TypeSafeUnprocessableEntityError) as info:
        clf.classify({"subject": "s", "body": BODY})
    assert BODY in str(info.value)
    assert describe_jev_error(info.value) == (
        "TypeSafeUnprocessableEntityError (HTTP 422, request_id=req-422)"
    )
    assert describe_jev_error(info.value, request_id=False) == (
        "TypeSafeUnprocessableEntityError (HTTP 422)"
    )
    assert describe_jev_error(TypeSafeError(BODY)) == "TypeSafeError"


@pytest.mark.parametrize(
    "message",
    [
        "At least one question is required.",
        'Choice question "category" has no criteria; at least one choice is required.',
        'Noul question "tag_invoice" has neither instructions nor criteria; at least one is '
        "required.",
        'Question "needs_reply" requires "criteria".',
        "State is required.",
        "The request body could not be encoded as JSON",
    ],
)
def test_sdk_client_side_messages_are_kept(message):
    """Raised before any request, they never hold the state and say which question is wrong."""
    assert describe_jev_error(TypeSafeError(message)) == f"TypeSafeError: {message}"


@pytest.mark.parametrize(
    "message",
    [
        BODY,
        f"State is required. {BODY}",  # the whole message must match
        f'Question "{BODY}" requires "criteria".',  # a question name is a short identifier
    ],
)
def test_other_plain_sdk_messages_are_reduced_to_the_type(message):
    assert describe_jev_error(TypeSafeError(message)) == "TypeSafeError"


def test_real_sdk_question_error_is_described(cfg):
    """Pins the allowlist to the installed SDK's wording: a category set with no choices."""
    clf = _sdk_classifier(cfg, _echoing(422))
    clf._questions["category"] = Choice(instructions="x", criteria={})
    with pytest.raises(TypeSafeError) as info:
        clf.classify({"subject": "s", "body": BODY})
    assert describe_jev_error(info.value) == (
        'TypeSafeError: Choice question "category" has no criteria; at least one choice is '
        "required."
    )


def _formatted(exc: BaseException) -> str:
    """`exc` as the CLI logs it (`log.exception` through JsonFormatter): the whole chain."""
    return JsonFormatter().formatException((type(exc), exc, exc.__traceback__))


def test_failing_to_record_a_jev_error_does_not_chain_its_text(setup, cfg, monkeypatch):
    """If storing a refused email fails (database locked…), the error that escapes the run
    carries no Jev error as context: `cli` logs its whole traceback."""
    ctx, gmail, account = setup
    for e in gmail.emails.values():
        e.body = BODY
    ctx = triage.Context(
        ctx.settings, cfg, ctx.sessions, classifier=_sdk_classifier(cfg, _echoing(422))
    )

    def locked(*args, **kwargs):
        raise OperationalError("UPDATE messages", {}, Exception("database is locked"))

    monkeypatch.setattr(triage, "_record_error", locked)
    with pytest.raises(OperationalError) as info:
        triage.run_account(ctx, account, Mode.ACT)

    assert info.value.__context__ is None
    assert "SECRET" not in _formatted(info.value)
    assert "SECRET" not in (_run(ctx).error or "")


def test_failing_to_handle_a_jev_error_in_the_guard_does_not_chain_its_text(
    setup, cfg, monkeypatch, caplog
):
    """Same for `JevGuard` and the flags-only call: whatever their handling of a failed call
    raises is logged with its traceback, which must not reach the Jev error."""
    ctx, gmail, account = setup
    for e in gmail.emails.values():
        e.body = BODY
    ctx = triage.Context(
        ctx.settings, cfg, ctx.sessions, classifier=_sdk_classifier(cfg, _echoing(422))
    )

    def broken(*args, **kwargs):
        raise RuntimeError("describe failed")

    monkeypatch.setattr(triage, "describe_jev_error", broken)
    caplog.set_level(logging.INFO, logger="typesafe_sdk")
    triage.run_account(ctx, account, Mode.ACT)

    failed = [r for r in caplog.records if r.getMessage() == "message failed"]
    assert failed and all(r.exc_info and r.exc_info[1].__context__ is None for r in failed)
    logged = [JsonFormatter().format(r) for r in caplog.records]
    stored = [r.error or "" for r in _rows(ctx).values()]
    assert not [t for t in logged + stored if "SECRET" in t]


def test_422_echoing_the_email_is_never_stored_or_logged(setup, cfg, caplog):
    ctx, gmail, account = setup
    for e in gmail.emails.values():
        e.body = BODY
    ctx = triage.Context(
        ctx.settings, cfg, ctx.sessions, classifier=_sdk_classifier(cfg, _echoing(422))
    )
    caplog.set_level(logging.DEBUG)
    # logs.setup keeps the SDK at INFO (its DEBUG lines are the raw request: see the test below).
    caplog.set_level(logging.INFO, logger="typesafe_sdk")
    with pytest.raises(triage.JevUnavailable):  # 1, 2 and 3 (flags only) refused in a row
        triage.run_account(ctx, account, Mode.ACT)

    rows = _rows(ctx)
    for gmail_id in ("1", "2"):
        assert rows[gmail_id].error == (
            "TypeSafeUnprocessableEntityError (HTTP 422, request_id=req-422)"
        )
        # Jev refuses this email: retrying it every pass won't help, it gives up at once.
        assert rows[gmail_id].error_attempts == MAX_ERROR_ATTEMPTS
    run = _run(ctx)
    assert run.error == (
        "Jev unavailable: 3 calls in a row failed (last: TypeSafeUnprocessableEntityError "
        "(HTTP 422)); emails that need Jev wait for the next run"
    )
    stored = [r.error or "" for r in rows.values()] + [run.error]
    logged = [JsonFormatter().format(r) for r in caplog.records]
    assert caplog.records and not [t for t in stored + logged if "SECRET" in t]


def test_auth_failure_stops_jev_without_blaming_the_email(setup, cfg, caplog):
    """401 on the first call: no email is charged, rule and allowlist emails still go through."""
    ctx, gmail, account = setup
    jev = JevFails(jev_http_error(TypeSafeAuthenticationError, 401, {"error": "bad key"}))
    ctx = triage.Context(ctx.settings, cfg, ctx.sessions, classifier=jev)
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "w@weird.biz", "marketing", RuleSource.MANUAL)
        s.add(  # failed twice before, due again now
            Message(
                account=account.email,
                gmail_id="1",
                thread_id="t1",
                sender="c@client.com",
                status=MessageStatus.ERROR,
                mode=Mode.ACT,
                error="TypeSafeAPITimeoutError: Request timed out (timeout=30.0).",
                error_attempts=2,
                processed_at=now().replace(year=2000),
            )
        )
        s.commit()
    with pytest.raises(triage.JevUnavailable):
        triage.run_account(ctx, account, Mode.ACT)

    assert jev.calls == 1  # email 1; 2 needs Jev too but isn't sent, 3's flags call is skipped
    rows = _rows(ctx)
    assert (rows["1"].status, rows["1"].error_attempts) == ("error", 2)  # untouched
    assert "2" not in rows  # waits for the next run
    assert (rows["3"].status, rows["3"].source, rows["3"].needs_reply) == (
        "done",
        "allowlist",
        None,
    )
    assert (rows["4"].status, rows["4"].source, rows["4"].archived) == ("done", "rule", True)
    run = _run(ctx)
    assert (run.processed, run.errors) == (2, 0)
    assert run.error == (
        "Jev rejected the API key or model (TypeSafeAuthenticationError (HTTP 401)): check "
        "TYPESAFE_API_KEY and TYPESAFE_MODEL; emails that need Jev wait for the next run"
    )
    assert gmail.written_queries == ["c@client.com"]  # no sent-mail search for skipped email 2
    [stopped] = [r for r in caplog.records if r.getMessage() == "stopped asking Jev"]
    assert stopped.levelno == logging.ERROR and stopped.account == account.email


def test_transient_failures_stop_jev_after_three_in_a_row(setup, cfg):
    ctx, gmail, account = setup
    gmail.emails.update({e.id: e for e in [email("5", "Ann <ann@other.org>", "Lunch?")]})
    jev = JevFails(TypeSafeAPITimeoutError(30.0))
    ctx = triage.Context(ctx.settings, cfg, ctx.sessions, classifier=jev)
    with pytest.raises(triage.JevUnavailable):
        triage.run_account(ctx, account, Mode.ACT)

    assert jev.calls == 3  # 1, 2 and 3's flags call; then 4 and 5 aren't sent
    rows = _rows(ctx)
    assert [rows[i].error_attempts for i in ("1", "2")] == [1, 1]  # the usual backoff
    assert rows["1"].error == "TypeSafeAPITimeoutError: Request timed out (timeout=30.0)."
    assert rows["3"].status == "done" and {"4", "5"}.isdisjoint(rows)
    assert _run(ctx).error.startswith("Jev unavailable: 3 calls in a row failed (last: ")


def test_a_success_resets_the_count(setup, cfg):
    ctx, gmail, account = setup
    answers = ctx.classifier.answers

    class Flaky(JevFails):
        def classify(self, state):  # fails all but "Internal" (email 3's flags call)
            self.error = None if state["subject"] == "Internal" else TypeSafeAPITimeoutError(1.0)
            return super().classify(state)

    jev = Flaky(None, answers)
    ctx = triage.Context(ctx.settings, cfg, ctx.sessions, classifier=jev)
    run = triage.run_account(ctx, account, Mode.ACT)  # 2 fails, a success, 1 fail: no stop
    assert (run.processed, run.errors, run.error) == (1, 3, None) and jev.calls == 4


def test_missing_api_key_fails_the_run_before_gmail(setup, monkeypatch):
    ctx, gmail, account = setup
    settings = Settings(_env_file=None, typesafe_api_key="", database_url="sqlite:///:memory:")
    ctx = triage.Context(settings, ctx.cfg, ctx.sessions)  # no injected Jev: built from settings

    def no_gmail(tokens_dir, email):
        raise AssertionError("Gmail must not be touched")

    monkeypatch.setattr(triage, "load_credentials", no_gmail)
    with pytest.raises(JevNotConfigured):
        triage.run_account(ctx, account, Mode.ACT)
    run = _run(ctx)
    assert run.error == "TYPESAFE_API_KEY is not set: add it to .env; nothing was processed"
    assert run.finished_at is not None and _rows(ctx) == {}


def test_malformed_api_key_is_not_configured(cfg):
    with pytest.raises(JevNotConfigured, match="TYPESAFE_API_KEY or TYPESAFE_MODEL is invalid"):
        Classifier("two words", "jev-latest", cfg)


def test_sdk_logger_never_logs_bodies_even_at_debug():
    root, sdk = logging.getLogger(), logging.getLogger("typesafe_sdk")
    saved = (root.handlers[:], root.level, sdk.level)
    try:
        logs.setup("DEBUG")
        assert root.level == logging.DEBUG and sdk.getEffectiveLevel() == logging.INFO
        logs.setup("WARNING")
        assert sdk.getEffectiveLevel() == logging.WARNING
    finally:
        root.handlers[:], _, _ = saved
        root.setLevel(saved[1])
        sdk.setLevel(saved[2])


def test_gmail_http_error_text_has_no_message_content():
    """googleapiclient's HttpError text: status, URL and Google's error message, never a body we
    sent, so storing it as is (as `run` does for Gmail failures) is safe."""
    from googleapiclient.errors import HttpError

    resp = type("Resp", (), {"status": 400, "reason": "Bad Request"})()
    content = json.dumps({"error": {"message": "Invalid label: Label_9"}}).encode()
    err = HttpError(resp, content, uri="https://gmail.googleapis.com/gmail/v1/users/me/messages/1")
    assert str(err) == (
        "<HttpError 400 when requesting https://gmail.googleapis.com/gmail/v1/users/me/messages/1 "
        'returned "Invalid label: Label_9". Details: "Invalid label: Label_9">'
    )
