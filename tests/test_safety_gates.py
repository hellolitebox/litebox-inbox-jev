"""The single unsubscribe and act gates (policy.unsubscribe_gate, policy.acts_on).

The cross-path tests run the same scenario through every path that can unsubscribe (the Jev
path and the rule path of `run`, and `unsubscribe-retry`) and assert one outcome, so the paths
can't drift apart again.
"""

from dataclasses import dataclass, field

import pytest
from conftest import make_classification, make_signals
from fakes import as_archived, email, failed_unsub

from inbox_jev import rules, triage, unsubscribe_retry
from inbox_jev.classification import PreviousContact
from inbox_jev.config import Mode
from inbox_jev.db import Message
from inbox_jev.policy import DecisionSource, GateResult, Veto, acts_on, unsubscribe_gate
from inbox_jev.rules import RuleSource


@pytest.mark.parametrize(
    ("mode", "act_categories", "acts"),
    [
        (Mode.ACT, None, True),
        (Mode.ACT, ["cold_outreach"], True),
        (Mode.ACT, ["marketing"], False),
        (Mode.ACT, [], False),
        (Mode.LABEL, None, False),
        (Mode.DRY_RUN, None, False),
        (Mode.LABEL, ["cold_outreach"], False),
    ],
)
def test_acts_on(mode, act_categories, acts):
    assert acts_on(mode, act_categories, "cold_outreach") is acts


@pytest.mark.parametrize(
    ("category", "signals", "contact", "expected"),
    [
        ("marketing", {}, None, GateResult(None)),
        ("cold_outreach", {}, PreviousContact.NEVER, GateResult(None)),
        ("cold_outreach", {}, None, GateResult(None, contact_unknown=True)),
        (
            "marketing",
            {},
            PreviousContact.WROTE_TO_SENDER,
            GateResult(None),
        ),  # marketing spares no one
        ("cold_outreach", {}, PreviousContact.WROTE_TO_SENDER, GateResult(Veto.KNOWN_CONTACT)),
        (
            "cold_outreach",
            {},
            PreviousContact.WROTE_TO_THEIR_DOMAIN,
            GateResult(Veto.KNOWN_CONTACT),
        ),
        ("newsletter", {}, None, GateResult(Veto.NOT_UNSUBSCRIBING)),
        ("important", {}, None, GateResult(Veto.NOT_UNSUBSCRIBING)),
        ("removed_category", {}, None, GateResult(Veto.NOT_UNSUBSCRIBING)),
        ("marketing", {"sender": "vip@partner.com"}, None, GateResult(Veto.ALLOWLISTED)),
        ("marketing", {"dmarc_pass": False}, None, GateResult(Veto.UNVERIFIED)),
        (
            "marketing",
            {"unsubscribe_https": (), "unsubscribe_mailto": ()},
            None,
            GateResult(Veto.UNVERIFIED),
        ),
        ("marketing", {"is_discussion_list": True}, None, GateResult(Veto.GROUP)),
        ("marketing", {"group_author": "dana@pitchco.io"}, None, GateResult(Veto.GROUP)),
        # A trusted relay: `dmarc_pass` is the author's (ADR 0050). Passing or not, never.
        (
            "marketing",
            {
                "sender": "dana@pitchco.io",
                "sender_domain": "pitchco.io",
                "relayed_by": "hi@acme.com",
            },
            None,
            GateResult(Veto.GROUP),
        ),
        (
            "marketing",
            {"sender": "dana@pitchco.io", "relayed_by": "hi@acme.com", "dmarc_pass": False},
            None,
            GateResult(Veto.UNVERIFIED),
        ),
        ("marketing", {"list_id": "news.acme.com"}, None, GateResult(Veto.GROUP)),
    ],
)
def test_unsubscribe_gate(cfg, account, category, signals, contact, expected):
    gate = unsubscribe_gate(
        cfg, account, make_signals(**signals), category, contact, manual_rule=False
    )
    assert gate == expected


