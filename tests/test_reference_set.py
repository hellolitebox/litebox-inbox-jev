"""scripts/dry_classify.py --reference: which stored emails make the reference set and what
their truth is, the summary's accuracy, confusion and borderline-band math, and a check that asks
Jev despite the correction rules, drops accepted emails the user edited since, and prints no
From or Subject with `quiet`."""

from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import make_classification
from dry_classify import (
    Checker,
    Outcome,
    ReferenceSummary,
    TruthKind,
    select_reference,
)
from fakes import FakeClassifier, FakeGmail, HttpError, email, jev_http_error
from typesafe_sdk import TypeSafeUnprocessableEntityError

from inbox_jev import rules, triage
from inbox_jev.config import Mode
from inbox_jev.db import Message, MessageStatus, SenderRule, now
from inbox_jev.gmail import Email
from inbox_jev.policy import DecisionSource
from inbox_jev.rules import RuleSource

C, A = TruthKind.CORRECTED, TruthKind.ACCEPTED


def _row(gmail_id: str, *, days_ago: float = 1, **fields) -> Message:
    base = {
        "account": "me@acme.com",
        "gmail_id": gmail_id,
        "thread_id": "t" + gmail_id,
        "sender": f"s{gmail_id}@other.org",
        "category": "notification",
        "confidence": 0.9,
        "source": DecisionSource.JEV,
        "mode": Mode.LABEL,
        "status": MessageStatus.DONE,
        "labels_added": ["Jev/Processed", "Jev/Notifications"],
        "processed_at": now() - timedelta(days=days_ago),
    }
    return Message(**(base | fields))


@pytest.fixture
def sessions(make_sessions):
    return make_sessions("sqlite:///:memory:")


def _store(sessions, *rows: Message) -> None:
    with sessions() as session:
        session.add_all(rows)
        session.commit()


def _select(sessions, cfg, **kw) -> tuple[list[tuple[str, TruthKind, str, str | None]], int]:
    with sessions() as session:
        sel = select_reference(session, cfg, "me@acme.com", **({"limit": 30} | kw))
        picked = [(r.row.gmail_id, r.kind, r.truth, r.stored) for r in sel.emails]
        return picked, sel.unknown_category


def test_corrections_are_the_reference_with_the_stored_decision_as_baseline(sessions, cfg):
    _store(
        sessions,
        _row("fixed", corrected_category="important", days_ago=2),
        _row("pulled", corrected_category="important", archived=True, restored=True, days_ago=3),
        _row("plain", days_ago=20),  # no correction: only with --accepted
        _row("failed", corrected_category="marketing", status=MessageStatus.ERROR),
        _row("gone_cat", corrected_category="recruiting"),  # removed from categories.yaml
        _row("other", corrected_category="important", account="you@acme.com"),
        # Redone after the correction: the correction rule's answer overwrote Jev's.
        _row(
            "redone",
            corrected_category="important",
            category="important",
            source="rule",
            days_ago=0.5,
        ),
        _row("allowed", corrected_category="marketing", source="allowlist", days_ago=5),
        _row(
            "old_rule",
            corrected_category="marketing",
            category="recruiting",
            source="rule",
            days_ago=6,
        ),
    )
    picked, unknown = _select(sessions, cfg)
    assert picked == [
        ("redone", C, "important", None),
        ("fixed", C, "important", "notification"),
        ("pulled", C, "important", "notification"),  # a restore doesn't undo the correction
        ("allowed", C, "marketing", None),
        ("old_rule", C, "marketing", None),  # the unknown baseline's category doesn't matter
    ]
    assert unknown == 1


def test_accepted_is_opt_in_and_only_untouched_label_or_act_rows_old_enough(sessions, cfg):
    _store(
        sessions,
        _row("old", days_ago=10),
        _row("act", days_ago=12, mode=Mode.ACT, category="newsletter", archived=True),
        _row("recent", days_ago=2),  # not yet "left untouched" long enough
        _row("review", days_ago=10, low_confidence=True),  # a suggestion, never confirmed
        _row("dry", days_ago=10, mode=Mode.DRY_RUN),  # the user never saw its labels
        _row("restored", days_ago=10, restored=True),
        _row("corrected", days_ago=10, corrected_category="important"),
    )
    assert _select(sessions, cfg)[0] == [("corrected", C, "important", "notification")]
    picked, _ = _select(sessions, cfg, accepted_after=timedelta(days=7))
    assert picked == [
        ("corrected", C, "important", "notification"),
        ("old", A, "notification", "notification"),
        ("act", A, "newsletter", "newsletter"),
    ]


def test_limit_applies_to_each_kind_newest_first_and_since_bounds_both(sessions, cfg):
    _store(
        sessions,
        *(_row(f"c{d}", days_ago=d, corrected_category="important") for d in (1, 5, 40)),
        *(_row(f"a{d}", days_ago=d) for d in (10, 20, 50)),
    )
    picked, _ = _select(sessions, cfg, limit=2, accepted_after=timedelta(days=7))
    assert [p[0] for p in picked] == ["c1", "c5", "a10", "a20"]
    picked, _ = _select(sessions, cfg, since=timedelta(days=30), accepted_after=timedelta(days=7))
    assert [p[0] for p in picked] == ["c1", "c5", "a10", "a20"]


