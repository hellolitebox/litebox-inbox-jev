from datetime import timedelta

import pytest
from conftest import make_classification, make_signals

from inbox_jev import corrections, rules
from inbox_jev.corrections import detect
from inbox_jev.db import Message, make_session_factory, now
from inbox_jev.policy import Decision, decide


@pytest.fixture
def session():
    with make_session_factory("sqlite:///:memory:")() as s:
        yield s


def msg(**kw) -> Message:
    base = dict(category="marketing", low_confidence=False, archived=True, spammed=False)
    base.update(kw)
    return Message(**base)


def test_detect_label_change(cfg):
    assert detect(msg(), {"Jev/Newsletters", "Jev/Processed"}, cfg) == "newsletter"


def test_detect_pulled_back_to_inbox(cfg):
    assert detect(msg(), {"INBOX", "Jev/Marketing"}, cfg) == "important"


def test_detect_accepted_low_confidence_suggestion(cfg):
    m = msg(low_confidence=True, archived=False)
    assert detect(m, {"INBOX", "Jev/Marketing", "Jev/Review"}, cfg) is None
    assert detect(m, {"INBOX", "Jev/Marketing"}, cfg) == "marketing"


def test_detect_untouched(cfg):
    assert detect(msg(), {"Jev/Marketing", "Jev/Processed"}, cfg) is None
    assert (
        detect(msg(archived=False, category="important"), {"INBOX", "Jev/Important"}, cfg) is None
    )


def test_auto_rule_activates_after_consistent_hits(cfg, session):
    c = make_classification("newsletter", 0.99)
    d = Decision(category="newsletter", confidence=0.99, source="jev")
    for _ in range(cfg.auto_rule.hits):
        assert rules.lookup(session, "me@acme.com", "n@sub.com") is None
        rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", c, d)
        session.commit()
    assert rules.lookup(session, "me@acme.com", "n@sub.com").category == "newsletter"


def test_auto_rule_resets_on_disagreement(cfg, session):
    news = Decision(category="newsletter", confidence=0.99, source="jev")
    mkt = Decision(category="marketing", confidence=0.99, source="jev")
    c = make_classification("newsletter", 0.99)
    rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", c, news)
    rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", c, news)
    rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", c, mkt)
    session.commit()
    assert rules.lookup(session, "me@acme.com", "n@sub.com") is None


def test_low_confidence_does_not_count(cfg, session):
    c = make_classification("newsletter", 0.5)
    d = Decision(category="newsletter", confidence=0.5, source="jev")
    for _ in range(5):
        rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", c, d)
    session.commit()
    assert rules.lookup(session, "me@acme.com", "n@sub.com") is None


def test_domain_rule_and_correction_override(cfg, session):
    rules.set_rule(session, "me@acme.com", "@sub.com", "notification", source="manual")
    session.commit()
    assert rules.lookup(session, "me@acme.com", "x@sub.com").category == "notification"
    rules.set_rule(session, "me@acme.com", "x@sub.com", "important", source="correction")
    session.commit()
    assert rules.lookup(session, "me@acme.com", "x@sub.com").category == "important"


def auto_rule(session):
    from sqlalchemy import select

    from inbox_jev.db import SenderRule

    return session.scalar(select(SenderRule).where(SenderRule.sender_key == "n@sub.com"))


def test_group_archive_does_not_count(cfg, session):
    c = make_classification("notification", 0.96)
    d = Decision(category="notification", confidence=0.96, source="jev", archive_group=True)
    for _ in range(5):
        rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", c, d)
    session.commit()
    assert auto_rule(session) is None


@pytest.mark.parametrize("keep", ["important", "action_required"])
def test_keep_probability_blocks_auto_rule(cfg, session, keep):
    c = make_classification("notification", 0.97)
    c.probabilities = {"notification": 0.97, keep: 0.03}
    d = Decision(category="notification", confidence=0.97, source="jev")
    for _ in range(5):
        rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", c, d)
    session.commit()
    assert auto_rule(session) is None


def test_keep_answer_restarts_pending_archive_rule(cfg, session):
    note = make_classification("notification", 0.97)
    d = Decision(category="notification", confidence=0.97, source="jev")
    for _ in range(cfg.auto_rule.hits - 1):
        rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", note, d)
    # "Your token expires tomorrow": a keep answer below the auto-rule confidence.
    act = make_classification("action_required", 0.8)
    act_d = Decision(category="action_required", confidence=0.8, source="jev")
    rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", act, act_d)
    rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", note, d)
    session.commit()
    rule = auto_rule(session)
    assert not rule.active and rule.hits == 1
    assert rules.lookup(session, "me@acme.com", "n@sub.com") is None


