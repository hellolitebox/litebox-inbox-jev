"""Worked examples from docs/how-it-decides.md, section 5, run against the real code.

Each `test_exNN_*` matches the doc's `EX-NN` heading. `test_doc_examples_match_tests` fails when
an ID exists on only one side, so the spec and these tests can't drift apart.
"""

import base64
import re
import sys
from datetime import timedelta
from email import policy as email_policy
from email.parser import BytesParser
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlencode

import httpx
import pytest
from conftest import in_language, make_signals
from fakes import (
    GMAIL_PASS,
    SENT_BASE_S,
    FakeClassifier,
    FakeGmail,
    HttpError,
    JevDown,
    JevFails,
    SentMailService,
    age_errors,
    calendar_payload,
    email,
    gmail_factory,
    group_relay_headers,
    jev_http_error,
    sent_mail_gmail,
)
from pydantic import ValidationError
from typesafe_sdk import (
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeUnprocessableEntityError,
)

from inbox_jev import digest, jev_guard, rules, scan_rows, triage
from inbox_jev import unsubscribe as unsub
from inbox_jev.body import calendar_method, extract_body, extract_link_domains
from inbox_jev.classification import Classification, PreviousContact
from inbox_jev.classifier import CATEGORY_INSTRUCTIONS, build_state
from inbox_jev.config import Account, DigestLanguage, Mode, Settings
from inbox_jev.db import (
    MAX_ERROR_ATTEMPTS,
    Message,
    Run,
    SenderRule,
    SentDomainScan,
    Unsubscribe,
)
from inbox_jev.gmail import Email
from inbox_jev.policy import decide, plan
from inbox_jev.rules import RuleSource
from inbox_jev.sent_scan import NEVER_VERDICT_TTL
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


class Post(NamedTuple):
    """A one-click POST as the server saw it: its URL, media type and form fields (urlencoded)."""

    url: str
    media_type: str
    fields: str


def _form_fields(request: httpx.Request) -> tuple[str, str]:
    """The request's media type and form fields, decoded by the stdlib's own parsers, whichever
    of RFC 8058's two encodings carried them."""
    content_type = request.headers["content-type"]
    media_type = content_type.split(";", 1)[0]
    if media_type != "multipart/form-data":
        return media_type, request.content.decode()
    raw = f"Content-Type: {content_type}\r\n\r\n".encode() + request.content
    parts = BytesParser(policy=email_policy.default).parsebytes(raw).iter_parts()
    fields = [
        (part.get_param("name", header="content-disposition"), part.get_content()) for part in parts
    ]
    return media_type, urlencode(fields)


ANY_FORM = frozenset({"multipart/form-data", "application/x-www-form-urlencoded"})


def _one_click_posts(ctx, parses=None):
    """Route `ctx`'s one-click POSTs to a mock transport (no network, no DNS) and record them.

    An endpoint answers 415 to a media type its host doesn't parse (`parses`: host → media
    types, every host parsing both by default), 200 otherwise."""
    parses = parses or {}
    posts = []
    real = unsub.unsubscribe

    def handler(request):
        # The POST goes to the IP the guard validated; Host carries the URL's hostname.
        url = f"https://{request.headers['host']}{request.url.raw_path.decode()}"
        posts.append(Post(url, *_form_fields(request)))
        parsed = parses.get(request.headers["host"], ANY_FORM)
        return httpx.Response(200 if posts[-1].media_type in parsed else 415)

    def fake(sig, send_mail):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return real(sig, send_mail, client=client, url_guard=lambda url: ["93.184.216.34"])

    ctx.unsubscriber = fake
    return posts


# --- EX-01 -----------------------------------------------------------------------------------


def test_ex01_tool_digest_split_is_archived_by_the_group(cfg, account, make_sessions):
    """EX-01 (docs/how-it-decides.md §5, §3.2 archive group)."""
    sig = make_signals(sender="digest@analytics.example", sender_domain="analytics.example")
    jev = answer("notification", notification=0.55, newsletter=0.42, marketing=0.02, important=0.01)
    d = decide(cfg, account, sig, jev, previous_contact=PreviousContact.NEVER)
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
    d_sure = decide(cfg, strict, sig, sure, previous_contact=PreviousContact.NEVER)
    assert d_sure.archive_group and sure.confidence >= cfg.auto_rule.min_confidence
    sessions = make_sessions("sqlite:///:memory:")
    with sessions() as s:
        rules.record_auto(s, cfg, account.email, sig.sender, jev, d)
        rules.record_auto(s, cfg, account.email, sig.sender, sure, d_sure)
        assert s.query(SenderRule).count() == 0

    narrowed = account.model_copy(update={"act_categories": ["notification"]})
    d = decide(cfg, narrowed, sig, jev, previous_contact=PreviousContact.NEVER)
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
        text = digest.build(s, ctx.cfg, in_language(account, DigestLanguage.ES), gmail=gmail).text
    assert "`Jev/Action` (1)" in text and text.count("Approve payment to Acme") == 1


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
    state = build_state(mail, account.email, 3000, PreviousContact.NEVER)
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


def _run_sequence(spec, written=frozenset()):
    ctx, gmail, account = spec
    gmail.written = written
    gmail.emails = {"1": _sequence_email()}
    ctx.classifier.answers["Re: quick question about your pipeline"] = answer(
        "cold_outreach", cold_outreach=0.96, marketing=0.04
    )
    posts = _one_click_posts(ctx)
    triage.run_account(ctx, account, Mode.ACT)
    return ctx, gmail, posts


def test_ex04_stranger_sequence_is_archived_and_unsubscribed(spec):
    """EX-04 (docs/how-it-decides.md §5, §3.3 unsubscribe conditions, §4.3 one-click)."""
    ctx, gmail, posts = _run_sequence(spec)
    assert gmail.written_queries == ["sam@growthagency.example", "growthagency.example"]
    assert ctx.classifier.states[0]["previous_contact"] == "never"
    assert gmail.modified[-1][2] == ["INBOX"]
    assert "Label_Jev/Outreach" in gmail.modified[-1][1]
    assert posts == [
        Post(
            "https://unsub.sequencer.example/u/abc",
            "multipart/form-data",
            "List-Unsubscribe=One-Click",
        )
    ]
    assert gmail.sent == []  # the mailto isn't needed
    with ctx.sessions() as s:
        row = s.query(Unsubscribe).one()
        assert (row.list_key, row.method, row.status) == (
            "growthagency.example",
            "one_click",
            "ok",
        )


