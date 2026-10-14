"""An email's readable text (`extract_body`) and where its links go (`extract_link_domains`),
from its MIME payload."""

from __future__ import annotations

import base64
import html
import ipaddress
import re
import unicodedata
from collections import Counter
from collections.abc import Iterator
from email.message import Message
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urlsplit

from . import psl
from .signals import DOMAIN

# Elements whose text isn't part of the readable body.
_SKIPPED_TAGS = frozenset({"script", "style", "head"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    # `attrs` is unused but part of HTMLParser's signature.
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:  # noqa: ARG002
        if tag in _SKIPPED_TAGS:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    parser = _TextExtractor()
    parser.feed(raw)
    return html.unescape(" ".join(parser.parts))


def _decode(data: str | None) -> str:
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


# Within one multipart/alternative, a text/plain part shorter than STUB_MAX_CHARS is treated as a
# stub ("your mail client doesn't support HTML, open this URL"), which bulk senders like emBlue or
# Constant Contact send instead of the content, when its text/html sibling is at least STUB_RATIO
# times longer (and at least STUB_RATIO * STUB_MIN_CHARS long, so a URL-only plain part isn't
# swapped for a one-word button). URLs don't count toward either length: a stub's "view online"
# link can be hundreds of characters long.
STUB_MAX_CHARS = 300
STUB_MIN_CHARS = 20
STUB_RATIO = 3
URL_RE = re.compile(r"https?://\S+")


def _normalize(text: str) -> str:
    # No-break spaces and zero-width non-joiners pad bulk-mail previews. The lookbehind keeps
    # the trailing-whitespace strip linear on long runs of other whitespace (e.g. U+2007).
    return re.sub(r"(?<!\s)\s+\n", "\n", re.sub("[ \t\u00a0\u200c]+", " ", text)).strip()


def _content_len(text: str) -> int:
    return len(URL_RE.sub("", text))


def _resolve_alternative(plain: list[str], rich: list[str]) -> str:
    """Text of one multipart/alternative: its plain part, or its HTML if plain is a stub."""
    text = _normalize("\n".join(plain))
    text_len = _content_len(text)
    if text_len < STUB_MAX_CHARS and rich:
        html_text = _normalize(html_to_text("\n".join(rich)))
        if not text or _content_len(html_text) >= STUB_RATIO * max(text_len, STUB_MIN_CHARS):
            return html_text
    return text


def extract_body(payload: dict[str, Any]) -> str:
    """Prefer text/plain; use text/html stripped of tags when plain is missing or a stub.

    The stub check only compares a multipart/alternative's own plain and HTML versions, so
    plain text elsewhere in the tree (e.g. a note above a forwarded HTML message) is never
    dropped.
    """

    # Each segment is ("text", resolved text) or ("html", raw HTML outside any alternative).
    def walk(part: dict[str, Any]) -> list[tuple[str, str]]:
        if part.get("filename"):
            return []
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            return [("text", _decode(part.get("body", {}).get("data")))]
        if mime == "text/html":
            return [("html", _decode(part.get("body", {}).get("data")))]
        segments = [s for child in part.get("parts", []) or [] for s in walk(child)]
        if mime == "multipart/alternative":
            plain = [v for k, v in segments if k == "text"]
            rich = [v for k, v in segments if k == "html"]
            if plain:
                return [("text", _resolve_alternative(plain, rich))]
        return segments

    segments = walk(payload)
    text = _normalize("\n".join(v for k, v in segments if k == "text"))
    rich = [v for k, v in segments if k == "html"]
    if not text and rich:
        return _normalize(html_to_text("\n".join(rich)))
    return text


# How many link domains Jev gets (`extract_link_domains`): enough for a lure's few targets next
# to a signature's links, few enough to keep the state small.
LINK_DOMAINS_MAX = 8
# A URL in plain text: up to whitespace, quotes or brackets; trailing punctuation is dropped.
_TEXT_URL = re.compile(r"https?://[^\s<>\"'()\[\]{}]+", re.IGNORECASE)
_LINK_TAGS = frozenset({"a", "area"})


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _LINK_TAGS:
            self.hrefs += [v.strip() for k, v in attrs if k == "href" and v]


def _text_parts(part: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """(mime type, decoded text) of every text/plain and text/html part that isn't an
    attachment, in tree order."""
    if part.get("filename"):
        return
    mime = part.get("mimeType", "")
    if mime in ("text/plain", "text/html"):
        yield mime, _decode(part.get("body", {}).get("data"))
    for child in part.get("parts", []) or []:
        yield from _text_parts(child)


# RFC 1035 2.3.4 (with RFC 1123 2.1): a host name is at most 253 characters, each label at most
# 63. A longer name resolves nowhere, and without the bound a link could make `link_domains`
# (and Jev's state) as long as the email itself.
HOSTNAME_MAX = 253
LABEL_MAX = 63
# What a browser does with an http(s) URL before reading its host (WHATWG URL Standard): leading
# and trailing C0 controls and spaces are stripped, tabs and newlines removed, and for these
# special schemes any run of `/` and `\` after the scheme ends at the host (`https:\\h`,
# `https:/h`, `https:h`). Without a scheme, two slashes start a host (`//h`): the webmail page's
# scheme, https.
_URL_TRIM = "".join(map(chr, range(0x21)))
_URL_TAB_NEWLINE = str.maketrans("", "", "\t\n\r")
_SPECIAL_URL = re.compile(r"(https?):[\\/]*(.*)", re.IGNORECASE | re.DOTALL)
_SCHEME_RELATIVE = re.compile(r"[\\/]{2}(.*)", re.DOTALL)
# The label separators IDNA maps to "." (UTS #46, as browsers apply it): ideographic, full-width
# and half-width ideographic full stops. NFKC turns the full-width one into "." and the half-width
# one into the ideographic one, which it keeps.
_IDNA_DOTS = str.maketrans(dict.fromkeys("\u3002\uff0e\uff61", "."))
# The code points UTS #46 maps to nothing ("ignored"), as browsers drop them from a host: soft
# hyphen, combining grapheme joiner, Mongolian free variation selectors, zero-width space, word
# joiner, variation selectors and the zero-width no-break space (byte order mark).
_IDNA_IGNORED = str.maketrans(
    dict.fromkeys(
        "\u00ad\u034f\u180b\u180c\u180d\u200b\u2060\ufeff"
        + "".join(map(chr, range(0xFE00, 0xFE10)))
    )
)
# A host whose last label is a number (`0x7f.1`, `2130706433`, `10.1`): browsers read it as IPv4.
_NUMERIC_LABEL = re.compile(r"(?:0x[0-9a-f]*|[0-9]+)", re.IGNORECASE)


def _normalise_url(url: str) -> str | None:
    """`url` as `scheme://rest` the way a browser reads an http(s) link (`_SPECIAL_URL`), with
    `\\` as `/`; None for any other scheme or a relative link."""
    url = url.strip(_URL_TRIM).translate(_URL_TAB_NEWLINE)
    if match := _SPECIAL_URL.fullmatch(url):
        scheme, rest = match.group(1).lower(), match.group(2)
    elif match := _SCHEME_RELATIVE.fullmatch(url):
        scheme, rest = "https", match.group(1)
    else:
        return None
    return f"{scheme}://{rest.replace(chr(92), '/')}"


def _ipv4_number(host: str) -> str | None:
    """The IPv4 address a browser reads in a host whose last label is a number (WHATWG IPv4
    parser: 1 to 4 parts, each decimal, `0x` hex or `0`-leading octal, the last filling the
    remaining bytes): `0x7f.1` → `127.0.0.1`. None when that parse fails (the URL is invalid)."""
    parts = host.split(".")  # no trailing dot: `psl.to_ascii` strips it
    if len(parts) > 4:
        return None
    numbers: list[int] = []
    for part in parts:
        if not _NUMERIC_LABEL.fullmatch(part):
            return None
        if part[:2].lower() == "0x":
            numbers.append(int(part[2:] or "0", 16))
        elif len(part) > 1 and part.startswith("0"):
            if not set(part) <= set("01234567"):
                return None
            numbers.append(int(part, 8))
        else:
            numbers.append(int(part))
    *head, last = numbers
    if any(n > 255 for n in head) or last >= 256 ** (5 - len(numbers)):
        return None
    value = last + sum(n << (8 * (3 - i)) for i, n in enumerate(head))
    return str(ipaddress.IPv4Address(value))


def link_domain(url: str) -> str | None:
    """The registrable domain (`psl.registrable_domain`) an http(s) `url` points to, or its IP
    address when the host is one; None for any other scheme (`mailto:`, `tel:`, `#anchor`), a
    malformed URL, a host longer than DNS allows (`HOSTNAME_MAX`, `LABEL_MAX`) or one nobody owns
    (a public suffix, a single label).

    The URL is read as a browser reads it (`_normalise_url`): backslashes, one or no slash after
    the scheme and scheme-relative `//host` links all reach a host, and so does a numeric one
    (`_ipv4_number`). UTS #46's ignored code points (`_IDNA_IGNORED`) are dropped from the host,
    which is then NFKC-normalised, and IDNA's other full stops (`_IDNA_DOTS`) count as dots, as
    browsers map them. On a private suffix of the list (`github.io`, `web.app`)
    the registrable domain is the site's own label, which can name a person or carry a
    per-recipient token.
    """
    normalised = _normalise_url(url)
    if normalised is None:
        return None
    try:
        host = urlsplit(normalised).hostname
    except ValueError:  # e.g. an unbalanced "[" in the host
        return None
    if not host:
        return None
    host = unquote(host)
    if len(host) > HOSTNAME_MAX:  # cheap check before any work on a huge name
        return None
    # As a browser maps it (UTS #46): a full-width `e` is `e`, an ideographic full stop a dot.
    # NFKC can lengthen a name, so the limits are checked again on the punycode below.
    host = unicodedata.normalize("NFKC", host.translate(_IDNA_IGNORED)).translate(_IDNA_DOTS)
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    ascii_host = psl.to_ascii(host)
    labels = ascii_host.split(".")
    if len(ascii_host) > HOSTNAME_MAX or any(len(label) > LABEL_MAX for label in labels):
        return None
    if _NUMERIC_LABEL.fullmatch(labels[-1]):
        return _ipv4_number(ascii_host)
    domain = psl.registrable_domain(ascii_host)
    return domain if domain and DOMAIN.match(domain) else None


def extract_link_domains(payload: dict[str, Any], limit: int = LINK_DOMAINS_MAX) -> list[str]:
    """The distinct domains (`link_domain`) the email's links point to, most frequent first
    (ties in order of appearance), at most `limit`.

    Links are the `href` of `<a>` and `<area>` tags in HTML parts and the http(s) URLs in plain
    text parts; attachments are skipped. Only domains, never a URL: a path or query string can
    carry the recipient's address or a login token.
    """
    urls: list[str] = []
    for mime, text in _text_parts(payload):
        if mime == "text/html":
            parser = _LinkExtractor()
            parser.feed(text)
            urls += parser.hrefs
        else:
            urls += [u.rstrip(".,;:!?") for u in _TEXT_URL.findall(text)]
    counts = Counter(d for d in map(link_domain, urls) if d)
    return [domain for domain, _ in counts.most_common(limit)]


# An iTIP method as RFC 5545 allows it (an iana-token, or an x-name, which is one too), capped:
# it comes from the sender, and `dry_classify` prints it.
_ITIP_METHOD = re.compile(r"[A-Za-z0-9-]{1,32}")


def calendar_method(payload: dict[str, Any]) -> str | None:
    """The iTIP method (RFC 5546) of the email's calendar part, uppercased, or None when it has
    none: an invitation or update (`REQUEST`), a response to one (`REPLY`), a cancellation
    (`CANCEL`), a proposed new time (`COUNTER`)...

    iMIP (RFC 6047) carries an iTIP message as a `text/calendar` part whose Content-Type names
    the method (`text/calendar; charset="UTF-8"; method=REPLY`), as Google Calendar and Outlook
    send them. Read from the part's own headers in the `full` payload, never from the Subject;
    the first `text/calendar` part with a method wins, wherever it sits in the tree. The value is
    sender-controlled, so only an RFC 5545 iana-token (letters, digits, `-`, at most 32) counts
    as a method: anything else (control or escape characters, a 100 KB string) is no method. An
    RFC 2231-encoded parameter (`method*=us-ascii''REPLY`) counts by its value, whatever charset
    it names. Never raises.
    """
    if payload.get("mimeType", "").lower() == "text/calendar":
        content_type = Message()
        for h in payload.get("headers", []) or []:
            if h.get("name", "").lower() == "content-type":
                content_type["Content-Type"] = h.get("value", "")
                break
        method = content_type.get_param("method")
        if method is not None:
            # RFC 2231 (`method*=us-ascii''REPLY`) gives (charset, language, value), the value
            # already percent-decoded. A token is ASCII, so the sender's charset is ignored:
            # decoding with it can raise (`idna`, `undefined`) or turn junk into a token.
            token = (method[2] if isinstance(method, tuple) else method).strip()
            if _ITIP_METHOD.fullmatch(token):
                return token.upper()
    for child in payload.get("parts", []) or []:
        if found := calendar_method(child):
            return found
    return None
