# inbox-jev — instructions for Claude

inbox-jev reads people's Gmail, sends email content to the Jev API, labels, archives, spams and
unsubscribes. A mistake loses or leaks someone's mail, so every change is reviewed by a separate
agent and verified before it reaches production. [README.md](README.md) says what the tool does,
[docs/operations.md](docs/operations.md) how it runs in production and
[docs/development.md](docs/development.md) how it is developed;
[docs/how-it-decides.md](docs/how-it-decides.md) is the functional spec of every decision,
[docs/decisions/](docs/decisions/README.md) the ADRs behind them and [docs/guide.md](docs/guide.md)
the plain-language guide for the inbox owner. The rules in `.claude/rules/` load when you touch the
files they cover; read one directly when you plan a change to its files.

## Commands

- `scripts/check.sh`: the checks CI runs (lock, ruff, mdformat, mypy, pytest with coverage) in one
  command; `--fix` formats first. Green before every push. A task is done when it passes and the PR
  is open.
- `uv run pytest -q tests/test_x.py` runs one file; coverage only counts on the full run.
- `uv run pre-commit install` once per clone: ruff, mdformat and gitleaks on every commit.

## Workflow, for every change however small

`main` is production: the VPS deploys from it, and the repo ruleset accepts only squash-merged pull
requests, up to date with `main`, with green `test`, `test-min-python`, `secrets`, `docker` and
`qa-verdict` checks and no unresolved review threads. `qa-verdict` is the QA reviewer's verdict for
the exact head commit (`scripts/qa_gate.py`, ADR 0028), and only a real review satisfies it. Never
`--admin`, never push to `main`, never force-push any branch (a branch behind `main` gets
`git merge origin/main`), never weaken a ruleset ("main: PR + CI", "all branches: no force-push"): a
hook (`scripts/guard_main.py`, ADR 0057) refuses those commands, and a failing check is fixed
through the loop.

1. **Branch** off `main`: `feat/…`, `fix/…`, `chore/…`.
2. **Implement** with tests. The Python skills to load are selected by path
   (`.claude/rules/python-skills.md`). A bug fix proves its test fails without the fix
   (`.claude/rules/tests.md`). A change to decision logic starts with its ADR draft, agreed before
   code, and is checked against real mail before merging (`.claude/rules/decision-logic.md`).
3. **Open a PR** with the template (`.github/pull_request_template.md`): what, why, verification,
   deploy notes, `Skills applied:`.
4. **`/ship <n>`**: the QA ⇄ Dev loop (`qa-reviewer`, then `dev-fixer` on `CHANGES_REQUESTED`, at
   most 5 rounds, then stop and summarize the disagreements for the user), the merge gates and the
   squash merge. Subagents never merge, approve or post a `## QA review` comment: only `qa-reviewer`
   does. Dependabot bumps and Public Suffix List updates have their own lanes there.
5. **Deploy** on the server with `scripts/deploy.sh` (`docs/operations.md` → "Updating a
   deployment"), `--hold` when the PR has one-off steps.

## Gotchas the code can't tell you

- Secrets live only in `.env`, `secrets/` and `accounts.yaml` (gitignored). Never log or post email
  bodies: ids, counts and truncated From/Subject only.
- CI runs gitleaks over the full history. A real secret it flags is **rotated first**, then its
  fingerprint goes in `.gitleaksignore`. Never just ignore it.
- Code, docs, commit messages and PR bodies are in English.
- Agents' worktrees are siblings of the checkout (`<repo>-qa-<n>`, `<repo>-dev-<n>`), removed when
  done. On the server, `/opt/inbox-jev` is the running deployment: never work in it.
