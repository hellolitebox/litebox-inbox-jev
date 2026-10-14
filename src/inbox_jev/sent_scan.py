"""The sent-mail domain scan behind `Gmail.has_written_to`: how far it read, across runs."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from . import psl, signals

if TYPE_CHECKING:
    from .gmail import Email

log = logging.getLogger(__name__)

# Sent emails matching `to:<address>` whose recipients are read (newest first) to find one that
# really went to the address (signals.wrote_to_address). Bounds the address check's cost.
SENT_ADDRESS_READS = 10
# Sent emails to a domain whose recipients one check reads to tell a person on it from machine
# addresses only (signals.wrote_to_person_at): the domain check's read budget per run. A scan
# that needs more goes on in later runs from where it stopped (`DomainScan`).
SENT_DOMAIN_READS = 25
# Bump when what makes a recipient a person changes (signals.wrote_to_person_at and the
# machine-address rules it uses), so stored scans are redone under the new rules (`scan_rules`).
SCAN_RULES_VERSION = 1
# Newer sent emails to a domain listed at once to bring a finished scan up to date; with more
# than that, the check doesn't conclude and the domain counts as written.
SENT_NEWER_LIMIT = 500
# How long a `never` verdict is trusted before its scan restarts from the newest match. The
# `after:` search only lists mail dated after the span, so sent mail that enters `in:sent` later
# with an older date (restored from Trash, imported, IMAP-appended) is never seen otherwise. A
# month bounds how long such a miss can let an irreversible unsubscribe through, for one full
# re-read a month of domains that are machine-only and so small (⌈N/25⌉ runs of the usual budget).
NEVER_VERDICT_TTL = timedelta(days=30)
# The headers `SentMail.sent_recipients` reads: who a sent email went to.
RECIPIENT_HEADERS = ("To", "Cc", "Bcc")
_RECIPIENT_KEYS = tuple(h.lower() for h in RECIPIENT_HEADERS)  # Email.headers keys


class DomainVerdict(StrEnum):
    """Where a domain's sent-mail scan stands (`DomainScan.verdict`)."""

    SCANNING = "scanning"  # matches left to read: the domain counts as written
    PERSON = "person"  # a sent email went to a person on it: written, for good
    NEVER = "never"  # every match read, each only to machine addresses: not written


@dataclass
class DomainScan:
    """How far the sent-mail search for one domain has read, newest first, across runs.

    The emails read span `oldest_s`..`newest_s` (their dates, Unix seconds, as Gmail's
    `before:`/`after:` take them) without a gap: a check lists the matches newer than
    `newest_s` and reads them oldest first, then the ones older than `oldest_s`, newest first.
    `newest_ids`/`oldest_ids` are the emails read at exactly those seconds, which the next
    search returns again and skips. `reached_end`: nothing older than `oldest_s` is left.
    """

    domain: str
    rules: str  # scan_rules() when the scan started
    verdict: DomainVerdict = DomainVerdict.SCANNING
    verdict_at: datetime | None = None  # when it became PERSON or NEVER (NEVER_VERDICT_TTL)
    newest_s: int | None = None
    newest_ids: list[str] = field(default_factory=list)
    oldest_s: int | None = None
    oldest_ids: list[str] = field(default_factory=list)
    reached_end: bool = False
    read: int = 0  # emails read so far, all only to machine addresses unless PERSON

    def include(self, message_id: str, sent_s: int) -> None:
        """Count email `message_id`, sent at `sent_s`, as read: widen the span to its date."""
        if self.newest_s is None or sent_s > self.newest_s:
            self.newest_s, self.newest_ids = sent_s, [message_id]
        elif sent_s == self.newest_s and message_id not in self.newest_ids:
            self.newest_ids.append(message_id)
        if self.oldest_s is None or sent_s < self.oldest_s:
            self.oldest_s, self.oldest_ids = sent_s, [message_id]
        elif sent_s == self.oldest_s and message_id not in self.oldest_ids:
            self.oldest_ids.append(message_id)


class SentMail(Protocol):
    """The two Gmail reads the sent-mail search makes: a `gmail.Gmail`, or a fake in tests."""

    def sent_search(self, query: str, limit: int) -> tuple[list[str], bool]:
        """Up to `limit` ids of emails matching the Gmail search `query` (newest first), and
        whether Gmail has more."""
        ...

    def sent_recipients(self, message_id: str) -> Email | None:
        """A sent email's RECIPIENT_HEADERS and date, or None when it was deleted since."""
        ...


