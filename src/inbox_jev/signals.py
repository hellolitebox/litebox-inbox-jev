"""Deterministic signals parsed from email headers (no LLM)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from email.utils import parseaddr

_ANGLE = re.compile(r"<([^>]+)>")
_DMARC = re.compile(r"\bdmarc=(\w+)", re.IGNORECASE)


@dataclass(frozen=True)
class Signals:
    sender: str  # lowercased email address
    sender_domain: str
    list_id: str | None
    unsubscribe_https: list[str] = field(default_factory=list)
    unsubscribe_mailto: list[str] = field(default_factory=list)
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

    @property
    def has_unsubscribe(self) -> bool:
        return bool(self.unsubscribe_https or self.unsubscribe_mailto)

    @property
    def list_key(self) -> str:
        """Identity of the mailing list, used to unsubscribe once per list."""
        return self.list_id or self.sender_domain


def parse_list_unsubscribe(value: str | None) -> tuple[list[str], list[str]]:
    """RFC 2369: comma-separated <uri> entries. Returns (https urls, mailto uris).

    Every other scheme is dropped, plain `http://` included: a one-click POST or a manual link
    over http can be read or rewritten on the way and only confirms the address. A header with
    nothing but http URIs counts as no List-Unsubscribe, so the sender is unverified.
    """
    https, mailto = [], []
    for uri in _ANGLE.findall(value or ""):
        uri = uri.strip()
        lower = uri.lower()
        if lower.startswith("https://"):
            https.append(uri)
        elif lower.startswith("mailto:"):
            mailto.append(uri)
    return https, mailto


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
_ADDRESS = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")


def original_sender(headers: dict[str, list[str]], from_addr: str, dmarc_pass: bool) -> str | None:
    """The claimed author of an email relayed by a Google Group, or None.

    Google Groups rewrites From to the group ("'Jane' via Hello <hello@co.com>") and keeps the
    author in X-Original-Sender. These checks need dmarc=pass for From and a Mailing-list header
    naming that same address, but anyone with a DMARC-passing domain can write both headers:
    the result is only a candidate, see resolve_relay.
    """

    def first(name: str) -> str:
        values = headers.get(name)
        return values[0] if values else ""

    match = _MAILING_LIST.match(first("mailing-list"))
    if not (dmarc_pass and match and match.group(1).lower() == from_addr):
        return None
    original = sender_address(first("x-original-sender"))
    if not _ADDRESS.match(original) or original == from_addr:
        return None
    return original


def extract(headers: dict[str, list[str]]) -> Signals:
    """Build Signals from headers keyed by lowercased name (multi-valued)."""

    def first(name: str) -> str | None:
        values = headers.get(name)
        return values[0] if values else None

    sender = sender_address(first("from"))
    dmarc_pass = dmarc_passed(headers.get("authentication-results", []))
    https, mailto = parse_list_unsubscribe(first("list-unsubscribe"))
    post = (first("list-unsubscribe-post") or "").replace(" ", "").lower()
    list_id_raw = first("list-id")
    list_id = None
    if list_id_raw:
        match = _ANGLE.search(list_id_raw)
        list_id = (match.group(1) if match else list_id_raw).strip().lower() or None
    return Signals(
        sender=sender,
        sender_domain=sender.rsplit("@", 1)[-1] if "@" in sender else sender,
        list_id=list_id,
        unsubscribe_https=https,
        unsubscribe_mailto=mailto,
        one_click="list-unsubscribe=one-click" in post and bool(https),
        dmarc_pass=dmarc_pass,
        is_discussion_list=bool((first("list-post") or "").strip()),
        group_author=original_sender(headers, sender, dmarc_pass),
    )


def _on_domain(domain: str, parent: str) -> bool:
    return domain == parent or domain.endswith("." + parent)


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
    if not any(_on_domain(sig.sender_domain, d) for d in domains):
        return sig
    author = sig.group_author
    return replace(
        sig, sender=author, sender_domain=author.rsplit("@", 1)[-1], relayed_by=sig.sender
    )
