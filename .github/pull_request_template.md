## What

<!-- What changed, file by file where it helps. -->

## Why

## Verification

<!-- `scripts/check.sh` (ruff, mdformat, mypy, pytest counts). A bug fix: the new test run
without the fix, and how it failed. A change to classification (categories.yaml
criteria, classifier state, body extraction) or to a destructive path (archive, spam,
unsubscribe, unsubscribe-retry) also needs the read-only real-mail check (docs/development.md,
"Checking a branch against real mail"): paste the output of `scripts/check_branch.sh`
(From/Subject truncated, never bodies), or say why it isn't needed.
A classification change adds the reference summary (`check_branch.sh … --reference --quiet`). -->

## Deploy notes

- Schema change: none
- One-off steps (`scripts/deploy.sh --hold`, the steps, then start the timer): none
- Config (`accounts.yaml`, `.env`) to add before or after: none
- Rollback: `scripts/deploy.sh <previous sha>`, nothing else

<!-- Decision logic (`.claude/rules/decision-logic.md`): spec, EX-NN examples and tests, ADR
updated in this PR, or an explicit "no behaviour change" justification. -->

Skills applied: <!-- names from .claude/rules/python-skills.md, or
"none: docs-only"; then the concrete changes they drove -->
