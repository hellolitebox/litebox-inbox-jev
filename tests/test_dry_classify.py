"""scripts/dry_classify.py: a failed Jev call never prints the SDK's text (it can echo the
email) and doesn't stop the check of the next emails; an accounts file that doesn't match
categories.yaml stops it with the CLI's one-line message."""

import sys

import dry_classify
import pytest
from conftest import ROOT, make_classification
from dry_classify import Checker
from fakes import FakeClassifier, FakeGmail, email, gmail_factory, jev_http_error, pitch_email
from typesafe_sdk import TypeSafeUnprocessableEntityError

from inbox_jev import rules, triage
from inbox_jev.config import Mode, Settings
from inbox_jev.rules import RuleSource

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


def _with_rules(sessions, account):
    """`sessions` with a keep rule (ann@keep.org), a cold_outreach rule (bob@agency.io: spares
    known contacts, so the sent-mail search can veto its unsubscribe) and a marketing rule
    (promo@shop.com: unsubscribes known contacts too, so no search)."""
    with sessions() as session:
        for sender, category in [
            ("ann@keep.org", "important"),
            ("bob@agency.io", "cold_outreach"),
            ("promo@shop.com", "marketing"),
        ]:
            rules.set_rule(session, account.email, sender, category, RuleSource.MANUAL)
        session.commit()
    return sessions


def _parity_emails():
    return [
        email("1", "Client <c@client.com>", "Proposal"),  # no rule: Jev + sent-mail search
        email("2", "Bank <x@acme.com>", "Internal"),  # allowlisted: Jev for the flags only
        email("3", "Ann <ann@keep.org>", "Hello"),  # keep rule: Jev for the flags only
        pitch_email("4"),  # cold_outreach rule, verified: sent-mail search only
        email("5", "Shop <promo@shop.com>", "Sale", list_unsubscribe="<mailto:u@shop.com>"),
    ]


_ANSWERS = {
    "Proposal": make_classification("important", 0.9),
    "Internal": make_classification("notification", 0.99),
    "Hello": make_classification("important", 0.95),
}


def test_checker_makes_the_same_jev_and_sent_mail_calls_as_run(cfg, account, make_sessions):
    """The script and `run` gather an email's inputs with one function (`triage.gather_inputs`):
    for the three paths (no rule; keep rule or allowlist; a rule that could unsubscribe) both ask
    Jev the same states and run the same sent-mail searches, and no more."""
    run_gmail, run_jev = FakeGmail(_parity_emails()), FakeClassifier(dict(_ANSWERS))
    settings = Settings(_env_file=None, typesafe_api_key="x", database_url="sqlite:///:memory:")
    run_sessions = _with_rules(make_sessions(settings.database_url), account)
    with triage.Context(
        settings, cfg, run_sessions, gmail_factory(run_gmail), classifier=run_jev
    ) as ctx:
        triage.run_account(ctx, account, Mode.ACT)

    dry_gmail, dry_jev = FakeGmail(_parity_emails()), FakeClassifier(dict(_ANSWERS))
    dry_sessions = _with_rules(make_sessions("sqlite:///:memory:"), account)
    with dry_sessions() as session:
        checker = Checker(session, dry_gmail, dry_jev, cfg, account, Mode.ACT, body_chars=3000)
        checker.run(["1", "2", "3", "4", "5"])

    assert dry_gmail.written_queries == run_gmail.written_queries
    assert dry_gmail.written_queries == ["c@client.com", "bob@agency.io", "agency.io"]
    assert dry_jev.states == run_jev.states
    assert [s["subject"] for s in dry_jev.states] == ["Proposal", "Internal", "Hello"]
    assert checker.jev_calls == 3


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
