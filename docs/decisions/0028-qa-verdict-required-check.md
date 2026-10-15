# 0028. QA's verdict is a required check tied to the head commit

- Status: Accepted (amended by [0054](0054-qa-reviews-count-only-from-people-with-write-access.md):
  only reviews by people with write access count; and by
  [0055](0055-an-update-merge-keeps-the-verdict-only-when-git-made-it.md): an update merge keeps the
  verdict only when its tree is the conflict-free merge of its parents)
- Date: 2026-10-13
- PR: #23

## Context

Every change reaches `main` through the QA ⇄ Dev loop (CLAUDE.md). The repo ruleset enforced only CI
and resolved review threads: that QA's latest verdict was `APPROVED` *for the commit being merged*
was checked by the orchestrating session by eye, and the review comment did not even record which
commit it had reviewed. A push after the approval, or a merge on a stale verdict, was not blocked by
anything. The loop itself was run by hand each time (21 PRs in four days), with the same chance of a
skipped gate every time. Dependency bumps had no path of their own: a 14-skill review of a lockfile
diff, or no review at all.

## Decision

- Every QA review names the commit it reviewed (`Head: <sha>`), and `scripts/qa_gate.py`, run by
  `.github/workflows/qa-gate.yml` on every push and comment, posts the `qa-verdict` commit status on
  the PR head: `success` only when the latest review is `APPROVED` for that commit, or for that
  commit merged with `main` by `gh pr update-branch`; `pending` when the head moved since; `failure`
  on `CHANGES_REQUESTED`. The ruleset requires `qa-verdict` and, with
  `strict_required_status_checks_policy`, that the branch is up to date with `main`.
- The loop, the merge gates and the Dependabot lane are a project skill (`/ship <n>`), so the
  orchestrating session runs the same steps every time.
- Python skills are loaded by the paths a PR changes (routing table in
  `.claude/agents/qa-reviewer.md`), not all fourteen on every round.
- Dependabot PRs are reviewed by the session (diff shape, release notes of major or sensitive bumps)
  and get the same review comment, so the same gate applies.

## Alternatives considered

- A real GitHub review (`gh pr review --approve`) from the agent, with one required approval:
  rejected, the agent runs as the author's account, so this would be self-approval with a stronger
  name, and CLAUDE.md forbids agents approving.
- Checking the SHA only in the orchestrator's merge step: kept as well, but it is a process control,
  not an enforced one.
- Skipping the gate for Dependabot: a lockfile bump can still break the deploy (ADR 0016's paired
  pins), and an unreviewed path to `main` is the thing this ADR removes.

## Consequences

- A stale review blocks the merge until QA reviews the new head; an `update-branch` merge does not.
- The comment-triggered gate runs the workflow and script from `main`, so on the PR that adds them
  only the push-triggered run can post a status; the ruleset requires `qa-verdict` from the next PR
  on. Dependabot's own pushes run with a read-only token: the gate's job is skipped on them and the
  status appears once the comment-triggered run sees the session's review.
- Anyone can post a comment that starts with `## QA review`; the gate trusts the loop's actors, as
  the ruleset already did. The repo is private and the author is the only committer.

## Addendum (2026-10-14, PR #52)

The `hellolitebox` org ruleset "AI review gate" requires a human approval on every PR; `inbox-jev`
is excluded from it by repo **name** (the ruleset's `conditions.repository_name.exclude`, currently
`litebox-inbox-jev`), and the repo ruleset above replaces it. Renaming the repo applies the org gate
again (one human approval per PR) until that exclude list is updated, which needs the `admin:org`
scope. The maintainer chose to keep name-based targeting. Since ADR 0044 a hook refuses `--admin`, a
push to `main` and writes to the rulesets, whatever a session decides.