def test_summary_scores_branch_and_stored_decision_against_the_truth():
    outcomes = [
        Outcome(C, "important", "notification", "important", margin=0.3),  # fixed
        Outcome(C, "important", "notification", "notification", margin=0.03),  # both wrong
        Outcome(C, "marketing", "marketing", "newsletter", margin=0.05),  # broken (edge)
        Outcome(C, "important", "newsletter", "important"),  # fixed, Jev's guard (no margin)
        Outcome(C, "important", None, "marketing", margin=0.2),  # baseline unknown: branch only
        Outcome(C, "marketing", None, "marketing", margin=0.4),  # baseline unknown, branch ok
        Outcome(A, "notification", "notification", "notification", margin=0.08),
        Outcome(A, "newsletter", "newsletter", "marketing", margin=0.12),
    ]
    summary = ReferenceSummary(outcomes, jev_calls=9, failed=1, gone=2, edited=3)
    assert summary.lines() == [
        "reference: emails=8 corrected=6 accepted=2 jev_calls=9 failed=1 gone=2 edited=3"
        " unknown_category=0",
        "corrected: branch=3/6 (50.0%) stored=1/4 (25.0%) fixed=2 broken=1 baseline_unknown=2",
        "  corrected important -> notification: branch=1 stored=2",
        "  corrected important -> marketing: branch=1 stored=0",
        "  corrected important -> newsletter: branch=0 stored=1",
        "  corrected marketing -> newsletter: branch=1 stored=0",
        "accepted: branch=1/2 (50.0%) (the stored decision is the truth)",
        "  accepted newsletter -> marketing: branch=1 stored=0",
        "borderline (Jev's category vs its threshold): within_0.05=2 within_0.10=3"
        " of jev_decided=7",
    ]


def test_summary_of_an_empty_reference_set():
    assert ReferenceSummary([], jev_calls=0, unknown_category=2).lines() == [
        "reference: emails=0 corrected=0 accepted=0 jev_calls=0 failed=0 gone=0 edited=0"
        " unknown_category=2",
        "borderline (Jev's category vs its threshold): within_0.05=0 within_0.10=0"
        " of jev_decided=0",
    ]


def test_the_band_edge_counts_despite_float_rounding():
    margin = abs(0.75 - 0.80)  # 0.05000000000000004
    assert ReferenceSummary([Outcome(C, "x", "x", "x", margin)], 0).within(0.05) == 1


class _FailingGmail(FakeGmail):
    """`FakeGmail` whose `get` raises `errors[id]` (HttpError(404): deleted from Gmail)."""

    def __init__(self, emails, errors):
        super().__init__(emails)
        self.errors = errors

    def get(self, message_id):
        if message_id in self.errors:
            raise self.errors[message_id]
        return super().get(message_id)


def _labeled(gmail: FakeGmail, e: Email, *names: str) -> Email:
    ids = gmail.ensure_labels(list(names))
    return replace(e, label_ids=["INBOX", *(ids[n] for n in names)])


SUBJECTS = {"c1": "Invoice", "c2": "Ping", "a1": "Digest", "a2": "Weekly", "x": "Merger"}


def _reference_setup(cfg, sessions, account):
    """Two corrections (each also a correction rule) and two accepted rows, one edited since;
    one more correction whose email is gone, one whose Jev call fails."""
    gmail = _FailingGmail([], errors={"g": HttpError(404)})
    processed, notif = cfg.processed_label, cfg.by_key["notification"].label
    emails = {i: email(i, f"S <s{i}@other.org>", s) for i, s in SUBJECTS.items()}
    emails["a1"] = _labeled(gmail, emails["a1"], processed, notif)
    emails["a2"] = _labeled(gmail, emails["a2"], processed)  # the user removed our label
    gmail.emails = emails
    _store(
        sessions,
        _row("c1", corrected_category="transactional", days_ago=1),
        _row("c2", corrected_category="important", days_ago=2),
        _row("g", corrected_category="important", days_ago=3),
        _row("x", corrected_category="important", days_ago=4),
        _row("a1", days_ago=10),
        _row("a2", days_ago=11),
    )
    with sessions() as session:
        for i in ("c1", "c2"):
            rules.set_rule(
                session, account.email, f"s{i}@other.org", "important", RuleSource.MANUAL
            )
        session.commit()
    jev = FakeClassifier(
        {
            "Invoice": make_classification("transactional", 0.83),  # right, 0.03 off 0.80
            "Ping": make_classification("notification", 0.95),  # wrong
            "Digest": make_classification("notification", 0.88),  # right
            "Merger": jev_http_error(TypeSafeUnprocessableEntityError, 422, {"detail": "Merger"}),
        }
    )
    return gmail, jev


