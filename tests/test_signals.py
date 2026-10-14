import pytest
from fakes import GMAIL_PASS

from inbox_jev.signals import (
    GENERIC_CCTLD_SLDS,
    PUBLIC_SLDS,
    dmarc_passed,
    domain_of,
    extract,
    is_automated_address,
    is_no_reply_address,
    is_public_suffix,
    on_domain,
    parse_list_unsubscribe,
    resolve_relay,
    wrote_to_person_at,
)


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


def test_domain_of_takes_the_part_after_the_last_at():
    assert domain_of("bob@Shop.com") == "Shop.com"  # case is the caller's job
    assert domain_of('"a@b"@shop.com') == "shop.com"
    assert domain_of("no-at-sign") == "no-at-sign"


@pytest.mark.parametrize(
    "host, parent, expected",
    [
        ("shop.com", "shop.com", True),
        ("mail.shop.com", "shop.com", True),
        ("evilshop.com", "shop.com", False),
        ("shop.com.evil.io", "shop.com", False),
    ],
)
def test_on_domain_matches_the_domain_and_its_subdomains_only(host, parent, expected):
    assert on_domain(host, parent) is expected


NO_REPLY = [
    "noreply@shop.com",
    "no-reply@shop.com",
    "no_reply@shop.com",
    "NoReply@Shop.com",
    "donotreply@bank.com",
    "do-not-reply@bank.com",
    "do_not_reply@bank.com",
    "messages-noreply@linkedin.com",
    "noreply-billing@vendor.com",
    "alerts.no-reply@vendor.com",
    "team+noreply@vendor.com",
]
# Look like it, but a person or a monitored mailbox may read them.
NOT_NO_REPLY = [
    "norah@shop.com",
    "noreen@shop.com",
    "snoreply@shop.com",
    "noreplies@shop.com",
    "reply@company.com",
    "notifications@github.com",
    "info@company.com",
    "support@company.com",
    "noreply",  # no @: not an address
    "jane@noreply.com",  # the domain isn't the local part
]


@pytest.mark.parametrize("address", NO_REPLY)
def test_no_reply_local_parts(address):
    assert is_no_reply_address(address)
    assert is_automated_address(address)  # every no-reply address is automated too


@pytest.mark.parametrize("address", NOT_NO_REPLY)
def test_not_no_reply_local_parts(address):
    assert not is_no_reply_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "m-a175bcd9x0e1@bounce.linkedin.com",  # VERP return path, the LinkedIn case
        "reply+ABCD1234@reply.github.com",
        "reply+abcd@github.com",
        "reply-8f3a2c@mail.basecamp.com",
        "bounce@shop.com",
        "bounces+12345-abcd=me@sendgrid.net",
        "bounce-mc.us5_123.456-me=example.com@mail42.mcsv.net",
        "MAILER-DAEMON@mx.example.com",
        "postmaster@example.com",
        "anything@bounces.example.com",
        "x@em.bounce.shop.co.uk",
        "x@bounce.linkedin.co.uk",  # a bounce subdomain under a ccTLD public suffix
        "x@reply.github.com.br",
        "x@bounces.acme.com.ar",
        "bounce-12345-67@mail.shop.com",
        "bounce.mc.us5_123@mail.shop.com",
    ],
)
def test_automated_addresses(address):
    assert is_automated_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "jane@linkedin.com",
        "reply@company.com",  # may be a shared inbox a person reads
        "reply-team@company.com",  # no token in it
        "replies@company.com",
        "bouncer@club.com",
        "bob.bounce@company.com",
        "notifications@github.com",  # not writable anyway, but not clear enough
        "sam@reply.io",  # a company called reply: only subdomain labels count
        "ana@bounce.com",
        # A company called Reply / Bounce under a two-level public suffix: its own name is the
        # third label from the right, not a subdomain.
        "jane@reply.co.uk",
        "ana@reply.com.ar",
        "joe@bounce.com.au",
        "bob@bounces.com.br",
        "eva@reply.gob.ar",
        "lu@bounce.adv.br",
        "x@bounce.sap.de",  # a short name under a ccTLD is taken as a suffix: safe side
        # Shared mailboxes that only start like a bounce or reply token (no 3-digit token).
        "bounce.house@party.com",
        "bounce-rentals@party.com",
        "reply-all2@company.com",
        "bounce_team@company.com",
        "norah@shop.com",
        "",
        "@bounce.linkedin.com",
        "not-an-address",
    ],
)
def test_not_automated_addresses(address):
    assert not is_automated_address(address)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (["m-a175@bounce.linkedin.com"], False),  # a reply to a LinkedIn notification
        (["LinkedIn <messages-noreply@linkedin.com>"], False),
        (["m-1@bounce.linkedin.com, m-2@bounce.linkedin.com"], False),
        (["Jane <jane@linkedin.com>"], True),
        (["m-a175@bounce.linkedin.com", "jane@linkedin.com"], True),  # a person in Cc
        (["jane@eu.linkedin.com"], True),  # a subdomain counts as the domain
        (["bob@other.com"], True),  # nothing on the domain: Gmail matched it, trust it
        ([], True),
    ],
)
def test_wrote_to_person_at(headers, expected):
    assert wrote_to_person_at(headers, "linkedin.com") is expected


