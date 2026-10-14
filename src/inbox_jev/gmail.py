"""Thin Gmail API wrapper: OAuth tokens, listing, reading, labelling and sending."""

from __future__ import annotations

import base64
import html
import logging
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from pathlib import Path
from types import TracebackType
from typing import Any, Self, TypeVar

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from . import signals
from .body import calendar_method, extract_body, extract_link_domains
from .sent_scan import (
    RECIPIENT_HEADERS,
    DomainScans,
    MemoryScans,
    wrote_to_address,
    wrote_to_person_at_domain,
)
from .system_labels import INBOX

log = logging.getLogger(__name__)

_T = TypeVar("_T")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]
# Calls per batch HTTP request: Gmail accepts up to 100 but advises at most 50, since larger
# batches are likely to trigger rate limiting.
BATCH_SIZE = 50
# Retries (exponential backoff with jitter) for a call asked again outside its batch (`_get_one`).
SINGLE_RETRIES = 3


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
        Path(tmp).replace(path)
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
    creds: Credentials = Credentials.from_authorized_user_file(str(path), SCOPES)
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
    # Domains the body's links point to (`body.extract_link_domains`); empty for a header-only
    # read (`Gmail.get_headers`).
    link_domains: list[str] = field(default_factory=list)
    # The iTIP method of its calendar part (`body.calendar_method`: "REPLY" for a response to an
    # invitation, "REQUEST" for one...), None for any other email and for a header-only read.
    calendar_method: str | None = None

    def header(self, name: str) -> str:
        return signals.first_header(self.headers, name.lower()) or ""

    def extract_signals(self) -> signals.Signals:
        """The sender signals parsed from the headers, unresolved (no group relay): parsed
        again on every call, so keep the result. Decisions use `triage.signals_for` instead."""
        return signals.extract(self.headers)


def _to_email(
    msg: dict[str, Any],
    snippet: str,
    body: str,
    link_domains: list[str] | None = None,
    calendar: str | None = None,
) -> Email:
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
        link_domains=link_domains or [],
        calendar_method=calendar,
    )


@dataclass(frozen=True)
class ThreadMessage:
    """One message of a thread as `Gmail.get_threads` reads it: no headers or body."""

    id: str
    label_ids: tuple[str, ...]
    internal_date_ms: int


def _label_ids(msg: dict[str, Any]) -> list[str]:
    label_ids: list[str] = msg.get("labelIds", [])
    return label_ids


def _thread_messages(thread: dict[str, Any]) -> list[ThreadMessage]:
    return [
        ThreadMessage(m["id"], tuple(m.get("labelIds", ())), int(m.get("internalDate", 0)))
        for m in thread.get("messages", [])
    ]


