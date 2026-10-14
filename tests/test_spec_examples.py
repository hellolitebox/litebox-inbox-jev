"""Worked examples from docs/how-it-decides.md, section 5, run against the real code.

Each `test_exNN_*` matches the doc's `EX-NN` heading. `test_doc_examples_match_tests` fails when
an ID exists on only one side, so the spec and these tests can't drift apart.
"""

import base64
import re
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from conftest import make_signals
from fakes import (
    GMAIL_PASS,
    FakeClassifier,
    FakeGmail,
    HttpError,
    JevDown,
    JevFails,
    SentMailService,
    age_errors,
    email,
    gmail_factory,
    jev_http_error,
    sent_mail_gmail,
)
from pydantic import ValidationError
from typesafe_sdk import (
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeUnprocessableEntityError,
)

from inbox_jev import digest, rules, triage
from inbox_jev import unsubscribe as unsub
from inbox_jev.classification import Classification
from inbox_jev.classifier import build_state
from inbox_jev.config import Account, DigestLanguage, Mode, Settings
from inbox_jev.db import (
    MAX_ERROR_ATTEMPTS,
    Message,
    Run,
    SenderRule,
    Unsubscribe,
)
from inbox_jev.gmail import Email, extract_body
from inbox_jev.policy import decide, plan
from inbox_jev.rules import RuleSource
from inbox_jev.signals import extract, resolve_relay

DOC = Path(__file__).resolve().parents[1] / "docs" / "how-it-decides.md"


def answer(category, needs_reply=0.05, **probabilities):
    """A Jev answer whose confidence is the top category's probability."""
    probabilities.setdefault(category, max(probabilities.values(), default=0.97))
    return Classification(
        category=category,
        confidence=probabilities[category],
        probabilities=probabilities,
        needs_reply=needs_reply,
    )


@pytest.fixture
def account():
    return Account(email="me@example.com", group_domains=["example.com"])


@pytest.fixture
def spec(cfg, account, make_sessions):
    """End-to-end triage with fake Gmail and a fake Jev, as the `setup` fixture (conftest.py)."""
    gmail = FakeGmail([])
    gmail.written = frozenset()
    settings = Settings(_env_file=None, typesafe_api_key="x", database_url="sqlite:///:memory:")
    sessions = make_sessions(settings.database_url)
    ctx = triage.Context(
        settings, cfg, sessions, gmail_factory(gmail), classifier=FakeClassifier({})
    )
    return ctx, gmail, account


def _row(ctx, gmail_id):
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id=gmail_id).one()
        s.expunge(row)
        return row


def _one_click_posts(monkeypatch):
    """Route one-click POSTs to a mock transport (no network, no DNS) and record them."""
    posts = []
    real = unsub.unsubscribe

    def handler(request):
        # The POST goes to the IP the guard validated; Host carries the URL's hostname.
        url = f"https://{request.headers['host']}{request.url.raw_path.decode()}"
        posts.append((url, request.content.decode()))
        return httpx.Response(200)

    def fake(sig, send_mail):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return real(sig, send_mail, client=client, url_guard=lambda url: ["93.184.216.34"])

    monkeypatch.setattr(triage.unsub, "unsubscribe", fake)
    return posts


# --- EX-01 -----------------------------------------------------------------------------------


def test_ex01_tool_digest_split_is_archived_by_the_group(cfg, account, make_sessions):
    """EX-01 (docs/how-it-decides.md §5, §3.2 archive group)."""
    sig = make_signals(sender="digest@analytics.example", sender_domain="analytics.example")
    jev = answer("notification", notification=0.55, newsletter=0.42, marketing=0.02, important=0.01)
    d = decide(cfg, account, sig, jev, previous_contact="never")
    assert (d.category, d.archive, d.archive_group) == ("notification", True, True)
    assert not (d.low_confidence or d.unsubscribe or d.spam)
    assert d.add_labels == ["Jev/Notifications"]
    p = plan(cfg, d, Mode.ACT)
    assert p.archived and p.remove_labels == ["INBOX"] and not p.unsubscribe

    # A group archive never earns a rule. Pinned at a confidence that would otherwise count
    # (0.96 >= auto_rule.min_confidence 0.95): notification's account threshold of 0.99 sends it
    # through the group (floor 0.99, archive categories 0.995).
    strict = account.model_copy(update={"min_confidence": {"notification": 0.99}})
    sure = answer("notification", notification=0.96, newsletter=0.035)
    d_sure = decide(cfg, strict, sig, sure, previous_contact="never")
    assert d_sure.archive_group and sure.confidence >= cfg.auto_rule.min_confidence
    sessions = make_sessions("sqlite:///:memory:")
    with sessions() as s:
        rules.record_auto(s, cfg, account.email, sig.sender, jev, d)
        rules.record_auto(s, cfg, account.email, sig.sender, sure, d_sure)
        assert s.query(SenderRule).count() == 0

    narrowed = account.model_copy(update={"act_categories": ["notification"]})
    d = decide(cfg, narrowed, sig, jev, previous_contact="never")
    assert d.low_confidence and "Jev/Review" in d.add_labels and not d.archive


# --- EX-02 -----------------------------------------------------------------------------------


def test_ex02_payment_approval_stays_with_action_label(spec):
    """EX-02 (docs/how-it-decides.md §5, §3.1 keep categories)."""
    ctx, gmail, account = spec
    gmail.emails = {"1": email("1", "Bank <alerts@bank.example>", "Approve payment to Acme")}
    ctx.classifier.answers["Approve payment to Acme"] = answer(
        "action_required",
        needs_reply=0.85,
        action_required=0.93,
        transactional=0.05,
        important=0.02,
    )
    triage.run_account(ctx, account, Mode.ACT)
    (_, add, remove) = gmail.modified[-1]
    assert remove == []
    assert {"Label_Jev/Action", "Label_Jev/Needs Reply"} <= set(add)
    row = _row(ctx, "1")
    assert (row.category, row.archived, row.low_confidence) == ("action_required", False, False)
    with ctx.sessions() as s:
        assert s.query(SenderRule).count() == 0  # 0.93 < 0.95 and a keep signal
        text = digest.build(s, ctx.cfg, account.email, language=DigestLanguage.ES)
    assert "Jev/Action (1)" in text and text.count("Approve payment to Acme") == 1


# --- EX-03 -----------------------------------------------------------------------------------


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


STUB = (
    "Your mail client does not support HTML messages. Copy and paste this URL into your "
    "browser: https://track.esp.example/v/" + "a1b2c3" * 40
)
EVENT_HTML = (
    "<html><head><style>p{color:red}</style></head><body>"
    "<h1>Annual Industry Summit</h1>"
    "<p>Join us on 12 November for a full day of talks on logistics, payments and AI in "
    "operations. The agenda includes a keynote on supply chains, three panels with operators "
    "from the region, and a networking lunch. Speakers: product leads, founders and analysts. "
    "Seats are limited; registration closes on 5 November. The venue is the convention centre "
    "downtown, with parking available on site.</p></body></html>"
)


