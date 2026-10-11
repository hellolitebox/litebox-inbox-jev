"""Deterministic signals parsed from email headers (no LLM)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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

    @property
    def has_unsubscribe(self) -> bool:
        return bool(self.unsubscribe_https or self.unsubscribe_mailto)

    @property
    def list_key(self) -> str:
        """Identity of the mailing list, used to unsubscribe once per list."""
        return self.list_id or self.sender_domain


def parse_list_unsubscribe(value: str | None) -> tuple[list[str], list[str]]:
    """RFC 2369: comma-separated <uri> entries. Returns (https urls, mailto uris)."""
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

    A sender can inject its own Authentication-Results header, so only the ones stamped by
    mx.google.com are trusted when present; every remaining verdict must be "pass".
    """
    trusted = [
        h for h in authentication_results if h.strip().lower().startswith("mx.google.com")
    ] or authentication_results
    verdicts = [m.lower() for h in trusted for m in _DMARC.findall(h)]
    return bool(verdicts) and all(v == "pass" for v in verdicts)


def sender_address(from_header: str | None) -> str:
    return parseaddr(from_header or "")[1].strip().lower()


def extract(headers: dict[str, list[str]]) -> Signals:
    """Build Signals from headers keyed by lowercased name (multi-valued)."""

    def first(name: str) -> str | None:
        values = headers.get(name)
        return values[0] if values else None

    sender = sender_address(first("from"))
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
        dmarc_pass=dmarc_passed(headers.get("authentication-results", [])),
    )
