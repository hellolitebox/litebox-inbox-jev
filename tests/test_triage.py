"""End-to-end triage with fake Gmail and a fake Jev classifier."""

from datetime import timedelta

import pytest
from conftest import GMAIL_PASS, make_classification

from inbox_jev import digest, rules, triage
from inbox_jev.config import Mode, Settings
from inbox_jev.db import Message, SenderRule, Unsubscribe, make_session_factory, now
from inbox_jev.gmail import Email


class FakeGmail:
    def __init__(self, emails):
        self.emails = {e.id: e for e in emails}
        self.modified = []
        self.sent = []
        self.written_queries = []
        self.header_errors = {}
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

    def get_label_ids(self, message_id):
        return self.emails[message_id].label_ids

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


def _age_errors(ctx, minutes):
    """Move every stored failure `minutes` into the past, as if later runs came by."""
    with ctx.sessions() as s:
        for m in s.query(Message).filter_by(status="error"):
            m.processed_at = m.processed_at - timedelta(minutes=minutes)
        s.commit()


def test_classifier_error_is_recorded_and_retried(setup):
    ctx, gmail, account = setup
    answers = ctx._classifier.answers
    good = answers.pop("Proposal")
    triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="1").one()
        assert (row.status, row.error_attempts) == ("error", 1)
    answers["Proposal"] = good
    _age_errors(ctx, 5)
    run = triage.run_account(ctx, account, Mode.ACT)
    assert run.processed == 1
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="1").one()
        assert (row.status, row.error, row.error_attempts) == ("done", None, 0)


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


def test_unverified_marketing_is_archived_not_spammed_or_unsubscribed(setup):
    ctx, gmail, account = setup
    # "Maybe promo" has no List-Unsubscribe: confident marketing is archived, never unsubscribed
    ctx._classifier.answers["Maybe promo"] = make_classification("marketing", 0.97)
    account.act_categories = ["marketing"]
    triage.run_account(ctx, account, Mode.ACT)
    [(_, add, remove)] = _mods(gmail, "4")
    assert remove == ["INBOX"] and "SPAM" not in add
    assert "Label_Jev/Marketing" in add and "Label_Jev/Spam" not in add
    assert gmail.sent == ["u@shop.com"]  # only "50% off", which has List-Unsubscribe
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="4").one()
        assert row.category == "marketing" and row.downgraded_to_spam
        assert row.archived and not row.spammed
        assert s.query(Unsubscribe).filter_by(sender="w@weird.biz").count() == 0


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
    _age_errors(ctx, 5)
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


def _act_with_only(ctx, account, categories):
    account.act_categories = categories
    return triage.run_account(ctx, account, Mode.ACT)


def test_mark_for_reprocess_redoes_rows_after_widening_act_categories(setup):
    ctx, gmail, account = setup
    # First pass: only notification acts, so marketing rows stay in the inbox as mode=act.
    _act_with_only(ctx, account, ["notification"])
    assert _act_with_only(ctx, account, ["notification", "marketing"]).processed == 0

    with ctx.sessions() as s:
        assert triage.mark_for_reprocess(s, ctx.cfg, account.email, "marketing") == 2  # "2", "4"
    gmail.modified.clear()
    run = _act_with_only(ctx, account, ["notification", "marketing"])
    assert run.processed == 2
    [(_, _, remove)] = _mods(gmail, "2")
    assert remove == ["INBOX"]  # confident marketing archived now
    [(_, _, remove)] = _mods(gmail, "4")
    assert remove == []  # low confidence: still in the inbox
    with ctx.sessions() as s:
        rows = {m.gmail_id: m for m in s.query(Message)}
        assert rows["2"].archived and rows["2"].mode == "act"
        assert not rows["4"].archived and rows["4"].mode == "act"
        # archived now: no longer eligible
        assert triage.mark_for_reprocess(s, ctx.cfg, account.email, "marketing") == 1