def test_ex05_known_contact_sequence_is_archived_only(spec):
    """EX-05 (docs/how-it-decides.md §5, §3.3 condition 5, §4.1 previous_contact)."""
    ctx, gmail, posts = _run_sequence(spec, written=frozenset({"sam@growthagency.example"}))
    assert ctx.classifier.states[0]["previous_contact"] == "wrote_to_sender"
    assert gmail.modified[-1][2] == ["INBOX"]
    assert posts == [] and gmail.sent == []
    with ctx.sessions() as s:
        assert s.query(Unsubscribe).count() == 0
        assert s.query(SenderRule).count() == 0  # known_contact_kept never earns a rule

    for contact in (PreviousContact.WROTE_TO_SENDER, PreviousContact.WROTE_TO_THEIR_DOMAIN):
        d = decide(
            ctx.cfg,
            spec[2],
            extract(_sequence_email().headers),
            answer("cold_outreach", cold_outreach=0.96),
            previous_contact=contact,
        )
        assert d.archive and d.known_contact_kept and not d.unsubscribe


# --- EX-06 -----------------------------------------------------------------------------------


def test_ex06_pitch_via_own_group_uses_author_and_never_unsubscribes(spec):
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
    posts = _one_click_posts(ctx)
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
    for contact in (PreviousContact.NEVER, None):
        d = decide(cfg, account, sig, _consultation_pitch(), previous_contact=contact)
        assert (d.category, d.archive, d.archive_group) == ("cold_outreach", True, True)
        assert not d.unsubscribe and not d.low_confidence
        assert d.add_labels == ["Jev/Outreach"]  # no Review, no Needs Reply


def test_ex10_known_vendor_follow_up_stays_in_review(cfg, account):
    """EX-10 (docs/how-it-decides.md §5, §3.2 archive group, needs-reply veto)."""
    sig = make_signals(sender="rep@vendor.example", sender_domain="vendor.example")
    d = decide(
        cfg,
        account,
        sig,
        _consultation_pitch(),
        previous_contact=PreviousContact.WROTE_TO_THEIR_DOMAIN,
    )
    assert not d.archive and d.low_confidence
    assert d.add_labels == ["Jev/Review", "Jev/Outreach", "Jev/Needs Reply"]

    keep_signal = answer(
        "cold_outreach", needs_reply=0.79, cold_outreach=0.64, marketing=0.31, important=0.05
    )
    d = decide(cfg, account, sig, keep_signal, previous_contact=PreviousContact.NEVER)
    assert not d.archive and d.low_confidence  # a stranger with keep probability ≥ 0.05


# --- EX-11 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "signals_kw", [{"unsubscribe_https": (), "unsubscribe_mailto": ()}, {"dmarc_pass": False}]
)
def test_ex11_unverified_cold_outreach_is_archived_never_unsubscribed(
    cfg, account, signals_kw, make_sessions
):
    """EX-11 (docs/how-it-decides.md §5, §3.3 condition 3, §3.5 auto rules)."""
    sig = make_signals(
        sender="founder@devshop.example", sender_domain="devshop.example", **signals_kw
    )
    jev = answer("cold_outreach", cold_outreach=0.97)
    d = decide(cfg, account, sig, jev, previous_contact=PreviousContact.NEVER)
    assert (d.category, d.archive, d.spam, d.unsubscribe) == ("cold_outreach", True, False, False)
    assert d.unsubscribe_skipped_unverified
    p = plan(cfg, d, Mode.ACT)
    assert p.archived and not p.spammed and "SPAM" not in p.add_labels

    sessions = make_sessions("sqlite:///:memory:")
    with sessions() as s:
        rules.record_auto(s, cfg, account.email, sig.sender, jev, d)
        assert s.query(SenderRule).one().hits == 1  # still counts toward an auto rule

    # A known contact: the unverified check stopped the unsubscribe, so known_contact_kept
    # isn't set (same order as the rule path), but the answer still doesn't count toward an
    # auto rule: known contacts never earn a cold_outreach rule.
    d = decide(cfg, account, sig, jev, previous_contact=PreviousContact.WROTE_TO_SENDER)
    assert (
        d.archive
        and d.unsubscribe_skipped_unverified
        and not d.known_contact_kept
        and not d.unsubscribe
    )
    with sessions() as s:
        s.query(SenderRule).delete()
        rules.record_auto(
            s, cfg, account.email, sig.sender, jev, d, PreviousContact.WROTE_TO_SENDER
        )
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
    d = decide(cfg, account, http_only, jev, previous_contact=PreviousContact.NEVER)
    assert d.archive and d.unsubscribe_skipped_unverified and not d.unsubscribe


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
        text = digest.build(
            s, ctx.cfg, in_language(account, DigestLanguage.ES), hours=48, gmail=gmail
        ).text
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
        text = digest.build(s, ctx.cfg, in_language(account, DigestLanguage.ES), gmail=gmail).text
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
    d = decide(cfg, account, sig, jev, previous_contact=PreviousContact.NEVER)
    assert d.low_confidence and not d.archive and not d.archive_group
    assert d.add_labels == ["Jev/Review", "Jev/Newsletters"]

    lenient = account.model_copy(update={"min_confidence": {"marketing": 0.90}})
    d = decide(cfg, lenient, sig, jev, previous_contact=PreviousContact.NEVER)
    assert d.archive and d.archive_group and not d.unsubscribe  # the account override wins


# --- EX-17 -----------------------------------------------------------------------------------


def test_ex17_no_gmail_authentication_results_is_unverified(spec):
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
    posts = _one_click_posts(ctx)
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
    with pytest.raises(jev_guard.JevUnavailableError):
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
        text = digest.build(s, ctx.cfg, in_language(account, DigestLanguage.ES), gmail=gmail).text
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
    with pytest.raises(jev_guard.JevUnavailableError):
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
        text = digest.build(s, ctx.cfg, in_language(account, DigestLanguage.ES), gmail=gmail).text
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
    assert contact == PreviousContact.NEVER
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
    assert (
        triage.previous_contact(gmail, account.email, sender)
        == PreviousContact.WROTE_TO_THEIR_DOMAIN
    )
    assert svc.reads == ["p"]


def test_ex25_no_reply_sender_reply_signal_is_judged_like_a_strangers(cfg, account):
    """EX-25 (docs/how-it-decides.md §5, §3.2 archive group, §4.8 machine addresses)."""
    known = PreviousContact.WROTE_TO_THEIR_DOMAIN
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


