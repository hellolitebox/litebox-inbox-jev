"""Sender rules in a pass: corrections, auto rules, stale rules and flags-only Jev calls."""

import pytest
from conftest import make_classification
from fakes import (
    JevDown,
    email,
)

from inbox_jev import digest, jev_guard, rules, triage
from inbox_jev.config import DigestLanguage, Mode
from inbox_jev.db import Message, Run, SenderRule
from inbox_jev.rules import RuleSource


def test_correction_creates_rule(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.ACT)
    # user moves the archived promo back to the inbox
    gmail.emails["2"].label_ids = ["INBOX", "Label_Jev/Marketing"]
    with ctx.sessions() as s:
        s.query(Message).update({Message.correction_checked_at: None})
        s.commit()
    triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        rule = s.query(SenderRule).filter_by(sender_key="promo@shop.com").one()
        assert (rule.category, rule.source, rule.active) == ("important", "correction", True)


def test_rule_to_removed_category_goes_through_jev_and_warns_once(setup, caplog):
    ctx, gmail, account = setup
    gmail.emails["5"] = email("5", "Shop <promo@shop.com>", "Second promo")
    ctx.classifier.answers["Second promo"] = make_classification("marketing", 0.97)
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "promo@shop.com", "promotions", RuleSource.MANUAL)
        s.commit()
    run = triage.run_account(ctx, account, Mode.LABEL)
    assert (run.processed, run.errors) == (5, 0)
    with ctx.sessions() as s:
        rows = {m.gmail_id: m for m in s.query(Message)}
        assert (rows["2"].source, rows["2"].category) == ("jev", "marketing")
        assert (rows["5"].source, rows["5"].category) == ("jev", "marketing")
        rule = s.query(SenderRule).filter_by(sender_key="promo@shop.com").one()
        assert (rule.category, rule.source, rule.active) == ("promotions", "manual", True)
    warned = [
        r for r in caplog.records if r.getMessage() == "sender rule ignored: unknown category"
    ]
    assert len(warned) == 1 and warned[0].sender_key == "promo@shop.com"


def test_stale_exact_rule_sends_sender_to_jev_not_to_the_domain_rule(setup, caplog):
    # promo@shop.com's own rule is the user's exception to @shop.com: with its category gone,
    # Jev decides (and relearns the auto rule); the @shop.com rule doesn't apply.
    ctx, _gmail, account = setup
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "promo@shop.com", "promotions", RuleSource.AUTO)
        rules.set_rule(s, account.email, "@shop.com", "newsletter", RuleSource.MANUAL)
        s.commit()
    triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="2").one()
        assert (row.source, row.category, row.jev_category) == ("jev", "marketing", "marketing")
        rule = s.query(SenderRule).filter_by(sender_key="promo@shop.com").one()
        assert (rule.category, rule.source, rule.hits, rule.active) == (
            "marketing",
            "auto",
            1,
            False,
        )
    warned = [
        r for r in caplog.records if r.getMessage() == "sender rule ignored: unknown category"
    ]
    assert [r.sender_key for r in warned] == ["promo@shop.com"]


def test_flags_only_jev_failure_still_labels_allowlist_and_keep_rule(setup, caplog):
    ctx, gmail, account = setup
    ctx = triage.Context(ctx.settings, ctx.cfg, ctx.sessions, ctx.gmail, classifier=JevDown({}))
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "promo@shop.com", "important", RuleSource.CORRECTION)
        s.commit()
    with pytest.raises(jev_guard.JevUnavailableError):  # 3 failed calls in a row (1, 2, 3) stop Jev
        triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        run = s.query(Run).one()
    assert (run.processed, run.errors) == (2, 1)  # only the Jev-decided email 1 fails
    important = "Label_" + ctx.cfg.by_key["important"].label
    mods = {m[0]: m for m in gmail.modified}
    with ctx.sessions() as s:
        rows = {m.gmail_id: m for m in s.query(Message)}
        for gmail_id, source in (("2", "rule"), ("3", "allowlist")):
            row = rows[gmail_id]
            assert (row.status, row.source, row.category) == ("done", source, "important")
            assert (row.jev_category, row.needs_reply, row.error_attempts) == (None, None, 0)
            assert important in mods[gmail_id][1] and mods[gmail_id][2] == []
            assert "Label_Jev/Needs Reply" not in mods[gmail_id][1]
        assert rows["1"].status == "error" and "4" not in rows  # 4 waits for the next run
    warned = [r for r in caplog.records if r.getMessage() == "flags-only Jev call failed"]
    assert sorted(r.id for r in warned) == ["2", "3"]
    assert {r.error_type for r in warned} == {"TypeSafeError"}
    assert not any("50% off" in r.getMessage() or "Internal" in r.getMessage() for r in warned)


def test_keep_rule_and_allowlist_ask_jev_for_flags_only(setup):
    ctx, gmail, account = setup
    tag = {ctx.cfg.tags[0].key: 0.9}
    ctx.classifier.answers["Internal"] = make_classification("notification", 0.99, 0.9, tag)
    ctx.classifier.answers["50% off"] = make_classification("marketing", 0.99, 0.9)
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "promo@shop.com", "important", RuleSource.CORRECTION)
        rules.set_rule(s, account.email, "w@weird.biz", "newsletter", RuleSource.MANUAL)
        s.commit()
    triage.run_account(ctx, account, Mode.ACT)
    asked = {st["subject"]: st for st in ctx.classifier.states}
    assert set(asked) == {"Proposal", "Internal", "50% off"}  # not the newsletter rule
    assert (
        "previous_contact" not in asked["Internal"] and "previous_contact" not in asked["50% off"]
    )
    assert "promo@shop.com" not in gmail.written_queries
    mods = {m[0]: m for m in gmail.modified}
    for gmail_id in ("2", "3"):
        assert "Label_Jev/Needs Reply" in mods[gmail_id][1] and mods[gmail_id][2] == []
    assert "Label_" + ctx.cfg.tags[0].label in mods["3"][1]
    assert mods["4"][2] == ["INBOX"]  # the newsletter rule archived it, no Jev
    with ctx.sessions() as s:
        rows = {m.gmail_id: m for m in s.query(Message)}
        assert (rows["2"].source, rows["2"].category, rows["2"].confidence) == (
            "rule",
            "important",
            1.0,
        )
        assert (rows["2"].jev_category, rows["2"].needs_reply) == ("marketing", 0.9)
        assert (rows["3"].source, rows["3"].category) == ("allowlist", "important")
        # Flags-only answers never count toward an auto rule ("Internal": notification 0.99).
        assert s.query(SenderRule).filter_by(source="auto").count() == 0
        text = digest.build(s, ctx.cfg, account.email, language=DigestLanguage.ES)
    reply_section = text.split("Piden respuesta")[1]
    assert "50% off" in reply_section and "Internal" in reply_section


def test_dry_run_learns_no_auto_rules(setup):
    ctx, _gmail, account = setup
    for _ in range(3):  # "50% off" (marketing 0.97) would earn a rule in three passes
        with ctx.sessions() as s:
            s.query(Message).delete()
            s.commit()
        triage.run_account(ctx, account, Mode.DRY_RUN)
    with ctx.sessions() as s:
        assert s.query(SenderRule).count() == 0
    triage.run_account(ctx, account, Mode.LABEL)
    with ctx.sessions() as s:
        rule = s.query(SenderRule).one()
        assert (rule.sender_key, rule.hits, rule.active) == ("promo@shop.com", 1, False)
