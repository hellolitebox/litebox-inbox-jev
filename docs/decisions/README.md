# Architecture decision records

Short records of the design decisions behind inbox-jev's behaviour: the context, the decision,
the alternatives that were rejected and the consequences. The behaviour itself is specified in
[`docs/how-it-decides.md`](../how-it-decides.md).

Add a record when a PR makes a design decision (a new rule, a guard, a trade-off between safety
and coverage), in the same PR. Name it `NNNN-short-title.md` with the next number and the
sections Context, Decision, Alternatives considered, Consequences. Don't rewrite an accepted
record when the decision changes: add a new one and mark the old one `Superseded by NNNN`.

| # | Decision | Status | PR |
|---|---|---|---|
| [0001](0001-needs-reply-only-on-kept-emails.md) | Flag "needs reply" only on emails that stay in the inbox | Accepted | #2 |
| [0002](0002-container-uid-not-1000.md) | Run the container as a configurable uid, not 1000 | Accepted | #4 |
| [0003](0003-gradual-act-rollout.md) | Roll out `act` gradually with `act_categories` | Accepted | #6 |
| [0004](0004-cold-outreach-archived-not-spammed.md) | Cold outreach is its own category, archived, not spammed | Accepted | #7 |
| [0005](0005-spam-narrowed-to-fraud.md) | Spam means fraud; unverified marketing keeps its category | Accepted | #7 |
| [0006](0006-archive-group-by-action-confidence.md) | Archive by action confidence (the archive group) | Accepted | #8 |
| [0007](0007-action-required-keep-category.md) | An `action_required` keep category | Accepted | #8, #11 |
| [0008](0008-needs-reply-veto-with-keep-signal.md) | Needs-reply veto only with a keep signal or a known contact | Accepted | #9 |
| [0009](0009-stub-plain-text-uses-html.md) | Use the HTML when `text/plain` is a stub | Accepted | #10 |
| [0010](0010-unsubscribe-only-verified-senders.md) | Unsubscribe only from verified senders, via `List-Unsubscribe` | Accepted | #7, #12, #13 |
| [0011](0011-never-unsubscribe-groups-or-known-contacts.md) | Never unsubscribe through groups, nor from known contacts | Accepted | #12, #13 |
| [0012](0012-group-author-trust-opt-in.md) | Trust a group's author only for `group_domains` | Accepted | #13 |
| [0013](0013-sender-rules-and-auto-rule-guards.md) | Sender rules and auto-rule guards | Accepted | #8, #12, #13 |
| [0014](0014-unsubscribe-retry-same-checks.md) | `unsubscribe-retry` applies the same checks as `run` | Accepted | #15 |
| [0015](0015-user-edits-hands-off.md) | Learn from corrections; leave edited emails alone | Accepted | #6, #15 |
| [0016](0016-reproducible-build.md) | Reproducible build with `uv.lock` | Accepted | #16 |
| [0017](0017-error-retry-backoff-and-cap.md) | Retry failed emails with a backoff, give up after 8 failures | Accepted | #20 |
| [0018](0018-one-click-pinned-to-validated-ip.md) | One-click unsubscribe connects to the validated IPs | Accepted | #20 |
| [0019](0019-rules-to-removed-categories-ignored.md) | A rule to a removed category is ignored, not deleted | Accepted | #21 |
| [0020](0020-flags-for-allowlist-and-keep-rules.md) | Allowlist and keep rules ask Jev for the flags only | Accepted | #21 |
| [0021](0021-archive-group-member-threshold.md) | An archive-group member counts against its own threshold | Accepted | #21 |
| [0022](0022-dmarc-only-from-gmail.md) | DMARC passes only on Gmail's own `Authentication-Results` | Accepted | #21 |
| [0023](0023-one-order-for-unsubscribe-checks.md) | One order for the unsubscribe checks | Accepted | #21 |
| [0024](0024-no-auto-rules-in-dry-run.md) | No automatic rules are learned in `dry_run` | Accepted | #21 |
| [0025](0025-http-only-list-unsubscribe-is-none.md) | An `http://`-only `List-Unsubscribe` counts as no header | Accepted | #21 |
| [0026](0026-one-unsubscribe-gate-for-every-path.md) | One unsubscribe gate and one act gate for every path | Accepted | #22 |
| [0027](0027-restore-skips-deleted-emails.md) | `restore` skips emails deleted in Gmail | Accepted | #22 |
| [0028](0028-qa-verdict-required-check.md) | QA's verdict is a required check tied to the head commit | Accepted | #23 |
