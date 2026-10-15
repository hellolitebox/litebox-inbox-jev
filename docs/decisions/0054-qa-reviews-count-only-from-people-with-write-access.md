# 0054. QA reviews count only from people with write access

- Status: Accepted (amends [0028](0028-qa-verdict-required-check.md))
- Date: 2026-10-15
- PR: #3

## Context

The `qa-verdict` status comes from the latest `## QA review` comment on a pull request
([0028](0028-qa-verdict-required-check.md)), whoever wrote it. ADR 0028 accepted that because the
repo was private and its author the only committer. The repo is public now: anyone with a GitHub
account can comment on a pull request, including one that reads `**Verdict: APPROVED**` with the
right `Head:` line, and turn the required check green on a pull request nobody reviewed.

## Decision

`scripts/qa_gate.py` counts a `## QA review` comment only when its author has write access to the
repo or more (`admin`, `maintain`, `write`), asked once per login through
`repos/{repo}/collaborators/{login}/permission`. Anyone else's review-looking comment is ignored, so
an earlier real review (or none) decides. If that permission can't be read, the comment's
`author_association` decides instead (`OWNER`, `MEMBER` or `COLLABORATOR`).

## Alternatives considered

- **`author_association` alone**: a member whose org membership is private shows as `CONTRIBUTOR` to
  some tokens, which could leave every real review uncounted and block every merge. It stays as the
  fallback only.
- **A fixed list of logins**: correct today, wrong as soon as a maintainer joins or leaves, and kept
  in a second place besides the repo's own access.
- **GitHub's own reviews instead of comments** (an approving review required by the ruleset): the QA
  reviewer runs as the PR author's account, and GitHub doesn't let an author approve their own pull
  request.

## Consequences

- A forged review can't satisfy the gate; merging still needs write access, as before.
- Each gate run makes one more API call per distinct commenter with a `## QA review` comment.
