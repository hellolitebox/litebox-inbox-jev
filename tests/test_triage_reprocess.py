"""Emails seen again: mode switches, `reprocess`, restored rows and user edits (hands off)."""

import pytest
from conftest import make_classification
from fakes import (
    age_errors,
    modifications,
)

from inbox_jev import triage
from inbox_jev.config import Mode
from inbox_jev.db import Message, SenderRule, now


def test_act_reprocesses_label_rows_and_drops_stale_labels(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.LABEL)
    # Jev changes its mind about "50% off" on the second look.
    ctx.classifier.answers["50% off"] = make_classification("newsletter", 0.95)
    gmail.modified.clear()
    run = triage.run_account(ctx, account, Mode.ACT)
    assert run.processed == 4
    _, add, remove = next(m for m in gmail.modified if m[0] == "2")
    assert "Label_Jev/Newsletters" in add
    assert "INBOX" in remove and "Label_Jev/Marketing" in remove
    assert "Label_Jev/Processed" not in remove
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="2").one()
        assert row.archived and row.mode == "act" and "Jev/Marketing" not in row.labels_added
    # third pass: nothing left to do
    assert triage.run_account(ctx, account, Mode.ACT).processed == 0


def test_restored_rows_are_never_reprocessed(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.LABEL)
    # user runs `restore` on "50% off": labels removed, row flagged restored
    gmail.emails["2"].label_ids = ["INBOX"]
    with ctx.sessions() as s:
        s.query(Message).filter_by(gmail_id="2").update({Message.restored: True})
        s.commit()
    gmail.modified.clear()
    account.act_categories = ["marketing"]
    triage.run_account(ctx, account, Mode.ACT)
    assert modifications(gmail, "2") == []
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="2").one()
        assert row.restored and not row.archived and row.mode == "label"


def test_reprocess_follows_category_the_user_changed_since_last_check(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.LABEL)
    calls = ctx.classifier.calls
    # user swaps Jev/Marketing for Jev/Important; corrections.check ran less than an hour ago
    gmail.emails["2"].label_ids = ["INBOX", "Label_Jev/Important", "Label_Jev/Processed"]
    with ctx.sessions() as s:
        s.query(Message).update({Message.correction_checked_at: now()})
        s.commit()
    gmail.modified.clear()
    triage.run_account(ctx, account, Mode.ACT)

    [(_, add, remove)] = modifications(gmail, "2")
    assert "Label_Jev/Important" in add and "Label_Jev/Marketing" not in add
    assert "INBOX" not in remove
    assert gmail.sent == []
    # The new correction rule (important, a keep category) decided "50% off": Jev was only
    # asked for its flags, like for the allowlisted "Internal"; 1 and 4 went through Jev again.
    assert ctx.classifier.calls == calls + 4
    with ctx.sessions() as s:
        rule = s.query(SenderRule).filter_by(sender_key="promo@shop.com").one()
        assert (rule.category, rule.source, rule.active) == ("important", "correction", True)
        row = s.query(Message).filter_by(gmail_id="2").one()
        assert row.corrected_category == "important" and row.category == "important"
        assert row.source == "rule" and not row.archived and row.mode == "act"


def test_reprocess_leaves_alone_email_whose_needs_reply_was_removed(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.LABEL)
    calls = ctx.classifier.calls
    gmail.emails["1"].label_ids.remove("Label_Jev/Needs Reply")
    gmail.modified.clear()
    triage.run_account(ctx, account, Mode.ACT)
    assert modifications(gmail, "1") == []
    assert ctx.classifier.calls == calls + 3  # "1" was not sent to Jev again
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="1").one()
        assert row.status == "done" and row.mode == "act"
        assert "Jev/Needs Reply" in row.labels_added  # unchanged
    # handled in act mode: not revisited
    gmail.modified.clear()
    assert triage.run_account(ctx, account, Mode.ACT).processed == 0
    assert gmail.modified == []


def test_reprocess_error_stays_retryable_and_keeps_old_labels(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.LABEL)
    answers = ctx.classifier.answers
    answers.pop("50% off")
    gmail.modified.clear()
    run = triage.run_account(ctx, account, Mode.ACT)
    assert run.errors == 1 and modifications(gmail, "2") == []
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="2").one()
        assert row.status == "error"
        assert row.labels_added == ["Jev/Marketing", "Jev/Processed"]

    answers["50% off"] = make_classification("newsletter", 0.95)
    age_errors(ctx, 5)
    assert triage.run_account(ctx, account, Mode.ACT).processed == 1
    [(_, add, remove)] = modifications(gmail, "2")
    assert "Label_Jev/Newsletters" in add
    assert "INBOX" in remove and "Label_Jev/Marketing" in remove


def _assert_left_alone_and_not_revisited(ctx, gmail, account, message_id):
    assert modifications(gmail, message_id) == []
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id=message_id).one()
        assert row.status == "done" and row.mode == "act" and not row.archived
    gmail.modified.clear()
    assert triage.run_account(ctx, account, Mode.ACT).processed == 0
    assert gmail.modified == []


def test_reprocess_leaves_alone_email_the_user_added_needs_reply_to(setup):
    ctx, gmail, account = setup
    ctx.classifier.answers["50% off"] = make_classification("newsletter", 0.97)
    triage.run_account(ctx, account, Mode.LABEL)
    # user flags it to reply later (or "Label as" Jev/Needs Reply on the whole conversation)
    gmail.emails["2"].label_ids.append("Label_Jev/Needs Reply")
    gmail.modified.clear()
    account.act_categories = ["transactional", "notification", "newsletter"]
    triage.run_account(ctx, account, Mode.ACT)
    assert "INBOX" in gmail.emails["2"].label_ids
    _assert_left_alone_and_not_revisited(ctx, gmail, account, "2")


