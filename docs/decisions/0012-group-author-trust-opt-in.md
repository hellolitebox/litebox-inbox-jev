# 0012. Trust a group's original author only for `group_domains`

- Status: Accepted (once the author is the sender, its `dmarc_pass` and Jev's `sender_auth` are the
  author's, from Google's ARC record: amended by
  [0050](0050-group-relayed-mail-carries-its-authors-authentication.md))
- Date: 2026-10-13
- PR: #13 (QA round 1 blocker; round 2 findings on public providers and suffixes)

## Context

Google Groups rewrites `From` to the group and keeps the author in `X-Original-Sender`. Rules,
corrections and `previous_contact` were applied to whole groups: a pending auto rule
`hello@ → cold_outreach`, and an active `growth@ → notification` rule that archived everything sent
to `growth@`. But `Mailing-list` and `X-Original-Sender` are ordinary headers: anyone with a
DMARC-passing domain, or any `@googlegroups.com` group, can claim any author.

## Decision

- `signals.original_sender` records a candidate only with `dmarc=pass`, a
  `Mailing-list: list <addr>` naming the `From` address, and a valid `X-Original-Sender` different
  from it.
- `signals.resolve_relay` uses it as the sender only when the `From` domain is in the account's
  `group_domains` (or a subdomain). This is opt-in and empty by default, not even the account's own
  domain. Entries that are public providers, `googlegroups.com` or bare suffixes are rejected.
- The allowlist checks the author; a group itself is allowlisted only by its exact address in
  `allow_senders`. Everything keyed on the sender uses `triage.signals_for`.
- `inbox-jev senders backfill` fixes rows stored before.

## Alternatives considered

- Trust any DMARC-passing group: an attacker's domain could impersonate an allowlisted author (QA
  blocker).
- Trust the account's own domain by default: an account on an unlisted public provider would trust
  every user of that provider (QA round 2).

## Consequences

- Untrusted groups stay keyed on the group address, so an `allow_domains` entry matching the group's
  domain allowlists everything sent through it.
- Group mail never unsubscribes ([0011](0011-never-unsubscribe-groups-or-known-contacts.md)).
