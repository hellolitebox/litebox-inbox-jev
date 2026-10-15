"""Group mail: keyed on the author for trusted groups, on the group otherwise; backfill."""

import pytest
from conftest import make_classification
from fakes import (
    HttpError,
    email,
)

from inbox_jev import corrections, rules, triage
from inbox_jev.config import Mode
from inbox_jev.db import Message, SenderRule, now
from inbox_jev.rules import RuleSource


def test_group_relayed_email_is_keyed_on_its_author(setup):
    ctx, gmail, account = setup
    gmail.emails["6"] = email(
        "6",
        "\"'Dana Lee' via Hello\" <hello@acme.com>",
        "Partnership idea",
        x_original_sender="dana@pitchco.io",
        mailing_list="list hello@acme.com; contact hello+owners@acme.com",
        list_id="<hello.acme.com>",
    )
    ctx.classifier.answers["Partnership idea"] = make_classification("cold_outreach", 0.99)
    triage.run_account(ctx, account, Mode.ACT)
    # acme.com is allowlisted, but the author isn't: Jev classified it.
    state = next(s for s in ctx.classifier.states if s["subject"] == "Partnership idea")
    assert state["original_sender"] == "dana@pitchco.io"
    assert state["sent_via_group"] == "hello@acme.com"
    assert "dana@pitchco.io" in gmail.written_queries
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="6").one()
        assert (row.sender, row.category, row.archived) == (
            "dana@pitchco.io",
            "cold_outreach",
            True,
        )
        assert {r.sender_key for r in s.query(SenderRule)} & {"hello@acme.com"} == set()
    assert gmail.sent == ["u@shop.com"]  # only the earlier marketing unsubscribe, none via group


def _group_email(id, subject, author="dana@pitchco.io", group="hello@acme.com"):
    domain = group.rsplit("@", 1)[1]
    return email(
        id,
        f"\"'Dana Lee' via Hello\" <{group}>",
        subject,
        x_original_sender=author,
        mailing_list=f"list {group}; contact owners@{domain}",
        list_id=f"<{group.replace('@', '.')}>",
        authentication_results=f"mx.google.com; dmarc=pass header.from={domain}",
    )


def test_external_group_email_keeps_the_group_as_sender(setup):
    ctx, gmail, account = setup
    gmail.emails = {"6": _group_email("6", "Hi", author="ceo@acme.com", group="news@evil.com")}
    ctx.classifier.answers["Hi"] = make_classification("spam", 0.99)
    triage.run_account(ctx, account, Mode.ACT)
    state = ctx.classifier.states[0]  # not allowlisted as ceo@acme.com: Jev decided
    assert "original_sender" not in state
    with ctx.sessions() as s:
        assert s.query(Message).filter_by(gmail_id="6").one().sender == "news@evil.com"


def test_correction_on_old_group_row_creates_rule_on_author(setup, monkeypatch):
    ctx, gmail, account = setup
    gmail.emails = {"6": _group_email("6", "Pitch")}
    ctx.classifier.answers["Pitch"] = make_classification("cold_outreach", 0.99)
    # Simulate a row stored before authors were recovered: sender was the group.
    with monkeypatch.context() as m:
        m.setattr(triage, "signals_for", lambda email, account: email.extract_signals())
        triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        assert s.query(Message).filter_by(gmail_id="6").one().sender == "hello@acme.com"
        assert triage.backfill_senders(s, gmail, account) == (1, 1, 0)
        assert triage.backfill_senders(s, gmail, account) == (1, 0, 0)  # idempotent
        s.query(Message).update({Message.correction_checked_at: None})
        s.commit()
    gmail.emails["6"].label_ids = ["INBOX", "Label_Jev/Outreach"]  # pulled back
    triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        keys = {r.sender_key: r.source for r in s.query(SenderRule)}
        assert keys.get("dana@pitchco.io") == "correction"
        assert "hello@acme.com" not in keys


def test_process_refreshes_stored_sender_before_checking_user_edits(setup, monkeypatch):
    ctx, gmail, account = setup
    gmail.emails = {"6": _group_email("6", "Pitch")}
    ctx.classifier.answers["Pitch"] = make_classification("cold_outreach", 0.99)
    # A row stored before authors were recovered: the group (on the allowlisted own domain).
    with monkeypatch.context() as m:
        m.setattr(triage, "signals_for", lambda email, account: email.extract_signals())
        triage.run_account(ctx, account, Mode.LABEL)
    with ctx.sessions() as s:
        # corrections.check already looked at it this hour: process's own check catches it.
        s.query(Message).update({Message.correction_checked_at: now()})
        s.commit()
    gmail.emails["6"].label_ids = ["INBOX", "Label_Jev/Marketing", "Label_Jev/Processed"]
    triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        rule = s.query(SenderRule).filter_by(source="correction").one()
        assert (rule.sender_key, rule.category) == ("dana@pitchco.io", "marketing")


