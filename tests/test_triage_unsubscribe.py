"""`unsubscribe-retry` (`triage.retry_unsubscribes`): the checks `run` applies, again."""

import pytest
from fakes import (
    HttpError,
    as_archived,
    email,
    failed_unsub,
    pitch_email,
)

from inbox_jev import rules, triage
from inbox_jev.config import Mode
from inbox_jev.db import Message, Unsubscribe
from inbox_jev.policy import DecisionSource
from inbox_jev.rules import RuleSource


def _retry(ctx, gmail, account):
    with ctx.sessions() as s:
        out = triage.retry_unsubscribes(ctx, s, gmail, account)
        rows = {r.list_key: (r.status, r.attempts) for r in s.query(Unsubscribe)}
    return dict(out), rows


def test_retry_unsubscribes_verified_stranger(setup):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = as_archived(pitch_email("9"))
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach")
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": "ok"} and rows["agency.io"] == ("ok", 2)
    assert gmail.sent == ["u@agency.io"] and gmail.written_queries == ["bob@agency.io", "agency.io"]


@pytest.mark.parametrize(
    ("headers", "written", "reason"),
    [
        (
            {"list_post": "<mailto:g@groups.io>"},
            frozenset(),
            "unverified sender, group or own list",
        ),
        ({"unsubscribe": None}, frozenset(), "unverified sender, group or own list"),
        ({}, frozenset({"bob@agency.io"}), "known contact"),
        ({}, frozenset({"agency.io"}), "known contact"),
    ],
)
def test_retry_skips_what_run_would_spare(setup, headers, written, reason):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.written = written
    gmail.emails = as_archived(pitch_email("9", **headers))
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach")
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": f"skipped: {reason}"}
    assert rows["agency.io"] == ("skipped", 1) and gmail.sent == []


def test_retry_skips_allowlisted_and_group_relayed(setup):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = as_archived(
        email("7", "Boss <vip@partner.com>", "Promo", list_unsubscribe="<mailto:u@p.com>"),
        email(
            "8",
            "\"'Dana' via Hello\" <hello@acme.com>",
            "Pitch",
            x_original_sender="dana@pitchco.io",
            mailing_list="list hello@acme.com; contact hello+owners@acme.com",
            list_unsubscribe="<mailto:x@pitchco.io>",
        ),
    )
    with ctx.sessions() as s:
        failed_unsub(s, account, "7", "partner.com", "marketing", sender="vip@partner.com")
        failed_unsub(s, account, "8", "pitchco.io", "cold_outreach", sender="dana@pitchco.io")
    out, rows = _retry(ctx, gmail, account)
    assert out == {
        "partner.com": "skipped: allowlisted sender",
        "pitchco.io": "skipped: unverified sender, group or own list",
    }
    assert gmail.sent == []


@pytest.mark.parametrize(
    ("mode", "act_categories", "category", "held"),
    [
        (Mode.LABEL, None, "cold_outreach", "held: mode is label"),
        (
            Mode.ACT,
            ["marketing"],
            "cold_outreach",
            "held: category cold_outreach doesn't unsubscribe here",
        ),
        (Mode.ACT, None, "newsletter", "held: category newsletter doesn't unsubscribe here"),
    ],
)
def test_retry_holds_rows_run_would_not_act_on(setup, mode, act_categories, category, held):
    ctx, gmail, account = setup
    account.mode, account.act_categories = mode, act_categories
    gmail.emails = as_archived(pitch_email("9"))
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", category)
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": held} and rows["agency.io"] == ("failed", 1)
    assert gmail.sent == [] and gmail.written_queries == []


def test_retry_respects_run_cap(setup):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    ctx.settings.max_unsubscribes_per_run = 1
    gmail.emails = as_archived(
        email("8", "A <a@one.io>", "P1", list_unsubscribe="<mailto:u@one.io>"),
        email("9", "B <b@two.io>", "P2", list_unsubscribe="<mailto:u@two.io>"),
    )
    with ctx.sessions() as s:
        failed_unsub(s, account, "8", "one.io", "marketing", sender="a@one.io")
        failed_unsub(s, account, "9", "two.io", "marketing", sender="b@two.io")
    out, _ = _retry(ctx, gmail, account)
    assert sorted(out.values()) == ["held: run cap reached", "ok"] and len(gmail.sent) == 1


def test_retry_holds_deleted_email_and_raises_other_errors(setup, monkeypatch):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach")

    def gone(message_id):
        raise HttpError(404)

    monkeypatch.setattr(gmail, "get", gone)
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": "held: email deleted in Gmail"} and rows["agency.io"] == (
        "failed",
        1,
    )

    def boom(message_id):
        raise RuntimeError("quota")

    monkeypatch.setattr(gmail, "get", boom)
    with pytest.raises(RuntimeError):
        _retry(ctx, gmail, account)


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"restored": True}, "restored by the user"),
        ({"corrected_category": "important"}, "corrected to important"),
    ],
)
def test_retry_skips_user_decisions_before_gmail(setup, monkeypatch, fields, reason):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach", **fields)
    monkeypatch.setattr(gmail, "get", lambda message_id: pytest.fail("no Gmail call expected"))
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": f"skipped: {reason}"} and rows["agency.io"] == ("skipped", 1)
    assert gmail.sent == []


@pytest.mark.parametrize("source", [RuleSource.MANUAL, RuleSource.CORRECTION])
def test_retry_follows_current_sender_rule(setup, source):
    # The user (or a correction) has since said Bob's mail is important: run would keep it.
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = as_archived(pitch_email("9"))
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach")
        rules.set_rule(s, account.email, "@agency.io", "important", source=source)
        s.commit()
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": "skipped: sender rule says important"}
    assert rows["agency.io"] == ("skipped", 1) and gmail.sent == []


