# 0030. Stop asking Jev in a pass when it rejects the key or keeps failing

- Status: Accepted; repeated refusals: superseded by [0035](0035-identical-refusals-stop-the-pass.md)
- Date: 2026-10-13
- PR: #32

## Context
Every failed Jev call was treated the same way: the email was stored as `error` and retried
with the backoff of [0017](0017-error-retry-backoff-and-cap.md), and the pass went on to the
next email. With Jev down (or our key revoked), each pass sent up to `MAX_MESSAGES_PER_RUN`
emails to Jev, each with the SDK's own retries (up to 30 s), spent one of each email's 8
attempts on a problem that wasn't the email's, and still finished as a successful run, so
`inbox-jev run` exited 0 every 10 minutes. A 401 was retried exactly like a timeout, and a 422
on one email (a request Jev will always refuse) was retried 8 times over 10 hours. Separately,
the stored error was `str(exc)`, which for an SDK HTTP error is the response body's message: on
a 400/422 that can quote the request, i.e. the email's body, which must never be stored or
logged.

0017 rejected "skip runs where every email fails" as too heuristic for one-email runs. What is
decided here is narrower: it never decides an email, only stops calling Jev.

## Decision
`classifier.failure_kind` sorts a failed call (the SDK has already retried 408, 429, 5xx,
timeouts and connection errors):
- **config** (401, 403, 404): the key or model is wrong for every email. The pass stops asking
  Jev at once, and the email that hit it is left untouched (not stored, no attempt counted).
- **rejected** (any other 4xx): Jev refuses this email's request; the email gives up at once
  (`error_attempts` = 8, `retry-errors` resets it) instead of being retried with the backoff.
- **transient** (everything else): the email fails with the usual backoff.

A per-pass `triage.JevGuard` wraps the client: after 3 failed calls in a row
(`MAX_CONSECUTIVE_JEV_FAILURES`, flags-only calls included, reset by any success), or one
config failure, it stops. For the rest of the pass, emails that need Jev's decision are left
untouched (no sent-mail search either) for the next run, allowlisted and keep-rule emails are
decided without the flags (as when their flags-only call fails, [0020](0020-flags-for-allowlist-and-keep-rules.md)),
and rule decisions go on. The run then fails with a stable `runs.error` (no request id, so the
digest groups identical failures), logs `stopped asking Jev` at ERROR, and `run` exits 1.

Jev errors are stored and logged as type, HTTP status and request id only
(`classifier.describe_jev_error`), without a traceback; connection and timeout errors keep the
SDK's redacted text (there is no response), and the SDK's client-side messages are kept when they
match an allowlist of its exact wording (they name a question at most). A failed call is handled
after its `try`, not inside the `except`, so no later error chains the Jev error into a logged
traceback. `logs.setup` keeps the `typesafe_sdk` logger at INFO
or above, since its DEBUG lines (which `TYPESAFE_LOG_LEVEL` can turn on) are the raw request.

## Alternatives considered
- Abort the whole pass on the first Jev failure: one bad email or one timeout would block
  every email behind it, every pass.
- Treat every 4xx as config (stop at once): a 422 on one email would stop every pass at that
  email forever.
- Keep calling Jev for allowlisted / keep-rule emails after stopping: a call per email that
  is known to fail, each with its SDK retries, for flags only.
- Skip all remaining emails, rules included, once stopped: rules and the allowlist don't need
  Jev, and holding them back would make the outage visible in the inbox for no reason.
- A circuit breaker shared across passes (state in the DB): more moving parts; each 10-minute
  pass probing Jev with at most 3 calls is cheap and recovers by itself.

## Consequences
- During an outage, each pass costs at most 3 Jev calls, and only those 3 emails count a
  failure; the digest shows the failed runs.
- An allowlisted or keep-rule email seen while Jev is stopped is labeled without Needs Reply or
  tags, and isn't revisited (the same trade-off as a failed flags-only call).
- If Jev starts refusing every request with 400/422 (a change on its side), 3 emails per pass
  give up before the pass stops; after the fix, `retry-errors` plus `run --lookback` recovers
  them (a known gap in the spec). Superseded by
  [0035](0035-identical-refusals-stop-the-pass.md): two identical refusals in a row now stop
  the pass, and neither email gives up at once.
- `messages.error` for a Jev failure no longer carries Jev's message, only type, status and
  request id: the request id is what TypeSafe support needs.
