import pytest

from inbox_jev import labels
from inbox_jev.config import Mode
from inbox_jev.db import Message, make_session_factory

SYSTEM = {"INBOX", "SPAM"}


class FakeLabels:
    def __init__(self, names, counts=None):
        self._labels = {n: f"Label_{i}" for i, n in enumerate(names)}
        self._counts = counts or {}  # label name -> messagesTotal
        self.renamed = []
        self.deleted = []

    def labels(self):
        return dict(self._labels)

    def user_labels(self):
        return {n for n in self._labels if n not in SYSTEM}

    def label_message_count(self, label_id):
        name = next(n for n, i in self._labels.items() if i == label_id)
        return self._counts.get(name, 0)

    def delete_label(self, label_id):
        name = next(n for n, i in self._labels.items() if i == label_id)
        del self._labels[name]
        self.deleted.append(label_id)

    def rename_label(self, label_id, new_name):
        old = next(n for n, i in self._labels.items() if i == label_id)
        self._labels[new_name] = self._labels.pop(old)
        self.renamed.append((label_id, new_name))


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
def factory(tmp_path):
    # File-backed so _rows can read through a fresh session and really check persistence.
    return make_session_factory(f"sqlite:///{tmp_path}/t.db")


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
    with pytest.raises(ValueError, match="has emails"):
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
    with pytest.raises(ValueError, match=match):
        labels.rename(gmail, session, "me@x.com", old, new)
    assert gmail.renamed == [] and gmail.deleted == []
    assert rows()["1"].labels_added[0] == "Jev/Revisar"


def test_rename_allows_case_only_change(session):
    gmail = FakeLabels(["jev/review"])
    assert labels.rename(gmail, session, "me@x.com", "jev/review", "Jev/Review") == "renamed"
    assert gmail.renamed == [("Label_0", "Jev/Review")]


def test_rename_refuses_same_name(session):
    with pytest.raises(ValueError):
        labels.rename(FakeLabels([]), session, "me@x.com", "Jev/Spam", "Jev/Spam")