def test_mark_for_reprocess_filters(setup):
    ctx, gmail, account = setup
    _act_with_only(ctx, account, ["notification"])
    with ctx.sessions() as s:
        rows = {m.gmail_id: m for m in s.query(Message)}
        rows["2"].restored = True
        s.commit()
        # "4" is low confidence, "2" is restored
        n = triage.mark_for_reprocess(s, ctx.cfg, account.email, "marketing", True)
        assert n == 1
        assert triage.mark_for_reprocess(s, ctx.cfg, "other@x.com", "marketing") == 0
        assert triage.mark_for_reprocess(s, ctx.cfg, account.email, "important") == 2  # "1", "3"
        rows = {m.gmail_id: m for m in s.query(Message)}
        assert rows["2"].mode == "act" and rows["4"].mode == "label"
        with pytest.raises(ValueError, match="unknown category"):
            triage.mark_for_reprocess(s, ctx.cfg, account.email, "nope")


def test_mark_for_reprocess_skips_label_mode_and_error_rows(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.LABEL)
    with ctx.sessions() as s:
        assert triage.mark_for_reprocess(s, ctx.cfg, account.email, "marketing") == 0


def test_previous_contact_is_sent_to_jev(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.LABEL)
    contacts = {s["subject"]: s.get("previous_contact") for s in ctx._classifier.states}
    assert contacts == {"Proposal": "wrote_to_sender", "50% off": "never", "Maybe promo": "never"}
    assert "acme.com" not in gmail.written_queries  # allowlisted sender: no lookup at all


def test_known_contact_reply_blocks_group_archive_end_to_end(setup):
    # Same uncertain pitch with needs_reply from a sender I wrote to (stays) and a stranger
    # (group-archived): triage passes previous_contact through to the policy.
    ctx, gmail, account = setup
    gmail.emails = {
        "5": email("5", "Vendor <c@client.com>", "Follow-up"),
        "6": email("6", "Stranger <s@pitch.biz>", "Pitch"),
    }
    for subject in ("Follow-up", "Pitch"):
        c = make_classification("cold_outreach", 0.60, needs_reply=0.9)
        c.probabilities = {"cold_outreach": 0.60, "marketing": 0.36, "important": 0.0}
        ctx._classifier.answers[subject] = c
    triage.run_account(ctx, account, Mode.ACT)
    mods = {m[0]: m for m in gmail.modified}
    assert "Label_Jev/Needs Reply" in mods["5"][1] and mods["5"][2] == []
    assert mods["6"][2] == ["INBOX"]


class ContactGmail:
    def __init__(self, written=(), fail=False):
        self.written, self.fail, self.queries = set(written), fail, []

    def has_written_to(self, query):
        self.queries.append(query)
        if self.fail:
            raise RuntimeError("boom")
        return query in self.written


@pytest.mark.parametrize(
    ("written", "sender", "expected", "queries"),
    [
        ({"a@corp.com"}, "A@Corp.com", "wrote_to_sender", ["a@corp.com"]),
        ({"corp.com"}, "b@corp.com", "wrote_to_their_domain", ["b@corp.com", "corp.com"]),
        ((), "b@corp.com", "never", ["b@corp.com", "corp.com"]),
        ({"gmail.com"}, "x@gmail.com", "never", ["x@gmail.com"]),  # free mail: no domain check
        ({"acme.com"}, "y@acme.com", "never", ["y@acme.com"]),  # my own domain
        ((), "x@corp.com OR from:me", None, []),  # never builds a query from odd input
    ],
)
def test_previous_contact(written, sender, expected, queries):
    gmail = ContactGmail(written)
    assert triage.previous_contact(gmail, "me@acme.com", sender) == expected
    assert gmail.queries == queries


def test_previous_contact_error_is_unknown():
    assert triage.previous_contact(ContactGmail(fail=True), "me@acme.com", "a@b.com") is None


def test_digest_lists_kept_action_category(setup):
    ctx, gmail, account = setup
    gmail.emails["5"] = email("5", "Bank <alerts@bank.com>", "Approve payment")
    ctx._classifier.answers["Approve payment"] = make_classification("action_required", 0.7)
    triage.run_account(ctx, account, Mode.ACT)
    assert all(m[2] == [] for m in gmail.modified if m[0] == "5")  # stays in the inbox
    with ctx.sessions() as s:
        text = digest.build(s, ctx.cfg, account.email)
    assert "Jev/Action (1)" in text and "Approve payment" in text


