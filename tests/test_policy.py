import pytest
from conftest import make_classification, make_signals
from fakes import GMAIL_PASS

from inbox_jev.config import Mode
from inbox_jev.policy import decide, flags_only, plan, rule_keys
from inbox_jev.signals import extract, resolve_relay


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
    # No unsubscribe was in play (Review), so it isn't flagged as a skipped one.
    assert d.low_confidence and not d.downgraded_to_spam
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


def test_cold_outreach_is_archived_and_unsubscribed_not_spammed(cfg, account):
    d = decide(cfg, account, make_signals(), make_classification("cold_outreach", 0.95))
    assert not d.low_confidence and d.archive and d.unsubscribe and not d.spam
    assert "Jev/Outreach" in d.add_labels
    act = plan(cfg, d, Mode.ACT, act_categories=["cold_outreach"])
    assert act.remove_labels == ["INBOX"] and "SPAM" not in act.add_labels and act.unsubscribe
    low = decide(cfg, account, make_signals(), make_classification("cold_outreach", 0.85))
    assert low.low_confidence and not (low.archive or low.unsubscribe)


def test_cold_outreach_between_thresholds_is_archived_by_group_without_unsubscribe(cfg, account):
    c = make_classification("cold_outreach", 0.85)
    c.probabilities = {"cold_outreach": 0.85, "marketing": 0.14, "important": 0.01}
    d = decide(cfg, account, make_signals(), c)
    assert d.archive and d.archive_group and not d.unsubscribe


@pytest.mark.parametrize(
    "signals_kw", [{"dmarc_pass": False}, {"unsubscribe_https": [], "unsubscribe_mailto": []}]
)
def test_unverified_cold_outreach_is_archived_without_unsubscribe(cfg, account, signals_kw):
    d = decide(cfg, account, make_signals(**signals_kw), make_classification("cold_outreach", 1.0))
    assert d.archive and not d.unsubscribe and not d.spam and d.downgraded_to_spam


@pytest.mark.parametrize("category", ["cold_outreach", "marketing"])
def test_rule_never_unsubscribes_from_unverified_sender(cfg, account, category):
    unverified = make_signals(dmarc_pass=False)
    d = decide(cfg, account, unverified, rule_category=category)
    assert (d.source, d.archive, d.unsubscribe, d.downgraded_to_spam) == ("rule", True, False, True)
    verified = decide(cfg, account, make_signals(), rule_category=category)
    assert verified.unsubscribe and not verified.downgraded_to_spam


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


def test_needs_reply_does_not_block_group_for_pitch_with_no_keep_signal(cfg, account):
    # Mass "book your free consultation" email: every pitch expects a reply, but nothing
    # suggests a real person who needs one, so the group archive still applies.
    c = probs("cold_outreach", 0.67, marketing=0.31, newsletter=0.01, important=0.0)
    c.needs_reply = 0.79
    d = decide(cfg, account, make_signals(), c)
    assert d.archive and d.archive_group and not d.low_confidence and not d.unsubscribe
    assert "Jev/Needs Reply" not in d.add_labels and "Jev/Review" not in d.add_labels


@pytest.mark.parametrize(
    "keep", [{"important": 0.05}, {"important": 0.03, "action_required": 0.02}]
)
def test_needs_reply_blocks_group_at_keep_threshold(cfg, account, keep):
    c = probs("cold_outreach", 0.62, marketing=0.33, **keep)
    c.needs_reply = 0.79
    d = decide(cfg, account, make_signals(), c)
    assert d.low_confidence and not d.archive
    assert {"Jev/Review", "Jev/Needs Reply"} <= set(d.add_labels)


def test_needs_reply_just_below_keep_threshold_archives(cfg, account):
    c = probs("cold_outreach", 0.62, marketing=0.34, important=0.04)
    c.needs_reply = 0.79
    d = decide(cfg, account, make_signals(), c)
    assert d.archive and d.archive_group and not d.low_confidence
    assert "Jev/Needs Reply" not in d.add_labels


