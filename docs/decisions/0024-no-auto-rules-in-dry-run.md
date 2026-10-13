# 0024. No automatic rules are learned in `dry_run`

- Status: Accepted
- Date: 2026-10-13
- PR: #21

## Context
`record_auto` ran in every mode. A trial in `dry_run` (the default `MODE`) could activate auto
rules, which then applied without Jev, and in `act` archived, spammed and unsubscribed, as soon as
the mode changed. Dry runs add no labels, so nobody could correct those answers first.

## Decision
`record_auto` runs only in `label` and `act`. In `dry_run` Jev's answers are stored on the rows
and nothing is learned.

## Alternatives considered
- Learn in dry_run but keep the rules inactive until a label/act pass: more state for no gain;
  switching modes reprocesses dry-run rows anyway, so they count then.
- Also skip `label`: in `label` you see the labels and corrections reset rules, so learning there
  is what makes `act` cheaper on day one.

## Consequences
- Switching from `dry_run` to `label`/`act` starts learning from zero; the dry-run rows are sent
  to Jev again on that pass and count then.
- Rules already learned in an earlier dry run are kept (they can't be told apart); review them
  with `inbox-jev rules list --all`. Production has run in `act` for weeks.
