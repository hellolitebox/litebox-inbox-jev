# 0055. An update merge keeps QA's verdict only when git would have made it unchanged

- Status: Accepted (amends [0028](0028-qa-verdict-required-check.md))
- Date: 2026-10-15
- PR: #5

## Context

[0028](0028-qa-verdict-required-check.md) lets an approval survive `gh pr update-branch`: when the
head is the reviewed commit plus merges whose other parent is on `main`, the reviewed content is
taken as unchanged and `qa-verdict` stays `success`. The gate checked only the shape of those
commits, never what they contain. A merge commit can carry anything: edits of its own made with
`git merge --no-commit` (an "evil merge"), or a conflict resolved by hand, whose resolution is
content no review has seen. Either one, pushed on top of an approved commit, kept the approval and
could be merged. The same hole was found and closed in litebox-automations (its PR #6), whose gate
this one shares.

## Decision

`scripts/qa_gate.py` keeps the verdict across a merge only when the merge's tree is exactly the tree
`git merge-tree --write-tree` writes for its two parents, and that merge has no conflicts. It checks
every merge between the head and the reviewed commit (up to `MAX_UPDATE_DEPTH`). A merge with edits
of its own, or with a conflict however it was resolved, needs a fresh review, like a plain commit.

- The gate fetches the PR head (`refs/pull/<n>/head`) and the base branch once, and only when a
  merge needs checking. A head that is the reviewed commit, or a plain commit, needs no fetch. The
  workflow checks out full history (`fetch-depth: 0`) but only the script's file.
- The gate reads objects only. It never checks out or runs the PR's code.
- If `git merge-tree` fails for any reason other than a conflict (an unknown commit, a failed
  fetch), the run fails (exit 1, to be re-run). An error is never read as a conflict or as a
  verdict, as with a failed `gh api` call.
- Only reviews by people with write access count
  ([0054](0054-qa-reviews-count-only-from-people-with-write-access.md)). That rule picks the review;
  this one decides whether the head still carries it.

## Alternatives considered

- **Diff the merge against its parents** (`git diff --cc`, or the merge against the reviewed commit
  restricted to files `main` didn't touch): this approximates the same question with more rules and
  more edge cases. `merge-tree` asks git directly what it would have produced.
- **Accept a conflicted merge whose resolution looks faithful** (for example, it keeps both sides):
  the gate can't tell a faithful resolution from new content, and a conflict resolved by hand is
  exactly where a subtle change hides. A conflicted update gets a QA round.
- **Drop the update-branch exception and review every new head**: this is safe but costs a QA round
  for every `BEHIND`, which the ruleset's up-to-date requirement makes common. A clean merge adds
  nothing to review.

## Consequences

- An evil merge or a hand-resolved conflict on top of an approved commit returns `qa-verdict` to
  `pending`. A clean `update-branch` merge still keeps it, with no new QA round.
- `dev-fixer` commits a merge of `main` alone and the fixes separately, so that QA reads the fixes
  as their own diff.
- The gate needs git 2.38+ on the runner (`merge-tree --write-tree`). A run that checks a merge
  fetches once more.
- The comment-triggered run uses `main`'s script ([0028](0028-qa-verdict-required-check.md)), so the
  stricter rule applies from the first comment after this record's PR merges. On that PR itself,
  only the push-triggered run uses the new script.