@pytest.mark.parametrize("contact", ["wrote_to_sender", "wrote_to_their_domain"])
def test_needs_reply_blocks_group_for_known_contact_without_keep_signal(cfg, account, contact):
    # A vendor I emailed replies with a sales-flavoured follow-up: stays in Review even though
    # the keep categories are below archive_group_reply_keep_probability.
    c = probs("cold_outreach", 0.60, marketing=0.36, important=0.0)
    c.needs_reply = 0.90
    d = decide(cfg, account, make_signals(), c, previous_contact=contact)
    assert d.low_confidence and not d.archive and not d.archive_group
    assert {"Jev/Review", "Jev/Needs Reply"} <= set(d.add_labels)


@pytest.mark.parametrize("contact", ["wrote_to_sender", "wrote_to_their_domain"])
def test_needs_reply_from_a_no_reply_sender_ignores_the_known_contact(cfg, account, contact):
    # Nobody reads a reply to messages-noreply@: a reply signal alone can't keep it.
    sig = make_signals(sender="messages-noreply@linkedin.com", sender_domain="linkedin.com")
    c = probs("notification", 0.62, newsletter=0.36, important=0.01)
    c.needs_reply = 0.75
    d = decide(cfg, account, sig, c, previous_contact=contact)
    assert d.archive and d.archive_group and not d.low_confidence
    assert "Jev/Needs Reply" not in d.add_labels


def test_needs_reply_from_a_no_reply_sender_still_vetoed_by_the_keep_signal(cfg, account):
    # "A reply or an action": with a keep signal the email may need an action, as for a stranger.
    sig = make_signals(sender="noreply@bank.com", sender_domain="bank.com")
    c = probs("notification", 0.62, transactional=0.32, action_required=0.06)
    c.needs_reply = 0.75
    d = decide(cfg, account, sig, c, previous_contact="wrote_to_their_domain")
    assert d.low_confidence and not d.archive
    assert {"Jev/Review", "Jev/Needs Reply"} <= set(d.add_labels)  # kept: the label stays


def test_needs_reply_from_no_reply_with_a_human_reply_to_keeps_the_veto(cfg, account):
    sig = make_signals(
        sender="noreply@vendor.com", sender_domain="vendor.com", reply_to=("ana@vendor.com",)
    )
    c = probs("notification", 0.62, newsletter=0.36, important=0.01)
    c.needs_reply = 0.75
    d = decide(cfg, account, sig, c, previous_contact="wrote_to_their_domain")
    assert d.low_confidence and not d.archive


@pytest.mark.parametrize("contact", ["never", None])
def test_needs_reply_relaxed_for_unknown_or_new_contact(cfg, account, contact):
    c = probs("cold_outreach", 0.60, marketing=0.36, important=0.0)
    c.needs_reply = 0.90
    d = decide(cfg, account, make_signals(), c, previous_contact=contact)
    assert d.archive and d.archive_group and not d.low_confidence


def test_known_contact_without_reply_signal_still_group_archives(cfg, account):
    c = probs("notification", 0.55, newsletter=0.42)
    d = decide(cfg, account, make_signals(), c, previous_contact="wrote_to_sender")
    assert d.archive and d.archive_group


def test_needs_reply_keep_threshold_zero_restores_strict_veto(cfg, account):
    cfg = cfg.model_copy(update={"archive_group_reply_keep_probability": 0.0})
    c = probs("cold_outreach", 0.67, marketing=0.33, important=0.0)
    c.needs_reply = 0.79
    d = decide(cfg, account, make_signals(), c)
    assert d.low_confidence and not d.archive


GROUP_UNSUB = ["mailto:googlegroups-manage+1+unsubscribe@googlegroups.com"]


