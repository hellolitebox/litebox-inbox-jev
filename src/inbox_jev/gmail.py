"""Thin Gmail API wrapper: OAuth tokens, listing, reading, labelling and sending."""

from __future__ import annotations

import base64
import html
import logging
import re
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from html.parser import HTMLParser
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from . import signals

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]
INBOX, SPAM = "INBOX", "SPAM"


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
    path.write_text(creds.to_json())
    path.chmod(0o600)
    return path


def load_credentials(tokens_dir: Path, email: str) -> Credentials:
    path = token_path(tokens_dir, email)
    if not path.exists():
        raise FileNotFoundError(f"No token for {email}; run `inbox-jev auth add {email}`")
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        path.write_text(creds.to_json())
    return creds


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
        values = self.headers.get(name.lower())
        return values[0] if values else ""

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


def extract_body(payload: dict) -> str:
    """Prefer text/plain; fall back to text/html stripped of tags."""
    plain, rich = [], []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        if part.get("filename"):
            return
        if mime == "text/plain":
            plain.append(_decode(part.get("body", {}).get("data")))
        elif mime == "text/html":
            rich.append(_decode(part.get("body", {}).get("data")))
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    text = "\n".join(plain) if any(p.strip() for p in plain) else html_to_text("\n".join(rich))
    return re.sub(r"\s+\n", "\n", re.sub(r"[ \t ‌]+", " ", text)).strip()


class Gmail:
    def __init__(self, creds: Credentials):
        self._svc = build("gmail", "v1", credentials=creds, cache_discovery=False).users()
        self._label_ids: dict[str, str] | None = None
        self._user_labels: set[str] = set()
        self._written: dict[str, bool] = {}

    def list_inbox_ids(self, lookback: str, limit: int) -> list[str]:
        ids: list[str] = []
        token = None
        while len(ids) < limit:
            resp = (
                self._svc.messages()
                .list(
                    userId="me",
                    labelIds=[INBOX],
                    q=f"newer_than:{lookback}",
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
        headers: dict[str, list[str]] = {}
        for h in payload.get("headers", []):
            headers.setdefault(h["name"].lower(), []).append(h["value"])
        return Email(
            id=msg["id"],
            thread_id=msg["threadId"],
            label_ids=msg.get("labelIds", []),
            headers=headers,
            snippet=html.unescape(msg.get("snippet", "")),
            body=extract_body(payload),
            internal_date_ms=int(msg.get("internalDate", 0)),
        )

    def get_label_ids(self, message_id: str) -> list[str] | None:
        """Current labels of a message, or None if it no longer exists."""
        try:
            msg = self._svc.messages().get(userId="me", id=message_id, format="minimal").execute()
        except Exception as exc:  # googleapiclient.errors.HttpError 404
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                return None
            raise
        return msg.get("labelIds", [])

    def has_written_to(self, query: str) -> bool:
        """Whether any sent email matches `to:<query>` (an address or a domain). Cached."""
        if query not in self._written:
            resp = (
                self._svc.messages()
                .list(userId="me", q=f"in:sent to:{query}", maxResults=1)
                .execute()
            )
            self._written[query] = bool(resp.get("messages"))
        return self._written[query]

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
