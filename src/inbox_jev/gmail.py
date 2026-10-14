"""Thin Gmail API wrapper: OAuth tokens, listing, reading, labelling and sending."""

from __future__ import annotations

import base64
import html
import logging
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage as MimeMessage
from enum import StrEnum
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from . import psl, signals
from .system_labels import INBOX

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]
# Calls per batch HTTP request: Gmail accepts up to 100 but advises at most 50, since larger
# batches are likely to trigger rate limiting.
BATCH_SIZE = 50
# Retries (exponential backoff with jitter) for a label check asked again outside its batch.
SINGLE_RETRIES = 3
# Sent emails matching `to:<address>` whose recipients are read (newest first) to find one that
# really went to the address (signals.wrote_to_address). Bounds the address check's cost.
SENT_ADDRESS_READS = 10
# Sent emails to a domain whose recipients one check reads to tell a person on it from machine
# addresses only (signals.wrote_to_person_at): the domain check's read budget per run. A scan
# that needs more goes on in later runs from where it stopped (`DomainScan`).
SENT_DOMAIN_READS = 25
# Bump when what makes a recipient a person changes (signals.wrote_to_person_at and the
# machine-address rules it uses), so stored scans are redone under the new rules (`scan_rules`).
SCAN_RULES_VERSION = 1
# Newer sent emails to a domain listed at once to bring a finished scan up to date; with more
# than that, the check doesn't conclude and the domain counts as written.
SENT_NEWER_LIMIT = 500
# How long a `never` verdict is trusted before its scan restarts from the newest match. The
# `after:` search only lists mail dated after the span, so sent mail that enters `in:sent` later
# with an older date (restored from Trash, imported, IMAP-appended) is never seen otherwise. A
# month bounds how long such a miss can let an irreversible unsubscribe through, for one full
# re-read a month of domains that are machine-only and so small (⌈N/25⌉ runs of the usual budget).
NEVER_VERDICT_TTL = timedelta(days=30)
_RECIPIENT_HEADERS = ["To", "Cc", "Bcc"]
_RECIPIENT_KEYS = tuple(h.lower() for h in _RECIPIENT_HEADERS)  # Email.headers keys


def is_token_refresh_error(exc: BaseException) -> bool:
    """Whether `exc` is a failure to refresh the Gmail OAuth token (permanent or transient)."""
    return isinstance(exc, RefreshError)


def token_error_detail(exc: BaseException) -> str | None:
    """The OAuth error summary if `exc` means the stored token is unusable, else None.

    Only a non-retryable `invalid_grant` refresh failure (token expired or revoked) counts: it
    needs `inbox-jev auth add`. google-auth also raises `RefreshError` for other token
    endpoint failures (5xx, `temporarily_unavailable`), whose first argument can be the raw
    response body (a non-JSON body comes back non-retryable); those return None. The summary
    is the first line of the first argument (e.g. "invalid_grant: Token has been expired or
    revoked."), never the raw OAuth response.
    """
    if not isinstance(exc, RefreshError) or exc.retryable:
        return None
    first = exc.args[0] if exc.args else ""
    if not isinstance(first, str) or not first.startswith("invalid_grant"):
        return None
    return first.splitlines()[0]


def is_not_found(exc: BaseException) -> bool:
    """Whether `exc` is a Gmail API error saying the resource is gone (HttpError, status 404).

    Duck-typed on `.resp.status`, as `googleapiclient.errors.HttpError` carries it, so test
    fakes don't need googleapiclient.
    """
    return getattr(getattr(exc, "resp", None), "status", None) == 404


def token_path(tokens_dir: Path, email: str) -> Path:
    return tokens_dir / f"{email.lower()}.json"


def authorize(client_secrets: Path, tokens_dir: Path, email: str) -> Path:
    """Run the browser OAuth flow for one account and store its refresh token."""
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
    creds = flow.run_local_server(port=0, login_hint=email, prompt="consent")
    profile = (
        build("gmail", "v1", credentials=creds, cache_discovery=False)
        .users()
        .getProfile(userId="me")
        .execute()
    )
    if profile["emailAddress"].lower() != email.lower():
        raise RuntimeError(f"Authorized {profile['emailAddress']}, expected {email}")
    tokens_dir.mkdir(parents=True, exist_ok=True)
    path = token_path(tokens_dir, email)
    save_token(path, creds.to_json())
    return path