@pytest.mark.parametrize(
    "signals_kw",
    [
        # A pitch relayed by my own Google Group (hello@acme.com): unsubscribing leaves the group.
        dict(
            sender="hello@acme.com",
            sender_domain="acme.com",
            list_id="hello.acme.com",
            unsubscribe_https=["https://groups.google.com/a/acme.com/group/hello/subscribe"],
            unsubscribe_mailto=GROUP_UNSUB,
            one_click=False,
        ),
        # An external Google Group I belong to.
        dict(list_id="founders.example.org", unsubscribe_https=[], unsubscribe_mailto=GROUP_UNSUB),
        # A list on my own domain with a regular ESP unsubscribe.
        dict(list_id="news.acme.com"),
    ],
)
@pytest.mark.parametrize("category", ["cold_outreach", "marketing"])
def test_never_unsubscribes_from_groups_or_own_lists(cfg, account, signals_kw, category):
    account.allow_domains = []  # the fixture allowlists acme.com, which would skip Jev entirely
    sig = make_signals(**signals_kw)
    d = decide(cfg, account, sig, make_classification(category, 0.99))
    assert d.archive and not d.unsubscribe
    rule = decide(cfg, account, sig, rule_category=category)
    assert rule.archive and not rule.unsubscribe


def _extract(sender, extra):
    headers = {"from": [sender], "authentication-results": [GMAIL_PASS]}
    headers.update({k: [v] for k, v in extra.items()})
    return extract(headers)


# Real header shapes; both lists rewrite From to the list domain, so DMARC passes.
DISCUSSION_LISTS = {
    "groups.io": (
        "Jane via founders <founders@groups.io>",
        {
            "list-id": "<founders.groups.io>",
            "list-post": "<mailto:founders@groups.io>",
            "list-unsubscribe": "<https://groups.io/g/founders/leave/123/abc>",
            "list-unsubscribe-post": "List-Unsubscribe=One-Click",
        },
    ),
    "mailman": (
        "Jane via Founders <founders@lists.example.org>",
        {
            "list-id": "Founders <founders.lists.example.org>",
            "list-post": "<mailto:founders@lists.example.org>",
            "list-unsubscribe": "<mailto:founders-leave@lists.example.org>",
        },
    ),
}


@pytest.mark.parametrize("software", sorted(DISCUSSION_LISTS))
@pytest.mark.parametrize("category", ["cold_outreach", "marketing"])
def test_never_unsubscribes_from_discussion_lists(cfg, account, software, category):
    sig = _extract(*DISCUSSION_LISTS[software])
    assert sig.is_discussion_list and sig.dmarc_pass and sig.has_unsubscribe
    d = decide(cfg, account, sig, make_classification(category, 0.99))
    assert d.archive and not d.unsubscribe
    rule = decide(cfg, account, sig, rule_category=category)
    assert rule.archive and not rule.unsubscribe


def test_sequence_tool_headers_still_unsubscribe(cfg, account):
    # Instantly / Smartlead style: one-click List-Unsubscribe, no List-Post.
    sig = _extract(
        "Sam <sam@trygameplan.co>",
        {
            "list-unsubscribe": "<https://track.instantly.ai/unsub/abc>, <mailto:u@inst.ly>",
            "list-unsubscribe-post": "List-Unsubscribe=One-Click",
        },
    )
    assert not sig.is_discussion_list
    d = decide(cfg, account, sig, make_classification("cold_outreach", 0.95))
    assert d.archive and d.unsubscribe
    assert decide(cfg, account, sig, rule_category="cold_outreach").unsubscribe


@pytest.mark.parametrize(
    "uri, group",
    [
        ("https://groups.google.com/a/x.org/group/y/subscribe", True),
        ("https://sub.googlegroups.com/u", True),
        ("mailto:googlegroups-manage+1+unsubscribe@googlegroups.com?subject=x", True),
        ("https://shop.com/u?ref=googlegroups.com", False),
        ("https://notgooglegroups.com/u", False),
        ("mailto:googlegroups.com@shop.com", False),
    ],
)
def test_group_host_matches_parsed_host(cfg, account, uri, group):
    key = "unsubscribe_mailto" if uri.startswith("mailto:") else "unsubscribe_https"
    sig = make_signals(**{"unsubscribe_https": [], "unsubscribe_mailto": [], key: [uri]})
    d = decide(cfg, account, sig, make_classification("marketing", 0.99))
    assert d.unsubscribe is not group


