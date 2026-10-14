# 0008. Needs-reply veto on the archive group only with a keep signal or a known contact

- Status: Accepted
- Date: 2026-10-13
- PR: #9 (the veto itself came from #8 QA round 1, finding 5; the known-contact condition from #9 QA
  round 1)

## Context

#8 made any `needs_reply ≥ 0.7` veto the archive group. A Constant Contact mass pitch ("Get Your
Free Consultation Today!": cold_outreach 0.67 / marketing 0.31, important and action_required 0.00,
needs_reply 0.79) stayed in Review. Every pitch "expects a reply".

## Decision

The reply signal blocks the group only when:

- `previous_contact` is `wrote_to_sender` or `wrote_to_their_domain` (then any reply signal blocks
  it), or
- the keep categories together reach `archive_group_reply_keep_probability` (0.05).

The setting must be at most `1 - archive_group_min_confidence` (rejected at load otherwise); `0`
restores the strict veto. `policy.decide` receives `previous_contact` as a keyword argument, so the
policy stays pure.

## Alternatives considered

- Keep the strict veto: mass pitches stay in Review.
- Drop the veto: a real contact's uncertain follow-up would be archived silently (QA's scenario: a
  vendor I emailed, cold_outreach 0.60 / marketing 0.36 / important 0.04).

## Consequences

- Strangers' pitches are archived through the group; known contacts' uncertain emails stay in Review
  with `Jev/Needs Reply`.
- When the sent-mail search fails (`None`), the relaxed rule applies.
