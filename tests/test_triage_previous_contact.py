"""`previous_contact` (the sent-mail search): when it runs and what it changes."""

import pytest
from conftest import make_classification, selects_from
from fakes import (
    email,
    pitch_email,
)

from inbox_jev import rules, triage
from inbox_jev.config import Mode
from inbox_jev.db import SenderRule, Unsubscribe
from inbox_jev.rules import RuleSource
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
    def __init__(self, written=(), fail=False):
        self.written, self.fail, self.queries = set(written), fail, []

    def has_written_to(self, query):
        self.queries.append(query)
        if self.fail:
            raise RuntimeError("boom")
        return query in self.written


@pytest.mark.parametrize(
    ("written", "sender", "expected", "queries"),
    [
        ({"a@corp.com"}, "A@Corp.com", "wrote_to_sender", ["a@corp.com"]),
        ({"corp.com"}, "b@corp.com", "wrote_to_their_domain", ["b@corp.com", "corp.com"]),
        ((), "b@corp.com", "never", ["b@corp.com", "corp.com"]),
        ({"gmail.com"}, "x@gmail.com", "never", ["x@gmail.com"]),  # free mail: no domain check
        ({"acme.com"}, "y@acme.com", "never", ["y@acme.com"]),  # my own domain
        ((), "x@corp.com OR from:me", None, []),  # never builds a query from odd input
        # A machine sender is never searched for itself, only its domain.
        (
            {"messages-noreply@linkedin.com"},
            "messages-noreply@linkedin.com",
            "never",
            ["linkedin.com"],
        ),
        (
            {"linkedin.com"},
            "messages-noreply@linkedin.com",
            "wrote_to_their_domain",
            ["linkedin.com"],
        ),
        ({"bounce@shop.com"}, "bounce@shop.com", "never", ["shop.com"]),
        ({"noreply@gmail.com"}, "noreply@gmail.com", "never", []),  # free mail: nothing to ask
        ({"reply@corp.com"}, "reply@corp.com", "wrote_to_sender", ["reply@corp.com"]),  # human
        # A company called Reply under a two-level public suffix is a person, searched for itself.
        ({"ana@reply.com.ar"}, "ana@reply.com.ar", "wrote_to_sender", ["ana@reply.com.ar"]),
    ],
)
def test_previous_contact(written, sender, expected, queries):
    gmail = ContactGmail(written)
    assert triage.previous_contact(gmail, "me@acme.com", sender) == expected
    assert gmail.queries == queries


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
