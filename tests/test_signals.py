import itertools
import random
import re

import pytest
from fakes import GMAIL_PASS

from inbox_jev.signals import (
    dmarc_passed,
    domain_of,
    extract,
    is_automated_address,
    is_no_reply_address,
    mailbox_key,
    on_domain,
    parse_list_unsubscribe,
    reply_without_thread,
    resolve_relay,
    sender_auth,
    wrote_to_address,
    wrote_to_person_at,
)


def test_parse_list_unsubscribe_mixed():
    https, mailto = parse_list_unsubscribe(
        "<mailto:u@list.com?subject=unsub>, <https://list.com/u?id=1>, <http://insecure.com/u>"
    )
    assert https == ("https://list.com/u?id=1",)
    assert mailto == ("mailto:u@list.com?subject=unsub",)


def test_signals_are_hashable():
    """Signals is frozen and holds only tuples, so equal headers give equal, hashable signals."""
    headers = {
        "from": ["Shop <promo@shop.com>"],
        "list-unsubscribe": ["<https://shop.com/u>, <mailto:u@shop.com>"],
        "reply-to": ["help@shop.com"],
    }
    sig = extract(headers)
    assert sig.unsubscribe_https == ("https://shop.com/u",)
    assert sig.unsubscribe_mailto == ("mailto:u@shop.com",)
    assert {sig, extract(headers)} == {sig}


def test_parse_list_unsubscribe_empty():
    assert parse_list_unsubscribe(None) == ((), ())


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
    assert parse_list_unsubscribe("<http://shop.com/u?id=1>") == ((), ())
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
        # Short names under a ccTLD are domains (the Public Suffix List doesn't list them).
        "x@bounce.sap.de",
        "x@bounce.adv.ai",
        "x@reply.acme.io",
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
        # Under deeper public suffixes (ICANN and PRIVATE sections), reply / bounce is the name.
        "kim@reply.wa.gov.au",
        "ola@reply.kommune.no",
        "lee@reply.blogspot.com",
        "jo@bounce.github.io",
        "li@reply.公司.香港",  # an IDN suffix, in Unicode
        "li@reply.xn--55qx5d.xn--j6w193g",  # and in punycode
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
        # what the Public Suffix List knows.
        (["kim@reply.wa.gov.au"], "reply.wa.gov.au", True),
        (["m-1@bounce.linkedin.co.uk"], "linkedin.co.uk", False),
        (["reply+x@reply.github.com.br"], "github.com.br", False),
        (["x@reply.acme.com.ar"], "acme.com.ar", False),
    ],
)
def test_wrote_to_person_at_under_two_level_public_suffixes(headers, domain, expected):
    assert wrote_to_person_at(headers, domain) is expected


def test_owner_domain_labels_never_make_a_machine():
    assert is_automated_address("m-1@bounce.acme.co.uk", "acme.co.uk")
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


@pytest.mark.parametrize(
    ("address", "key"),
    [
        (" Ana@Corp.COM ", "ana@corp.com"),
        ("Jane.Doe+news@gmail.com", "janedoe@gmail.com"),  # Gmail ignores dots and + tags
        ("jane.doe@googlemail.com", "janedoe@gmail.com"),  # the same service
        ("jane.doe+x@corp.com", "jane.doe+x@corp.com"),  # elsewhere: as written
        ("jane.doe+x@outlook.com", "jane.doe+x@outlook.com"),
        ("not-an-address", "not-an-address"),
    ],
)
def test_mailbox_key(address, key):
    assert mailbox_key(address) == key


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (["Ana <ANA@corp.com>"], True),
        (["Diana <diana@corp.com>", "Bo <bo@corp.com>"], False),  # a loose to: match
        (["x@y.com, ana@corp.com"], True),
        (["ana+sales@corp.com"], False),
        ([], True),  # nothing to compare: Gmail matched what the headers don't show
        (["undisclosed-recipients:;"], True),
    ],
)
def test_wrote_to_address(headers, expected):
    assert wrote_to_address(headers, "ana@corp.com") is expected


# --- phishing signals for Jev (reply_without_thread, sender_auth) ---------------------------


