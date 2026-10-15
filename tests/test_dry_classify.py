"""scripts/dry_classify.py: a failed Jev call never prints the SDK's text (it can echo the
email) and doesn't stop the check of the next emails; an accounts file that doesn't match
categories.yaml stops it with the CLI's one-line message."""

import sys
from types import SimpleNamespace

import dry_classify
import pytest
from conftest import ROOT, make_classification
from dry_classify import Checker
from fakes import FakeClassifier, FakeGmail, email, gmail_factory, jev_http_error, pitch_email
from typesafe_sdk import TypeSafeUnprocessableEntityError

from inbox_jev import rules, triage
from inbox_jev.config import Mode, Settings
from inbox_jev.db import Message
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


def test_report_shows_the_calendar_method(cfg, account, make_sessions, capsys):
    """A real-mail check can see which emails are calendar messages (kept out of auto rules,
    ADR 0047)."""
    rsvp = email("1", "Ana <ana@client.example>", "Accepted: Kickoff")
    rsvp.calendar_method = "REPLY"
    gmail = FakeGmail([rsvp, email("2", "Bob <bob@else.org>", "Lunch")])
    jev = FakeClassifier(
        {
            "Accepted: Kickoff": make_classification("notification", 0.99),
            "Lunch": make_classification("important", 0.9),
        }
    )
    with make_sessions("sqlite:///:memory:")() as session:
        Checker(session, gmail, jev, cfg, account, Mode.ACT, body_chars=3000).run(["1", "2"])

    lines = capsys.readouterr().out.splitlines()
    assert " calendar=REPLY]" in lines[0]
    assert "calendar=" not in lines[2]


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


class _ScriptGmail(FakeGmail):
    """`FakeGmail` as `main` uses it: opened as a context manager, searched with `list_ids`."""

    def __init__(self, emails, matches):
        super().__init__(emails)
        self.matches, self.searches = matches, []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def list_ids(self, query, limit):
        self.searches.append((query, limit))
        return self.matches[:limit]


class _ClosedClassifier(FakeClassifier):
    """`FakeClassifier` counting `close` calls: `main` opens the client, so it closes it."""

    closes = 0

    def close(self):
        self.closes += 1


@pytest.fixture
def script_env(monkeypatch, tmp_path, make_sessions):
    """An accounts file with me@acme.com, a file DB holding one stored decision, the script's
    Gmail (`connect`) and Jev (`Classifier`) replaced by fakes; returns them, with the Gmail
    addresses opened (`opened`) and the Jev clients built (`built`)."""
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text("accounts:\n  - email: me@acme.com\n")
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    sessions = make_sessions(url)
    with sessions() as session:
        session.add(
            Message(
                account="me@acme.com",
                gmail_id="1",
                thread_id="t1",
                sender="ann@other.org",
                category="marketing",
                confidence=0.5,
                mode="act",
                source="jev",
            )
        )
        session.commit()
    monkeypatch.setenv("ACCOUNTS_FILE", str(accounts))
    monkeypatch.setenv("CATEGORIES_FILE", str(ROOT / "categories.yaml"))
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("TYPESAFE_API_KEY", "x")
    gmail = _ScriptGmail(
        [email("1", "Ann <ann@other.org>", "Lunch"), email("2", "Bob <bob@else.org>", "Report")],
        matches=["2", "1"],
    )
    jev = _ClosedClassifier(
        {
            "Lunch": make_classification("important", 0.9),
            "Report": make_classification("important", 0.8),
        }
    )
    opened, built = [], []

    def connect(tokens_dir, address):
        opened.append(address)
        return gmail

    def classifier(key, model, cfg):
        built.append(jev)
        return jev

    monkeypatch.setattr(dry_classify, "connect", connect)
    monkeypatch.setattr(dry_classify, "Classifier", classifier)
    return SimpleNamespace(
        gmail=gmail, jev=jev, opened=opened, built=built, sessions=sessions, tmp_path=tmp_path
    )


def _main(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["dry_classify.py", *args])
    dry_classify.main()


def test_main_checks_the_ids_then_the_query_matches_once_each(script_env, monkeypatch, capsys):
    _main(
        monkeypatch, "--account", "me@acme.com", "--ids", "1", "--query", "in:inbox", "--limit", "5"
    )
    out = capsys.readouterr().out.splitlines()
    assert script_env.opened == ["me@acme.com"]
    assert script_env.gmail.searches == [("in:inbox", 5)]
    assert out[0].startswith("1  was=marketing@0.50  now=important@0.90  KEEP")
    assert out[2].startswith("2  was=-  now=important@0.80  KEEP")
    assert out[-1] == "emails=2 jev_calls=2"
    assert script_env.gmail.closes == 1  # the client is closed when the check ends
    assert script_env.gmail.modified == [] and script_env.gmail.sent == []
    assert script_env.built == [script_env.jev] and script_env.jev.closes == 1
    with script_env.sessions() as session:  # nothing was written
        assert [m.gmail_id for m in session.query(Message)] == ["1"]


