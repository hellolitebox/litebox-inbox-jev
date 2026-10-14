# 0035. Two identical Jev refusals in a row stop the pass; neither email gives up at once

- Status: Accepted
- Date: 2026-10-13
- PR: #36

## Context

[0030](0030-stop-asking-jev-when-it-is-down.md) made a refused request (`JevFailure.REJECTED`: a 4xx
other than 401/403/404/408/429, e.g. 400 or 422) give up its email at once: Jev would refuse the
same request on every retry. It left a known gap: if Jev starts refusing every request the same way
(a schema change on its side rejecting our state), each pass gave up on the emails that hit it (up
to 3, until the 3-in-a-row limit stopped the pass), 8 attempts each at once. Every 10-minute pass
buried more emails until someone noticed and ran `retry-errors`.

One refusal alone can't tell "this email" from "every email". The next Jev call can: if Jev answers
it, or fails it another way, the first refusal was about that email.

## Decision

A refused email is **held**, not stored, until the pass's next Jev call (`triage._HeldRejection`,
settled in `_run_pass`). Flags-only calls count as calls; an email decided by a rule makes none and
settles nothing.

- If that call is refused **the same way** (same SDK error type and HTTP status;
  `triage.MAX_IDENTICAL_REJECTIONS` = 2 in a row, tracked by `JevGuard`), Jev refuses everything:
  the pass stops asking Jev (like a config failure) and the run fails with "Jev refused 2 requests
  in a row the same way (…): likely a change on Jev's side; …". Each of the two emails is stored as
  `error` with one **ordinary** failure (`error_attempts` + 1, the usual backoff, `error` = the type
  and status), not the give-up-at-once of a refusal: Jev may refuse everything, or these may be two
  bad emails in a row. The emails after them wait for the next run, untouched.
- Otherwise (an answer, a refusal with another status, another failure, or no further call before
  the pass ends), the held email is stored as `error` and gives up at once, as in 0030.

This supersedes 0030's handling of repeated refusals; the rest of 0030 stands.

## Alternatives considered

- Store the first refusal at once and undo it when the second arrives: the row (new or an earlier
  failure) would have to be restored field by field, and a crash between the two would leave a
  wrongly given-up email.
- Count refusals per pass rather than in a row: a single bad email early in a pass and another one
  an hour of mail later would stop a pass whose calls mostly succeed.
- Treat any two refusals in a row as systemic, whatever the status: two different refusals (400,
  then 422) say less about Jev than two identical ones; each stays the email's own.
- Stop at the first refusal (treat REJECTED as config): one bad email would stop every pass at that
  email forever (rejected in 0030 too).
- Leave both emails untouched (not stored, no failure counted; this ADR's first version): two emails
  Jev really refuses the same way, next to each other (one message delivered to two aliases, two
  issues of one newsletter that trip the same 400), would stop every pass on themselves forever.
  They'd never be stored, so the backoff never skipped them and `retry-errors` had nothing to reset;
  every email behind them that needs Jev waited until it left the lookback.
- Give both up at once (as a single refusal does): while Jev refuses everything, each pass would
  bury two more emails until someone ran `retry-errors`, the gap this ADR closes.

## Consequences

- With Jev refusing everything, each pass costs 2 Jev calls and charges the 2 emails it asked one
  ordinary failure each; after the fix the backoff retries them without `retry-errors`. An email
  charged on every retry gives up after 8 failures (about 10.5 hours of backoff): an outage that
  long needs `retry-errors` after the fix.
- Two genuinely bad emails in a row stop only the passes where both are due again, fewer as their
  backoff grows; the passes in between decide the emails behind them, and the two give up after 8
  failures (EX-30). Those stopped passes still decide none of the emails behind them that need Jev.
- A refused email that is the only Jev call of its pass still gives up at once even when Jev refuses
  everything; the next pass stops on its first two refusals. `retry-errors` recovers it.
- An email's refusal is stored a little later in the pass (after the next Jev call): if the run
  crashes in between, the email is left as it was and retried next run.
