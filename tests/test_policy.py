import pytest
from conftest import make_classification, make_signals

from inbox_jev.config import Mode
from inbox_jev.policy import decide, plan, rule_keys


def test_allowlist_skips_everything(cfg, account):
    d = decide(cfg, account, make_signals(sender="ceo@acme.com", sender_domain="acme.com"))
    assert (d.category, d.source, d.archive, d.unsubscribe) == (
        "important",
        "allowlist",
        False,
        False,
    )
    d = decide(cfg, account, make_signals(sender="x@eu.acme.com", sender_domain="eu.acme.com"))
    assert d.source == "allowlist"
    d = decide(cfg, account, make_signals(sender="vip@partner.com"))
    assert d.source == "allowlist"


def test_rule_overrides_jev(cfg, account):
    d = decide(cfg, account, make_signals(), rule_category="newsletter")
    assert (d.category, d.source, d.archive) == ("newsletter", "rule", True)


def test_marketing_high_confidence_unsubscribes(cfg, account):
    d = decide(cfg, account, make_signals(), make_classification("marketing", 0.95))
    assert d.category == "marketing"
    assert d.archive and d.unsubscribe and not d.low_confidence
    assert "Jev/Marketing" in d.add_labels


@pytest.mark.parametrize(
    "signals_kw", [{"dmarc_pass": False}, {"unsubscribe_https": [], "unsubscribe_mailto": []}]
)
def test_marketing_without_proof_is_spam(cfg, account, signals_kw):
    d = decide(cfg, account, make_signals(**signals_kw), make_classification("marketing", 0.95))
    assert d.category == "spam" and d.downgraded_to_spam
    assert d.spam and not d.unsubscribe


def test_low_confidence_keeps_in_inbox_with_review_label(cfg, account):
    d = decide(cfg, account, make_signals(), make_classification("marketing", 0.85))
    assert d.low_confidence
    assert not (d.archive or d.spam or d.unsubscribe)
    assert d.add_labels[:2] == ["Jev/Review", "Jev/Marketing"]


def test_asymmetric_thresholds(cfg, account):
    # 0.85 is enough to archive a receipt but not to unsubscribe
    d = decide(cfg, account, make_signals(), make_classification("transactional", 0.85))
    assert d.archive and not d.low_confidence


def test_account_threshold_override(cfg, account):
    account.min_confidence = {"marketing": 0.99}
    d = decide(cfg, account, make_signals(), make_classification("marketing", 0.97))
    assert d.low_confidence


def test_important_never_low_confidence(cfg, account):
    d = decide(cfg, account, make_signals(), make_classification("important", 0.3))
    assert not d.low_confidence and not d.archive


def test_unknown_category_goes_to_review(cfg, account):
    d = decide(cfg, account, make_signals(), make_classification("deleted_category", 0.99))
    assert d.low_confidence and d.add_labels == ["Jev/Review"]


def test_unknown_category_still_flags_needs_reply(cfg, account):
    c = make_classification("deleted_category", 0.99, needs_reply=0.95)
    d = decide(cfg, account, make_signals(), c)
    assert d.low_confidence and not (d.archive or d.spam)
    assert d.add_labels == ["Jev/Review", "Jev/Needs Reply"]


def test_needs_reply_and_tags(cfg, account):
    c = make_classification("important", 0.9, needs_reply=0.8, tags={"invoice_to_pay": 0.9})
    d = decide(cfg, account, make_signals(), c)
    assert "Jev/Needs Reply" in d.add_labels and "Jev/Invoices" in d.add_labels


def test_plan_by_mode(cfg, account):
    d = decide(cfg, account, make_signals(), make_classification("marketing", 0.95))
    dry = plan(cfg, d, Mode.DRY_RUN)
    assert dry.add_labels == [] and not dry.unsubscribe
    lbl = plan(cfg, d, Mode.LABEL)
    assert lbl.add_labels == ["Jev/Marketing", "Jev/Processed"] and lbl.remove_labels == []
    act = plan(cfg, d, Mode.ACT)
    assert act.remove_labels == ["INBOX"] and act.archived and act.unsubscribe


def test_plan_spam(cfg, account):
    d = decide(cfg, account, make_signals(dmarc_pass=False), make_classification("spam", 0.95))
    act = plan(cfg, d, Mode.ACT)
    assert "SPAM" in act.add_labels and act.spammed and not act.archived


def test_rule_keys():
    assert rule_keys("a@b.com") == ["a@b.com", "@b.com"]


def test_needs_reply_only_for_kept_emails(cfg, account):
    c = make_classification("spam", 0.95, needs_reply=0.95)
    d = decide(cfg, account, make_signals(), c)
    assert "Jev/Needs Reply" not in d.add_labels


def test_needs_reply_skipped_for_downgraded_marketing(cfg, account):
    # Sponsorship pitch without DMARC: marketing -> spam, never flagged as needing a reply.
    c = make_classification("marketing", 0.95, needs_reply=0.95)
    d = decide(cfg, account, make_signals(dmarc_pass=False), c)
    assert d.downgraded_to_spam and d.spam
    assert "Jev/Needs Reply" not in d.add_labels


@pytest.mark.parametrize("category", ["spam", "newsletter"])
def test_needs_reply_flagged_for_low_confidence(cfg, account, category):
    # Low confidence keeps the email in the inbox, so the reply flag still applies.
    c = make_classification(category, 0.5, needs_reply=0.95)
    d = decide(cfg, account, make_signals(), c)
    assert d.low_confidence and not (d.archive or d.spam)
    assert "Jev/Needs Reply" in d.add_labels
