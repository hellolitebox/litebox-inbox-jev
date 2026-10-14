---
paths:
  - "src/inbox_jev/**/*.py"
---

# Where code goes

- `policy.py` stays pure (no I/O). Gmail access goes in `gmail.py` (body extraction in `body.py`,
  the sent-mail search in `sent_scan.py`) and orchestration in `triage.py` (the pass's Jev client in
  `jev_guard.py`, the stored domain scans in `scan_rows.py`, `unsubscribe-retry` in
  `unsubscribe_retry.py`).
- New categories or labels go in `categories.yaml`, not in code.
- Open the DB with `db.open_sessions` (a context manager that disposes the engine): pytest turns
  `ResourceWarning` into an error, so an engine, file or client left open fails the tests.
- Logs carry ids and counts, never email bodies or addresses that identify people beyond what the
  line needs.
- Schema changes: the project uses `create_all` with no migrations yet. A change to an existing
  table needs a migration story (or an explicit note in the PR) for databases already deployed.
