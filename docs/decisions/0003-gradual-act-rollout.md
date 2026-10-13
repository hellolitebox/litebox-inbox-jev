# 0003. Roll out `act` gradually with `act_categories`

- Status: Accepted
- Date: 2026-10-12
- PR: #6 (`reprocess` added in #7)

## Context
The user wanted reversible archiving for transactional, notification and newsletter right away,
but to keep marketing (irreversible unsubscribe) and spam as labels for a week of review. Also,
switching `label` → `act` silently skipped every email already labeled, so none of them would ever
be archived.

## Decision
- An optional per-account `act_categories` list. In `act`, only listed categories get their
  action; the others are planned as `label`. Absent = all, `[]` = none, an empty key is rejected
  (fail closed). Unknown keys fail the account's run before any Gmail write.
- `act` processes again the rows still in the inbox that were seen in `dry_run` or `label`, and
  removes stale labels from the earlier pass.
- `run --lookback` for one-off backlog passes, and `inbox-jev reprocess` to mark act rows Jev left
  in the inbox for one more pass.

## Alternatives considered
- One global `MODE` only: all or nothing, including irreversible unsubscribes.
- A per-category `min_confidence` of 1.0 as an off switch: also sends everything to Review.

## Consequences
- `act_categories` is matched against the final category, and the archive group only sums listed
  categories ([0006](0006-archive-group-by-action-confidence.md)).
- Widening `act_categories` only affects new emails unless rows are marked with `reprocess`.
- A new key must be deployed before it is added to `accounts.yaml`, and removed from
  `accounts.yaml` first on rollback.
