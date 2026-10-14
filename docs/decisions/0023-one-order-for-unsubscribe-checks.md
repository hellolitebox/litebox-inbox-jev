# 0023. One order for the unsubscribe checks, and flags that mean what they say

- Status: Accepted
- Date: 2026-10-13
- PR: #21

## Context

The Jev path and the rule path ran the sender checks in different orders. The Jev path set
`downgraded_to_spam` for any unverified `archive_unsubscribe` answer, even archive-group and Review
decisions where no unsubscribe was possible, and set `known_contact_kept` before the verified and
group checks; the rule path set each flag only when its check is what stopped an unsubscribe. The
same email could carry different flags depending on whether a rule decided it, and `record_auto`
read `known_contact_kept`, so an unverified known contact never earned a rule on the Jev path but
the flag said nothing true about it.

## Decision

- One helper, `policy._check_unsubscribe`, runs on both paths, only on a decision that would
  unsubscribe: unverified → `downgraded_to_spam`; else a group or discussion list; else a known
  contact in a category that spares known contacts → `known_contact_kept`.
- `downgraded_to_spam` keeps its meaning "unsubscribe skipped: unverified sender", and is now only
  set when that is true.
- `record_auto` no longer reads `known_contact_kept`. It gets `previous_contact` and never counts a
  known contact in a category that spares known contacts, verified or not, group or not. This keeps
  the guard of [0011](0011-never-unsubscribe-groups-or-known-contacts.md) and
  [0013](0013-sender-rules-and-auto-rule-guards.md) where it was strongest.

## Alternatives considered

- Align the flag as proposed and keep `record_auto` reading it: unverified and group-relayed known
  contacts would start earning `cold_outreach` archive rules, the case where Jev is most likely
  wrong. Taking the safer guard separately costs nothing.
- Move the rule path to the Jev path's order: the flag would keep meaning two things.

## Consequences

- No change in what is archived, spammed, unsubscribed or learned. Rows stored from now on have
  `downgraded_to_spam = false` on unverified archive-group and Review decisions.
- Since [0026](0026-one-unsubscribe-gate-for-every-path.md), `_check_unsubscribe` applies
  `policy.unsubscribe_gate`, which holds this order for every path, `unsubscribe-retry` included.
