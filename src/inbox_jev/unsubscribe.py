"""Unsubscribe via List-Unsubscribe headers: RFC 8058 one-click POST, then mailto, else manual."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from .signals import Signals

USER_AGENT = "inbox-jev/0.1 (+List-Unsubscribe one-click)"
TIMEOUT = httpx.Timeout(15.0)

SendMail = Callable[[str, str, str], None]  # (to, subject, body)


@dataclass
class Result:
    method: str  # one_click | mailto | manual
    status: str  # ok | failed | manual_pending | skipped
    target: str | None = None
    http_status: int | None = None
    error: str | None = None


def is_public_https(url: str) -> bool:
    """Reject non-https and hosts resolving to private/loopback/link-local addresses (SSRF)."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    return all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


def parse_mailto(uri: str) -> tuple[str, str, str]:
    """mailto:addr?subject=..&body=.. -> (to, subject, body)."""
    parts = urlsplit(uri)
    to = unquote(parts.path)
    query = {k.lower(): v[0] for k, v in parse_qs(parts.query).items()}
    return to, query.get("subject", "unsubscribe"), query.get("body", "unsubscribe")


def one_click(url: str, client: httpx.Client) -> Result:
    try:
        resp = client.post(
            url,
            data={"List-Unsubscribe": "One-Click"},
            headers={"User-Agent": USER_AGENT},
        )
    except httpx.HTTPError as exc:
        return Result("one_click", "failed", url, error=f"{type(exc).__name__}: {exc}")
    # RFC 8058 servers answer 2xx; some ESPs reply with a redirect to a confirmation page.
    ok = 200 <= resp.status_code < 400
    return Result(
        "one_click",
        "ok" if ok else "failed",
        url,
        http_status=resp.status_code,
        error=None if ok else resp.text[:500],
    )


def unsubscribe(
    sig: Signals,
    send_mail: SendMail,
    client: httpx.Client | None = None,
    url_guard: Callable[[str], bool] = is_public_https,
) -> Result:
    if not sig.dmarc_pass:
        return Result("manual", "skipped", error="sender not authenticated (dmarc != pass)")

    if sig.one_click:
        for url in sig.unsubscribe_https:
            if not url_guard(url):
                continue
            own_client = client is None
            # No redirects: a redirect could point at an internal host and bypass url_guard.
            client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
            try:
                result = one_click(url, client)
            finally:
                if own_client:
                    client.close()
            if result.status == "ok" or not sig.unsubscribe_mailto:
                return result
            break  # one-click failed: fall through to mailto

    for uri in sig.unsubscribe_mailto:
        to, subject, body = parse_mailto(uri)
        if "@" not in to:
            continue
        try:
            send_mail(to, subject, body)
        except Exception as exc:
            return Result("mailto", "failed", uri, error=f"{type(exc).__name__}: {exc}")
        return Result("mailto", "ok", uri)

    # Only a web page (no one-click) or a link in the body: needs a human (or a browser agent).
    target = sig.unsubscribe_https[0] if sig.unsubscribe_https else None
    return Result("manual", "manual_pending", target)