def test_ex26_writing_to_a_friend_on_country_free_mail_makes_no_known_contact(spec):
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
    posts = _one_click_posts(ctx)
    triage.run_account(ctx, account, Mode.ACT)

    assert gmail.written_queries == ["growth.lead@hotmail.com.ar"]  # no domain search
    assert ctx.classifier.states[0]["previous_contact"] == "never"
    assert gmail.modified[-1][2] == ["INBOX"]
    assert [post.url for post in posts] == ["https://unsub.sequencer.example/u/xyz"]
    with ctx.sessions() as s:
        assert s.query(Unsubscribe).one().status == "ok"


# --- EX-27 -----------------------------------------------------------------------------------


def test_ex27_a_public_suffix_in_group_domains_is_refused():
    """EX-27 (docs/how-it-decides.md §5, §3.4 group_domains, ADR 0033)."""
    with pytest.raises(ValidationError, match=r"'gouv\.fr' is a public suffix"):
        Account(email="me@exemple.gouv.fr", group_domains=["gouv.fr"])
    with pytest.raises(ValidationError, match=r"'com\.ai' is a public suffix"):
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
    with pytest.raises(jev_guard.JevUnavailableError):
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
        with pytest.raises(jev_guard.JevUnavailableError, match="refused 2 requests in a row"):
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
    assert contact == PreviousContact.NEVER
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
        PreviousContact.WROTE_TO_SENDER
    )
    assert svc.queries == ["in:sent to:kim@reply.wa.gov.au"]

    for suffix in ("wa.gov.au", "blogspot.com"):
        with pytest.raises(ValidationError, match=f"'{suffix}' is a public suffix"):
            Account(email="me@example.com", group_domains=[suffix])
    assert Account(email="me@sch.gg", group_domains=["sch.gg"]).group_domains == ["sch.gg"]


# --- EX-33 -----------------------------------------------------------------------------------


def test_ex33_satisfaction_survey_is_not_vetoed_by_needs_reply(cfg, account):
    """EX-33 (docs/how-it-decides.md §5, §3.2 needs-reply veto, §4.7 questions, ADR 0038)."""
    # The question and the action criteria tell Jev that surveys aren't a personal action.
    assert "surveys" in cfg.needs_reply.question
    assert "surveys" in cfg.by_key["action_required"].criteria

    sig = _sig("Experiencia <noreply@health.example>")
    assert sig.no_reply

    def survey(needs_reply):
        return answer(
            "marketing",
            needs_reply=needs_reply,
            marketing=0.87,
            newsletter=0.05,
            transactional=0.03,
            action_required=0.04,
            important=0.01,
        )

    # Before: "give us your opinion" read as a request (0.88) with keep 0.05 -> Review.
    d = decide(
        cfg, account, sig, survey(0.88), previous_contact=PreviousContact.WROTE_TO_THEIR_DOMAIN
    )
    assert not d.archive and d.low_confidence
    assert d.add_labels == ["Jev/Review", "Jev/Marketing", "Jev/Needs Reply"]

    # Now: Jev answers no, so the archive group (0.95) archives it, never unsubscribing.
    d = decide(
        cfg, account, sig, survey(0.15), previous_contact=PreviousContact.WROTE_TO_THEIR_DOMAIN
    )
    assert (d.category, d.archive, d.archive_group) == ("marketing", True, True)
    assert not (d.unsubscribe or d.low_confidence or d.spam)
    assert d.add_labels == ["Jev/Marketing"]


# --- EX-34 -----------------------------------------------------------------------------------


def test_ex34_an_email_to_a_similar_address_makes_no_known_contact(spec, monkeypatch):
    """EX-34 (docs/how-it-decides.md §5, §4.1 previous_contact, ADR 0039)."""
    ctx, gmail, account = spec
    # Gmail's to:lee@outlook.com also returned an email to someone else whose address has "lee".
    svc = SentMailService({"s0": {"To": "Ash <ashlee@outlook.com>"}})
    gmail.has_written_to = sent_mail_gmail(monkeypatch, svc).has_written_to
    gmail.emails = {
        "1": email(
            "1",
            "Lee <lee@outlook.com>",
            "Quick question about your pipeline",
            list_unsubscribe="<https://unsub.sequencer.example/u/lee>",
            list_unsubscribe_post="List-Unsubscribe=One-Click",
        )
    }
    ctx.classifier.answers["Quick question about your pipeline"] = answer(
        "cold_outreach", cold_outreach=0.96
    )
    posts = _one_click_posts(ctx)
    triage.run_account(ctx, account, Mode.ACT)

    assert svc.queries == ["in:sent to:lee@outlook.com"]  # free mail: no domain search
    assert svc.reads == ["s0"]  # its To says who it went to
    assert ctx.classifier.states[0]["previous_contact"] == "never"
    assert gmail.modified[-1][2] == ["INBOX"]
    assert [post.url for post in posts] == ["https://unsub.sequencer.example/u/lee"]

    # An email to her own address, however it is capitalised, makes her a known contact.
    svc = SentMailService(
        {"s0": {"To": "Ash <ashlee@outlook.com>"}, "s1": {"Cc": "Lee@Outlook.com"}}
    )
    known = triage.previous_contact(
        sent_mail_gmail(monkeypatch, svc), account.email, "lee@outlook.com"
    )
    assert known == PreviousContact.WROTE_TO_SENDER


# --- EX-35 -----------------------------------------------------------------------------------