def test_digest_does_not_repeat_kept_emails_in_needs_reply(setup):
    ctx, gmail, account = setup
    gmail.emails["5"] = email("5", "Bank <alerts@bank.com>", "Approve payment")
    ctx._classifier.answers["Approve payment"] = make_classification(
        "action_required", 0.7, needs_reply=0.9
    )
    gmail.emails["6"] = email("6", "Ana <ana@client.com>", "Can we meet?")
    ctx._classifier.answers["Can we meet?"] = make_classification("important", 0.9, needs_reply=0.9)
    triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        text = digest.build(s, ctx.cfg, account.email)
    assert text.count("Approve payment") == 1
    reply = text.split("Piden respuesta")[1]
    assert "Can we meet?" in reply and "Approve payment" not in reply


def _pitch(id, unsubscribe="<mailto:u@agency.io>", **headers):
    if unsubscribe:
        headers["list_unsubscribe"] = unsubscribe
    return email(id, "Bob <bob@agency.io>", f"Pitch {id}", **headers)


def test_auto_rule_then_reply_to_sender_never_unsubscribes(setup):
    # Bob earns an auto rule while a stranger (label mode, so nothing is unsubscribed yet).
    # Then I reply to him: the rule still archives his next pitch but never unsubscribes.
    ctx, gmail, account = setup
    hits = ctx.cfg.auto_rule.hits
    gmail.emails = {str(i): _pitch(str(i)) for i in range(hits)}
    for i in range(hits):
        ctx._classifier.answers[f"Pitch {i}"] = make_classification("cold_outreach", 0.97)
    triage.run_account(ctx, account, Mode.LABEL)
    with ctx.sessions() as s:
        rule = s.query(SenderRule).filter_by(sender_key="bob@agency.io").one()
        assert (rule.category, rule.source, rule.active) == ("cold_outreach", "auto", True)

    gmail.written = frozenset({"bob@agency.io"})
    gmail.emails["9"] = _pitch("9")
    calls = ctx._classifier.calls
    triage.run_account(ctx, account, Mode.ACT)
    assert ctx._classifier.calls == calls  # the rule decided (the label-mode ones are redone too)
    assert all(m[2] == ["INBOX"] for m in gmail.modified[-(hits + 1) :])  # archived
    assert gmail.sent == []
    with ctx.sessions() as s:
        assert s.query(Unsubscribe).count() == 0


@pytest.mark.parametrize("act_categories", [None, ["cold_outreach"]])
def test_manual_domain_rule_on_known_domain_never_unsubscribes(setup, act_categories):
    ctx, gmail, account = setup
    account.act_categories = act_categories  # an explicit list must still run the search
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "@agency.io", "cold_outreach", "manual")
        s.commit()
    gmail.written = frozenset({"agency.io"})  # wrote_to_their_domain
    gmail.emails = {"9": _pitch("9")}
    triage.run_account(ctx, account, Mode.ACT)
    assert gmail.written_queries == ["bob@agency.io", "agency.io"]
    assert gmail.modified[-1][2] == ["INBOX"]
    assert gmail.sent == []


def test_rule_path_still_unsubscribes_strangers(setup):
    ctx, gmail, account = setup
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "@agency.io", "cold_outreach", "manual")
        s.commit()
    gmail.emails = {"9": _pitch("9")}
    triage.run_account(ctx, account, Mode.ACT)
    assert gmail.written_queries == ["bob@agency.io", "agency.io"]  # "never"
    assert gmail.sent == ["u@agency.io"]


