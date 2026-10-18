# 0056. The QA gate runs only `main`'s workflow; tests chosen by risk, and the Python standards

- Status: Accepted (amends [0028](0028-qa-verdict-required-check.md))
- Date: 2026-10-18
- PR: #7

Ported from litebox-automations, whose process this repo shares: its ADR 0008 (the QA gate's status
is posted only by `main`'s workflow and script) and the test policy and Python standards of its ADR
0009\. That ADR's shared kit for its suites has no counterpart here and is not ported.

## Context

**The gate.** The `qa-verdict` status the repo ruleset requires is posted by
`.github/workflows/qa-gate.yml`, which ran on `pull_request` (every push to a PR) and on
`issue_comment` (every comment). The `issue_comment` run uses `main`'s workflow and script, but a
`pull_request` run executed the PR's own copy of the workflow and of `scripts/qa_gate.py`, with
`statuses: write`. A PR from a branch of this repo that edits `qa-gate.yml` could replace the steps
with one that posts `qa-verdict: success` for its head, and the ruleset would accept it without the
gate having run. The workflow's comment admitted it and asked whoever merges to read the status of
the next comment-triggered run instead: a rule enforced only by attention. Dependabot's
`pull_request` runs got a read-only token, so the gate skipped them, and their PRs had no
`qa-verdict` at all until the review comment.

**The tests.** `.claude/rules/tests.md` asked to raise `fail_under` whenever coverage grows. In
litebox-automations, two audits found real defects behind 96–99% line coverage: partial answers from
an API taken as complete, a skipped run reported as a success, a sign dropped by a parser. Each was
a contract with another system or an invariant no test stated; the lines involved were all covered.
A ratchet rewards covering lines, not catching failures, and makes deleting a redundant test look
like a regression. inbox-jev's code has the same kinds of boundary (Gmail's paginated lists, Jev's
answers, unsubscribe endpoints) and the same ratchet.

## Decision

### The gate runs only `main`'s workflow

- The push-time trigger is `pull_request_target`, which always runs the default branch's copy of the
  workflow. With `issue_comment`, which already did, no run of the gate executes a definition the PR
  controls. A PR that changes the gate is judged by `main`'s workflow and script; its changes take
  effect once merged.
- That run has a write token on every PR, Dependabot's and forks' included, so it never checks out
  or runs the PR's code: the one checkout is the default branch's `scripts/qa_gate.py` (sparse, full
  history, `ref: ${{ github.event.repository.default_branch }}`), and the script reads the head from
  the API and fetches `refs/pull/<n>/head` only as git objects for `git merge-tree` (ADR 0055).
  Nothing from the event that the PR author writes (title, branch name, body) is interpolated into a
  `run:`.
- **Why `pull_request_target` is safe in a public repo.** Anyone can fork this repo and open a PR,
  and a fork's PR gets the write-token `pull_request_target` run too (GitHub's warning about that
  trigger is precisely this). The danger it warns of is running the fork's code with that token: a
  checkout of the PR's head followed by a build, a test or a script from it. Here nothing of the PR
  runs: the job runs `main`'s workflow, checks out `main`'s script, and that script only reads the
  PR through the API and git objects, then posts one commit status. The token can't write contents,
  issues or pull requests (`contents`, `pull-requests` and `issues` read, `statuses` write), and the
  status it posts is the gate's own verdict, which a fork can't influence: only reviews by people
  with write access count (ADR 0054). `tests/test_workflows.py` checks the triggers, the
  permissions, the single checkout of `main`'s script and the absence of PR refs and of a Dependabot
  skip.
- **Scope**: this stops a PR from replacing the gate's own definition, not from posting the status
  some other way. The ruleset requires `qa-verdict` from GitHub Actions, and every workflow's
  `GITHUB_TOKEN` posts as that app. A branch of this repo (so someone with write access) can add or
  edit any `pull_request` workflow to request `statuses: write` and post `qa-verdict: success` on
  its head; a fork can't, because a fork's `pull_request` runs get a read-only token. The remaining
  control is review of `.github/workflows/` diffs: the QA reviewer treats a PR that grants
  `statuses: write` (or any permission that can post a commit status) anywhere but `qa-gate.yml`,
  posts `qa-verdict` from any other workflow, or names a job or check run `qa-verdict`, as a
  blocker. A repo test can't enforce it, because a PR's CI runs the PR's own copy of the tests.
- Dependabot's PRs are no longer skipped: GitHub gives Dependabot's `pull_request_target` runs the
  token `permissions:` declares, so their `qa-verdict` is `pending` from the first push, like any
  other PR's, and a Dependabot rebase turns it `pending` again (`/ship`, Dependabot lane).

### Tests chosen by risk

- **Every test protects something named**: a decision rule (an `EX-NN`), a contract with another
  system, an incident that happened, or a risk the PR states. A test that only re-executes a helper
  another test already covers, or asserts incidental text or internal structure, is removed when its
  code is touched.
- **Fakes model the other system's contract, not its happy path**: partial and paginated answers,
  errors inside a 2xx, timeouts, rate limits, an answer lost after a write.
- **Invariants for parsers and calculations**, and **one end-to-end test per outcome that matters**
  for each scheduled job (result, exit code, what is changed in Gmail, what is posted, what alerts).
- **The coverage floor stays where it is and is not raised by rule.** `fail_under` is a floor
  against forgetting tests, not a goal; deleting a redundant test may lower coverage, and the floor
  is then lowered in the same PR, with the reason in its body. Lowering it to get untested code
  through is still refused (a **major** QA finding).

### Python standards

`docs/development.md` gets a "Python standards" table (design, types, errors, retries, resources,
persistence, tests), the same as litebox-automations' with this repo's modules as examples, and the
QA reviewer's checklist points to it.

## Alternatives considered

- **Keep `pull_request` and read the comment-triggered status by hand**: the hole stays open, and
  nothing on the PR says which run posted the status.
- **`pull_request` with a read-only token, and a `workflow_run` workflow that posts the status**:
  also runs only `main`'s definition, but takes two workflows and an artifact handed between them
  for what `pull_request_target` does in one, and the artifact would come from a run of the PR's
  code.
- **A dedicated GitHub App posts the status** (closes the remaining hole; a later ADR): the gate
  posts `qa-verdict` with the installation token of an App only this repo uses, its key an
  environment secret limited to `main`, and the ruleset's `qa-verdict` pinned to that App instead of
  GitHub Actions. It needs the maintainer to create and install the App and edit the ruleset by
  hand.
- **Keep the coverage ratchet**: see Context; the floor still catches a forgotten test.

## Consequences

- A PR that edits `qa-gate.yml` is gated by `main`'s version, including the PR that introduces this
  ADR. Its pushes run no gate at all: `main`'s workflow has no `pull_request_target` trigger yet,
  and its own copy has no `pull_request` one. Its `qa-verdict` comes only from the review comment's
  `issue_comment` run.
- A `pull_request_target` run's `GITHUB_SHA` is `main`'s latest commit, not the PR's; the status is
  still posted on the PR head the script reads from the API.
- `pyproject.toml`'s `fail_under` comment, `.claude/rules/tests.md` and `pins.md`, the QA checklist
  and `docs/development.md` state the floor rule instead of the ratchet.
- The workflow and these rules stay the same as litebox-automations'; a later change to either is
  ported both ways.