def test_ex35_a_domain_written_to_only_at_machine_addresses_is_read_in_full(
    cfg, account, make_sessions, monkeypatch
):
    """EX-35 (docs/how-it-decides.md §5, §4.1 previous_contact, ADR 0040)."""
    replies = {f"r{i}": {"To": f"reply+{i}abc@reply.code.example"} for i in range(60)}
    svc = SentMailService(replies)
    sender = "notifications@code.example"
    sessions = make_sessions("sqlite:///:memory:")

    def one_run():
        svc.queries, svc.reads = [], []
        with sessions() as s:
            scans = scan_rows.DomainScanRows(s, account.email)
            contact = triage.previous_contact(
                sent_mail_gmail(monkeypatch, svc), account.email, sender, scans
            )
            s.commit()
        return contact, len(svc.reads)

    sig = _sig(f"Code <{sender}>")
    runs = [one_run() for _ in range(3)]
    assert runs == [
        (PreviousContact.WROTE_TO_THEIR_DOMAIN, 25),
        (PreviousContact.WROTE_TO_THEIR_DOMAIN, 25),
        (PreviousContact.NEVER, 10),
    ]
    first = decide(cfg, account, sig, _lead_suggestions(), previous_contact=runs[0][0])
    assert (first.archive, first.low_confidence) == (False, True)  # Review, as before
    assert "Jev/Needs Reply" in first.add_labels
    last = decide(cfg, account, sig, _lead_suggestions(), previous_contact=runs[2][0])
    assert (last.category, last.archive, last.archive_group) == ("notification", True, True)
    assert last.add_labels == ["Jev/Notifications"]
    with sessions() as s:
        row = s.query(SentDomainScan).one()
        assert (row.domain, row.verdict, row.read, row.reached_end) == (
            "code.example",
            "never",
            60,
            True,
        )
        assert row.verdict_at is not None

    assert one_run() == (
        PreviousContact.NEVER,
        0,
    )  # one search for newer sent mail, nothing to read
    # A new email to a person there: read once, then known without asking Gmail again.
    svc.sent = {"p": {"To": "Ana <ana@code.example>"}, **svc.sent}
    svc.dates["p"] = SENT_BASE_S + 3600
    assert one_run() == (PreviousContact.WROTE_TO_THEIR_DOMAIN, 1)
    assert one_run() == (PreviousContact.WROTE_TO_THEIR_DOMAIN, 0)
    assert svc.queries == ["in:sent to:notifications@code.example"]


# --- EX-36 / EX-37 ---------------------------------------------------------------------------


def _vendor_pitch():
    """A verified one-click pitch from sales@vendor.example (EX-36, EX-37)."""
    return _sig(
        "Sales <sales@vendor.example>",
        list_unsubscribe="<https://unsub.vendor.example/u/1>",
        list_unsubscribe_post="List-Unsubscribe=One-Click",
    )


def _domain_runs(account, monkeypatch, svc, sessions):
    """One run's `previous_contact` for sales@vendor.example, its scan kept in `sessions`."""

    def one_run():
        svc.queries, svc.reads = [], []
        with sessions() as s:
            scans = scan_rows.DomainScanRows(s, account.email)
            contact = triage.previous_contact(
                sent_mail_gmail(monkeypatch, svc), account.email, "sales@vendor.example", scans
            )
            s.commit()
        return contact

    return one_run


def test_ex36_a_never_verdict_is_redone_after_a_month(cfg, account, make_sessions, monkeypatch):
    """EX-36 (docs/how-it-decides.md §5, §4.1 previous_contact, ADR 0040)."""
    svc = SentMailService({f"b{i}": {"To": f"bounces+{i}@vendor.example"} for i in range(3)})
    sessions = make_sessions("sqlite:///:memory:")
    one_run = _domain_runs(account, monkeypatch, svc, sessions)
    jev = answer("cold_outreach", cold_outreach=0.96)

    assert one_run() == PreviousContact.NEVER
    d = decide(cfg, account, _vendor_pitch(), jev, previous_contact=PreviousContact.NEVER)
    assert d.archive and d.unsubscribe

    # You restore from Trash an email you sent Ana there long ago: older than the span read.
    svc.sent["ana"] = {"To": "Ana <ana@vendor.example>"}
    svc.dates["ana"] = SENT_BASE_S - 400 * 24 * 3600

    def age(by):
        with sessions() as s:
            s.query(SentDomainScan).one().verdict_at -= by
            s.commit()

    age(NEVER_VERDICT_TTL - timedelta(days=1))
    assert one_run() == PreviousContact.NEVER  # within the month: only newer mail is listed
    assert svc.reads == []
    age(timedelta(days=1))
    assert (
        one_run() == PreviousContact.WROTE_TO_THEIR_DOMAIN
    )  # a month old: read again from the newest
    assert svc.queries == ["in:sent to:sales@vendor.example", "in:sent to:vendor.example"]
    assert svc.reads[-1] == "ana"
    d = decide(
        cfg, account, _vendor_pitch(), jev, previous_contact=PreviousContact.WROTE_TO_THEIR_DOMAIN
    )
    assert d.archive and d.known_contact_kept and not d.unsubscribe
    with sessions() as s:
        assert s.query(SentDomainScan).one().verdict == "person"


def test_ex37_a_scan_read_out_of_date_order_is_not_trusted(
    cfg, account, make_sessions, monkeypatch, caplog
):
    """EX-37 (docs/how-it-decides.md §5, §4.1 previous_contact, ADR 0040)."""
    caplog.set_level("WARNING", logger="inbox_jev.sent_scan")
    sent = {
        "a": {"To": "bounces+a@vendor.example"},
        "b": {"To": "bounces+b@vendor.example"},
        "c": {"To": "bounces+c@vendor.example"},
    }
    dates = {"a": SENT_BASE_S, "b": SENT_BASE_S - 600, "c": SENT_BASE_S - 300}
    svc = SentMailService(sent, dates=dates, ordered=False)
    sessions = make_sessions("sqlite:///:memory:")
    one_run = _domain_runs(account, monkeypatch, svc, sessions)

    assert (
        one_run() == PreviousContact.WROTE_TO_THEIR_DOMAIN
    )  # c came after b though newer: not trusted
    assert "out of date order" in caplog.text
    d = decide(
        cfg,
        account,
        _vendor_pitch(),
        answer("cold_outreach", cold_outreach=0.96),
        previous_contact=PreviousContact.WROTE_TO_THEIR_DOMAIN,
    )
    assert d.known_contact_kept and not d.unsubscribe
    with sessions() as s:
        row = s.query(SentDomainScan).one()
        assert (row.verdict, row.read, row.newest_s) == ("scanning", 0, None)  # starts over

    svc.ordered = True
    assert one_run() == PreviousContact.NEVER
    assert svc.queries[-1] == "in:sent to:vendor.example"  # from the newest match again


# --- EX-38 -----------------------------------------------------------------------------------

# A fake "voice letter" whose play button goes through an unrelated brand's click tracker, then a
# conversation between other people pasted below it. Every name and domain is invented.
VOICE_LURE_HTML = (
    "<p>Voice Letter &#9654; Duration: 1 min 30 sec</p>"
    '<a href="https://links.notify.bigbrand.example/ls/click?upn=TOKEN&amp;r=me%40example.com">'
    "Play</a>"
    "<p>Hi Alex and Sam, please find the revised permit schedule below.</p>"
    "<p>Robin Doe, Permits Officer, County Works</p>"
    '<a href="https://www.countyworks-gov.example/permits">Permits</a> '
    '<a href="https://countyworks-gov.example/contact">Contact</a>'
    "<p>From: Alex Roe Sent: Monday To: Sam Poe Subject: Permit schedule</p>"
)