def test_retry_rule_to_unsubscribing_category_skips_confidence_check(setup):
    # As in run, a rule decides with confidence 1.0: Jev's stored 0.5 no longer matters.
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = as_archived(pitch_email("9"))
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach", confidence=0.5)
        rules.set_rule(s, account.email, "bob@agency.io", "marketing", source=RuleSource.MANUAL)
        s.commit()
    out, _ = _retry(ctx, gmail, account)
    assert out == {"agency.io": "ok"} and gmail.sent == ["u@agency.io"]


def test_retry_skips_email_the_user_touched_in_gmail(setup):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    label_ids = gmail.ensure_labels(ctx.cfg.all_labels())
    pulled_back = pitch_email("7")  # still has INBOX: dragged back after the archive
    starred = as_archived(pitch_email("8"))["8"]
    starred.label_ids.append("STARRED")
    relabeled = as_archived(pitch_email("9"))["9"]
    relabeled.label_ids.append(label_ids[ctx.cfg.by_key["newsletter"].label])
    gmail.emails = {e.id: e for e in (pulled_back, starred, relabeled)}
    with ctx.sessions() as s:
        failed_unsub(s, account, "7", "a.io", "cold_outreach")
        failed_unsub(s, account, "8", "b.io", "cold_outreach")
        failed_unsub(s, account, "9", "c.io", "cold_outreach")
    out, rows = _retry(ctx, gmail, account)
    assert out == {
        "a.io": "skipped: corrected in Gmail to important",
        "b.io": "skipped: starred",
        "c.io": "skipped: corrected in Gmail to newsletter",
    }
    assert {r[0] for r in rows.values()} == {"skipped"} and gmail.sent == []


@pytest.mark.parametrize(
    ("live", "outcome"),
    [
        (["Jev/Outreach", "Jev/Processed"], "ok"),  # untouched since the archive
        (
            ["Jev/Outreach", "Jev/Processed", "Jev/Important"],
            "skipped: label added in Gmail: Jev/Important",
        ),
        (["Jev/Processed"], "skipped: label removed in Gmail: Jev/Outreach"),
        (["Jev/Outreach", "Jev/Processed", "SPAM"], "skipped: reported as spam"),
        (["Jev/Outreach", "Jev/Processed", "TRASH"], "skipped: in the trash"),
    ],
)
def test_retry_skips_edits_run_leaves_alone(setup, live, outcome):
    # Two category labels or none: corrections.detect sees nothing, _user_edit does.
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    label_ids = gmail.ensure_labels(ctx.cfg.all_labels())
    pitch = as_archived(pitch_email("9"))["9"]
    pitch.label_ids += [label_ids.get(n, n) for n in live]
    gmail.emails = {"9": pitch}
    with ctx.sessions() as s:
        failed_unsub(
            s,
            account,
            "9",
            "agency.io",
            "cold_outreach",
            labels_added=["Jev/Outreach", "Jev/Processed"],
        )
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": outcome}
    if outcome == "ok":
        assert gmail.sent == ["u@agency.io"]
    else:
        assert rows["agency.io"] == ("skipped", 1) and gmail.sent == []


@pytest.mark.parametrize("source", [DecisionSource.RULE, None])
def test_retry_holds_rule_decision_whose_rule_was_removed(setup, source):
    # `rules delete` means "Jev decides again": the deleted rule's 1.0 must not unsubscribe.
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = as_archived(pitch_email("9"))
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach", source=source, confidence=1.0)
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": "held: rule since removed"} and rows["agency.io"] == ("failed", 1)
    assert gmail.sent == [] and gmail.written_queries == []


@pytest.mark.parametrize(
    ("fields", "override"),
    [
        ({"confidence": 0.85}, {}),  # below categories.yaml 0.90
        ({"confidence": 0.92}, {"cold_outreach": 0.95}),  # account raised the bar since
        ({"confidence": 0.97, "low_confidence": True}, {}),
        ({"confidence": None}, {}),
    ],
)
def test_retry_holds_jev_answers_below_current_threshold(setup, fields, override):
    ctx, gmail, account = setup
    account.mode, account.min_confidence = Mode.ACT, override
    gmail.emails = as_archived(pitch_email("9"))
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach", **fields)
    out, rows = _retry(ctx, gmail, account)
    assert out["agency.io"].startswith("held: confidence")
    assert rows["agency.io"] == ("failed", 1) and gmail.sent == []


def test_retry_holds_when_sent_mail_search_fails(setup, monkeypatch):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = as_archived(pitch_email("9"))
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach")

    def broken(query):
        raise RuntimeError("search failed")

    monkeypatch.setattr(gmail, "has_written_to", broken)
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": "held: sent-mail search failed"}
    assert rows["agency.io"] == ("failed", 1) and gmail.sent == []


def test_retry_holds_row_without_stored_message(setup):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach")
        s.query(Message).filter_by(gmail_id="9").delete()
        s.commit()
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": "held: no stored message"} and rows["agency.io"] == ("failed", 1)


def test_retry_holds_rule_decision_whose_exact_rule_went_stale(setup):
    # The @agency.io rule would unsubscribe, but bob's own (now stale) rule overrides it.
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = as_archived(pitch_email("9"))
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "bob@agency.io", "promotions", RuleSource.MANUAL)
        rules.set_rule(s, account.email, "@agency.io", "cold_outreach", RuleSource.MANUAL)
        failed_unsub(
            s, account, "9", "agency.io", "cold_outreach", source=DecisionSource.RULE, confidence=1
        )
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": "held: rule since removed"} and rows["agency.io"] == ("failed", 1)
    assert gmail.sent == []
