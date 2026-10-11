# inbox-jev — instructions for Claude

See [README.md](README.md) for what the tool does and how it is configured.

## Development workflow (mandatory for every change)

`main` is production: the VPS deploys from it, and the org ruleset only accepts changes through
pull requests. Every change, however small, follows this loop:

1. **Branch** off `main`: `feat/…`, `fix/…`, `chore/…`.
2. **Implement** with tests. Before pushing, run `ruff format`, `ruff check` and `pytest` and
   make them green.
3. **Open a PR** (`gh pr create`) whose body says what changed, why, and how it was verified.
4. **QA ⇄ Dev loop**:
   - Spawn the `qa-reviewer` subagent with the PR number. It posts its review on the PR.
   - If the verdict is `CHANGES_REQUESTED`, spawn the `dev-fixer` subagent with the PR number
     and the full review. It pushes fixes and posts a reply.
   - Spawn `qa-reviewer` again with the PR number, its previous review and the dev reply.
   - Repeat until QA returns `APPROVED`.
   - After **5 rounds** without approval, stop and summarize the open disagreements for the
     user instead of looping further.
5. **Merge to production**, only after the final push, with all of these true:
   - QA's latest verdict is `APPROVED` for the current head commit.
   - CI is green: `gh pr checks <n>` exits 0 when run right before merging. The ruleset doesn't
     require status checks yet, so CI is enforced only by this process. Adding `test` and
     `secrets` as required status checks on the `main` ruleset is recommended.
   - No unresolved review threads (CodeRabbit or human). `dev-fixer` addresses or replies to
     them. Check this yourself: `--admin` bypasses the ruleset's thread-resolution rule.
   - Then the orchestrating session merges (see "Merge and approval" below).
6. **Deploy** (once the VPS is set up): `cd /opt/inbox-jev && git pull && docker compose build`.
   The systemd timers pick up the new image on their next run.

Never merge with failing CI, an unresolved review thread or a QA verdict other than APPROVED.
Subagents (`qa-reviewer`, `dev-fixer`) never merge, approve or use `--admin`.

### Merge and approval
The `hellolitebox` org ruleset requires 1 approving review on every PR. The PR author can't
approve their own PR, so the QA subagent can't satisfy that rule. The user has authorized an
admin bypass as the standing path: once the conditions in step 5 hold, the orchestrating session
runs `gh pr merge <n> --squash --delete-branch --admin`. A teammate approval is not required.
If a teammate approval is used instead, request it only after the final push and QA APPROVED.
The ruleset dismisses stale approvals on push, so any later push means asking again.

### Secret scanning
CI runs gitleaks over the full history. If it flags a real secret, **rotate it first**, then add
its fingerprint to `.gitleaksignore`. Never just ignore it.

## Conventions
- Python ≥ 3.11, ruff (line length 100), pytest. No network calls in tests: use fakes like
  `tests/test_triage.py`.
- `policy.py` stays pure (no I/O). Gmail access goes in `gmail.py` and orchestration in `triage.py`.
- New categories or labels go in `categories.yaml`, not in code.
- Secrets live only in `.env`, `secrets/` and `accounts.yaml`, which are gitignored. Never log
  email bodies.
- Schema changes: the project uses `create_all` with no migrations yet. A change to an existing
  table needs a migration story (or an explicit note in the PR) for databases already deployed.