def test_ex38_a_fake_reply_with_a_voicemail_lure_is_spam(spec):
    """EX-38 (docs/how-it-decides.md §5, §4.7 what Jev receives, ADR 0042)."""
    ctx, gmail, account = spec
    payload = {"mimeType": "text/html", "body": {"data": _b64(VOICE_LURE_HTML)}}
    subject = "RE: Updated permit schedule for review"
    mail = email(
        "1",
        "Daily Wire <noreply@dailywire-news.example>",
        subject,
        return_path="<bounces+77@em1234.dailywire-news.example>",
        authentication_results=(
            "mx.google.com; dkim=pass header.i=@dailywire-news.example header.s=s1; "
            "spf=pass smtp.mailfrom=bounces+77@em1234.dailywire-news.example"
        ),
    )
    mail.body = extract_body(payload)
    mail.link_domains = extract_link_domains(payload)
    gmail.emails = {"1": mail}
    ctx.classifier.answers[subject] = answer("spam", needs_reply=0.10, spam=0.95, important=0.03)
    triage.run_account(ctx, account, Mode.ACT)

    (state,) = ctx.classifier.states
    assert state["reply_without_thread"] is True  # RE: with no In-Reply-To or References
    assert state["link_domains"] == ["countyworks-gov.example", "bigbrand.example"]
    assert state["sender_auth"] == {"spf": "pass", "dkim": "pass", "dmarc": "none"}
    assert "upn=TOKEN" not in str(state) and "me%40example.com" not in str(state)
    row = _row(ctx, "1")
    assert (row.category, row.spammed, row.archived) == ("spam", True, False)
    _, add, remove = gmail.modified[-1]
    assert "SPAM" in add and remove == ["INBOX"]

    # The criteria tell Jev what these keys mean, and keep such mail out of important/Needs Reply.
    spam = ctx.cfg.by_key["spam"].criteria
    assert all(k in spam for k in ("sender_auth", "reply_without_thread", "link_domains"))
    assert "conversation between other people" in ctx.cfg.by_key["important"].criteria
    assert "phishing" in ctx.cfg.needs_reply.question


# --- EX-39 -----------------------------------------------------------------------------------

# A tax-return portal's notice: sent from its mail domain, linking to its own brand and portal
# domains (not the sending domain). Every name and domain is invented.
TAX_PORTAL_HTML = (
    "<p>Your 2025 tax return is ready for your review and signature.</p>"
    '<a href="https://app.taxreturnsend.example/review?token=TOKEN">Review and sign</a> '
    '<a href="https://www.taxportal-sign.example/help">Help</a> '
    '<a href="https://taxreturnsend.example/privacy">Privacy</a>'
)


def test_ex39_a_tax_portal_notice_with_dmarc_pass_is_not_spam(spec):
    """EX-39 (docs/how-it-decides.md §5, §4.7 what Jev receives, ADR 0042)."""
    ctx, gmail, account = spec
    payload = {"mimeType": "text/html", "body": {"data": _b64(TAX_PORTAL_HTML)}}
    subject = "Action Required: Your 2025 Tax Return is Ready for Review"
    mail = email(
        "1",
        "Tax Return Desk <noreply@taxreturnsend-mail.example>",
        subject,
        authentication_results=(
            "mx.google.com; dkim=pass header.i=@taxreturnsend-mail.example; "
            "spf=pass smtp.mailfrom=bounce@taxreturnsend-mail.example; "
            "dmarc=pass header.from=taxreturnsend-mail.example"
        ),
    )
    mail.body = extract_body(payload)
    mail.link_domains = extract_link_domains(payload)
    gmail.emails = {"1": mail}
    ctx.classifier.answers[subject] = answer(
        "action_required", needs_reply=0.85, action_required=0.88, transactional=0.06, spam=0.03
    )
    triage.run_account(ctx, account, Mode.ACT)

    (state,) = ctx.classifier.states
    assert "reply_without_thread" not in state  # not a reply subject
    assert state["link_domains"] == ["taxreturnsend.example", "taxportal-sign.example"]
    assert state["sender_auth"] == {"spf": "pass", "dkim": "pass", "dmarc": "pass"}
    row = _row(ctx, "1")
    assert (row.category, row.spammed, row.archived) == ("action_required", False, False)
    _, add, remove = gmail.modified[-1]
    assert remove == [] and "SPAM" not in add
    assert {"Label_Jev/Action", "Label_Jev/Needs Reply"} <= set(add)

    # The criteria make the phishing signs about mismatch, not about the kind of service.
    spam = ctx.cfg.by_key["spam"].criteria
    assert "Not spam" in spam and "own brand domains" in spam and "dmarc none or fail" in spam
    assert "e-signature" in ctx.cfg.by_key["action_required"].criteria


# --- EX-40 -----------------------------------------------------------------------------------


def test_ex40_an_envelope_sender_that_looks_like_dmarc_pass_does_not_verify(spec):
    """EX-40 (docs/how-it-decides.md §5, §4.2 DMARC, §3.3 condition 3)."""
    ctx, gmail, account = spec
    bounce = "dmarc=pass@bounce.shop.example"
    gmail.emails = {
        "1": email(
            "1",
            "Shop <promo@shop.example>",
            "Flash sale",
            authentication_results=(
                "mx.google.com; dkim=pass header.i=@shop.example header.s=s1; "
                f"spf=pass (google.com: domain of {bounce} designates 192.0.2.1 as permitted "
                f"sender) smtp.mailfrom={bounce}"
            ),
            list_unsubscribe="<https://shop.example/u/1>",
            list_unsubscribe_post="List-Unsubscribe=One-Click",
        )
    }
    ctx.classifier.answers["Flash sale"] = answer("marketing", marketing=0.97)
    posts = _one_click_posts(ctx)
    triage.run_account(ctx, account, Mode.ACT)

    (state,) = ctx.classifier.states
    assert state["sender_auth"] == {"spf": "pass", "dkim": "pass", "dmarc": "none"}
    assert gmail.modified[-1][2] == ["INBOX"] and "SPAM" not in gmail.modified[-1][1]
    assert posts == [] and gmail.sent == []
    row = _row(ctx, "1")
    assert (row.category, row.archived, row.downgraded_to_spam) == ("marketing", True, True)
    with ctx.sessions() as s:
        assert s.query(Unsubscribe).count() == 0


