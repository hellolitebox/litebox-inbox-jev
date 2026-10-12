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
def test_marketing_without_proof_is_archived_without_unsubscribe(cfg, account, signals_kw):
    d = decide(cfg, account, make_signals(**signals_kw), make_classification("marketing", 0.95))
    assert d.category == "marketing" and d.downgraded_to_spam
    assert d.archive and not d.spam and not d.unsubscribe
    assert "Jev/Marketing" in d.add_labels and "Jev/Spam" not in d.add_labels
    act = plan(cfg, d, Mode.ACT)
    assert act.archived and not act.spammed and not act.unsubscribe
    assert "SPAM" not in act.add_labels


def test_unverified_marketing_below_threshold_stays_in_inbox(cfg, account):
    c = make_classification("marketing", 0.85)
    d = decide(cfg, account, make_signals(dmarc_pass=False), c)
    assert d.low_confidence and d.downgraded_to_spam
    assert not (d.archive or d.spam or d.unsubscribe)
    assert d.add_labels[:2] == ["Jev/Review", "Jev/Marketing"]


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


def test_needs_reply_skipped_for_unverified_marketing(cfg, account):
    # Sponsorship pitch without DMARC: archived as marketing, never flagged as needing a reply.
    c = make_classification("marketing", 0.95, needs_reply=0.95)
    d = decide(cfg, account, make_signals(dmarc_pass=False), c)
    assert d.downgraded_to_spam and d.archive and not d.spam
    assert "Jev/Needs Reply" not in d.add_labels


@pytest.mark.parametrize("category", ["spam", "newsletter"])
def test_needs_reply_flagged_for_low_confidence(cfg, account, category):
    # Low confidence keeps the email in the inbox, so the reply flag still applies.
    c = make_classification(category, 0.5, needs_reply=0.95)
    d = decide(cfg, account, make_signals(), c)
    assert d.low_confidence and not (d.archive or d.spam)
    assert "Jev/Needs Reply" in d.add_labels


def test_plan_act_categories_limits_actions_to_listed_categories(cfg, account):
    d = decide(cfg, account, make_signals(), make_classification("marketing", 0.95))
    held = plan(cfg, d, Mode.ACT, act_categories=["notification"])
    assert held.add_labels == ["Jev/Marketing", "Jev/Processed"]
    assert held.remove_labels == [] and not (held.archived or held.spammed or held.unsubscribe)
    acted = plan(cfg, d, Mode.ACT, act_categories=["marketing"])
    assert acted.archived and acted.unsubscribe
    assert plan(cfg, d, Mode.ACT, act_categories=None).archived  # default: all categories
    assert plan(cfg, d, Mode.LABEL, act_categories=["marketing"]).remove_labels == []


def test_cold_outreach_is_archived_not_spammed(cfg, account):
    d = decide(cfg, account, make_signals(), make_classification("cold_outreach", 0.85))
    assert not d.low_confidence and d.archive and not d.spam and not d.unsubscribe
    assert "Jev/Outreach" in d.add_labels
    act = plan(cfg, d, Mode.ACT, act_categories=["cold_outreach"])
    assert act.remove_labels == ["INBOX"] and "SPAM" not in act.add_labels
    low = decide(cfg, account, make_signals(), make_classification("cold_outreach", 0.75))
    assert low.low_confidence and not low.archive


def probs(category, confidence, **others):
    c = make_classification(category, confidence)
    c.probabilities = {category: confidence, **others}
    return c


def test_archive_group_archives_when_unsure_which_archive_label(cfg, account):
    d = decide(cfg, account, make_signals(), probs("notification", 0.55, newsletter=0.42))
    assert d.archive and d.archive_group and not d.low_confidence
    assert d.category == "notification" and d.add_labels == ["Jev/Notifications"]
    assert plan(cfg, d, Mode.ACT).archived


def test_archive_group_never_unsubscribes(cfg, account):
    d = decide(cfg, account, make_signals(), probs("marketing", 0.6, newsletter=0.35))
    assert d.archive and d.archive_group and not d.unsubscribe
    assert not plan(cfg, d, Mode.ACT).unsubscribe


@pytest.mark.parametrize(
    "classification",
    [
        probs("notification", 0.6, important=0.35),  # might be personal
        probs("notification", 0.5, action_required=0.45),  # keep category not in the group
        probs("newsletter", 0.5, marketing=0.35, spam=0.15),  # spam not in the group
        probs("spam", 0.5, cold_outreach=0.45),  # top answer must be an archive category
    ],
)
def test_archive_group_stays_in_review_otherwise(cfg, account, classification):
    d = decide(cfg, account, make_signals(), classification)
    assert d.low_confidence and not (d.archive or d.spam or d.archive_group)
    assert "Jev/Review" in d.add_labels


def test_archive_group_disabled(cfg, account):
    cfg = cfg.model_copy(update={"archive_group_min_confidence": None})
    d = decide(cfg, account, make_signals(), probs("notification", 0.55, newsletter=0.45))
    assert d.low_confidence and not d.archive


def test_archive_group_respects_stricter_account_threshold(cfg, account):
    account.min_confidence = {"notification": 0.99}
    d = decide(cfg, account, make_signals(), probs("notification", 0.55, newsletter=0.42))
    assert d.low_confidence and not d.archive


def test_action_required_stays_in_inbox(cfg, account):
    d = decide(cfg, account, make_signals(), make_classification("action_required", 0.6))
    assert not (d.low_confidence or d.archive or d.spam)
    assert d.add_labels == ["Jev/Action"]


def test_archive_group_floor_is_at_least_top_threshold(cfg, account):
    cfg = cfg.model_copy(update={"archive_group_min_confidence": 0.5})
    # 0.75 is above the configured 0.5 but below cold_outreach's own 0.80.
    d = decide(cfg, account, make_signals(), probs("cold_outreach", 0.55, newsletter=0.20))
    assert d.low_confidence and not d.archive
    d = decide(cfg, account, make_signals(), probs("cold_outreach", 0.55, important=0.45))
    assert d.low_confidence and not d.archive
    d = decide(cfg, account, make_signals(), probs("cold_outreach", 0.55, newsletter=0.40))
    assert d.archive and d.archive_group  # 0.95 >= max(0.5, 0.80)


def test_archive_group_only_counts_act_categories(cfg, account):
    account.act_categories = ["notification"]
    d = decide(cfg, account, make_signals(), probs("notification", 0.50, marketing=0.45))
    assert d.low_confidence and not d.archive
    account.act_categories = ["notification", "newsletter"]
    d = decide(cfg, account, make_signals(), probs("notification", 0.50, newsletter=0.45))
    assert d.archive and d.archive_group


def test_archive_group_top_category_outside_act_categories(cfg, account):
    account.act_categories = ["newsletter"]
    d = decide(cfg, account, make_signals(), probs("notification", 0.50, newsletter=0.45))
    assert d.low_confidence and not d.archive


def test_archive_group_skips_member_with_stricter_account_threshold(cfg, account):
    account.min_confidence = {"marketing": 0.99}
    d = decide(cfg, account, make_signals(), probs("newsletter", 0.50, marketing=0.45))
    assert d.low_confidence and not d.archive


def test_archive_group_vetoed_by_needs_reply(cfg, account):
    c = probs("cold_outreach", 0.50, transactional=0.42, important=0.08)
    c.needs_reply = 0.95
    d = decide(cfg, account, make_signals(), c)
    assert d.low_confidence and not d.archive and not d.archive_group
    assert {"Jev/Review", "Jev/Needs Reply"} <= set(d.add_labels)
