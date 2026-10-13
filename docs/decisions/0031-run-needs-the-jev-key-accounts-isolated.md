# 0031. `run` needs the Jev key; one account's failure never stops the others

- Status: Accepted
- Date: 2026-10-13
- PR: #32

## Context
`TYPESAFE_API_KEY` defaults to an empty string and the Jev client was created the first time an
email needed it. With no key, every such email failed (`status = error`, one attempt spent) and
the run still exited 0, every 10 minutes, until the emails gave up. Separately, `run` and
`labels rename` caught a failing account and went on to the next, but `digest`,
`unsubscribe-retry` and `senders backfill` stopped at the first account that failed (an expired
token, a Slack error), so the accounts after it got no digest or retry, and `unsubscribe-retry`
didn't close its Jev context.

## Decision
- A `run` pass creates the Jev client before touching Gmail. A missing or malformed key fails
  the pass with `runs.error` "TYPESAFE_API_KEY is not set: add it to .env; nothing was
  processed" (shown in the digest), even when rules would decide every email, and `run` exits 1.
  The check lives in the pass, not in `Settings`: commands that never ask Jev (`digest`,
  `rules`, `stats`, `restore`, `reprocess`, `retry-errors`, `unsubscribe-retry`, `labels`,
  `senders backfill`, `--version`) keep working without the key.
- Every command that loops over accounts (`run`, `digest`, `unsubscribe-retry`,
  `senders backfill`; `labels rename` already did) runs each account on its own: a failure is
  logged with its traceback, printed as `EMAIL: <what> failed: <reason>` (the same text as
  `runs.error`, so tokens and Jev errors are described without their raw bodies), the other
  accounts go on, and the command exits 1 at the end if any failed. `unsubscribe-retry`
  closes its context in a `finally`.

## Alternatives considered
- Make the key a required `Settings` field: every command, including `digest` and `restore`,
  would refuse to start without it.
- Check the key once in `cli.run` and exit before any account: clear on a terminal, but no
  `runs` row is written, so the daily digest (the user's monitoring) would say nothing.
- Keep stopping at the first failed account: one expired token silences the digest for every
  account listed after it.

## Consequences
- With no key, each timer pass writes one failed run per account (shown and grouped in the
  digest) and the systemd unit shows a failed run.
- Scripts or timers that relied on `digest`/`unsubscribe-retry`/`senders backfill` stopping at
  the first failure now see the remaining accounts processed and exit 1 at the end.
