# 0019. A rule to a removed category is ignored, not deleted

- Status: Accepted
- Date: 2026-10-13
- PR: #21

## Context

A sender rule stores a category key. When that key is removed from `categories.yaml` (a category
renamed or merged), `decide` raised `classification is required…` for every email from that sender:
each was stored as `error`, retried with the backoff ([0017](0017-error-retry-backoff-and-cap.md))
and gave up after 8 failures, untouched and unlabeled. `unsubscribe-retry` already ignored such a
rule.

## Decision

- The first active rule decides (exact address, then `@domain`), as before. If its category isn't in
  `categories.yaml`, the sender has **no rule**: the email goes through Jev. Lookup never falls
  through to the `@domain` rule behind a stale exact rule: the exact rule is the user's exception to
  the domain rule, and applying the domain rule would undo it without Jev, in `act` possibly
  irreversibly (an unsubscribe). One helper, `rules.match`, returns `(rule, stale)`; `policy.decide`
  also treats an unknown `rule_category` as absent (defence in depth).
- Each stale rule that decides is logged once per invocation
  (`sender rule ignored: unknown category`, with `sender_key`, `category` and `rule_source`), not
  once per email.
- The rule stays in the DB, active. An **auto** rule in that state is relearned: auto rules are
  keyed on the exact address, so a stale one is always the first match, Jev decides
  (`source == "jev"`), and the next counted answer replaces its category with `hits = 1` (pending),
  as for a pending rule.
- `run` (`triage.rule_for`), `unsubscribe-retry` and `scripts/dry_classify.py` all use
  `rules.match`.

## Alternatives considered

- Deactivate the rule on sight (the proposed resolution): a typo or a temporarily removed category
  in `categories.yaml` would silently wipe manual and correction rules, which can't be relearned; it
  would also be a DB write from `dry_run` and from read-only checks. The warning plus `rules delete`
  keeps that decision with the user.
- Fail config load when a rule points to a missing category: one stale rule would stop every run of
  every account.
- Skip the stale rule and fall through to the `@domain` rule (this PR's first version): an exact
  rule exists precisely because the domain rule was wrong for that sender, so it would archive (and
  maybe unsubscribe) that sender's mail without Jev. It also left a stale auto rule shadowed by a
  domain rule unrelearnable, warning on every run.
- Keep raising: an outage for that sender's mail until someone reads the error.

## Consequences

- Removing a category no longer breaks anything; its rules' senders cost a Jev call each until their
  rules are deleted (or, for auto rules, relearned), even when a `@domain` rule also matches them.
- No production rule was affected at deploy (0 active rules to unknown categories).
