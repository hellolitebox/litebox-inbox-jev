"""Worked examples from docs/how-it-decides.md, section 5, run against the real code.

Each `test_exNN_*` matches the doc's `EX-NN` heading. `test_doc_examples_match_tests` fails when
an ID exists on only one side, so the spec and these tests can't drift apart.
"""

import base64
import re
import sys
from pathlib import Path

import httpx
import pytest
from conftest import GMAIL_PASS, make_signals
from test_triage import FakeClassifier, FakeGmail, email

from inbox_jev import digest, rules, triage
from inbox_jev import unsubscribe as unsub
from inbox_jev.classifier import Classification, build_state
from inbox_jev.config import Account, Mode, Settings
from inbox_jev.db import Message, SenderRule, Unsubscribe, make_session_factory
from inbox_jev.gmail import Email, extract_body
from inbox_jev.policy import decide, plan
from inbox_jev.signals import extract

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
def spec(cfg, account, monkeypatch):
    """End-to-end triage with fake Gmail and a fake Jev, as in tests/test_triage.py."""
    gmail = FakeGmail([])
    gmail.written = frozenset()
    monkeypatch.setattr(triage, "Gmail", lambda creds: gmail)
    monkeypatch.setattr(triage, "load_credentials", lambda d, e: None)
    settings = Settings(_env_file=None, typesafe_api_key="x", database_url="sqlite:///:memory:")
    ctx = triage.Context(settings, cfg, make_session_factory(settings.database_url))
    ctx._classifier = FakeClassifier({})
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
        posts.append((str(request.url), request.content.decode()))
        return httpx.Response(200)

    def fake(sig, send_mail):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return real(sig, send_mail, client=client, url_guard=lambda url: True)

    monkeypatch.setattr(triage.unsub, "unsubscribe", fake)
    return posts


# --- EX-01 -----------------------------------------------------------------------------------


def test_ex01_tool_digest_split_is_archived_by_the_group(cfg, account):
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
    sessions = make_session_factory("sqlite:///:memory:")
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
    ctx._classifier.answers["Approve payment to Acme"] = answer(
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
        text = digest.build(s, ctx.cfg, account.email)
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
    ctx._classifier.answers["Re: quick question about your pipeline"] = answer(
        "cold_outreach", cold_outreach=0.96, marketing=0.04
    )
    posts = _one_click_posts(monkeypatch)
    triage.run_account(ctx, account, Mode.ACT)
    return ctx, gmail, posts


def test_ex04_stranger_sequence_is_archived_and_unsubscribed(spec, monkeypatch):
    """EX-04 (docs/how-it-decides.md §5, §3.3 unsubscribe conditions, §4.3 one-click)."""
    ctx, gmail, posts = _run_sequence(spec, monkeypatch)
    assert gmail.written_queries == ["sam@growthagency.example", "growthagency.example"]
    assert ctx._classifier.states[0]["previous_contact"] == "never"
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
    assert ctx._classifier.states[0]["previous_contact"] == "wrote_to_sender"
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
    ctx._classifier.answers["Partnership idea"] = answer("cold_outreach", cold_outreach=0.97)
    posts = _one_click_posts(monkeypatch)
    triage.run_account(ctx, account, Mode.ACT)

    state = ctx._classifier.states[0]  # allow_domains matched the author, not the group
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
def test_ex11_unverified_cold_outreach_is_archived_never_unsubscribed(cfg, account, signals_kw):
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

    sessions = make_session_factory("sqlite:///:memory:")
    with sessions() as s:
        rules.record_auto(s, cfg, account.email, sig.sender, jev, d)
        assert s.query(SenderRule).one().hits == 1  # still counts toward an auto rule

    # Known gap (Jev path): a known contact gets known_contact_kept before the verified check,
    # so the same unverified answer doesn't count toward an auto rule.
    d = decide(cfg, account, sig, jev, previous_contact="wrote_to_sender")
    assert d.archive and d.downgraded_to_spam and d.known_contact_kept and not d.unsubscribe
    with sessions() as s:
        s.query(SenderRule).delete()
        rules.record_auto(s, cfg, account.email, sig.sender, jev, d)
        assert s.query(SenderRule).count() == 0


# --- EX-12 -----------------------------------------------------------------------------------


def test_ex12_pulling_back_to_inbox_becomes_a_rule(spec):
    """EX-12 (docs/how-it-decides.md §5, §3.7 corrections)."""
    ctx, gmail, account = spec
    gmail.emails = {"1": email("1", "Weekly <news@weekly.example>", "Issue 41")}
    ctx._classifier.answers["Issue 41"] = answer("newsletter", newsletter=0.97)
    triage.run_account(ctx, account, Mode.ACT)
    assert gmail.modified[-1][2] == ["INBOX"]

    gmail.emails["1"].label_ids.append("INBOX")  # the user moves it back
    gmail.emails["2"] = email("2", "Weekly <news@weekly.example>", "Issue 42")
    calls = ctx._classifier.calls
    triage.run_account(ctx, account, Mode.ACT)

    assert _row(ctx, "1").corrected_category == "important"
    with ctx.sessions() as s:
        rule = s.query(SenderRule).filter_by(sender_key="news@weekly.example").one()
        assert (rule.category, rule.source, rule.active) == ("important", "correction", True)
    assert ctx._classifier.calls == calls  # the rule decided issue 42
    row = _row(ctx, "2")
    assert (row.category, row.source, row.archived) == ("important", "rule", False)
    assert "Label_Jev/Important" in gmail.modified[-1][1] and gmail.modified[-1][2] == []


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