@pytest.mark.parametrize(
    ("signals", "manual_rule", "expected"),
    [
        ({}, False, GateResult(Veto.DECIDED_EACH_EMAIL)),
        ({}, True, GateResult(None)),
        ({"sender": "news@vendor.example"}, False, GateResult(None)),  # not listed
        ({"sender": "web@sub.forms.example"}, False, GateResult(Veto.DECIDED_EACH_EMAIL)),
        # The checks after it still apply to a manual rule.
        ({"dmarc_pass": False}, True, GateResult(Veto.UNVERIFIED)),
        ({"is_discussion_list": True}, True, GateResult(Veto.GROUP)),
        # Order: the allowlist first, then the list, then the sender's own checks.
        ({"sender": "vip@partner.com"}, False, GateResult(Veto.ALLOWLISTED)),
        ({"dmarc_pass": False}, False, GateResult(Veto.DECIDED_EACH_EMAIL)),
    ],
)
def test_unsubscribe_gate_spares_a_listed_sender_without_a_manual_rule(
    cfg, account, signals, manual_rule, expected
):
    """A sender in decide_each_email is unsubscribed only on a manual rule (ADR 0045)."""
    listed = account.model_copy(
        update={"decide_each_email": ["bob@agency.io", "@forms.example", "vip@partner.com"]}
    )
    sig = make_signals(**{"sender": "bob@agency.io", **signals})
    assert (
        unsubscribe_gate(cfg, listed, sig, "marketing", None, manual_rule=manual_rule) == expected
    )


@pytest.mark.parametrize(
    ("signals", "contact", "veto"),
    [
        # Several checks fail at once: the first in the gate's order is the one reported.
        ({"sender": "vip@partner.com", "dmarc_pass": False}, None, Veto.ALLOWLISTED),
        (
            {"dmarc_pass": False, "is_discussion_list": True},
            PreviousContact.WROTE_TO_SENDER,
            Veto.UNVERIFIED,
        ),
        ({"is_discussion_list": True}, PreviousContact.WROTE_TO_SENDER, Veto.GROUP),
    ],
)
def test_unsubscribe_gate_reports_the_first_check_that_fails(cfg, account, signals, contact, veto):
    gate = unsubscribe_gate(
        cfg, account, make_signals(**signals), "cold_outreach", contact, manual_rule=False
    )
    assert gate.veto == veto and not gate.allowed and not gate.contact_unknown


@dataclass
class Scenario:
    category: str
    headers: dict = field(default_factory=dict)
    sender: str = "Bob <bob@agency.io>"
    written: frozenset = frozenset()  # what the sent-mail search finds
    mode: Mode = Mode.ACT
    act_categories: list[str] | None = None
    unsubscribed: bool = False
    veto: Veto | None = None  # unsubscribe_gate's answer for the email and its real contact


SCENARIOS = {
    "stranger cold outreach": Scenario("cold_outreach", unsubscribed=True),
    "stranger marketing": Scenario("marketing", unsubscribed=True),
    "marketing from a known contact": Scenario(
        "marketing", written=frozenset({"bob@agency.io"}), unsubscribed=True
    ),
    "no List-Unsubscribe": Scenario(
        "cold_outreach", {"list_unsubscribe": None}, veto=Veto.UNVERIFIED
    ),
    "http-only List-Unsubscribe": Scenario(
        "marketing", {"list_unsubscribe": "<http://agency.io/u>"}, veto=Veto.UNVERIFIED
    ),
    "DMARC fail": Scenario(
        "marketing",
        {"authentication_results": "mx.google.com; dmarc=fail header.from=agency.io"},
        veto=Veto.UNVERIFIED,
    ),
    "discussion list": Scenario(
        "cold_outreach", {"list_post": "<mailto:g@groups.io>"}, veto=Veto.GROUP
    ),
    "Google Groups target": Scenario(
        "marketing",
        {"list_unsubscribe": "<mailto:googlegroups-manage+1+unsubscribe@googlegroups.com>"},
        veto=Veto.GROUP,
    ),
    "own-domain list": Scenario("marketing", {"list_id": "<news.acme.com>"}, veto=Veto.GROUP),
    "wrote to the sender": Scenario(
        "cold_outreach", written=frozenset({"bob@agency.io"}), veto=Veto.KNOWN_CONTACT
    ),
    "wrote to their domain": Scenario(
        "cold_outreach", written=frozenset({"agency.io"}), veto=Veto.KNOWN_CONTACT
    ),
    "allowlisted sender": Scenario(
        "marketing", sender="Boss <vip@partner.com>", veto=Veto.ALLOWLISTED
    ),
    "category not in act_categories": Scenario("cold_outreach", act_categories=["marketing"]),
    "label mode": Scenario("cold_outreach", mode=Mode.LABEL),
}