def test_reprocess_leaves_alone_email_the_user_added_a_second_category_to(setup):
    ctx, gmail, account = setup
    ctx.classifier.answers["50% off"] = make_classification("newsletter", 0.97)
    triage.run_account(ctx, account, Mode.LABEL)
    gmail.emails["2"].label_ids.append("Label_Jev/Important")
    gmail.modified.clear()
    account.act_categories = ["newsletter"]
    triage.run_account(ctx, account, Mode.ACT)
    assert "INBOX" in gmail.emails["2"].label_ids
    _assert_left_alone_and_not_revisited(ctx, gmail, account, "2")


def test_reprocess_leaves_alone_starred_email(setup):
    ctx, gmail, account = setup
    ctx.classifier.answers["50% off"] = make_classification("newsletter", 0.97)
    ctx.classifier.answers["Maybe promo"] = make_classification("newsletter", 0.97)
    triage.run_account(ctx, account, Mode.LABEL)
    gmail.emails["2"].label_ids.append("STARRED")
    gmail.modified.clear()
    account.act_categories = ["newsletter"]
    triage.run_account(ctx, account, Mode.ACT)
    assert "INBOX" in gmail.emails["2"].label_ids
    [(_, _, remove)] = modifications(gmail, "4")  # an unstarred sibling is still archived
    assert remove == ["INBOX"]
    gmail.modified = [m for m in gmail.modified if m[0] != "4"]
    _assert_left_alone_and_not_revisited(ctx, gmail, account, "2")


def test_starred_email_with_a_correction_teaches_the_rule_and_is_left_alone(setup):
    """Learning the correction is its own step in `process`, before the hands-off check: an
    email both relabeled and starred teaches the sender rule and is still left alone."""
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.LABEL)
    with ctx.sessions() as s:
        # corrections.check already looked at it this hour: process's own step catches it.
        s.query(Message).update({Message.correction_checked_at: now()})
        s.commit()
    relabeled = ["INBOX", "Label_Jev/Newsletters", "Label_Jev/Processed", "STARRED"]
    gmail.emails["2"].label_ids = relabeled
    gmail.modified.clear()
    triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        rule = s.query(SenderRule).filter_by(source="correction").one()
        assert (rule.sender_key, rule.category) == ("promo@shop.com", "newsletter")
        assert s.query(Message).filter_by(gmail_id="2").one().corrected_category == "newsletter"
    _assert_left_alone_and_not_revisited(ctx, gmail, account, "2")


def test_starred_new_email_is_processed(setup):
    ctx, gmail, account = setup
    ctx.classifier.answers["50% off"] = make_classification("newsletter", 0.97)
    gmail.emails["2"].label_ids.append("STARRED")
    account.act_categories = ["newsletter"]
    triage.run_account(ctx, account, Mode.ACT)
    [(_, _, remove)] = modifications(gmail, "2")
    assert remove == ["INBOX"]


def _act_with_only(ctx, account, categories):
    account.act_categories = categories
    return triage.run_account(ctx, account, Mode.ACT)


def test_mark_for_reprocess_redoes_rows_after_widening_act_categories(setup):
    ctx, gmail, account = setup
    # First pass: only notification acts, so marketing rows stay in the inbox as mode=act.
    _act_with_only(ctx, account, ["notification"])
    assert _act_with_only(ctx, account, ["notification", "marketing"]).processed == 0

    with ctx.sessions() as s:
        assert triage.mark_for_reprocess(s, ctx.cfg, account.email, "marketing") == 2  # "2", "4"
    gmail.modified.clear()
    run = _act_with_only(ctx, account, ["notification", "marketing"])
    assert run.processed == 2
    [(_, _, remove)] = modifications(gmail, "2")
    assert remove == ["INBOX"]  # confident marketing archived now
    [(_, _, remove)] = modifications(gmail, "4")
    assert remove == []  # low confidence: still in the inbox
    with ctx.sessions() as s:
        rows = {m.gmail_id: m for m in s.query(Message)}
        assert rows["2"].archived and rows["2"].mode == "act"
        assert not rows["4"].archived and rows["4"].mode == "act"
        # archived now: no longer eligible
        assert triage.mark_for_reprocess(s, ctx.cfg, account.email, "marketing") == 1


def test_mark_for_reprocess_filters(setup):
    ctx, gmail, account = setup
    _act_with_only(ctx, account, ["notification"])
    with ctx.sessions() as s:
        rows = {m.gmail_id: m for m in s.query(Message)}
        rows["2"].restored = True
        s.commit()
        # "4" is low confidence, "2" is restored
        n = triage.mark_for_reprocess(s, ctx.cfg, account.email, "marketing", True)
        assert n == 1
        assert triage.mark_for_reprocess(s, ctx.cfg, "other@x.com", "marketing") == 0
        assert triage.mark_for_reprocess(s, ctx.cfg, account.email, "important") == 2  # "1", "3"
        rows = {m.gmail_id: m for m in s.query(Message)}
        assert rows["2"].mode == "act" and rows["4"].mode == "label"
        with pytest.raises(ValueError, match="unknown category"):
            triage.mark_for_reprocess(s, ctx.cfg, account.email, "nope")


def test_mark_for_reprocess_skips_label_mode_and_error_rows(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.LABEL)
    with ctx.sessions() as s:
        assert triage.mark_for_reprocess(s, ctx.cfg, account.email, "marketing") == 0
