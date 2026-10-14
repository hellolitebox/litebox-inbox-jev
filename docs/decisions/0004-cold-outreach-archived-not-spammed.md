# 0004. Cold outreach is its own category, archived, not spammed

- Status: Accepted
- Date: 2026-10-12
- PR: #7 (became `archive_unsubscribe` in #12, see
  [0010](0010-unsubscribe-only-verified-senders.md))

## Context

`spam` mixed scams with cold outreach from real people, so Jev split its answers between `spam` and
`important`: 28 outreach emails sat in the inbox below the 0.90 spam threshold (0.4–0.88). Outreach
isn't malicious, while the Spam folder auto-deletes after 30 days.

## Decision

A `cold_outreach` category (`Jev/Outreach`) for unsolicited sales or partnership pitches written to
look personal. It is archived, never moved to Spam. Its threshold was raised from the proposed 0.70
to 0.80 in QA (a real first-contact lead looks a lot like a pitch), and to 0.90 in #12 when it
started to unsubscribe.

## Alternatives considered

- Keep outreach under `spam`: wrong folder, auto-deletion, and a split vote that keeps it in Review.
- Fold it into `marketing`: different senders (people, not companies) and a different risk (a
  "pitch" from a real contact).

## Consequences

- With real Jev, 24 low-confidence `spam` rows became `cold_outreach` at 0.81–1.0, and no real
  conversation was reclassified.
- Old `spam` sender rules for outreach senders had to be deleted at deploy: a rule skips Jev, so it
  keeps the old meaning.