@pytest.mark.parametrize(
    ("category", "headers", "settled"),
    [
        ("marketing", {}, False),  # unsubscribes known contacts too
        ("newsletter", {}, False),  # never unsubscribes
        ("cold_outreach", {"unsubscribe": None}, False),  # unverified: no header
        ("cold_outreach", {"list_post": "<mailto:g@groups.io>"}, False),  # discussion list
        ("cold_outreach", {}, True),  # list already unsubscribed
    ],
)
def test_rule_hit_that_cannot_unsubscribe_skips_contact_check(setup, category, headers, settled):
    ctx, gmail, account = setup
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "bob@agency.io", category, "manual")
        if settled:
            s.add(
                Unsubscribe(
                    account=account.email,
                    list_key="agency.io",
                    sender="bob@agency.io",
                    gmail_id="0",
                    method="mailto",
                    status="ok",
                )
            )
        s.commit()
    gmail.emails = {"9": _pitch("9", **headers)}
    triage.run_account(ctx, account, Mode.ACT)
    assert gmail.written_queries == []
    assert gmail.modified[-1][2] == ["INBOX"]


@pytest.mark.parametrize(
    ("mode", "act_categories"),
    [
        (Mode.LABEL, None),
        (Mode.DRY_RUN, None),
        (Mode.ACT, ["marketing"]),  # cold_outreach not acted on
    ],
)
def test_rule_hit_that_will_not_act_skips_contact_check(setup, mode, act_categories):
    ctx, gmail, account = setup
    account.act_categories = act_categories
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "bob@agency.io", "cold_outreach", "manual")
        s.commit()
    gmail.emails = {"9": _pitch("9")}
    triage.run_account(ctx, account, mode)
    assert gmail.written_queries == []  # no has_written_to calls
    assert gmail.sent == []


def test_group_relayed_email_is_keyed_on_its_author(setup):
    ctx, gmail, account = setup
    gmail.emails["6"] = email(
        "6",
        "\"'Dana Lee' via Hello\" <hello@acme.com>",
        "Partnership idea",
        x_original_sender="dana@pitchco.io",
        mailing_list="list hello@acme.com; contact hello+owners@acme.com",
        list_id="<hello.acme.com>",
    )
    ctx._classifier.answers["Partnership idea"] = make_classification("cold_outreach", 0.99)
    triage.run_account(ctx, account, Mode.ACT)
    # acme.com is allowlisted, but the author isn't: Jev classified it.
    state = next(s for s in ctx._classifier.states if s["subject"] == "Partnership idea")
    assert state["original_sender"] == "dana@pitchco.io"
    assert state["sent_via_group"] == "hello@acme.com"
    assert "dana@pitchco.io" in gmail.written_queries
    with ctx.sessions() as s:
        row = s.query(Message).filter_by(gmail_id="6").one()
        assert (row.sender, row.category, row.archived) == (
            "dana@pitchco.io",
            "cold_outreach",
            True,
        )
        assert {r.sender_key for r in s.query(SenderRule)} & {"hello@acme.com"} == set()
    assert gmail.sent == ["u@shop.com"]  # only the earlier marketing unsubscribe, none via group


def _group_email(id, subject, author="dana@pitchco.io", group="hello@acme.com"):
    domain = group.rsplit("@", 1)[1]
    return email(
        id,
        f"\"'Dana Lee' via Hello\" <{group}>",
        subject,
        x_original_sender=author,
        mailing_list=f"list {group}; contact owners@{domain}",
        list_id=f"<{group.replace('@', '.')}>",
        authentication_results=f"mx.google.com; dmarc=pass header.from={domain}",
    )


def test_external_group_email_keeps_the_group_as_sender(setup):
    ctx, gmail, account = setup
    gmail.emails = {"6": _group_email("6", "Hi", author="ceo@acme.com", group="news@evil.com")}
    ctx._classifier.answers["Hi"] = make_classification("spam", 0.99)
    triage.run_account(ctx, account, Mode.ACT)
    state = ctx._classifier.states[0]  # not allowlisted as ceo@acme.com: Jev decided
    assert "original_sender" not in state
    with ctx.sessions() as s:
        assert s.query(Message).filter_by(gmail_id="6").one().sender == "news@evil.com"