def _alternative(plain: str, html: str) -> dict:
    return {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64(plain)}},
            {"mimeType": "text/html", "body": {"data": _b64(html)}},
        ],
    }


def test_ex03_stub_plain_part_is_replaced_by_the_html(cfg, account):
    """EX-03 (docs/how-it-decides.md §5, §4.6 body extraction)."""
    body = extract_body(_alternative(STUB, EVENT_HTML))
    assert body.startswith("Annual Industry Summit")
    assert "does not support HTML" not in body and "p{color" not in body

    headers = {
        "from": ["Events <events@summit.example>"],
        "subject": ["Annual Industry Summit"],
        "authentication-results": [GMAIL_PASS],
        "list-unsubscribe": ["<mailto:leave@esp.example>"],
    }
    mail = Email("1", "t1", ["INBOX"], headers, "snippet", body, 1_700_000_000_000)
    state = build_state(mail, account.email, 3000, "never")
    assert state["body"].startswith("Annual Industry Summit") and state["is_mailing_list"]

    d = decide(
        cfg, account, extract(headers), answer("newsletter", newsletter=0.91, marketing=0.09)
    )
    assert (d.category, d.archive, d.archive_group, d.unsubscribe) == (
        "newsletter",
        True,
        False,
        False,
    )

    reply = "Sounds good, see you Monday."
    similar = "<p>Sounds good, see you Monday.</p><p>Sent from my phone</p>"
    assert extract_body(_alternative(reply, similar)) == reply  # a real short reply is kept


# --- EX-04 / EX-05 ---------------------------------------------------------------------------


def _sequence_email(id="1"):
    return email(
        id,
        "Sam <sam@growthagency.example>",
        "Re: quick question about your pipeline",
        list_unsubscribe=(
            "<https://unsub.sequencer.example/u/abc>, <mailto:unsub@growthagency.example>"
        ),
        list_unsubscribe_post="List-Unsubscribe=One-Click",
    )


def _run_sequence(spec, monkeypatch, written=frozenset()):
    ctx, gmail, account = spec
    gmail.written = written
    gmail.emails = {"1": _sequence_email()}
    ctx.classifier.answers["Re: quick question about your pipeline"] = answer(
        "cold_outreach", cold_outreach=0.96, marketing=0.04
    )
    posts = _one_click_posts(monkeypatch)
    triage.run_account(ctx, account, Mode.ACT)
    return ctx, gmail, posts


def test_ex04_stranger_sequence_is_archived_and_unsubscribed(spec, monkeypatch):
    """EX-04 (docs/how-it-decides.md §5, §3.3 unsubscribe conditions, §4.3 one-click)."""
    ctx, gmail, posts = _run_sequence(spec, monkeypatch)
    assert gmail.written_queries == ["sam@growthagency.example", "growthagency.example"]
    assert ctx.classifier.states[0]["previous_contact"] == "never"
    assert gmail.modified[-1][2] == ["INBOX"]
    assert "Label_Jev/Outreach" in gmail.modified[-1][1]
    assert posts == [("https://unsub.sequencer.example/u/abc", "List-Unsubscribe=One-Click")]
    assert gmail.sent == []  # the mailto isn't needed
    with ctx.sessions() as s:
        row = s.query(Unsubscribe).one()
        assert (row.list_key, row.method, row.status) == (
            "growthagency.example",
            "one_click",
            "ok",
        )


def test_ex05_known_contact_sequence_is_archived_only(spec, monkeypatch):
    """EX-05 (docs/how-it-decides.md §5, §3.3 condition 5, §4.1 previous_contact)."""
    ctx, gmail, posts = _run_sequence(
        spec, monkeypatch, written=frozenset({"sam@growthagency.example"})
    )
    assert ctx.classifier.states[0]["previous_contact"] == "wrote_to_sender"
    assert gmail.modified[-1][2] == ["INBOX"]
    assert posts == [] and gmail.sent == []
    with ctx.sessions() as s:
        assert s.query(Unsubscribe).count() == 0
        assert s.query(SenderRule).count() == 0  # known_contact_kept never earns a rule

    for contact in ("wrote_to_sender", "wrote_to_their_domain"):
        d = decide(
            ctx.cfg,
            spec[2],
            extract(_sequence_email().headers),
            answer("cold_outreach", cold_outreach=0.96),
            previous_contact=contact,
        )
        assert d.archive and d.known_contact_kept and not d.unsubscribe


# --- EX-06 -----------------------------------------------------------------------------------


def test_ex06_pitch_via_own_group_uses_author_and_never_unsubscribes(spec, monkeypatch):
    """EX-06 (docs/how-it-decides.md §5, §3.4 groups, §4.5 X-Original-Sender trust)."""
    ctx, gmail, account = spec
    account.allow_domains = ["example.com"]
    gmail.emails = {
        "1": email(
            "1",
            "\"'Dana' via Hello\" <hello@example.com>",
            "Partnership idea",
            x_original_sender="dana@pitchco.example",
            mailing_list="list hello@example.com; contact hello+owners@example.com",
            list_id="<hello.example.com>",
            list_post="<mailto:hello@example.com>",
            list_unsubscribe="<mailto:googlegroups-manage+1+unsubscribe@googlegroups.com>",
            authentication_results="mx.google.com; dmarc=pass header.from=example.com",
        )
    }
    ctx.classifier.answers["Partnership idea"] = answer("cold_outreach", cold_outreach=0.97)
    posts = _one_click_posts(monkeypatch)
    triage.run_account(ctx, account, Mode.ACT)

    state = ctx.classifier.states[0]  # allow_domains matched the author, not the group
    assert (state["original_sender"], state["sent_via_group"]) == (
        "dana@pitchco.example",
        "hello@example.com",
    )
    assert gmail.written_queries[0] == "dana@pitchco.example"
    assert gmail.modified[-1][2] == ["INBOX"]
    assert posts == [] and gmail.sent == []
    row = _row(ctx, "1")
    assert (row.sender, row.category, row.archived) == (
        "dana@pitchco.example",
        "cold_outreach",
        True,
    )
    with ctx.sessions() as s:
        assert s.query(Unsubscribe).count() == 0
        assert {r.sender_key for r in s.query(SenderRule)} == {"dana@pitchco.example"}


# --- EX-07 / EX-08 ---------------------------------------------------------------------------


def _group_notice_signals():
    return extract(
        {
            "from": ["Hello <hello@example.com>"],
            "authentication-results": ["mx.google.com; dmarc=pass header.from=example.com"],
            "list-id": ["<hello.example.com>"],
        }
    )