def _run_reference(cfg, account, sessions, gmail, jev, quiet):
    with sessions() as session:
        selection = select_reference(
            session, cfg, account.email, limit=30, accepted_after=timedelta(days=7)
        )
        checker = Checker(
            session, gmail, jev, cfg, account, Mode.ACT, body_chars=3000, ignore_rules=True
        )
        checker.run_reference(selection, quiet=quiet)
    return checker


def test_run_reference_asks_jev_scores_and_skips_what_it_cannot_score(
    cfg, account, sessions, capsys
):
    gmail, jev = _reference_setup(cfg, sessions, account)
    checker = _run_reference(cfg, account, sessions, gmail, jev, quiet=False)

    out = capsys.readouterr().out
    # The rules would answer "important"; Jev was asked instead (the failed call included).
    assert [s["subject"] for s in jev.states] == ["Invoice", "Ping", "Merger", "Digest"]
    assert checker.jev_calls == 4
    assert "c1  was=notification@0.90  now=transactional@0.83" in out
    assert "    truth=transactional (corrected)  branch=ok  stored=WRONG" in out
    assert "    truth=important (corrected)  branch=WRONG  stored=WRONG" in out
    assert "x  JEV FAILED: TypeSafeUnprocessableEntityError" in out
    assert "from='S <sc1@other.org>'" in out
    assert out.splitlines()[-6:] == [
        "reference: emails=3 corrected=2 accepted=1 jev_calls=4 failed=1 gone=1 edited=1"
        " unknown_category=0",
        "corrected: branch=1/2 (50.0%) stored=0/2 (0.0%) fixed=1 broken=0 baseline_unknown=0",
        "  corrected important -> notification: branch=1 stored=1",
        "  corrected transactional -> notification: branch=0 stored=1",
        "accepted: branch=1/1 (100.0%) (the stored decision is the truth)",
        "borderline (Jev's category vs its threshold): within_0.05=1 within_0.10=2"
        " of jev_decided=3",
    ]
    assert gmail.modified == [] and gmail.sent == []
    with sessions() as session:  # nothing written: no row changed, no rule learned
        assert session.query(Message).filter(Message.corrected_category.is_not(None)).count() == 4
        assert {r.sender_key: r.category for r in session.query(SenderRule)} == {
            "sc1@other.org": "important",
            "sc2@other.org": "important",
        }


def test_run_reference_quiet_prints_only_the_summary(cfg, account, sessions, capsys):
    gmail, jev = _reference_setup(cfg, sessions, account)
    _run_reference(cfg, account, sessions, gmail, jev, quiet=True)

    out = capsys.readouterr().out
    assert out.startswith("reference: emails=3 ")
    assert len(out.splitlines()) == 6
    for leak in ["from=", "subject=", "@other.org", "Invoice", "c1", "JEV FAILED"]:
        assert leak not in out


def test_run_reference_raises_gmail_errors_other_than_gone(cfg, account, sessions):
    _store(sessions, _row("1", corrected_category="important"))
    gmail = _FailingGmail([], errors={"1": HttpError(500)})
    with sessions() as session:
        checker = Checker(session, gmail, FakeClassifier({}), cfg, account, Mode.ACT, 3000)
        with pytest.raises(HttpError):
            checker.run_reference(select_reference(session, cfg, account.email, limit=5))


def test_a_correction_redone_by_an_act_pass_has_no_baseline(setup, capsys):
    """Built through the real path: a `label` pass, the user swaps Jev/Marketing for
    Jev/Important on "50% off", then an `act` pass redoes it with the new correction rule, which
    overwrites the stored `category` with the correction. A branch answering what Jev answered
    first (marketing) is wrong, but not `broken`: the baseline is unknown, not "stored=ok"."""
    ctx, gmail, account = setup
    triage.run_account(ctx, account, Mode.LABEL)
    gmail.emails["2"].label_ids = ["INBOX", "Label_Jev/Important", "Label_Jev/Processed"]
    triage.run_account(ctx, account, Mode.ACT)
    with ctx.sessions() as session:
        row = session.query(Message).filter_by(gmail_id="2").one()
        assert (row.category, row.corrected_category, row.source) == (
            "important",
            "important",
            "rule",
        )
        selection = select_reference(session, ctx.cfg, account.email, limit=30)
        assert [(r.row.gmail_id, r.truth, r.stored) for r in selection.emails] == [
            ("2", "important", None)
        ]
        checker = Checker(
            session, gmail, ctx.classifier, ctx.cfg, account, Mode.ACT, 3000, ignore_rules=True
        )
        checker.run_reference(selection)

    out = capsys.readouterr().out
    assert "    truth=important (corrected)  branch=WRONG  stored=unknown" in out
    assert out.splitlines()[-3:] == [
        "corrected: branch=0/1 (0.0%) stored=0/0 (n/a) fixed=0 broken=0 baseline_unknown=1",
        "  corrected important -> marketing: branch=1 stored=0",
        "borderline (Jev's category vs its threshold): within_0.05=0 within_0.10=1"
        " of jev_decided=1",
    ]
