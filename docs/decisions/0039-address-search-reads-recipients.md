# 0039. The address search reads the recipients of its matches

- Status: Accepted
- Date: 2026-10-14
- PR: #39

## Context

`previous_contact` asks Gmail `in:sent to:<sender>` and, until now, took any match as
`wrote_to_sender`. Gmail's `to:` is a search, not an address comparison: it matches words of
addresses and display names, so an email to someone with a similar address can match. A false
`wrote_to_sender` is a known contact: no `cold_outreach` unsubscribe, the needs-reply veto on the
archive group, and no auto rule in categories that spare known contacts. The domain search already
read recipients (ADR 0032); the address search, which also decides senders on free mail where no
domain search runs, didn't.

## Decision

The address search reads the `To`/`Cc`/`Bcc` headers (`format=metadata`, no body) of the newest
`gmail.SENT_ADDRESS_READS` = 10 matches, one at a time, and counts the first that went to the
sender's own address (`signals.wrote_to_address`). Addresses compare by `signals.mailbox_key`:
lowercased and trimmed; for consumer Gmail (`gmail.com`, `googlemail.com`) also without dots and
without a `+tag` in the local part, on `gmail.com`, because that is how Gmail delivers. No other
provider's rules are applied.

It stays on the safe side where it can't tell: an email with no recipient address in those headers
counts (Gmail matched something they don't show, as in the domain search), and 10 matches none of
which went to the sender count when Gmail has more. Retries (`num_retries=SINGLE_RETRIES`), 404
skips and the per-run cache are the domain search's.

## Alternatives considered

- **Plus-addressing and dots for every provider**: Outlook, Fastmail and Proton deliver `+tags` to
  the base mailbox, but a company domain may route `support+123@` to a ticket or `j.doe@` to someone
  else, and whether dots matter differs per provider and even per mailbox. Treating two addresses as
  one when they aren't makes a stranger a known contact (the less safe side for a check whose job is
  to reject loose matches), so only Gmail's own documented rules are applied. A missed match on a
  company domain still usually counts through the domain search.
- **Quote the address in the query** (`to:"lee@outlook.com"`): Gmail documents no exact-address mode
  for `to:`, so the result would still need checking; reading the headers is exact.
- **Read every match**: unbounded for a common token. Ten, newest first, stopping at the first hit,
  keeps a real contact at one read.

## Consequences

- A sender is a known contact only if a sent email went to their address (case-insensitive; Gmail's
  dots and `+tags` on consumer Gmail).
- Cost: the address search goes from one `list` to one `list` plus one metadata read for a known
  sender (10 at most), per sender and run. Unknown senders without matches cost the same as before.
- `jane+news@outlook.com` and `jane@outlook.com` are different contacts (documented known gap).
