# 0057. The guard keeps what GitHub can't enforce, and refuses when in doubt

- Status: Accepted (amends [ADR 0044](0044-instructions-rules-by-path-one-check-hook.md)'s hook)
- Date: 2026-10-18
- Ported from: litebox-automations' ADR 0010 (its PR #61, twelve rounds of review)

## Context

`scripts/guard_main.py` is litebox-automations' hook, copied here (ADR 0044): 1,044 lines that read
bash as bash does, for four refusals. Two of them GitHub already enforces here too: the ruleset
"main: PR + CI" (id 25199261) has no bypass actors, so neither a push to `main` nor
`gh pr merge --admin` gets past it. The two it can't enforce were the weak ones. A ruleset write is
allowed to the user's admin token. A force push to a PR branch was refused by nothing: on 2026-10-18
a session in litebox-automations force-pushed a PR branch (its #58), past the hook.

## Decision

The same as litebox-automations' ADR 0010, with the same `scripts/guard_main.py` and tests:

- **The hook refuses four things.**

  - Ruleset writes: REST, GraphQL, and a `gh` alias that could hide one.
  - Force pushes to any branch: every flag, any abbreviation of one, a `+` refspec, and the config
    that can force one.
  - `gh pr merge --admin`.
  - Pushes to `main`.

  The last two are an early message: GitHub is the barrier for them.

- **It reads words, several ways, and refuses if any reading does.**

  - `shlex`, with every operator and newline a command boundary.
  - Every substitution's body, except a certainly quoted heredoc's body.
  - A reading blind to quoting (`$'…'` decoded as bash and zsh may decode it) for the critical
    refusals.
  - In a `gh api` write or GraphQL call, an expansion may stand only in a plain field
    (`-F owner="$OWNER"`).

- **A second ruleset, "all branches: no force-push"** (`non_fast_forward` on every branch but
  `dependabot/**`, no bypass actors), is created by the user: it is this ADR's precondition.

- **What no guard here stops:** expansions it doesn't interpret (variables, `bash -c`, `eval`,
  `xargs`, brace and pathname expansion, unknown runners), config the command doesn't show, and a
  ruleset write outside `gh`'s command line (`curl` with the token). The hook is a guardrail, not a
  barrier.

## Alternatives considered

The same as litebox-automations' ADR 0010: keep the bash interpreter, 1,872 lines here (1,044 of the
hook, 828 of tests), which missed force pushes; drop the hook, which leaves ruleset writes
unguarded; or guard force pushes only in the hook, which covers sessions but not people or other
tools.

## Consequences

- A forbidden command named in a code span inside single quotes is refused. Such a text goes in a
  file or a quoted heredoc, and the message says so.
- A branch behind `main` is updated with `git merge origin/main`, never a rebase and a force push.
- The hook and its tests stay identical in both repos (`ADR` references aside); a fix lands in
  litebox-automations first.