def save_token(path: Path, data: str) -> None:
    """Write an OAuth token file atomically, readable by its owner only from the start.

    The text goes to a temporary file in the same directory, created with mode 0600
    (`tempfile.mkstemp`), is flushed to disk and then renamed over `path`. A crash or a full
    disk leaves the previous token whole (never a truncated file), and the token is never
    readable by others, not even briefly. The temporary file is removed if anything fails.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    """Persist a rename in `directory` (POSIX), best effort.

    The rename has already happened, so this only makes it durable sooner: where a directory
    can't be opened, or can't be fsynced (EINVAL on some filesystems), it is skipped.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        log.debug("fsync of %s skipped: %s", directory, exc)
    finally:
        os.close(fd)


def load_credentials(tokens_dir: Path, email: str) -> Credentials:
    """The stored token for `email`, refreshed (and saved, `save_token`) if it has expired."""
    path = token_path(tokens_dir, email)
    if not path.exists():
        raise FileNotFoundError(f"No token for {email}; run `inbox-jev auth add {email}`")
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        save_token(path, creds.to_json())
    return creds


def connect(tokens_dir: Path, email: str) -> Gmail:
    """A Gmail client for `email` from its stored token (`load_credentials`)."""
    return Gmail(load_credentials(tokens_dir, email))


@dataclass
class Email:
    id: str
    thread_id: str
    label_ids: list[str]
    headers: dict[str, list[str]]  # lowercased name -> values
    snippet: str
    body: str
    internal_date_ms: int

    def header(self, name: str) -> str:
        return signals.first_header(self.headers, name.lower()) or ""

    @property
    def signals(self) -> signals.Signals:
        return signals.extract(self.headers)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "head"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head") and self._skip:
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


def extract_body(payload: dict) -> str:
    """Prefer text/plain; use text/html stripped of tags when plain is missing or a stub.

    The stub check only compares a multipart/alternative's own plain and HTML versions, so
    plain text elsewhere in the tree (e.g. a note above a forwarded HTML message) is never
    dropped.
    """

    # Each segment is ("text", resolved text) or ("html", raw HTML outside any alternative).
    def walk(part: dict) -> list[tuple[str, str]]:
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


def _to_email(msg: dict, snippet: str, body: str) -> Email:
    """An `Email` from a messages.get response; its headers keyed by lowercased name."""
    headers: dict[str, list[str]] = {}
    for h in msg.get("payload", {}).get("headers", []):
        headers.setdefault(h["name"].lower(), []).append(h["value"])
    return Email(
        id=msg["id"],
        thread_id=msg["threadId"],
        label_ids=msg.get("labelIds", []),
        headers=headers,
        snippet=snippet,
        body=body,
        internal_date_ms=int(msg.get("internalDate", 0)),
    )


class DomainVerdict(StrEnum):
    """Where a domain's sent-mail scan stands (`DomainScan.verdict`)."""

    SCANNING = "scanning"  # matches left to read: the domain counts as written
    PERSON = "person"  # a sent email went to a person on it: written, for good
    NEVER = "never"  # every match read, each only to machine addresses: not written


@dataclass
class DomainScan:
    """How far the sent-mail search for one domain has read, newest first, across runs.

    The emails read span `oldest_s`..`newest_s` (their dates, Unix seconds, as Gmail's
    `before:`/`after:` take them) without a gap: a check lists the matches newer than
    `newest_s` and reads them oldest first, then the ones older than `oldest_s`, newest first.
    `newest_ids`/`oldest_ids` are the emails read at exactly those seconds, which the next
    search returns again and skips. `reached_end`: nothing older than `oldest_s` is left.
    """

    domain: str
    rules: str  # scan_rules() when the scan started
    verdict: DomainVerdict = DomainVerdict.SCANNING
    verdict_at: datetime | None = None  # when it became PERSON or NEVER (NEVER_VERDICT_TTL)
    newest_s: int | None = None
    newest_ids: list[str] = field(default_factory=list)
    oldest_s: int | None = None
    oldest_ids: list[str] = field(default_factory=list)
    reached_end: bool = False
    read: int = 0  # emails read so far, all only to machine addresses unless PERSON

    def include(self, message_id: str, sent_s: int) -> None:
        """Count email `message_id`, sent at `sent_s`, as read: widen the span to its date."""
        if self.newest_s is None or sent_s > self.newest_s:
            self.newest_s, self.newest_ids = sent_s, [message_id]
        elif sent_s == self.newest_s and message_id not in self.newest_ids:
            self.newest_ids.append(message_id)
        if self.oldest_s is None or sent_s < self.oldest_s:
            self.oldest_s, self.oldest_ids = sent_s, [message_id]
        elif sent_s == self.oldest_s and message_id not in self.oldest_ids:
            self.oldest_ids.append(message_id)


