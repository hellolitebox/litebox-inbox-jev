# 0021. An archive-group member counts against its own threshold

- Status: Accepted
- Date: 2026-10-13
- PR: #21

## Context

The archive group ([0006](0006-archive-group-by-action-confidence.md)) excludes a non-top member
whose threshold is above the floor, so a category someone made stricter can't be archived through
someone else's confidence. Only the account-level `min_confidence` was read (default 0.0): a
stricter `min_confidence` in `categories.yaml` was ignored, contradicting "the group can only add
confidence, never lower a bar" (QA nit in #8, left as is).

## Decision

A member's threshold is the account's `min_confidence` for it, else the category's own, the same
`threshold` used everywhere else. It counts when that is at most the floor.

## Alternatives considered

- Keep account-level only: the category setting would mean different things in different places.

## Consequences

- No change with the shipped thresholds (0.80 and 0.90 against a 0.90 floor). A category raised
  above 0.90 in `categories.yaml` stops counting toward other categories' group archives.
