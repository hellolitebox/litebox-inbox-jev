# 0007. An `action_required` keep category for automated mail that needs you

- Status: Accepted
- Date: 2026-10-12
- PR: #8 (held-message wording in #11)

## Context
Automated "action needed" mail (approve a payment, update a card, verify a domain) had no
category: it went to Review or was archived as notification or transactional. Later (#11) a Google
Groups held-spam report came back as notification 0.52 / action_required 0.40, while real
pending-approval notices must stay visible: they are the only way a non-member's message (for
example a prospect writing to `hello@`) gets delivered.

## Decision
`action_required` (`Jev/Action`, `keep`, threshold 0.0) for automated email that needs me to do
something, explicitly including a held message pending my approval and excluding receipts, FYI
alerts and held-spam reports (which are `notification`). The digest gets a section per keep
category other than `important`. The transactional and notification criteria say "nothing left
to do".

## Alternatives considered
- Put these under `important`: mixes personal mail with machine alerts.
- A sender rule for the group's spam reports: the sender is the group address, which also carries
  real client mail.

## Consequences
- Auto rules must not archive senders that mix routine and action-needed mail
  ([0013](0013-sender-rules-and-auto-rule-guards.md)).
- Keep categories feed the keep probability used by the needs-reply veto and the auto-rule keep
  signal.
