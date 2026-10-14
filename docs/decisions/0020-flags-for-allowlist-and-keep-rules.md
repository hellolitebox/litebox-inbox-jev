# 0020. Allowlist and keep rules ask Jev for the flags only

- Status: Accepted
- Date: 2026-10-13
- PR: #21

## Context

Rules and the allowlist skip Jev, so their emails never got `Jev/Needs Reply` or the tag labels
([0001](0001-needs-reply-only-on-kept-emails.md),
[0013](0013-sender-rules-and-auto-rule-guards.md)). That hits exactly the wrong senders: a colleague
on the allowlist, a client whose email you pulled back to the inbox (a `correction` rule to
`important`), or a sender that earned an `important` auto rule all dropped out of the digest's
"needs reply" section.

## Decision

- When the allowlist or a rule to a **keep** category (`important`, `action_required`) decides, Jev
  is asked anyway (`policy.flags_only`). Only its `needs_reply` and tags are used: the category,
  confidence (1.0) and source stay the allowlist's or the rule's. Jev's category is stored in
  `jev_category`, with `probabilities`, `needs_reply` and `tags`, for audit.
- Such answers never count toward an auto rule (`record_auto` only sees `source == "jev"`).
- No sent-mail search for them: `previous_contact` can't change a keep decision, so it is left out
  of the Jev state (as when it is unknown), saving 1–2 Gmail searches per email.
- A failed flags-only call doesn't fail the email: `triage.flags_classification` catches the Jev
  error (`JevError`, the SDK's `TypeSafeError`), logs `flags-only Jev call failed` (account, id,
  error type; no content) and the email is decided with no Jev answer, as before this ADR: labeled
  by the allowlist or the rule, without Needs Reply or tags. Jev-decided emails keep the
  error/backoff path ([0017](0017-error-retry-backoff-and-cap.md)).
- Rules to **archive** or **spam** categories keep skipping Jev: their email leaves the inbox and
  would never get Needs Reply ([0001](0001-needs-reply-only-on-kept-emails.md)); tags on those
  aren't worth a call per email.

## Alternatives considered

- Leave it (the gap): the senders that matter most lose Needs Reply.
- Ask Jev for every rule decision: the archive rules are where the savings are (119 of 138 rule or
  allowlist decisions in the last 7 days on the production account were archive rules).
- A separate, cheaper "needs reply only" question: TypeSafe prices per input token, and the email is
  the input, so one question costs about as much as the full call.
- A per-category or per-account switch: no account needs it yet; easy to add if one does.
- Let a flags-only failure fail the email like any Jev failure (this PR's first version): the
  allowlist ("always important") would depend on a service it didn't need, and during an outage
  longer than the backoff window (~10 h) or on a per-email Jev error, the email would give up after
  8 attempts and never be labeled without `retry-errors`.

## Consequences

- Cost: Jev is priced per input token, roughly one email's state (headers plus up to `BODY_CHARS`,
  3000 characters) per call. On the production account in the 7 days before this change there were
  269 Jev calls, 3 allowlisted emails and 0 keep-rule emails (2 active keep rules): about +1% calls.
  An account with a large allowlist (a whole company domain) pays one call per colleague email.
- During a Jev outage, allowlisted and keep-rule emails are labeled as before this ADR, just without
  Needs Reply or tags, and they aren't asked again later (they are `done`). The warning makes the
  miss visible.
- Supersedes the "rule decisions carry no `needs_reply` or tags" consequences of 0001 and 0013.