class Gmail:
    """One account's Gmail API client. Close it (`close`, or use it as a context manager) when
    the account's work ends: it holds the HTTP connections of the googleapiclient service."""

    def __init__(self, creds: Credentials) -> None:
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._svc = self._service.users()
        self._label_ids: dict[str, str] | None = None
        self._user_labels: set[str] = set()
        self._written: dict[str, bool] = {}
        self._scans = MemoryScans()

    def close(self) -> None:
        """Close the service's HTTP connections. Safe to call more than once."""
        self._service.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

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
        payload = msg.get("payload", {})
        return _to_email(
            msg,
            snippet=html.unescape(msg.get("snippet", "")),
            body=extract_body(payload),
            link_domains=extract_link_domains(payload),
            calendar=calendar_method(payload),
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

        Batched (`_batched_get`): one HTTP request per BATCH_SIZE messages.
        """
        return self._batched_get(message_ids, self._minimal_get, _label_ids, "label check")

    def get_threads(self, thread_ids: Sequence[str]) -> dict[str, list[ThreadMessage] | None]:
        """The messages (id, labels, date) of each thread, or None for one that no longer
        exists (HTTP 404). Read-only, `format=minimal` (no headers or body).

        Batched (`_batched_get`): one HTTP request per BATCH_SIZE threads.
        """
        return self._batched_get(thread_ids, self._minimal_thread, _thread_messages, "thread check")

    def _batched_get(
        self,
        ids: Sequence[str],
        request: Callable[[str], Any],
        parse: Callable[[dict[str, Any]], _T],
        what: str,
    ) -> dict[str, _T | None]:
        """`parse` of the response to `request(id)` for each id, or None for a 404.

        One batch HTTP request per BATCH_SIZE ids instead of one request each. An id whose call
        fails otherwise (e.g. rate limited), alone in the batch or because the whole batch
        request failed, is logged and asked again on its own, with backoff; if that fails too,
        the error is raised and no later batch is sent. `what` names the call in that log.
        """
        found: dict[str, _T | None] = {}
        unique = list(dict.fromkeys(ids))  # a batch rejects duplicate request ids
        for start in range(0, len(unique), BATCH_SIZE):
            chunk = unique[start : start + BATCH_SIZE]
            answers, failed = self._batch(chunk, request, parse)
            found.update(answers)
            if failed:
                log.warning(
                    "batched Gmail calls failed: retrying one by one",
                    extra={
                        "call": what,
                        "failed": len(failed),
                        "error_types": sorted({type(exc).__name__ for exc in failed.values()}),
                    },
                )
            for item_id in chunk:
                if item_id in failed:
                    found[item_id] = self._get_one(item_id, request, parse)
        return found

    def _batch(
        self,
        ids: list[str],
        request: Callable[[str], Any],
        parse: Callable[[dict[str, Any]], _T],
    ) -> tuple[dict[str, _T | None], dict[str, Exception]]:
        """One batch request for `ids`: (answers, failures to ask again one by one).

        If the batch request itself fails (HTTP error on the batch, transport error, malformed
        reply), every id without an answer yet counts as failed with that error.
        """
        answers: dict[str, _T | None] = {}
        failed: dict[str, Exception] = {}

        def collect(
            request_id: str, response: dict[str, Any] | None, exc: Exception | None
        ) -> None:
            if exc is None:
                answers[request_id] = parse(response or {})
            elif is_not_found(exc):
                answers[request_id] = None
            else:
                failed[request_id] = exc

        batch = self._service.new_batch_http_request(callback=collect)
        for item_id in ids:
            batch.add(request(item_id), request_id=item_id)
        try:
            batch.execute()
        except Exception as exc:
            for item_id in ids:
                if item_id not in answers:
                    failed.setdefault(item_id, exc)
        return answers, failed

    @staticmethod
    def _get_one(
        item_id: str, request: Callable[[str], Any], parse: Callable[[dict[str, Any]], _T]
    ) -> _T | None:
        """`parse` of one call, or None if its resource no longer exists.

        googleapiclient retries a 429, a 5xx or a rate-limit 403 up to SINGLE_RETRIES times with
        exponential backoff; any other error, or one that outlasts the retries, is raised.
        """
        try:
            response: dict[str, Any] = request(item_id).execute(num_retries=SINGLE_RETRIES)
        except Exception as exc:
            if is_not_found(exc):
                return None
            raise
        return parse(response)

    def _minimal_get(self, message_id: str) -> Any:
        """The messages.get request for a message's id and labels only (not yet executed)."""
        return self._svc.messages().get(userId="me", id=message_id, format="minimal")

    def _minimal_thread(self, thread_id: str) -> Any:
        """The threads.get request for its messages' ids, labels and dates (not yet executed)."""
        return self._svc.threads().get(userId="me", id=thread_id, format="minimal")

    def has_written_to(self, query: str, scans: DomainScans | None = None) -> bool:
        """Whether I wrote to `query`, an address or a domain. Cached for this object's life.

        An address: a sent email matching `to:<address>` that really went to it
        (`sent_scan.wrote_to_address`). A domain: a sent email matching `to:<domain>` that went
        to a person on it (`sent_scan.wrote_to_person_at_domain`), its scan kept in `scans`
        across runs (when None, in this object only, so each run starts it over).
        """
        if query not in self._written:
            if "@" in query:
                self._written[query] = wrote_to_address(self, query)
            else:
                store = scans if scans is not None else self._scans
                self._written[query] = wrote_to_person_at_domain(self, query, store)
        return self._written[query]

    def sent_search(self, query: str, limit: int) -> tuple[list[str], bool]:
        """Up to `limit` ids of emails matching the Gmail search `query` (newest first), and
        whether Gmail has more. One `messages.list`, retried (SINGLE_RETRIES)."""
        resp = (
            self._svc.messages()
            .list(userId="me", q=query, maxResults=limit)
            .execute(num_retries=SINGLE_RETRIES)
        )
        return [m["id"] for m in resp.get("messages", [])], bool(resp.get("nextPageToken"))

    def sent_recipients(self, message_id: str) -> Email | None:
        """A sent email's To/Cc/Bcc headers and date (format=metadata, no body), or None when it
        was deleted since the search (HTTP 404). Retried like `sent_search`; any other error is
        raised once the retries run out."""
        try:
            return self.get_headers(message_id, list(RECIPIENT_HEADERS), SINGLE_RETRIES)
        except Exception as exc:
            if is_not_found(exc):
                return None
            raise

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
