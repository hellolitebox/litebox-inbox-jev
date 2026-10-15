"""Deterministic signals parsed from email headers (no LLM)."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from email.utils import getaddresses, parseaddr
from typing import NamedTuple, TypeVar

from . import psl

_ANGLE = re.compile(r"<([^>]+)>")
# A plain lowercase address, safe to put in a Gmail search query or trust as a group's author.
ADDRESS = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")
# A plain lowercase domain name (example.org, mail.example.co.uk): dot-separated labels of
# letters, digits and hyphens, ending in a top-level domain of 2+ letters or an IDN TLD in
# punycode (xn--p1ai for .рф, xn--fiqs8s for .中国).
DOMAIN = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:[a-z]{2,}|xn--[a-z0-9-]+)$")


# A local part that says "don't reply": noreply, no-reply, no_reply, donotreply, do-not-reply,
# do_not_reply, alone or as one piece of a local part split on . _ + - (messages-noreply,
# noreply-billing, alerts.no-reply). Anything else ("norah", "noreplies", "reply") isn't.
_NO_REPLY = re.compile(r"(?:^|[._+-])(?:no[._-]?reply|do[._-]?not[._-]?reply)(?:$|[._+-])")
# Bounce handlers and VERP return paths (bounce@, bounces+…@, bounce-123-45@), reply-by-email
# tokens (reply+TOKEN@, or reply-TOKEN@ whose token has 3+ digits) and the mail system's own
# mailboxes. A bounce./bounce- local part needs 3+ digits after the separator too, so a shared
# mailbox like bounce.house@, bounce-rentals@ or reply-all2@ stays a person.
_MACHINE_LOCAL = re.compile(
    r"^(?:bounces?(?:$|[+=]|[._-](?=(?:\D*\d){3}))|reply\+.|reply-(?=(?:\D*\d){3})"
    r"|mailer-daemon$|postmaster$)"
)
# Subdomain labels of hosts that only take machine replies: m-a175…@bounce.linkedin.com,
# reply+…@reply.github.com. Only labels left of the registrable domain count (the Public Suffix
# List's, `psl.registrable_domain`), so reply.io, reply.co.uk, bounces.com.br or reply.wa.gov.au
# themselves don't.
_MACHINE_HOST_LABELS = frozenset({"bounce", "bounces", "reply"})


def is_no_reply_address(address: str) -> bool:
    """Whether `address`'s local part clearly says not to reply (see _NO_REPLY).

    Deliberately narrow: `notifications@`, `info@` or `reply@` are not no-reply.
    """
    local, at, _ = address.strip().lower().rpartition("@")
    return bool(at and _NO_REPLY.search(local))


def is_automated_address(address: str, owner_domain: str = "") -> bool:
    """Whether `address` is a machine mailbox: writing to it doesn't make its owner a contact.

    No-reply local parts, bounce/VERP and reply-token local parts, mailer-daemon, postmaster,
    and hosts under a bounce., bounces. or reply. subdomain of their registrable domain and of
    `owner_domain` (lowercase), when given and the host is on it: that domain is known to be
    someone's, so none of its own labels count. When unsure the answer is no: a false
    "automated" hides a real contact, which archives and unsubscribes more.
    """
    local, at, host = address.strip().lower().rpartition("@")
    if not (at and local and host):
        return False
    if _NO_REPLY.search(local) or _MACHINE_LOCAL.match(local):
        return True
    labels = psl.to_ascii(host).split(".")
    registrable = psl.registrable_domain(host)
    keep = registrable.count(".") + 1 if registrable else len(labels)
    if owner_domain and on_domain(host, owner_domain):
        keep = max(keep, owner_domain.count(".") + 1)
    return not _MACHINE_HOST_LABELS.isdisjoint(labels[:-keep])


def wrote_to_person_at(recipient_headers: list[str], domain: str) -> bool:
    """Whether a sent email with these To/Cc/Bcc values counts as writing to `domain`.

    It counts unless every recipient on `domain` (or a subdomain) is automated: a reply to a
    LinkedIn notification (m-…@bounce.linkedin.com) says nothing about knowing LinkedIn. Labels
    of `domain` itself never make a recipient a machine (jane@reply.com.ar on reply.com.ar).
    With no recipient on the domain at all (Gmail matched something these headers don't show),
    it counts, as it did before recipients were checked. `domain` must be lowercase.
    """
    addresses = [a.strip().lower() for _, a in getaddresses(recipient_headers) if a.strip()]
    on_it = [a for a in addresses if on_domain(domain_of(a), domain)]
    return not on_it or not all(is_automated_address(a, domain) for a in on_it)


def wrote_to_address(recipient_headers: list[str], address: str) -> bool:
    """Whether a sent email with these To/Cc/Bcc values went to `address` (lowercase).

    A recipient counts when it is the same mailbox (`mailbox_key`): Gmail's `to:` search also
    matches similar addresses, so a hit alone isn't proof. With no recipient at all in these
    headers (Gmail matched something they don't show), it counts, as `wrote_to_person_at` does.
    """
    addresses = [a for _, a in getaddresses(recipient_headers) if a.strip()]
    if not addresses:
        return True
    key = mailbox_key(address)
    return any(mailbox_key(a) == key for a in addresses)


# Consumer Gmail ignores dots and anything after a "+" in the local part, and googlemail.com is
# the same service. Google Workspace and other providers don't promise either (a `+` tag can be
# routed elsewhere, e.g. a ticket address `support+123@`), so their addresses compare as written.
_GMAIL_HOSTS = frozenset({"gmail.com", "googlemail.com"})


def mailbox_key(address: str) -> str:
    """`address` as a comparable mailbox: lowercased and trimmed; a consumer Gmail address
    (gmail.com, googlemail.com) without dots or a `+` tag in its local part, on gmail.com.

    Only Gmail's own rules are applied: other providers' plus-addressing and dot handling vary
    by provider and by mailbox, and treating two addresses as one when they aren't would make a
    stranger a known contact.
    """
    local, at, host = address.strip().lower().rpartition("@")
    if not at or host not in _GMAIL_HOSTS:
        return address.strip().lower()
    return local.partition("+")[0].replace(".", "") + "@gmail.com"


def domain_of(address: str) -> str:
    """The part after the last "@" (the whole string when there is none). Case is kept."""
    return address.rsplit("@", 1)[-1]


def on_domain(host: str, parent: str) -> bool:
    """Whether `host` is `parent` or one of its subdomains. Both must already be lowercase."""
    return host == parent or host.endswith("." + parent)


def first_header(headers: dict[str, list[str]], name: str) -> str | None:
    """The first value of header `name` (lowercased key), or None when it is absent."""
    values = headers.get(name)
    return values[0] if values else None


class AuthVerdicts(NamedTuple):
    """Gmail's SPF, DKIM and DMARC verdicts for one message, each one lowercased RFC 8601 result
    word ("none" when Gmail gave none, or when they can't be trusted)."""

    spf: str
    dkim: str
    dmarc: str


# Verdicts nobody vouches for: what Jev gets for a trusted group's author without Google's record.
UNKNOWN_AUTH = AuthVerdicts("none", "none", "none")


@dataclass(frozen=True)
class Signals:
    sender: str  # lowercased email address
    sender_domain: str
    list_id: str | None
    unsubscribe_https: tuple[str, ...] = ()
    unsubscribe_mailto: tuple[str, ...] = ()
    one_click: bool = False
    dmarc_pass: bool = False
    # RFC 2369 List-Post: set by discussion-list software (Google Groups, Mailman, groups.io,
    # LISTSERV, Discourse), not by ESPs or sequence tools. Its List-Unsubscribe leaves the list.
    is_discussion_list: bool = False
    # Author named by X-Original-Sender when the email looks relayed by a Google Group (see
    # original_sender). Not trusted on its own: resolve_relay moves it into `sender` only for
    # groups on the account's domains.
    group_author: str | None = None
    # Group address that relayed the email (e.g. hello@company.com), set by resolve_relay when
    # the author was moved into `sender`. None for direct mail and untrusted groups.
    relayed_by: str | None = None
    # Gmail's verdicts for the message `group_author` sent to the group, from the ARC record
    # Google added when the group received it (relay_author_auth). None without a group author
    # or without a record to trust. resolve_relay makes it `dmarc_pass` once the author is the
    # sender: `dmarc_pass` is the group's own verdict until then.
    author_auth: AuthVerdicts | None = None
    # Reply-To addresses (lowercased), empty when the header is absent.
    reply_to: tuple[str, ...] = ()

    @property
    def no_reply(self) -> bool:
        """A reply can't reach anyone: the sender is a no-reply address and so is every Reply-To.

        A Reply-To to any other address (a person, a support desk, a reply-by-email token) makes
        the email replyable, whatever From says.
        """
        return is_no_reply_address(self.sender) and all(
            is_no_reply_address(r) for r in self.reply_to
        )

    @property
    def has_unsubscribe(self) -> bool:
        return bool(self.unsubscribe_https or self.unsubscribe_mailto)

    @property
    def list_key(self) -> str:
        """Identity of the mailing list, used to unsubscribe once per list."""
        return self.list_id or self.sender_domain


def parse_list_unsubscribe(value: str | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """RFC 2369: comma-separated <uri> entries. Returns (https urls, mailto uris).

    Every other scheme is dropped, plain `http://` included: a one-click POST or a manual link
    over http can be read or rewritten on the way and only confirms the address. A header with
    nothing but http URIs counts as no List-Unsubscribe, so the sender is unverified.
    """
    https: list[str] = []
    mailto: list[str] = []
    for uri in _ANGLE.findall(value or ""):
        uri = uri.strip()
        lower = uri.lower()
        if lower.startswith("https://"):
            https.append(uri)
        elif lower.startswith("mailto:"):
            mailto.append(uri)
    return tuple(https), tuple(mailto)


# The authserv-id Gmail stamps on its own Authentication-Results header.
GMAIL_AUTHSERV_ID = "mx.google.com"
AUTH_METHODS = ("spf", "dkim", "dmarc")
# The methods `_parse_auth_results` returns verdicts for: the three above, and ARC (RFC 8617),
# whose verdict in Gmail's own header says whether Gmail validated the message's ARC chain.
_VERDICT_METHODS = (*AUTH_METHODS, "arc")
# The result words RFC 8601 2.7 defines for these methods, plus "hardfail", the legacy synonym of
# SPF "fail" (RFC 5451). A Gmail header that gives one of them any other word is not trusted
# (`_parse_auth_results`). ARC's results (`pass`, `fail`, `none`) are among them.
AUTH_RESULTS = frozenset(
    {"pass", "fail", "softfail", "hardfail", "neutral", "none", "temperror", "permerror", "policy"}
)

# The opaque pieces of an Authentication-Results header, which can carry text a sender chose: a
# quoted string (with `\` pairs) and a domain literal (RFC 5321 `[...]`, whose dcontent excludes
# `[`, `]` and `\` but includes `;`, `(`, `)` and `"`); then a `\` pair, a parenthesis, or a run
# of anything else. An unterminated quoted string or literal, a stray `]` or a final lone `\`
# matches none of them.
_HEADER_PIECE = re.compile(r'"(?:[^"\\]|\\.)*"|\[[^\[\]\\]*\]|\\.|[()]|[^"\\()\[\]]+', re.DOTALL)


def _reduce_free_text(value: str) -> tuple[str, bool]:
    """`value` with every comment (`(...)`, nested) replaced by a space, every quoted string by
    `""` and every domain literal by `[]`, and whether `value` is well-formed.

    These are the parts of an Authentication-Results header that carry free text, such as the
    envelope sender in the SPF comment and in `smtp.mailfrom`. Each is read as one opaque piece,
    in a comment or not, so text a sender chose can't close the comment around it or split off a
    resinfo of its own. A header that ends inside a comment, a quoted string, a literal or an
    escape, closes a comment it never opened, or has a stray `]` or a `\\` outside all three is
    not well-formed; the text returned then stops at the break.
    """
    out: list[str] = []
    depth = pos = 0
    for match in _HEADER_PIECE.finditer(value):
        piece = match.group()
        if match.start() != pos:  # an unterminated quoted string or literal, a `]`, a final `\`
            break
        pos = match.end()
        if piece == "(":
            depth += 1
        elif piece == ")":
            if not depth:
                return "".join(out), False
            depth -= 1
            if not depth:
                out.append(" ")
        elif depth:
            continue
        elif piece[0] == '"':
            out.append('""')
        elif piece[0] == "[":
            out.append("[]")
        elif piece[0] == "\\":  # no RFC 8601 token holds one (RFC 2045 tspecials)
            return "".join(out), False
        else:
            out.append(piece)
    return "".join(out), pos == len(value) and not depth


# RFC 8601 2.2's grammar, over `_reduce_free_text`'s output with whitespace collapsed to one
# space (so " ?" is an optional CFWS and " " a required one). A value is a run without
# whitespace, `;`, `@` or a delimiter `_reduce_free_text` keeps paired: wider than RFC 2045's
# token, because Gmail's `header.b` is base64 (`/`, `+`, `=`) and envelope local parts hold `=`
# and `+` (`bounces+1-user=domain.example@…`). It can never hold a `;`, so it can't end a resinfo.
_KEYWORD = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_VALUE = r'(?:""|[^\s;()"\\\[\]@]+)'
_DOMAIN = r'(?:\[\]|[^\s;()"\\\[\]@=/]+)'
_PVALUE = rf"(?:\[\]|{_VALUE}?@{_DOMAIN}|{_VALUE})"
_AUTHSERV = re.compile(rf" ?({_VALUE})(?: [0-9]+)? ?")
_RESINFO = re.compile(
    rf" ?(?P<method>{_KEYWORD}) ?(?:/ ?[0-9]+ ?)?= ?(?P<result>{_KEYWORD})"
    rf"(?: reason ?= ?{_VALUE})?"
    rf"(?P<properties>(?: {_KEYWORD} ?\. ?{_KEYWORD} ?= ?{_PVALUE})*) ?"
)
# One `ptype.property=pvalue` of a resinfo that `_RESINFO` matched (its `properties` group).
_PROPERTY = re.compile(rf" ({_KEYWORD}) ?\. ?({_KEYWORD}) ?= ?({_PVALUE})")


class _AuthResults(NamedTuple):
    authserv_id: str  # lowercased
    verdicts: list[tuple[str, str]]  # (method, result) for `_VERDICT_METHODS`, lowercased
    # The `header.from` of each DMARC resinfo: lowercased, without a final dot.
    dmarc_header_from: frozenset[str]


def _parse_auth_results(header: str) -> _AuthResults:
    """An Authentication-Results header's authserv-id (lowercased), its (method, result)
    verdicts for `_VERDICT_METHODS`, lowercased, in order (RFC 8601 2.2), and the `header.from`
    its DMARC resinfos name.

    The header must match RFC 8601's grammar exactly (an allowlist, not a search for verdicts):
    `authserv-id [version] ; resinfo ; …` or `authserv-id ; none`, each resinfo being
    `method[/version]=result [reason=…] [ptype.property=pvalue …]`, once comments, quoted
    strings and domain literals are reduced to opaque pieces (`_reduce_free_text`). Only the
    `method=result` that starts a resinfo is a verdict, and an SPF, DKIM, DMARC or ARC result
    must be an RFC 8601 word (`AUTH_RESULTS`). A header with anything else anywhere (a stray `;` or
    delimiter, a property with no value, `dmarc=passx`) has no verdicts at all: fail closed,
    never what came before the break. The authserv-id is still read from such a header, so a
    malformed Gmail header is still the one trusted (`_gmail_verdicts`), with nothing in it.
    Other methods (`dara`) must parse but are dropped. An ARC-Authentication-Results header is
    read the same way once its `i=N;` is taken off (`_arc_auth_results`). A malformed header
    names no `header.from` either.
    """
    text, well_formed = _reduce_free_text(header)
    authserv, *resinfos = re.sub(r"\s+", " ", text).split(";")
    id_tokens = authserv.split()
    nothing = _AuthResults(id_tokens[0].lower() if id_tokens else "", [], frozenset())
    if not well_formed or not _AUTHSERV.fullmatch(authserv) or not resinfos:
        return nothing
    if len(resinfos) == 1 and resinfos[0].strip().lower() == "none":
        return nothing
    verdicts: list[tuple[str, str]] = []
    dmarc_header_from: set[str] = set()
    for resinfo in resinfos:
        match = _RESINFO.fullmatch(resinfo)
        if match is None:
            return nothing
        method, result = match["method"].lower(), match["result"].lower()
        if method in _VERDICT_METHODS:
            if result not in AUTH_RESULTS:
                return nothing
            verdicts.append((method, result))
        if method == "dmarc":
            dmarc_header_from.update(
                value.lower().removesuffix(".")
                for ptype, prop, value in _PROPERTY.findall(match["properties"])
                if (ptype.lower(), prop.lower()) == ("header", "from")
            )
    return nothing._replace(verdicts=verdicts, dmarc_header_from=frozenset(dmarc_header_from))


def _gmail_verdicts(authentication_results: list[str]) -> list[tuple[str, str]] | None:
    """The verdicts (`_parse_auth_results`) of the one Authentication-Results header to trust:
    the topmost one whose authserv-id is exactly `GMAIL_AUTHSERV_ID`; None without one.

    A sender can inject its own Authentication-Results header, even one claiming to be
    mx.google.com. Gmail prepends its own, so only the topmost mx.google.com header is trusted.
    Without one, nothing is trusted: Gmail stamps every inbound email, and any other header
    (`mx.google.com.attacker.example` included) could have been written by the sender.
    """
    for header in authentication_results:
        parsed = _parse_auth_results(header)
        if parsed.authserv_id == GMAIL_AUTHSERV_ID:
            return parsed.verdicts
    return None


def _method_verdict(verdicts: list[tuple[str, str]], method: str) -> str:
    """One method's verdict among `verdicts`: "none" when it has none. DKIM is "pass" when any
    signature passes (one valid signature authenticates the message), else its first verdict.
    SPF and DMARC are checked once per message (Gmail writes one resinfo each), so two that
    disagree are not trusted: "none", fail closed for that method.
    """
    found = [result for m, result in verdicts if m == method]
    if not found:
        return "none"
    if method == "dkim":
        return "pass" if "pass" in found else found[0]
    return found[0] if len(set(found)) == 1 else "none"


def dmarc_passed(authentication_results: list[str]) -> bool:
    """True if Gmail's Authentication-Results (`_gmail_verdicts`) reports dmarc=pass, and no
    other DMARC verdict (`_method_verdict`)."""
    return _method_verdict(_gmail_verdicts(authentication_results) or [], "dmarc") == "pass"


def sender_auth(authentication_results: list[str]) -> dict[str, str] | None:
    """SPF, DKIM and DMARC verdicts from Gmail's own Authentication-Results, for Jev.

    Only the header `_gmail_verdicts` trusts is read; None without one (the verdicts are
    unknown, which is not the same as failing). Each method's verdict is `_method_verdict`'s:
    "none" when the header has none for it, and for every method when that header doesn't parse
    (`_parse_auth_results`). A verdict is one lowercased RFC 8601 result word, never free text
    from the header.
    """
    parsed = _gmail_verdicts(authentication_results)
    if parsed is None:
        return None
    return _auth_verdicts(parsed)._asdict()


def _auth_verdicts(verdicts: list[tuple[str, str]]) -> AuthVerdicts:
    """Each of `AUTH_METHODS`' verdict among `verdicts` (`_method_verdict`)."""
    return AuthVerdicts(*(_method_verdict(verdicts, method) for method in AUTH_METHODS))


# The signing domain (`d=`) of the ARC sets Google adds: Google Groups when a group receives a
# message, and Gmail when it delivers the group's copy. Only Google can sign for it.
GOOGLE_ARC_SEALER = "google.com"
# RFC 8617 4.2.1: an ARC set's instance (`i=`) is 1 to 50, written without leading zeros here.
_ARC_INSTANCE = re.compile(r"[1-9][0-9]?")
_ARC_MAX_INSTANCE = 50
# RFC 8617 4.1.1: an ARC-Authentication-Results is `i=N;` and then an Authentication-Results.
_ARC_AUTH_RESULTS = re.compile(
    r"[ \t\r\n]*i[ \t\r\n]*=[ \t\r\n]*([0-9]+)[ \t\r\n]*;(.*)", re.DOTALL
)
# RFC 6376 3.2, the tag-list of an ARC-Seal and ARC-Message-Signature: `name=value` pieces split
# on `;`, a value being printable ASCII but `;`, with folding whitespace inside.
_TAG_SPEC = re.compile(
    r"[ \t\r\n]*([A-Za-z][A-Za-z0-9_]*)[ \t\r\n]*=[ \t\r\n]*"
    r"((?:[\x21-\x3a\x3c-\x7e]+(?:[ \t\r\n]+[\x21-\x3a\x3c-\x7e]+)*)?)[ \t\r\n]*"
)

_T = TypeVar("_T")


class _ArcSeal(NamedTuple):
    cv: str  # chain validation status the sealer found: none (first set), pass or fail
    sealer: str  # `d=`, lowercased


def _tag_list(header: str) -> dict[str, str] | None:
    """An ARC-Seal or ARC-Message-Signature's tags (RFC 6376 3.2), or None when it doesn't
    parse or names a tag twice (RFC 6376 3.2: such a header is invalid). A final `;` is allowed.
    """
    pieces = header.split(";")
    if len(pieces) > 1 and not pieces[-1].strip():
        pieces.pop()
    tags: dict[str, str] = {}
    for piece in pieces:
        match = _TAG_SPEC.fullmatch(piece)
        if match is None or match[1] in tags:
            return None
        tags[match[1]] = match[2]
    return tags


def _arc_instance(value: str) -> int | None:
    """An ARC instance number (1 to 50), or None when `value` isn't one."""
    if not _ARC_INSTANCE.fullmatch(value) or int(value) > _ARC_MAX_INSTANCE:
        return None
    return int(value)


def _arc_seal(header: str) -> tuple[int, _ArcSeal] | None:
    """An ARC-Seal's instance, `cv=` and `d=`, or None when it lacks one or doesn't parse."""
    tags = _tag_list(header)
    instance = _arc_instance(tags.get("i", "")) if tags else None
    if tags is None or instance is None or not tags.get("cv") or not tags.get("d"):
        return None
    return instance, _ArcSeal(tags["cv"].lower(), tags["d"].lower())


def _arc_message_signature(header: str) -> tuple[int, None] | None:
    """An ARC-Message-Signature's instance, or None when it lacks one or doesn't parse."""
    tags = _tag_list(header)
    instance = _arc_instance(tags.get("i", "")) if tags else None
    return None if instance is None else (instance, None)


def _arc_auth_results(header: str) -> tuple[int, str] | None:
    """An ARC-Authentication-Results' instance and the Authentication-Results after its `i=N;`,
    or None when it doesn't start with a valid `i=N;`."""
    match = _ARC_AUTH_RESULTS.fullmatch(header)
    instance = _arc_instance(match[1]) if match else None
    return None if match is None or instance is None else (instance, match[2])


def _by_instance(parsed: list[tuple[int, _T] | None]) -> dict[int, _T] | None:
    """`parsed` ARC headers of one kind keyed by instance, or None when one didn't parse or two
    share an instance (RFC 8617 5.1.1: the chain is then invalid)."""
    by_instance: dict[int, _T] = {}
    for item in parsed:
        if item is None or item[0] in by_instance:
            return None
        by_instance[item[0]] = item[1]
    return by_instance


def relay_author_auth(headers: dict[str, list[str]], author: str) -> AuthVerdicts | None:
    """Gmail's verdicts for the message a Google Group received from `author` (its
    X-Original-Sender, a lowercase address), or None when there is no record of them to trust.

    A group re-sends the author's message from its own address, so the Authentication-Results
    Gmail writes on delivery (`sender_auth`) authenticates the group, not the author. Google
    records the author's verdicts in the ARC set it adds when the group receives the message
    (RFC 8617): Google Groups adds two, both with the author's verdicts, and Gmail adds one more
    on delivery, with its own. The one read is the ARC-Authentication-Results just below Gmail's
    delivery set (instance N-1 of N), trusted only when all of these hold:

    - Gmail's own Authentication-Results (the one `_gmail_verdicts` trusts) says `arc=pass`:
      Gmail checked every ARC-Seal's signature. A sender can write ARC headers but can't sign
      for google.com, so a forged set breaks the chain (`arc=fail`).
    - Each kind of ARC header (ARC-Seal, ARC-Message-Signature, ARC-Authentication-Results)
      parses and has exactly one per instance, numbered 1 to N with N of at least 3.
    - Every seal's `cv` is `none` at instance 1 and `pass` after it.
    - The seals at N, N-1 and N-2 are signed by google.com (`d=`): N is Gmail's delivery set,
      N-1 and N-2 Google Groups' two records of the message the group received.
    - The ARC-Authentication-Results at N-1 has the authserv-id `mx.google.com` and is parsed
      like Gmail's own header (`_parse_auth_results`, strict).
    - That record is about `author`'s message: its DMARC resinfo's `header.from` (the domain of
      the From the message had, which Google writes inside the sealed record) is exactly
      `author`'s domain, and it names no other. One more Google hop after the group (a nested
      group, a set Google adds to its layout) puts Google's record of the group's own copy at
      N-1, with `header.from` the group's domain and the group's pass: that one is not the
      author's. A record without `header.from`, or a malformed one, is not bound to the author
      either.
    - The ARC-Authentication-Results at N-2 is Google Groups' second record of the same message:
      it parses to the same authserv-id, verdicts and `header.from` as the one at N-1. A single
      extra Google set after the group (Google's record of the group's copy, the group's pass)
      differs from the author's record below it, so it is not read even when the author is on
      the group's own domain, where the `header.from` binding can't tell the two apart.

    Gmail's `arc=pass` also comes with a comment summarising the author's verdicts, but a
    comment is free text that is never read (ADR 0043).
    """
    top = _gmail_verdicts(headers.get("authentication-results", []))
    if top is None or _method_verdict(top, "arc") != "pass":
        return None
    seals = _by_instance([_arc_seal(h) for h in headers.get("arc-seal", [])])
    signatures = _by_instance(
        [_arc_message_signature(h) for h in headers.get("arc-message-signature", [])]
    )
    results = _by_instance(
        [_arc_auth_results(h) for h in headers.get("arc-authentication-results", [])]
    )
    if seals is None or signatures is None or results is None:
        return None
    last = len(seals)
    instances = set(range(1, last + 1))
    if last < 3 or not set(seals) == set(signatures) == set(results) == instances:
        return None
    if any(seal.cv != ("none" if i == 1 else "pass") for i, seal in seals.items()):
        return None
    if {seals[i].sealer for i in (last, last - 1, last - 2)} != {GOOGLE_ARC_SEALER}:
        return None
    record = _parse_auth_results(results[last - 1])
    if record.authserv_id != GMAIL_AUTHSERV_ID:
        return None
    if record.dmarc_header_from != {domain_of(author)}:
        return None
    if _parse_auth_results(results[last - 2]) != record:
        return None
    return _auth_verdicts(record.verdicts)


# Reply and forward prefixes in the languages mail clients localise them to: RE, FW, FWD; AW and
# WG (German), RV and RES (Spanish, Portuguese), TR (French), SV and VS (Nordic), ANTW (Dutch),
# ODP (Polish), ENC (Portuguese). Case-insensitive, optionally numbered (`Re[2]:`, `RE(3):`),
# with a colon or a full-width colon, possibly repeated ("Re: Fwd: ").
_REPLY_PREFIX = re.compile(
    r"^\s*(?:re|fwd?|aw|wg|rv|res|tr|sv|vs|antw|odp|enc)\s*(?:[\[(]\d+[\])])?\s*[:\uff1a]",
    re.IGNORECASE,
)


def reply_without_thread(subject: str, headers: dict[str, list[str]]) -> bool:
    """Whether `subject` claims a reply or forward (`_REPLY_PREFIX`) but the message names no
    earlier one: no In-Reply-To and no References header (`headers` keyed by lowercased name).

    A real reply carries them (RFC 5322 3.6.4); a "RE:" with neither is a fake reply, a
    phishing trick to look like an ongoing conversation. Some clients forward without them,
    so it is a signal for Jev, not a verdict.
    """
    if not _REPLY_PREFIX.match(subject):
        return False
    return not any(
        (first_header(headers, name) or "").strip() for name in ("in-reply-to", "references")
    )


def sender_address(from_header: str | None) -> str:
    return parseaddr(from_header or "")[1].strip().lower()


_MAILING_LIST = re.compile(r"^\s*list\s+([^;\s]+)", re.IGNORECASE)


def original_sender(
    headers: dict[str, list[str]], from_addr: str, *, dmarc_pass: bool
) -> str | None:
    """The claimed author of an email relayed by a Google Group, or None.

    Google Groups rewrites From to the group ("'Jane' via Hello <hello@co.com>") and keeps the
    author in X-Original-Sender. These checks need dmarc=pass for From and a Mailing-list header
    naming that same address, but anyone with a DMARC-passing domain can write both headers:
    the result is only a candidate, see resolve_relay.
    """
    match = _MAILING_LIST.match(first_header(headers, "mailing-list") or "")
    if not (dmarc_pass and match and match.group(1).lower() == from_addr):
        return None
    original = sender_address(first_header(headers, "x-original-sender"))
    if not ADDRESS.match(original) or original == from_addr:
        return None
    return original


def extract(headers: dict[str, list[str]]) -> Signals:
    """Build Signals from headers keyed by lowercased name (multi-valued)."""
    sender = sender_address(first_header(headers, "from"))
    dmarc_pass = dmarc_passed(headers.get("authentication-results", []))
    https, mailto = parse_list_unsubscribe(first_header(headers, "list-unsubscribe"))
    post = (first_header(headers, "list-unsubscribe-post") or "").replace(" ", "").lower()
    list_id_raw = first_header(headers, "list-id")
    list_id = None
    if list_id_raw:
        match = _ANGLE.search(list_id_raw)
        list_id = (match.group(1) if match else list_id_raw).strip().lower() or None
    group_author = original_sender(headers, sender, dmarc_pass=dmarc_pass)
    return Signals(
        sender=sender,
        sender_domain=domain_of(sender),
        list_id=list_id,
        unsubscribe_https=https,
        unsubscribe_mailto=mailto,
        one_click="list-unsubscribe=one-click" in post and bool(https),
        dmarc_pass=dmarc_pass,
        is_discussion_list=bool((first_header(headers, "list-post") or "").strip()),
        group_author=group_author,
        author_auth=relay_author_auth(headers, group_author) if group_author else None,
        reply_to=tuple(
            a.strip().lower() for _, a in getaddresses(headers.get("reply-to", [])) if a.strip()
        ),
    )


def resolve_relay(sig: Signals, own_domains: list[str]) -> Signals:
    """Use the group's author as the sender, only for groups on one of `own_domains`.

    `own_domains` is the account's `group_domains` (opt-in, empty by default). Only that
    Workspace can produce dmarc=pass for a group address on it, so its X-Original-Sender is
    Google's. Any other group (an attacker's domain, @googlegroups.com, a public provider) keeps
    sender = From.

    Once the author is the sender, `dmarc_pass` is the author's (`author_auth`), not the group's:
    the group's pass, which `original_sender` already required, says nothing about the author,
    and without Google's record the author's DMARC is unknown, so it doesn't pass.
    """
    if not sig.group_author or sig.relayed_by:
        return sig
    domains = [d.lower().strip() for d in own_domains if d.strip()]
    if not any(on_domain(sig.sender_domain, d) for d in domains):
        return sig
    author = sig.group_author
    return replace(
        sig,
        sender=author,
        sender_domain=domain_of(author),
        relayed_by=sig.sender,
        dmarc_pass=(sig.author_auth or UNKNOWN_AUTH).dmarc == "pass",
    )
