# 0040. The domain search's scan is kept across runs until every match is read

- Status: Accepted
- Date: 2026-10-14
- PR: #39

## Context
The domain search (ADR 0032) reads the recipients of the newest 25 sent emails matching
`to:<domain>` and, when all went to machine addresses and Gmail had more, counted the domain as
written: an older email might be to a person, and a missed contact is the dangerous side. So a
service you replied to more than 25 times at machine addresses (GitHub's
`reply+…@reply.github.com`, a ticketing system) stayed a known contact forever: its routine mail
with a reply signal stayed in Review, and its outreach was never unsubscribed. Reading everything
in one check would make a run's Gmail cost unbounded.

## Decision
The scan's progress is stored per account and domain in a new table, `sent_domain_scans`
(`db.SentDomainScan`, written by `triage.DomainScanRows` on the pass's session and committed with
the email it was made for; the write is an upsert, see Consequences). `gmail.DomainScan` keeps the span of sent dates read without gaps
(newest and oldest second, the ids read in those two seconds), whether nothing older is left,
how many were read, the verdict and when it was reached. Each check reads at most
`gmail.SENT_DOMAIN_READS` = 25 emails (the per-run budget for that domain):

1. A `person` verdict is final: no Gmail call.
2. With a scan, the matches newer than the span (`after:`, up to `SENT_NEWER_LIMIT` = 500) are
   read oldest first, so the span stays contiguous; then, until the end is reached, the next older
   ones (`before:`), newest first.
3. `never` once nothing older and nothing newer is unread; `scanning` (counts as written)
   otherwise. A `never` is re-checked by the `after:` search on every check, so new sent mail to
   the domain is read the first time it matters.
4. A `never` reached `gmail.NEVER_VERDICT_TTL` = 30 days ago or more starts over as a new scan.
   The `after:` search can't see sent mail that enters the folder later with an older date
   (restored from Trash, imported or migrated, appended over IMAP), and the miss would sit on the
   irreversible side (an unsubscribe from a domain you wrote to). 30 days: these domains are
   machine-only and small, so the re-read is ⌈N / 25⌉ runs of the usual budget once a month,
   while a week would quadruple that cost for little, and a quarter would leave a missed contact
   exposed to unsubscribes three times as long.
5. Every read is checked against the order the span depends on (walking down: no newer than the
   previous read and the span's oldest; walking up: no older than the previous read and the
   span's newest). A violation means Gmail's list order or its date filters don't match the
   dates read, so the span may hide an unread email: the scan is dropped and starts over at the
   next check, the domain counts as written, and a warning with counts only is logged. That check
   never concludes `never`.

Until the scan is complete, the answer stays "written" (the safe side). A scan made under other
rules (`SCAN_RULES_VERSION` in code, the bundled Public Suffix List's digest) starts over.
`scripts/dry_classify.py` keeps scans in memory only, so it never writes the DB.

## Alternatives considered
- **Read every match in one check**: one run could spend hundreds of reads on a single domain.
- **Gmail page tokens to continue**: not documented to stay valid across runs or as new mail
  arrives; date bounds (`before:`/`after:` in Unix seconds, which Gmail documents) are stable.
  Seconds can tie, so the ids read in the boundary seconds are kept and skipped.
- **Re-scan a `never` domain only from scratch, on every check**: costs a full scan each time;
  listing what is newer than the span is one `list` and reads only new mail. Rejected as the
  only mechanism, but kept as a monthly restart (decision 4): first rejected for its cost,
  without weighing that the `after:` search alone misses mail that appears with an old date. A
  bounded re-read once a month per machine-only domain is a small, one-off cost against an
  irreversible unsubscribe.
- **Trust Gmail's order without checking**: the checks cost nothing (the dates come with the
  reads), and an unchecked violation would land on the dangerous side.
- **Commit scans on their own session**: in SQLite it would wait on the pass's open write
  transaction (busy timeout) or deadlock; saving with the email loses only the progress of an
  email whose processing failed, which the next check redoes.
- **Save with a select-then-insert on the ORM**: the insert is only flushed later, so a row
  another process inserted in between fails that commit on the unique key. In
  `unsubscribe-retry` the commit comes after the unsubscribe request, so the attempt row would
  roll back after an irreversible request and the next retry would send it again.
- **Store rows for domains with no sent match**: one `list` answers those already; rows would
  grow with every sender domain ever seen.

## Consequences
- A domain written to only at machine addresses N times becomes `never` after ⌈N / 25⌉ runs in
  which an email from it is decided; until then it is a known contact, as before.
- Gmail cost per domain and run is bounded: one or two `list` calls and at most 25 metadata
  reads (135 quota units); a `person` domain costs nothing, a `never` domain one `list`.
- `person` never expires; deleting the sent email doesn't undo it. A `never` is at most 30 days
  stale for sent mail that appears with an older date; while its scan restarts, the domain counts
  as written.
- `DomainScanRows.save` runs an `INSERT … ON CONFLICT DO UPDATE` at once (SQLite and Postgres,
  the databases the README documents), so `run` and `unsubscribe-retry` checking the same new
  domain can't fail either commit on the unique key: the last scan saved wins (each is gap-free
  on its own), except that a `person` saved under the same rules stays. Running it at once also
  takes the write lock during the sent-mail search, before any unsubscribe: a locked database
  fails the save there, and `previous_contact` raises it instead of answering "unknown" (which
  `run` doesn't treat as a veto), so `run` fails the email (retried) and `unsubscribe-retry`
  stops, instead of rolling back an attempt after the request. In `run` the unique-key race used
  to fail only that email, which is retried.
- Both unsubscribe paths take the write lock before the request, so a locked database fails the
  attempt before it is sent rather than rolling back its row after. `run`'s `_unsubscribe`
  always has a write to flush (the email's row, `processed_at` just set). `unsubscribe-retry`
  often has none (no search, a cached or `person` verdict, a `never` with nothing new, an earlier
  row's skip already committed), so it stamps the row's `attempted_at` before flushing
  (`record_attempt` sets it again after the request).
- `run` now holds the write lock from the scan's save, before Jev's classification, the Gmail
  label calls and its own unsubscribe (15 s timeout), until that email commits, instead of from
  the email's write after them. A concurrent CLI writer (`unsubscribe-retry`, `rules set`,
  `restore`) waits longer under the 30 s busy timeout; past it, the command fails with "database
  is locked" before any irreversible request and can be run again.
- Other databases have no upsert statement: `DomainScanRows` refuses them when it is built, at
  the start of a `run` pass (any mode, `dry_run` included) and of `unsubscribe-retry`'s
  `retry_unsubscribes` (before its row loop), so the account fails before its mail is read or
  any unsubscribe is sent; and `previous_contact` raises that error rather than answer
  "unknown", which `run` doesn't treat as a veto.
- New table, created by `create_all` on the first run after deploy; no change to existing
  tables, no migration. Rolling back leaves an unused table.
- A change to what counts as a machine address must bump `SCAN_RULES_VERSION`.
