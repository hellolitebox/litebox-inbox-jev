"""scripts/dry_classify.py: a failed Jev call never prints the SDK's text (it can echo the
email) and doesn't stop the check of the next emails; an accounts file that doesn't match
categories.yaml stops it with the CLI's one-line message."""

import sys

import dry_classify
import pytest
from conftest import ROOT, make_classification
from dry_classify import Checker
from fakes import FakeClassifier, FakeGmail, email, jev_http_error
from typesafe_sdk import TypeSafeUnprocessableEntityError

from inbox_jev.config import Mode

SECRET = "Dear Ann, the merger closes on Friday"


class _RefusesMerger(FakeClassifier):
    """Jev refusing the "Merger" email with an error whose body echoes it."""

    def classify(self, state):
        if state["subject"] == "Merger":
            self.calls += 1
            raise jev_http_error(TypeSafeUnprocessableEntityError, 422, {"detail": SECRET})
        return super().classify(state)


def test_jev_failure_prints_a_safe_description_and_goes_on(cfg, account, make_sessions, capsys):
    gmail = FakeGmail(
        [email("1", "Ann <ann@other.org>", "Merger"), email("2", "Bob <bob@else.org>", "Lunch")]
    )
    jev = _RefusesMerger({"Lunch": make_classification("important", 0.9)})
    sessions = make_sessions("sqlite:///:memory:")
    with sessions() as session:
        Checker(session, gmail, jev, cfg, account, Mode.ACT, body_chars=3000).run(["1", "2"])

    out = capsys.readouterr().out
    assert SECRET not in out
    lines = out.splitlines()
    assert (
        lines[0] == "1  JEV FAILED: TypeSafeUnprocessableEntityError (HTTP 422, request_id=req-1)"
    )
    assert lines[1].startswith("2  was=-  now=important@0.90  KEEP")
    assert lines[-1] == "emails=2 jev_calls=2"
    assert gmail.modified == [] and gmail.sent == []  # read-only, as ever


def test_accounts_not_matching_categories_exit_with_a_message(monkeypatch, tmp_path):
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text("accounts:\n  - email: a@acme.com\n    act_categories: [markting]\n")
    monkeypatch.setenv("ACCOUNTS_FILE", str(accounts))
    monkeypatch.setenv("CATEGORIES_FILE", str(ROOT / "categories.yaml"))
    monkeypatch.setattr(sys, "argv", ["dry_classify.py", "--account", "a@acme.com", "--ids", "1"])
    monkeypatch.setattr(dry_classify, "connect", lambda d, email: pytest.fail("Gmail opened"))
    with pytest.raises(SystemExit) as exc:
        dry_classify.main()
    message = str(exc.value.code)
    assert message.startswith(f"{accounts}: accounts use categories")
    assert "a@acme.com: unknown act_categories ['markting']" in message
