# inbox-jev — instructions for Claude

See [README.md](README.md) for what the tool does and how it is configured.

## Development workflow (mandatory for every change)

`main` is production: the VPS deploys from it. The repo ruleset "main: PR + CI" only accepts
changes through squash-merged pull requests whose branch is up to date with `main`, with green
`test`, `secrets`, `docker` and `qa-verdict` checks and no unresolved review threads; it
requires no approving review. `qa-verdict` is the QA reviewer's verdict for the exact head
commit, set by the `QA gate` workflow (`scripts/qa_gate.py`, ADR 0028). Every change, however
small, follows this loop; `/ship <n>` (`.claude/skills/ship`) runs steps 4 and 5:

1. **Branch** off `main`: `feat/…`, `fix/…`, `chore/…`.
2. **Implement** with tests. Before writing code, load with the Skill tool the Python skills that
   the routing table in `.claude/agents/qa-reviewer.md` ("Python skills") selects for the paths
   you change (a change that touches none of them, e.g. docs only, loads none), and apply them;
   the repo's own conventions win over a skill's generic advice. The PR body gets a
   `Skills applied:` line naming the skills (or `none: docs-only`) and any concrete change they
   drove; if a skill isn't available, say so on that line and continue. Whoever implements (this
   session or a spawned agent) does this; a spawned agent needs the Skill tool. Before pushing,
   run `ruff format`, `ruff check`, `mypy` and `pytest` and make them green (`pre-commit install`
   once per clone runs ruff and gitleaks on every commit). A change to decision logic also updates
   the spec, its examples and their tests in the same PR (see
   [Decision logic docs](#decision-logic-docs-mandatory)).
3. **Open a PR** (`gh pr create`) with the template (`.github/pull_request_template.md`): what
   changed, why, how it was verified, deploy notes.
4. **QA ⇄ Dev loop**:
   - Spawn the `qa-reviewer` subagent with the PR number. It posts its review on the PR, with a
     `Head: <sha>` line naming the commit it reviewed. Every review loads the Python skills the
     routing table selects (Skill tool), so if you spawn a generic agent pointed at that file,
     it must have the Skill tool.
   - If the verdict is `CHANGES_REQUESTED`, spawn the `dev-fixer` subagent with the PR number
     and the full review. It pushes fixes and posts a reply.
   - Spawn `qa-reviewer` again with the PR number, its previous review and the dev reply.
   - Repeat until QA returns `APPROVED`.
   - After **5 rounds** without approval, stop and summarize the open disagreements for the
     user instead of looping further.
5. **Merge to production**, only after the final push, with all of these true:
   - `qa-verdict` is `success` for the current head commit: QA's latest verdict is `APPROVED`
     and its `Head:` is this commit (or this commit is that one merged with `main` by
     `gh pr update-branch`). A verdict for an older commit is `pending`: run another QA round.
   - CI is green (`gh pr checks <n>`) and the branch is up to date with `main`
     (`mergeStateStatus` is `CLEAN`; when `BEHIND`, `gh pr update-branch <n>` and wait for CI).
   - No unresolved review threads (human or bot). `dev-fixer` addresses or replies to them.
   - The ruleset enforces all three. Then the orchestrating session runs
     `gh pr merge <n> --squash --delete-branch`. The user authorized this as the standing merge
     path; no human approval is needed.
6. **Deploy**: on the server, `scripts/deploy.sh` (README → "Updating a deployment"). It stops
   the triage timer and waits for any running pass (the oneshot service's `ActiveState` is
   `activating` while it runs, so `systemctl is-active` doesn't wait), backs up
   `data/inbox-jev.db`, checks out `origin/main` (or the ref you pass: `scripts/deploy.sh
   <previous sha>` rolls back), pulls the image CI built for that commit (ADR 0029), records its
   tag in `.env` and checks `inbox-jev --version` against the checkout.
   When the PR has one-off deploy steps (e.g. `reprocess`, `senders backfill`, deleting rules
   that no longer fit), run `scripts/deploy.sh --hold`, the steps, then start the timer. Server
   config (`accounts.yaml`, `.env`) is not in git: back it up before editing it.

**Dependabot PRs** take a lighter lane (see `/ship`, "Dependabot lane"): no `qa-reviewer` or
`dev-fixer`; the orchestrating session checks that the diff is only pins and lockfile, reads the
release notes of major or sensitive bumps, posts the same `## QA review` comment with its
`Head:` so the gate can pass, and merges through the same gates. A bump that needs code changes
goes through the full loop.

Never merge with failing CI, an unresolved review thread or a QA verdict other than APPROVED.
Subagents (`qa-reviewer`, `dev-fixer`) never merge, approve or use `--admin`, and never post a
`## QA review` comment except `qa-reviewer` itself.

### Merge and approval
`inbox-jev` is excluded from the `hellolitebox` org ruleset "AI review gate" (which requires a
human approval); the repo ruleset above replaces it. Never use `--admin`, and never weaken or
bypass the ruleset. If a check fails, fix it through the loop. The `qa-verdict` check is only
ever satisfied by a real review: `qa-reviewer`'s comment, or the session's own review of a
Dependabot bump. Posting a `## QA review` comment to get past it is bypassing the ruleset.

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

[`scripts/check_branch.sh`](scripts/check_branch.sh), run on the server from the deployment
checkout, does the whole thing: it fetches the branch into a throwaway worktree next to the
deployment (never over it: the running app uses `/opt/inbox-jev`), mounts the branch's
`src/inbox_jev`, `categories.yaml` and `dry_classify.py` into the app image ahead of the
installed package (the image has it non-editable in site-packages, so a `PYTHONPATH` directory
comes first), runs it, and removes the worktree:

```bash
cd /opt/inbox-jev
scripts/check_branch.sh <branch> --account you@example.com --query 'in:inbox newer_than:7d' --limit 30
scripts/check_branch.sh <branch> --account you@example.com --ids ID …   # --ignore-rules to always ask Jev
```

The compose service still mounts `data/` read-write; the script must never write it (no
`make_session_factory`, which runs `create_all`). A branch that changes `pyproject.toml` or
`uv.lock` is refused: build a separate image instead. Put the output in the PR's "Verification"
section (QA checks for it, see the reviewer's "Real-mail check"); never log or post email bodies.

## Conventions
- Dependencies are locked in `uv.lock`: after changing `pyproject.toml`, run `uv lock` and commit
  both (CI runs `uv lock --check`; the Docker image installs with `uv sync --frozen`).
  Dependabot opens weekly update PRs, which take the Dependabot lane (see the workflow above).
- `ruff` is pinned twice: in the `dev` extra of `pyproject.toml` and as `rev` in
  `.pre-commit-config.yaml`. Bump both together (Dependabot only updates `pyproject.toml`).
- `uv` is pinned twice: the `ghcr.io/astral-sh/uv` stage tag in the `Dockerfile` and the
  `setup-uv` `version` in `.github/workflows/ci.yml`. Bump both together: Dependabot only updates
  the Dockerfile tag, and CI's `test` job fails when the two differ. Don't add
  `[tool.uv] required-version`: Dependabot's bundled uv would refuse to lock and no dependency
  PRs would be opened.
- The base image's Python (`python:3.x-slim` in the `Dockerfile`) must equal `PYTHON_VERSION`
  in `ci.yml`. The `docker` job checks it, so a Dependabot base-image bump needs `PYTHON_VERSION`
  bumped in the same PR. The `docker` job also builds the image, smoke-tests it as a non-root
  uid, as production runs it, and on `main` pushes it to GHCR: that image is what
  `scripts/deploy.sh` deploys (ADR 0029). Base images are pinned by tag **and** digest, and
  GitHub Actions by commit SHA with the version as a comment; Dependabot bumps both halves, so
  keep that shape when editing them by hand.
- Python ≥ 3.11, ruff (line length 100), mypy (`[tool.mypy]` in `pyproject.toml`: `src` and
  `scripts`; only `yaml`, `googleapiclient` and `google_auth_oauthlib` are untyped, their objects
  are `Any`), pytest. No network calls in tests: use fakes like `tests/test_triage.py`.
- `policy.py` stays pure (no I/O). Gmail access goes in `gmail.py` and orchestration in `triage.py`.
- New categories or labels go in `categories.yaml`, not in code.
- Anything keyed on the sender (rules, corrections, `previous_contact`, allowlist) uses
  `triage.signals_for()` (resolved signals), never `Email.signals` directly. Rules for trusted
  groups (`group_domains`) key on the author; untrusted groups stay keyed on the group address,
  never on the author they claim.
- Every new path that can unsubscribe must go through the same checks and respect what the user
  decided since (sender rules, corrections, restore, star). Unsubscribes are irreversible. The
  sender checks (category, allowlist, verified, group, known contact) are
  `policy.unsubscribe_gate` and the mode / `act_categories` check is `policy.acts_on`: call them,
  never re-implement them. Once-per-list, confidence and the run cap stay with the caller, and
  an attempt is stored with `triage.record_attempt`. `run` and `unsubscribe-retry`
  (`triage.retry_unsubscribes`) both do; `tests/test_safety_gates.py` runs the same scenarios
  through every path, so add a new path there.
- Keep README.md, `categories.yaml` comments and `accounts.example.yaml` in sync with behaviour
  in the same PR.
- Secrets live only in `.env`, `secrets/` and `accounts.yaml`, which are gitignored. Never log
  email bodies.
- Schema changes: the project uses `create_all` with no migrations yet. A change to an existing
  table needs a migration story (or an explicit note in the PR) for databases already deployed.
- Agents create their worktrees as siblings of the checkout (`<repo>-qa-<n>`, `<repo>-dev-<n>`)
  and remove them when done; Claude Code's own go under `.claude/worktrees/`, which is
  gitignored.

## Decision logic docs (mandatory)
[`docs/how-it-decides.md`](docs/how-it-decides.md) is the functional spec of how an email is
decided, and [`docs/decisions/`](docs/decisions/README.md) holds the ADRs explaining why. Every
change to decision logic must, **in the same PR**:

- update `docs/how-it-decides.md` (flowcharts, tables, numbers, "Known gaps") so it matches the
  code;
- add or update the worked examples (`EX-NN`) it affects, and their tests in
  `tests/test_spec_examples.py` (`test_exNN_…`, docstring citing `EX-NN`). A **new** rule or
  guard that changes an outcome (what is labeled, archived, spammed, unsubscribed or learned) gets
  a **new** `EX-NN` showing it. The test `test_doc_examples_match_tests` fails when an ID exists
  on only one side, or when the doc's "**Test**:" name isn't the test's name;
- add an ADR (`docs/decisions/NNNN-title.md`, plus the index) when the change is a design
  decision: a new rule or guard, a changed threshold or default, a trade-off between safety and
  coverage. Supersede an old ADR instead of rewriting it.

Decision logic means: `categories.yaml` (categories, actions, thresholds, criteria, archive group,
needs_reply, tags, auto_rule), `policy.py`, `triage.py` (pipeline, `previous_contact`, user edits,
unsubscribe paths), `rules.py`, `corrections.py`, `signals.py`, `unsubscribe.py`, the classifier
state and questions (`classifier.py`), body extraction (`gmail.extract_body`), the sent-mail
search and its cache (`gmail.has_written_to`), `config.py` (`FREE_MAIL`, `PUBLIC_GROUP_HOSTS`,
`group_domains` normalisation and rejection, the `act_categories` and
`archive_group_reply_keep_probability` validators, and the `Settings` defaults `MODE`,
`LOOKBACK`, `MAX_MESSAGES_PER_RUN`, `MAX_UNSUBSCRIBES_PER_RUN`, `BODY_CHARS`), and the `cli.py`
commands that change decisions or stored state (`restore`, `reprocess`, `rules set` /
`rules delete`, `unsubscribe-retry`). QA rejects a PR that changes any of these without the
matching doc, example and test updates (or an explicit "no behaviour change" justification in
the PR body, e.g. a pure refactor).