def test_correction_on_old_group_row_creates_rule_on_author(setup, monkeypatch):
    ctx, gmail, account = setup
    gmail.emails = {"6": _group_email("6", "Pitch")}
    ctx._classifier.answers["Pitch"] = make_classification("cold_outreach", 0.99)
    # Simulate a row stored before authors were recovered: sender was the group.
    with monkeypatch.context() as m:
        m.setattr(triage, "signals_for", lambda email, account: email.signals)
        triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        assert s.query(Message).filter_by(gmail_id="6").one().sender == "hello@acme.com"
        assert triage.backfill_senders(s, gmail, account) == (1, 1, 0)
        assert triage.backfill_senders(s, gmail, account) == (1, 0, 0)  # idempotent
        s.query(Message).update({Message.correction_checked_at: None})
        s.commit()
    gmail.emails["6"].label_ids = ["INBOX", "Label_Jev/Outreach"]  # pulled back
    triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        keys = {r.sender_key: r.source for r in s.query(SenderRule)}
        assert keys.get("dana@pitchco.io") == "correction"
        assert "hello@acme.com" not in keys


def test_process_refreshes_stored_sender_before_checking_user_edits(setup, monkeypatch):
    ctx, gmail, account = setup
    gmail.emails = {"6": _group_email("6", "Pitch")}
    ctx._classifier.answers["Pitch"] = make_classification("cold_outreach", 0.99)
    # A row stored before authors were recovered: the group (on the allowlisted own domain).
    with monkeypatch.context() as m:
        m.setattr(triage, "signals_for", lambda email, account: email.signals)
        triage.run_account(ctx, account, Mode.LABEL)
    with ctx.sessions() as s:
        # corrections.check already looked at it this hour: process's own check catches it.
        s.query(Message).update({Message.correction_checked_at: now()})
        s.commit()
    gmail.emails["6"].label_ids = ["INBOX", "Label_Jev/Marketing", "Label_Jev/Processed"]
    triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as s:
        rule = s.query(SenderRule).filter_by(source="correction").one()
        assert (rule.sender_key, rule.category) == ("dana@pitchco.io", "marketing")