def test_ex07_held_spam_report_is_archived_as_notification(cfg, account):
    """EX-07 (docs/how-it-decides.md §5, §3.1 categories)."""
    assert "held-spam reports" in cfg.by_key["notification"].criteria
    assert "held-spam reports" in cfg.by_key["action_required"].criteria  # as an exclusion
    sig = _group_notice_signals()
    assert sig.sender == "hello@example.com" and sig.relayed_by is None
    d = decide(cfg, account, sig, answer("notification", notification=0.90, action_required=0.06))
    assert (d.category, d.archive, d.unsubscribe, d.low_confidence) == (
        "notification",
        True,
        False,
        False,
    )
    allowlisted = account.model_copy(update={"allow_domains": ["example.com"]})
    assert decide(cfg, allowlisted, sig).source == "allowlist"


def test_ex08_pending_approval_notice_stays_actionable(cfg, account):
    """EX-08 (docs/how-it-decides.md §5, §3.1 categories)."""
    assert "pending my approval" in cfg.by_key["action_required"].criteria
    d = decide(
        cfg,
        account,
        _group_notice_signals(),
        answer("action_required", needs_reply=0.75, action_required=0.96, notification=0.03),
    )
    assert (d.category, d.archive) == ("action_required", False)
    assert d.add_labels == ["Jev/Action", "Jev/Needs Reply"]
    assert plan(cfg, d, Mode.ACT).remove_labels == []


# --- EX-09 / EX-10 ---------------------------------------------------------------------------


def _consultation_pitch():
    return answer(
        "cold_outreach",
        needs_reply=0.79,
        cold_outreach=0.67,
        marketing=0.31,
        important=0.0,
        action_required=0.0,
    )


def test_ex09_mass_pitch_expecting_reply_is_group_archived(cfg, account):
    """EX-09 (docs/how-it-decides.md §5, §3.2 archive group, needs-reply veto)."""
    sig = make_signals(sender="offers@bulkmail.example", sender_domain="bulkmail.example")
    for contact in ("never", None):
        d = decide(cfg, account, sig, _consultation_pitch(), previous_contact=contact)
        assert (d.category, d.archive, d.archive_group) == ("cold_outreach", True, True)
        assert not d.unsubscribe and not d.low_confidence
        assert d.add_labels == ["Jev/Outreach"]  # no Review, no Needs Reply


def test_ex10_known_vendor_follow_up_stays_in_review(cfg, account):
    """EX-10 (docs/how-it-decides.md §5, §3.2 archive group, needs-reply veto)."""
    sig = make_signals(sender="rep@vendor.example", sender_domain="vendor.example")
    d = decide(cfg, account, sig, _consultation_pitch(), previous_contact="wrote_to_their_domain")
    assert not d.archive and d.low_confidence
    assert d.add_labels == ["Jev/Review", "Jev/Outreach", "Jev/Needs Reply"]

    keep_signal = answer(
        "cold_outreach", needs_reply=0.79, cold_outreach=0.64, marketing=0.31, important=0.05
    )
    d = decide(cfg, account, sig, keep_signal, previous_contact="never")
    assert not d.archive and d.low_confidence  # a stranger with keep probability ≥ 0.05


# --- EX-11 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "signals_kw", [{"unsubscribe_https": [], "unsubscribe_mailto": []}, {"dmarc_pass": False}]
)
def test_ex11_unverified_cold_outreach_is_archived_never_unsubscribed(
    cfg, account, signals_kw, make_sessions
):
    """EX-11 (docs/how-it-decides.md §5, §3.3 condition 3, §3.5 auto rules)."""
    sig = make_signals(
        sender="founder@devshop.example", sender_domain="devshop.example", **signals_kw
    )
    jev = answer("cold_outreach", cold_outreach=0.97)
    d = decide(cfg, account, sig, jev, previous_contact="never")
    assert (d.category, d.archive, d.spam, d.unsubscribe) == ("cold_outreach", True, False, False)
    assert d.downgraded_to_spam
    p = plan(cfg, d, Mode.ACT)
    assert p.archived and not p.spammed and "SPAM" not in p.add_labels

    sessions = make_sessions("sqlite:///:memory:")
    with sessions() as s:
        rules.record_auto(s, cfg, account.email, sig.sender, jev, d)
        assert s.query(SenderRule).one().hits == 1  # still counts toward an auto rule

    # A known contact: the unverified check stopped the unsubscribe, so known_contact_kept
    # isn't set (same order as the rule path), but the answer still doesn't count toward an
    # auto rule: known contacts never earn a cold_outreach rule.
    d = decide(cfg, account, sig, jev, previous_contact="wrote_to_sender")
    assert d.archive and d.downgraded_to_spam and not d.known_contact_kept and not d.unsubscribe
    with sessions() as s:
        s.query(SenderRule).delete()
        rules.record_auto(s, cfg, account.email, sig.sender, jev, d, "wrote_to_sender")
        assert s.query(SenderRule).count() == 0

    # A List-Unsubscribe with only http:// URIs counts as none: unverified the same way.
    http_only = extract(
        {
            "from": ["Founder <founder@devshop.example>"],
            "authentication-results": [GMAIL_PASS],
            "list-unsubscribe": ["<http://devshop.example/unsubscribe?u=1>"],
        }
    )
    assert not http_only.has_unsubscribe
    d = decide(cfg, account, http_only, jev, previous_contact="never")
    assert d.archive and d.downgraded_to_spam and not d.unsubscribe


# --- EX-12 -----------------------------------------------------------------------------------


def test_ex12_pulling_back_to_inbox_becomes_a_rule(spec):
    """EX-12 (docs/how-it-decides.md §5, §3.7 corrections)."""
    ctx, gmail, account = spec
    gmail.emails = {"1": email("1", "Weekly <news@weekly.example>", "Issue 41")}
    ctx.classifier.answers["Issue 41"] = answer("newsletter", newsletter=0.97)
    triage.run_account(ctx, account, Mode.ACT)
    assert gmail.modified[-1][2] == ["INBOX"]

    gmail.emails["1"].label_ids.append("INBOX")  # the user moves it back
    gmail.emails["2"] = email("2", "Weekly <news@weekly.example>", "Issue 42")
    ctx.classifier.answers["Issue 42"] = answer("newsletter", newsletter=0.97)
    states = len(ctx.classifier.states)
    triage.run_account(ctx, account, Mode.ACT)

    assert _row(ctx, "1").corrected_category == "important"
    with ctx.sessions() as s:
        rule = s.query(SenderRule).filter_by(sender_key="news@weekly.example").one()
        assert (rule.category, rule.source, rule.active) == ("important", "correction", True)
    # A keep rule decided issue 42: Jev was asked only for the flags, its newsletter answer
    # was ignored.
    assert [st["subject"] for st in ctx.classifier.states[states:]] == ["Issue 42"]
    row = _row(ctx, "2")
    assert (row.category, row.source, row.archived) == ("important", "rule", False)
    assert "Label_Jev/Important" in gmail.modified[-1][1] and gmail.modified[-1][2] == []


