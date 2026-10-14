"""`inbox-jev restore` (triage.restore): undo label/act passes, with fake Gmail and Jev."""

from datetime import timedelta

import pytest
from conftest import make_classification
from fakes import FakeGmail, HttpError, email
from typer.testing import CliRunner

from inbox_jev import cli, triage
from inbox_jev.config import Mode
from inbox_jev.db import Message, SenderRule, Unsubscribe

DAY = timedelta(days=1)


def _restore(ctx, gmail, account, since=DAY, category=None):
    with ctx.sessions() as s:
        return triage.restore(s, gmail, account.email, since, category)


def _rows(ctx):
    with ctx.sessions() as s:
        return {m.gmail_id: (m.restored, m.archived, m.spammed) for m in s.query(Message)}


def _jev_labels(gmail, message_id):
    return [i for i in gmail.emails[message_id].label_ids if i.startswith("Label_")]


@pytest.fixture
def acted(setup):
    """setup's four emails after an act pass, plus a scam moved to Spam."""
    ctx, gmail, account = setup
    gmail.emails["5"] = email("5", "Prize <win@lottery.biz>", "You won")
    ctx.classifier.answers["You won"] = make_classification("spam", 0.99)
    triage.run_account(ctx, account, Mode.ACT)
    assert "INBOX" not in gmail.emails["2"].label_ids  # archived marketing
    assert "SPAM" in gmail.emails["5"].label_ids
    return ctx, gmail, account


def test_restore_puts_archived_and_spammed_back_and_removes_our_labels(acted):
    ctx, gmail, account = acted
    assert _restore(ctx, gmail, account).restored == 5
    for message_id in gmail.emails:
        assert "INBOX" in gmail.emails[message_id].label_ids
        assert _jev_labels(gmail, message_id) == []  # incl. Jev/Processed and Jev/Review
    assert "SPAM" not in gmail.emails["5"].label_ids
    assert all(restored for restored, _, _ in _rows(ctx).values())
    # The archive/spam columns keep what the pass did: restore only adds the flag.
    assert _rows(ctx)["2"] == (True, True, False) and _rows(ctx)["5"] == (True, False, True)


def test_restore_never_adds_inbox_to_an_email_that_stayed_there(acted):
    ctx, gmail, account = acted
    _restore(ctx, gmail, account)
    adds = {message_id: add for message_id, add, _ in gmail.modified[-5:]}
    assert adds == {"1": [], "2": ["INBOX"], "3": [], "4": [], "5": ["INBOX"]}


def test_restore_cannot_undo_an_unsubscribe(acted):
    ctx, gmail, account = acted
    _restore(ctx, gmail, account)
    with ctx.sessions() as s:
        assert [(u.list_key, u.status) for u in s.query(Unsubscribe)] == [("shop.com", "ok")]


def test_restore_only_the_given_category(acted):
    ctx, gmail, account = acted
    # "2" archived as marketing, "4" left in Review with the marketing suggestion.
    assert _restore(ctx, gmail, account, category="marketing").restored == 2
    rows = _rows(ctx)
    assert rows["2"][0] and rows["4"][0] and not any(rows[i][0] for i in ("1", "3", "5"))
    assert "SPAM" in gmail.emails["5"].label_ids and _jev_labels(gmail, "1") != []


def test_restore_only_rows_processed_inside_the_window(acted):
    ctx, gmail, account = acted
    with ctx.sessions() as s:
        old = s.query(Message).filter_by(gmail_id="2").one()
        old.processed_at -= timedelta(days=3)
        s.commit()
    assert _restore(ctx, gmail, account, since=timedelta(days=2)).restored == 4
    assert not _rows(ctx)["2"][0] and "INBOX" not in gmail.emails["2"].label_ids
    assert _restore(ctx, gmail, account, since=timedelta(days=4)).restored == 1


def test_restore_is_idempotent(acted):
    ctx, gmail, account = acted
    assert _restore(ctx, gmail, account).restored == 5
    calls = len(gmail.modified)
    assert _restore(ctx, gmail, account).restored == 0
    assert len(gmail.modified) == calls


