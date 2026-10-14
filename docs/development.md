# Developing inbox-jev

The repo is developed with Claude Code: `CLAUDE.md` holds the rules that apply everywhere,
`.claude/rules/` the ones scoped to paths, `.claude/agents/` the QA reviewer and the dev fixer, and
`/ship` the loop that reviews and merges a pull request
([ADR 0028](decisions/0028-qa-verdict-required-check.md),
[ADR 0044](decisions/0044-instructions-rules-by-path-one-check-hook.md)). The checks and the
conventions are the same for a human.

## Workflow

`main` is production and changes only through a squash-merged pull request. Branch off `main`
(`feat/…`, `fix/…`, `chore/…`), implement with tests, and open the PR with the template
(`.github/pull_request_template.md`). `/ship <n>` runs the QA ⇄ Dev loop (`qa-reviewer` reviews;
`dev-fixer` addresses a `CHANGES_REQUESTED` verdict; at most 5 rounds) and the merge gates (green
CI, a `qa-verdict` of `success` for the head commit, i.e. an `APPROVED` review whose `Head:` is this
commit, no unresolved review threads, branch up to date with `main`), then the squash merge. A
Claude Code hook (`scripts/guard_main.py`) refuses `--admin`, any push to `main` and edits to the
ruleset; the ruleset on GitHub enforces the gates either way. The details are in `CLAUDE.md`; the
Dependabot and Public Suffix List lanes in `/ship` (`.claude/skills/ship/SKILL.md`).

## Checks

```bash
scripts/check.sh                       # what CI's test job runs: uv.lock, ruff, mdformat, mypy, pytest --cov
scripts/check.sh --fix                 # fix first (ruff check --fix-only, ruff format, mdformat), then check
uv run pytest -q tests/test_policy.py  # one file; coverage only counts on the full run
```

`uv run pre-commit install` once per clone runs ruff, mdformat and gitleaks on every commit. In
Claude Code, a long task gets a finish line it can wait on:
`/goal scripts/check.sh passes and the PR is open` for an implementation,
`/goal PR <n> is merged by /ship or 5 rounds are exhausted` for the loop.

## Checking a branch against real mail

Changes to classification (`categories.yaml` criteria, `classifier.py` state, body extraction) or to
destructive actions (archive, spam, unsubscribe) must be checked against real mail before merging,
not only with unit tests. [`scripts/dry_classify.py`](../scripts/dry_classify.py) runs in the app
container on the server, with the branch's code and `categories.yaml`. For each email it prints the
category stored in the DB, then the branch's category, confidence, outcome (REVIEW / ARCHIVE / KEEP
/ SPAM, group / unsubscribe flags) and top-3 probabilities, with From and Subject truncated. It
calls Jev (one classification per email) and the sent-mail search, but never modifies Gmail, opens
SQLite read-only and commits nothing.

[`scripts/check_branch.sh`](../scripts/check_branch.sh), run on the server from the deployment
checkout, does the whole thing: it fetches the branch into a throwaway worktree next to the
deployment (never over it: the running app uses `/opt/inbox-jev`), mounts the branch's
`src/inbox_jev`, `categories.yaml` and `dry_classify.py` into the app image ahead of the installed
package (the image has it non-editable in site-packages, so a `PYTHONPATH` directory comes first),
runs it, and removes the worktree:

```bash
cd /opt/inbox-jev
scripts/check_branch.sh <branch> --account you@example.com --query 'in:inbox newer_than:7d' --limit 30
scripts/check_branch.sh <branch> --account you@example.com --ids ID …   # --ignore-rules to always ask Jev
scripts/check_branch.sh <branch> --account you@example.com --reference --quiet [--accepted 7]
```

**Reference set**: a per-email check is judged by eye, and Jev's run-to-run noise flips borderline
emails (a notification at 0.82, then 0.77, around a 0.80 threshold). `--reference` turns the user's
own corrections into a score: it checks the account's `done` emails whose category the user
corrected (`corrected_category`, as `corrections.detect` learns it: a different category label, the
Review label removed from a suggestion, or an archived or spammed email pulled back to the inbox,
which counts as `important`), the correction being the truth. With `--accepted DAYS` (opt-in,
weaker, reported separately) it adds emails from a `label`/`act` pass at least DAYS days old that
the user left alone: no correction, not restored, not left in Review, and no edit in their current
Gmail labels; their truth is the stored decision. `--limit N` caps each kind (newest first) and
`--since DAYS` keeps rows processed in the last DAYS days. It always asks Jev (a correction is also
a sender rule to the truth) and ends with a summary: the branch's accuracy against the truth and the
stored decision's (what Jev decided then, on the deployed code) as the baseline, with `fixed` /
`broken` counts. The baseline only covers corrections whose stored decision was Jev's (`source`
`jev`); the others are scored for the branch alone and counted as `baseline_unknown`: a rule or the
allowlist decided them (not like-for-like with a branch that ignores rules), or a later pass redid
them (an `act` pass over `label` rows, `reprocess`) and stored the correction rule's answer over
Jev's. It also prints the mismatches as `truth -> answer` category pairs, how many of Jev's answers
are within ±0.05 and ±0.10 of their category's threshold (the borderline band) and the Jev calls;
emails deleted from Gmail, accepted ones edited since and failed Jev calls are counted, not scored.
`--quiet` prints only that summary: category names and counts, no ids, From or Subject.

A PR that changes classification puts both in "Verification": the per-email check on recent mail and
the `--reference --quiet` summary, with the branch's accuracy next to the stored baseline (run it on
`main` too when the stored decisions predate a change already deployed).

The compose service still mounts `data/` read-write; the script must never write it (no
`db.make_engine` or `db.open_sessions`, which run `create_all`). A branch whose runtime dependencies
differ from the deployment's is refused (build a separate image instead): `uv.lock` differs, or
`pyproject.toml`'s `requires-python`, `dependencies` or an optional-dependency group other than
`dev` (`scripts/runtime_deps.py`, run with the host's `python3`). A change to tool config
(`[tool.ruff]`, `[tool.mypy]`, coverage) passes. A change to the version or the `dev` extra is
refused anyway: `uv.lock` records both, so it changes with them. Put the output in the PR's
"Verification" section (QA checks for it, see the reviewer's "Real-mail check"); never log or post
email bodies.
