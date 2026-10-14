"""An account's sent-mail domain scans stored in the database (`sent_domain_scans`)."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import Executable

from .db import SentDomainScan, now
from .sent_scan import DomainScan, DomainVerdict

# The databases with the ON CONFLICT upsert `scan_upsert` writes: the ones the app documents.
SCAN_UPSERT_DIALECTS = ("sqlite", "postgresql")


class DomainScanRows:
    """An account's sent-mail domain scans (`sent_scan.DomainScans`) in `sent_domain_scans`.

    On the caller's session and never committed here: a scan is saved with the email it was
    made for (a rolled-back email loses it, and the next check reads those emails again). Its
    own session would wait on the pass's write lock in SQLite.

    `save` is an upsert, so a row another process inserted since `load` (`run` and
    `unsubscribe-retry` checking the same new domain) can't fail the commit with a unique
    conflict: the last scan saved wins, except over a `person` made under the same rules, which
    stays (it is final). Either scan is gap-free on its own, so the next check goes on from it.

    `dialects`: the databases it accepts, those `scan_upsert` writes by default; on any other it
    raises `NotImplementedError` (tests narrow it to stand for a database without the upsert).
    """

    def __init__(
        self,
        session: Session,
        account: str,
        *,
        dialects: Collection[str] = SCAN_UPSERT_DIALECTS,
    ) -> None:
        # Checked here, not at the first save: `previous_contact` must not reach a domain
        # verdict it then can't store (its caller would see an unknown contact, no veto).
        self._dialect = session.get_bind().dialect.name
        _require_scan_upsert(self._dialect, dialects)
        self._session, self._account = session, account

    def load(self, domain: str) -> DomainScan | None:
        # populate_existing: `save` writes with Core, so a row already in the session is stale.
        row = self._session.scalar(
            select(SentDomainScan)
            .where(SentDomainScan.account == self._account, SentDomainScan.domain == domain)
            .execution_options(populate_existing=True)
        )
        if row is None:
            return None
        return DomainScan(
            domain=row.domain,
            rules=row.rules,
            verdict=DomainVerdict(row.verdict),
            verdict_at=_utc(row.verdict_at),
            newest_s=row.newest_s,
            newest_ids=list(row.newest_ids or []),
            oldest_s=row.oldest_s,
            oldest_ids=list(row.oldest_ids or []),
            reached_end=row.reached_end,
            read=row.read,
        )

    def save(self, scan: DomainScan) -> None:
        self._session.execute(scan_upsert(self._dialect, self._account, scan))


def _require_scan_upsert(dialect: str, dialects: Collection[str] = SCAN_UPSERT_DIALECTS) -> None:
    """Raise NotImplementedError on a database not in `dialects` (by default, one `scan_upsert`
    has no statement for)."""
    if dialect not in dialects:
        raise NotImplementedError(f"no upsert for sent_domain_scans on {dialect}")


def scan_upsert(dialect: str, account: str, scan: DomainScan) -> Executable:
    """The statement `DomainScanRows.save` runs: insert the scan, or update the account's row
    for its domain unless that row is a `person` made under the same rules. ON CONFLICT exists
    in the databases the app documents, SQLite and Postgres."""
    _require_scan_upsert(dialect)
    insert: sqlite.Insert | postgresql.Insert
    if dialect == "sqlite":
        insert = sqlite.insert(SentDomainScan)
    else:
        insert = postgresql.insert(SentDomainScan)
    values = {
        "rules": scan.rules,
        "verdict": scan.verdict.value,
        "verdict_at": scan.verdict_at,
        "newest_s": scan.newest_s,
        "newest_ids": list(scan.newest_ids),
        "oldest_s": scan.oldest_s,
        "oldest_ids": list(scan.oldest_ids),
        "reached_end": scan.reached_end,
        "read": scan.read,
        "updated_at": now(),
    }
    insert = insert.values(account=account, domain=scan.domain, **values)
    stored = SentDomainScan
    return insert.on_conflict_do_update(
        index_elements=[stored.account, stored.domain],
        set_=values,
        where=or_(
            stored.verdict != DomainVerdict.PERSON.value, stored.rules != insert.excluded.rules
        ),
    )


def _utc(at: datetime | None) -> datetime | None:
    """`at` as an aware UTC datetime: SQLite returns naive ones (stored as UTC)."""
    return at.replace(tzinfo=UTC) if at is not None and at.tzinfo is None else at