def test_restore_skips_dry_run_rows(setup):
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.DRY_RUN)
    calls = len(gmail.modified)
    assert _restore(ctx, gmail, account).restored == 0
    assert len(gmail.modified) == calls  # dry_run changed nothing in Gmail: nothing to undo
    assert not any(restored for restored, _, _ in _rows(ctx).values())


def test_restore_only_touches_the_given_account(acted):
    ctx, gmail, account = acted
    with ctx.sessions() as s:
        assert triage.restore(s, gmail, "other@acme.com", DAY).restored == 0
    assert not any(restored for restored, _, _ in _rows(ctx).values())


def test_restored_emails_are_not_reprocessed_or_learned_as_corrections(acted):
    # Back in the inbox without our labels looks like a correction to `important`; restore
    # means "undo", not "this sender is important".
    ctx, gmail, account = acted
    _restore(ctx, gmail, account)
    with ctx.sessions() as s:
        s.query(Message).update({Message.correction_checked_at: None})
        s.commit()
    calls, jev_calls = len(gmail.modified), ctx.classifier.calls
    assert triage.run_account(ctx, account, Mode.ACT).processed == 0
    assert (len(gmail.modified), ctx.classifier.calls) == (calls, jev_calls)
    with ctx.sessions() as s:
        assert s.query(SenderRule).filter_by(source="correction").count() == 0


def test_cli_restore_is_a_thin_wrapper(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'db.sqlite'}")
    monkeypatch.setattr(cli.logs, "setup", lambda level: None)
    gmail = FakeGmail([])
    monkeypatch.setattr(cli.gmail_mod, "connect", lambda d, e: gmail)
    seen = []

    def restore(session, gmail, account, since, category):
        seen.append((gmail, account, since, category))
        return triage.RestoreResult(restored=3)

    monkeypatch.setattr(triage, "restore", restore)
    args = ["restore", "-a", "me@acme.com", "--since", "12h", "--category", "marketing"]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert seen == [(gmail, "me@acme.com", timedelta(hours=12), "marketing")]
    assert gmail.closes == 1  # closed once the restore is done
    assert "Restored 3 emails. Unsubscribes cannot be undone." in result.output

    bad = CliRunner().invoke(cli.app, ["restore", "-a", "me@acme.com", "--since", "2w"])
    assert bad.exit_code != 0 and len(seen) == 1


def _fail_modify(monkeypatch, gmail, failing):
    """Make gmail.modify raise `failing[message_id]` for those emails."""
    real = gmail.modify

    def modify(message_id, add, remove):
        if message_id in failing:
            raise failing[message_id]
        real(message_id, add, remove)

    monkeypatch.setattr(gmail, "modify", modify)


def test_restore_skips_emails_deleted_in_gmail(acted, monkeypatch):
    ctx, gmail, account = acted
    _fail_modify(monkeypatch, gmail, {"2": HttpError(404)})
    assert _restore(ctx, gmail, account) == triage.RestoreResult(restored=4, deleted=1)
    rows = _rows(ctx)
    assert rows["2"] == (False, True, False)  # left as it was
    assert all(rows[i][0] for i in ("1", "3", "4", "5"))
    # Again: the deleted one is skipped again, nothing else is touched.
    assert _restore(ctx, gmail, account) == triage.RestoreResult(restored=0, deleted=1)


@pytest.mark.parametrize("error", [HttpError(401), HttpError(429), HttpError(503), OSError()])
def test_restore_raises_other_gmail_errors(acted, monkeypatch, error):
    ctx, gmail, account = acted
    _fail_modify(monkeypatch, gmail, {"2": error})
    with pytest.raises(type(error)):
        _restore(ctx, gmail, account)
    assert not _rows(ctx)["2"][0]


def test_cli_restore_reports_deleted_emails(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'db.sqlite'}")
    monkeypatch.setattr(cli.logs, "setup", lambda level: None)
    monkeypatch.setattr(cli.gmail_mod, "connect", lambda d, e: FakeGmail([]))
    monkeypatch.setattr(triage, "restore", lambda *a: triage.RestoreResult(restored=2, deleted=1))
    result = CliRunner().invoke(cli.app, ["restore", "-a", "me@acme.com"])
    assert result.exit_code == 0, result.output
    assert "Restored 2 emails. Skipped 1 deleted in Gmail. Unsubscribes" in result.output