def test_ex13_failing_email_backs_off_then_gives_up(spec):
    """EX-13 (docs/how-it-decides.md §5, §1 the pass over an inbox)."""
    ctx, gmail, account = spec
    gmail.emails = {
        "1": email("1", "Vendor <billing@vendor.example>", "Invoice 7"),
        "2": email("2", "Weekly <news@weekly.example>", "Issue 41"),
    }
    ctx.classifier.answers["Issue 41"] = answer("newsletter", newsletter=0.97)  # no "Invoice 7"

    def later(minutes):
        with ctx.sessions() as s:
            row = s.query(Message).filter_by(gmail_id="1").one()
            row.processed_at = row.processed_at - timedelta(minutes=minutes)
            s.commit()

    run = triage.run_account(ctx, account, Mode.ACT)
    assert (run.processed, run.errors) == (1, 1)  # the run carries on
    assert (_row(ctx, "1").status, _row(ctx, "1").error_attempts) == ("error", 1)
    assert triage.run_account(ctx, account, Mode.ACT).errors == 0  # < 5 minutes: not retried

    for wait in (5, 10, 20, 40, 80, 160, 320):
        later(wait)
        assert triage.run_account(ctx, account, Mode.ACT).errors == 1
    assert _row(ctx, "1").error_attempts == 8

    later(60 * 24)
    calls = ctx.classifier.calls
    assert triage.run_account(ctx, account, Mode.ACT).errors == 0  # gave up
    assert ctx.classifier.calls == calls
    assert not [m for m in gmail.modified if m[0] == "1"]  # untouched in Gmail
    with ctx.sessions() as s:
        text = digest.build(s, ctx.cfg, account.email, hours=48, language=DigestLanguage.ES)
    assert "Invoice 7" in text.split("Sin más reintentos")[1]

    ctx.classifier.answers["Invoice 7"] = answer("transactional", transactional=0.95)
    with ctx.sessions() as s:
        triage.reset_error_attempts(s, account.email)
    triage.run_account(ctx, account, Mode.ACT)
    row = _row(ctx, "1")
    assert (row.status, row.category, row.error_attempts) == ("done", "transactional", 0)


# --- EX-14 -----------------------------------------------------------------------------------


def test_ex14_rule_to_removed_category_goes_through_jev(spec, caplog):
    """EX-14 (docs/how-it-decides.md §5, §3.5 sender rules)."""
    ctx, gmail, account = spec
    gmail.emails = {
        "1": email("1", "Deals <sale@deals.example>", "Weekend picks"),
        "2": email("2", "Deals <sale@deals.example>", "Monday picks"),
    }
    for subject in ("Weekend picks", "Monday picks"):
        ctx.classifier.answers[subject] = answer("newsletter", newsletter=0.93, marketing=0.07)
    gmail.emails["3"] = email("3", "Billing <billing@vendor.example>", "Invoice 88")
    ctx.classifier.answers["Invoice 88"] = answer("important", important=0.96, marketing=0.04)
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "@deals.example", "promotions", RuleSource.MANUAL)
        rules.set_rule(s, account.email, "@vendor.example", "marketing", RuleSource.MANUAL)
        rules.set_rule(s, account.email, "billing@vendor.example", "promotions", RuleSource.MANUAL)
        s.commit()
    run = triage.run_account(ctx, account, Mode.ACT)

    assert (run.processed, run.errors) == (3, 0)
    assert ctx.classifier.calls == 3
    for gmail_id in ("1", "2"):
        row = _row(ctx, gmail_id)
        assert (row.source, row.category, row.archived) == ("jev", "newsletter", True)
    # The stale exact rule doesn't hand billing@ to the @vendor.example rule it overrode.
    row = _row(ctx, "3")
    assert (row.source, row.category, row.archived) == ("jev", "important", False)
    warned = [
        r for r in caplog.records if r.getMessage() == "sender rule ignored: unknown category"
    ]
    # once per stale rule and run, not once per email
    assert sorted(r.sender_key for r in warned) == ["@deals.example", "billing@vendor.example"]
    with ctx.sessions() as s:
        rule = s.query(SenderRule).filter_by(sender_key="@deals.example").one()
        assert (rule.category, rule.active) == ("promotions", True)  # left for you to delete


# --- EX-15 -----------------------------------------------------------------------------------


def test_ex15_keep_rule_still_gets_needs_reply(spec):
    """EX-15 (docs/how-it-decides.md §5, §3.1 extra labels, §3.5 sender rules)."""
    ctx, gmail, account = spec
    gmail.emails = {
        "1": email("1", "Ana <ana@client.example>", "Signed contract by Friday?"),
        "2": email("2", "Weekly <news@weekly.example>", "Issue 43"),
    }
    ctx.classifier.answers["Signed contract by Friday?"] = answer(
        "cold_outreach", needs_reply=0.92, cold_outreach=0.55, important=0.45
    )
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "ana@client.example", "important", RuleSource.CORRECTION)
        rules.set_rule(s, account.email, "news@weekly.example", "newsletter", RuleSource.AUTO)
        s.commit()
    triage.run_account(ctx, account, Mode.ACT)

    assert [st["subject"] for st in ctx.classifier.states] == ["Signed contract by Friday?"]
    assert "previous_contact" not in ctx.classifier.states[0]
    assert gmail.written_queries == []  # no sent-mail search for a keep decision
    mods = {m[0]: m for m in gmail.modified}
    assert {"Label_Jev/Important", "Label_Jev/Needs Reply"} <= set(mods["1"][1])
    assert "Label_Jev/Outreach" not in mods["1"][1] and mods["1"][2] == []
    assert mods["2"][2] == ["INBOX"]  # the newsletter rule still skips Jev
    row = _row(ctx, "1")
    assert (row.source, row.category, row.confidence) == ("rule", "important", 1.0)
    with ctx.sessions() as s:
        assert s.query(SenderRule).count() == 2  # the answer counted toward nothing
        text = digest.build(s, ctx.cfg, account.email, language=DigestLanguage.ES)
    assert "Signed contract by Friday?" in text.split("Piden respuesta")[1]


# --- EX-19 -----------------------------------------------------------------------------------


def test_ex19_flags_only_jev_failure_still_labels(spec, caplog):
    """EX-19 (docs/how-it-decides.md §5, §3.5 sender rules)."""
    ctx, gmail, account = spec
    account.allow_domains = ["acme.example"]
    gmail.emails = {
        "1": email("1", "Lea <lea@acme.example>", "Budget review"),
        "2": email("2", "Ana <ana@client.example>", "Signed contract by Friday?"),
    }
    ctx = triage.Context(ctx.settings, ctx.cfg, ctx.sessions, ctx.gmail, classifier=JevDown({}))
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "ana@client.example", "important", RuleSource.CORRECTION)
        s.commit()
    run = triage.run_account(ctx, account, Mode.ACT)

    assert (run.processed, run.errors) == (2, 0) and ctx.classifier.calls == 2
    mods = {m[0]: m for m in gmail.modified}
    for gmail_id, source in (("1", "allowlist"), ("2", "rule")):
        row = _row(ctx, gmail_id)
        assert (row.status, row.source, row.category) == ("done", source, "important")
        assert row.needs_reply is None and row.tags is None
        assert "Label_Jev/Important" in mods[gmail_id][1] and mods[gmail_id][2] == []
        assert "Label_Jev/Needs Reply" not in mods[gmail_id][1]
    warned = [r for r in caplog.records if r.getMessage() == "flags-only Jev call failed"]
    assert sorted(r.id for r in warned) == ["1", "2"]
    for r in warned:
        assert not {"Budget review", "Signed contract by Friday?"} & {
            str(v) for v in vars(r).values()
        }


