# 0032. Machine addresses make no known contact; a no-reply email's reply signal is judged like a stranger's

- Status: Accepted (registrable domain refined by
  [0037](0037-public-suffix-list-for-suffixes-and-registrable-domains.md); the 25-email sample by
  [0040](0040-domain-scan-kept-across-runs.md))
- Date: 2026-10-13
- PR: #33

## Context

"Guillermo, you have 107 new lead suggestions" from `messages-noreply@linkedin.com` (Jev:
notification 0.62, below its 0.80; archive categories 0.98 together, above the group's 0.90;
`needs_reply` 0.75) stayed in Review with `Jev/Needs Reply` instead of being archived. The
needs-reply veto on the archive group fired because `previous_contact` was `wrote_to_their_domain`:
the sent folder has about 12 emails from 2021 to `m-a175…@bounce.linkedin.com`, replies to LinkedIn
notifications. `in:sent to:linkedin.com` matched them, so every LinkedIn email counted as a known
contact (ADR 0008: any reply signal then blocks the group). The same happens for any service whose
notifications accept replies (GitHub's `reply+…@reply.github.com`, ticketing systems, bounce
addresses).

## Decision

One set of pure address helpers in `signals.py`, used by two guards:

1. **Writing to a machine address doesn't make a known contact.** `signals.is_automated_address`
   recognises no-reply local parts, bounce / VERP / reply-token local parts (a `bounce-` / `reply-`
   token needs 3+ digits, so `bounce-rentals@` or `reply-all2@` stay people), `mailer-daemon`,
   `postmaster`, and hosts under a `bounce.`, `bounces.` or `reply.` subdomain of their registrable
   domain. That is the last two labels, or the last three under a two-letter TLD whose second-level
   label has at most 4 characters (`reply.com.ar`, `reply.co.uk`, `bounces.com.br` are companies,
   not subdomains); in the domain search the searched domain's own labels never count.
   `triage.previous_contact` doesn't search for a machine sender itself, and `Gmail.has_written_to`
   for a domain reads the `To`/`Cc`/`Bcc` headers (metadata only) of the newest 25 sent matches, one
   at a time, and counts the domain only when an email went to a recipient on it that isn't a
   machine (`signals.wrote_to_person_at`). An email with no recipient on the domain still counts
   (Gmail matched it for a reason we can't see), and so does a sample of 25 machine-only emails with
   more behind it.
2. **A no-reply email's known contact doesn't veto the archive group.** `Signals.no_reply` is true
   when the sender is a clear no-reply address (`noreply`, `no-reply`, `donotreply`, `do-not-reply`,
   `no_reply`, `do_not_reply`, alone or as one piece of the local part, e.g. `messages-noreply@`)
   and every `Reply-To` is too. For such an email the veto ignores `previous_contact` and applies
   only through the keep categories (`archive_group_reply_keep_probability`), as for a stranger.

When unsure, an address is **not** a machine. A false "machine" makes a real contact look unknown,
which archives more and lets `cold_outreach` unsubscribe from them: the dangerous direction. So
`reply@company.com`, `reply-team@`, `notifications@`, `info@`, `support@` and opaque tokens on an
ordinary host are left as people.

## Alternatives considered

- **Exclude patterns in the Gmail query** (`-to:bounce.linkedin.com`): Gmail search has no wildcards
  or regular expressions on addresses, and its `to:` matching is loose (tokens, display names), so
  the patterns can't be expressed generally or reliably. Reading the headers is exact.
- **A public suffix list** (`publicsuffixlist`, `tldextract`) instead of the two-letter-TLD rule:
  none is in `uv.lock` today, so it would be a new runtime dependency (bundled snapshot only, never
  fetched at runtime) for a heuristic whose errors on the rule's side are already safe: treating a
  short name under a ccTLD as a suffix only means fewer machines. The rule's dangerous misses need a
  deeper or longer suffix (`wa.gov.au`, `kommune.no`) *and* a company named `reply` / `bounce`, and
  only affect the sender's own address search (the domain search never counts the searched domain's
  labels). Revisit if that shows up in real mail.
- **Read every sent match**: unbounded cost for domains with years of notification replies. 25 reads
  at most (130 quota units) per domain and run, stopping at the first person.
- **Count a machine-only sample with more behind it as "never"**: it would turn a real contact
  written to once, long ago, into a stranger whenever newer mail went to machines. Counting it keeps
  today's behaviour for that rare case (documented as a known gap).
- **Drop the whole needs-reply veto for no-reply senders**, as first proposed: Jev's question is "a
  reply or an action from me personally"; a no-reply email can still need an action, and the keep
  categories are the signal for that. Only the known-contact half is meaningless without a reply
  path, so only that half is dropped.
- **Drop `Jev/Needs Reply` on no-reply emails**: for the same reason the label stays. It only goes
  on emails kept in the inbox, so it never archives anything; dropping it would hide "action needed"
  notices (a review request, a signature) from the digest.
- **Treat any `Reply-To` as replyable only when it looks like a person**: a reply-by-email token
  (`reply+…@reply.github.com`) does reach a conversation. Only a `Reply-To` that is itself a clear
  no-reply address keeps the email no-reply.

## Consequences

- Replies to notifications no longer make the whole service a known contact: its routine mail with a
  reply signal is archived through the group again, and `cold_outreach` from it can be unsubscribed
  if the other checks pass.
- The sent-mail `list` and each metadata read retry a 429, 5xx or rate-limit 403 with backoff
  (`SINGLE_RETRIES`), so a transient error rarely turns a contact into "unknown".
- A domain search costs one extra metadata read for a real contact's domain, and up to 25 for a
  domain written to only at machine addresses (per account and run, cached).
- Sent mail recipients are read (headers only); nothing new is stored or logged.
- A manual rule for `messages-noreply@linkedin.com`, added in production as a stopgap, can be
  deleted after deploy.