class DomainScans(Protocol):
    """Where `Gmail.has_written_to` keeps an account's domain scans between runs: the DB
    (`scan_rows.DomainScanRows`), or `MemoryScans`."""

    def load(self, domain: str) -> DomainScan | None: ...
    def save(self, scan: DomainScan) -> None: ...


class MemoryScans:
    """Domain scans in memory: they last as long as this object (one run, by default)."""

    def __init__(self) -> None:
        self._scans: dict[str, DomainScan] = {}

    def load(self, domain: str) -> DomainScan | None:
        scan = self._scans.get(domain)
        return _copy_scan(scan) if scan is not None else None

    def save(self, scan: DomainScan) -> None:
        self._scans[scan.domain] = _copy_scan(scan)


def _copy_scan(scan: DomainScan) -> DomainScan:
    return replace(scan, newest_ids=list(scan.newest_ids), oldest_ids=list(scan.oldest_ids))


def scan_rules() -> str:
    """The rules a domain scan's verdict depends on: SCAN_RULES_VERSION and the bundled Public
    Suffix List (`psl.fingerprint`), which decides machine hosts. A scan made under others is
    redone."""
    return f"{SCAN_RULES_VERSION}-{psl.fingerprint()}"


def _never_expired(scan: DomainScan) -> bool:
    """A `never` verdict older than NEVER_VERDICT_TTL: its scan starts over."""
    return scan.verdict is DomainVerdict.NEVER and (
        scan.verdict_at is None or datetime.now(UTC) - scan.verdict_at >= NEVER_VERDICT_TTL
    )


def _progress(check: _ScanCheck) -> bool:
    """Whether a finished or interrupted check changed what is worth storing: an email read
    (not only deleted ones), a verdict reached without a read, or a restart."""
    if check.out_of_order:
        return True
    return bool(check.scan.read) and (bool(check.reads) or check.scan.verdict is not check.was)


def _recipients(sent: Email) -> list[str]:
    """The To, Cc and Bcc values of a sent email read with `RECIPIENT_HEADERS`."""
    return [v for name in _RECIPIENT_KEYS for v in sent.headers.get(name, [])]


