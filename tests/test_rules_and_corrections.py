from datetime import timedelta

import pytest
from conftest import make_classification, make_signals, selects_from
from fakes import HttpError

from inbox_jev import corrections, rules
from inbox_jev.config import Account, Mode
from inbox_jev.corrections import detect
from inbox_jev.db import Message, MessageStatus, SenderRule, now, open_sessions
from inbox_jev.policy import Decision, DecisionSource, decide
from inbox_jev.rules import RuleSource


@pytest.fixture
def session():
    with open_sessions("sqlite:///:memory:") as sessions, sessions() as s:
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
    d = Decision(category="newsletter", confidence=0.99, source=DecisionSource.JEV)
    for _ in range(cfg.auto_rule.hits):
        assert rules.lookup(session, "me@acme.com", "n@sub.com") is None
        rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", c, d)
        session.commit()
    assert rules.lookup(session, "me@acme.com", "n@sub.com").category == "newsletter"


def test_auto_rule_resets_on_disagreement(cfg, session):
    news = Decision(category="newsletter", confidence=0.99, source=DecisionSource.JEV)
    mkt = Decision(category="marketing", confidence=0.99, source=DecisionSource.JEV)
    c = make_classification("newsletter", 0.99)
    rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", c, news)
    rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", c, news)
    rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", c, mkt)
    session.commit()
    assert rules.lookup(session, "me@acme.com", "n@sub.com") is None


def test_low_confidence_does_not_count(cfg, session):
    c = make_classification("newsletter", 0.5)
    d = Decision(category="newsletter", confidence=0.5, source=DecisionSource.JEV)
    for _ in range(5):
        rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", c, d)
    session.commit()
    assert rules.lookup(session, "me@acme.com", "n@sub.com") is None


def test_domain_rule_and_correction_override(cfg, session):
    rules.set_rule(session, "me@acme.com", "@sub.com", "notification", source=RuleSource.MANUAL)
    session.commit()
    assert rules.lookup(session, "me@acme.com", "x@sub.com").category == "notification"
    rules.set_rule(session, "me@acme.com", "x@sub.com", "important", source=RuleSource.CORRECTION)
    session.commit()
    assert rules.lookup(session, "me@acme.com", "x@sub.com").category == "important"


def auto_rule(session):
    from sqlalchemy import select

    from inbox_jev.db import SenderRule

    return session.scalar(select(SenderRule).where(SenderRule.sender_key == "n@sub.com"))


def test_group_archive_does_not_count(cfg, session):
    c = make_classification("notification", 0.96)
    d = Decision(
        category="notification", confidence=0.96, source=DecisionSource.JEV, archive_group=True
    )
    for _ in range(5):
        rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", c, d)
    session.commit()
    assert auto_rule(session) is None


@pytest.mark.parametrize("keep", ["important", "action_required"])
def test_keep_probability_blocks_auto_rule(cfg, session, keep):
    c = make_classification("notification", 0.97)
    c.probabilities = {"notification": 0.97, keep: 0.03}
    d = Decision(category="notification", confidence=0.97, source=DecisionSource.JEV)
    for _ in range(5):
        rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", c, d)
    session.commit()
    assert auto_rule(session) is None


def test_keep_answer_restarts_pending_archive_rule(cfg, session):
    note = make_classification("notification", 0.97)
    d = Decision(category="notification", confidence=0.97, source=DecisionSource.JEV)
    for _ in range(cfg.auto_rule.hits - 1):
        rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", note, d)
    # "Your token expires tomorrow": a keep answer below the auto-rule confidence.
    act = make_classification("action_required", 0.8)
    act_d = Decision(category="action_required", confidence=0.8, source=DecisionSource.JEV)
    rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", act, act_d)
    rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", note, d)
    session.commit()
    rule = auto_rule(session)
    assert not rule.active and rule.hits == 1
    assert rules.lookup(session, "me@acme.com", "n@sub.com") is None


def test_keep_signal_restarts_pending_rule_even_when_low_confidence(cfg, session):
    note = make_classification("notification", 0.97)
    d = Decision(category="notification", confidence=0.97, source=DecisionSource.JEV)
    for _ in range(cfg.auto_rule.hits - 1):
        rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", note, d)
    unsure = make_classification("notification", 0.6)
    unsure.probabilities = {"notification": 0.6, "action_required": 0.4}
    low = Decision(
        category="notification", confidence=0.6, source=DecisionSource.JEV, low_confidence=True
    )
    rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", unsure, low)
    session.commit()
    assert auto_rule(session).hits == 0