class DomainScans(Protocol):
    """Where `Gmail.has_written_to` keeps an account's domain scans between runs: the DB
    (`triage.DomainScanRows`), or `MemoryScans`."""

    def load(self, domain: str) -> DomainScan | None: ...
    def save(self, scan: DomainScan) -> None: ...


class MemoryScans:
    """Domain scans in memory: they last as long as this object (one run, by default)."""

    def __init__(self) -> None:
        self._scans: dict[str, DomainScan] = {}

    def load(self, domain: str) -> DomainScan | None:
        scan = self._scans.get(domain)
        return _copy_scan(scan) if scan is not None else None

    def save(self, scan: DomainScan) -> None:
        self._scans[scan.domain] = _copy_scan(scan)


def _copy_scan(scan: DomainScan) -> DomainScan:
    return replace(scan, newest_ids=list(scan.newest_ids), oldest_ids=list(scan.oldest_ids))


def scan_rules() -> str:
    """The rules a domain scan's verdict depends on: SCAN_RULES_VERSION and the bundled Public
    Suffix List (`psl.fingerprint`), which decides machine hosts. A scan made under others is
    redone."""
    return f"{SCAN_RULES_VERSION}-{psl.fingerprint()}"


def _never_expired(scan: DomainScan) -> bool:
    """A `never` verdict older than NEVER_VERDICT_TTL: its scan starts over."""
    return scan.verdict is DomainVerdict.NEVER and (
        scan.verdict_at is None or datetime.now(UTC) - scan.verdict_at >= NEVER_VERDICT_TTL
    )


def _progress(check: _ScanCheck) -> bool:
    """Whether a finished or interrupted check changed what is worth storing: an email read
    (not only deleted ones), a verdict reached without a read, or a restart."""
    if check.out_of_order:
        return True
    return bool(check.scan.read) and (bool(check.reads) or check.scan.verdict is not check.was)


def _recipients(sent: Email) -> list[str]:
    """The To, Cc and Bcc values of a sent email read with `_RECIPIENT_HEADERS`."""
    return [v for name in _RECIPIENT_KEYS for v in sent.headers.get(name, [])]


