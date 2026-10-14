"""Deterministic signals parsed from email headers (no LLM)."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from email.utils import getaddresses, parseaddr

from . import psl

_ANGLE = re.compile(r"<([^>]+)>")
_DMARC = re.compile(r"\bdmarc=(\w+)", re.IGNORECASE)
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


def dmarc_passed(authentication_results: list[str]) -> bool:
    """True if Gmail's Authentication-Results reports dmarc=pass.

    A sender can inject its own Authentication-Results header, even one claiming to be
    mx.google.com. Gmail prepends its own, so only the topmost mx.google.com header is trusted;
    every verdict in it must be "pass". Without one, nothing is trusted: Gmail stamps every
    inbound email, and any other header could have been written by the sender.
    """
    gmail = [h for h in authentication_results if h.strip().lower().startswith("mx.google.com")]
    verdicts = [m.lower() for h in gmail[:1] for m in _DMARC.findall(h)]
    return bool(verdicts) and all(v == "pass" for v in verdicts)


def sender_address(from_header: str | None) -> str:
    return parseaddr(from_header or "")[1].strip().lower()


_MAILING_LIST = re.compile(r"^\s*list\s+([^;\s]+)", re.IGNORECASE)


def original_sender(headers: dict[str, list[str]], from_addr: str, dmarc_pass: bool) -> str | None:
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
    return Signals(
        sender=sender,
        sender_domain=domain_of(sender),
        list_id=list_id,
        unsubscribe_https=https,
        unsubscribe_mailto=mailto,
        one_click="list-unsubscribe=one-click" in post and bool(https),
        dmarc_pass=dmarc_pass,
        is_discussion_list=bool((first_header(headers, "list-post") or "").strip()),
        group_author=original_sender(headers, sender, dmarc_pass),
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
    """
    if not sig.group_author or sig.relayed_by:
        return sig
    domains = [d.lower().strip() for d in own_domains if d.strip()]
    if not any(on_domain(sig.sender_domain, d) for d in domains):
        return sig
    author = sig.group_author
    return replace(sig, sender=author, sender_domain=domain_of(author), relayed_by=sig.sender)
