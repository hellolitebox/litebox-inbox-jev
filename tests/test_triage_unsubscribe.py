"""`unsubscribe-retry` (`unsubscribe_retry.retry_unsubscribes`): the checks `run` applies, again."""

import pytest
from fakes import (
    SENT_BASE_S,
    HttpError,
    SentMailService,
    as_archived,
    email,
    failed_unsub,
    pitch_email,
    sent_mail_gmail,
)
from sqlalchemy import update
from sqlalchemy.exc import OperationalError

from inbox_jev import db, rules, scan_rows, triage, unsubscribe_retry
from inbox_jev.classification import PreviousContact
from inbox_jev.config import Mode
from inbox_jev.db import Message, SentDomainScan, Unsubscribe
from inbox_jev.policy import DecisionSource
from inbox_jev.rules import RuleSource
from inbox_jev.sent_scan import DomainScan, scan_rules


def _retry(ctx, gmail, account):
    with ctx.sessions() as s:
        out = unsubscribe_retry.retry_unsubscribes(ctx, s, gmail, account)
        rows = {r.list_key: (r.status, r.attempts) for r in s.query(Unsubscribe)}
    return dict(out), rows


def test_retry_unsubscribes_verified_stranger(setup):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = as_archived(pitch_email("9"))
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach")
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
    gmail.emails = as_archived(pitch_email("9", **headers))
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach")
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": f"skipped: {reason}"}
    assert rows["agency.io"] == ("skipped", 1) and gmail.sent == []


def test_retry_skips_allowlisted_and_group_relayed(setup):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = as_archived(
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
        failed_unsub(s, account, "7", "partner.com", "marketing", sender="vip@partner.com")
        failed_unsub(s, account, "8", "pitchco.io", "cold_outreach", sender="dana@pitchco.io")
    out, _rows = _retry(ctx, gmail, account)
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
    gmail.emails = as_archived(pitch_email("9"))
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", category)
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": held} and rows["agency.io"] == ("failed", 1)
    assert gmail.sent == [] and gmail.written_queries == []


def test_retry_respects_run_cap(setup):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    ctx.settings.max_unsubscribes_per_run = 1
    gmail.emails = as_archived(
        email("8", "A <a@one.io>", "P1", list_unsubscribe="<mailto:u@one.io>"),
        email("9", "B <b@two.io>", "P2", list_unsubscribe="<mailto:u@two.io>"),
    )
    with ctx.sessions() as s:
        failed_unsub(s, account, "8", "one.io", "marketing", sender="a@one.io")
        failed_unsub(s, account, "9", "two.io", "marketing", sender="b@two.io")
    out, _ = _retry(ctx, gmail, account)
    assert sorted(out.values()) == ["held: run cap reached", "ok"] and len(gmail.sent) == 1


def test_retry_holds_deleted_email_and_raises_other_errors(setup, monkeypatch):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach")

    def gone(message_id):
        raise HttpError(404)

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
        failed_unsub(s, account, "9", "agency.io", "cold_outreach", **fields)
    monkeypatch.setattr(gmail, "get", lambda message_id: pytest.fail("no Gmail call expected"))
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": f"skipped: {reason}"} and rows["agency.io"] == ("skipped", 1)
    assert gmail.sent == []


@pytest.mark.parametrize("source", [RuleSource.MANUAL, RuleSource.CORRECTION])
def test_retry_follows_current_sender_rule(setup, source):
    # The user (or a correction) has since said Bob's mail is important: run would keep it.
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = as_archived(pitch_email("9"))
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach")
        rules.set_rule(s, account.email, "@agency.io", "important", source=source)
        s.commit()
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": "skipped: sender rule says important"}
    assert rows["agency.io"] == ("skipped", 1) and gmail.sent == []


@pytest.mark.parametrize("source", [RuleSource.AUTO, RuleSource.CORRECTION])
def test_retry_ignores_a_learned_rule_on_a_list_address(setup, source):
    # Mail sent as the list's own address: a learned rule on that address doesn't decide it
    # (ADR 0051), so the retry reaches the gate, which vetoes the list as a group.
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    address = "dev@lists.example.org"
    gmail.emails = as_archived(
        email(
            "9",
            f"Dev <{address}>",
            "Pitch 9",
            list_post=f"<mailto:{address}>",
            list_unsubscribe="<mailto:u@lists.example.org>",
        )
    )
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "lists.example.org", "cold_outreach", sender=address)
        rules.set_rule(s, account.email, address, "important", source=source)
        s.commit()
    out, rows = _retry(ctx, gmail, account)
    assert out == {"lists.example.org": "skipped: unverified sender, group or own list"}
    assert rows["lists.example.org"] == ("skipped", 1) and gmail.sent == []


def test_retry_rule_to_unsubscribing_category_skips_confidence_check(setup):
    # As in run, a rule decides with confidence 1.0: Jev's stored 0.5 no longer matters.
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = as_archived(pitch_email("9"))
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach", confidence=0.5)
        rules.set_rule(s, account.email, "bob@agency.io", "marketing", source=RuleSource.MANUAL)
        s.commit()
    out, _ = _retry(ctx, gmail, account)
    assert out == {"agency.io": "ok"} and gmail.sent == ["u@agency.io"]