# --- EX-16 -----------------------------------------------------------------------------------


def test_ex16_archive_group_respects_category_thresholds(cfg, account):
    """EX-16 (docs/how-it-decides.md §5, §3.2 archive group condition 5)."""
    cfg.by_key["marketing"].min_confidence = 0.95
    sig = make_signals(sender="hello@brand.example", sender_domain="brand.example")
    jev = answer("newsletter", newsletter=0.50, marketing=0.45, important=0.01)
    d = decide(cfg, account, sig, jev, previous_contact="never")
    assert d.low_confidence and not d.archive and not d.archive_group
    assert d.add_labels == ["Jev/Review", "Jev/Newsletters"]

    lenient = account.model_copy(update={"min_confidence": {"marketing": 0.90}})
    d = decide(cfg, lenient, sig, jev, previous_contact="never")
    assert d.archive and d.archive_group and not d.unsubscribe  # the account override wins


# --- EX-17 -----------------------------------------------------------------------------------


def test_ex17_no_gmail_authentication_results_is_unverified(spec, monkeypatch):
    """EX-17 (docs/how-it-decides.md §5, §4.2 DMARC, §3.3 condition 3)."""
    ctx, gmail, account = spec
    gmail.emails = {
        "1": email(
            "1",
            "Shop <promo@shop.example>",
            "Flash sale",
            authentication_results="mail.shop.example; dmarc=pass header.from=shop.example",
            list_unsubscribe="<https://shop.example/u/1>",
            list_unsubscribe_post="List-Unsubscribe=One-Click",
        )
    }
    ctx.classifier.answers["Flash sale"] = answer("marketing", marketing=0.97)
    posts = _one_click_posts(monkeypatch)
    triage.run_account(ctx, account, Mode.ACT)

    assert gmail.modified[-1][2] == ["INBOX"] and "SPAM" not in gmail.modified[-1][1]
    assert posts == [] and gmail.sent == []
    row = _row(ctx, "1")
    assert (row.category, row.archived, row.downgraded_to_spam) == ("marketing", True, True)
    with ctx.sessions() as s:
        assert s.query(Unsubscribe).count() == 0


# --- EX-18 -----------------------------------------------------------------------------------


def test_ex18_dry_run_learns_no_rules(spec):
    """EX-18 (docs/how-it-decides.md §5, §3.5 automatic rules, §3.6 modes)."""
    ctx, gmail, account = spec
    gmail.emails = {
        str(n): email(str(n), "Weekly <news@weekly.example>", f"Issue {n}") for n in (1, 2, 3)
    }
    for n in (1, 2, 3):
        ctx.classifier.answers[f"Issue {n}"] = answer("newsletter", newsletter=0.97)
    triage.run_account(ctx, account, Mode.DRY_RUN)
    with ctx.sessions() as s:
        assert s.query(SenderRule).count() == 0
    assert all(add == [] and remove == [] for _, add, remove in gmail.modified)

    triage.run_account(ctx, account, Mode.ACT)  # the dry-run rows are processed again
    assert ctx.classifier.calls == 6
    with ctx.sessions() as s:
        rule = s.query(SenderRule).one()
        assert (rule.category, rule.hits, rule.active) == ("newsletter", 3, True)


# --- EX-20 -----------------------------------------------------------------------------------


def test_ex20_restore_skips_emails_deleted_in_gmail(spec, monkeypatch):
    """EX-20 (docs/how-it-decides.md §5, §3.7 restore)."""
    ctx, gmail, account = spec
    gmail.emails = {
        "1": email("1", "Weekly <news@weekly.example>", "Issue 42"),
        "2": email("2", "Shop <promo@shop.example>", "Sale"),
    }
    ctx.classifier.answers.update({"Issue 42": answer("newsletter"), "Sale": answer("marketing")})
    triage.run_account(ctx, account, Mode.ACT)
    assert _row(ctx, "1").archived and _row(ctx, "2").archived

    real = gmail.modify

    def modify(message_id, add, remove):
        if message_id == "1":
            raise HttpError(404)  # deleted in Gmail
        real(message_id, add, remove)

    monkeypatch.setattr(gmail, "modify", modify)
    for again in (False, True):
        with ctx.sessions() as s:
            result = triage.restore(s, gmail, account.email, timedelta(days=1))
        assert result == triage.RestoreResult(restored=0 if again else 1, deleted=1)
    sale = gmail.emails["2"].label_ids
    assert "INBOX" in sale and not [i for i in sale if i.startswith("Label_")]
    assert _row(ctx, "2").restored
    assert not _row(ctx, "1").restored and _row(ctx, "1").archived


# --- EX-21 -----------------------------------------------------------------------------------


def test_ex21_revoked_jev_key_stops_jev_but_not_rules(spec):
    """EX-21 (docs/how-it-decides.md §5, §1 the pass over an inbox)."""
    ctx, gmail, account = spec
    account.allow_domains = ["acme.example"]
    gmail.emails = {
        "1": email("1", "Sam <sam@growthagency.example>", "Quick question"),
        "2": email("2", "Lea <lea@acme.example>", "Budget review"),
        "3": email("3", "Weekly <news@weekly.example>", "Issue 41"),
        "4": email("4", "Ana <ana@client.example>", "Signed contract by Friday?"),
    }
    jev = JevFails(jev_http_error(TypeSafeAuthenticationError, 401, {"error": "invalid key"}))
    ctx = triage.Context(ctx.settings, ctx.cfg, ctx.sessions, ctx.gmail, classifier=jev)
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "news@weekly.example", "newsletter", RuleSource.MANUAL)
        s.commit()
    with pytest.raises(triage.JevUnavailable):
        triage.run_account(ctx, account, Mode.ACT)

    assert jev.calls == 1  # Sam's email; nothing more is sent to Jev in this pass
    with ctx.sessions() as s:
        assert {m.gmail_id for m in s.query(Message)} == {"2", "3"}  # 1 and 4 wait
        run = s.query(Run).one()
    assert (run.processed, run.errors) == (2, 0)
    assert run.error.startswith("Jev rejected the API key or model (TypeSafeAuthenticationError")
    lea, issue = _row(ctx, "2"), _row(ctx, "3")
    assert (lea.source, lea.category, lea.needs_reply) == ("allowlist", "important", None)
    assert (issue.source, issue.archived) == ("rule", True)
    with ctx.sessions() as s:
        text = digest.build(s, ctx.cfg, account.email, language=DigestLanguage.ES)
    assert "1 corrida fallida" in text and "TYPESAFE_API_KEY" in text

    jev.error = None  # the key is fixed: the next run decides the emails that waited
    jev.answers.update(
        {
            "Quick question": answer("cold_outreach", cold_outreach=0.97),
            "Signed contract by Friday?": answer("important", needs_reply=0.9),
        }
    )
    run = triage.run_account(ctx, account, Mode.ACT)
    assert (run.processed, run.errors, run.error) == (2, 0, None)
    assert _row(ctx, "1").category == "cold_outreach" and _row(ctx, "1").error_attempts == 0


