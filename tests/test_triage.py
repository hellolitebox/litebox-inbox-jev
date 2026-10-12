"""End-to-end triage with fake Gmail and a fake Jev classifier."""

import pytest
from conftest import GMAIL_PASS, make_classification

from inbox_jev import digest, triage
from inbox_jev.config import Mode, Settings
from inbox_jev.db import Message, SenderRule, Unsubscribe, make_session_factory, now
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
        e = self.emails[message_id]
        kept = [i for i in e.label_ids if i not in remove]
        e.label_ids = kept + [i for i in add if i not in kept]

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
    assert "Label_Jev/Needs Reply" in mods["1"][1] and mods["1"][2] == []
    assert mods["2"][2] == ["INBOX"]
    assert "Label_Jev/Review" in mods["4"][1] and mods["4"][2] == []
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

    with ctx.sessions() as s:
        rows = {m.gmail_id: m for m in s.query(Message)}
        assert "Jev/Needs Reply" in rows["1"].decided_labels
        assert all(m.labels_added == [] for m in rows.values())
        text = digest.build(s, ctx.cfg, account.email)
    reply_section = text.split("Piden respuesta (1)*", 1)[1].split("\n\n", 1)[0]
    assert "Proposal" in reply_section
    assert not any(subj in reply_section for subj in ("50% off", "Internal", "Maybe promo"))

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


def test_act_categories_gate_actions_end_to_end(setup):
    ctx, gmail, account = setup
    account.act_categories = ["notification"]
    triage.run_account(ctx, account, Mode.ACT)
    mods = {m[0]: m for m in gmail.modified}
    assert "Label_Jev/Marketing" in mods["2"][1] and mods["2"][2] == []  # labeled, not archived
    assert gmail.sent == []  # no unsubscribe
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="2").one()
        assert row.mode == "act" and not row.archived
        assert s.query(Unsubscribe).count() == 0


def test_unknown_act_category_fails_the_run(setup):
    ctx, gmail, account = setup
    account.act_categories = ["notifications"]  # typo
    with pytest.raises(ValueError, match="unknown act_categories"):
        triage.run_account(ctx, account, Mode.ACT)
    assert gmail.modified == []


def test_act_reprocesses_label_rows_and_drops_stale_labels(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.LABEL)
    # Jev changes its mind about "50% off" on the second look.
    ctx._classifier.answers["50% off"] = make_classification("newsletter", 0.95)
    gmail.modified.clear()
    run = triage.run_account(ctx, account, Mode.ACT)
    assert run.processed == 4
    _, add, remove = next(m for m in gmail.modified if m[0] == "2")
    assert "Label_Jev/Newsletters" in add
    assert "INBOX" in remove and "Label_Jev/Marketing" in remove
    assert "Label_Jev/Processed" not in remove
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="2").one()
        assert row.archived and row.mode == "act" and "Jev/Marketing" not in row.labels_added
    # third pass: nothing left to do
    assert triage.run_account(ctx, account, Mode.ACT).processed == 0


def _mods(gmail, message_id):
    return [m for m in gmail.modified if m[0] == message_id]


def test_restored_rows_are_never_reprocessed(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.LABEL)
    # user runs `restore` on "50% off": labels removed, row flagged restored
    gmail.emails["2"].label_ids = ["INBOX"]
    with ctx.sessions() as s:
        s.query(Message).filter_by(gmail_id="2").update({Message.restored: True})
        s.commit()
    gmail.modified.clear()
    account.act_categories = ["marketing"]
    triage.run_account(ctx, account, Mode.ACT)
    assert _mods(gmail, "2") == []
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="2").one()
        assert row.restored and not row.archived and row.mode == "label"


def test_reprocess_follows_category_the_user_changed_since_last_check(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.LABEL)
    calls = ctx._classifier.calls
    # user swaps Jev/Marketing for Jev/Important; corrections.check ran less than an hour ago
    gmail.emails["2"].label_ids = ["INBOX", "Label_Jev/Important", "Label_Jev/Processed"]
    with ctx.sessions() as s:
        s.query(Message).update({Message.correction_checked_at: now()})
        s.commit()
    gmail.modified.clear()
    triage.run_account(ctx, account, Mode.ACT)

    [(_, add, remove)] = _mods(gmail, "2")
    assert "Label_Jev/Important" in add and "Label_Jev/Marketing" not in add
    assert "INBOX" not in remove
    assert gmail.sent == []
    assert ctx._classifier.calls == calls + 2  # "50% off" skipped Jev: the new rule decided
    with ctx.sessions() as s:
        rule = s.query(SenderRule).filter_by(sender_key="promo@shop.com").one()
        assert (rule.category, rule.source, rule.active) == ("important", "correction", True)
        row = s.query(Message).filter_by(gmail_id="2").one()
        assert row.corrected_category == "important" and row.category == "important"
        assert row.source == "rule" and not row.archived and row.mode == "act"