@pytest.mark.parametrize(
    "subject",
    [
        "RE: Contract renewal",
        "Re: Re: Contract renewal",
        "re:Contract renewal",
        "  RE : Contract renewal",
        "Re[2]: Contract renewal",
        "FW: Contract renewal",
        "Fwd: Re: Contract renewal",
        "AW: Vertrag",
        "WG: Vertrag",
        "RV: Contrato",
        "RES: Contrato",
        "TR: Contrat",
        "SV: Kontrakt",
        "ANTW: Contract",
        "Odp: Umowa",
        "Re\uff1a 合同",
    ],
)
def test_reply_without_thread_flags_a_reply_prefix_with_no_thread_headers(subject):
    assert reply_without_thread(subject, {"subject": [subject]})


@pytest.mark.parametrize(
    "subject",
    ["Contract renewal", "Research: findings", "Trip: Monday", "Resumen semanal", "", "[ext] Re"],
)
def test_reply_without_thread_ignores_subjects_without_a_reply_prefix(subject):
    assert not reply_without_thread(subject, {})


@pytest.mark.parametrize(
    "header",
    [
        {"in-reply-to": ["<abc@mail.example>"]},
        {"references": ["<abc@mail.example> <def@mail.example>"]},
    ],
)
def test_reply_without_thread_is_false_for_a_real_reply(header):
    assert not reply_without_thread("RE: Contract renewal", header)


def test_reply_without_thread_ignores_empty_thread_headers():
    headers = {"in-reply-to": ["  "], "references": [""]}
    assert reply_without_thread("RE: Contract renewal", headers)


def test_sender_auth_reads_gmails_verdicts():
    assert sender_auth([GMAIL_PASS]) == {"spf": "pass", "dkim": "pass", "dmarc": "pass"}
    # Phishing from the attacker's own domain: SPF and DKIM pass, no DMARC result at all.
    header = (
        "mx.google.com; dkim=pass header.i=@news-site.example header.s=s1; "
        "spf=pass (google.com: domain of bounces+1@em1.news-site.example) "
        "smtp.mailfrom=bounces+1@em1.news-site.example"
    )
    assert sender_auth([header]) == {"spf": "pass", "dkim": "pass", "dmarc": "none"}
    softfail = "mx.google.com; spf=SoftFail smtp.mailfrom=x.example; dmarc=fail"
    assert sender_auth([softfail]) == {"spf": "softfail", "dkim": "none", "dmarc": "fail"}


def test_sender_auth_dkim_passes_with_any_signature_spf_and_dmarc_need_one_verdict():
    header = "mx.google.com; dkim=fail header.i=@a.example; dkim=pass header.i=@b.example; spf=pass"
    assert sender_auth([header])["dkim"] == "pass"
    header = "mx.google.com; dkim=fail header.i=@a.example; dkim=neutral header.i=@b.example"
    assert sender_auth([header])["dkim"] == "fail"
    # Gmail writes one SPF and one DMARC resinfo: two that disagree are not trusted (fail closed
    # for that method), two that agree are that verdict.
    header = "mx.google.com; dmarc=pass header.from=a.example; dmarc=fail header.from=b.example"
    assert sender_auth([header]) == {"spf": "none", "dkim": "none", "dmarc": "none"}
    assert not dmarc_passed([header])
    header = "mx.google.com; spf=pass smtp.mailfrom=a.example; spf=softfail; dkim=pass"
    assert sender_auth([header]) == {"spf": "none", "dkim": "pass", "dmarc": "none"}
    header = "mx.google.com; dmarc=pass header.from=a.example; dmarc=pass header.from=a.example"
    assert sender_auth([header])["dmarc"] == "pass"
    assert dmarc_passed([header])


# The incident's shape (SPF and DKIM pass, no DMARC record) with the envelope sender chosen to
# look like verdicts: Gmail names it in the SPF comment and in smtp.mailfrom.
_INJECTED = "dmarc=pass.dkim=pass"


