from conftest import GMAIL_PASS

from inbox_jev.signals import dmarc_passed, extract, parse_list_unsubscribe


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
