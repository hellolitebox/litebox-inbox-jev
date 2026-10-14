# 0011. Never unsubscribe through groups or discussion lists, nor from known contacts

- Status: Accepted
- Date: 2026-10-13
- PR: #12 (QA rounds 1–2); relayed mail in #13

## Context

A pitch relayed by the Google Group `hello@` carried
`List-Unsubscribe: <mailto:googlegroups-manage+…+unsubscribe@googlegroups.com>`: unsubscribing would
have removed the user from `hello@`. groups.io and Mailman also rewrite `From` (so DMARC passes) and
send a valid `List-Unsubscribe`. Separately, a vendor rep the user already works with can send an
upsell through a sales tool with RFC 8058 headers; if Jev calls it cold outreach, the opt-out can
cover all of that company's mail.

## Decision

- `policy._group_list`: no unsubscribe when the email has a `List-Post` header, an unsubscribe
  target on a Google Groups host, a sender or `List-Id` on the account's own domain, or was relayed
  by a Google Group (trusted or not).
- `unsubscribe_known_contacts: false` (on `cold_outreach`): no unsubscribe when `previous_contact`
  is `wrote_to_sender` or `wrote_to_their_domain`, on the Jev path and on the rule path. The email
  is still archived. Such answers never count toward an auto rule, so these senders stay on the Jev
  path.

## Alternatives considered

- Recognise only Google Groups targets: misses other list software (QA round 1, finding 1).
- Check known contacts only when an auto rule is earned: misses relationships that start later, and
  manual or correction rules (QA round 2).
- Also block when the contact check fails (`None`): one failed search would stop every outreach
  unsubscribe. `run` goes ahead under the other checks; `unsubscribe-retry` holds the row
  ([0014](0014-unsubscribe-retry-same-checks.md)).

## Consequences

- Rule hits that could unsubscribe run 1–2 sent-mail searches, only when the answer can change the
  outcome.
- `marketing` still unsubscribes known contacts (its flag is the default `true`).