@pytest.mark.parametrize(
    ("headers", "domain", "expected"),
    [
        (["ana@reply.com.ar"], "reply.com.ar", True),
        (["jane@reply.co.uk"], "reply.co.uk", True),
        (["bob@bounces.com.br"], "bounces.com.br", True),
        # The domain searched is the sender's: its own labels never make a machine, even past
        # what the suffix rule knows (wa.gov.au is a three-level public suffix).
        (["kim@reply.wa.gov.au"], "reply.wa.gov.au", True),
        (["m-1@bounce.linkedin.co.uk"], "linkedin.co.uk", False),
        (["reply+x@reply.github.com.br"], "github.com.br", False),
        (["x@reply.acme.com.ar"], "acme.com.ar", False),
    ],
)
def test_wrote_to_person_at_under_two_level_public_suffixes(headers, domain, expected):
    assert wrote_to_person_at(headers, domain) is expected


def test_owner_domain_labels_never_make_a_machine():
    assert is_automated_address("kim@reply.wa.gov.au")  # beyond the suffix rule: a known gap
    assert not is_automated_address("kim@reply.wa.gov.au", "reply.wa.gov.au")
    assert is_automated_address("m-1@bounce.acme.wa.gov.au", "acme.wa.gov.au")
    assert is_automated_address("m-1@bounce.acme.com", "other.com")  # not on it: ignored


def _sender_headers(sender, reply_to=None):
    headers = {"from": [sender], "authentication-results": [GMAIL_PASS]}
    if reply_to is not None:
        headers["reply-to"] = [reply_to]
    return headers


@pytest.mark.parametrize(
    ("sender", "reply_to", "no_reply"),
    [
        ("LinkedIn <messages-noreply@linkedin.com>", None, True),
        ("Bank <donotreply@bank.com>", "noreply@bank.com", True),
        ("Shop <noreply@shop.com>", "Ana Support <ana@shop.com>", False),  # replies reach Ana
        ("GitHub <noreply@github.com>", "reply+ABC@reply.github.com", False),  # reply-by-email
        ("Shop <noreply@shop.com>", "noreply@shop.com, help@shop.com", False),
        ("Shop <noreply@shop.com>", "", True),  # an empty header is no Reply-To
        ("Jane <jane@client.com>", None, False),
        ("Jane <jane@client.com>", "noreply@client.com", False),  # only the sender decides
        ("GitHub <notifications@github.com>", None, False),  # not clear enough
    ],
)
def test_no_reply_needs_a_no_reply_sender_and_reply_to(sender, reply_to, no_reply):
    sig = extract(_sender_headers(sender, reply_to))
    assert sig.no_reply is no_reply


def test_extract_reply_to_addresses():
    sig = extract(_sender_headers("a@x.com", '"Help, Desk" <Help@X.com>, b@x.com'))
    assert sig.reply_to == ("help@x.com", "b@x.com")
    assert extract(_sender_headers("a@x.com")).reply_to == ()


