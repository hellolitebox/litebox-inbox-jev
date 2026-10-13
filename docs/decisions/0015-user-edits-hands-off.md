# 0015. Learn from corrections; leave edited emails alone

- Status: Accepted
- Date: 2026-10-12
- PR: initial design; #6 (processing again and user edits); #15 (shared `_user_edit`)

## Context
Processing an email again (`label` → `act`, `reprocess`) touches emails the user may already have
handled: relabeled, starred, or with `Jev/Needs Reply` removed. Overwriting that would undo the
user's own decision.

## Decision
- A changed Jev category label, an accepted Review suggestion (`Jev/Review` removed), or an
  archived email pulled back to the inbox is a **correction**: a `correction` sender rule on the
  stored sender.
- Otherwise, a starred email (even after a correction), a removed label of ours, or an added Jev
  label we didn't set means **hands off**: no Gmail change, not revisited in that mode.
- `restore` undoes labels and archive/spam; the row is never processed again and isn't a
  correction.

## Alternatives considered
- Apply the new decision anyway: overwrites the user's own triage.
- Learn every edit as a correction: a removed `Jev/Needs Reply` says nothing about the category.

## Consequences
- Corrections key on the exact stored sender (the author for trusted groups).
- An email pulled back to the inbox without a new label means `important`, so that sender's later
  email skips Jev and gets no `Jev/Needs Reply`.
