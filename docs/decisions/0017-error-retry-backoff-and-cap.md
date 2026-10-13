# 0017. Retry failed emails with a backoff, and give up after 8 failures

- Status: Accepted
- Date: 2026-10-13
- PR: #20

## Context
An email whose processing raised (Jev error, a malformed message, a sender rule whose category
was removed) was stored as `status = error` and retried on every run while it stayed inside
`LOOKBACK`. With the timer every 10 minutes and the default `2d`, that is up to 288 attempts,
each one a Jev call (or a Gmail read) that fails the same way and a stack trace in the journal.
Nothing told the user the email would never be handled. On the other side, a short cap with no
spacing (say 5 attempts, one per run) would make every email that arrived during a 50-minute Jev
or Gmail outage give up, and those emails would stay unlabeled in the inbox.

## Decision
Count consecutive failures per email in `messages.error_attempts` (reset to 0 on success). After
the n-th failure the email is retried no sooner than 5 × 2<sup>n−1</sup> minutes later (5, 10,
20 … 320). After 8 failures (`db.MAX_ERROR_ATTEMPTS`, about 10.5 hours after the first) it is no
longer retried: the row keeps `status = error` and the digest lists it under "Sin más
reintentos". `inbox-jev retry-errors -a EMAIL` resets the count so the next run tries again (for
emails still within its `--lookback`). A Gmail token refresh that fails mid-run (expired, revoked
or a transient token-endpoint error) fails the whole run instead of being counted against each
email. `runs.error` then has one of three texts: `invalid_grant` (expired or revoked) tells
the user to run `inbox-jev auth add`; a failure google-auth marks retryable says it is
temporary and will retry next run; any other refresh failure (e.g. a 5xx with a non-JSON body,
which google-auth marks non-retryable) says it will retry next run and to run `auth add` if it
persists. None of them stores the token endpoint's response, only the `invalid_grant` summary.

## Alternatives considered
- A fixed cap of N attempts, one per run: simple, but a one-hour outage exhausts it for every
  email that arrived meanwhile.
- A new `failed` status: clearer in the DB, but `failed` already means "retryable" for
  unsubscribes, and every query on `status = 'error'` (digest, `stats`) would need to know both.
- Skip runs where every email fails (outage detection): heuristic, and a run with one email
  can't tell an outage from a bad email.

## Consequences
- New NOT NULL column `messages.error_attempts` (default 0). `make_engine` adds it to existing
  databases (`ALTER TABLE … DEFAULT 0`), so old `error` rows start at 0 and are retried as before.
- An outage longer than ~10 hours still makes the emails of that window give up; after it,
  `inbox-jev retry-errors` plus `run --lookback` recovers them.
- A sender rule whose category was removed now fails each of that sender's emails 8 times, not
  for the whole `LOOKBACK` (since [0019](0019-rules-to-removed-categories-ignored.md) such a rule
  is ignored instead).