def test_keep_answer_leaves_manual_and_correction_rules_alone(cfg, session):
    rules.set_rule(session, "me@acme.com", "n@sub.com", "notification", source=RuleSource.MANUAL)
    session.commit()
    rule = auto_rule(session)
    rule.active = False  # even an inactive manual rule is never touched
    rule.hits = 2
    act = make_classification("action_required", 0.97)
    act_d = Decision(category="action_required", confidence=0.97, source=DecisionSource.JEV)
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
        rules.record_auto(
            session, cfg, "me@acme.com", "rep@vendor.com", c, d, "wrote_to_their_domain"
        )
        session.commit()
    assert rules.lookup(session, "me@acme.com", "rep@vendor.com") is None


class _LabelGmail:
    """Gmail that only answers label lookups, recording which emails were checked."""

    def __init__(self, current=None, error=None):
        self.checked = []
        self.calls = 0
        self.current = current or {}
        self.error = error

    def labels(self):
        return {}

    def get_label_ids(self, gmail_ids):
        self.calls += 1
        if self.error:
            raise self.error
        self.checked += gmail_ids
        # untouched unless `current` says otherwise; None: deleted in Gmail
        return {i: self.current.get(i, ["Jev/Marketing"]) for i in gmail_ids}


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
                status=MessageStatus.DONE,
                mode=Mode.ACT,
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


def test_match_never_falls_through_a_stale_exact_rule_to_the_domain_rule(cfg, session):
    rules.set_rule(session, "me@acme.com", "news@shop.com", "promotions", RuleSource.MANUAL)
    rules.set_rule(session, "me@acme.com", "@shop.com", "newsletter", RuleSource.MANUAL)
    session.commit()
    assert rules.lookup(session, "me@acme.com", "news@shop.com").category == "promotions"
    rule, stale = rules.match(session, "me@acme.com", "news@shop.com", cfg.by_key)
    assert rule is None and stale.sender_key == "news@shop.com"  # Jev decides, not @shop.com
    rule, stale = rules.match(session, "me@acme.com", "other@shop.com", cfg.by_key)
    assert (rule.sender_key, stale) == ("@shop.com", None)


def test_match_with_stale_domain_rule_and_no_exact_rule(cfg, session):
    rules.set_rule(session, "me@acme.com", "@shop.com", "promotions", RuleSource.MANUAL)
    session.commit()
    rule, stale = rules.match(session, "me@acme.com", "news@shop.com", cfg.by_key)
    assert rule is None and stale.sender_key == "@shop.com"
    assert rules.match(session, "me@acme.com", "x@other.com", cfg.by_key) == (None, None)


def test_active_auto_rule_to_removed_category_is_relearned(cfg, session):
    rules.set_rule(session, "me@acme.com", "news@shop.com", "promotions", RuleSource.AUTO)
    session.commit()
    c = make_classification("newsletter", 0.99)
    d = decide(cfg, Account(email="me@acme.com"), make_signals(), c)
    rules.record_auto(session, cfg, "me@acme.com", "news@shop.com", c, d)
    rule = session.query(SenderRule).one()
    assert (rule.category, rule.hits, rule.active) == ("newsletter", 1, False)


def test_known_contact_never_earns_cold_outreach_rule_even_unverified(cfg, session, account):
    c = make_classification("cold_outreach", 0.99)
    for sig in (make_signals(dmarc_pass=False), make_signals(is_discussion_list=True)):
        for _ in range(cfg.auto_rule.hits):
            d = decide(cfg, account, sig, c, previous_contact="wrote_to_sender")
            rules.record_auto(
                session, cfg, "me@acme.com", "rep@vendor.com", c, d, "wrote_to_sender"
            )
        assert rules.lookup(session, "me@acme.com", "rep@vendor.com") is None
    # marketing unsubscribes known contacts too, so they can earn a marketing rule.
    m = make_classification("marketing", 0.99)
    for _ in range(cfg.auto_rule.hits):
        d = decide(cfg, account, make_signals(), m, previous_contact="wrote_to_sender")
        rules.record_auto(session, cfg, "me@acme.com", "rep@vendor.com", m, d, "wrote_to_sender")
    assert rules.lookup(session, "me@acme.com", "rep@vendor.com").category == "marketing"


