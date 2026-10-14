"""Fakes and builders shared by the triage tests: no network, no Gmail, no Jev.

mypy checks this module (`[tool.mypy] files`): `gmail_factory` makes it check `FakeGmail`
against `triage.GmailApi`, so the fake can't drift from what triage calls.
"""

import re
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from inbox_jev.classifier import JevError
from inbox_jev.config import Mode
from inbox_jev.db import Message, MessageStatus, Unsubscribe
from inbox_jev.gmail import DomainScans, Email
from inbox_jev.policy import DecisionSource
from inbox_jev.triage import GmailFactory
from inbox_jev.unsubscribe import UnsubscribeMethod, UnsubscribeStatus

GMAIL_PASS = (
    "mx.google.com; dkim=pass header.i=@news.shop.com; spf=pass; dmarc=pass header.from=shop.com"
)


class FakeGmail:
    """`gmail.Gmail` in memory: records label changes, sends and sent-mail searches."""

    def __init__(self, emails: Sequence[Email]) -> None:
        self.emails = {e.id: e for e in emails}
        self.modified: list[tuple[str, list[str], list[str]]] = []
        self.sent: list[str] = []
        self.written_queries: list[str] = []
        self.header_errors: dict[str, Exception] = {}
        self.label_batches: list[list[str]] = []
        self._labels = {"INBOX": "INBOX", "SPAM": "SPAM"}

    def ensure_labels(self, names: list[str]) -> dict[str, str]:
        for n in names:
            self._labels.setdefault(n, f"Label_{n}")
        return self._labels

    def labels(self) -> dict[str, str]:
        return self._labels

    def list_inbox_ids(self, lookback: str, limit: int) -> list[str]:
        return list(self.emails)[:limit]

    def get(self, message_id: str) -> Email:
        return self.emails[message_id]

    def get_headers(self, message_id: str, names: list[str]) -> Email:
        if message_id in self.header_errors:
            raise self.header_errors[message_id]
        e = self.emails[message_id]
        wanted = {n.lower() for n in names}
        headers = {k: v for k, v in e.headers.items() if k in wanted}
        return Email(e.id, e.thread_id, e.label_ids, headers, "", "", e.internal_date_ms)

    def get_label_ids(self, message_ids: Sequence[str]) -> dict[str, list[str] | None]:
        self.label_batches.append(list(message_ids))
        return {i: self.emails[i].label_ids if i in self.emails else None for i in message_ids}

    def modify(self, message_id: str, add: list[str], remove: list[str]) -> None:
        self.modified.append((message_id, add, remove))
        e = self.emails[message_id]
        kept = [i for i in e.label_ids if i not in remove]
        e.label_ids = kept + [i for i in add if i not in kept]

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append(to)

    written = frozenset({"c@client.com"})

    def has_written_to(self, query: str, scans: DomainScans | None = None) -> bool:
        self.written_queries.append(query)
        return query in self.written


def gmail_factory(gmail: FakeGmail) -> GmailFactory:
    """A `triage.Context` Gmail factory that hands out `gmail` for every account."""
    return lambda account: gmail


class FakeClassifier:
    """Jev answering from `answers` by subject (an exception there is raised, as an SDK error
    would be); records each call's state."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = 0
        self.states = []

    def classify(self, state):
        self.calls += 1
        self.states.append(state)
        answer = self.answers[state["subject"]]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self):
        pass


def email(id, sender, subject, **headers):
    """An inbox email passing DMARC; `headers` keys use _ for - (list_unsubscribe=…)."""
    h = {"from": [sender], "subject": [subject], "authentication-results": [GMAIL_PASS]}
    h.update({k.replace("_", "-"): [v] for k, v in headers.items()})
    return Email(id, "t" + id, ["INBOX"], h, "", "body", 1_700_000_000_000)


class HttpError(Exception):
    """Shaped like googleapiclient.errors.HttpError: the status is on .resp."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = type("Resp", (), {"status": status})()


class JevDown(FakeClassifier):
    """Jev failing every call (counted)."""

    def classify(self, state):
        self.calls += 1
        raise JevError("Jev is down")


def age_errors(ctx, minutes):
    """Move every stored failure `minutes` into the past, as if later runs came by."""
    with ctx.sessions() as s:
        for m in s.query(Message).filter_by(status="error"):
            m.processed_at = m.processed_at - timedelta(minutes=minutes)
        s.commit()


def modifications(gmail, message_id):
    """The (id, add, remove) label changes FakeGmail recorded for one email."""
    return [m for m in gmail.modified if m[0] == message_id]


def pitch_email(id, unsubscribe="<mailto:u@agency.io>", **headers):
    """A pitch from bob@agency.io, with a mailto List-Unsubscribe unless `unsubscribe` is None."""
    if unsubscribe:
        headers["list_unsubscribe"] = unsubscribe
    return email(id, "Bob <bob@agency.io>", f"Pitch {id}", **headers)


