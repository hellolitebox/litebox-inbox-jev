"""Unsubscribe via List-Unsubscribe headers: RFC 8058 one-click POST, then mailto, else manual."""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from . import __version__
from .signals import Signals

log = logging.getLogger(__name__)

USER_AGENT = f"inbox-jev/{__version__} (+List-Unsubscribe one-click)"
TIMEOUT = httpx.Timeout(15.0)

SendMail = Callable[[str, str, str], None]  # (to, subject, body)
# Validates a one-click URL and returns the IP addresses to connect to, in order (empty: skip it).
UrlGuard = Callable[[str], list[str]]


class UnsubscribeMethod(StrEnum):
    """How an unsubscribe was attempted (stored in `unsubscribes.method`)."""

    ONE_CLICK = "one_click"
    MAILTO = "mailto"
    MANUAL = "manual"


class UnsubscribeStatus(StrEnum):
    """An attempt's outcome (stored in `unsubscribes.status`)."""

    OK = "ok"
    FAILED = "failed"
    MANUAL_PENDING = "manual_pending"  # only a web page or a link in the body: needs a human
    SKIPPED = "skipped"  # not attempted: unverified sender, or ruled out by unsubscribe-retry


@dataclass
class Result:
    method: UnsubscribeMethod
    status: UnsubscribeStatus
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


def _one_click_failed(url: str, error: str) -> Result:
    return Result(UnsubscribeMethod.ONE_CLICK, UnsubscribeStatus.FAILED, url, error=error)


def _request_failed(url: str, exc: Exception) -> Result:
    return _one_click_failed(url, f"{type(exc).__name__}: {exc}")


class _Encoding(StrEnum):
    """How the one-click body is encoded (RFC 8058 section 3.2)."""

    MULTIPART = "multipart/form-data"  # SHOULD
    FORM = "application/x-www-form-urlencoded"  # MAY


# The single key/value pair RFC 8058 sends as the POST body.
_FIELD, _VALUE = "List-Unsubscribe", "One-Click"
_MESSAGE_CHARS = 120
_MESSAGE_BODY_BYTES = 4096  # larger error bodies are not parsed for a message
_MESSAGE_KEYS = ("message", "error", "detail")


def _post(client: httpx.Client, url: str, address: str, encoding: _Encoding) -> httpx.Response:
    """POST the one-click body to `url`, connected to `address`, with the hostname as `Host` and
    for TLS (SNI and certificate check)."""
    target = httpx.URL(url)
    multipart = encoding == _Encoding.MULTIPART
    return client.post(
        target.copy_with(host=address),
        # A `(None, value)` file is a plain form field: no filename, no part Content-Type.
        files={_FIELD: (None, _VALUE)} if multipart else None,
        data=None if multipart else {_FIELD: _VALUE},
        headers={"User-Agent": USER_AGENT, "Host": target.netloc.decode("ascii")},
        extensions={"sni_hostname": target.raw_host.decode("ascii")},
    )


def describe_http_status(status: int) -> str:
    """`HTTP 415 Unsupported Media Type`: the status and its standard reason phrase (not the
    server's own, which is sender-controlled text); `HTTP 499` for a status without one."""
    return f"HTTP {status} {httpx.codes.get_reason_phrase(status)}".rstrip()


def _server_message(resp: httpx.Response) -> str:
    """A short, single-line, printable-ASCII message from a JSON error body (`message`, `error`
    or `detail`), or "" for any other body: a body is sender-controlled text, often a whole HTML
    page, so only a field meant as a message is kept, and only its plain words, without links.

    Total: it runs after the POST was sent, so it never raises. Only a small body is parsed (a
    huge or deeply nested one costs CPU or raises RecursionError), and a parse failure is "".
    """
    if len(resp.content) > _MESSAGE_BODY_BYTES:
        return ""
    try:
        body = resp.json()
    # ValueError: not JSON, or not UTF-8 (UnicodeDecodeError); RecursionError: deeply nested.
    except (ValueError, RecursionError, TypeError):
        return ""
    if not isinstance(body, dict):
        return ""
    for key in _MESSAGE_KEYS:
        if isinstance(value := body.get(key), str):
            text = "".join(c if c.isascii() and c.isprintable() else " " for c in value)
            words = text.replace("{", " ").replace("}", " ").split()
            # A link in the owner's digest would be the sender's, so no token with a scheme.
            text = " ".join(w for w in words if "://" not in w)
            if text:
                return text[:_MESSAGE_CHARS]
    return ""


def describe_http_failure(resp: httpx.Response) -> str:
    """The `unsubscribes.error` of a refused one-click: `HTTP 415 Unsupported Media Type`, plus
    the server's own short message when its JSON body has one that says more than the status."""
    status = describe_http_status(resp.status_code)
    message = _server_message(resp)
    reason = httpx.codes.get_reason_phrase(resp.status_code)
    if not message or message.casefold() == reason.casefold():
        return status
    return f"{status}: {message}"


