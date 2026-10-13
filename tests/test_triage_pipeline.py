"""End-to-end triage passes: what each mode labels, archives, spams and unsubscribes."""

import re

import pytest
from conftest import make_classification, selects_from
from fakes import (
    age_errors,
    email,
    modifications,
    pitch_email,
)
from sqlalchemy import event

from inbox_jev import digest, rules, triage
from inbox_jev.config import DigestLanguage, Mode
from inbox_jev.db import Message, Unsubscribe
from inbox_jev.rules import RuleSource


def test_act_mode_end_to_end(setup):
    ctx, gmail, account = setup
    run = triage.run_account(ctx, account, Mode.ACT)
    assert (run.processed, run.errors) == (4, 0)
    assert ctx.classifier.calls == 4  # the allowlisted one only for the flags

    mods = {m[0]: m for m in gmail.modified}
    assert "Label_Jev/Needs Reply" in mods["1"][1] and mods["1"][2] == []
    assert mods["2"][2] == ["INBOX"]
    assert "Label_Jev/Review" in mods["4"][1] and mods["4"][2] == []
    assert gmail.sent == ["u@shop.com"]

    with ctx.sessions() as s:
        rows = {m.gmail_id: m for m in s.query(Message)}
        assert rows["2"].archived and rows["2"].category == "marketing"
        assert rows["3"].source == "allowlist"
        assert (rows["3"].category, rows["3"].archived) == ("important", False)
        assert rows["3"].jev_category == "notification"  # Jev's answer, ignored
        assert rows["4"].low_confidence
        unsub = s.query(Unsubscribe).one()
        assert (unsub.list_key, unsub.method, unsub.status) == ("shop.com", "mailto", "ok")

    # second pass: nothing new, no duplicate work
    run2 = triage.run_account(ctx, account, Mode.ACT)
    assert run2.processed == 0 and ctx.classifier.calls == 4


def test_dry_run_touches_nothing_then_label_reprocesses(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.DRY_RUN)
    assert all(add == [] and rem == [] for _, add, rem in gmail.modified)
    assert gmail.sent == []

    with ctx.sessions() as s:
        rows = {m.gmail_id: m for m in s.query(Message)}
        assert "Jev/Needs Reply" in rows["1"].decided_labels
        assert all(m.labels_added == [] for m in rows.values())
        text = digest.build(s, ctx.cfg, account.email, language=DigestLanguage.ES)
    reply_section = text.split("Piden respuesta (1)*", 1)[1].split("\n\n", 1)[0]
    assert "Proposal" in reply_section
    assert not any(subj in reply_section for subj in ("50% off", "Internal", "Maybe promo"))

    run = triage.run_account(ctx, account, Mode.LABEL)
    assert run.processed == 4
    assert all(rem == [] for _, _, rem in gmail.modified)
    assert gmail.sent == []


def test_classifier_error_is_recorded_and_retried(setup):
    ctx, gmail, account = setup
    answers = ctx.classifier.answers
    good = answers.pop("Proposal")
    triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="1").one()
        assert (row.status, row.error_attempts) == ("error", 1)
    answers["Proposal"] = good
    age_errors(ctx, 5)
    run = triage.run_account(ctx, account, Mode.ACT)
    assert run.processed == 1
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="1").one()
        assert (row.status, row.error, row.error_attempts) == ("done", None, 0)


def test_act_categories_gate_actions_end_to_end(setup):
    ctx, gmail, account = setup
    account.act_categories = ["notification"]
    triage.run_account(ctx, account, Mode.ACT)
    mods = {m[0]: m for m in gmail.modified}
    assert "Label_Jev/Marketing" in mods["2"][1] and mods["2"][2] == []  # labeled, not archived
    assert gmail.sent == []  # no unsubscribe
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="2").one()
        assert row.mode == "act" and not row.archived
        assert s.query(Unsubscribe).count() == 0


def test_unknown_act_category_fails_the_run(setup):
    ctx, gmail, account = setup
    account.act_categories = ["notifications"]  # typo
    with pytest.raises(ValueError, match="unknown act_categories"):
        triage.run_account(ctx, account, Mode.ACT)
    assert gmail.modified == []


def test_unverified_marketing_is_archived_not_spammed_or_unsubscribed(setup):
    ctx, gmail, account = setup
    # "Maybe promo" has no List-Unsubscribe: confident marketing is archived, never unsubscribed
    ctx.classifier.answers["Maybe promo"] = make_classification("marketing", 0.97)
    account.act_categories = ["marketing"]
    triage.run_account(ctx, account, Mode.ACT)
    [(_, add, remove)] = modifications(gmail, "4")
    assert remove == ["INBOX"] and "SPAM" not in add
    assert "Label_Jev/Marketing" in add and "Label_Jev/Spam" not in add
    assert gmail.sent == ["u@shop.com"]  # only "50% off", which has List-Unsubscribe
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="4").one()
        assert row.category == "marketing" and row.downgraded_to_spam
        assert row.archived and not row.spammed
        assert s.query(Unsubscribe).filter_by(sender="w@weird.biz").count() == 0


