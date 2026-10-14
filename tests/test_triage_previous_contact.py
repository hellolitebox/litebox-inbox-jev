"""`previous_contact` (the sent-mail search): when it runs and what it changes."""

from types import SimpleNamespace

import pytest
from conftest import make_classification, selects_from
from fakes import (
    SentMailService,
    email,
    pitch_email,
    sent_mail_gmail,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import OperationalError

from inbox_jev import rules, scan_rows, triage
from inbox_jev.classification import PreviousContact
from inbox_jev.config import Mode
from inbox_jev.db import SenderRule, SentDomainScan, Unsubscribe, now
from inbox_jev.rules import RuleSource
from inbox_jev.sent_scan import DomainScan, DomainVerdict, MemoryScans, scan_rules
from inbox_jev.unsubscribe import UnsubscribeMethod, UnsubscribeStatus


def test_previous_contact_is_sent_to_jev(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.LABEL)
    contacts = {s["subject"]: s.get("previous_contact") for s in ctx.classifier.states}
    assert contacts == {
        "Proposal": "wrote_to_sender",
        "50% off": "never",
        "Maybe promo": "never",
        "Internal": None,  # allowlisted: asked for the flags, without previous_contact
    }
    assert "acme.com" not in gmail.written_queries  # allowlisted sender: no lookup at all


def test_known_contact_reply_blocks_group_archive_end_to_end(setup):
    # Same uncertain pitch with needs_reply from a sender I wrote to (stays) and a stranger
    # (group-archived): triage passes previous_contact through to the policy.
    ctx, gmail, account = setup
    gmail.emails = {
        "5": email("5", "Vendor <c@client.com>", "Follow-up"),
        "6": email("6", "Stranger <s@pitch.biz>", "Pitch"),
    }
    for subject in ("Follow-up", "Pitch"):
        c = make_classification("cold_outreach", 0.60, needs_reply=0.9)
        c.probabilities = {"cold_outreach": 0.60, "marketing": 0.36, "important": 0.0}
        ctx.classifier.answers[subject] = c
    triage.run_account(ctx, account, Mode.ACT)
    mods = {m[0]: m for m in gmail.modified}
    assert "Label_Jev/Needs Reply" in mods["5"][1] and mods["5"][2] == []
    assert mods["6"][2] == ["INBOX"]


class ContactGmail:
    def __init__(self, written=(), *, fail=False):
        self.written, self.fail, self.queries, self.scans = set(written), fail, [], []

    def has_written_to(self, query, scans=None):
        self.queries.append(query)
        self.scans.append(scans)
        if self.fail:
            raise RuntimeError("boom")
        return query in self.written


@pytest.mark.parametrize(
    ("written", "sender", "expected", "queries"),
    [
        ({"a@corp.com"}, "A@Corp.com", PreviousContact.WROTE_TO_SENDER, ["a@corp.com"]),
        (
            {"corp.com"},
            "b@corp.com",
            PreviousContact.WROTE_TO_THEIR_DOMAIN,
            ["b@corp.com", "corp.com"],
        ),
        ((), "b@corp.com", PreviousContact.NEVER, ["b@corp.com", "corp.com"]),
        (
            {"gmail.com"},
            "x@gmail.com",
            PreviousContact.NEVER,
            ["x@gmail.com"],
        ),  # free mail: no domain check
        # Free mail under a country domain is free mail too: no domain check.
        ({"yahoo.com.ar"}, "ana@yahoo.com.ar", PreviousContact.NEVER, ["ana@yahoo.com.ar"]),
        ({"hotmail.co.uk"}, "bo@hotmail.co.uk", PreviousContact.NEVER, ["bo@hotmail.co.uk"]),
        ({"acme.com"}, "y@acme.com", PreviousContact.NEVER, ["y@acme.com"]),  # my own domain
        ((), "x@corp.com OR from:me", None, []),  # never builds a query from odd input
        # A machine sender is never searched for itself, only its domain.
        (
            {"messages-noreply@linkedin.com"},
            "messages-noreply@linkedin.com",
            PreviousContact.NEVER,
            ["linkedin.com"],
        ),
        (
            {"linkedin.com"},
            "messages-noreply@linkedin.com",
            PreviousContact.WROTE_TO_THEIR_DOMAIN,
            ["linkedin.com"],
        ),
        ({"bounce@shop.com"}, "bounce@shop.com", PreviousContact.NEVER, ["shop.com"]),
        (
            {"noreply@gmail.com"},
            "noreply@gmail.com",
            PreviousContact.NEVER,
            [],
        ),  # free mail: nothing to ask
        (
            {"reply@corp.com"},
            "reply@corp.com",
            PreviousContact.WROTE_TO_SENDER,
            ["reply@corp.com"],
        ),  # human
        # A company called Reply under a two-level public suffix is a person, searched for itself.
        (
            {"ana@reply.com.ar"},
            "ana@reply.com.ar",
            PreviousContact.WROTE_TO_SENDER,
            ["ana@reply.com.ar"],
        ),
    ],
)
def test_previous_contact(written, sender, expected, queries):
    gmail = ContactGmail(written)
    assert triage.previous_contact(gmail, "me@acme.com", sender) == expected
    assert gmail.queries == queries


def test_previous_contact_keeps_only_the_domain_search_in_the_scans():
    gmail, scans = ContactGmail(), MemoryScans()
    assert (
        triage.previous_contact(gmail, "me@acme.com", "b@corp.com", scans) == PreviousContact.NEVER
    )
    assert list(zip(gmail.queries, gmail.scans, strict=True)) == [
        ("b@corp.com", None),
        ("corp.com", scans),
    ]


def test_domain_scan_rows_round_trip(make_sessions):
    sessions = make_sessions("sqlite:///:memory:")
    scan = DomainScan("code.example", scan_rules(), newest_s=20, newest_ids=["a"])
    scan.include("b", 10)
    with sessions() as s:
        rows = scan_rows.DomainScanRows(s, "me@acme.com")
        assert rows.load("code.example") is None
        rows.save(scan)
        s.commit()
    with sessions() as s:
        assert scan_rows.DomainScanRows(s, "other@acme.com").load("code.example") is None
        rows = scan_rows.DomainScanRows(s, "me@acme.com")
        assert rows.load("code.example") == scan
        scan.verdict, scan.verdict_at, scan.read = DomainVerdict.NEVER, now(), 2
        scan.reached_end = True
        rows.save(scan)
        s.commit()
    with sessions() as s:
        assert scan_rows.DomainScanRows(s, "me@acme.com").load("code.example") == scan
        assert s.query(SentDomainScan).count() == 1


def test_domain_scan_rows_save_over_a_row_another_session_stored(make_sessions, tmp_path):
    # `run` and `unsubscribe-retry` start a scan of the same domain: both load no row, the other
    # one saves and commits first. Saving updates that row; loading again sees the update.
    sessions = make_sessions(f"sqlite:///{tmp_path / 'race.db'}")
    mine = DomainScan("code.example", scan_rules(), DomainVerdict.NEVER, now(), 20, ["a"], 10)
    mine.read, mine.reached_end = 2, True
    with sessions() as s, sessions() as other:
        rows = scan_rows.DomainScanRows(s, "me@acme.com")
        assert rows.load("code.example") is None
        theirs = DomainScan("code.example", scan_rules(), newest_s=30, newest_ids=["z"], read=1)
        scan_rows.DomainScanRows(other, "me@acme.com").save(theirs)
        other.commit()
        assert rows.load("code.example") == theirs  # not a stale copy from the identity map
        rows.save(mine)
        s.commit()
        assert rows.load("code.example") == mine  # the last scan saved wins
    with sessions() as s:
        assert s.query(SentDomainScan).count() == 1


@pytest.mark.parametrize(("rules_then", "kept"), [(scan_rules(), True), ("0-old", False)])
def test_domain_scan_rows_save_keeps_a_concurrent_person(make_sessions, tmp_path, rules_then, kept):
    # The other process found a person; a scan that started before it can't undo that, unless
    # the person was found under other rules.
    sessions = make_sessions(f"sqlite:///{tmp_path / 'race.db'}")
    person = DomainScan("code.example", rules_then, DomainVerdict.PERSON, now(), 30, ["p"], 30)
    mine = DomainScan("code.example", scan_rules(), DomainVerdict.NEVER, now(), 20, ["a"], 20)
    with sessions() as s, sessions() as other:
        rows = scan_rows.DomainScanRows(s, "me@acme.com")
        assert rows.load("code.example") is None
        scan_rows.DomainScanRows(other, "me@acme.com").save(person)
        other.commit()
        rows.save(mine)
        s.commit()
    with sessions() as s:
        stored = scan_rows.DomainScanRows(s, "me@acme.com").load("code.example")
        assert stored == (person if kept else mine)


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_scan_upsert_on_each_documented_database(dialect):
    scan = DomainScan("code.example", scan_rules())
    sql = str(scan_rows.scan_upsert(dialect.name, "me@acme.com", scan).compile(dialect=dialect))
    assert "ON CONFLICT (account, domain) DO UPDATE" in sql
    assert "WHERE sent_domain_scans.verdict !=" in sql


def test_scan_upsert_refuses_a_database_without_one():
    with pytest.raises(NotImplementedError, match="mysql"):
        scan_rows.scan_upsert("mysql", "me@acme.com", DomainScan("code.example", scan_rules()))


def test_domain_scan_rows_refuse_a_database_without_the_upsert():
    # Before any search: a domain verdict it then couldn't store would reach `run` as unknown.
    mysql = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="mysql")))
    with pytest.raises(NotImplementedError, match="mysql"):
        scan_rows.DomainScanRows(mysql, "me@acme.com")