def test_no_reply_follows_the_trusted_group_author():
    headers = {
        "from": ["'Dana' via Hello <hello@example.com>"],
        "authentication-results": [GMAIL_PASS],
        "mailing-list": ["list hello@example.com; contact hello+owners@example.com"],
        "x-original-sender": ["noreply@tool.example"],
    }
    sig = extract(headers)
    assert not sig.no_reply  # the group address
    assert resolve_relay(sig, ["example.com"]).no_reply  # the author it relays


def test_every_named_public_suffix_is_one_for_the_registrable_domain_too():
    """`is_public_suffix` (by name) never calls a suffix what `is_automated_address`'s
    registrable-domain rule (by length) takes as a name: x.<sld>.<cc> keeps 3 labels for each."""
    for sld in PUBLIC_SLDS:
        assert is_public_suffix(f"{sld}.uk")
        assert not is_automated_address(f"jane@bounce.{sld}.uk")  # bounce is the name here
    for tld, slds in GENERIC_CCTLD_SLDS.items():
        for sld in slds:
            assert is_public_suffix(f"{sld}.{tld}")
            assert not is_automated_address(f"jane@bounce.{sld}.{tld}")


# The Public Suffix List's ICANN entries under each generic-use ccTLD, copied from
# https://publicsuffix.org/list/public_suffix_list.dat at `VERSION: 2026-10-07_07-28-19_UTC`,
# `COMMIT: 3929462652695bad04f0a27afb600974014a3c8b`. Tests don't fetch it (no network): when the
# PSL changes, update this snapshot and GENERIC_CCTLD_SLDS together.
PSL_SNAPSHOT = {
    "ai": {"com", "net", "off", "org"},
    "co": {"com", "edu", "gov", "mil", "net", "nom", "org"},
    "io": {"co", "com", "edu", "gov", "mil", "net", "nom", "org"},
    "me": {"ac", "co", "edu", "gov", "its", "net", "org", "priv"},
    "tv": set(),
}
# What `config._PUBLIC_SLD` refused under every two-letter TLD before GENERIC_CCTLD_SLDS.
OLD_REFUSED_SLDS = {"ac", "co", "com", "edu", "gob", "go", "gov", "ne", "net", "or", "org"}


def test_generic_cctld_slds_match_the_public_suffix_list():
    assert {tld: set(slds) for tld, slds in GENERIC_CCTLD_SLDS.items()} == PSL_SNAPSHOT


@pytest.mark.parametrize("tld", sorted(PSL_SNAPSHOT))
def test_generic_cctld_table_never_accepts_a_listed_suffix_the_old_check_refused(tld):
    """The table may accept what the old check wrongly refused (adv.ai, plc.io), but never a
    name the old check refused that the PSL lists as a suffix (org.io, gov.co)."""
    for sld in OLD_REFUSED_SLDS & PSL_SNAPSHOT[tld]:
        assert is_public_suffix(f"{sld}.{tld}"), f"{sld}.{tld}"


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("co.uk", True),
        ("gouv.fr", True),
        ("nhs.uk", True),
        ("acme.co.uk", False),
        ("acme.io", False),  # a short name under a ccTLD is a domain
        ("sap.de", False),
        ("org.com", False),  # not a ccTLD
        ("wa.gov.au", False),  # deeper suffixes: the known gap
        # ccTLDs sold as generic names: only their own PSL second levels are suffixes.
        ("com.ai", True),
        ("off.ai", True),
        ("com.co", True),
        ("org.co", True),
        ("web.co", False),  # dropped from the PSL by the .co registry (2024-11-18)
        ("arts.co", False),
        ("com.io", True),
        ("org.io", True),
        ("co.io", True),
        ("its.me", True),
        ("adv.ai", False),
        ("sch.ai", False),
        ("gov.ai", False),
        ("plc.io", False),
        ("ltd.co", False),
        ("com.tv", False),
        ("gouv.me", False),
        ("plc.uk", True),  # elsewhere PUBLIC_SLDS still applies
        ("xn--p1ai.xn--p1ai", False),
    ],
)
def test_is_public_suffix(domain, expected):
    assert is_public_suffix(domain) is expected