def test_empty_act_categories_acts_on_nothing(setup):
    ctx, gmail, account = setup
    account.act_categories = []
    run = triage.run_account(ctx, account, Mode.ACT)
    assert run.processed == 4
    assert all(remove == [] for _, _, remove in gmail.modified)
    assert gmail.sent == []
    with ctx.sessions() as s:
        assert not any(m.archived or m.spammed for m in s.query(Message))


def test_digest_lists_kept_action_category(setup):
    ctx, gmail, account = setup
    gmail.emails["5"] = email("5", "Bank <alerts@bank.com>", "Approve payment")
    ctx.classifier.answers["Approve payment"] = make_classification("action_required", 0.7)
    triage.run_account(ctx, account, Mode.ACT)
    assert all(m[2] == [] for m in gmail.modified if m[0] == "5")  # stays in the inbox
    with ctx.sessions() as s:
        text = digest.build(s, ctx.cfg, account.email, language=DigestLanguage.ES)
    assert "Jev/Action (1)" in text and "Approve payment" in text


def test_digest_does_not_repeat_kept_emails_in_needs_reply(setup):
    ctx, gmail, account = setup
    gmail.emails["5"] = email("5", "Bank <alerts@bank.com>", "Approve payment")
    ctx.classifier.answers["Approve payment"] = make_classification(
        "action_required", 0.7, needs_reply=0.9
    )
    gmail.emails["6"] = email("6", "Ana <ana@client.com>", "Can we meet?")
    ctx.classifier.answers["Can we meet?"] = make_classification("important", 0.9, needs_reply=0.9)
    triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        text = digest.build(s, ctx.cfg, account.email, language=DigestLanguage.ES)
    assert text.count("Approve payment") == 1
    reply = text.split("Piden respuesta")[1]
    assert "Can we meet?" in reply and "Approve payment" not in reply


def test_unsubscribe_budget_counts_down_to_exhausted():
    budget = triage.UnsubscribeBudget(2)
    assert not budget.exhausted
    budget.spend()
    budget.spend()
    assert budget.exhausted and budget.remaining == 0
    assert triage.UnsubscribeBudget(0).exhausted  # MAX_UNSUBSCRIBES_PER_RUN=0 never unsubscribes


def test_process_looks_up_the_stored_row_once_per_email(setup):
    ctx, gmail, account = setup
    engine = ctx.sessions.kw["bind"]
    by_id = re.compile(r"WHERE messages\.account = \S+ AND messages\.gmail_id = \S+$")
    with selects_from(engine, "messages") as first:
        triage.run_account(ctx, account, Mode.LABEL)  # new rows
    with selects_from(engine, "messages") as again:
        triage.run_account(ctx, account, Mode.ACT)  # the same rows, reprocessed
    assert len([q for q in first if by_id.search(q)]) == 4
    assert len([q for q in again if by_id.search(q)]) == 4


@pytest.mark.parametrize("via_rule", [False, True], ids=["jev", "rule"])
def test_email_row_is_flushed_before_the_unsubscribe_request(setup, monkeypatch, via_rule):
    # The unsubscribe is irreversible: the email's row is written (taking SQLite's write lock)
    # before the request, so a locked DB fails the email before unsubscribing, not after.
    ctx, gmail, account = setup
    if via_rule:
        with ctx.sessions() as s:
            rules.set_rule(s, account.email, "@agency.io", "cold_outreach", RuleSource.MANUAL)
            s.commit()
        gmail.emails = {"9": pitch_email("9")}
    gmail_id = "9" if via_rule else "2"
    written: list[str] = []
    at_unsubscribe: list[list[str]] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO messages") and gmail_id in parameters:
            written.append(statement)

    def spy(sig, send_mail):
        at_unsubscribe.append(list(written))
        return real_unsubscribe(sig, send_mail=send_mail)

    real_unsubscribe = triage.unsub.unsubscribe
    monkeypatch.setattr(triage.unsub, "unsubscribe", spy)
    engine = ctx.sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        triage.run_account(ctx, account, Mode.ACT)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(at_unsubscribe) == 1 and len(at_unsubscribe[0]) == 1


def test_corrections_check_reads_labels_in_one_gmail_call(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.LABEL)
    with ctx.sessions() as s:
        s.query(Message).update({Message.correction_checked_at: None})
        s.commit()
    triage.run_account(ctx, account, Mode.LABEL)
    assert sorted(gmail.label_batches[-1]) == ["1", "2", "3", "4"]
