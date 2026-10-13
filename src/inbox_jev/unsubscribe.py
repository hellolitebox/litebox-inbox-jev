"""Unsubscribe via List-Unsubscribe headers: RFC 8058 one-click POST, then mailto, else manual."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from .signals import Signals

USER_AGENT = "inbox-jev/0.1 (+List-Unsubscribe one-click)"
TIMEOUT = httpx.Timeout(15.0)

SendMail = Callable[[str, str, str], None]  # (to, subject, body)
# Validates a one-click URL and returns the IP addresses to connect to, in order (empty: skip it).
UrlGuard = Callable[[str], list[str]]


@dataclass
class Result:
    method: str  # one_click | mailto | manual
    status: str  # ok | failed | manual_pending | skipped
    target: str | None = None
    http_status: int | None = None
    error: str | None = None


def resolve_public_https(url: str) -> list[str]:
    """The addresses to POST to, in DNS order; empty for non-https URLs and non-public hosts.

    SSRF guard: every address the host resolves to must be global (no private, loopback or
    link-local IP); a DNS failure or a malformed URL rejects it. The caller connects to the
    returned addresses itself, so a second DNS answer (DNS rebinding) can't point the request
    somewhere else.
    """
    try:
        parts = urlsplit(url)
        port = parts.port or 443
    except ValueError:
        return []
    if parts.scheme != "https" or not parts.hostname:
        return []
    try:
        infos = socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return []
    # Drop an IPv6 zone ("fe80::1%eth0"): ip_address rejects it, and such addresses aren't global.
    addresses = [ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos]
    if not all(a.is_global for a in addresses):
        return []
    return list(dict.fromkeys(str(a) for a in addresses))  # one entry per address, DNS order


def parse_mailto(uri: str) -> tuple[str, str, str]:
    """mailto:addr?subject=..&body=.. -> (to, subject, body)."""
    parts = urlsplit(uri)
    to = unquote(parts.path)
    query = {k.lower(): v[0] for k, v in parse_qs(parts.query).items()}
    return to, query.get("subject", "unsubscribe"), query.get("body", "unsubscribe")


def one_click(url: str, addresses: Sequence[str], client: httpx.Client) -> Result:
    """RFC 8058 POST to `url`, connecting to the IPs the guard validated, in order.

    The request goes to the IP with the original Host header, and TLS uses the hostname for SNI
    and certificate verification (httpx's `sni_hostname` extension), so the certificate must
    still be valid for the hostname. When an address can't be connected to (refused, timeout,
    TLS failure) the next one is tried, as `socket.create_connection` does; once a request has
    been sent, its answer (or error) is the result.
    """
    result = Result("one_click", "failed", url, error="no address to connect to")
    for address in addresses:
        try:
            target = httpx.URL(url)
            resp = client.post(
                target.copy_with(host=address),
                data={"List-Unsubscribe": "One-Click"},
                headers={"User-Agent": USER_AGENT, "Host": target.netloc.decode("ascii")},
                extensions={"sni_hostname": target.raw_host.decode("ascii")},
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:  # not sent: try the next IP
            result = Result("one_click", "failed", url, error=f"{type(exc).__name__}: {exc}")
            continue
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
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
    return result


def _client() -> httpx.Client:
    """The production one-click client.

    No redirects: a redirect could point at an internal host and bypass the URL guard. No
    proxy from the environment (`trust_env=False`): httpcore's proxy tunnel ignores
    `sni_hostname`, so a pinned request would fail TLS, and the proxy would pick the destination.
    """
    return httpx.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False)


def unsubscribe(
    sig: Signals,
    send_mail: SendMail,
    client: httpx.Client | None = None,
    url_guard: UrlGuard = resolve_public_https,
) -> Result:
    if not sig.dmarc_pass:
        return Result("manual", "skipped", error="sender not authenticated (dmarc != pass)")

    if sig.one_click:
        for url in sig.unsubscribe_https:
            addresses = url_guard(url)
            if not addresses:
                continue
            own_client = client is None
            client = client or _client()
            try:
                result = one_click(url, addresses, client)
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