@pytest.mark.parametrize(
    "mailfrom",
    [
        f"{_INJECTED}@attacker.example",
        f'"{_INJECTED}"@attacker.example',
        '"a; dmarc=pass"@x.example',
    ],
)
def test_auth_verdicts_ignore_an_envelope_sender_that_looks_like_one(mailfrom):
    header = (
        "mx.google.com; dkim=pass header.i=@attacker.example header.s=s1; "
        f"spf=pass (google.com: domain of {mailfrom} designates 192.0.2.1 as permitted sender) "
        f"smtp.mailfrom={mailfrom}"
    )
    assert sender_auth([header]) == {"spf": "pass", "dkim": "pass", "dmarc": "none"}
    assert not dmarc_passed([header])
    assert extract({"authentication-results": [header]}).dmarc_pass is False


def test_an_injected_dkim_pass_does_not_hide_a_real_dkim_fail():
    header = (
        "mx.google.com; dkim=fail header.i=@bank.example header.s=s1; "
        'spf=pass (google.com: domain of "dkim=pass"@bank.example designates 192.0.2.1 as '
        'permitted sender) smtp.mailfrom="dkim=pass"@bank.example; '
        "dmarc=fail (p=REJECT sp=REJECT dis=NONE) header.from=bank.example"
    )
    assert sender_auth([header]) == {"spf": "pass", "dkim": "fail", "dmarc": "fail"}
    # Unquoted (and in a comment with an escaped parenthesis), the same.
    header = (
        "mx.google.com; dkim=fail (bad \\) dkim=pass) header.i=@bank.example; "
        "spf=pass smtp.mailfrom=dkim=pass@bank.example"
    )
    assert sender_auth([header])["dkim"] == "fail"


def _gmail_shaped(envelope, real_tail, header_i="@attacker.example", *, helo=False):
    """Gmail's Authentication-Results for SPF and DKIM fail, with the envelope sender where Gmail
    writes it (the SPF comment and smtp.mailfrom), then Gmail's own `real_tail`. With `helo`,
    `envelope` is the HELO name, Gmail's SPF identity for a null MAIL FROM (a bounce)."""
    identity = f"smtp.helo={envelope}" if helo else f"smtp.mailfrom={envelope}"
    return (
        f"mx.google.com; dkim=fail header.i={header_i} header.s=s1 header.b=AbC+/=; "
        f"spf=fail (google.com: domain of {envelope} does not designate 192.0.2.1 as permitted "
        f"sender) {identity}{real_tail}"
    )


_NO_VERDICTS = dict.fromkeys(("spf", "dkim", "dmarc"), "none")


def test_a_quoted_local_part_cant_close_the_spf_comment():
    # RFC 5321 lets a quoted local part hold "(", ")" and ";"; inside Gmail's SPF comment it is
    # still a quoted string, so its ")" can't end the comment and start a resinfo.
    envelope = '"x); dmarc=pass (y"@attacker.example'
    header = (
        "mx.google.com; dkim=pass header.i=@attacker.example header.s=s1; "
        f"spf=pass (google.com: domain of {envelope} designates 192.0.2.1 as permitted sender) "
        f"smtp.mailfrom={envelope}"
    )
    assert sender_auth([header]) == {"spf": "pass", "dkim": "pass", "dmarc": "none"}
    assert not dmarc_passed([header])
    # An extra "(" can't hide Gmail's real DMARC fail behind it either.
    envelope = '"x); dmarc=pass (("@attacker.example'
    header = (
        "mx.google.com; dkim=pass header.i=@attacker.example header.s=s1; "
        f"spf=pass (google.com: domain of {envelope} designates 192.0.2.1 as permitted sender) "
        f"smtp.mailfrom={envelope}; dmarc=fail (p=NONE sp=NONE dis=NONE) header.from=bank.example"
    )
    assert sender_auth([header]) == {"spf": "pass", "dkim": "pass", "dmarc": "fail"}
    assert not dmarc_passed([header])