class _ScanCheck:
    """One check of a domain's sent mail (`wrote_to_person_at_domain`): reads at most
    SENT_DOMAIN_READS emails, updating `scan` as it goes."""

    def __init__(self, mail: SentMail, scan: DomainScan) -> None:
        self._mail, self.scan = mail, scan
        self.was = scan.verdict
        self.reads = 0  # messages.get calls made (a 404 included)
        # The date of the email read last in the current walk (or the span's bound it starts
        # from), and whether the walk is newest first: what `_in_order` checks against.
        self._last_s: int | None = None
        self._newest_first = True
        self.out_of_order = False  # a read broke date order: the scan restarts (`run`)

    def run(self) -> bool:
        """Whether the domain counts as written after this check."""
        scan = self.scan
        newer_done = True
        if scan.newest_s is None:
            self._older()  # nothing read yet: from the newest match down
        else:
            newer_done = self._newer()
            if newer_done and not scan.reached_end:
                self._older()
        if scan.verdict is DomainVerdict.PERSON:
            return True
        if self.out_of_order:
            self._restart()
            return True
        if scan.reached_end and newer_done:
            self._conclude(DomainVerdict.NEVER)
            return False
        scan.verdict = DomainVerdict.SCANNING
        log.info(
            "sent-mail domain scan not finished: counted as written",
            extra={"read": scan.read, "read_now": self.reads},
        )
        return True

    @property
    def _remaining(self) -> int:
        return SENT_DOMAIN_READS - self.reads

    def _newer(self) -> bool:
        """Read the matches sent since the span's newest, oldest first. Whether all were read
        without finding a person (False when Gmail lists more than SENT_NEWER_LIMIT)."""
        scan = self.scan
        assert scan.newest_s is not None
        # after: one second early, so emails sent in the span's newest second are listed too.
        query = f"in:sent to:{scan.domain} after:{scan.newest_s - 1}"
        ids, more = self._mail.sent_search(query, SENT_NEWER_LIMIT)
        if more:
            return False
        self._last_s, self._newest_first = scan.newest_s, False
        seen = set(scan.newest_ids)
        for message_id in reversed(ids):
            if message_id in seen:
                continue
            if self._remaining <= 0 or self._read(message_id):
                return False
        return True

    def _older(self) -> None:
        """Read the matches older than the span (all of them when nothing was read yet), newest
        first, until the budget runs out or one went to a person."""
        scan = self.scan
        if self._remaining <= 0:
            return
        query = f"in:sent to:{scan.domain}"
        seen: set[str] = set()
        if scan.oldest_s is not None:
            # before: one second late, so emails sent in the span's oldest second are listed too.
            query, seen = f"{query} before:{scan.oldest_s + 1}", set(scan.oldest_ids)
        self._last_s, self._newest_first = scan.oldest_s, True
        ids, more = self._mail.sent_search(query, self._remaining + len(seen))
        for message_id in ids:
            if message_id in seen:
                continue
            if self._remaining <= 0 or self._read(message_id):
                return
        if not more:
            scan.reached_end = True

    def _read(self, message_id: str) -> bool:
        """Read one match's recipients. Whether the walk stops here: it went to a person (the
        verdict is then PERSON), or it is out of date order (`out_of_order`)."""
        self.reads += 1
        sent = self._mail.sent_recipients(message_id)
        if sent is None:
            return False
        scan = self.scan
        scan.read += 1
        if signals.wrote_to_person_at(_recipients(sent), scan.domain):
            self._conclude(DomainVerdict.PERSON)
            return True
        sent_s = sent.internal_date_ms // 1000
        if not self._in_order(sent_s):
            self.out_of_order = True
            return True
        scan.include(message_id, sent_s)
        return False

    def _in_order(self, sent_s: int) -> bool:
        """Whether an email sent at `sent_s` comes where the walk expects it: no newer than the
        one before it (and the span's oldest) walking down, no older walking up. The span is only
        gap-free if Gmail lists matches in the date order `after:`/`before:` filter on."""
        last, self._last_s = self._last_s, sent_s
        if last is None:
            return True
        return sent_s <= last if self._newest_first else sent_s >= last

    def _restart(self) -> None:
        """Drop the scan after a read out of date order: the span it built may have gaps, so it
        can't back a `never`. Counts as written now; the next check starts from the newest match."""
        log.warning(
            "sent-mail domain scan read an email out of date order: restarted",
            extra={"read": self.scan.read, "read_now": self.reads},
        )
        self.scan = DomainScan(self.scan.domain, self.scan.rules)

    def _conclude(self, verdict: DomainVerdict) -> None:
        if verdict is not self.was:
            self.scan.verdict_at = datetime.now(UTC)
        self.scan.verdict = verdict


def wrote_to_address(mail: SentMail, address: str) -> bool:
    """Whether a sent email went to `address` itself (`signals.wrote_to_address`).

    Gmail's `to:` search also matches similar addresses, so the To/Cc/Bcc of the newest
    SENT_ADDRESS_READS matches are read, one at a time, stopping at the first that went to
    it: a real contact costs the search and one read. When none of those did and Gmail has
    more matches, the answer is yes (an older one might have; a missed contact is the
    dangerous side). Errors as in `wrote_to_person_at_domain`.
    """
    ids, more = mail.sent_search(f"in:sent to:{address}", SENT_ADDRESS_READS)
    for message_id in ids:
        sent = mail.sent_recipients(message_id)
        if sent is not None and signals.wrote_to_address(_recipients(sent), address):
            return True
    if more:
        log.info(
            "sent-mail address check inconclusive: counted as written", extra={"read": len(ids)}
        )
        return True
    return False


def wrote_to_person_at_domain(mail: SentMail, domain: str, scans: DomainScans) -> bool:
    """Whether a sent email to `domain` had a recipient on it that isn't a machine address.

    The answer comes from a scan of every sent email matching `to:<domain>`, kept in
    `scans` (`DomainScan`, `_ScanCheck.run`): a person found once is final (no Gmail
    call after that); otherwise each check reads at most SENT_DOMAIN_READS emails, and the
    domain counts as written until every match has been read without finding one. A scan
    made under other rules (`scan_rules`) starts over. Whatever was read is saved, also
    when a Gmail error stops the check: the error is raised (previous_contact unknown) and
    the next check goes on from there. An email deleted between the search and the read
    (HTTP 404) is skipped.
    """
    rules = scan_rules()
    scan = scans.load(domain)
    if scan is None or scan.rules != rules or _never_expired(scan):
        scan = DomainScan(domain, rules)
    if scan.verdict is DomainVerdict.PERSON:
        return True
    check = _ScanCheck(mail, scan)
    try:
        return check.run()
    finally:
        if _progress(check):
            scans.save(check.scan)
