# 0006. Archive by action confidence (the archive group)

- Status: Accepted
- Date: 2026-10-12
- PR: #8 (QA round 1, findings 2–5)

## Context
25 of 54 inbox emails sat in `Jev/Review`. Most were automated mail split between two archive
categories (for example notification 0.58 / newsletter 0.42): Jev was sure the email should go,
just not under which label.

## Decision
When Jev's top category is an archive category below its threshold, and the archive categories
together reach `archive_group_min_confidence` (0.90), archive under the top category. Guards:
- it never unsubscribes or spams (spam isn't in the group; an unsubscribe needs the category's
  own confidence);
- floor = max(0.90, the top category's effective threshold): it can only add confidence;
- a non-top member whose account-level threshold is above the floor doesn't count;
- with `act_categories`, only listed categories count;
- the needs-reply veto ([0008](0008-needs-reply-veto-with-keep-signal.md));
- a group archive never counts toward an auto rule.

## Alternatives considered
- Lower the archive thresholds: would also archive emails split between keep and archive.
- Merge notification and newsletter: loses a useful label for the confident cases.
- `archive_group_min_confidence` as a plain global cut: rejected in QA (0.5 would archive
  cold_outreach 0.55 / important 0.45), hence the floor.

## Consequences
- Review shrinks to genuinely uncertain mail.
- `archive_group` is logged but not stored; it can be reconstructed from `probabilities`.
- A category-level (not account-level) threshold above the floor doesn't exclude a non-top member
  (QA nit, left as is).