def test_domain_scan_rows_accept_only_the_given_dialects():
    sqlite_db = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    )
    scan_rows.DomainScanRows(sqlite_db, "me@acme.com")  # documented: accepted
    with pytest.raises(NotImplementedError, match="sqlite"):
        scan_rows.DomainScanRows(sqlite_db, "me@acme.com", dialects=("postgresql",))


class NoUpsertScans(MemoryScans):
    """Domain scans saved in a database without the upsert (e.g. MySQL)."""

    def save(self, scan):
        scan_rows.scan_upsert("mysql", "me@acme.com", scan)


def test_previous_contact_raises_when_the_database_has_no_upsert(monkeypatch):
    # Not `None` (unknown, no veto in run): the scan found a person and couldn't store it.
    svc = SentMailService({"g0": {"To": "ana@code.example"}})
    with pytest.raises(NotImplementedError, match="mysql"):
        triage.previous_contact(
            sent_mail_gmail(monkeypatch, svc), "me@acme.com", "sales@code.example", NoUpsertScans()
        )


def test_run_fails_the_account_before_gmail_on_a_database_without_the_upsert(setup):
    ctx, gmail, account = setup
    ctx.scan_dialects = ("postgresql",)  # stands for a database without the upsert
    with pytest.raises(NotImplementedError, match="sqlite"):
        triage.run_account(ctx, account, Mode.ACT)
    assert gmail.sent == [] and gmail.modified == [] and ctx.classifier.states == []