# --- EX-22 -----------------------------------------------------------------------------------


def test_ex22_jev_down_stops_after_three_failures(spec):
    """EX-22 (docs/how-it-decides.md §5, §1 the pass over an inbox)."""
    ctx, gmail, account = spec
    gmail.emails = {
        str(i): email(str(i), f"Sender {i} <s{i}@vendor{i}.example>", f"Update {i}")
        for i in range(1, 6)
    }
    jev = JevFails(TypeSafeAPITimeoutError(30.0))
    ctx = triage.Context(ctx.settings, ctx.cfg, ctx.sessions, ctx.gmail, classifier=jev)
    with pytest.raises(triage.JevUnavailable):
        triage.run_account(ctx, account, Mode.ACT)

    assert jev.calls == 3
    with ctx.sessions() as s:
        rows = {m.gmail_id: m.error_attempts for m in s.query(Message)}
        run = s.query(Run).one()
    assert rows == {"1": 1, "2": 1, "3": 1}  # 4 and 5: never sent, not stored
    assert (run.processed, run.errors) == (0, 3)
    assert run.error == (
        "Jev unavailable: 3 calls in a row failed (last: TypeSafeAPITimeoutError: Request timed "
        "out (timeout=30.0).); emails that need Jev wait for the next run"
    )

    jev.error = None  # Jev is back on the next pass
    jev.answers.update({f"Update {i}": answer("notification") for i in range(1, 6)})
    run = triage.run_account(ctx, account, Mode.ACT)
    assert (run.processed, run.error) == (2, None)  # 4 and 5; 1-3 wait for their backoff


# --- EX-23 -----------------------------------------------------------------------------------


def test_ex23_jev_refuses_one_email(spec):
    """EX-23 (docs/how-it-decides.md §5, §1 the pass over an inbox)."""
    ctx, gmail, account = spec
    gmail.emails = {
        "1": email("1", "Vendor <billing@vendor.example>", "Invoice 7"),
        "2": email("2", "Weekly <news@weekly.example>", "Issue 41"),
    }
    refused = jev_http_error(
        TypeSafeUnprocessableEntityError, 422, {"detail": "state.body: 'Invoice 7 ...'"}
    )

    class RefusesInvoice(FakeClassifier):
        def classify(self, state):
            if state["subject"] == "Invoice 7":
                self.calls += 1
                raise refused
            return super().classify(state)

    jev = RefusesInvoice({"Issue 41": answer("newsletter", newsletter=0.97)})
    ctx = triage.Context(ctx.settings, ctx.cfg, ctx.sessions, ctx.gmail, classifier=jev)
    run = triage.run_account(ctx, account, Mode.ACT)

    assert (run.processed, run.errors, run.error) == (1, 1, None)
    row = _row(ctx, "1")
    assert row.error == "TypeSafeUnprocessableEntityError (HTTP 422, request_id=req-1)"
    assert (row.status, row.error_attempts) == ("error", MAX_ERROR_ATTEMPTS)
    calls = jev.calls
    triage.run_account(ctx, account, Mode.ACT)
    assert jev.calls == calls  # never retried by itself
    with ctx.sessions() as s:
        text = digest.build(s, ctx.cfg, account.email, language=DigestLanguage.ES)
    gave_up = text.split("Sin más reintentos")[1]
    assert "#all/1|Invoice 7> — billing@vendor.example" in gave_up  # stored though Jev failed
    assert "TypeSafeUnprocessableEntityError (HTTP 422" in gave_up
    assert "state.body" not in text


# --- EX-24 / EX-25 ---------------------------------------------------------------------------


def _lead_suggestions():
    """The LinkedIn-style digest: archive categories 0.98 together, a reply signal of 0.75."""
    return answer(
        "notification", needs_reply=0.75, notification=0.62, newsletter=0.36, important=0.01
    )


def _sig(sender, **headers):
    raw = {"from": [sender], "authentication-results": [GMAIL_PASS]}
    raw |= {k.replace("_", "-"): [v] for k, v in headers.items()}
    return extract(raw)


def test_ex24_replies_to_notifications_make_no_known_contact(cfg, account, monkeypatch):
    """EX-24 (docs/how-it-decides.md §5, §4.1 previous_contact, §4.8 machine addresses)."""
    replies = {f"s{i}": {"To": f"m-a175x{i}@bounce.social.example"} for i in range(12)}
    svc = SentMailService(replies)
    gmail = sent_mail_gmail(monkeypatch, svc)
    sender = "messages-noreply@social.example"
    contact = triage.previous_contact(gmail, account.email, sender)
    assert contact == "never"
    assert svc.queries == ["in:sent to:social.example"]  # the machine sender isn't searched
    assert len(svc.reads) == 12  # To/Cc/Bcc of each match, no body

    d = decide(
        cfg, account, _sig(f"Social <{sender}>"), _lead_suggestions(), previous_contact=contact
    )
    assert (d.category, d.archive, d.archive_group) == ("notification", True, True)
    assert d.add_labels == ["Jev/Notifications"]  # no Review, no Needs Reply

    # One email to a person there makes the domain known again.
    svc = SentMailService({"p": {"To": "Jane <jane@social.example>"}, **replies})
    gmail = sent_mail_gmail(monkeypatch, svc)
    assert triage.previous_contact(gmail, account.email, sender) == "wrote_to_their_domain"
    assert svc.reads == ["p"]


def test_ex25_no_reply_sender_reply_signal_is_judged_like_a_strangers(cfg, account):
    """EX-25 (docs/how-it-decides.md §5, §3.2 archive group, §4.8 machine addresses)."""
    known = "wrote_to_their_domain"
    sig = _sig("Vendor <noreply@vendor.example>")
    assert sig.no_reply
    d = decide(cfg, account, sig, _lead_suggestions(), previous_contact=known)
    assert (d.archive, d.archive_group) == (True, True)
    assert d.add_labels == ["Jev/Notifications"]

    # A Reply-To to a person: replies reach Ana, so the known-contact veto applies (EX-10).
    sig = _sig("Vendor <noreply@vendor.example>", reply_to="Ana <ana@vendor.example>")
    d = decide(cfg, account, sig, _lead_suggestions(), previous_contact=known)
    assert not d.archive and d.low_confidence
    assert d.add_labels == ["Jev/Review", "Jev/Notifications", "Jev/Needs Reply"]

    # A keep signal still vetoes: the email may need an action, not a reply.
    action = answer(
        "notification",
        needs_reply=0.75,
        notification=0.62,
        transactional=0.32,
        action_required=0.06,
    )
    d = decide(cfg, account, _sig("noreply@vendor.example"), action, previous_contact=known)
    assert not d.archive and d.low_confidence
    assert "Jev/Needs Reply" in d.add_labels