def test_ex41_a_quoted_envelope_sender_cant_hide_a_real_dmarc_fail(spec):
    """EX-41 (docs/how-it-decides.md §5, §4.2 DMARC, §3.3 condition 3)."""
    ctx, gmail, account = spec
    bounce = '"x); dmarc=pass (("@bounce.attacker.example'
    gmail.emails = {
        "1": email(
            "1",
            "Bank <offers@bank.example>",
            "Your statement",
            authentication_results=(
                "mx.google.com; dkim=pass header.i=@attacker.example header.s=s1; "
                f"spf=pass (google.com: domain of {bounce} designates 192.0.2.1 as permitted "
                f"sender) smtp.mailfrom={bounce}; "
                "dmarc=fail (p=NONE sp=NONE dis=NONE) header.from=bank.example"
            ),
            list_unsubscribe="<https://bank.example/u/1>",
            list_unsubscribe_post="List-Unsubscribe=One-Click",
        )
    }
    ctx.classifier.answers["Your statement"] = answer("marketing", marketing=0.97)
    posts = _one_click_posts(ctx)
    triage.run_account(ctx, account, Mode.ACT)

    (state,) = ctx.classifier.states
    assert state["sender_auth"] == {"spf": "pass", "dkim": "pass", "dmarc": "fail"}
    assert gmail.modified[-1][2] == ["INBOX"] and "SPAM" not in gmail.modified[-1][1]
    assert posts == [] and gmail.sent == []
    row = _row(ctx, "1")
    assert (row.category, row.archived, row.downgraded_to_spam) == ("marketing", True, True)
    with ctx.sessions() as s:
        assert s.query(Unsubscribe).count() == 0


def test_ex42_a_malformed_gmail_authentication_header_verifies_nothing(spec):
    """EX-42 (docs/how-it-decides.md §5, §4.2 DMARC, §3.3 condition 3)."""
    ctx, gmail, account = spec
    gmail.emails = {
        "1": email(
            "1",
            "Shop <promo@shop.example>",
            "Flash sale",
            authentication_results=(
                "mx.google.com; dkim=pass header.i=promo)@shop.example header.s=s1; "
                "spf=pass smtp.mailfrom=bounce@shop.example; dmarc=pass header.from=shop.example"
            ),
            list_unsubscribe="<https://shop.example/u/1>",
            list_unsubscribe_post="List-Unsubscribe=One-Click",
        )
    }
    ctx.classifier.answers["Flash sale"] = answer("marketing", marketing=0.97)
    posts = _one_click_posts(ctx)
    triage.run_account(ctx, account, Mode.ACT)

    (state,) = ctx.classifier.states
    assert state["sender_auth"] == {"spf": "none", "dkim": "none", "dmarc": "none"}
    assert gmail.modified[-1][2] == ["INBOX"] and "SPAM" not in gmail.modified[-1][1]
    assert posts == [] and gmail.sent == []
    row = _row(ctx, "1")
    assert (row.category, row.archived, row.downgraded_to_spam) == ("marketing", True, True)
    with ctx.sessions() as s:
        assert s.query(Unsubscribe).count() == 0


def test_ex43_an_address_literal_cant_forge_a_dmarc_pass(spec):
    """EX-43 (docs/how-it-decides.md §5, §4.2 DMARC, §3.3 condition 3)."""
    ctx, gmail, account = spec
    bounce = "x@[t:;dmarc=pass;]"
    gmail.emails = {
        "1": email(
            "1",
            "Shop <promo@shop.example>",
            "Flash sale",
            authentication_results=(
                "mx.google.com; dkim=fail header.i=@shop.example header.s=s1; "
                f"spf=fail (google.com: domain of {bounce} does not designate 192.0.2.1 as "
                f"permitted sender) smtp.mailfrom={bounce}"
            ),
            list_unsubscribe="<https://shop.example/u/1>",
            list_unsubscribe_post="List-Unsubscribe=One-Click",
        )
    }
    ctx.classifier.answers["Flash sale"] = answer("marketing", marketing=0.97)
    posts = _one_click_posts(ctx)
    triage.run_account(ctx, account, Mode.ACT)

    (state,) = ctx.classifier.states
    assert state["sender_auth"] == {"spf": "fail", "dkim": "fail", "dmarc": "none"}
    assert gmail.modified[-1][2] == ["INBOX"] and "SPAM" not in gmail.modified[-1][1]
    assert posts == [] and gmail.sent == []
    row = _row(ctx, "1")
    assert (row.category, row.archived, row.downgraded_to_spam) == ("marketing", True, True)
    with ctx.sessions() as s:
        assert s.query(Unsubscribe).count() == 0


# --- EX-44 -----------------------------------------------------------------------------------


def test_ex44_a_sender_decided_per_email_keeps_its_leads_and_archives_its_fyis(spec):
    """EX-44 (docs/how-it-decides.md §5, §3.5 sender rules, §3.1 categories, ADR 0045)."""
    ctx, gmail, account = spec
    crm = "noreply@notifications.crm.example"
    account = account.model_copy(update={"decide_each_email": [crm]})
    with ctx.sessions() as s:  # learned from three FYIs before the address was listed
        rules.set_rule(s, account.email, crm, "notification", RuleSource.AUTO)
        s.commit()
    lead = 'You\'ve got a new submission on the form "Contact Form - Acme Website"'
    gmail.emails = {
        "1": email("1", f"CRM <{crm}>", lead),
        "2": email("2", f"CRM <{crm}>", "Your weekly dashboard is ready"),
    }
    ctx.classifier.answers[lead] = answer(
        "action_required", needs_reply=0.85, action_required=0.88, notification=0.10
    )
    ctx.classifier.answers["Your weekly dashboard is ready"] = answer(
        "notification", notification=0.99
    )
    triage.run_account(ctx, account, Mode.ACT)

    assert [st["subject"] for st in ctx.classifier.states] == [
        lead,
        "Your weekly dashboard is ready",
    ]
    row = _row(ctx, "1")
    assert (row.source, row.category, row.archived) == ("jev", "action_required", False)
    assert {"Label_Jev/Action", "Label_Jev/Needs Reply"} <= set(gmail.emails["1"].label_ids)
    assert "INBOX" in gmail.emails["1"].label_ids
    row = _row(ctx, "2")
    assert (row.source, row.category, row.archived) == ("jev", "notification", True)
    with ctx.sessions() as s:  # nothing learned: the old auto rule is untouched, and inert
        rule = s.query(SenderRule).one()
        assert (rule.source, rule.category, rule.hits, rule.active) == (
            "auto",
            "notification",
            0,
            True,
        )

    # The criteria tell Jev that a website form's lead needs me, and its tools' FYIs don't.
    action = ctx.cfg.by_key["action_required"].criteria
    assert "form on my or my company's own website or landing page" in action
    assert "a lead or contact request), which is action_required" in (
        ctx.cfg.by_key["notification"].criteria
    )
    assert "form on my own website" in ctx.cfg.needs_reply.question


