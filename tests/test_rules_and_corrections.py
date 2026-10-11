import pytest
from conftest import make_classification

from inbox_jev import rules
from inbox_jev.corrections import detect
from inbox_jev.db import Message, make_session_factory
from inbox_jev.policy import Decision


@pytest.fixture
def session():
    with make_session_factory("sqlite:///:memory:")() as s:
        yield s


def msg(**kw) -> Message:
    base = dict(category="marketing", low_confidence=False, archived=True, spammed=False)
    base.update(kw)
    return Message(**base)


def test_detect_label_change(cfg):
    assert detect(msg(), {"Jev/Newsletters", "Jev/Procesado"}, cfg) == "newsletter"


def test_detect_pulled_back_to_inbox(cfg):
    assert detect(msg(), {"INBOX", "Jev/Marketing"}, cfg) == "important"


def test_detect_accepted_low_confidence_suggestion(cfg):
    m = msg(low_confidence=True, archived=False)
    assert detect(m, {"INBOX", "Jev/Marketing", "Jev/Revisar"}, cfg) is None
    assert detect(m, {"INBOX", "Jev/Marketing"}, cfg) == "marketing"


def test_detect_untouched(cfg):
    assert detect(msg(), {"Jev/Marketing", "Jev/Procesado"}, cfg) is None
    assert (
        detect(msg(archived=False, category="important"), {"INBOX", "Jev/Importante"}, cfg) is None
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
