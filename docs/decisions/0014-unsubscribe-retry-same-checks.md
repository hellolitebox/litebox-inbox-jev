# 0014. `unsubscribe-retry` applies the same checks as `run`

- Status: Accepted
- Date: 2026-10-13
- PR: #15 (found in #14 QA; QA rounds 1–2)

## Context

`unsubscribe-retry` re-checked only DMARC and the one-click URL guard. A `failed` row from before
the group and `List-Post` guards, or for a sender the user has since written to, could unsubscribe
the user from a group or opt them out of a contact's mail. It also ignored later corrections, sender
rules, restores, label edits and thresholds.

## Decision

`triage.retry_unsubscribes` re-checks each `failed` row against the email's current state: mode, run
cap, restore and corrections, live Gmail labels (spam, trash, correction, user edits), the current
sender rule, `act_categories`, the current threshold for Jev answers, the allowlist,
`can_unsubscribe` and, for `cold_outreach`, the sent-mail search. Rows the user or the sender ruled
out become `skipped` (so `run` won't retry them either); rows held by settings or transient state
stay `failed`. A failed sent-mail search holds the row, which is stricter than `run`.

## Alternatives considered

- Tell operators not to run the command (the interim README warning from #14).
- Run the full triage on the email again: calls Jev again for a manual retry.

## Consequences

- Every path that can unsubscribe goes through the same checks (a CLAUDE.md convention).
- A row whose rule was deleted is held, never acted on with the deleted rule's verdict.
- Since [0026](0026-one-unsubscribe-gate-for-every-path.md), the allowlist, `can_unsubscribe` and
  known-contact checks named above are one call to `policy.unsubscribe_gate`, and the mode /
  `act_categories` check is `policy.acts_on`, shared with `run`.
