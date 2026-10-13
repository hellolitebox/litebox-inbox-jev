"""Group mail: keyed on the author for trusted groups, on the group otherwise; backfill."""

import pytest
from conftest import make_classification
from fakes import (
    HttpError,
    email,
)

from inbox_jev import triage
from inbox_jev.config import Mode
from inbox_jev.db import Message, SenderRule, now


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
        m.setattr(triage, "signals_for", lambda email, account: email.signals)
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
        m.setattr(triage, "signals_for", lambda email, account: email.signals)
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
        m.setattr(triage, "signals_for", lambda email, account: email.signals)
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