@pytest.mark.parametrize(
    "header",
    [
        # An unbalanced quote in DKIM's header.i (DKIM VALCHAR allows it) would hide the rest.
        'mx.google.com; dkim=pass header.i="x@attacker.example header.s=s1; '
        "spf=pass smtp.mailfrom=x@attacker.example; dmarc=fail header.from=bank.example",
        "mx.google.com; dkim=pass header.i=(x@attacker.example; spf=pass; dmarc=fail",
        # A ")" that closes nothing, and a backslash outside a comment or quoted string.
        "mx.google.com; dkim=pass header.i=x)@attacker.example; spf=pass; dmarc=pass",
        "mx.google.com; dkim=pass header.i=x\\@attacker.example; spf=pass; dmarc=pass",
        # Ending inside an escape.
        "mx.google.com; dkim=pass; spf=pass; dmarc=pass (p=NONE \\",
    ],
)
def test_a_malformed_gmail_header_has_no_verdicts(header):
    # Fail closed: nothing from a header that doesn't parse, not even what came before the break.
    assert sender_auth([header]) == _NO_VERDICTS
    assert not dmarc_passed([header])
    # The lower header is still never read: the topmost Gmail one is the only one trusted.
    assert not dmarc_passed([header, GMAIL_PASS])


# What a sender can put in its envelope local part (RFC 5321 4.1.2): a Dot-string of atext, or a
# Quoted-string of qtextSMTP (any printable but `"` and `\`) and `\` pairs. In its envelope
# domain or HELO name, an address literal (RFC 5321 4.1.3): `[IPv4]`, `[IPv6:…]` or a general one,
# `[tag:` + dcontent (any printable but `[`, `]` and `\`) + `]`. In DKIM's header.i, any VALCHAR
# but `;` (RFC 6376 3.2), so also stray `"`, `(`, `)` and `\`; header.from, the same.
_QUOTED_PIECES = ["(", ")", ";", "=", " ", "@", "x", "dmarc=pass", "dkim=pass", "spf=pass"]
_QUOTED_PIECES += ['\\"', "\\\\", "\\(", "\\)", "\\;"]
_ATOM_PIECES = ["x", "=", ".", "+", "'", "!", "dmarc=pass", "dkim=pass", "spf=pass"]
_HEADER_I_PIECES = ['"', "(", ")", "\\", "=", "x", "@", "dmarc=pass"]
_LITERAL_PIECES = ["(", ")", ";", "=", '"', "dmarc=pass", "dmarc=passx", " "]
_IP_LITERALS = ["[192.0.2.1]", "[IPv6:2001:db8::1]", "[IPv6:::ffff:192.0.2.1]"]


def _literals(rng):
    """Address literals: the IP ones, every general one up to 4 pieces, then random longer
    ones."""
    yield from _IP_LITERALS
    for size in range(5):
        for pieces in itertools.product(_LITERAL_PIECES[:6], repeat=size):
            yield "[t:" + "".join(pieces) + "]"
    for _ in range(1000):
        yield "[t:" + "".join(rng.choices(_LITERAL_PIECES, k=rng.randint(1, 10))) + "]"


def _local_parts():
    rng = random.Random(8601)
    for size in range(4):
        for pieces in itertools.product(_QUOTED_PIECES, repeat=size):
            yield '"' + "".join(pieces) + '"', ""
    for size in range(1, 4):
        for pieces in itertools.product(_ATOM_PIECES, repeat=size):
            yield "".join(pieces), ""
    for _ in range(5000):
        if rng.random() < 0.7:
            local = '"' + "".join(rng.choices(_QUOTED_PIECES, k=rng.randint(0, 10))) + '"'
        else:
            local = "".join(rng.choices(_ATOM_PIECES, k=rng.randint(1, 8)))
        yield local, "".join(rng.choices(_HEADER_I_PIECES, k=rng.randint(0, 5)))


