# 0013. Sender rules, and when Jev's answers may become automatic rules

- Status: Accepted (amended by [0020](0020-flags-for-allowlist-and-keep-rules.md), [0023](0023-one-order-for-unsubscribe-checks.md), [0024](0024-no-auto-rules-in-dry-run.md))
- Date: 2026-10-13
- PR: initial design; #8 (keep guards); #12 (unverified senders, known contacts); #13 (author key)

## Context
Rules skip Jev, which saves calls but freezes a verdict. Senders that mix routine and
action-needed mail from one address (`noreply@github.com`: notifications plus "token expiring";
a bank: receipts plus approval requests) would earn an archive auto rule and then archive the
next approval request without asking Jev (#8 QA round 1, finding 1).

## Decision
- Manual rules (address or `@domain`), correction rules (exact sender) and auto rules (exact
  sender). Lookup: exact address, then `@domain`; the allowlist wins over rules.
- An auto rule activates after `auto_rule.hits` (3) consistent answers at ≥ 0.95. An answer
  doesn't count when it is low confidence, an archive-group decision, an unsubscribe skipped for a
  known contact, or carries a keep signal (keep categories other than the decided one ≥ 0.02).
- A keep answer or a keep signal resets a pending auto rule that points to an archive category.
- Unverified senders may earn rules (since #12): the rule path re-runs the sender checks.
- `record_auto` never changes manual or correction rules.

## Alternatives considered
- Auto rules only for unsubscribe or spam senders: still wrong for mixed senders, and more Jev
  calls for clean ones.
- No auto rules: every email costs a Jev call and 1–2 sent-mail searches.

## Consequences
- Fewer auto rules form (more Jev calls), which is the safe direction.
- Existing active auto rules had to be reviewed at deploy; the guards only prevent new ones.
- Rule decisions carry no `needs_reply` or tags (superseded by
  [0020](0020-flags-for-allowlist-and-keep-rules.md) for keep rules and the allowlist).