class _HttpError(Exception):
    """Shaped like googleapiclient.errors.HttpError: the status is on .resp."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = type("Resp", (), {"status": status})()


def _old_group_rows(ctx, gmail, account, monkeypatch, ids):
    gmail.emails = {i: _group_email(i, f"Pitch {i}") for i in ids}
    for i in ids:
        ctx._classifier.answers[f"Pitch {i}"] = make_classification("cold_outreach", 0.99)
    with monkeypatch.context() as m:
        m.setattr(triage, "signals_for", lambda email, account: email.signals)
        triage.run_account(ctx, account, Mode.LABEL)


def test_backfill_skips_only_deleted_emails(setup, monkeypatch):
    ctx, gmail, account = setup
    _old_group_rows(ctx, gmail, account, monkeypatch, ["6", "7"])
    gmail.header_errors["6"] = _HttpError(404)
    with ctx.sessions() as s:
        assert triage.backfill_senders(s, gmail, account) == (2, 1, 1)
        senders = {m.gmail_id: m.sender for m in s.query(Message).filter(Message.gmail_id >= "6")}
    assert senders == {"6": "hello@acme.com", "7": "dana@pitchco.io"}


@pytest.mark.parametrize("error", [_HttpError(401), _HttpError(429), _HttpError(503), OSError()])
def test_backfill_raises_other_gmail_errors(setup, monkeypatch, error):
    ctx, gmail, account = setup
    _old_group_rows(ctx, gmail, account, monkeypatch, ["6"])
    gmail.header_errors["6"] = error
    with ctx.sessions() as s, pytest.raises(type(error)):
        triage.backfill_senders(s, gmail, account)


def test_group_on_unlisted_public_provider_is_not_trusted(setup):
    # Without group_domains nothing is trusted, even the account's own (public) domain:
    # anyone at mac.com can pass DMARC and write the Google Groups headers.
    ctx, gmail, account = setup
    account.email, account.group_domains = "me@mac.com", []
    account.allow_senders = ["ceo@bank.com"]
    gmail.emails = {"6": _group_email("6", "Hi", author="ceo@bank.com", group="attacker@mac.com")}
    ctx._classifier.answers["Hi"] = make_classification("spam", 0.99)
    triage.run_account(ctx, account, Mode.ACT)
    assert "original_sender" not in ctx._classifier.states[0]  # not allowlisted: Jev decided
    with ctx.sessions() as s:
        assert s.query(Message).filter_by(gmail_id="6").one().sender == "attacker@mac.com"


def _failed_unsub(s, account, gmail_id, list_key, category, sender="bob@agency.io", **msg):
    fields = dict(source="jev", confidence=0.95, archived=True, labels_added=[])
    fields.update(msg)
    s.add(
        Message(
            account=account.email,
            gmail_id=gmail_id,
            thread_id="t" + gmail_id,
            sender=sender,
            category=category,
            mode="act",
            status="done",
            **fields,
        )
    )
    s.add(
        Unsubscribe(
            account=account.email,
            list_key=list_key,
            sender=sender,
            gmail_id=gmail_id,
            method="mailto",
            status="failed",
            attempts=1,
        )
    )
    s.commit()


def _archived(*emails):
    """What Gmail holds after an act-mode archive: out of the inbox."""
    for e in emails:
        e.label_ids = [i for i in e.label_ids if i != "INBOX"]
    return {e.id: e for e in emails}


def _retry(ctx, gmail, account):
    with ctx.sessions() as s:
        out = triage.retry_unsubscribes(ctx, s, gmail, account)
        rows = {r.list_key: (r.status, r.attempts) for r in s.query(Unsubscribe)}
    return dict(out), rows


def test_retry_unsubscribes_verified_stranger(setup):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = _archived(_pitch("9"))
    with ctx.sessions() as s:
        _failed_unsub(s, account, "9", "agency.io", "cold_outreach")
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": "ok"} and rows["agency.io"] == ("ok", 2)
    assert gmail.sent == ["u@agency.io"] and gmail.written_queries == ["bob@agency.io", "agency.io"]


@pytest.mark.parametrize(
    ("headers", "written", "reason"),
    [
        (
            {"list_post": "<mailto:g@groups.io>"},
            frozenset(),
            "unverified sender, group or own list",
        ),
        ({"unsubscribe": None}, frozenset(), "unverified sender, group or own list"),
        ({}, frozenset({"bob@agency.io"}), "known contact"),
        ({}, frozenset({"agency.io"}), "known contact"),
    ],
)
def test_retry_skips_what_run_would_spare(setup, headers, written, reason):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.written = written
    gmail.emails = _archived(_pitch("9", **headers))
    with ctx.sessions() as s:
        _failed_unsub(s, account, "9", "agency.io", "cold_outreach")
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": f"skipped: {reason}"}
    assert rows["agency.io"] == ("skipped", 1) and gmail.sent == []


def test_retry_skips_allowlisted_and_group_relayed(setup):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = _archived(
        email("7", "Boss <vip@partner.com>", "Promo", list_unsubscribe="<mailto:u@p.com>"),
        email(
            "8",
            "\"'Dana' via Hello\" <hello@acme.com>",
            "Pitch",
            x_original_sender="dana@pitchco.io",
            mailing_list="list hello@acme.com; contact hello+owners@acme.com",
            list_unsubscribe="<mailto:x@pitchco.io>",
        ),
    )
    with ctx.sessions() as s:
        _failed_unsub(s, account, "7", "partner.com", "marketing", sender="vip@partner.com")
        _failed_unsub(s, account, "8", "pitchco.io", "cold_outreach", sender="dana@pitchco.io")
    out, rows = _retry(ctx, gmail, account)
    assert out == {
        "partner.com": "skipped: allowlisted sender",
        "pitchco.io": "skipped: unverified sender, group or own list",
    }
    assert gmail.sent == []


@pytest.mark.parametrize(
    ("mode", "act_categories", "category", "held"),
    [
        (Mode.LABEL, None, "cold_outreach", "held: mode is label"),
        (
            Mode.ACT,
            ["marketing"],
            "cold_outreach",
            "held: category cold_outreach doesn't unsubscribe here",
        ),
        (Mode.ACT, None, "newsletter", "held: category newsletter doesn't unsubscribe here"),
    ],
)
def test_retry_holds_rows_run_would_not_act_on(setup, mode, act_categories, category, held):
    ctx, gmail, account = setup
    account.mode, account.act_categories = mode, act_categories
    gmail.emails = _archived(_pitch("9"))
    with ctx.sessions() as s:
        _failed_unsub(s, account, "9", "agency.io", category)
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": held} and rows["agency.io"] == ("failed", 1)
    assert gmail.sent == [] and gmail.written_queries == []


def test_retry_respects_run_cap(setup):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    ctx.settings.max_unsubscribes_per_run = 1
    gmail.emails = _archived(
        email("8", "A <a@one.io>", "P1", list_unsubscribe="<mailto:u@one.io>"),
        email("9", "B <b@two.io>", "P2", list_unsubscribe="<mailto:u@two.io>"),
    )
    with ctx.sessions() as s:
        _failed_unsub(s, account, "8", "one.io", "marketing", sender="a@one.io")
        _failed_unsub(s, account, "9", "two.io", "marketing", sender="b@two.io")
    out, _ = _retry(ctx, gmail, account)
    assert sorted(out.values()) == ["held: run cap reached", "ok"] and len(gmail.sent) == 1


class _Http404(Exception):
    resp = type("R", (), {"status": 404})()


def test_retry_holds_deleted_email_and_raises_other_errors(setup, monkeypatch):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    with ctx.sessions() as s:
        _failed_unsub(s, account, "9", "agency.io", "cold_outreach")

    def gone(message_id):
        raise _Http404()

    monkeypatch.setattr(gmail, "get", gone)
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": "held: email deleted in Gmail"} and rows["agency.io"] == (
        "failed",
        1,
    )

    def boom(message_id):
        raise RuntimeError("quota")

    monkeypatch.setattr(gmail, "get", boom)
    with pytest.raises(RuntimeError):
        _retry(ctx, gmail, account)


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"restored": True}, "restored by the user"),
        ({"corrected_category": "important"}, "corrected to important"),
    ],
)
def test_retry_skips_user_decisions_before_gmail(setup, monkeypatch, fields, reason):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    with ctx.sessions() as s:
        _failed_unsub(s, account, "9", "agency.io", "cold_outreach", **fields)
    monkeypatch.setattr(gmail, "get", lambda message_id: pytest.fail("no Gmail call expected"))
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": f"skipped: {reason}"} and rows["agency.io"] == ("skipped", 1)
    assert gmail.sent == []


@pytest.mark.parametrize("source", ["manual", "correction"])
def test_retry_follows_current_sender_rule(setup, source):
    # The user (or a correction) has since said Bob's mail is important: run would keep it.
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = _archived(_pitch("9"))
    with ctx.sessions() as s:
        _failed_unsub(s, account, "9", "agency.io", "cold_outreach")
        rules.set_rule(s, account.email, "@agency.io", "important", source=source)
        s.commit()
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": "skipped: sender rule says important"}
    assert rows["agency.io"] == ("skipped", 1) and gmail.sent == []


def test_retry_rule_to_unsubscribing_category_skips_confidence_check(setup):
    # As in run, a rule decides with confidence 1.0: Jev's stored 0.5 no longer matters.
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = _archived(_pitch("9"))
    with ctx.sessions() as s:
        _failed_unsub(s, account, "9", "agency.io", "cold_outreach", confidence=0.5)
        rules.set_rule(s, account.email, "bob@agency.io", "marketing", source="manual")
        s.commit()
    out, _ = _retry(ctx, gmail, account)
    assert out == {"agency.io": "ok"} and gmail.sent == ["u@agency.io"]


def test_retry_skips_email_the_user_touched_in_gmail(setup):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    label_ids = gmail.ensure_labels(ctx.cfg.all_labels())
    pulled_back = _pitch("7")  # still has INBOX: dragged back after the archive
    starred = _archived(_pitch("8"))["8"]
    starred.label_ids.append("STARRED")
    relabeled = _archived(_pitch("9"))["9"]
    relabeled.label_ids.append(label_ids[ctx.cfg.by_key["newsletter"].label])
    gmail.emails = {e.id: e for e in (pulled_back, starred, relabeled)}
    with ctx.sessions() as s:
        _failed_unsub(s, account, "7", "a.io", "cold_outreach")
        _failed_unsub(s, account, "8", "b.io", "cold_outreach")
        _failed_unsub(s, account, "9", "c.io", "cold_outreach")
    out, rows = _retry(ctx, gmail, account)
    assert out == {
        "a.io": "skipped: corrected in Gmail to important",
        "b.io": "skipped: starred",
        "c.io": "skipped: corrected in Gmail to newsletter",
    }
    assert {r[0] for r in rows.values()} == {"skipped"} and gmail.sent == []


@pytest.mark.parametrize(
    ("live", "outcome"),
    [
        (["Jev/Outreach", "Jev/Processed"], "ok"),  # untouched since the archive
        (
            ["Jev/Outreach", "Jev/Processed", "Jev/Important"],
            "skipped: label added in Gmail: Jev/Important",
        ),
        (["Jev/Processed"], "skipped: label removed in Gmail: Jev/Outreach"),
        (["Jev/Outreach", "Jev/Processed", "SPAM"], "skipped: reported as spam"),
        (["Jev/Outreach", "Jev/Processed", "TRASH"], "skipped: in the trash"),
    ],
)
def test_retry_skips_edits_run_leaves_alone(setup, live, outcome):
    # Two category labels or none: corrections.detect sees nothing, _user_edit does.
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    label_ids = gmail.ensure_labels(ctx.cfg.all_labels())
    pitch = _archived(_pitch("9"))["9"]
    pitch.label_ids += [label_ids.get(n, n) for n in live]
    gmail.emails = {"9": pitch}
    with ctx.sessions() as s:
        _failed_unsub(
            s,
            account,
            "9",
            "agency.io",
            "cold_outreach",
            labels_added=["Jev/Outreach", "Jev/Processed"],
        )
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": outcome}
    if outcome == "ok":
        assert gmail.sent == ["u@agency.io"]
    else:
        assert rows["agency.io"] == ("skipped", 1) and gmail.sent == []


@pytest.mark.parametrize("source", ["rule", None])
def test_retry_holds_rule_decision_whose_rule_was_removed(setup, source):
    # `rules delete` means "Jev decides again": the deleted rule's 1.0 must not unsubscribe.
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = _archived(_pitch("9"))
    with ctx.sessions() as s:
        _failed_unsub(s, account, "9", "agency.io", "cold_outreach", source=source, confidence=1.0)
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": "held: rule since removed"} and rows["agency.io"] == ("failed", 1)
    assert gmail.sent == [] and gmail.written_queries == []


@pytest.mark.parametrize(
    ("fields", "override"),
    [
        ({"confidence": 0.85}, {}),  # below categories.yaml 0.90
        ({"confidence": 0.92}, {"cold_outreach": 0.95}),  # account raised the bar since
        ({"confidence": 0.97, "low_confidence": True}, {}),
        ({"confidence": None}, {}),
    ],
)
def test_retry_holds_jev_answers_below_current_threshold(setup, fields, override):
    ctx, gmail, account = setup
    account.mode, account.min_confidence = Mode.ACT, override
    gmail.emails = _archived(_pitch("9"))
    with ctx.sessions() as s:
        _failed_unsub(s, account, "9", "agency.io", "cold_outreach", **fields)
    out, rows = _retry(ctx, gmail, account)
    assert out["agency.io"].startswith("held: confidence")
    assert rows["agency.io"] == ("failed", 1) and gmail.sent == []


def test_retry_holds_when_sent_mail_search_fails(setup, monkeypatch):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = _archived(_pitch("9"))
    with ctx.sessions() as s:
        _failed_unsub(s, account, "9", "agency.io", "cold_outreach")

    def broken(query):
        raise RuntimeError("search failed")

    monkeypatch.setattr(gmail, "has_written_to", broken)
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": "held: sent-mail search failed"}
    assert rows["agency.io"] == ("failed", 1) and gmail.sent == []


def test_retry_holds_row_without_stored_message(setup):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    with ctx.sessions() as s:
        _failed_unsub(s, account, "9", "agency.io", "cold_outreach")
        s.query(Message).filter_by(gmail_id="9").delete()
        s.commit()
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": "held: no stored message"} and rows["agency.io"] == ("failed", 1)
