---
name: ship
description: Run inbox-jev's QA ⇄ Dev loop on an open pull request and merge it once every gate holds. Use when a PR is open and ready for review, or when the user says "ship", "mergeá" or "pasá el PR <n>".
---

# Ship a pull request

Input: a PR number (`/ship 23`). This is CLAUDE.md's step 4 (the QA ⇄ Dev loop, the merge gates, the
merge) and the deploy hand-over, made explicit so that no gate is skipped. Never `--admin`, never
edit the ruleset, never merge on a verdict for an older commit.

## 0. Preconditions

```bash
n=<n>
gh pr view $n --json state,isDraft,author,baseRefName,headRefOid,title,mergeStateStatus
head=$(gh pr view $n --json headRefOid -q .headRefOid)
until id=$(gh run list --commit "$head" --workflow CI --json databaseId -q '.[0].databaseId') && [ -n "$id" ]; do sleep 5; done
gh run watch --exit-status "$id"     # the run appears a few seconds after the push
```

Watch the CI run, never `gh pr checks --watch`: the `qa-verdict` status stays `pending` until QA
reviews this head, and `--watch` would wait on it forever.

Open, not a draft, base `main`. A PR by `app/dependabot` takes the **Dependabot lane** below. A
Public Suffix List update takes the normal round loop; the **PSL lane** below says how a session
opens it from the `psl-update` issue.

## 1. Round loop (at most 5 rounds)

For `r` = 1, 2, …, 5:

1. Spawn `qa-reviewer` (Agent tool, `subagent_type: qa-reviewer`) with the PR number and the round
   number; from round 2 on, also the previous QA review and the dev reply, verbatim.
2. Read its answer: the `**Verdict: …**` line and the `Head: <sha>` line.
3. `APPROVED` → go to the merge gates. `CHANGES_REQUESTED` → spawn `dev-fixer`
   (`subagent_type: dev-fixer`) with the PR number, the round number and the full review, then the
   next round.

After 5 rounds without `APPROVED`: stop and summarize the open disagreements for the user. Do not
merge.

## 2. Merge gates

All of these must hold for the **current** head. Check them; don't assume them from the loop.

```bash
head=$(gh pr view $n --json headRefOid -q .headRefOid)
gh pr view $n --json mergeStateStatus -q .mergeStateStatus     # want CLEAN
gh pr checks $n                                                  # test, test-min-python, secrets, docker, qa-verdict: all pass
read OWNER NAME < <(gh repo view --json owner,name -q '"\(.owner.login) \(.name)"')
gh api "repos/$OWNER/$NAME/commits/$head/status" \
  --jq '.statuses[] | select(.context == "qa-verdict") | "\(.state) \(.description)"'
gh api graphql -F owner="$OWNER" -F name="$NAME" -F n=$n -f query='query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){pullRequest(number:$n){reviewThreads(first:100){nodes{isResolved}}}}}' \
  --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved | not)] | length'   # want 0
```

- **`qa-verdict` is `success` for `$head`**: QA's latest review says `APPROVED` and its `Head:` is
  this commit (or this commit is that one merged with `main` by `update-branch`). `pending` means
  the head moved after the review: run another QA round. `failure` means the latest review requested
  changes.
- **`BEHIND`**: `gh pr update-branch $n`, then watch the new head's CI run as in step 0 and
  re-check. The gate accepts the merge commit; no new QA round is needed for it.
- **`BLOCKED`, `UNSTABLE`, `DIRTY`**: find the gate that fails (a check, an unresolved thread, a
  conflict) and fix it through the loop (`dev-fixer`), then re-check.

## 3. Merge and hand over

```bash
gh pr merge $n --squash --delete-branch
gh pr view $n --json body -q .body | sed -n '/^## Deploy notes/,/^## /p'
```

Print the deploy notes and point at `docs/operations.md` → "Updating a deployment":
`scripts/deploy.sh` on the server, `scripts/deploy.sh --hold` when the notes have one-off steps.