class _ScanCheck:
    """One check of a domain's sent mail (`Gmail._wrote_to_person_at_domain`): reads at most
    SENT_DOMAIN_READS emails, updating `scan` as it goes."""

    def __init__(self, gmail: Gmail, scan: DomainScan) -> None:
        self._gmail, self.scan = gmail, scan
        self.was = scan.verdict
        self.reads = 0  # messages.get calls made (a 404 included)
        # The date of the email read last in the current walk (or the span's bound it starts
        # from), and whether the walk is newest first: what `_in_order` checks against.
        self._last_s: int | None = None
        self._newest_first = True
        self.out_of_order = False  # a read broke date order: the scan restarts (`run`)

    def run(self) -> bool:
        """Whether the domain counts as written after this check."""
        scan = self.scan
        newer_done = True
        if scan.newest_s is None:
            self._older()  # nothing read yet: from the newest match down
        else:
            newer_done = self._newer()
            if newer_done and not scan.reached_end:
                self._older()
        if scan.verdict is DomainVerdict.PERSON:
            return True
        if self.out_of_order:
            self._restart()
            return True
        if scan.reached_end and newer_done:
            self._conclude(DomainVerdict.NEVER)
            return False
        scan.verdict = DomainVerdict.SCANNING
        log.info(
            "sent-mail domain scan not finished: counted as written",
            extra={"read": scan.read, "read_now": self.reads},
        )
        return True

    @property
    def _remaining(self) -> int:
        return SENT_DOMAIN_READS - self.reads

    def _newer(self) -> bool:
        """Read the matches sent since the span's newest, oldest first. Whether all were read
        without finding a person (False when Gmail lists more than SENT_NEWER_LIMIT)."""
        scan = self.scan
        assert scan.newest_s is not None
        # after: one second early, so emails sent in the span's newest second are listed too.
        query = f"in:sent to:{scan.domain} after:{scan.newest_s - 1}"
        ids, more = self._gmail._sent_search(query, SENT_NEWER_LIMIT)
        if more:
            return False
        self._last_s, self._newest_first = scan.newest_s, False
        seen = set(scan.newest_ids)
        for message_id in reversed(ids):
            if message_id in seen:
                continue
            if self._remaining <= 0 or self._read(message_id):
                return False
        return True

    def _older(self) -> None:
        """Read the matches older than the span (all of them when nothing was read yet), newest
        first, until the budget runs out or one went to a person."""
        scan = self.scan
        if self._remaining <= 0:
            return
        query = f"in:sent to:{scan.domain}"
        seen: set[str] = set()
        if scan.oldest_s is not None:
            # before: one second late, so emails sent in the span's oldest second are listed too.
            query, seen = f"{query} before:{scan.oldest_s + 1}", set(scan.oldest_ids)
        self._last_s, self._newest_first = scan.oldest_s, True
        ids, more = self._gmail._sent_search(query, self._remaining + len(seen))
        for message_id in ids:
            if message_id in seen:
                continue
            if self._remaining <= 0 or self._read(message_id):
                return
        if not more:
            scan.reached_end = True

    def _read(self, message_id: str) -> bool:
        """Read one match's recipients. Whether the walk stops here: it went to a person (the
        verdict is then PERSON), or it is out of date order (`out_of_order`)."""
        self.reads += 1
        sent = self._gmail._sent_recipients(message_id)
        if sent is None:
            return False
        scan = self.scan
        scan.read += 1
        if signals.wrote_to_person_at(_recipients(sent), scan.domain):
            self._conclude(DomainVerdict.PERSON)
            return True
        sent_s = sent.internal_date_ms // 1000
        if not self._in_order(sent_s):
            self.out_of_order = True
            return True
        scan.include(message_id, sent_s)
        return False

    def _in_order(self, sent_s: int) -> bool:
        """Whether an email sent at `sent_s` comes where the walk expects it: no newer than the
        one before it (and the span's oldest) walking down, no older walking up. The span is only
        gap-free if Gmail lists matches in the date order `after:`/`before:` filter on."""
        last, self._last_s = self._last_s, sent_s
        if last is None:
            return True
        return sent_s <= last if self._newest_first else sent_s >= last

    def _restart(self) -> None:
        """Drop the scan after a read out of date order: the span it built may have gaps, so it
        can't back a `never`. Counts as written now; the next check starts from the newest match."""
        log.warning(
            "sent-mail domain scan read an email out of date order: restarted",
            extra={"read": self.scan.read, "read_now": self.reads},
        )
        self.scan = DomainScan(self.scan.domain, self.scan.rules)

    def _conclude(self, verdict: DomainVerdict) -> None:
        if verdict is not self.was:
            self.scan.verdict_at = datetime.now(UTC)
        self.scan.verdict = verdict


