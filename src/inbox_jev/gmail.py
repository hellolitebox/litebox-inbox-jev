"""Thin Gmail API wrapper: OAuth tokens, listing, reading, labelling and sending."""

from __future__ import annotations

import base64
import html
import logging
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from . import signals
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
# Sent emails to a domain whose recipients are read (newest first) to tell a person on it from
# machine addresses only (signals.wrote_to_person_at). Bounds the domain check's cost.
SENT_DOMAIN_SAMPLE = 25
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
    """Persist a rename in `directory` (POSIX); a no-op where directories can't be opened."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
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


class Gmail:
    def __init__(self, creds: Credentials):
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._svc = self._service.users()
        self._label_ids: dict[str, str] | None = None
        self._user_labels: set[str] = set()
        self._written: dict[str, bool] = {}

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

    def has_written_to(self, query: str) -> bool:
        """Whether I wrote to `query`, an address or a domain. Cached for this object's life.

        An address: any sent email matching `to:<address>` (one search). A domain: a sent email
        matching `to:<domain>` that went to a person on it (`_wrote_to_person_at_domain`).
        """
        if query not in self._written:
            if "@" in query:
                ids, _ = self._sent_ids(query, 1)
                self._written[query] = bool(ids)
            else:
                self._written[query] = self._wrote_to_person_at_domain(query)
        return self._written[query]

    def _sent_ids(self, query: str, limit: int) -> tuple[list[str], bool]:
        """Up to `limit` ids of sent emails matching `to:<query>` (newest first), and whether
        Gmail has more."""
        resp = (
            self._svc.messages()
            .list(userId="me", q=f"in:sent to:{query}", maxResults=limit)
            .execute(num_retries=SINGLE_RETRIES)
        )
        return [m["id"] for m in resp.get("messages", [])], bool(resp.get("nextPageToken"))

    def _wrote_to_person_at_domain(self, domain: str) -> bool:
        """Whether a sent email to `domain` had a recipient on it that isn't a machine address.

        Reads the To/Cc/Bcc headers (format=metadata, no body) of the newest
        SENT_DOMAIN_SAMPLE matches, one at a time, and stops at the first that counts
        (signals.wrote_to_person_at), so a real contact's domain costs one list call and
        usually one read. A domain written to only at machine addresses (replies to
        notifications) costs the list call and one read per match, at most SENT_DOMAIN_SAMPLE.
        When that many all went to machines and Gmail has more, the answer is yes: an older
        email might be to a person, and a missed contact is the dangerous side. An email deleted
        between the search and the read (HTTP 404) is skipped; any other error is raised once
        googleapiclient's retries (SINGLE_RETRIES, on 429, 5xx and rate-limit 403) run out, for
        the list call and each read: a transient error would make previous_contact unknown.
        """
        ids, more = self._sent_ids(domain, SENT_DOMAIN_SAMPLE)
        for message_id in ids:
            try:
                sent = self.get_headers(message_id, _RECIPIENT_HEADERS, SINGLE_RETRIES)
            except Exception as exc:
                if is_not_found(exc):
                    continue
                raise
            recipients = [v for name in _RECIPIENT_KEYS for v in sent.headers.get(name, [])]
            if signals.wrote_to_person_at(recipients, domain):
                return True
        if ids and more:
            log.info("sent-mail sample inconclusive: counted as written", extra={"read": len(ids)})
            return True
        return False

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