# --- docs <-> tests ----------------------------------------------------------------------------


def test_doc_examples_match_tests():
    """Every EX-NN in docs/how-it-decides.md has a test_exNN_* here, and vice versa."""
    text = DOC.read_text()
    doc_ids = set(re.findall(r"^### EX-(\d{2})\b", text, flags=re.MULTILINE))
    module = sys.modules[__name__]
    tests = {
        name: getattr(module, name) for name in dir(module) if re.match(r"test_ex\d{2}_", name)
    }
    test_ids = {name[len("test_ex") :][:2] for name in tests}
    assert doc_ids, "no EX-NN headings found in the doc"
    only_doc, only_tests = sorted(doc_ids - test_ids), sorted(test_ids - doc_ids)
    assert doc_ids == test_ids, f"only in the doc: {only_doc}; only in tests: {only_tests}"
    for name, func in tests.items():
        assert f"EX-{name[7:9]}" in (func.__doc__ or ""), f"{name} docstring must cite its EX ID"
    mentioned = set(re.findall(r"\bEX-(\d{2})\b", text))
    assert mentioned <= doc_ids, f"referenced but not defined: {sorted(mentioned - doc_ids)}"
    sections = re.split(r"^### (?=EX-\d{2}\b)", text, flags=re.MULTILINE)[1:]
    doc_tests = {}
    for section in sections:
        named = re.search(r"^- \*\*Test\*\*: `(test_ex\d{2}_\w+)`", section, flags=re.MULTILINE)
        assert named, f"{section[:5]} has no **Test**: line"
        doc_tests[section[3:5]] = named.group(1)
    test_names = {name[7:9]: name for name in tests}
    assert doc_tests == test_names, "the doc's **Test**: names don't match the test functions"


# --- EX-26 -----------------------------------------------------------------------------------


def test_ex26_writing_to_a_friend_on_country_free_mail_makes_no_known_contact(spec, monkeypatch):
    """EX-26 (docs/how-it-decides.md §5, §4.7 previous_contact, config.FREE_MAIL)."""
    ctx, gmail, account = spec
    # I wrote to my cousin at hotmail.com.ar: a domain search would say "yes" for anyone there.
    gmail.written = frozenset({"primo@hotmail.com.ar", "hotmail.com.ar"})
    gmail.emails = {
        "1": email(
            "1",
            "Growth <growth.lead@hotmail.com.ar>",
            "Escalá tus ventas",
            list_unsubscribe="<https://unsub.sequencer.example/u/xyz>",
            list_unsubscribe_post="List-Unsubscribe=One-Click",
        )
    }
    ctx.classifier.answers["Escalá tus ventas"] = answer("cold_outreach", cold_outreach=0.96)
    posts = _one_click_posts(monkeypatch)
    triage.run_account(ctx, account, Mode.ACT)

    assert gmail.written_queries == ["growth.lead@hotmail.com.ar"]  # no domain search
    assert ctx.classifier.states[0]["previous_contact"] == "never"
    assert gmail.modified[-1][2] == ["INBOX"]
    assert [url for url, _ in posts] == ["https://unsub.sequencer.example/u/xyz"]
    with ctx.sessions() as s:
        assert s.query(Unsubscribe).one().status == "ok"


# --- EX-27 -----------------------------------------------------------------------------------


def test_ex27_a_public_suffix_in_group_domains_is_refused():
    """EX-27 (docs/how-it-decides.md §5, §3.4 group_domains, ADR 0033)."""
    with pytest.raises(ValidationError, match="'gouv.fr' is a public suffix"):
        Account(email="me@exemple.gouv.fr", group_domains=["gouv.fr"])
    with pytest.raises(ValidationError, match="'com.ai' is a public suffix"):
        Account(email="me@acme.com", group_domains=["com.ai"])
    # .ai is sold as a generic name: only its Public Suffix List names are suffixes there.
    assert Account(email="me@adv.ai", group_domains=["adv.ai"]).group_domains == ["adv.ai"]

    account = Account(email="me@exemple.gouv.fr", group_domains=["exemple.gouv.fr"])
    group = {
        "from": ["\"'Dana' via Info\" <info@autre.gouv.fr>"],
        "x-original-sender": ["dana@pitchco.example"],
        "mailing-list": ["list info@autre.gouv.fr; contact info+owners@autre.gouv.fr"],
        "authentication-results": ["mx.google.com; dmarc=pass header.from=autre.gouv.fr"],
    }
    other = resolve_relay(extract(group), account.group_domains)
    assert (other.sender, other.relayed_by) == ("info@autre.gouv.fr", None)  # not trusted
    own = {k: [v[0].replace("autre", "exemple")] for k, v in group.items()}
    mine = resolve_relay(extract(own), account.group_domains)
    assert (mine.sender, mine.relayed_by) == ("dana@pitchco.example", "info@exemple.gouv.fr")


# --- EX-28 -----------------------------------------------------------------------------------


def test_ex28_jev_refusing_everything_gives_no_email_up(spec):
    """EX-28 (docs/how-it-decides.md §5, §1 the pass over an inbox, ADR 0035)."""
    ctx, gmail, account = spec
    account.allow_domains = ["acme.example"]
    gmail.emails = {
        "1": email("1", "Vendor <billing@vendor.example>", "Invoice 7"),
        "2": email("2", "Sam <sam@growthagency.example>", "Quick question"),
        "3": email("3", "Lea <lea@acme.example>", "Budget review"),
        "4": email("4", "Weekly <news@weekly.example>", "Issue 41"),
    }
    jev = JevFails(jev_http_error(TypeSafeUnprocessableEntityError, 422))
    ctx = triage.Context(ctx.settings, ctx.cfg, ctx.sessions, ctx.gmail, classifier=jev)
    with pytest.raises(triage.JevUnavailable):
        triage.run_account(ctx, account, Mode.ACT)

    assert jev.calls == 2  # Invoice 7 and Sam's pitch; nothing more is sent
    with ctx.sessions() as s:
        assert {m.gmail_id for m in s.query(Message)} == {"1", "2", "3"}  # the issue waits
        run = s.query(Run).one()
    assert (run.processed, run.errors) == (1, 2)
    for refused in ("1", "2"):  # an ordinary failure each, retried after the backoff
        row = _row(ctx, refused)
        assert (row.status, row.error_attempts) == ("error", 1)
        assert row.error == "TypeSafeUnprocessableEntityError (HTTP 422, request_id=req-1)"
    assert run.error == (
        "Jev refused 2 requests in a row the same way (TypeSafeUnprocessableEntityError "
        "(HTTP 422)): likely a change on Jev's side; emails that need Jev wait for the next run"
    )
    lea = _row(ctx, "3")
    assert (lea.source, lea.category, lea.needs_reply) == ("allowlist", "important", None)

    jev.error = None  # Jev is fixed: the next run, 10 minutes later, decides the three that waited
    age_errors(ctx, 10)
    jev.answers.update(
        {
            "Invoice 7": answer("transactional", transactional=0.95),
            "Quick question": answer("cold_outreach", cold_outreach=0.97),
            "Issue 41": answer("newsletter", newsletter=0.97),
        }
    )
    run = triage.run_account(ctx, account, Mode.ACT)
    assert (run.processed, run.errors, run.error) == (3, 0, None)
    assert _row(ctx, "1").category == "transactional" and _row(ctx, "1").error_attempts == 0