class LockedScans(MemoryScans):
    """Domain scans whose save fails as SQLite does when another process holds the lock."""

    def save(self, scan):
        raise OperationalError("INSERT INTO sent_domain_scans", {}, Exception("database is locked"))


def test_previous_contact_raises_when_the_scan_cant_be_saved(monkeypatch):
    # Not `None`: run would treat an unknown contact as no veto on the unsubscribe.
    svc = SentMailService({"g0": {"To": "reply+0@reply.code.example"}})
    with pytest.raises(OperationalError):
        triage.previous_contact(
            sent_mail_gmail(monkeypatch, svc), "me@acme.com", "ci@code.example", LockedScans()
        )


def test_run_fails_the_email_when_the_scan_cant_be_saved(setup, monkeypatch):
    ctx, gmail, account = setup
    svc = SentMailService({"g0": {"To": "bounces+0@agency.io"}})
    gmail.has_written_to = sent_mail_gmail(monkeypatch, svc).has_written_to
    monkeypatch.setattr(scan_rows.DomainScanRows, "save", LockedScans.save)
    gmail.emails = {"9": pitch_email("9")}
    ctx.classifier.answers["Pitch 9"] = make_classification("cold_outreach", 0.97)
    triage.run_account(ctx, account, Mode.ACT)
    assert gmail.sent == [] and gmail.modified == []
    with ctx.sessions() as s:
        assert s.query(Unsubscribe).count() == 0
        assert triage.find_message(s, account.email, "9").status == "error"  # retried later


def test_run_keeps_the_domain_scan_between_runs(setup, monkeypatch):
    # A sender at a domain I replied to 30 times at machine addresses: the first run reads 25
    # (known for now), the next one the other 5 (never), each saved with the email's commit.
    ctx, gmail, account = setup
    replies = {f"g{i}": {"To": f"reply+{i}@reply.code.example"} for i in range(30)}
    svc = SentMailService(replies)
    contacts = []
    for i in range(2):
        gmail.has_written_to = sent_mail_gmail(monkeypatch, svc).has_written_to
        gmail.emails = {f"n{i}": email(f"n{i}", "Code <ci@code.example>", f"Build {i}")}
        ctx.classifier.answers[f"Build {i}"] = make_classification("notification", 0.97)
        triage.run_account(ctx, account, Mode.LABEL)
        contacts.append(ctx.classifier.states[-1]["previous_contact"])
    assert contacts == ["wrote_to_their_domain", "never"]
    with ctx.sessions() as s:
        row = s.query(SentDomainScan).one()
        assert (row.account, row.domain, row.verdict, row.read) == (
            account.email,
            "code.example",
            "never",
            30,
        )


def test_previous_contact_error_is_unknown():
    assert triage.previous_contact(ContactGmail(fail=True), "me@acme.com", "a@b.com") is None


