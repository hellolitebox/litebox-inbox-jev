# 0001. Flag "needs reply" only on emails that stay in the inbox

- Status: Accepted
- Date: 2026-10-11
- PR: #2 (extended by #8 and #9)

## Context
The first dry run on a real inbox (30 emails) flagged sponsorship pitches and LinkedIn Jobs mail
with `Jev/Needs Reply`. Jev's `needs_reply` question ("does the sender expect a reply or an
action from me personally?") is answered "yes" by almost every pitch: cold outreach is written to
look like it expects a reply.

## Decision
`policy._add_flags` adds `Jev/Needs Reply` only when the decision neither archives nor spams the
email: keep categories, low-confidence (Review) emails and unknown categories. The policy's labels
are stored in `messages.decided_labels` in every mode, so the digest's "needs reply" section
works in `dry_run` too.

## Alternatives considered
- Flag every email above the threshold: the label and the digest section fill up with pitches.
- Raise the `needs_reply` threshold: doesn't separate pitches from real requests, which score just
  as high.

## Consequences
- An archived email never carries `Jev/Needs Reply`, even if Jev thought it expected a reply.
- Rule and allowlist decisions don't ask Jev, so they never get the label (see
  [how-it-decides, Known gaps](../how-it-decides.md#known-gaps)).
- The reply signal is still used on the archive group
  ([0008](0008-needs-reply-veto-with-keep-signal.md)).
