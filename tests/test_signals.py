import pytest
from conftest import GMAIL_PASS

from inbox_jev.signals import dmarc_passed, extract, parse_list_unsubscribe, resolve_relay


def test_parse_list_unsubscribe_mixed():
    https, mailto = parse_list_unsubscribe(
        "<mailto:u@list.com?subject=unsub>, <https://list.com/u?id=1>, <http://insecure.com/u>"
    )
    assert https == ["https://list.com/u?id=1"]
    assert mailto == ["mailto:u@list.com?subject=unsub"]


def test_parse_list_unsubscribe_empty():
    assert parse_list_unsubscribe(None) == ([], [])


def test_dmarc_only_trusts_gmail_header():
    forged = "attacker.com; dmarc=pass"
    gmail_fail = "mx.google.com; spf=fail; dmarc=fail header.from=bank.com"
    assert not dmarc_passed([forged, gmail_fail])
    assert dmarc_passed([GMAIL_PASS])
    assert not dmarc_passed(["mx.google.com; spf=pass"])  # no dmarc verdict
    assert not dmarc_passed([])


def test_dmarc_only_trusts_the_topmost_gmail_header():
    # Gmail prepends its own header; a sender can add one further down claiming mx.google.com.
    gmail_none = "mx.google.com; spf=softfail smtp.mailfrom=evil.com"
    injected = "mx.google.com; dmarc=pass header.from=bank.com"
    assert not dmarc_passed([gmail_none, injected])
    assert not dmarc_passed(["mx.google.com; dmarc=fail header.from=bank.com", injected])
    assert dmarc_passed([GMAIL_PASS, "mx.google.com; dmarc=fail header.from=x.com"])


def test_extract_full():
    sig = extract(
        {
            "from": ['"Shop" <Promo@News.Shop.com>'],
            "list-unsubscribe": ["<https://shop.com/u/1>, <mailto:u@shop.com>"],
            "list-unsubscribe-post": ["List-Unsubscribe=One-Click"],
            "list-id": ["Shop News <news.shop.com>"],
            "authentication-results": [GMAIL_PASS],
        }
    )
    assert sig.sender == "promo@news.shop.com"
    assert sig.sender_domain == "news.shop.com"
    assert sig.list_key == "news.shop.com"
    assert sig.one_click and sig.dmarc_pass and sig.has_unsubscribe


def test_one_click_requires_https_url():
    sig = extract(
        {
            "from": ["a@b.com"],
            "list-unsubscribe": ["<mailto:u@b.com>"],
            "list-unsubscribe-post": ["List-Unsubscribe=One-Click"],
        }
    )
    assert not sig.one_click
    assert sig.list_key == "b.com"


def test_extract_list_post_marks_discussion_list():
    base = {"from": ["a@lists.example.org"], "list-unsubscribe": ["<mailto:x-leave@l.org>"]}
    assert not extract(base).is_discussion_list
    assert extract({**base, "list-post": ["<mailto:x@l.org>"]}).is_discussion_list
    assert extract({**base, "list-post": ["NO"]}).is_discussion_list  # announce-only list
    assert not extract({**base, "list-post": ["  "]}).is_discussion_list


def _group_headers(**over):
    h = {
        "from": ["\"'David Cohen' via Hello\" <hello@acme.com>"],
        "x-original-sender": ["david.cohen@techstars.com"],
        "mailing-list": ["list hello@acme.com; contact hello+owners@acme.com"],
        "list-id": ["<hello.acme.com>"],
        "authentication-results": [
            "mx.google.com; dkim=pass; spf=pass; dmarc=pass header.from=acme.com"
        ],
    }
    h.update(over)
    return h


