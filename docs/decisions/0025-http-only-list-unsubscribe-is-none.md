# 0025. An `http://`-only `List-Unsubscribe` counts as no header

- Status: Accepted
- Date: 2026-10-13
- PR: #21

## Context

`parse_list_unsubscribe` keeps only `https://` and `mailto:` URIs. A header with nothing but an
`http://` URL therefore counted as no header: the sender is unverified (archived only,
`downgraded_to_spam`) and the URL is never offered as a `manual_pending` link. This was documented
as a gap, not decided.

## Decision

Keep it, on purpose. No one-click `POST` and no manual link over plain http: the request can be read
or rewritten on the way, a `manual_pending` link sends the user to it, and an unsubscribe only
confirms the address. The sender keeps its category and is archived, as any unverified sender
([0010](0010-unsubscribe-only-verified-senders.md)). `is_mailing_list` in the Jev state also ignores
it (`List-Id` alone decides), as before.

## Alternatives considered

- Offer the http URL as `manual_pending`: the digest would link users to unencrypted unsubscribe
  pages that a network attacker can swap.
- Upgrade the URL to `https://`: often a different or missing endpoint, and the sender didn't offer
  it.

## Consequences

- No behaviour change. The choice is now explicit in `signals.parse_list_unsubscribe`, the spec
  ([4.3](../how-it-decides.md#43-list-unsubscribe-one-click-and-mailto)), EX-11 and tests.
