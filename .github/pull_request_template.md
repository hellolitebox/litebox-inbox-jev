## What

<!-- What changed, file by file where it helps. -->

## Why

## Verification

<!-- ruff format / ruff check / mypy / pytest counts. A change to classification (categories.yaml
criteria, classifier state, body extraction) or to a destructive path (archive, spam,
unsubscribe, unsubscribe-retry) also needs the read-only real-mail check: paste the output of
`scripts/check_branch.sh` (From/Subject truncated, never bodies), or say why it isn't needed.
A classification change adds the reference summary (`check_branch.sh … --reference --quiet`). -->

## Deploy notes

- Schema change: none
- One-off steps (`scripts/deploy.sh --hold`, the steps, then start the timer): none
- Config (`accounts.yaml`, `.env`) to add before or after: none
- Rollback: `scripts/deploy.sh <previous sha>`, nothing else

<!-- Decision logic (see CLAUDE.md → "Decision logic docs"): spec, EX-NN examples and tests, ADR
updated in this PR, or an explicit "no behaviour change" justification. -->

Skills applied: <!-- names from the routing table in .claude/agents/qa-reviewer.md, or
"none: docs-only"; then the concrete changes they drove -->