def test_retry_skips_email_the_user_touched_in_gmail(setup):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    label_ids = gmail.ensure_labels(ctx.cfg.all_labels())
    pulled_back = pitch_email("7")  # still has INBOX: dragged back after the archive
    starred = as_archived(pitch_email("8"))["8"]
    starred.label_ids.append("STARRED")
    relabeled = as_archived(pitch_email("9"))["9"]
    relabeled.label_ids.append(label_ids[ctx.cfg.by_key["newsletter"].label])
    gmail.emails = {e.id: e for e in (pulled_back, starred, relabeled)}
    with ctx.sessions() as s:
        failed_unsub(s, account, "7", "a.io", "cold_outreach")
        failed_unsub(s, account, "8", "b.io", "cold_outreach")
        failed_unsub(s, account, "9", "c.io", "cold_outreach")
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
    # Two category labels or none: corrections.detect sees nothing, user_edit does.
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    label_ids = gmail.ensure_labels(ctx.cfg.all_labels())
    pitch = as_archived(pitch_email("9"))["9"]
    pitch.label_ids += [label_ids.get(n, n) for n in live]
    gmail.emails = {"9": pitch}
    with ctx.sessions() as s:
        failed_unsub(
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


@pytest.mark.parametrize("source", [DecisionSource.RULE, None])
def test_retry_holds_rule_decision_whose_rule_was_removed(setup, source):
    # `rules delete` means "Jev decides again": the deleted rule's 1.0 must not unsubscribe.
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = as_archived(pitch_email("9"))
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach", source=source, confidence=1.0)
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
    gmail.emails = as_archived(pitch_email("9"))
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach", **fields)
    out, rows = _retry(ctx, gmail, account)
    assert out["agency.io"].startswith("held: confidence")
    assert rows["agency.io"] == ("failed", 1) and gmail.sent == []


def test_retry_holds_when_sent_mail_search_fails(setup, monkeypatch):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = as_archived(pitch_email("9"))
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach")

    def broken(query):
        raise RuntimeError("search failed")

    monkeypatch.setattr(gmail, "has_written_to", broken)
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": "held: sent-mail search failed"}
    assert rows["agency.io"] == ("failed", 1) and gmail.sent == []


def _scan_row(ctx):
    with ctx.sessions() as s:
        row = s.query(SentDomainScan).one()
        return row.domain, row.verdict, row.read


# Sent mail to agency.io only at machine addresses: the domain scan decides the retry.
AGENCY_BOUNCES = {f"s{i}": {"To": f"bounces+{i}@agency.io"} for i in range(30)}


def test_retry_stores_the_domain_scan_of_a_row_it_unsubscribes(setup, monkeypatch):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = as_archived(pitch_email("9"))
    svc = SentMailService(dict(list(AGENCY_BOUNCES.items())[:3]))
    gmail.has_written_to = sent_mail_gmail(monkeypatch, svc).has_written_to
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach")
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": "ok"} and rows["agency.io"] == ("ok", 2)
    assert _scan_row(ctx) == ("agency.io", "never", 3)


@pytest.mark.parametrize(
    ("errors", "outcome", "status", "scan"),
    [
        # A Gmail error after 3 reads: held, and those 3 are kept for the next check.
        ({"s3": HttpError(500)}, "held: sent-mail search failed", "failed", "scanning"),
        # 30 matches: 25 read, not finished, so a known contact for now.
        ({}, "skipped: known contact", "skipped", "scanning"),
    ],
)
def test_retry_stores_the_domain_scan_of_a_row_it_holds_or_skips(
    setup, monkeypatch, errors, outcome, status, scan
):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = as_archived(pitch_email("9"))
    svc = SentMailService(dict(AGENCY_BOUNCES), get_errors=errors)
    gmail.has_written_to = sent_mail_gmail(monkeypatch, svc).has_written_to
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach")
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": outcome} and rows["agency.io"] == (status, 1)
    assert gmail.sent == []
    assert _scan_row(ctx) == ("agency.io", scan, 3 if errors else 25)


def test_retry_unsubscribes_when_run_stored_the_same_scan_meanwhile(setup, monkeypatch):
    # `run` checks the same new domain and commits its scan right after the retry saved its own
    # (both found no row): the retry still records its attempt. Before, the retry's insert was
    # only flushed by the commit after the unsubscribe, which then failed on the unique
    # constraint: the attempt was lost and the next retry unsubscribed again.
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = as_archived(pitch_email("9"))
    svc = SentMailService(dict(list(AGENCY_BOUNCES.items())[:3]))
    gmail.has_written_to = sent_mail_gmail(monkeypatch, svc).has_written_to
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach")
    save = scan_rows.DomainScanRows.save

    def save_then_run_saves(self, scan):
        save(self, scan)
        with ctx.sessions() as other:
            run_scan = DomainScan(scan.domain, scan_rules(), newest_s=SENT_BASE_S, read=1)
            save(scan_rows.DomainScanRows(other, account.email), run_scan)
            other.commit()

    monkeypatch.setattr(scan_rows.DomainScanRows, "save", save_then_run_saves)
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": "ok"} and rows["agency.io"] == ("ok", 2)
    assert gmail.sent == ["u@agency.io"]
    assert _scan_row(ctx) == ("agency.io", "scanning", 1)  # the last scan saved, run's


def test_retry_stops_before_unsubscribing_when_the_scan_cant_be_saved(setup, monkeypatch):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = as_archived(pitch_email("9"))
    svc = SentMailService(dict(list(AGENCY_BOUNCES.items())[:3]))
    gmail.has_written_to = sent_mail_gmail(monkeypatch, svc).has_written_to

    def locked(self, scan):
        raise OperationalError("INSERT INTO sent_domain_scans", {}, Exception("database is locked"))

    monkeypatch.setattr(scan_rows.DomainScanRows, "save", locked)
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach")
    with pytest.raises(OperationalError), ctx.sessions() as s:
        unsubscribe_retry.retry_unsubscribes(ctx, s, gmail, account)
    assert gmail.sent == []
    with ctx.sessions() as s:
        assert (s.query(Unsubscribe).one().status, s.query(Unsubscribe).one().attempts) == (
            "failed",
            1,
        )


def test_retry_stops_before_unsubscribing_while_another_process_writes(
    setup, monkeypatch, make_sessions, tmp_path
):
    # The timer's `run` holds SQLite's write lock (it saved a scan before asking Jev) while a
    # manual retry checks a `never` domain with nothing new: the retry has nothing of its own to
    # save, yet must take the lock before the request, not find it held when storing the attempt.
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    monkeypatch.setattr(db, "SQLITE_BUSY_TIMEOUT_MS", 50)
    ctx.sessions = make_sessions(f"sqlite:///{tmp_path / 'locked.db'}")
    gmail.emails = as_archived(pitch_email("9"))
    svc = SentMailService(dict(list(AGENCY_BOUNCES.items())[:3]))
    gmail.has_written_to = sent_mail_gmail(monkeypatch, svc).has_written_to
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach")
        rows = scan_rows.DomainScanRows(s, account.email)
        assert (
            triage.previous_contact(gmail, account.email, "bob@agency.io", rows)
            == PreviousContact.NEVER
        )
        s.commit()
    with ctx.sessions() as run_session:
        # `run`'s write, not committed yet.
        run_session.execute(update(Message).where(Message.gmail_id == "9").values(subject="x"))
        with pytest.raises(OperationalError, match="locked"), ctx.sessions() as s:
            unsubscribe_retry.retry_unsubscribes(ctx, s, gmail, account)
        run_session.rollback()
    assert gmail.sent == []
    with ctx.sessions() as s:
        row = s.query(Unsubscribe).one()
        assert (row.status, row.attempts) == ("failed", 1)
        assert _scan_row(ctx) == ("agency.io", "never", 3)


def test_retry_fails_the_account_before_gmail_on_a_database_without_the_upsert(setup, monkeypatch):
    # Like `run`: the account fails before any email is read in Gmail, not at the first row
    # that needs the sent-mail search (rows before it would have been unsubscribed).
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = as_archived(pitch_email("9"))
    gets = []
    monkeypatch.setattr(gmail, "get", lambda message_id: gets.append(message_id))
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach")
    ctx.scan_dialects = ("postgresql",)  # stands for a database without the upsert
    with pytest.raises(NotImplementedError, match="sqlite"), ctx.sessions() as s:
        unsubscribe_retry.retry_unsubscribes(ctx, s, gmail, account)
    assert gets == [] and gmail.sent == [] and gmail.written_queries == []
    with ctx.sessions() as s:
        row = s.query(Unsubscribe).one()
        assert (row.status, row.attempts) == ("failed", 1)


def test_retry_holds_row_without_stored_message(setup):
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    with ctx.sessions() as s:
        failed_unsub(s, account, "9", "agency.io", "cold_outreach")
        s.query(Message).filter_by(gmail_id="9").delete()
        s.commit()
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": "held: no stored message"} and rows["agency.io"] == ("failed", 1)


def test_retry_holds_rule_decision_whose_exact_rule_went_stale(setup):
    # The @agency.io rule would unsubscribe, but bob's own (now stale) rule overrides it.
    ctx, gmail, account = setup
    account.mode = Mode.ACT
    gmail.emails = as_archived(pitch_email("9"))
    with ctx.sessions() as s:
        rules.set_rule(s, account.email, "bob@agency.io", "promotions", RuleSource.MANUAL)
        rules.set_rule(s, account.email, "@agency.io", "cold_outreach", RuleSource.MANUAL)
        failed_unsub(
            s, account, "9", "agency.io", "cold_outreach", source=DecisionSource.RULE, confidence=1
        )
    out, rows = _retry(ctx, gmail, account)
    assert out == {"agency.io": "held: rule since removed"} and rows["agency.io"] == ("failed", 1)
    assert gmail.sent == []