# --- EX-29 -----------------------------------------------------------------------------------


def test_ex29_an_email_answered_again_counts_once_toward_an_auto_rule(spec):
    """EX-29 (docs/how-it-decides.md §5, §3.5 automatic rules, ADR 0036)."""
    ctx, gmail, account = spec
    gmail.emails = {
        str(n): email(str(n), "Weekly <news@weekly.example>", f"Issue {n}") for n in (1, 2, 3)
    }
    for n in (1, 2, 3):
        ctx.classifier.answers[f"Issue {n}"] = answer("newsletter", newsletter=0.97)

    def rule():
        with ctx.sessions() as s:
            r = s.query(SenderRule).one()
            return r.hits, r.active

    del gmail.emails["3"]  # issues 1 and 2 arrive while the account is in label
    triage.run_account(ctx, account, Mode.LABEL)
    assert rule() == (2, False)

    triage.run_account(ctx, account, Mode.ACT)  # the switch to act asks Jev again for both
    assert ctx.classifier.calls == 4 and _row(ctx, "1").archived
    assert rule() == (2, False)  # still two emails

    gmail.emails["3"] = email("3", "Weekly <news@weekly.example>", "Issue 3")
    triage.run_account(ctx, account, Mode.ACT)
    assert rule() == (3, True)


# --- EX-30 -----------------------------------------------------------------------------------


def test_ex30_two_emails_jev_refuses_dont_hold_up_the_rest(spec):
    """EX-30 (docs/how-it-decides.md §5, §1 the pass over an inbox, ADR 0035)."""
    ctx, gmail, account = spec
    gmail.emails = {
        "1": email("1", "Forms <forms@survey.example>", "Your response"),
        "2": email("2", "Forms <forms@survey.example>", "Your response"),  # to another alias
        "3": email("3", "Weekly <news@weekly.example>", "Issue 41"),
    }
    ctx.classifier.answers.update(
        {
            "Your response": jev_http_error(TypeSafeUnprocessableEntityError, 422),
            "Issue 41": answer("newsletter", newsletter=0.97),
        }
    )

    def attempts():
        return [_row(ctx, i).error_attempts for i in ("1", "2")]

    for passes in (1, 2, 3):  # every 10 minutes: both copies are due again and stop the pass
        if passes > 1:
            age_errors(ctx, 10)
        with pytest.raises(triage.JevUnavailable, match="refused 2 requests in a row"):
            triage.run_account(ctx, account, Mode.ACT)
        assert attempts() == [passes, passes]
    with ctx.sessions() as s:
        assert s.query(Message).filter_by(gmail_id="3").count() == 0  # it waited behind them

    age_errors(ctx, 10)  # 10 of the 20 minutes the third failure waits
    run = triage.run_account(ctx, account, Mode.ACT)
    assert (run.processed, run.errors, run.error) == (1, 0, None)
    issue = _row(ctx, "3")
    assert (issue.status, issue.category, issue.archived) == ("done", "newsletter", True)
    assert attempts() == [3, 3]  # still retried later, until 8 failures give up


# --- EX-31 -----------------------------------------------------------------------------------


def test_ex31_an_email_answered_differently_restarts_a_pending_auto_rule(spec):
    """EX-31 (docs/how-it-decides.md §5, §3.5 automatic rules, ADR 0036)."""
    ctx, gmail, account = spec
    gmail.emails = {
        str(n): email(str(n), "Weekly <news@weekly.example>", f"Issue {n}") for n in (1, 2, 3)
    }
    for n in (1, 2, 3):
        ctx.classifier.answers[f"Issue {n}"] = answer("newsletter", newsletter=0.97)

    def rule():
        with ctx.sessions() as s:
            r = s.query(SenderRule).one()
            return r.category, r.hits, r.active

    del gmail.emails["3"]
    triage.run_account(ctx, account, Mode.LABEL)
    assert rule() == ("newsletter", 2, False)

    ctx.classifier.answers["Issue 1"] = answer("marketing", marketing=0.97)
    triage.run_account(ctx, account, Mode.ACT)  # asked again: issue 1 is now marketing
    assert _row(ctx, "1").category == "marketing"
    assert rule() == ("newsletter", 0, False)  # restarted, not switched

    gmail.emails["3"] = email("3", "Weekly <news@weekly.example>", "Issue 3")
    triage.run_account(ctx, account, Mode.ACT)
    assert rule() == ("newsletter", 1, False)


# --- EX-32 -----------------------------------------------------------------------------------


def test_ex32_the_public_suffix_list_decides_what_a_domain_is(cfg, account, monkeypatch):
    """EX-32 (docs/how-it-decides.md §5, §4.8 machine addresses, §4.9 Public Suffix List,
    §3.4 group_domains, ADR 0037)."""
    # Replies to acme.de's bounce subdomain: acme.de is the registrable domain, not a suffix.
    replies = {f"s{i}": {"To": f"m-77x{i}@bounce.acme.de"} for i in range(12)}
    svc = SentMailService(replies)
    gmail = sent_mail_gmail(monkeypatch, svc)
    contact = triage.previous_contact(gmail, account.email, "updates@acme.de")
    assert contact == "never"
    assert svc.queries == ["in:sent to:updates@acme.de", "in:sent to:acme.de"]
    d = decide(
        cfg, account, _sig("Acme <updates@acme.de>"), _lead_suggestions(), previous_contact=contact
    )
    assert (d.category, d.archive, d.archive_group) == ("notification", True, True)
    assert d.add_labels == ["Jev/Notifications"]  # no Review, no Needs Reply

    # A person at a company called reply under the three-level wa.gov.au is searched for herself.
    svc = SentMailService({"k": {"To": "Kim <kim@reply.wa.gov.au>"}})
    gmail = sent_mail_gmail(monkeypatch, svc)
    assert triage.previous_contact(gmail, account.email, "kim@reply.wa.gov.au") == (
        "wrote_to_sender"
    )
    assert svc.queries == ["in:sent to:kim@reply.wa.gov.au"]

    for suffix in ("wa.gov.au", "blogspot.com"):
        with pytest.raises(ValidationError, match=f"'{suffix}' is a public suffix"):
            Account(email="me@example.com", group_domains=[suffix])
    assert Account(email="me@sch.gg", group_domains=["sch.gg"]).group_domains == ["sch.gg"]