class Gmail:
    def __init__(self, creds: Credentials):
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._svc = self._service.users()
        self._label_ids: dict[str, str] | None = None
        self._user_labels: set[str] = set()
        self._written: dict[str, bool] = {}
        self._scans = MemoryScans()

    def list_inbox_ids(self, lookback: str, limit: int) -> list[str]:
        """Ids of inbox emails newer than `lookback` (a Gmail `newer_than` value), newest first."""
        return self.list_ids(f"newer_than:{lookback}", limit, label_ids=[INBOX])

    def list_ids(self, query: str, limit: int, label_ids: list[str] | None = None) -> list[str]:
        """Ids of up to `limit` emails matching the Gmail search `query` (and all `label_ids`)."""
        ids: list[str] = []
        token = None
        while len(ids) < limit:
            resp = (
                self._svc.messages()
                .list(
                    userId="me",
                    labelIds=label_ids,
                    q=query,
                    maxResults=min(500, limit - len(ids)),
                    pageToken=token,
                )
                .execute()
            )
            ids += [m["id"] for m in resp.get("messages", [])]
            token = resp.get("nextPageToken")
            if not token:
                break
        return ids

    def get(self, message_id: str) -> Email:
        msg = self._svc.messages().get(userId="me", id=message_id, format="full").execute()
        return _to_email(
            msg,
            snippet=html.unescape(msg.get("snippet", "")),
            body=extract_body(msg.get("payload", {})),
        )

    def get_headers(self, message_id: str, names: list[str], num_retries: int = 0) -> Email:
        """Only the named headers of a message (no body): format=metadata.

        `num_retries`: googleapiclient's own retries (backoff) of a 429, 5xx or rate-limit 403.
        """
        msg = (
            self._svc.messages()
            .get(userId="me", id=message_id, format="metadata", metadataHeaders=names)
            .execute(num_retries=num_retries)
        )
        return _to_email(msg, snippet="", body="")

    def get_label_ids(self, message_ids: Sequence[str]) -> dict[str, list[str] | None]:
        """Current labels of each message, or None for one that no longer exists (HTTP 404).

        One batch HTTP request per BATCH_SIZE messages instead of one request each. A message
        whose call fails otherwise (e.g. rate limited), alone in the batch or because the whole
        batch request failed, is logged and asked again on its own, with backoff; if that fails
        too, the error is raised and no later batch is sent.
        """
        found: dict[str, list[str] | None] = {}
        unique = list(dict.fromkeys(message_ids))  # a batch rejects duplicate request ids
        for start in range(0, len(unique), BATCH_SIZE):
            chunk = unique[start : start + BATCH_SIZE]
            answers, failed = self._get_label_ids_batch(chunk)
            found.update(answers)
            if failed:
                log.warning(
                    "batched label check failed: retrying one by one",
                    extra={
                        "failed": len(failed),
                        "error_types": sorted({type(exc).__name__ for exc in failed.values()}),
                    },
                )
            for message_id in chunk:
                if message_id in failed:
                    found[message_id] = self._get_label_ids_one(message_id)
        return found

    def _get_label_ids_batch(
        self, message_ids: list[str]
    ) -> tuple[dict[str, list[str] | None], dict[str, Exception]]:
        """One batch request for `message_ids`: (answers, failures to ask again one by one).

        If the batch request itself fails (HTTP error on the batch, transport error, malformed
        reply), every message without an answer yet counts as failed with that error.
        """
        answers: dict[str, list[str] | None] = {}
        failed: dict[str, Exception] = {}

        def collect(request_id: str, response: dict | None, exc: Exception | None) -> None:
            if exc is None:
                answers[request_id] = (response or {}).get("labelIds", [])
            elif is_not_found(exc):
                answers[request_id] = None
            else:
                failed[request_id] = exc

        batch = self._service.new_batch_http_request(callback=collect)
        for message_id in message_ids:
            batch.add(self._minimal_get(message_id), request_id=message_id)
        try:
            batch.execute()
        except Exception as exc:
            for message_id in message_ids:
                if message_id not in answers:
                    failed.setdefault(message_id, exc)
        return answers, failed

    def _get_label_ids_one(self, message_id: str) -> list[str] | None:
        """Current labels of one message, or None if it no longer exists.

        googleapiclient retries a 429, a 5xx or a rate-limit 403 up to SINGLE_RETRIES times with
        exponential backoff; any other error, or one that outlasts the retries, is raised.
        """
        try:
            msg = self._minimal_get(message_id).execute(num_retries=SINGLE_RETRIES)
        except Exception as exc:
            if is_not_found(exc):
                return None
            raise
        return msg.get("labelIds", [])

    def _minimal_get(self, message_id: str) -> Any:
        """The messages.get request for a message's id and labels only (not yet executed)."""
        return self._svc.messages().get(userId="me", id=message_id, format="minimal")

    def has_written_to(self, query: str, scans: DomainScans | None = None) -> bool:
        """Whether I wrote to `query`, an address or a domain. Cached for this object's life.

        An address: a sent email matching `to:<address>` that really went to it
        (`_wrote_to_address`). A domain: a sent email matching `to:<domain>` that went to a person
        on it (`_wrote_to_person_at_domain`), its scan kept in `scans` across runs (when None,
        in this object only, so each run starts it over).
        """
        if query not in self._written:
            if "@" in query:
                self._written[query] = self._wrote_to_address(query)
            else:
                store = scans if scans is not None else self._scans
                self._written[query] = self._wrote_to_person_at_domain(query, store)
        return self._written[query]

    def _sent_search(self, query: str, limit: int) -> tuple[list[str], bool]:
        """Up to `limit` ids of emails matching the Gmail search `query` (newest first), and
        whether Gmail has more. One `messages.list`, retried (SINGLE_RETRIES)."""
        resp = (
            self._svc.messages()
            .list(userId="me", q=query, maxResults=limit)
            .execute(num_retries=SINGLE_RETRIES)
        )
        return [m["id"] for m in resp.get("messages", [])], bool(resp.get("nextPageToken"))

    def _sent_recipients(self, message_id: str) -> Email | None:
        """A sent email's To/Cc/Bcc headers and date (format=metadata, no body), or None when it
        was deleted since the search (HTTP 404). Retried like `_sent_search`; any other error is
        raised once the retries run out."""
        try:
            return self.get_headers(message_id, _RECIPIENT_HEADERS, SINGLE_RETRIES)
        except Exception as exc:
            if is_not_found(exc):
                return None
            raise

    def _wrote_to_address(self, address: str) -> bool:
        """Whether a sent email went to `address` itself (`signals.wrote_to_address`).

        Gmail's `to:` search also matches similar addresses, so the To/Cc/Bcc of the newest
        SENT_ADDRESS_READS matches are read, one at a time, stopping at the first that went to
        it: a real contact costs the search and one read. When none of those did and Gmail has
        more matches, the answer is yes (an older one might have; a missed contact is the
        dangerous side). Errors as in `_wrote_to_person_at_domain`.
        """
        ids, more = self._sent_search(f"in:sent to:{address}", SENT_ADDRESS_READS)
        for message_id in ids:
            sent = self._sent_recipients(message_id)
            if sent is not None and signals.wrote_to_address(_recipients(sent), address):
                return True
        if more:
            log.info(
                "sent-mail address check inconclusive: counted as written", extra={"read": len(ids)}
            )
            return True
        return False

    def _wrote_to_person_at_domain(self, domain: str, scans: DomainScans) -> bool:
        """Whether a sent email to `domain` had a recipient on it that isn't a machine address.

        The answer comes from a scan of every sent email matching `to:<domain>`, kept in
        `scans` (`DomainScan`, `_ScanCheck.run`): a person found once is final (no Gmail
        call after that); otherwise each check reads at most SENT_DOMAIN_READS emails, and the
        domain counts as written until every match has been read without finding one. A scan
        made under other rules (`scan_rules`) starts over. Whatever was read is saved, also
        when a Gmail error stops the check: the error is raised (previous_contact unknown) and
        the next check goes on from there. An email deleted between the search and the read
        (HTTP 404) is skipped.
        """
        rules = scan_rules()
        scan = scans.load(domain)
        if scan is None or scan.rules != rules or _never_expired(scan):
            scan = DomainScan(domain, rules)
        if scan.verdict is DomainVerdict.PERSON:
            return True
        check = _ScanCheck(self, scan)
        try:
            return check.run()
        finally:
            if _progress(check):
                scans.save(check.scan)

    def labels(self) -> dict[str, str]:
        """Label name -> id (user and system labels)."""
        if self._label_ids is None:
            resp = self._svc.labels().list(userId="me").execute()
            self._label_ids = {lbl["name"]: lbl["id"] for lbl in resp.get("labels", [])}
            self._user_labels = {
                lbl["name"] for lbl in resp.get("labels", []) if lbl.get("type") == "user"
            }
        return self._label_ids

    def user_labels(self) -> set[str]:
        """Names of user-created labels (system labels like INBOX can't be renamed)."""
        self.labels()
        return self._user_labels

    def label_message_count(self, label_id: str) -> int:
        resp = self._svc.labels().get(userId="me", id=label_id).execute()
        return int(resp.get("messagesTotal", 0))

    def delete_label(self, label_id: str) -> None:
        self._svc.labels().delete(userId="me", id=label_id).execute()
        self._label_ids = None
        log.info("deleted label", extra={"label_id": label_id})

    def ensure_labels(self, names: list[str]) -> dict[str, str]:
        existing = self.labels()
        for name in names:
            if name not in existing:
                created = (
                    self._svc.labels()
                    .create(
                        userId="me",
                        body={
                            "name": name,
                            "labelListVisibility": "labelShow",
                            "messageListVisibility": "show",
                        },
                    )
                    .execute()
                )
                existing[name] = created["id"]
                self._user_labels.add(name)
                log.info("created label", extra={"label": name})
        return existing

    def rename_label(self, label_id: str, new_name: str) -> None:
        """Rename keeps the label id, so emails that have it keep it."""
        self._svc.labels().patch(userId="me", id=label_id, body={"name": new_name}).execute()
        self._label_ids = None
        log.info("renamed label", extra={"label": new_name})

    def modify(self, message_id: str, add: list[str], remove: list[str]) -> None:
        if not add and not remove:
            return
        self._svc.messages().modify(
            userId="me", id=message_id, body={"addLabelIds": add, "removeLabelIds": remove}
        ).execute()

    def send(self, to: str, subject: str, body: str) -> None:
        mime = MimeMessage()
        mime["To"] = to
        mime["Subject"] = subject
        mime.set_content(body)
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        self._svc.messages().send(userId="me", body={"raw": raw}).execute()