def test_auto_rule_then_reply_to_sender_never_unsubscribes(setup):
    # Bob earns an auto rule while a stranger (label mode, so nothing is unsubscribed yet).
    # Then I reply to him: the rule still archives his next pitch but never unsubscribes.
    ctx, gmail, account = setup
    hits = ctx.cfg.auto_rule.hits
    gmail.emails = {str(i): pitch_email(str(i)) for i in range(hits)}
    for i in range(hits):
        ctx.classifier.answers[f"Pitch {i}"] = make_classification("cold_outreach", 0.97)
    triage.run_account(ctx, account, Mode.LABEL)
    with ctx.sessions() as s:
        rule = s.query(SenderRule).filter_by(sender_key="bob@agency.io").one()
        assert (rule.category, rule.source, rule.active) == ("cold_outreach", "auto", True)

    gmail.written = frozenset({"bob@agency.io"})
    gmail.emails["9"] = pitch_email("9")
    calls = ctx.classifier.calls
    triage.run_account(ctx, account, Mode.ACT)
    assert ctx.classifier.calls == calls  # the rule decided (the label-mode ones are redone too)
    assert all(m[2] == ["INBOX"] for m in gmail.modified[-(hits + 1) :])  # archived
    assert gmail.sent == []
    with ctx.sessions() as s:
        assert s.query(Unsubscribe).count() == 0


@pytest.mark.parametrize("act_categories", [None, ["cold_outreach"]])
def test_manual_domain_rule_on_known_domain_never_unsubscribes(setup, act_categories):
    ctx, gmail, account = setup
    account.act_categories = act_categories  # an explicit list must still run the search
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "@agency.io", "cold_outreach", RuleSource.MANUAL)
        s.commit()
    gmail.written = frozenset({"agency.io"})  # wrote_to_their_domain
    gmail.emails = {"9": pitch_email("9")}
    triage.run_account(ctx, account, Mode.ACT)
    assert gmail.written_queries == ["bob@agency.io", "agency.io"]
    assert gmail.modified[-1][2] == ["INBOX"]
    assert gmail.sent == []


def test_rule_path_still_unsubscribes_strangers(setup):
    ctx, gmail, account = setup
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "@agency.io", "cold_outreach", RuleSource.MANUAL)
        s.commit()
    gmail.emails = {"9": pitch_email("9")}
    triage.run_account(ctx, account, Mode.ACT)
    assert gmail.written_queries == ["bob@agency.io", "agency.io"]  # "never"
    assert gmail.sent == ["u@agency.io"]


@pytest.mark.parametrize(
    ("category", "headers", "settled"),
    [
        ("marketing", {}, False),  # unsubscribes known contacts too
        ("newsletter", {}, False),  # never unsubscribes
        ("cold_outreach", {"unsubscribe": None}, False),  # unverified: no header
        ("cold_outreach", {"list_post": "<mailto:g@groups.io>"}, False),  # discussion list
        ("cold_outreach", {}, True),  # list already unsubscribed
    ],
)
def test_rule_hit_that_cannot_unsubscribe_skips_contact_check(setup, category, headers, settled):
    ctx, gmail, account = setup
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "bob@agency.io", category, RuleSource.MANUAL)
        if settled:
            s.add(
                Unsubscribe(
                    account=account.email,
                    list_key="agency.io",
                    sender="bob@agency.io",
                    gmail_id="0",
                    method=UnsubscribeMethod.MAILTO,
                    status=UnsubscribeStatus.OK,
                )
            )
        s.commit()
    gmail.emails = {"9": pitch_email("9", **headers)}
    triage.run_account(ctx, account, Mode.ACT)
    assert gmail.written_queries == []
    assert gmail.modified[-1][2] == ["INBOX"]


@pytest.mark.parametrize(
    ("mode", "act_categories"),
    [
        (Mode.LABEL, None),
        (Mode.DRY_RUN, None),
        (Mode.ACT, ["marketing"]),  # cold_outreach not acted on
    ],
)
def test_rule_hit_that_will_not_act_skips_contact_check(setup, mode, act_categories):
    ctx, gmail, account = setup
    account.act_categories = act_categories
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "bob@agency.io", "cold_outreach", RuleSource.MANUAL)
        s.commit()
    gmail.emails = {"9": pitch_email("9")}
    triage.run_account(ctx, account, mode)
    assert gmail.written_queries == []  # no has_written_to calls
    assert gmail.sent == []


def test_rule_hit_that_unsubscribes_reads_the_list_row_once(setup):
    # The sent-mail check on the rule path and the unsubscribe share one read of the list's row.
    ctx, gmail, account = setup
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "@agency.io", "cold_outreach", RuleSource.MANUAL)
        s.commit()
    gmail.emails = {"9": pitch_email("9")}
    with selects_from(ctx.sessions.kw["bind"], "unsubscribes") as reads:
        triage.run_account(ctx, account, Mode.ACT)
    assert gmail.written_queries == ["bob@agency.io", "agency.io"]
    assert gmail.sent == ["u@agency.io"] and len(reads) == 1
