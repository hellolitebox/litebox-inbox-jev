# 0005. Spam means fraud; unverified marketing keeps its category

- Status: Accepted
- Date: 2026-10-12
- PR: #7 (QA round 1, finding 1)

## Context

Once `spam` was narrowed to scams, phishing, impersonation and forged authentication, the old
behaviour of sending unverifiable marketing (no `dmarc=pass` or no `List-Unsubscribe`) to `spam`
became wrong: a legitimate promo would land in the Spam folder under "fraud" criteria, and the
`Jev/Spam` review would mix fraud with promos.

## Decision

An `archive_unsubscribe` email from an unverified sender keeps its category and label, is archived,
and is never unsubscribed or moved to Spam. The `downgraded_to_spam` column keeps its historical
name (no migration) and now means "unsubscribe skipped: unverified sender". Only what Jev itself
calls `spam`, at ≥ 0.90, goes to Spam.

## Alternatives considered

- Keep the downgrade and document it: enabling `spam` in `act_categories` would also spam legitimate
  promos.
- Rename the column: needs a migration for deployed databases, for no behaviour change.

## Consequences

- Listing `spam` in `act_categories` only spams Jev's own spam answers.
- Readers must know the column's real meaning (see the glossary in
  [how-it-decides](../how-it-decides.md#6-glossary)).
