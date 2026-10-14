"""Rename a Gmail label in place, keeping it on the emails that already have it."""

from __future__ import annotations

import json
from typing import Protocol

from sqlalchemy import ColumnElement, String, cast, or_, select, update
from sqlalchemy.orm import Session

from .db import Message


class LabelRenameRefused(Exception):
    """`rename` refused the rename before changing anything (system label, child labels, a
    clash, NEW already holding emails): the message says why and what to do."""


class LabelApi(Protocol):
    def labels(self) -> dict[str, str]: ...
    def user_labels(self) -> set[str]: ...
    def label_message_count(self, label_id: str) -> int: ...
    def delete_label(self, label_id: str) -> None: ...
    def rename_label(self, label_id: str, new_name: str) -> None: ...


def _check(existing: dict[str, str], user: set[str], old: str, new: str) -> None:
    """Refuse renames Gmail would reject or that would leave things half-renamed."""
    if old in existing:
        if old not in user:
            raise LabelRenameRefused(f"{old!r} is a system label and can't be renamed")
        # The API renames only this label; children keep the old path in their names.
        children = [n for n in existing if n.casefold().startswith(old.casefold() + "/")]
        if children:
            raise LabelRenameRefused(f"{old!r} has child labels ({', '.join(sorted(children))})")
    # Gmail label names are case-insensitive. A case-only rename of OLD itself is fine.
    clash = next((n for n in existing if n.casefold() == new.casefold() and n != old), None)
    if clash is not None and clash not in user:
        raise LabelRenameRefused(f"{new!r} conflicts with the system label {clash!r}")
    if clash is not None and clash != new:
        raise LabelRenameRefused(f"{new!r} conflicts with the existing label {clash!r}")


def _may_hold(label: str) -> ColumnElement[bool]:
    """SQL prefilter: stored label lists whose JSON text contains `label` as a JSON string.

    Never misses a row: the label is looked for encoded both ways `json.dumps` can store it
    (non-ASCII escaped or not), with LIKE wildcards escaped. It can let through rows without it
    (SQLite's LIKE ignores ASCII case), so the caller still checks the parsed list.
    """
    needles = {json.dumps(label), json.dumps(label, ensure_ascii=False)}
    return or_(
        *(
            cast(column, String).contains(needle, autoescape=True)
            for column in (Message.labels_added, Message.decided_labels)
            for needle in needles
        )
    )


def rename(gmail: LabelApi, session: Session, account: str, old: str, new: str) -> str:
    """Rename OLD to NEW in Gmail and in the stored label lists. Safe to re-run.

    Returns what happened in Gmail: "renamed", "renamed (replaced empty NEW)",
    "already renamed" or "not in gmail". If both exist and NEW is empty (e.g. a triage pass
    created it after the config changed), NEW is deleted first. Refuses (`LabelRenameRefused`,
    before anything changes) when NEW already holds emails, since merging them is not
    supported, and the renames `_check` rejects.
    """
    if old == new:
        raise LabelRenameRefused("old and new names are the same")
    existing = gmail.labels()
    _check(existing, gmail.user_labels(), old, new)
    if old in existing and new in existing:
        if gmail.label_message_count(existing[new]) > 0:
            raise LabelRenameRefused(
                f"both {old!r} and {new!r} exist in Gmail and {new!r} has emails; "
                "merge them by hand first"
            )
        gmail.delete_label(existing[new])
        gmail.rename_label(existing[old], new)
        outcome = "renamed (replaced empty NEW)"
    elif old in existing:
        gmail.rename_label(existing[old], new)
        outcome = "renamed"
    elif new in existing:
        outcome = "already renamed"
    else:
        outcome = "not in gmail"

    rows = session.execute(
        select(Message.id, Message.labels_added, Message.decided_labels).where(
            Message.account == account, _may_hold(old)
        )
    )
    changes = []  # the prefilter only narrows the rows: this checks the exact label
    for row_id, added, decided in rows:
        if (added and old in added) or (decided and old in decided):
            changes.append(
                {
                    "id": row_id,
                    "labels_added": [new if n == old else n for n in added] if added else added,
                    "decided_labels": (
                        [new if n == old else n for n in decided] if decided else decided
                    ),
                }
            )
    if changes:
        session.execute(update(Message), changes)  # bulk UPDATE by primary key
    session.commit()
    return outcome