@pytest.mark.parametrize("contact", ["wrote_to_sender", "wrote_to_their_domain"])
def test_cold_outreach_from_known_contact_is_archived_without_unsubscribe(cfg, account, contact):
    c = make_classification("cold_outreach", 0.99)
    d = decide(cfg, account, make_signals(), c, previous_contact=contact)
    assert d.archive and not d.unsubscribe and d.known_contact_kept
    assert plan(cfg, d, Mode.ACT).remove_labels == ["INBOX"]
    # Marketing keeps unsubscribing (unsubscribe_known_contacts defaults to true).
    mkt = make_classification("marketing", 0.99)
    m = decide(cfg, account, make_signals(), mkt, previous_contact=contact)
    assert m.unsubscribe and not m.known_contact_kept


@pytest.mark.parametrize("contact", ["never", None])
def test_cold_outreach_from_stranger_still_unsubscribes(cfg, account, contact):
    c = make_classification("cold_outreach", 0.99)
    d = decide(cfg, account, make_signals(), c, previous_contact=contact)
    assert d.unsubscribe and not d.known_contact_kept


@pytest.mark.parametrize("contact", ["wrote_to_sender", "wrote_to_their_domain"])
def test_rule_path_spares_known_contacts(cfg, account, contact):
    d = decide(
        cfg, account, make_signals(), rule_category="cold_outreach", previous_contact=contact
    )
    assert d.archive and not d.unsubscribe and d.known_contact_kept
    # Marketing keeps unsubscribing (unsubscribe_known_contacts defaults to true).
    m = decide(cfg, account, make_signals(), rule_category="marketing", previous_contact=contact)
    assert m.unsubscribe and not m.known_contact_kept


@pytest.mark.parametrize("contact", ["never", None])
def test_rule_path_still_unsubscribes_strangers(cfg, account, contact):
    d = decide(
        cfg, account, make_signals(), rule_category="cold_outreach", previous_contact=contact
    )
    assert d.unsubscribe and not d.known_contact_kept


def _relayed(**kw):
    base = dict(
        sender="david.cohen@techstars.com",
        sender_domain="techstars.com",
        list_id="hello.acme.com",
        relayed_by="hello@acme.com",
        unsubscribe_https=[],
        unsubscribe_mailto=GROUP_UNSUB,
        one_click=False,
    )
    base.update(kw)
    return make_signals(**base)


def test_group_relayed_mail_is_judged_by_its_author(cfg, account):
    # acme.com (the account's own domain) is allowlisted, but the author is external: Jev decides.
    with pytest.raises(ValueError):
        decide(cfg, account, _relayed())  # not allowlisted, so a classification is required
    d = decide(cfg, account, _relayed(), make_classification("cold_outreach", 0.99))
    assert d.category == "cold_outreach" and d.archive and not d.unsubscribe


def test_group_relayed_mail_allowlisted_by_author_or_group(cfg, account):
    account.allow_domains = []
    account.allow_senders = ["david.cohen@techstars.com"]
    assert decide(cfg, account, _relayed()).source == "allowlist"
    account.allow_senders = ["hello@acme.com"]  # allowlisting the group keeps working
    assert decide(cfg, account, _relayed()).source == "allowlist"


def test_group_relayed_mail_never_unsubscribes_even_with_esp_headers(cfg, account):
    sig = _relayed(
        unsubscribe_https=["https://esp.example.com/u"],
        unsubscribe_mailto=[],
        one_click=True,
        list_id=None,
    )
    assert not decide(cfg, account, sig, make_classification("marketing", 0.99)).unsubscribe
    assert not decide(cfg, account, sig, rule_category="cold_outreach").unsubscribe