## Dependabot lane

No `qa-reviewer` or `dev-fixer`; the session reviews the bump itself:

1. `gh pr diff $n`: only `uv.lock`, `pyproject.toml`, `Dockerfile` tags or digests, or workflow pins
   change. Anything else, or a bump that needs code changes, goes through the full loop.

2. For a **major** bump, and for any bump of `typesafe-sdk`, `google-auth*`,
   `google-api-python-client`, `httpx`, `sqlalchemy` or `pydantic*`, read the release notes
   (`gh api repos/<owner>/<repo>/releases --jq '.[0:3][] | .name, .body'`) for breaking changes that
   touch this code. CI's paired-pin checks cover `uv` and the Python base image.

3. Post the same review comment the agent would, so the gate can pass:

   ```
   ## QA review — round 1
   **Verdict: APPROVED**
   Head: <full head sha>
   Checks: CI (`gh pr checks`) ✅
   Skills: none: dependency bump
   Read: <release notes read, and why nothing applies>
   ```

4. Merge gates, then merge, as above. The gate's job is skipped on Dependabot's own pushes (their
   token is read-only), so `qa-verdict` appears only after this comment, set by the
   comment-triggered run; until then `gh pr checks` doesn't list it at all. A Dependabot rebase
   after the review (auto-rebase on a `main` push, or `@dependabot rebase`) moves the head and
   leaves it with no status at all: post a new review for the new head.

## PSL lane

The weekly `Update public suffix list` workflow only checks: when publicsuffix.org has a newer valid
list, it opens (or updates) an issue labeled `psl-update`, titled
`Public Suffix List update available: <VERSION>`, with the VERSION and COMMIT its script validated.
It never pushes or opens a PR. A session that sees the issue ships the update as a normal PR:

1. `gh issue list --label psl-update --state open`: note the issue number `$i` and its VERSION.
2. Branch off `main`
   (`git fetch origin && git switch -c chore/update-psl-<VERSION date> origin/main`) and run
   `uv run python scripts/update_psl.py`. It downloads and validates the list again and prints
   `updated: VERSION … COMMIT …` (a later VERSION than the issue's is fine: ship that one).
   `unchanged` means `main` already has it: close the issue with a note.
3. `git diff --stat`: only `src/inbox_jev/public_suffix_list.dat`. Read the rules it adds and
   removes (`git diff | grep -E '^[-+][^-+/ ]'`): a rule that is (or is a parent of) a domain in the
   README, `accounts.example.yaml` or the tests shows up as a failing test; note any suffix that
   touches mail you know of. Run the checks (`ruff`, `mypy`, `pytest`) as for any change.
4. Commit only the `.dat` (`Update the public suffix list to <VERSION>`) and push the branch.
5. On the server, once (any production account):
   `cd /opt/inbox-jev && scripts/check_branch.sh <branch> --account <email> --query 'in:inbox newer_than:1d' --limit 1`
   (`dry_classify.py` needs a query; this one reads at most one email and makes at most one Jev
   call, read-only). It loads the whole `accounts.yaml` with the new list, whatever `--account`
   names, so one run checks every account's `group_domains`: an entry that became a public suffix
   fails here ("is a public suffix") instead of at deploy. If it fails, stop and tell the user: the
   entry has to change before the list can.
6. `gh pr create` with the template: What (VERSION, COMMIT with its
   `https://github.com/publicsuffix/list/commit/<COMMIT>` link, rules added/removed in a line), Why
   (ADR 0037), Verification (the checks and the step 5 output, no bodies), deploy notes (none;
   rollback as usual), `Skills applied: none: data update`, and `Closes #$i`. The `.dat` alone is
   exempt from the decision-logic docs (`.claude/rules/decision-logic.md`).
7. `/ship <n>` from step 0: the normal round loop with `qa-reviewer`, merge gates and merge. The
   merge closes the issue; deploy as usual (no one-off steps).
