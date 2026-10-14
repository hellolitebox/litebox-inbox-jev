import pytest
from fakes import FakeLabels
from sqlalchemy import select

from inbox_jev import labels
from inbox_jev.config import Mode
from inbox_jev.db import Message


def _msg(account, gmail_id, added, decided):
    return Message(
        account=account,
        gmail_id=gmail_id,
        thread_id="t",
        sender="a@b.com",
        mode=Mode.LABEL,
        labels_added=added,
        decided_labels=decided,
    )


@pytest.fixture
def factory(tmp_path, make_sessions):
    # File-backed so _rows can read through a fresh session and really check persistence.
    return make_sessions(f"sqlite:///{tmp_path}/t.db")


@pytest.fixture
def session(factory):
    with factory() as s:
        s.add_all(
            [
                _msg("me@x.com", "1", ["Jev/Revisar", "Jev/Spam"], ["Jev/Revisar", "Jev/Spam"]),
                _msg("me@x.com", "2", None, ["Jev/Revisar"]),
                _msg("other@x.com", "3", ["Jev/Revisar"], ["Jev/Revisar"]),
            ]
        )
        s.commit()
        yield s


@pytest.fixture
def rows(factory):
    def read():
        with factory() as fresh:
            return {m.gmail_id: m for m in fresh.query(Message)}

    return read


def test_rename_keeps_label_id_and_rewrites_this_accounts_rows(session, rows):
    gmail = FakeLabels(["INBOX", "Jev/Revisar"])
    assert labels.rename(gmail, session, "me@x.com", "Jev/Revisar", "Jev/Review") == "renamed"
    assert gmail.renamed == [("Label_1", "Jev/Review")]
    stored = rows()
    assert stored["1"].labels_added == ["Jev/Review", "Jev/Spam"]
    assert stored["1"].decided_labels == ["Jev/Review", "Jev/Spam"]
    assert stored["2"].labels_added is None and stored["2"].decided_labels == ["Jev/Review"]
    assert stored["3"].labels_added == ["Jev/Revisar"]  # other account untouched


def test_rename_is_idempotent(session, rows):
    gmail = FakeLabels(["Jev/Revisar"])
    labels.rename(gmail, session, "me@x.com", "Jev/Revisar", "Jev/Review")
    again = labels.rename(gmail, session, "me@x.com", "Jev/Revisar", "Jev/Review")
    assert again == "already renamed" and len(gmail.renamed) == 1
    assert rows()["1"].labels_added == ["Jev/Review", "Jev/Spam"]


def test_rename_without_gmail_label_still_updates_db(session, rows):
    gmail = FakeLabels(["INBOX"])
    assert labels.rename(gmail, session, "me@x.com", "Jev/Revisar", "Jev/Review") == "not in gmail"
    assert rows()["2"].decided_labels == ["Jev/Review"]


def test_rename_refuses_when_both_exist_and_new_has_emails(session, rows):
    gmail = FakeLabels(["Jev/Revisar", "Jev/Review"], counts={"Jev/Review": 3})
    with pytest.raises(labels.LabelRenameRefusedError, match="has emails"):
        labels.rename(gmail, session, "me@x.com", "Jev/Revisar", "Jev/Review")
    assert gmail.renamed == [] and gmail.deleted == []
    assert rows()["1"].labels_added[0] == "Jev/Revisar"


def test_rename_replaces_empty_new_label(session, rows):
    # A triage pass ran after the config change and created NEW empty.
    gmail = FakeLabels(["Jev/Revisar", "Jev/Review"])
    outcome = labels.rename(gmail, session, "me@x.com", "Jev/Revisar", "Jev/Review")
    assert outcome == "renamed (replaced empty NEW)"
    assert gmail.deleted == ["Label_1"] and gmail.renamed == [("Label_0", "Jev/Review")]
    assert gmail.labels() == {"Jev/Review": "Label_0"}
    assert rows()["1"].labels_added == ["Jev/Review", "Jev/Spam"]


@pytest.mark.parametrize(
    ("existing", "old", "new", "match"),
    [
        (["Jev/Revisar", "jev/review"], "Jev/Revisar", "Jev/Review", "existing label 'jev/review'"),
        (["jev/review"], "Jev/Revisar", "Jev/Review", "existing label"),
        (["Jev/Revisar", "SPAM"], "Jev/Revisar", "spam", "system label 'SPAM'"),
        (["INBOX"], "INBOX", "Jev/Inbox", "system label"),
        (["Jev", "Jev/Revisar"], "Jev", "Jevv", "child labels"),
    ],
)
def test_rename_refuses_conflicts(session, rows, existing, old, new, match):
    gmail = FakeLabels(existing)
    with pytest.raises(labels.LabelRenameRefusedError, match=match):
        labels.rename(gmail, session, "me@x.com", old, new)
    assert gmail.renamed == [] and gmail.deleted == []
    assert rows()["1"].labels_added[0] == "Jev/Revisar"


def test_rename_allows_case_only_change(session):
    gmail = FakeLabels(["jev/review"])
    assert labels.rename(gmail, session, "me@x.com", "jev/review", "Jev/Review") == "renamed"
    assert gmail.renamed == [("Label_0", "Jev/Review")]


def test_rename_refuses_same_name(session):
    with pytest.raises(labels.LabelRenameRefusedError):
        labels.rename(FakeLabels([]), session, "me@x.com", "Jev/Spam", "Jev/Spam")


@pytest.mark.parametrize("old", ["Jev/Café", "Jev/50%_off", 'Jev/"quoted"\\x'])
def test_rename_finds_labels_the_sql_prefilter_must_encode(factory, rows, old):
    """The prefilter matches the stored JSON text: non-ASCII (escaped by `json.dumps`), LIKE
    wildcards and JSON escapes must still reach the exact check."""
    with factory() as s:
        s.add(_msg("me@x.com", "9", [old, "Jev/Spam"], [old]))
        s.commit()
        labels.rename(FakeLabels([]), s, "me@x.com", old, "Jev/New")
    stored = rows()["9"]
    assert (stored.labels_added, stored.decided_labels) == (["Jev/New", "Jev/Spam"], ["Jev/New"])


def test_rename_prefilter_narrows_rows_and_the_exact_check_keeps_lookalikes(session, rows):
    lowered = "jev/revisar"  # SQLite's LIKE ignores ASCII case: the prefilter lets it through
    session.add(_msg("me@x.com", "8", ["Jev/Revisar/Old"], ["Jev/Revisa"]))
    session.add(_msg("me@x.com", "9", None, [lowered]))
    session.commit()
    holding = select(Message.gmail_id).where(labels._may_hold("Jev/Revisar"))
    assert sorted(session.scalars(holding)) == ["1", "2", "3", "9"]  # not 8: no '"Jev/Revisar"'
    labels.rename(FakeLabels([]), session, "me@x.com", "Jev/Revisar", "Jev/Review")
    stored = rows()
    assert (stored["8"].labels_added, stored["8"].decided_labels) == (
        ["Jev/Revisar/Old"],
        ["Jev/Revisa"],
    )
    assert stored["9"].decided_labels == [lowered]  # let through, then left by the exact check
    assert stored["2"].decided_labels == ["Jev/Review"]