@pytest.mark.parametrize("group", ["news@evil.com", "founders@googlegroups.com"])
def test_untrusted_group_cannot_claim_an_allowlisted_author(cfg, account, group):
    # Valid DMARC for the attacker's own domain, plus the headers Google Groups would write.
    domain = group.rsplit("@", 1)[1]
    sig = extract(
        {
            "from": [f"\"'CEO' via News\" <{group}>"],
            "x-original-sender": ["ceo@acme.com"],  # acme.com is allowlisted
            "mailing-list": [f"list {group}; contact owners@{domain}"],
            "authentication-results": [f"mx.google.com; dmarc=pass header.from={domain}"],
        }
    )
    sig = resolve_relay(sig, account.group_domains)
    assert sig.sender == group and sig.relayed_by is None
    with pytest.raises(ValueError):
        decide(cfg, account, sig)  # not allowlisted: Jev has to classify it
    # No unsubscribe through something shaped like a group, trusted or not.
    assert not decide(cfg, account, sig, rule_category="cold_outreach").unsubscribe


def test_rule_to_removed_category_is_ignored(cfg, account):
    c = make_classification("newsletter", 0.97)
    d = decide(cfg, account, make_signals(), c, rule_category="promotions")
    assert (d.source, d.category, d.archive) == ("jev", "newsletter", True)
    with pytest.raises(ValueError, match="classification is required"):
        decide(cfg, account, make_signals(), rule_category="promotions")


def test_keep_rule_with_classification_adds_flags_not_category(cfg, account):
    c = make_classification("cold_outreach", 0.99, needs_reply=0.9, tags={cfg.tags[0].key: 0.95})
    d = decide(cfg, account, make_signals(), c, rule_category="important")
    assert (d.source, d.category, d.confidence, d.archive) == ("rule", "important", 1.0, False)
    assert d.add_labels == ["Jev/Important", "Jev/Needs Reply", cfg.tags[0].label]
    allow = make_signals(sender="ceo@acme.com", sender_domain="acme.com")
    d = decide(cfg, account, allow, c)
    assert (d.source, d.category) == ("allowlist", "important")
    assert d.add_labels == ["Jev/Important", "Jev/Needs Reply", cfg.tags[0].label]
    assert decide(cfg, account, allow).add_labels == ["Jev/Important"]  # no answer: no flags


def test_flags_only_for_allowlist_and_keep_rules(cfg, account):
    sig = make_signals()
    assert flags_only(cfg, account, make_signals(sender="vip@partner.com"), None)
    assert flags_only(cfg, account, sig, "important")
    assert flags_only(cfg, account, sig, "action_required")
    assert not flags_only(cfg, account, sig, "newsletter")
    assert not flags_only(cfg, account, sig, "spam")
    assert not flags_only(cfg, account, sig, "promotions")  # removed category
    assert not flags_only(cfg, account, sig, None)


def test_archive_group_skips_member_with_stricter_category_threshold(cfg, account):
    # Category-level, not only account-level: marketing at 0.95 (above the 0.90 floor) is out.
    cfg.by_key["marketing"].min_confidence = 0.95
    d = decide(cfg, account, make_signals(), probs("newsletter", 0.50, marketing=0.45))
    assert d.low_confidence and not d.archive
    account.min_confidence = {"marketing": 0.90}  # the account override wins
    d = decide(cfg, account, make_signals(), probs("newsletter", 0.50, marketing=0.45))
    assert d.archive and d.archive_group


@pytest.mark.parametrize("confidence", [0.60, 0.85])  # archive group / Review
def test_unverified_flag_only_when_an_unsubscribe_was_in_play(cfg, account, confidence):
    c = probs("marketing", confidence, newsletter=0.98 - confidence)
    d = decide(cfg, account, make_signals(dmarc_pass=False), c)
    assert not d.unsubscribe and not d.downgraded_to_spam


def test_known_contact_flag_set_after_the_verified_and_group_checks(cfg, account):
    c = make_classification("cold_outreach", 0.97)
    for sig in (make_signals(dmarc_pass=False), make_signals(is_discussion_list=True)):
        for rule in (None, "cold_outreach"):
            d = decide(
                cfg, account, sig, None if rule else c, rule, previous_contact="wrote_to_sender"
            )
            assert d.archive and not d.unsubscribe and not d.known_contact_kept
    d = decide(cfg, account, make_signals(), c, previous_contact="wrote_to_sender")
    assert d.known_contact_kept and not d.unsubscribe and not d.downgraded_to_spam
