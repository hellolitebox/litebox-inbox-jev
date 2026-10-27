# 0058. Spam counts toward the archive group, which still never spams

- Status: Accepted (amends [0006](0006-archive-group-by-action-confidence.md))
- Date: 2026-10-27
- PR: —

## Context

The archive group ([0006](0006-archive-group-by-action-confidence.md)) archives an email whose top
category is an archive category below its threshold when the archive categories together reach
`archive_group_min_confidence` (0.90): Jev is sure the email goes, just not under which label. Spam
was left out of the sum together with the rule that the group never moves anything to Spam, but
those are two different things. The second protects a reversible outcome; the first throws away
evidence that the email should leave the inbox.

A cold pitch on 2026-10-26 ("Re:" subject with no earlier message) came back cold_outreach 0.70 /
spam 0.30, everything else 0.00. Every category Jev considered takes the email out of the inbox, yet
it sat in `Jev/Review` because the group counted 0.70.

In production (`ggonzaleza@litebox.ai`, `act` mode since 2025-11-17), 69 emails are in Review with
an archive category on top. 9 of them would leave Review if spam counted (cold_outreach 1, marketing
1, newsletter 1, notification 6). The six notifications are near-even splits with spam (0.49 / 0.48,
0.50 / 0.44, …), with important + action_required at 0.02–0.05 and needs_reply at 0.15–0.52.

## Decision

`spam` counts toward the archive group's sum under the same member rules as any other member: its
threshold (account `min_confidence`, else the category's own, 0.90) must be at most the floor, and
with `act_categories` it must be listed. Nothing else changes:

- the top category must still be an archive category (`archive` or `archive_unsubscribe`): an email
  with spam on top stays where it is today (Spam at ≥ 0.90, Review below);
- the email is archived under the top category's label, never moved to Spam and never unsubscribed;
- floor, needs-reply veto, and "a group archive never counts toward an auto rule" stay as they are.

## Alternatives considered

- Leave it: pitches and phishing-looking notifications split with spam stay in Review, which is
  where the owner looks for mail that might matter.
- Let a spam-top email below 0.90 use the group too (archived under its best archive category): the
  label would be a category Jev ranked second, and a spam-top email is where the owner most wants to
  look.
- Send to Spam when spam ≥ 0.50 and spam + cold_outreach ≥ 0.90, guarded by known contacts and group
  mail: no email in production would have qualified. The 5 Review emails with spam on top are split
  with action_required, transactional or notification, never mostly with outreach. Can be revisited
  if that changes.
- Lower `cold_outreach`'s threshold: also archives cold_outreach / important splits, which 0004 and
  0008 kept in Review on purpose.

## Consequences

- About 9 of 69 current Review emails would have been archived instead; none moved to Spam, none
  unsubscribed.
- A phishing email dressed as a notification and split with spam is archived as a notification
  rather than shown with `Jev/Review`. Archiving it removes it from sight, which is the safer side;
  it is not in Spam, so Gmail doesn't delete it after 30 days.
- A real notification Jev half-suspects is archived instead of reviewed; notifications already
  archive at 0.80 on their own, and a keep signal or a known contact with a reply signal still
  vetoes the group.
- Code: `policy._archive_group_confident` (and its docstring), the `categories.yaml` comment on
  `archive_group_min_confidence`, `docs/how-it-decides.md` (archive-group table, glossary), a new
  `EX-NN` with this email's split, and `docs/guide.md` where it explains Review.