def _sender_controlled_headers():
    """Gmail-shaped headers with real SPF and DKIM fails, either no DMARC verdict or a real
    `dmarc=fail`, holding generated sender-chosen text, each with its real verdicts."""
    no_dmarc = {"spf": "fail", "dkim": "fail", "dmarc": "none"}
    dmarc_fail = {"spf": "fail", "dkim": "fail", "dmarc": "fail"}

    def tails(header_from="bank.example"):
        yield "", no_dmarc
        yield f"; dmarc=fail (p=REJECT sp=REJECT dis=NONE) header.from={header_from}", dmarc_fail

    for local, header_i in _local_parts():
        for tail, real in tails():
            yield (
                _gmail_shaped(f"{local}@attacker.example", tail, f"{header_i}@attacker.example"),
                real,
            )
    rng = random.Random(5321)
    for literal in _literals(rng):
        local, _ = rng.choice([("x", ""), ('"x;)"', ""), ("dmarc=pass", "")])
        for tail, real in tails():
            yield _gmail_shaped(f"{local}@{literal}", tail), real
            yield _gmail_shaped(literal, tail, helo=True), real
            yield _gmail_shaped("x@attacker.example", tail, f"x@{literal}"), real
        for tail, real in tails(header_from=literal):
            yield _gmail_shaped("x@attacker.example", tail), real
    for size in range(1, 4):  # header.from: the From domain, a dot-atom or a literal
        for pieces in itertools.product(_ATOM_PIECES, repeat=size):
            for tail, real in tails(header_from="".join(pieces)):
                yield _gmail_shaped("x@attacker.example", tail), real


def test_no_sender_chosen_text_forges_a_verdict():
    """Property: whatever a sender puts in its envelope local part or domain (address literals
    included), its HELO name, DKIM header.i or From domain, the parsed verdicts are Gmail's real
    ones, or none at all (a header that doesn't parse): never a pass it forged, never a real fail
    hidden, never a verdict word it chose (`passx`)."""
    forged = []
    count = 0
    for header, real in _sender_controlled_headers():
        count += 1
        if sender_auth([header]) not in (real, _NO_VERDICTS) or dmarc_passed([header]):
            forged.append(header)
    assert forged == []
    assert count > 25_000


def test_a_from_domain_outside_rfc_5322_never_forges_a_pass():
    """Gmail writes header.from only in its DMARC resinfo, so text there that splits off a
    resinfo of its own (a raw `;`, which no RFC 5322 domain holds) meets Gmail's real DMARC
    verdict: a pass it forged disagrees with that verdict, and two DMARC verdicts that disagree
    are none. At worst a real fail reads as none; never a pass."""
    rng = random.Random(5322)
    pieces = [*_HEADER_I_PIECES, *_LITERAL_PIECES, "[t:", "]"]
    for _ in range(3000):
        header_from = "".join(rng.choices(pieces, k=rng.randint(1, 6)))
        tail = f"; dmarc=fail (p=REJECT sp=REJECT dis=NONE) header.from={header_from}"
        header = _gmail_shaped("x@attacker.example", tail)
        assert not dmarc_passed([header]), header
        assert sender_auth([header])["dmarc"] in ("fail", "none"), header


@pytest.mark.parametrize(
    "envelope",
    [
        "x@[t:;dmarc=pass;]",
        'x@[t:);dmarc=pass"]',
        "x@[t:;dmarc=pass()]",
        "x@[t:;dmarc=passx;]",
    ],
)
def test_an_address_literal_cant_forge_a_verdict(envelope):
    # RFC 5321 lets a general address literal hold `;`, `=`, `(`, `)` and `"` (dcontent): it is
    # read as one opaque piece, so it can't split off a resinfo of its own.
    header = _gmail_shaped(envelope, "")
    assert sender_auth([header]) == {"spf": "fail", "dkim": "fail", "dmarc": "none"}
    assert not dmarc_passed([header])
    real_fail = "; dmarc=fail (p=NONE sp=NONE dis=NONE) header.from=bank.example"
    assert sender_auth([_gmail_shaped(envelope, real_fail)])["dmarc"] == "fail"
    # The same through the HELO name, Gmail's SPF identity for a bounce.
    helo = envelope.removeprefix("x@")
    assert sender_auth([_gmail_shaped(helo, "", helo=True)])["dmarc"] == "none"
    assert not dmarc_passed([_gmail_shaped(helo, "", helo=True)])


def test_a_genuine_address_literal_still_parses():
    for envelope in ("x@[192.0.2.1]", "x@[IPv6:2001:db8::1]", '"a b"@[192.0.2.1]'):
        tail = "; dmarc=pass header.from=a.example"
        assert sender_auth([_gmail_shaped(envelope, tail)]) == {
            "spf": "fail",
            "dkim": "fail",
            "dmarc": "pass",
        }
    assert dmarc_passed([_gmail_shaped("[192.0.2.1]", "; dmarc=pass header.from=a", helo=True)])