def test_keep_signal_restarts_pending_rule_even_when_low_confidence(cfg, session):
    note = make_classification("notification", 0.97)
    d = Decision(category="notification", confidence=0.97, source="jev")
    for _ in range(cfg.auto_rule.hits - 1):
        rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", note, d)
    unsure = make_classification("notification", 0.6)
    unsure.probabilities = {"notification": 0.6, "action_required": 0.4}
    low = Decision(category="notification", confidence=0.6, source="jev", low_confidence=True)
    rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", unsure, low)
    session.commit()
    assert auto_rule(session).hits == 0


def test_keep_answer_leaves_manual_and_correction_rules_alone(cfg, session):
    rules.set_rule(session, "me@acme.com", "n@sub.com", "notification", source="manual")
    session.commit()
    rule = auto_rule(session)
    rule.active = False  # even an inactive manual rule is never touched
    rule.hits = 2
    act = make_classification("action_required", 0.97)
    act_d = Decision(category="action_required", confidence=0.97, source="jev")
    rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", act, act_d)
    session.commit()
    rule = auto_rule(session)
    assert (rule.source, rule.category, rule.hits) == ("manual", "notification", 2)


def test_unverified_cold_outreach_earns_rule_that_never_unsubscribes(cfg, session, account):
    unverified = make_signals(dmarc_pass=False)
    c = make_classification("cold_outreach", 0.99)
    for _ in range(cfg.auto_rule.hits):
        d = decide(cfg, account, unverified, c)
        assert d.downgraded_to_spam and d.archive and not d.unsubscribe
        rules.record_auto(session, cfg, "me@acme.com", "s@pitch.io", c, d)
        session.commit()
    rule = rules.lookup(session, "me@acme.com", "s@pitch.io")
    assert rule.category == "cold_outreach"
    d = decide(cfg, account, unverified, rule_category=rule.category)
    assert d.archive and not d.unsubscribe and d.downgraded_to_spam
    no_header = make_signals(unsubscribe_https=[], unsubscribe_mailto=[])
    assert not decide(cfg, account, no_header, rule_category=rule.category).unsubscribe


def test_unverified_marketing_rule_never_unsubscribes(cfg, session, account):
    unverified = make_signals(dmarc_pass=False)
    c = make_classification("marketing", 0.99)
    for _ in range(cfg.auto_rule.hits):
        rules.record_auto(
            session, cfg, "me@acme.com", "p@shop.com", c, decide(cfg, account, unverified, c)
        )
        session.commit()
    rule = rules.lookup(session, "me@acme.com", "p@shop.com")
    assert rule.category == "marketing"
    d = decide(cfg, account, unverified, rule_category=rule.category)
    assert d.archive and not d.unsubscribe


def test_known_contact_cold_outreach_does_not_earn_rule(cfg, session, account):
    c = make_classification("cold_outreach", 0.99)
    for _ in range(cfg.auto_rule.hits):
        d = decide(cfg, account, make_signals(), c, previous_contact="wrote_to_their_domain")
        rules.record_auto(session, cfg, "me@acme.com", "rep@vendor.com", c, d)
        session.commit()
    assert rules.lookup(session, "me@acme.com", "rep@vendor.com") is None


class _LabelGmail:
    """Gmail that only answers label lookups, recording which emails were checked."""

    def __init__(self):
        self.checked = []

    def labels(self):
        return {}

    def get_label_ids(self, gmail_id):
        self.checked.append(gmail_id)
        return ["Jev/Marketing"]  # untouched


def test_check_puts_never_and_longest_unchecked_rows_first(cfg, session, monkeypatch):
    """With more candidates than the cap, older rows still get checked instead of starving."""
    t = now()
    rows = {
        "new_checked": (t - timedelta(minutes=10), t - timedelta(hours=2)),
        "old_unchecked": (t - timedelta(days=3), None),
        "old_checked_long_ago": (t - timedelta(days=5), t - timedelta(hours=30)),
        "new_unchecked": (t - timedelta(minutes=5), None),
    }
    for gmail_id, (processed_at, checked_at) in rows.items():
        session.add(
            msg(
                account="me@acme.com",
                gmail_id=gmail_id,
                thread_id="t",
                sender="promo@shop.com",
                status="done",
                mode="act",
                processed_at=processed_at,
                correction_checked_at=checked_at,
            )
        )
    session.commit()
    monkeypatch.setattr(corrections, "MAX_PER_RUN", 3)
    gmail = _LabelGmail()
    corrections.check(session, gmail, "me@acme.com", cfg)
    assert gmail.checked == ["new_unchecked", "old_unchecked", "old_checked_long_ago"]
    corrections.check(session, gmail, "me@acme.com", cfg)  # the others are within the hour now
    assert gmail.checked[3:] == ["new_checked"]