def one_click(url: str, addresses: Sequence[str], client: httpx.Client) -> Result:
    """RFC 8058 POST to `url`, connecting to the IPs the guard validated, in order.

    The body is `List-Unsubscribe=One-Click` as multipart/form-data, the encoding RFC 8058 says
    SHOULD be used. A server that answers 415 (Unsupported Media Type) refused the request
    without acting on it, so the same address gets it **once** more as
    application/x-www-form-urlencoded (which RFC 8058 allows), and that answer (or error) is the
    result. No other answer is retried: any other status may come from a server that acted on it.

    The request goes to the IP with the original Host header, and TLS uses the hostname for SNI
    and certificate verification (httpx's `sni_hostname` extension), so the certificate must
    still be valid for the hostname. When an address can't be connected to (refused, timeout,
    TLS failure) the next one is tried, as `socket.create_connection` does; once a request has
    been sent, its answer (or error) is the result.
    """
    result = _one_click_failed(url, "no address to connect to")
    for address in addresses:
        try:
            resp = _post(client, url, address, _Encoding.MULTIPART)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:  # not sent: try the next IP
            result = _request_failed(url, exc)
            continue
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            return _request_failed(url, exc)
        if resp.status_code == httpx.codes.UNSUPPORTED_MEDIA_TYPE:
            try:
                resp = _post(client, url, address, _Encoding.FORM)
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                return _request_failed(url, exc)
            log.info(
                "one-click multipart refused (415), retried urlencoded",
                extra={"host": httpx.URL(url).host, "http_status": resp.status_code},
            )
        return _answered(url, resp)
    return result


def _answered(url: str, resp: httpx.Response) -> Result:
    """The result of a one-click POST the server answered. It never raises: the request was
    sent, so the attempt must be recorded, or the next run would POST again."""
    # RFC 8058 servers answer 2xx; some ESPs reply with a redirect to a confirmation page.
    ok = 200 <= resp.status_code < 400
    error = None
    if not ok:
        try:
            error = describe_http_failure(resp)
        except Exception:  # a bug in describing it must not lose the attempt
            log.warning(
                "describing a refused one-click failed",
                extra={"host": httpx.URL(url).host, "http_status": resp.status_code},
                exc_info=True,
            )
            error = describe_http_status(resp.status_code)
    return Result(
        UnsubscribeMethod.ONE_CLICK,
        UnsubscribeStatus.OK if ok else UnsubscribeStatus.FAILED,
        url,
        http_status=resp.status_code,
        error=error,
    )


def _client() -> httpx.Client:
    """The production one-click client.

    No redirects: a redirect could point at an internal host and bypass the URL guard. No
    proxy from the environment (`trust_env=False`): httpcore's proxy tunnel ignores
    `sni_hostname`, so a pinned request would fail TLS, and the proxy would pick the destination.
    """
    return httpx.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False)


def _first_one_click(
    sig: Signals, client: httpx.Client | None, url_guard: UrlGuard
) -> Result | None:
    """The one-click POST to the first https URL the guard accepts, through `client` or a
    client of its own (closed after it); None when the guard accepts none."""
    for url in sig.unsubscribe_https:
        addresses = url_guard(url)
        if not addresses:
            continue
        if client is not None:
            return one_click(url, addresses, client)
        with _client() as own:
            return one_click(url, addresses, own)
    return None


def unsubscribe(
    sig: Signals,
    send_mail: SendMail,
    client: httpx.Client | None = None,
    url_guard: UrlGuard = resolve_public_https,
) -> Result:
    if not sig.dmarc_pass:
        return Result(
            UnsubscribeMethod.MANUAL,
            UnsubscribeStatus.SKIPPED,
            error="sender not authenticated (dmarc != pass)",
        )

    if sig.one_click:
        result = _first_one_click(sig, client, url_guard)
        # One-click failed: fall through to mailto, when there is one.
        if result is not None and (
            result.status == UnsubscribeStatus.OK or not sig.unsubscribe_mailto
        ):
            return result

    for uri in sig.unsubscribe_mailto:
        to, subject, body = parse_mailto(uri)
        if "@" not in to:
            continue
        try:
            send_mail(to, subject, body)
        except Exception as exc:
            # Any failure (Gmail HTTP, auth, transport, or a bug) is a failed attempt that
            # `unsubscribe-retry` can retry; the traceback is logged so a bug doesn't hide as
            # an ordinary failure.
            log.warning("unsubscribe mail failed", extra={"target": to}, exc_info=True)
            return Result(
                UnsubscribeMethod.MAILTO,
                UnsubscribeStatus.FAILED,
                uri,
                error=f"{type(exc).__name__}: {exc}",
            )
        return Result(UnsubscribeMethod.MAILTO, UnsubscribeStatus.OK, uri)

    # Only a web page (no one-click) or a link in the body: needs a human (or a browser agent).
    target = sig.unsubscribe_https[0] if sig.unsubscribe_https else None
    return Result(UnsubscribeMethod.MANUAL, UnsubscribeStatus.MANUAL_PENDING, target)
