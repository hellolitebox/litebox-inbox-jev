"""Fakes and builders shared by the triage tests: no network, no Gmail, no Jev."""

from datetime import timedelta

from inbox_jev.classifier import JevError
from inbox_jev.config import Mode
from inbox_jev.db import Message, MessageStatus, Unsubscribe
from inbox_jev.gmail import Email
from inbox_jev.policy import DecisionSource
from inbox_jev.unsubscribe import UnsubscribeMethod, UnsubscribeStatus

GMAIL_PASS = (
    "mx.google.com; dkim=pass header.i=@news.shop.com; spf=pass; dmarc=pass header.from=shop.com"
)


class FakeGmail:
    """`gmail.Gmail` in memory: records label changes, sends and sent-mail searches."""

    def __init__(self, emails):
        self.emails = {e.id: e for e in emails}
        self.modified = []
        self.sent = []
        self.written_queries = []
        self.header_errors = {}
        self.label_batches = []
        self._labels = {"INBOX": "INBOX", "SPAM": "SPAM"}

    def ensure_labels(self, names):
        for n in names:
            self._labels.setdefault(n, f"Label_{n}")
        return self._labels

    def labels(self):
        return self._labels

    def list_inbox_ids(self, lookback, limit):
        return list(self.emails)[:limit]

    def get(self, message_id):
        return self.emails[message_id]

    def get_headers(self, message_id, names):
        if message_id in self.header_errors:
            raise self.header_errors[message_id]
        e = self.emails[message_id]
        wanted = {n.lower() for n in names}
        headers = {k: v for k, v in e.headers.items() if k in wanted}
        return Email(e.id, e.thread_id, e.label_ids, headers, "", "", e.internal_date_ms)

    def get_label_ids(self, message_ids):
        self.label_batches.append(list(message_ids))
        return {i: self.emails[i].label_ids if i in self.emails else None for i in message_ids}

    def modify(self, message_id, add, remove):
        self.modified.append((message_id, add, remove))
        e = self.emails[message_id]
        kept = [i for i in e.label_ids if i not in remove]
        e.label_ids = kept + [i for i in add if i not in kept]

    def send(self, to, subject, body):
        self.sent.append(to)

    written = frozenset({"c@client.com"})

    def has_written_to(self, query):
        self.written_queries.append(query)
        return query in self.written


class FakeClassifier:
    """Jev answering from `answers` by subject; records each call's state."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = 0
        self.states = []

    def classify(self, state):
        self.calls += 1
        self.states.append(state)
        return self.answers[state["subject"]]

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
