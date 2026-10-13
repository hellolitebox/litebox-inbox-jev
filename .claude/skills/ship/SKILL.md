---
name: ship
description: Run inbox-jev's QA ⇄ Dev loop on an open pull request and merge it once every gate holds. Use when a PR is open and ready for review, or when the user says "ship", "mergeá" or "pasá el PR <n>".
---

# Ship a pull request

Input: a PR number (`/ship 23`). This is CLAUDE.md → "Development workflow", steps 4 to 6, made
explicit so that no gate is skipped. Never `--admin`, never edit the ruleset, never merge on a
verdict for an older commit.

## 0. Preconditions

```bash
n=<n>
gh pr view $n --json state,isDraft,author,baseRefName,headRefOid,title,mergeStateStatus
head=$(gh pr view $n --json headRefOid -q .headRefOid)
gh run watch --exit-status "$(gh run list --commit "$head" --workflow CI --json databaseId -q '.[0].databaseId')"
```

Watch the CI run, never `gh pr checks --watch`: the `qa-verdict` status stays `pending` until
QA reviews this head, and `--watch` would wait on it forever.

Open, not a draft, base `main`. A PR by `app/dependabot` takes the **Dependabot lane** below.

## 1. Round loop (at most 5 rounds)

For `r` = 1, 2, …, 5:

1. Spawn `qa-reviewer` (Agent tool, `subagent_type: qa-reviewer`) with the PR number and the
   round number; from round 2 on, also the previous QA review and the dev reply, verbatim.
2. Read its answer: the `**Verdict: …**` line and the `Head: <sha>` line.
3. `APPROVED` → go to the merge gates. `CHANGES_REQUESTED` → spawn `dev-fixer`
   (`subagent_type: dev-fixer`) with the PR number, the round number and the full review, then
   the next round.

After 5 rounds without `APPROVED`: stop and summarize the open disagreements for the user. Do not
merge.

## 2. Merge gates

All of these must hold for the **current** head. Check them; don't assume them from the loop.

```bash
head=$(gh pr view $n --json headRefOid -q .headRefOid)
gh pr view $n --json mergeStateStatus -q .mergeStateStatus     # want CLEAN
gh pr checks $n                                                  # test, secrets, docker, qa-verdict: all pass
read OWNER NAME < <(gh repo view --json owner,name -q '"\(.owner.login) \(.name)"')
gh api "repos/$OWNER/$NAME/commits/$head/status" \
  --jq '.statuses[] | select(.context == "qa-verdict") | "\(.state) \(.description)"'
gh api graphql -F owner="$OWNER" -F name="$NAME" -F n=$n -f query='query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){pullRequest(number:$n){reviewThreads(first:100){nodes{isResolved}}}}}' \
  --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved | not)] | length'   # want 0
```

- **`qa-verdict` is `success` for `$head`**: QA's latest review says `APPROVED` and its `Head:`
  is this commit (or this commit is that one merged with `main` by `update-branch`). `pending`
  means the head moved after the review: run another QA round. `failure` means the latest review
  requested changes.
- **`BEHIND`**: `gh pr update-branch $n`, then watch the new head's CI run as in step 0 and
  re-check. The gate accepts the merge commit; no new QA round is needed for it.
- **`BLOCKED`, `UNSTABLE`, `DIRTY`**: find the gate that fails (a check, an unresolved thread, a
  conflict) and fix it through the loop (`dev-fixer`), then re-check.

## 3. Merge and hand over

```bash
gh pr merge $n --squash --delete-branch
gh pr view $n --json body -q .body | sed -n '/^## Deploy notes/,/^## /p'
```

Print the deploy notes and point at README → "Updating a deployment".

## Dependabot lane

No `qa-reviewer` or `dev-fixer`; the session reviews the bump itself:

1. `gh pr diff $n`: only `uv.lock`, `pyproject.toml`, `Dockerfile` tags or digests, or workflow
   pins change. Anything else, or a bump that needs code changes, goes through the full loop.
2. For a **major** bump, and for any bump of `typesafe-sdk`, `google-auth*`,
   `google-api-python-client`, `httpx`, `sqlalchemy` or `pydantic*`, read the release notes
   (`gh api repos/<owner>/<repo>/releases --jq '.[0:3][] | .name, .body'`) for breaking changes
   that touch this code. CI's paired-pin checks cover `uv` and the Python base image.
3. Post the same review comment the agent would, so the gate can pass:

   ```
   ## QA review — round 1
   **Verdict: APPROVED**
   Head: <full head sha>
   Checks: CI (`gh pr checks`) ✅
   Skills: none: dependency bump
   Read: <release notes read, and why nothing applies>
   ```

4. Merge gates, then merge, as above. The gate's job is skipped on Dependabot's own pushes
   (their token is read-only), so `qa-verdict` appears only after this comment, set by the
   comment-triggered run; until then `gh pr checks` doesn't list it at all.
