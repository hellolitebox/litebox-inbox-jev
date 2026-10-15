# 0022. DMARC passes only on Gmail's own Authentication-Results

- Status: Accepted (the header parsed per RFC 8601: amended by
  [0043](0043-authentication-results-parsed-per-rfc-8601.md); ARC read for a trusted group's author,
  on Gmail's own `arc=pass`: amended by
  [0050](0050-group-relayed-mail-carries-its-authors-authentication.md))
- Date: 2026-10-13
- PR: #21

## Context

`dmarc_passed` trusted the topmost `mx.google.com` `Authentication-Results`, but when there was none
it fell back to every header present. Those can be written by the sender or any relay before Gmail,
so `dmarc=pass` could be forged on exactly the emails where Gmail's verdict was missing. DMARC gates
every unsubscribe ([0010](0010-unsubscribe-only-verified-senders.md)) and the Google Groups author
recovery ([0012](0012-group-author-trust-opt-in.md)).

## Decision

Without an `mx.google.com` header DMARC doesn't pass. Gmail stamps every inbound email, so a missing
header means an unusual delivery path, not a reason to trust someone else's verdict.

## Alternatives considered

- Keep the fallback for "unusual delivery paths": no known path needs it, and it is a spoofing hole
  on an irreversible action.
- Trust ARC headers: more parsing, and a forged chain is still possible without validating
  signatures.

## Consequences

- Such senders are unverified: archived only, never unsubscribed, and a group without the header
  never has its author recovered.
- On the production account, 1 of the last 400 received emails had no `mx.google.com` header, and
  the old fallback wouldn't have passed it either: no outcome changed.
