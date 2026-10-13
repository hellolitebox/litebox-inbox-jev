# 0026. One unsubscribe gate and one act gate for every path

- Status: Accepted
- Date: 2026-10-13
- PR: #22

## Context
The checks that stop an unsubscribe were written out once per path: `policy._check_unsubscribe`
(Jev and rule paths), `triage.rule_needs_contact` (whether the rule path runs the sent-mail
search) and `triage.retry_unsubscribes` (`unsubscribe-retry`), each calling `can_unsubscribe`,
`spares_known_contacts` and `KNOWN_CONTACT` in its own order. The mode / `act_categories` check
was copied three times (`policy.plan`, `rule_needs_contact`, `retry_unsubscribes`), and the code
storing an attempt twice. [0014](0014-unsubscribe-retry-same-checks.md) and
[0023](0023-one-order-for-unsubscribe-checks.md) each fixed a drift between these copies; the
copies made the next drift likely, and an unsubscribe can't be undone.

## Decision
- `policy.unsubscribe_gate(cfg, account, sig, category, previous_contact) -> GateResult` is the
  only implementation of the sender checks, in one order: the category unsubscribes, not
  allowlisted, verified, not a group or own list, not a known contact for a category that spares
  them. It is pure and returns the first `Veto`, or none with `contact_unknown` when only an
  unknown `previous_contact` is left. Each caller decides what an unknown contact means, as
  before: `run` goes ahead, `unsubscribe-retry` holds the row, and the rule path runs the
  sent-mail search.
- `policy.acts_on(mode, act_categories, category)` is the only mode / `act_categories` check.
- `triage.record_attempt(row, result)` stores every attempt.
- I/O stays in `triage`: the sent-mail search, the once-per-list state, the run cap.
- `tests/test_safety_gates.py` runs the same scenarios through the Jev path, the rule path and
  `unsubscribe-retry` and asserts one outcome for all three.
- `restore` moved from `cli.py` to `triage.restore`, with tests; the CLI only parses and prints.

## Alternatives considered
- Keep the copies and rely on review: two drifts were found that way, after they shipped.
- Fold confidence, once-per-list and the cap into the gate: they need stored state or differ by
  path (a rule has no confidence, a retry row is already the list's), and `policy` stays pure.
- Pass the whole `Account` to `acts_on`: `policy.plan` takes `act_categories`, and its callers and
  tests would have to change for no gain.

## Consequences
- No change in what is archived, spammed, unsubscribed or learned, nor in the `skipped` / `held`
  reasons `unsubscribe-retry` prints. A retried attempt now also logs the `unsubscribe` line.
- A new path that can unsubscribe calls the gate and joins the cross-path tests.
