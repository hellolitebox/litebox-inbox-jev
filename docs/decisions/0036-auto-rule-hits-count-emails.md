# 0036. Auto-rule hits count emails, not answers

- Status: Accepted
- Date: 2026-10-13
- PR: #36

## Context
`rules.record_auto` counts each confident, consistent Jev answer toward an automatic rule
(`auto_rule.hits` = 3, [0013](0013-sender-rules-and-auto-rule-guards.md)). An email seen in a
weaker mode is processed again in a stronger one (`label`, then `act`), and `reprocess` sends
`act` rows back through `label`. Each of those passes asks Jev again, and each answer counted:
two emails seen in `label` and again in `act` gave a sender 4 hits, an active rule, on half the
evidence the setting asks for. `dry_run` was already excluded
([0024](0024-no-auto-rules-in-dry-run.md)).

## Decision
A hit is one email. `triage.process` reads, before overwriting the row, whether an earlier pass
already offered Jev's answer on this email to `record_auto`: its stored row holds a Jev decision
(`source = jev`) from `label` or `act`. If so, `record_auto(count=False)` adds no hit, creates no
rule and changes no category. It still applies the resets: a keep answer or keep signal restarts
a pending archive rule, and an answer that would count, in another category than the pending
auto rule's, restarts it at 0 hits without switching its category (EX-31): Jev disagreeing with
itself about the same email is weak evidence that the sender is consistent.

The email is still sent to Jev again: reusing the stored answer when only the mode got stronger
was considered and declined (an earlier decision of the user's), and this ADR doesn't change it.

## Alternatives considered
- Store a per-email "counted" flag (a new `messages` column): exact (a first answer that didn't
  count would let a later one count), but a schema change, and rows from before it would all read
  "not counted" and count again once.
- Skip `record_auto` entirely for a re-answered email: simpler, but loses the reset, the side
  that keeps more mail going through Jev.
- Leave the hits alone when the re-answer is in another category: keeps "one email = one hit"
  but ignores the disagreement. Switching the rule to the new category at 1 hit (as a counted
  answer does) would count the email twice.
- Reuse the stored classification when only the mode changed: saves a Jev call, declined (see
  above).

## Consequences
- An auto rule needs `auto_rule.hits` different emails from the sender, whatever the mode
  history.
- A re-answered email never counts, even when its first answer didn't (e.g. 0.93 in `label`,
  0.97 in `act`); and a `dry_run` row whose `label` pass failed reads as already learned from.
  Both err toward fewer rules: the sender needs one more email.
- A re-answer that disagrees with a pending auto rule restarts it, so a sender Jev isn't sure
  about needs `auto_rule.hits` more emails after the disagreement.
