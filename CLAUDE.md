# inbox-jev — instructions for Claude

See [README.md](README.md) for what the tool does and how it is configured.

## Development workflow (mandatory for every change)

`main` is production: the VPS deploys from it. The repo ruleset "main: PR + CI" only accepts
changes through squash-merged pull requests with green `test` and `secrets` checks and no
unresolved review threads; it requires no approving review. Every change, however small,
follows this loop:

1. **Branch** off `main`: `feat/…`, `fix/…`, `chore/…`.
2. **Implement** with tests. Before pushing, run `ruff format`, `ruff check` and `pytest` and
   make them green.
3. **Open a PR** (`gh pr create`) whose body says what changed, why, and how it was verified.
4. **QA ⇄ Dev loop**:
   - Spawn the `qa-reviewer` subagent with the PR number. It posts its review on the PR. Every
     review loads the Python skills listed in `.claude/agents/qa-reviewer.md` (Skill tool), so
     if you spawn a generic agent pointed at that file, it must have the Skill tool.
   - If the verdict is `CHANGES_REQUESTED`, spawn the `dev-fixer` subagent with the PR number
     and the full review. It pushes fixes and posts a reply.
   - Spawn `qa-reviewer` again with the PR number, its previous review and the dev reply.
   - Repeat until QA returns `APPROVED`.
   - After **5 rounds** without approval, stop and summarize the open disagreements for the
     user instead of looping further.
5. **Merge to production**, only after the final push, with all of these true:
   - QA's latest verdict is `APPROVED` for the current head commit.
   - CI is green (`gh pr checks <n>`). The ruleset also enforces this.
   - No unresolved review threads (CodeRabbit or human). `dev-fixer` addresses or replies to
     them; the ruleset also enforces this.
   - Then the orchestrating session runs `gh pr merge <n> --squash --delete-branch`. The user
     authorized this as the standing merge path; no human approval is needed.
6. **Deploy**: follow README → "Updating a deployment". Stop the triage timer and wait for any
   running pass (the oneshot service's `ActiveState` is `activating` while it runs, so
   `systemctl is-active` doesn't wait), back up `data/inbox-jev.db`, then
   `git pull && docker compose build`. Run the PR's one-off deploy steps (e.g. `reprocess`,
   `senders backfill`, deleting rules that no longer fit), then start the timer again. Server
   config (`accounts.yaml`, `.env`) is not in git: back it up before editing it.

Never merge with failing CI, an unresolved review thread or a QA verdict other than APPROVED.
Subagents (`qa-reviewer`, `dev-fixer`) never merge, approve or use `--admin`.

### Merge and approval
`inbox-jev` is excluded from the `hellolitebox` org ruleset "AI review gate" (which requires a
human approval); the repo ruleset above replaces it. Never use `--admin`, and never weaken or
bypass the ruleset. If a check fails, fix it through the loop.

### Secret scanning
CI runs gitleaks over the full history. If it flags a real secret, **rotate it first**, then add
its fingerprint to `.gitleaksignore`. Never just ignore it.

## Validating behaviour changes
Changes to classification (`categories.yaml` criteria, `classifier.py` state, body extraction)
or to destructive actions (archive, spam, unsubscribe) must be checked against real mail before
merging, not only with unit tests. Use [`scripts/dry_classify.py`](scripts/dry_classify.py)
in the app container on the server, with the branch's code and `categories.yaml`. For each email
it prints the category stored in the DB, then the branch's category, confidence, outcome
(REVIEW / ARCHIVE / KEEP / SPAM, group / unsubscribe flags) and top-3 probabilities, with From
and Subject truncated. It calls Jev (one classification per email) and the sent-mail search, but
never modifies Gmail, opens SQLite read-only and commits nothing.

The image has the package installed non-editable in site-packages, so put the branch's
`src/inbox_jev` in a directory on `PYTHONPATH`, which comes first. Check the branch out next to
the deployment (not over it: the running app uses `/opt/inbox-jev`) and run from there:

```bash
cd /opt/inbox-jev
B=/tmp/inbox-jev-check; sudo git worktree remove --force "$B" 2>/dev/null
sudo git fetch origin <branch> && sudo git worktree add --detach "$B" FETCH_HEAD
docker compose run --rm --no-deps -e PYTHONPATH=/opt/branch \
  -v "$B/src/inbox_jev:/opt/branch/inbox_jev:ro" \
  -v "$B/categories.yaml:/opt/branch/categories.yaml:ro" \
  -v "$B/scripts/dry_classify.py:/opt/branch/dry_classify.py:ro" \
  --entrypoint python app /opt/branch/dry_classify.py \
  --account you@example.com --categories /opt/branch/categories.yaml \
  --query 'in:inbox newer_than:7d' --limit 30   # or --ids ID …; --ignore-rules to always ask Jev
sudo git worktree remove --force "$B"
```

The compose service still mounts `data/` read-write; the script must never write it (no
`make_session_factory`, which runs `create_all`). If the branch changes dependencies in
`pyproject.toml`, build a separate image instead. Put the output in the PR; never log or post
email bodies.

## Conventions
- Dependencies are locked in `uv.lock`: after changing `pyproject.toml`, run `uv lock` and commit
  both (CI runs `uv lock --check`; the Docker image installs with `uv sync --frozen`).
  Dependabot opens weekly update PRs, which go through the same QA loop.
- `ruff` is pinned twice: in the `dev` extra of `pyproject.toml` and as `rev` in
  `.pre-commit-config.yaml`. Bump both together (Dependabot only updates `pyproject.toml`).
- `uv` is pinned twice: the `ghcr.io/astral-sh/uv` stage tag in the `Dockerfile` and the
  `setup-uv` `version` in `.github/workflows/ci.yml`. Bump both together: Dependabot only updates
  the Dockerfile tag, and CI's `test` job fails when the two differ. Don't add
  `[tool.uv] required-version`: Dependabot's bundled uv would refuse to lock and no dependency
  PRs would be opened.
- The base image's Python (`python:3.x-slim` in the `Dockerfile`) must equal `PYTHON_VERSION`
  in `ci.yml`. The `docker` job checks it, so a Dependabot base-image bump needs `PYTHON_VERSION`
  bumped in the same PR. The `docker` job also builds the image and smoke-tests it as a non-root
  uid, as production runs it.
- Python ≥ 3.11, ruff (line length 100), pytest. No network calls in tests: use fakes like
  `tests/test_triage.py`.
- `policy.py` stays pure (no I/O). Gmail access goes in `gmail.py` and orchestration in `triage.py`.
- New categories or labels go in `categories.yaml`, not in code.
- Anything keyed on the sender (rules, corrections, `previous_contact`, allowlist) uses
  `triage.signals_for()` (resolved signals), never `Email.signals` directly. Rules for trusted
  groups (`group_domains`) key on the author; untrusted groups stay keyed on the group address,
  never on the author they claim.
- Every new path that can unsubscribe must go through the same checks (`policy.can_unsubscribe`,
  known contacts, once-per-list, mode and `act_categories`, confidence, the run cap) and
  respect what the user decided since (sender rules, corrections, restore, star). Unsubscribes
  are irreversible. `run` and `unsubscribe-retry` (`triage.retry_unsubscribes`) both do.
- Keep README.md, `categories.yaml` comments and `accounts.example.yaml` in sync with behaviour
  in the same PR.
- Secrets live only in `.env`, `secrets/` and `accounts.yaml`, which are gitignored. Never log
  email bodies.
- Schema changes: the project uses `create_all` with no migrations yet. A change to an existing
  table needs a migration story (or an explicit note in the PR) for databases already deployed.
