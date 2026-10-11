"""End-to-end triage with fake Gmail and a fake Jev classifier."""

import pytest
from conftest import GMAIL_PASS, make_classification

from inbox_jev import triage
from inbox_jev.config import Mode, Settings
from inbox_jev.db import Message, SenderRule, Unsubscribe, make_session_factory
from inbox_jev.gmail import Email


class FakeGmail:
    def __init__(self, emails):
        self.emails = {e.id: e for e in emails}
        self.modified = []
        self.sent = []
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

    def get_label_ids(self, message_id):
        return self.emails[message_id].label_ids

    def modify(self, message_id, add, remove):
        self.modified.append((message_id, add, remove))

    def send(self, to, subject, body):
        self.sent.append(to)


class FakeClassifier:
    def __init__(self, answers):
        self.answers = answers
        self.calls = 0

    def classify(self, state):
        self.calls += 1
        return self.answers[state["subject"]]

    def close(self):
        pass


def email(id, sender, subject, **headers):
    h = {"from": [sender], "subject": [subject], "authentication-results": [GMAIL_PASS]}
    h.update({k.replace("_", "-"): [v] for k, v in headers.items()})
    return Email(id, "t" + id, ["INBOX"], h, "", "body", 1_700_000_000_000)


@pytest.fixture
def setup(cfg, account, monkeypatch):
    emails = [
        email(
            "1",
            "Client <c@client.com>",
            "Proposal",
        ),
        email(
            "2",
            "Shop <promo@shop.com>",
            "50% off",
            list_unsubscribe="<mailto:u@shop.com>",
        ),
        email("3", "Bank <x@acme.com>", "Internal"),  # allowlisted domain
        email("4", "Weird <w@weird.biz>", "Maybe promo"),
    ]
    answers = {
        "Proposal": make_classification("important", 0.9, needs_reply=0.9),
        "50% off": make_classification("marketing", 0.97),
        "Maybe promo": make_classification("marketing", 0.6),
    }
    gmail = FakeGmail(emails)
    monkeypatch.setattr(triage, "Gmail", lambda creds: gmail)
    monkeypatch.setattr(triage, "load_credentials", lambda d, e: None)
    settings = Settings(_env_file=None, typesafe_api_key="x", database_url="sqlite:///:memory:")
    ctx = triage.Context(settings, cfg, make_session_factory(settings.database_url))
    ctx._classifier = FakeClassifier(answers)
    return ctx, gmail, account


def test_act_mode_end_to_end(setup):
    ctx, gmail, account = setup
    run = triage.run_account(ctx, account, Mode.ACT)
    assert (run.processed, run.errors) == (4, 0)
    assert ctx._classifier.calls == 3  # allowlisted sender skips Jev

    mods = {m[0]: m for m in gmail.modified}
    assert "Label_Jev/Responder" in mods["1"][1] and mods["1"][2] == []
    assert mods["2"][2] == ["INBOX"]
    assert "Label_Jev/Revisar" in mods["4"][1] and mods["4"][2] == []
    assert gmail.sent == ["u@shop.com"]

    with ctx.sessions() as s:
        rows = {m.gmail_id: m for m in s.query(Message)}
        assert rows["2"].archived and rows["2"].category == "marketing"
        assert rows["3"].source == "allowlist"
        assert rows["4"].low_confidence
        unsub = s.query(Unsubscribe).one()
        assert (unsub.list_key, unsub.method, unsub.status) == ("shop.com", "mailto", "ok")

    # second pass: nothing new, no duplicate work
    run2 = triage.run_account(ctx, account, Mode.ACT)
    assert run2.processed == 0 and ctx._classifier.calls == 3


def test_dry_run_touches_nothing_then_label_reprocesses(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.DRY_RUN)
    assert all(add == [] and rem == [] for _, add, rem in gmail.modified)
    assert gmail.sent == []

    run = triage.run_account(ctx, account, Mode.LABEL)
    assert run.processed == 4
    assert all(rem == [] for _, _, rem in gmail.modified)
    assert gmail.sent == []


def test_correction_creates_rule(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.ACT)
    # user moves the archived promo back to the inbox
    gmail.emails["2"].label_ids = ["INBOX", "Label_Jev/Marketing"]
    with ctx.sessions() as s:
        s.query(Message).update({Message.correction_checked_at: None})
        s.commit()
    triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        rule = s.query(SenderRule).filter_by(sender_key="promo@shop.com").one()
        assert (rule.category, rule.source, rule.active) == ("important", "correction", True)


def test_classifier_error_is_recorded_and_retried(setup):
    ctx, gmail, account = setup
    answers = ctx._classifier.answers
    good = answers.pop("Proposal")
    triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        assert s.query(Message).filter_by(gmail_id="1").one().status == "error"
    answers["Proposal"] = good
    run = triage.run_account(ctx, account, Mode.ACT)
    assert run.processed == 1
