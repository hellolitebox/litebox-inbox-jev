# 0010. Unsubscribe only from verified senders, via `List-Unsubscribe`

- Status: Accepted
- Date: 2026-10-13
- PR: initial design; #7; #12 (cold outreach, rule path); #13 (topmost `Authentication-Results`)

## Context
An unsubscribe is irreversible and, to an unverified sender, only confirms that the address is
live. Cold-outreach sequences (Instantly, Smartlead…) send valid RFC 8058 headers, pass DMARC and
stop the sequence on unsubscribe; one kept sending follow-ups that were archived one by one.

## Decision
- Unsubscribe only for `archive_unsubscribe` categories (`marketing`, and since #12
  `cold_outreach`, whose threshold went from 0.80 to 0.90), at the category's own confidence, in
  `act` mode and `act_categories`.
- Only with `dmarc=pass` in the topmost `mx.google.com` `Authentication-Results` and a
  `List-Unsubscribe` header. Never from links in the body.
- RFC 8058 one-click `POST` to a public https host (no redirects, no private IPs), else
  `mailto:`, else `manual_pending` for the user to click.
- Once per list, at most `MAX_UNSUBSCRIBES_PER_RUN` per run.
- The rule path runs the same sender checks: a rule skips Jev, not the checks.

## Alternatives considered
- Follow unsubscribe links in the body: unverifiable, phishing-prone, and they often confirm the
  address.
- Move unverified senders to Spam: rejected in [0005](0005-spam-narrowed-to-fraud.md).
- Keep cold outreach archive-only: the sequence keeps coming.

## Consequences
- 32 of 47 archived cold-outreach emails had no `List-Unsubscribe`: they are archived (and can
  earn archive rules) but never unsubscribed.
- Accounts that already listed `cold_outreach` in `act_categories` started unsubscribing on
  deploy, including through existing `cold_outreach` rules.
- The guards against groups and known contacts are in
  [0011](0011-never-unsubscribe-groups-or-known-contacts.md).