def test_main_needs_ids_or_a_query(script_env, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _main(monkeypatch, "--account", "me@acme.com")
    assert exc.value.code == 2
    assert "give --query and/or --ids" in capsys.readouterr().err
    assert script_env.opened == []


def test_main_refuses_an_account_not_in_the_file(script_env, monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _main(monkeypatch, "--account", "you@acme.com", "--ids", "1")
    assert str(exc.value.code).startswith("you@acme.com is not in ")
    assert script_env.opened == []


def test_main_refuses_a_missing_sqlite_db(script_env, monkeypatch):
    missing = script_env.tmp_path / "nope.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{missing}")
    with pytest.raises(SystemExit) as exc:
        _main(monkeypatch, "--account", "me@acme.com", "--ids", "1")
    assert exc.value.code == f"DB not found: {missing.resolve()}"
    assert not missing.exists()  # the read-only check never creates the file
    assert script_env.gmail.closes == 1
    assert script_env.built == []  # the DB is checked before a Jev client exists to leak


def test_main_reference_quiet_prints_only_the_summary_and_writes_nothing(
    script_env, monkeypatch, capsys
):
    with script_env.sessions() as session:  # the user corrected email 1 to important
        session.query(Message).update({Message.corrected_category: "important"})
        session.commit()
    db = script_env.tmp_path / "db.sqlite"
    before = db.read_bytes()

    _main(
        monkeypatch,
        *("--account", "me@acme.com", "--reference", "--quiet"),
        *("--since", "30", "--accepted", "7"),
    )
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "reference: emails=1 corrected=1 accepted=0 jev_calls=1 failed=0 gone=0 edited=0"
        " unknown_category=0",
        "corrected: branch=1/1 (100.0%) stored=0/1 (0.0%) fixed=1 broken=0 baseline_unknown=0",
        "  corrected important -> marketing: branch=0 stored=1",
        "borderline (Jev's category vs its threshold): within_0.05=0 within_0.10=0"
        " of jev_decided=1",
    ]
    assert "Lunch" not in out and "ann@other.org" not in out
    assert script_env.gmail.searches == []  # the reference set comes from the DB
    assert script_env.gmail.modified == [] and script_env.gmail.sent == []
    assert db.read_bytes() == before  # opened read-only


def test_main_quiet_without_reference_prints_only_the_totals(script_env, monkeypatch, capsys):
    _main(monkeypatch, "--account", "me@acme.com", "--ids", "1", "2", "--quiet")
    assert capsys.readouterr().out.splitlines() == ["emails=2 jev_calls=2"]


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["--reference", "--ids", "1"], "--reference checks the stored reference set"),
        (["--reference", "--query", "in:inbox"], "--reference checks the stored reference set"),
        (["--ids", "1", "--since", "7"], "--since and --accepted go with --reference"),
        (["--ids", "1", "--accepted", "7"], "--since and --accepted go with --reference"),
        (["--reference", "--since", "0"], "must be at least 1 day, got 0"),
        (["--reference", "--accepted", "week"], "not a number of days: 'week'"),
    ],
)
def test_main_rejects_mixed_or_bad_reference_options(script_env, monkeypatch, capsys, args, error):
    with pytest.raises(SystemExit) as exc:
        _main(monkeypatch, "--account", "me@acme.com", *args)
    assert exc.value.code == 2
    assert error in capsys.readouterr().err
    assert script_env.opened == []


def test_checker_ignores_the_learned_rules_of_a_sender_decided_per_email(
    cfg, account, make_sessions
):
    """As `run`: an auto rule of a sender in decide_each_email doesn't decide, Jev does; a manual
    rule still does."""
    account = account.model_copy(update={"decide_each_email": ["noreply@crm.example"]})
    gmail = FakeGmail(
        [
            email("1", "CRM <noreply@crm.example>", "New submission"),
            email("2", "Shop <promo@shop.com>", "Sale"),
        ]
    )
    jev = FakeClassifier({"New submission": make_classification("action_required", 0.96)})
    sessions = make_sessions("sqlite:///:memory:")
    with sessions() as session:
        rules.set_rule(
            session, account.email, "noreply@crm.example", "notification", RuleSource.AUTO
        )
        rules.set_rule(session, account.email, "promo@shop.com", "newsletter", RuleSource.MANUAL)
        session.commit()
        checker = Checker(session, gmail, jev, cfg, account, Mode.ACT, body_chars=3000)
        lead, sale = (checker.decide(gmail.get(i)) for i in ("1", "2"))
    assert (lead.decision.source, lead.decision.category) == ("jev", "action_required")
    assert (sale.decision.source, sale.decision.category) == ("rule", "newsletter")


def test_checker_ignores_the_learned_rules_of_a_list_address(cfg, account, make_sessions):
    """As `run` (ADR 0051): a correction rule on an address doesn't decide an email sent as that
    list's own address; Jev does."""
    gmail = FakeGmail(
        [
            email(
                "1",
                "Hello <hello@groups.example>",
                "Question",
                list_post="<mailto:hello@groups.example>",
            )
        ]
    )
    jev = FakeClassifier({"Question": make_classification("important", 0.96)})
    sessions = make_sessions("sqlite:///:memory:")
    with sessions() as session:
        rules.set_rule(
            session, account.email, "hello@groups.example", "spam", RuleSource.CORRECTION
        )
        session.commit()
        checker = Checker(session, gmail, jev, cfg, account, Mode.ACT, body_chars=3000)
        checked = checker.decide(gmail.get("1"))
    assert (checked.decision.source, checked.decision.category) == ("jev", "important")