def test_reprocess_leaves_alone_email_whose_needs_reply_was_removed(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.LABEL)
    calls = ctx._classifier.calls
    gmail.emails["1"].label_ids.remove("Label_Jev/Needs Reply")
    gmail.modified.clear()
    triage.run_account(ctx, account, Mode.ACT)
    assert _mods(gmail, "1") == []
    assert ctx._classifier.calls == calls + 2  # "1" was not sent to Jev again
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="1").one()
        assert row.status == "done" and row.mode == "act"
        assert "Jev/Needs Reply" in row.labels_added  # unchanged
    # handled in act mode: not revisited
    gmail.modified.clear()
    assert triage.run_account(ctx, account, Mode.ACT).processed == 0
    assert gmail.modified == []


def test_marketing_downgraded_to_spam_is_not_spammed_when_only_marketing_acts(setup):
    ctx, gmail, account = setup
    # "Maybe promo" has no List-Unsubscribe: confident marketing gets downgraded to spam
    ctx._classifier.answers["Maybe promo"] = make_classification("marketing", 0.97)
    account.act_categories = ["marketing"]
    triage.run_account(ctx, account, Mode.ACT)
    [(_, add, remove)] = _mods(gmail, "4")
    assert remove == [] and "SPAM" not in add and "Label_Jev/Spam" in add
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="4").one()
        assert row.category == "spam" and row.downgraded_to_spam
        assert not row.spammed and not row.archived


def test_empty_act_categories_acts_on_nothing(setup):
    ctx, gmail, account = setup
    account.act_categories = []
    run = triage.run_account(ctx, account, Mode.ACT)
    assert run.processed == 4
    assert all(remove == [] for _, _, remove in gmail.modified)
    assert gmail.sent == []
    with ctx.sessions() as s:
        assert not any(m.archived or m.spammed for m in s.query(Message))


def test_reprocess_error_stays_retryable_and_keeps_old_labels(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.LABEL)
    answers = ctx._classifier.answers
    answers.pop("50% off")
    gmail.modified.clear()
    run = triage.run_account(ctx, account, Mode.ACT)
    assert run.errors == 1 and _mods(gmail, "2") == []
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="2").one()
        assert row.status == "error"
        assert row.labels_added == ["Jev/Marketing", "Jev/Processed"]

    answers["50% off"] = make_classification("newsletter", 0.95)
    assert triage.run_account(ctx, account, Mode.ACT).processed == 1
    [(_, add, remove)] = _mods(gmail, "2")
    assert "Label_Jev/Newsletters" in add
    assert "INBOX" in remove and "Label_Jev/Marketing" in remove


def _assert_left_alone_and_not_revisited(ctx, gmail, account, message_id):
    assert _mods(gmail, message_id) == []
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id=message_id).one()
        assert row.status == "done" and row.mode == "act" and not row.archived
    gmail.modified.clear()
    assert triage.run_account(ctx, account, Mode.ACT).processed == 0
    assert gmail.modified == []


def test_reprocess_leaves_alone_email_the_user_added_needs_reply_to(setup):
    ctx, gmail, account = setup
    ctx._classifier.answers["50% off"] = make_classification("newsletter", 0.97)
    triage.run_account(ctx, account, Mode.LABEL)
    # user flags it to reply later (or "Label as" Jev/Needs Reply on the whole conversation)
    gmail.emails["2"].label_ids.append("Label_Jev/Needs Reply")
    gmail.modified.clear()
    account.act_categories = ["transactional", "notification", "newsletter"]
    triage.run_account(ctx, account, Mode.ACT)
    assert "INBOX" in gmail.emails["2"].label_ids
    _assert_left_alone_and_not_revisited(ctx, gmail, account, "2")


def test_reprocess_leaves_alone_email_the_user_added_a_second_category_to(setup):
    ctx, gmail, account = setup
    ctx._classifier.answers["50% off"] = make_classification("newsletter", 0.97)
    triage.run_account(ctx, account, Mode.LABEL)
    gmail.emails["2"].label_ids.append("Label_Jev/Important")
    gmail.modified.clear()
    account.act_categories = ["newsletter"]
    triage.run_account(ctx, account, Mode.ACT)
    assert "INBOX" in gmail.emails["2"].label_ids
    _assert_left_alone_and_not_revisited(ctx, gmail, account, "2")


def test_reprocess_leaves_alone_starred_email(setup):
    ctx, gmail, account = setup
    ctx._classifier.answers["50% off"] = make_classification("newsletter", 0.97)
    ctx._classifier.answers["Maybe promo"] = make_classification("newsletter", 0.97)
    triage.run_account(ctx, account, Mode.LABEL)
    gmail.emails["2"].label_ids.append("STARRED")
    gmail.modified.clear()
    account.act_categories = ["newsletter"]
    triage.run_account(ctx, account, Mode.ACT)
    assert "INBOX" in gmail.emails["2"].label_ids
    [(_, _, remove)] = _mods(gmail, "4")  # an unstarred sibling is still archived
    assert remove == ["INBOX"]
    gmail.modified = [m for m in gmail.modified if m[0] != "4"]
    _assert_left_alone_and_not_revisited(ctx, gmail, account, "2")


def test_starred_new_email_is_processed(setup):
    ctx, gmail, account = setup
    ctx._classifier.answers["50% off"] = make_classification("newsletter", 0.97)
    gmail.emails["2"].label_ids.append("STARRED")
    account.act_categories = ["newsletter"]
    triage.run_account(ctx, account, Mode.ACT)
    [(_, _, remove)] = _mods(gmail, "2")
    assert remove == ["INBOX"]