# --- EX-45 -----------------------------------------------------------------------------------


def test_ex45_a_sender_decided_per_email_is_unsubscribed_only_on_a_manual_rule(spec):
    """EX-45 (docs/how-it-decides.md §5, §3.3 unsubscribe conditions, §3.5, ADR 0045)."""
    ctx, gmail, account = spec
    crm, vendor = "noreply@notifications.crm.example", "news@vendor.example"
    account = account.model_copy(update={"decide_each_email": [crm, vendor]})
    with ctx.sessions() as s:  # your own choice for every email of the vendor
        rules.set_rule(s, account.email, vendor, "marketing", RuleSource.MANUAL)
        s.commit()
    webinar = "Join our live product webinar"
    gmail.emails = {
        "1": email("1", f"CRM <{crm}>", webinar, list_unsubscribe="<mailto:u@crm.example>"),
        "2": email(
            "2", f"Vendor <{vendor}>", "Spring sale", list_unsubscribe="<mailto:u@vendor.example>"
        ),
    }
    ctx.classifier.answers[webinar] = answer("marketing", marketing=0.93, notification=0.05)
    triage.run_account(ctx, account, Mode.ACT)

    row = _row(ctx, "1")  # Jev's word: archived as marketing, never unsubscribed
    assert (row.source, row.category, row.archived) == ("jev", "marketing", True)
    assert "Label_Jev/Marketing" in gmail.emails["1"].label_ids
    row = _row(ctx, "2")  # the manual rule: archived and unsubscribed
    assert (row.source, row.category, row.archived) == ("rule", "marketing", True)
    assert gmail.sent == ["u@vendor.example"]
    with ctx.sessions() as s:
        assert [u.list_key for u in s.query(Unsubscribe)] == ["vendor.example"]


# --- EX-46 -----------------------------------------------------------------------------------


def test_ex46_a_meeting_booked_through_a_scheduling_link_stays_in_the_inbox(spec):
    """EX-46 (docs/how-it-decides.md §5, §3.1 categories, §3.2 needs reply, ADR 0046)."""
    ctx, gmail, account = spec
    tool = "Scheduler <noreply@meetings.scheduler.example>"
    booked = "You've been booked by: Ana Diaz ana@client.example"
    canceled = "Canceled: Intro call with Ana Diaz"
    reminder = "Reminder: Intro call with Ana Diaz starts in 10 minutes"
    invitation = "Invitation: Weekly sync @ Wed 10am - 10:30am"
    accepted = "Accepted: Ana Diaz and me + 1 @ Thu 3pm - 3:30pm"
    new_time = "New time proposed: Weekly sync @ Wed 10am - 10:30am"
    colleague = "Bea Ruiz <bea@partner.example>"
    gmail.emails = {
        "1": email("1", tool, booked),
        "2": email("2", tool, canceled),
        "3": email("3", tool, reminder),
        "4": email("4", colleague, invitation),
        "5": email("5", "Ana Diaz <ana@client.example>", accepted),
        "6": email("6", colleague, new_time),
    }
    ctx.classifier.answers[booked] = answer(
        "action_required", needs_reply=0.20, action_required=0.90, notification=0.08
    )
    ctx.classifier.answers[canceled] = answer(
        "action_required", needs_reply=0.15, action_required=0.86, notification=0.12
    )
    ctx.classifier.answers[reminder] = answer(
        "notification", notification=0.96, action_required=0.03
    )
    ctx.classifier.answers[invitation] = answer(
        "action_required", needs_reply=0.25, action_required=0.82, notification=0.10
    )
    ctx.classifier.answers[accepted] = answer(
        "notification", notification=0.93, action_required=0.05
    )
    ctx.classifier.answers[new_time] = answer(
        "action_required", needs_reply=0.30, action_required=0.84, notification=0.12
    )
    triage.run_account(ctx, account, Mode.ACT)

    # booked, cancelled, a person's invitation and a proposed new time: kept as an action,
    # no Needs Reply
    for gmail_id in ("1", "2", "4", "6"):
        row = _row(ctx, gmail_id)
        assert (row.category, row.archived) == ("action_required", False)
        labels = gmail.emails[gmail_id].label_ids
        assert "INBOX" in labels and "Label_Jev/Action" in labels
        assert "Label_Jev/Needs Reply" not in labels
    # a reminder of a meeting already in the calendar, and a plain "Accepted:" response to an
    # invitation I sent: archived
    for gmail_id in ("3", "5"):
        row = _row(ctx, gmail_id)
        assert (row.category, row.archived) == ("notification", True)
        labels = gmail.emails[gmail_id].label_ids
        assert "INBOX" not in labels and "Label_Jev/Notifications" in labels

    # The criteria tell Jev that a booking or a person's invitation needs me, that reminders
    # don't, that a booking made for me is a record, and that neither waits for an email reply.
    action = ctx.cfg.by_key["action_required"].criteria
    assert "booked, rescheduled or cancelled with me through a scheduling link" in action
    assert "invitation, update or cancellation that a person sent me" in action
    assert "reminders of things already done or of meetings already in my calendar" in action
    assert "a plain response to an invitation I sent (Accepted, Declined, Tentative" in action
    assert "invitation I sent that proposes a new time or carries a note asking me" in action
    notification = ctx.cfg.by_key["notification"].criteria
    assert "reminders of meetings already in my calendar" in notification
    assert "but not a meeting someone booked, rescheduled or cancelled with me" in notification
    assert "nor an invitation, update or cancellation a person sent me" in notification
    assert "It includes a plain response to an invitation I sent" in notification
    assert "not one that proposes a new time or carries a note asking me something" in notification
    transactional = ctx.cfg.by_key["transactional"].criteria
    assert "bookings and reservations made by me or for me" in transactional
    assert "Not a meeting someone booked with me through my scheduling link" in transactional
    question = ctx.cfg.needs_reply.question
    assert "booked, moved or cancelled a meeting with me" in question
    assert "a calendar invitation, update or cancellation, or a" in question
    assert "response proposing a new time (I answer it in my calendar" in question


# --- EX-47 -----------------------------------------------------------------------------------