def failed_unsub(s, account, gmail_id, list_key, category, sender="bob@agency.io", **msg):
    """Store an act-mode row and its `failed` unsubscribe (1 attempt), as `run` leaves them."""
    fields = dict(source=DecisionSource.JEV, confidence=0.95, archived=True, labels_added=[])
    fields.update(msg)
    s.add(
        Message(
            account=account.email,
            gmail_id=gmail_id,
            thread_id="t" + gmail_id,
            sender=sender,
            category=category,
            mode=Mode.ACT,
            status=MessageStatus.DONE,
            **fields,
        )
    )
    s.add(
        Unsubscribe(
            account=account.email,
            list_key=list_key,
            sender=sender,
            gmail_id=gmail_id,
            method=UnsubscribeMethod.MAILTO,
            status=UnsubscribeStatus.FAILED,
            attempts=1,
        )
    )
    s.commit()


def as_archived(*emails):
    """What Gmail holds after an act-mode archive: out of the inbox."""
    for e in emails:
        e.label_ids = [i for i in e.label_ids if i != "INBOX"]
    return {e.id: e for e in emails}


class JevFails(FakeClassifier):
    """Jev raising `error` on every call (counted), answering from `answers` once it is None."""

    def __init__(self, error, answers=None):
        super().__init__(answers or {})
        self.error = error

    def classify(self, state):
        if self.error is None:
            return super().classify(state)
        self.calls += 1
        raise self.error


def jev_http_error(error_type, status, body=None, request_id="req-1"):
    """An SDK HTTP error as `typesafe_sdk` raises it (type, status, body, response headers)."""
    import httpx2

    headers = httpx2.Headers({"x-typesafe-request-id": request_id} if request_id else {})
    return error_type(status, body, headers, endpoint="POST https://api.typesafe.ai/v1/systemone")


# Sent dates of SentMailService emails without one: a minute apart, newest first, from here.
SENT_BASE_S = 1_800_000_000
_SENT_QUERY = re.compile(r"^in:sent to:(\S+)(?: after:(\d+))?(?: before:(\d+))?$")


class SentMailService:
    """googleapiclient's Gmail service for the sent-mail search: messages().list and metadata get.

    `sent` maps a message id (newest first) to its recipient headers ({"To": "...", ...}). A
    `in:sent to:X` search matches an email whose recipient headers contain X, ignoring case, as
    Gmail's loose `to:` does, newest first; `after:S` / `before:S` keep the emails sent after /
    before Unix second S. `dates` maps an id to its sent second (default: SENT_BASE_S, then a
    minute earlier for each later id). `get_errors` maps an id to the exception its read
    raises. Every `execute` must pass `num_retries` (recorded in `retries`), as this path relies
    on googleapiclient's backoff. `ordered=False` lists matches in `sent`'s order instead of by
    date (a Gmail breaking the date order the domain scan relies on).
    """

    def __init__(self, sent, get_errors=None, dates=None, ordered=True):
        self.sent = sent
        self.get_errors = get_errors or {}
        self.dates = dict(dates or {})
        self.ordered = ordered
        self.queries = []
        self.reads = []
        self.retries = []

    def date_of(self, message_id):
        if message_id in self.dates:
            return self.dates[message_id]
        return SENT_BASE_S - 60 * list(self.sent).index(message_id)

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults, **kw):
        self.queries.append(q)
        match = _SENT_QUERY.match(q)
        assert match, q
        target, after, before = match.groups()
        ids = [i for i, h in self.sent.items() if any(target in v.lower() for v in h.values())]
        if after is not None:
            ids = [i for i in ids if self.date_of(i) > int(after)]
        if before is not None:
            ids = [i for i in ids if self.date_of(i) < int(before)]
        if self.ordered:
            ids.sort(key=self.date_of, reverse=True)  # stable: same second keeps the given order
        page: dict[str, Any] = {"messages": [{"id": i} for i in ids[:maxResults]]}
        if len(ids) > maxResults:
            page["nextPageToken"] = "next"
        svc = self

        class Req:
            def execute(self, *, num_retries):
                svc.retries.append(num_retries)
                return page

        return Req()

    def get(self, userId, id, format, metadataHeaders):
        assert (userId, format, metadataHeaders) == ("me", "metadata", ["To", "Cc", "Bcc"])
        self.reads.append(id)
        svc = self

        class Req:
            def execute(self, *, num_retries):
                svc.retries.append(num_retries)
                if id in svc.get_errors:
                    raise svc.get_errors[id]
                headers = [{"name": k, "value": v} for k, v in svc.sent[id].items()]
                return {
                    "id": id,
                    "threadId": "t" + id,
                    "internalDate": str(svc.date_of(id) * 1000 + 123),
                    "payload": {"headers": headers},
                }

        return Req()


def sent_mail_gmail(monkeypatch, svc):
    """A real `gmail.Gmail` talking to `svc` (a SentMailService) instead of Google."""
    from inbox_jev import gmail as gmail_module

    monkeypatch.setattr(gmail_module, "build", lambda *args, **kwargs: svc)
    return gmail_module.Gmail(creds=None)  # type: ignore[arg-type]  # build() is replaced