def _checkable(session, *gmail_ids):
    for gmail_id in gmail_ids:
        session.add(
            msg(
                account="me@acme.com",
                gmail_id=gmail_id,
                thread_id="t",
                sender=f"{gmail_id}@shop.com",
                status=MessageStatus.DONE,
                mode=Mode.ACT,
                processed_at=now() - timedelta(hours=2),
            )
        )
    session.commit()


def test_check_reads_every_candidate_in_one_gmail_call(cfg, session):
    _checkable(session, "pulled_back", "deleted", "untouched")
    gmail = _LabelGmail(current={"pulled_back": ["INBOX", "Jev/Marketing"], "deleted": None})
    assert corrections.check(session, gmail, "me@acme.com", cfg) == 1
    assert gmail.calls == 1
    assert sorted(gmail.checked) == ["deleted", "pulled_back", "untouched"]
    rows = {m.gmail_id: m for m in session.query(Message)}
    assert rows["pulled_back"].corrected_category == "important"
    assert rows["deleted"].corrected_category is None
    assert all(m.correction_checked_at is not None for m in rows.values())
    rule = session.query(SenderRule).one()
    assert (rule.sender_key, rule.category) == ("pulled_back@shop.com", "important")


def test_check_without_candidates_learns_nothing(cfg, session):
    gmail = _LabelGmail()
    assert corrections.check(session, gmail, "me@acme.com", cfg) == 0
    assert gmail.checked == []


def test_check_gmail_failure_is_raised_and_changes_no_row(cfg, session):
    _checkable(session, "a", "b")
    gmail = _LabelGmail(current={"a": ["INBOX"]}, error=HttpError(503))
    with pytest.raises(HttpError):
        corrections.check(session, gmail, "me@acme.com", cfg)
    assert all(m.correction_checked_at is None for m in session.query(Message))
    assert session.query(SenderRule).count() == 0


def test_match_reads_address_and_domain_rules_in_one_query(cfg, session):
    rules.set_rule(session, "me@acme.com", "news@shop.com", "promotions", RuleSource.MANUAL)
    rules.set_rule(session, "me@acme.com", "@shop.com", "newsletter", RuleSource.MANUAL)
    session.commit()
    with selects_from(session.get_bind(), "sender_rules") as queries:
        rule, stale = rules.match(session, "me@acme.com", "other@shop.com", cfg.by_key)
    assert rule.sender_key == "@shop.com" and stale is None
    assert len(queries) == 1


def test_an_answer_not_counted_still_resets_a_pending_rule(cfg, session):
    """`count=False` (an email already learned from): no hit, no new rule, but the reset holds."""
    note = make_classification("notification", 0.97)
    d = Decision(category="notification", confidence=0.97, source=DecisionSource.JEV)
    rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", note, d, count=False)
    session.commit()
    assert session.query(SenderRule).count() == 0
    rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", note, d)
    rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", note, d, count=False)
    session.commit()
    assert auto_rule(session).hits == 1
    act = make_classification("action_required", 0.8)
    act_d = Decision(category="action_required", confidence=0.8, source=DecisionSource.JEV)
    rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", act, act_d, count=False)
    session.commit()
    assert auto_rule(session).hits == 0


def test_an_answer_not_counted_in_another_category_restarts_a_pending_rule(cfg, session):
    """A re-answered email (`count=False`) confidently in another category than the pending
    auto rule's: Jev disagrees with itself about one email, so the rule restarts at 0 hits and
    keeps its category (switching it would count the email a second time)."""
    news = make_classification("newsletter", 0.97)
    news_d = Decision(category="newsletter", confidence=0.97, source=DecisionSource.JEV)
    promo = make_classification("marketing", 0.97)
    promo_d = Decision(category="marketing", confidence=0.97, source=DecisionSource.JEV)
    for _ in range(2):
        rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", news, news_d)
    rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", news, news_d, count=False)
    session.commit()
    assert (auto_rule(session).category, auto_rule(session).hits) == ("newsletter", 2)
    rules.record_auto(session, cfg, "me@acme.com", "n@sub.com", promo, promo_d, count=False)
    session.commit()
    rule = auto_rule(session)
    assert (rule.category, rule.hits, rule.active) == ("newsletter", 0, False)