def test_ex47_rsvp_responses_from_a_person_never_teach_a_rule_on_them(spec):
    """EX-47 (docs/how-it-decides.md §5, §3.5 automatic rules, §4.6, ADR 0047)."""
    ctx, gmail, account = spec
    ana = "Ana Diaz <ana@client.example>"
    responses = {
        "1": "Accepted: Kickoff @ Mon 10am",
        "2": "Accepted: Review @ Tue 3pm",
        "3": "Accepted: Retro @ Fri 4pm",
    }
    for gmail_id, subject in responses.items():
        rsvp = email(gmail_id, ana, subject)
        # As `Gmail.get` reads it: the iTIP part of Google Calendar's response
        rsvp.calendar_method = calendar_method(calendar_payload("REPLY", subject))
        gmail.emails[gmail_id] = rsvp
        ctx.classifier.answers[subject] = answer(
            "notification", notification=0.99, important=0.003, action_required=0.002
        )
    triage.run_account(ctx, account, Mode.ACT)

    for gmail_id in responses:
        row = _row(ctx, gmail_id)
        assert (row.category, row.archived) == ("notification", True)
    with ctx.sessions() as s:
        assert s.query(SenderRule).filter_by(sender_key="ana@client.example").count() == 0

    # Her next real email goes to Jev and stays
    gmail.emails["4"] = email("4", ana, "Contract question")
    ctx.classifier.answers["Contract question"] = answer("important", important=0.97)
    triage.run_account(ctx, account, Mode.ACT)
    row = _row(ctx, "4")
    assert (row.source, row.category, row.archived) == ("jev", "important", False)
    assert "INBOX" in gmail.emails["4"].label_ids


# --- EX-48 -----------------------------------------------------------------------------------


def test_ex48_a_one_click_endpoint_that_parses_one_encoding_only(spec):
    """EX-48 (docs/how-it-decides.md §5, §4.3 one-click, ADR 0048)."""
    ctx, gmail, account = spec
    multipart, form = "multipart/form-data", "application/x-www-form-urlencoded"
    one_click = "List-Unsubscribe=One-Click"
    senders = {
        "1": ("deals@shop.example", "news.shop.example", {multipart}),
        "2": ("offers@store.example", "go.store.example", {form}),
        "3": ("promo@outlet.example", "u.outlet.example", set()),
    }
    for gmail_id, (sender, host, _) in senders.items():
        subject = f"Sale {gmail_id}"
        gmail.emails[gmail_id] = email(
            gmail_id,
            sender,
            subject,
            list_unsubscribe=f"<https://{host}/u/{gmail_id}>",
            list_unsubscribe_post=one_click,
        )
        ctx.classifier.answers[subject] = answer("marketing", marketing=0.97)
    posts = _one_click_posts(ctx, {host: parses for _, host, parses in senders.values()})
    triage.run_account(ctx, account, Mode.ACT)

    assert posts == [
        Post("https://news.shop.example/u/1", multipart, one_click),
        Post("https://go.store.example/u/2", multipart, one_click),
        Post("https://go.store.example/u/2", form, one_click),
        Post("https://u.outlet.example/u/3", multipart, one_click),
        Post("https://u.outlet.example/u/3", form, one_click),
    ]
    with ctx.sessions() as s:
        rows = {r.list_key: r for r in s.query(Unsubscribe)}
        outcome = {k: (r.method, r.status, r.http_status, r.error) for k, r in rows.items()}
        assert outcome == {
            "shop.example": ("one_click", "ok", 200, None),
            "store.example": ("one_click", "ok", 200, None),
            "outlet.example": ("one_click", "failed", 415, "HTTP 415 Unsupported Media Type"),
        }
        text = digest.build(s, ctx.cfg, in_language(account, DigestLanguage.EN), gmail=gmail).text
    assert "• outlet.example: HTTP 415 Unsupported Media Type" in text


# --- EX-49 ----------------------------------------------------------------------------------

LURE_RESULTS = (
    "mx.google.com;\r\n       spf=softfail (google.com: domain of transitioning "
    "docs@homescape-lure.example does not designate 192.0.2.9 as permitted sender) "
    "smtp.mailfrom=docs@homescape-lure.example;\r\n       dmarc=fail (p=NONE sp=NONE dis=NONE) "
    "header.from=homescape-lure.example"
)


def test_ex49_phishing_relayed_by_your_own_group_shows_its_authors_authentication(spec):
    """EX-49 (docs/how-it-decides.md §5, §4.2 DMARC, §4.5 X-Original-Sender trust, ADR 0050)."""
    ctx, gmail, account = spec
    subject = "Document ID: New Pending Review"
    headers = group_relay_headers(author="docs@homescape-lure.example", author_results=LURE_RESULTS)
    headers |= {
        "subject": [subject],
        "list-post": ["<mailto:hello@example.com>"],
        "list-unsubscribe": ["<mailto:googlegroups-manage+1+unsubscribe@googlegroups.com>"],
    }
    # The same email without Google's record of what the group received (no ARC sets).
    bare = {k: v for k, v in headers.items() if not k.startswith("arc-")}
    bare["subject"] = [subject + " (2)"]
    gmail.emails = {
        "1": Email("1", "t1", ["INBOX"], headers, "", "body", 1_700_000_000_000),
        "2": Email("2", "t2", ["INBOX"], bare, "", "body", 1_700_000_000_000),
    }
    for s in (subject, subject + " (2)"):
        ctx.classifier.answers[s] = answer("spam", spam=0.95, action_required=0.03)
    posts = _one_click_posts(ctx)
    triage.run_account(ctx, account, Mode.ACT)

    first, second = ctx.classifier.states
    assert (first["original_sender"], first["sent_via_group"]) == (
        "docs@homescape-lure.example",
        "hello@example.com",
    )
    # The author's verdicts, as Google recorded them when the group received the email; Gmail's
    # own header says spf, dkim and dmarc pass, for the group's re-sent copy.
    assert first["sender_auth"] == {"spf": "softfail", "dkim": "none", "dmarc": "fail"}
    assert second["sender_auth"] == {"spf": "none", "dkim": "none", "dmarc": "none"}
    for gmail_id in ("1", "2"):
        row = _row(ctx, gmail_id)
        assert (row.sender, row.category, row.spammed) == (
            "docs@homescape-lure.example",
            "spam",
            True,
        )
    assert posts == [] and gmail.sent == []
    assert "sent via a group, sender_auth is the original sender's" in CATEGORY_INSTRUCTIONS