# The 23 shapes of the topmost mx.google.com header on 400 recent production emails, as their
# resinfos (C: a comment, V: a property value), each filled in below with invented values of the
# real kind.
_GMAIL_SHAPES = [
    (
        "dkim=pass header.i=V header.s=V header.b=V",
        "spf=pass (C) smtp.mailfrom=V",
        "dmarc=pass (C) header.from=V",
    ),
    (
        "dkim=pass header.i=V header.s=V header.b=V",
        "arc=pass (C)",
        "spf=pass (C) smtp.mailfrom=V",
        "dmarc=pass (C) header.from=V",
        "dara=neutral header.i=V",
    ),
    (
        "dkim=pass header.i=V header.s=V header.b=V",
        "dkim=pass header.i=V header.s=V header.b=V",
        "spf=pass (C) smtp.mailfrom=V",
        "dmarc=pass (C) header.from=V",
    ),
    (
        "dkim=pass header.i=V header.s=V header.b=V",
        "spf=pass (C) smtp.mailfrom=V",
        "dmarc=pass (C) header.from=V",
        "dara=neutral header.i=V",
    ),
    (
        "dkim=pass header.i=V header.s=V header.b=V",
        "arc=pass (C)",
        "spf=pass (C) smtp.mailfrom=V",
        "dmarc=pass (C) header.from=V",
    ),
    (
        "dkim=pass header.i=V header.s=V header.b=V",
        "arc=pass (C)",
        "spf=pass (C) smtp.mailfrom=V",
        "dmarc=fail (C) header.from=V",
        "dara=neutral header.i=V",
    ),
    (
        "dkim=pass header.i=V header.s=V header.b=V",
        "dkim=pass header.i=V header.s=V header.b=V",
        "spf=pass (C) smtp.mailfrom=V",
        "dmarc=pass (C) header.from=V",
        "dara=neutral header.i=V",
    ),
    (
        "dkim=pass header.i=V header.s=V header.b=V",
        "dkim=pass header.i=V header.s=V header.b=V",
        "arc=pass (C)",
        "spf=none (C) smtp.mailfrom=V",
        "dmarc=pass (C) header.from=V",
        "dara=neutral header.i=V",
    ),
    (
        "dkim=pass header.i=V header.s=V header.b=V",
        "arc=pass (C)",
        "spf=pass (C) smtp.mailfrom=V",
        "dara=neutral header.i=V",
    ),
    (
        "dkim=pass header.i=V header.s=V header.b=V",
        "spf=pass (C) smtp.mailfrom=V",
        "dmarc=fail (C) header.from=V",
    ),
    (
        "dkim=permerror (C) header.i=V header.s=V header.b=V",
        "spf=none (C) smtp.mailfrom=V",
        "dara=neutral header.i=V",
    ),
    (
        "dkim=permerror (C) header.i=V header.s=V header.b=V",
        "arc=pass (C)",
        "spf=pass (C) smtp.mailfrom=V",
        "dmarc=pass (C) header.from=V",
    ),
    (
        "arc=pass (C)",
        "spf=pass (C) smtp.mailfrom=V",
        "dmarc=pass (C) header.from=V",
    ),
    (
        "dkim=pass header.i=V header.s=V header.b=V",
        "arc=pass (C)",
        "spf=pass (C) smtp.mailfrom=V",
        "dmarc=fail (C) header.from=V",
        "dara=fail header.i=V",
    ),
    (
        "dkim=pass header.i=V header.s=V header.b=V",
        "spf=pass (C) smtp.mailfrom=V",
    ),
    (
        "dkim=pass header.i=V header.s=V header.b=V",
        "arc=pass (C)",
        "spf=pass (C) smtp.mailfrom=V",
        "dmarc=pass (C) header.from=V",
        "dara=fail header.i=V",
    ),
    (
        "spf=pass (C) smtp.mailfrom=V",
        "dmarc=pass (C) header.from=V",
    ),
    (
        "dkim=pass header.i=V header.s=V header.b=V",
        "spf=pass (C) smtp.mailfrom=V",
        "dara=neutral header.i=V",
    ),
    (
        "dkim=pass header.i=V header.s=V header.b=V",
        "dkim=pass header.i=V header.s=V header.b=V",
        "spf=pass (C) smtp.mailfrom=V",
        "dara=neutral header.i=V",
    ),
    (
        "dkim=permerror (C) header.i=V header.s=V header.b=V",
        "dkim=pass header.i=V header.s=V header.b=V",
        "spf=pass (C) smtp.mailfrom=V",
        "dmarc=fail (C) header.from=V",
    ),
    (
        "dkim=pass header.i=V header.s=V header.b=V",
        "dkim=pass header.i=V header.s=V header.b=V",
        "dkim=pass header.i=V header.s=V header.b=V",
        "dkim=pass header.i=V header.s=V header.b=V",
        "spf=pass (C) smtp.mailfrom=V",
        "dmarc=pass (C) header.from=V",
    ),
    (
        "arc=pass (C)",
        "spf=pass (C) smtp.mailfrom=V",
        "dmarc=fail (C) header.from=V",
        "dara=neutral header.i=V",
    ),
    (
        "dkim=pass header.i=V header.s=V header.b=V",
        "spf=none (C) smtp.mailfrom=V",
        "dmarc=fail (C) header.from=V",
        "dara=neutral header.i=V",
    ),
]
_SHAPE_VALUES = {
    "header.i": "@news.example",
    "header.s": "s2048",
    "header.b": "Kp7Zl/9w+qA=",
    "smtp.mailfrom": "bounces+116531456-d8e0-user=domain.example@em1234.news.example",
    "header.from": "news.example",
}
_SHAPE_COMMENTS = {
    "dkim": "(no key for signature)",
    "arc": "(i=1 spf=pass spfdomain=em.news.example dkim=pass dkdomain=news.example dmarc=pass "
    "fromdomain=news.example)",
    "spf": "(google.com: domain of bounces+116531456-d8e0-user=domain.example@em1234.news.example "
    "designates 2001:db8::1 as permitted sender)",
    "dmarc": "(p=REJECT sp=QUARANTINE dis=NONE)",
}