def _email(sc):
    headers = {"list_unsubscribe": "<mailto:u@agency.io>", **sc.headers}
    headers = {k: v for k, v in headers.items() if v is not None}
    return email("9", sc.sender, "Offer", **headers)


def _jev_path(ctx, gmail, account, sc):
    gmail.emails = {"9": _email(sc)}
    ctx.classifier.answers["Offer"] = make_classification(sc.category, 0.97)
    triage.run_account(ctx, account, sc.mode)


def _rule_path(ctx, gmail, account, sc):
    gmail.emails = {"9": _email(sc)}
    sender = triage.signals_for(gmail.emails["9"], account).sender
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, sender, sc.category, RuleSource.MANUAL)
        s.commit()
    triage.run_account(ctx, account, sc.mode)


def _retry_path(ctx, gmail, account, sc):
    account.mode = sc.mode
    gmail.emails = as_archived(_email(sc))
    sender = triage.signals_for(gmail.emails["9"], account).sender
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "the-list", sc.category, sender=sender, confidence=0.97)
    with ctx.sessions() as s:
        unsubscribe_retry.retry_unsubscribes(ctx, s, gmail, account)


PATHS = {"jev": _jev_path, "rule": _rule_path, "retry": _retry_path}


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("name", SCENARIOS)
def test_every_path_reaches_the_same_unsubscribe_outcome(setup, cfg, name, path):
    ctx, gmail, account = setup
    sc = SCENARIOS[name]
    gmail.written = sc.written
    account.act_categories = sc.act_categories

    PATHS[path](ctx, gmail, account, sc)

    assert bool(gmail.sent) is sc.unsubscribed
    assert gmail.sent == (["u@agency.io"] if sc.unsubscribed else [])


@pytest.mark.parametrize("name", SCENARIOS)
def test_scenarios_match_the_gate(cfg, account, name):
    """Each scenario's outcome is the gate's (plus acts_on), not a path-specific rule."""
    sc = SCENARIOS[name]
    sig = triage.signals_for(_email(sc), account)
    contact = PreviousContact.WROTE_TO_SENDER if sig.sender in sc.written else None
    contact = contact or (
        PreviousContact.WROTE_TO_THEIR_DOMAIN
        if sig.sender_domain in sc.written
        else PreviousContact.NEVER
    )
    gate = unsubscribe_gate(cfg, account, sig, sc.category, contact, manual_rule=False)
    assert gate.veto == sc.veto
    assert sc.unsubscribed is (gate.allowed and acts_on(sc.mode, sc.act_categories, sc.category))


@pytest.mark.parametrize("name", SCENARIOS)
def test_jev_and_rule_paths_store_the_same_unverified_flag(setup, name):
    ctx, gmail, account = setup
    sc = SCENARIOS[name]
    gmail.written = sc.written
    account.act_categories = sc.act_categories
    flags = []
    for path in (_jev_path, _rule_path):
        path(ctx, gmail, account, sc)
        with ctx.sessions() as s:
            row = s.query(Message).filter_by(gmail_id="9").one()
            flags.append(row.downgraded_to_spam)
            s.delete(row)
            s.commit()
    assert flags == [sc.veto == Veto.UNVERIFIED] * 2


# --- a sender in decide_each_email (ADR 0045) -----------------------------------------------

LISTED_VETO = "skipped: sender decided email by email, no manual rule"


def _set_rule(ctx, account, category, source):
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "bob@agency.io", category, source)
        s.commit()


def _ignored_rule_path(source):
    """`run` on a listed sender with a rule of `source` to the scenario's category: ignored,
    so Jev decides (and says the same category)."""

    def path(ctx, gmail, account, sc):
        _set_rule(ctx, account, sc.category, source)
        _jev_path(ctx, gmail, account, sc)

    return path