def _old_group_rows(ctx, gmail, account, monkeypatch, ids):
    gmail.emails = {i: _group_email(i, f"Pitch {i}") for i in ids}
    for i in ids:
        ctx.classifier.answers[f"Pitch {i}"] = make_classification("cold_outreach", 0.99)
    with monkeypatch.context() as m:
        m.setattr(triage, "signals_for", lambda email, account: email.extract_signals())
        triage.run_account(ctx, account, Mode.LABEL)


def test_backfill_skips_only_deleted_emails(setup, monkeypatch):
    ctx, gmail, account = setup
    _old_group_rows(ctx, gmail, account, monkeypatch, ["6", "7"])
    gmail.header_errors["6"] = HttpError(404)
    with ctx.sessions() as s:
        assert triage.backfill_senders(s, gmail, account) == (2, 1, 1)
        senders = {m.gmail_id: m.sender for m in s.query(Message).filter(Message.gmail_id >= "6")}
    assert senders == {"6": "hello@acme.com", "7": "dana@pitchco.io"}


@pytest.mark.parametrize("error", [HttpError(401), HttpError(429), HttpError(503), OSError()])
def test_backfill_raises_other_gmail_errors(setup, monkeypatch, error):
    ctx, gmail, account = setup
    _old_group_rows(ctx, gmail, account, monkeypatch, ["6"])
    gmail.header_errors["6"] = error
    with ctx.sessions() as s, pytest.raises(type(error)):
        triage.backfill_senders(s, gmail, account)


def test_group_on_unlisted_public_provider_is_not_trusted(setup):
    # Without group_domains nothing is trusted, even the account's own (public) domain:
    # anyone at mac.com can pass DMARC and write the Google Groups headers.
    ctx, gmail, account = setup
    account.email, account.group_domains = "me@mac.com", []
    account.allow_senders = ["ceo@bank.com"]
    gmail.emails = {"6": _group_email("6", "Hi", author="ceo@bank.com", group="attacker@mac.com")}
    ctx.classifier.answers["Hi"] = make_classification("spam", 0.99)
    triage.run_account(ctx, account, Mode.ACT)
    assert "original_sender" not in ctx.classifier.states[0]  # not allowlisted: Jev decided
    with ctx.sessions() as s:
        assert s.query(Message).filter_by(gmail_id="6").one().sender == "attacker@mac.com"


# --- Mail sent as a list's own address (ADR 0051) ---------------------------------------------


def _as_list(id, subject, address="hello@acme.com", **headers):
    """An email the list sent as itself: its From is the address its List-Post names."""
    return email(id, f"Hello <{address}>", subject, list_post=f"<mailto:{address}>", **headers)


@pytest.fixture
def unlisted(setup):
    """`setup` with an account that allowlists nothing, so rules and Jev decide acme.com mail."""
    ctx, gmail, account = setup
    gmail.emails = {}
    return ctx, gmail, account.model_copy(update={"allow_domains": [], "allow_senders": []})


def _rules(ctx):
    with ctx.sessions() as s:
        return {r.sender_key: (r.source, r.category, r.active) for r in s.query(SenderRule)}


def test_three_confident_answers_teach_no_auto_rule_on_a_list_address(unlisted):
    ctx, gmail, account = unlisted
    for i in "123":
        gmail.emails[i] = _as_list(i, f"Digest {i}")
        ctx.classifier.answers[f"Digest {i}"] = make_classification("newsletter", 0.99)
    triage.run_account(ctx, account, Mode.ACT)
    assert len(ctx.classifier.states) == 3
    assert _rules(ctx) == {}


def test_a_colleague_on_the_group_domain_still_earns_an_auto_rule(unlisted):
    ctx, gmail, account = unlisted
    for i in "123":
        gmail.emails[i] = email(i, "Mia <mia@acme.com>", f"Report {i}")
        ctx.classifier.answers[f"Report {i}"] = make_classification("important", 0.99)
    triage.run_account(ctx, account, Mode.ACT)
    assert _rules(ctx) == {"mia@acme.com": ("auto", "important", True)}


@pytest.mark.parametrize("source", [RuleSource.AUTO, RuleSource.CORRECTION])
def test_a_learned_rule_on_a_list_address_decides_only_its_direct_mail(unlisted, source):
    ctx, gmail, account = unlisted
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "hello@acme.com", "spam", source)
        s.commit()
    gmail.emails = {
        "1": _as_list("1", "Question about invoice 7"),
        # The same address writing directly (no list headers): the rule still decides.
        "2": email("2", "Hello <hello@acme.com>", "Sent from the shared inbox"),
    }
    ctx.classifier.answers["Question about invoice 7"] = make_classification("important", 0.97)
    triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        rows = {m.gmail_id: m for m in s.query(Message)}
        assert (rows["1"].source, rows["1"].category, rows["1"].spammed) == (
            "jev",
            "important",
            False,
        )
        assert (rows["2"].source, rows["2"].category, rows["2"].spammed) == ("rule", "spam", True)
    assert _rules(ctx)["hello@acme.com"] == (source.value, "spam", True)  # kept, inert