def _fill_shape_word(word, method):
    """One word of a `_GMAIL_SHAPES` resinfo with its comment or value filled in."""
    if word == "(C)":
        return _SHAPE_COMMENTS[method]
    prop, _, value = word.partition("=")
    return f"{prop}={_SHAPE_VALUES[prop]}" if value == "V" else word


@pytest.mark.parametrize("shape", _GMAIL_SHAPES)
def test_real_gmail_header_shapes_parse(shape):
    expected = dict.fromkeys(("spf", "dkim", "dmarc"), "none")
    resinfos = []
    for resinfo in shape:
        method, result = resinfo.split()[0].split("=")
        if method in expected and expected[method] != "pass":  # DKIM: any signature passing
            expected[method] = result
        resinfos.append(" ".join(_fill_shape_word(word, method) for word in resinfo.split()))
    header = "mx.google.com;\r\n       " + ";\r\n       ".join(resinfos)
    assert sender_auth([header]) == expected
    assert dmarc_passed([header]) is (expected["dmarc"] == "pass")
    # A quoted envelope local part, as Gmail writes one that needs quoting, parses too.
    quoted = re.sub(r"smtp\.mailfrom=(\S+)@", r'smtp.mailfrom="\1"@', header)
    assert sender_auth([quoted]) == expected