def test_google_group_author_is_a_candidate_until_resolved():
    sig = extract(_group_headers())
    # extract() has no account: the author is only a candidate, sender stays the group.
    assert (sig.sender, sig.group_author, sig.relayed_by) == (
        "hello@acme.com",
        "david.cohen@techstars.com",
        None,
    )
    resolved = resolve_relay(sig, ["acme.com"])
    assert resolved.sender == "david.cohen@techstars.com"
    assert resolved.sender_domain == "techstars.com"
    assert resolved.relayed_by == "hello@acme.com"
    assert resolved.list_key == "hello.acme.com"  # unsubscribe dedupe still keyed on the list
    assert resolve_relay(resolved, ["acme.com"]) == resolved  # idempotent


@pytest.mark.parametrize(
    "group, own, trusted",
    [
        ("hello@acme.com", ["acme.com"], True),
        ("hello@eu.acme.com", ["acme.com"], True),  # subdomain of an own domain
        ("hello@sister.org", ["acme.com", "sister.org"], True),  # group_domains entry
        # Attacker-owned domain passing DMARC, with the same headers Google Groups writes.
        ("news@evil.com", ["acme.com"], False),
        ("news@notacme.com", ["acme.com"], False),
        # Anyone can create a public Google Group.
        ("founders@googlegroups.com", ["acme.com"], False),
        ("hello@acme.com", [], False),
    ],
)
def test_group_author_only_trusted_on_own_domains(group, own, trusted):
    domain = group.rsplit("@", 1)[1]
    sig = extract(
        _group_headers(
            **{
                "from": [f"\"'CEO' via News\" <{group}>"],
                "x-original-sender": ["ceo@acme.com"],
                "mailing-list": [f"list {group}; contact owners@{domain}"],
                "authentication-results": [f"mx.google.com; dmarc=pass header.from={domain}"],
            }
        )
    )
    resolved = resolve_relay(sig, own)
    if trusted:
        assert (resolved.sender, resolved.relayed_by) == ("ceo@acme.com", group)
    else:
        assert resolved == sig
        assert (resolved.sender, resolved.relayed_by) == (group, None)


@pytest.mark.parametrize(
    "over",
    [
        # Direct email faking the header: no Mailing-list naming the From address.
        {"mailing-list": []},
        {"mailing-list": ["list other@acme.com; contact other+owners@acme.com"]},
        # The group's domain didn't pass DMARC.
        {"authentication-results": ["mx.google.com; dmarc=fail header.from=acme.com"]},
        # Missing, malformed or self-referencing original sender.
        {"x-original-sender": []},
        {"x-original-sender": ["not an address"]},
        {"x-original-sender": ["hello@acme.com"]},
    ],
)
def test_original_sender_only_trusted_from_a_verified_group(over):
    sig = resolve_relay(extract(_group_headers(**over)), ["acme.com"])
    assert sig.sender == "hello@acme.com" and sig.relayed_by is None and sig.group_author is None


def test_dmarc_without_a_gmail_header_is_not_trusted():
    # Gmail stamps every inbound email; any other Authentication-Results may be the sender's.
    assert not dmarc_passed(["attacker.com; dmarc=pass header.from=bank.com"])
    assert not dmarc_passed(["relay.example; dmarc=pass", "other.example; dmarc=pass"])
    sig = extract(
        {
            "from": ["Shop <promo@shop.com>"],
            "authentication-results": ["shop.com; dmarc=pass header.from=shop.com"],
            "list-unsubscribe": ["<https://shop.com/u>"],
        }
    )
    assert not sig.dmarc_pass


def test_http_only_list_unsubscribe_counts_as_none():
    assert parse_list_unsubscribe("<http://shop.com/u?id=1>") == ([], [])
    sig = extract(
        {
            "from": ["Shop <promo@shop.com>"],
            "authentication-results": [GMAIL_PASS],
            "list-unsubscribe": ["<http://shop.com/u?id=1>"],
            "list-unsubscribe-post": ["List-Unsubscribe=One-Click"],
        }
    )
    assert sig.dmarc_pass and not sig.has_unsubscribe and not sig.one_click