def _retry_with_manual_rule(ctx, gmail, account, sc):
    _set_rule(ctx, account, sc.category, RuleSource.MANUAL)
    _retry_path(ctx, gmail, account, sc)


def _own_correction_path(ctx, gmail, account, sc):
    """In label, the user relabels Jev's answer to the scenario's category; then act: the
    email's own correction decides it (`triage._own_correction`)."""
    gmail.emails = {"9": _email(sc)}
    ctx.classifier.answers["Offer"] = make_classification("newsletter", 0.97)
    triage.run_account(ctx, account, Mode.LABEL)
    labels = gmail.emails["9"].label_ids
    labels[labels.index("Label_Jev/Newsletters")] = "Label_Jev/Marketing"
    triage.run_account(ctx, account, Mode.ACT)


LISTED_PATHS = {
    "jev": (_jev_path, False),
    "ignored auto rule": (_ignored_rule_path(RuleSource.AUTO), False),
    "ignored correction rule": (_ignored_rule_path(RuleSource.CORRECTION), False),
    "own correction": (_own_correction_path, False),
    "retry of a jev decision": (_retry_path, False),
    "manual rule": (_rule_path, True),
    "retry with a manual rule": (_retry_with_manual_rule, True),
}


@pytest.mark.parametrize("name", LISTED_PATHS)
def test_listed_sender_is_unsubscribed_only_on_a_manual_rule(setup, name):
    """A sender in decide_each_email, verified and with a usable List-Unsubscribe, that Jev (or
    a correction, or a stored decision) calls marketing: no path unsubscribes it except a
    manual rule (`policy.unsubscribe_gate`, Veto.DECIDED_EACH_EMAIL)."""
    ctx, gmail, account = setup
    account.decide_each_email = ["bob@agency.io"]
    gmail.written = frozenset()
    path, unsubscribed = LISTED_PATHS[name]

    path(ctx, gmail, account, Scenario("marketing"))

    assert gmail.sent == (["u@agency.io"] if unsubscribed else [])
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="9").one()
        assert row.category == "marketing" and row.archived  # still archived, just kept on
        assert not row.downgraded_to_spam  # not the unverified flag


@pytest.mark.parametrize(
    ("rule", "stored_source", "outcome"),
    [
        (None, DecisionSource.JEV, LISTED_VETO),
        # Rules the list ignores: as if absent.
        ((RuleSource.AUTO, "marketing"), DecisionSource.RULE, "held: rule since removed"),
        ((RuleSource.CORRECTION, "marketing"), DecisionSource.RULE, "held: rule since removed"),
        ((RuleSource.AUTO, "important"), DecisionSource.JEV, LISTED_VETO),
        ((RuleSource.CORRECTION, "newsletter"), DecisionSource.JEV, LISTED_VETO),
        # A manual rule still decides: a keep one skips, an unsubscribing one retries.
        (
            (RuleSource.MANUAL, "important"),
            DecisionSource.JEV,
            "skipped: sender rule says important",
        ),
        ((RuleSource.MANUAL, "marketing"), DecisionSource.RULE, "ok"),
    ],
)
def test_unsubscribe_retry_of_a_listed_sender(setup, rule, stored_source, outcome):
    """`unsubscribe-retry` reads a listed sender's rules as `run` does (only a manual one) and
    shares its veto: a failed unsubscribe of a stored `marketing` row."""
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    account.decide_each_email = ["bob@agency.io"]
    gmail.written = frozenset()
    if rule is not None:
        _set_rule(ctx, account, rule[1], rule[0])
    gmail.emails = as_archived(_email(Scenario("marketing")))
    with ctx.sessions() as s:
        failed_unsub(
            s, account, "9", "the-list", "marketing", confidence=0.97, source=stored_source
        )
    with ctx.sessions() as s:
        assert unsubscribe_retry.retry_unsubscribes(ctx, s, gmail, account) == [
            ("the-list", outcome)
        ]
    assert gmail.sent == (["u@agency.io"] if outcome == "ok" else [])