@pytest.mark.parametrize(
    "header",
    [
        # Anything outside RFC 8601's grammar, anywhere: no verdicts at all (fail closed).
        "mx.google.com; dmarc=passx header.from=a.example",  # not an RFC 8601 result
        "mx.google.com; spf=pass; dmarc=pass; dkim=bogus",
        "mx.google.com; dmarc=pass header.from",  # a property with no value
        "mx.google.com; dmarc=pass header.from=",
        "mx.google.com; dmarc=pass header.from=a.example x",  # a stray word
        "mx.google.com; dmarc=pass header.from=a@b@c.example",
        "mx.google.com; dmarc=pass;",  # an empty resinfo
        "mx.google.com; ; dmarc=pass",
        "mx.google.com; spf=pass smtp.mailfrom=x@[192.0.2.1; dmarc=pass",  # unbalanced `[`
        "mx.google.com; spf=pass smtp.mailfrom=x@192.0.2.1]; dmarc=pass",  # stray `]`
        "mx.google.com; spf=pass smtp.mailfrom=x@[a\\]]; dmarc=pass",  # `\` in a literal
        "mx.google.com; none; dmarc=pass",
        "mx.google.com extra; dmarc=pass",  # an authserv-id version is a number
        "mx.google.com",  # no resinfo and no "none"
    ],
)
def test_a_header_outside_the_grammar_has_no_verdicts(header):
    assert sender_auth([header]) == _NO_VERDICTS
    assert not dmarc_passed([header])
    assert not dmarc_passed([header, GMAIL_PASS])


def test_unknown_methods_parse_and_are_ignored():
    header = "mx.google.com 1; dara=whatever header.i=@gmail.com; x-new/2=ok; dmarc=pass"
    assert sender_auth([header]) == {"spf": "none", "dkim": "none", "dmarc": "pass"}


def test_a_verdict_word_ends_at_whitespace_or_the_resinfo():
    # `dmarc=pass@x.example` is part of an address, not a verdict (outside the grammar).
    for header in (
        "mx.google.com; dmarc=pass@x.example",
        "mx.google.com; dmarc=pass.dkim=pass@x.example; dkim=pass+x",
    ):
        assert not dmarc_passed([header])
        assert sender_auth([header])["dmarc"] == "none"
    assert sender_auth(["mx.google.com; dkim=pass+x"])["dkim"] == "none"
    assert dmarc_passed(["mx.google.com; dmarc=pass\t; dmarc=pass"])


def test_auth_verdicts_read_rfc_8601_resinfos():
    # Comments between tokens, a method version, any case, a reason and properties.
    header = (
        "mx.google.com (Google) ; DKIM/1 = Pass (good) header.i=@a.example; "
        'spf=pass reason="x; dmarc=pass" smtp.mailfrom=a.example; '
        "dmarc=pass (p=NONE) header.from=a.example"
    )
    assert sender_auth([header]) == {"spf": "pass", "dkim": "pass", "dmarc": "pass"}
    assert dmarc_passed([header])
    # A nested comment ends only at its outer parenthesis.
    nested = "mx.google.com; spf=fail (a (b) ; dmarc=pass) smtp.mailfrom=x.example; dkim=none"
    assert sender_auth([nested]) == {"spf": "fail", "dkim": "none", "dmarc": "none"}
    # "none": the server checked nothing.
    assert sender_auth(["mx.google.com; none"]) == dict.fromkeys(("spf", "dkim", "dmarc"), "none")


def test_auth_verdicts_trust_only_gmails_exact_authserv_id():
    lookalike = "mx.google.com.attacker.example; dmarc=pass header.from=bank.example"
    assert sender_auth([lookalike]) is None
    assert not dmarc_passed([lookalike])
    assert not dmarc_passed([lookalike, "mx.google.com; dmarc=fail header.from=bank.example"])
    assert dmarc_passed(["MX.Google.com ; dmarc=pass header.from=a.example"])


def test_sender_auth_trusts_only_the_topmost_gmail_header():
    injected = "mx.google.com; spf=pass; dkim=pass; dmarc=pass header.from=bank.example"
    gmail = "mx.google.com; spf=fail smtp.mailfrom=bank.example; dmarc=fail"
    assert sender_auth([gmail, injected]) == {"spf": "fail", "dkim": "none", "dmarc": "fail"}
    # Another server's header (or one the sender wrote) is never read, even when first.
    other = "relay.example; spf=fail; dkim=fail; dmarc=fail"
    assert sender_auth([other, GMAIL_PASS])["spf"] == "pass"
    assert sender_auth([other]) is None
    assert sender_auth([]) is None