def test_a_correction_on_a_list_address_email_decides_it_in_a_stronger_mode(unlisted):
    """In label, the user swaps Jev/Important for Jev/Newsletters on mail sent as the list; when
    the account moves to act, that email is decided by its own correction (no rule is learned),
    not by Jev's answer again."""
    ctx, gmail, account = unlisted
    gmail.emails = {"1": _as_list("1", "Monthly update")}
    ctx.classifier.answers["Monthly update"] = make_classification("important", 0.97)
    triage.run_account(ctx, account, Mode.LABEL)
    labels = gmail.emails["1"].label_ids
    labels[labels.index("Label_Jev/Important")] = "Label_Jev/Newsletters"
    triage.run_account(ctx, account, Mode.ACT)

    assert len(ctx.classifier.states) == 1  # Jev was asked in label only
    assert _rules(ctx) == {}
    with ctx.sessions() as s:
        row = s.query(Message).one()
        assert (row.corrected_category, row.category, row.source, row.archived) == (
            "newsletter",
            "newsletter",
            "rule",
            True,
        )


def test_a_manual_rule_on_a_list_address_still_decides(unlisted):
    ctx, gmail, account = unlisted
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "hello@acme.com", "newsletter", RuleSource.MANUAL)
        s.commit()
    gmail.emails = {"1": _as_list("1", "Weekly digest")}
    triage.run_account(ctx, account, Mode.ACT)
    assert ctx.classifier.states == []
    with ctx.sessions() as s:
        row = s.query(Message).one()
        assert (row.source, row.category, row.archived) == ("rule", "newsletter", True)


def test_a_newsletter_list_is_its_own_address_an_esp_newsletter_is_not(unlisted):
    """A list posting as itself (an announce list naming its own List-Post) learns no rule; an
    ESP newsletter (no List-Post) does."""
    ctx, gmail, account = unlisted
    announce = "news@lists.vendor.example"
    for i in "123":
        gmail.emails[f"a{i}"] = _as_list(f"a{i}", f"Release notes {i}", announce)
        gmail.emails[f"e{i}"] = email(f"e{i}", "Shop <deals@shop.com>", f"Deals {i}")
        ctx.classifier.answers[f"Release notes {i}"] = make_classification("newsletter", 0.99)
        ctx.classifier.answers[f"Deals {i}"] = make_classification("newsletter", 0.99)
    triage.run_account(ctx, account, Mode.ACT)
    assert _rules(ctx) == {"deals@shop.com": ("auto", "newsletter", True)}


def test_correction_check_on_a_list_address_row_creates_no_rule(unlisted):
    """`corrections.check` reads the stored flag: a row sent as the list makes no rule; a row
    stored before the flag existed (None) still does."""
    ctx, gmail, account = unlisted
    gmail.emails = {
        "1": _as_list("1", "Lure"),
        "2": email("2", "Old <old@acme.com>", "Old row"),
    }
    ctx.classifier.answers["Lure"] = make_classification("cold_outreach", 0.97)
    ctx.classifier.answers["Old row"] = make_classification("cold_outreach", 0.97)
    triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        s.query(Message).filter_by(gmail_id="2").update({Message.sender_is_list: None})
        s.commit()
    gmail.emails["1"].label_ids = ["Label_Jev/Spam"]
    gmail.emails["2"].label_ids = ["Label_Jev/Spam"]
    with ctx.sessions() as s:
        assert corrections.check(s, gmail, account, ctx.cfg) == 2
    assert _rules(ctx) == {"old@acme.com": ("correction", "spam", True)}


def test_backfill_stores_whether_the_sender_is_the_list(setup, monkeypatch):
    ctx, gmail, account = setup
    gmail.emails = {"6": _group_email("6", "Pitch")}
    ctx.classifier.answers["Pitch"] = make_classification("cold_outreach", 0.99)
    with monkeypatch.context() as m:  # a row stored with neither the author nor the flag
        m.setattr(triage, "signals_for", lambda email, account: email.extract_signals())
        triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        s.query(Message).update({Message.sender_is_list: None})
        s.commit()
        assert triage.backfill_senders(s, gmail, account) == (1, 1, 0)
        row = s.query(Message).one()
        assert (row.sender, row.sender_is_list) == ("dana@pitchco.io", False)


def test_backfill_flags_a_list_named_only_by_list_post(setup):
    """A non-Google list (Mailman, groups.io) names itself only in List-Post: backfill reads that
    header too, so its own-address rows are flagged."""
    ctx, gmail, account = setup
    address = "dev@lists.example.org"
    gmail.emails = {"6": _as_list("6", "Release vote", address)}
    gmail.emails["6"].headers["from"] = [f"Dana Lee via Dev <{address}>"]
    ctx.classifier.answers["Release vote"] = make_classification("newsletter", 0.99)
    triage.run_account(ctx, account, Mode.LABEL)
    with ctx.sessions() as s:
        s.query(Message).update({Message.sender_is_list: None})
        s.commit()
        assert triage.backfill_senders(s, gmail, account) == (1, 1, 0)
        row = s.query(Message).one()
        assert (row.sender, row.sender_is_list) == (address, True)
