"""An email's readable text from its MIME payload (`extract_body`)."""

from __future__ import annotations

import base64
import html
import re
from html.parser import HTMLParser
from typing import Any

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
