# 0044. Instructions: invariants in CLAUDE.md, rules by path, one check script, a hook for the hard prohibitions

- Status: Accepted
- Date: 2026-10-14
- PR: #52

## Context

CLAUDE.md had grown to 260 lines (about 2,900 words), loaded into every session and into every
`qa-reviewer` and `dev-fixer` spawn: a five-round PR loads it ten times. More than half of it
repeated what already had a home: the loop, the merge gates and the Dependabot and PSL lanes in
`/ship`, the deploy procedure in the README, the Python-skills routing table in the reviewer, and
tool settings that `pyproject.toml` declares. Claude Code's own guidance puts the adherence limit at
about 200 lines per instruction file and says a bloated file gets its real rules lost. The two
hardest rules (never `--admin`, never push to `main`) were prose only; "run the checks" was five
commands spelled out, slightly differently, in three files; and six skills loaded for any `.py`
change, whether or not it touched what they cover.

## Decision

- CLAUDE.md keeps only what applies to every session: the commands, the workflow's invariants and
  the gotchas the code can't tell. Procedures live where they run (`/ship`, the README) and are
  pointed at, not repeated.
- Instructions that matter for part of the codebase are path-scoped rules in `.claude/rules/`
  (`paths:` frontmatter), loaded when Claude reads or edits a matching file: decision logic (spec,
  examples, ADR, the real-mail check, ADR-first planning), module layout, tests, the double pins,
  config and docs that move together, and the Python-skills routing table, which the reviewer and
  the fixer read by path.
- `scripts/check.sh` is the one check: CI's `test` job runs it, and so do the reviewer, the fixer
  and the implementing session. "Done" for an implementation is that script passing and the PR open,
  which a `/goal` can wait on.
- `scripts/guard_main.py` is a `PreToolUse` hook on Bash (`.claude/settings.json`): it refuses
  `gh pr merge --admin`, a push to `main` and writes to the rulesets, whatever a session or a
  subagent decides. It is a guardrail, not a security boundary: the ruleset on GitHub enforces.
- `python-code-style` leaves the routing table: over the merged PRs its concrete changes were
  docstrings, which the surrounding code already shows, and naming that ruff's `N` and `FBT` rules
  now enforce. The other skills stay, by path.
- A change to decision logic starts with the ADR draft, agreed before code: the ADR is the plan.

## Alternatives considered

- Prune CLAUDE.md but keep one file: a docs PR would still load the pins and the decision-logic
  list, and the duplication with `/ship` and the agents would regrow.
- `@path` imports to organise it: they load at launch, so the context cost is the same.
- Nested CLAUDE.md files per directory: they load for a directory, rules load for a glob, and the
  decision-logic set is a list of files across `src/`, `categories.yaml`, `docs/` and `tests/`.
- Keep the prohibitions as prose: they held so far, but a hook costs nothing and holds when the
  prose is lost in a long context.
- A Stop hook that runs `scripts/check.sh` before every turn ends: slow, and wrong for a docs turn;
  `/goal` gives the same finish line when the user wants it.

## Consequences

- A session that plans a change to decision logic before reading its files doesn't have the rule
  loaded yet: CLAUDE.md names the rule files so they can be read directly.
- Path rules don't fire for `qa-reviewer`, which reads the diff with `gh` in a sibling worktree: its
  checklist stays in the agent file and points at the rules for the definitions.
- The hook pattern-matches shell commands, without parsing the shell. It judges `git` and `gh` only
  in command position (the first word of a segment, after `sudo`, `env` and `VAR=value`), so a
  comment, an `echo`, a `grep` or a review line that mentions a forbidden command passes, while a
  bare `git push origin main` line inside a heredoc is refused like a command. A push with no
  refspec is judged by the branch checked out in the call's `cwd` (moved by a `cd` or a `git -C` in
  the command), so an unknown directory can't block. A false positive is a clear message and a
  rephrased command; a false negative (`sudo -u`, a GraphQL mutation, a shell function) is what the
  ruleset catches. A missing script exits 0 and an unknown directory names no branch: neither ever
  blocks.
- Each instruction file now has a different reader in mind, so `/doctor prompt-audit` is the check
  that they don't contradict each other: run it after editing any of them.

## Addendum (2026-10-15, PR #4): the hook reads the shell

The hook is now litebox-automations' `scripts/guard_main.py`, where review closed the gaps the
consequences above accept as false negatives. It reads as much of the shell as it needs, as bash
reads it, instead of pattern-matching:

- It splits only outside quotes, escapes and comments, so `--subject 'Sync Q&A' --admin` is one
  command.
- Line continuations are read as bash reads them.
- Command substitutions are judged as commands of their own. A push destination that is one whole
  substitution counts as the current branch.
- `$((…))` arithmetic is one word, unless it holds a `$(` or a backquote, whose command is judged.
- Redirections (`2>&1`, `> log`, `main>log`) are dropped from a command's words, so they hide no
  switch or refspec: `git push origin 2>&1` on `main` is refused.
- `gh pr merge --admin=true` (any value but a false one) counts as `--admin`.
- A heredoc's body is split blind, and a refusal there points to `--body-file` / `git commit -F`.
- A lone `&` separates commands.
- The reserved words (`do`, `then`, `coproc`…) and the wrappers `sudo -u root`, `timeout`, `nice`
  and `command` are skipped.
- `--all`, `--branches` and `--mirror` count as a push to `main`, and `--tags` with no refspec as no
  branch.
- `@` means `HEAD`, and a `heads/main` destination is `main`.
- A `git switch` / `git checkout` earlier in the command moves the current branch of its worktree,
  as `cd` moves the directory: `git checkout main && git push` from a feature branch is refused, and
  `git switch -c fix/x && git push -u origin HEAD` from `main` passes. A switch counts as done only
  through `&&`: after `;` it may have failed, so the branch before it is judged too. Inside a
  command substitution a `;` or a newline ends only the substitution's own chain, so the usual
  `git switch -c x && git commit -m "$(cat <<'EOF' … EOF)" && git push -u origin HEAD` passes, and a
  heredoc line is never read as a switch. A `git checkout` counts only to a local branch or `main`,
  never as a file restore (`-p`, a pathspec, a deleted file).

The decision is unchanged: a guardrail, not a security boundary. What still slips past it
(`bash -c`, `eval`, `xargs`, a refspec in a variable…) is listed in its docstring, and the ruleset
on GitHub enforces. For `gh pr merge --admin`, which bypasses the ruleset, the hook is the only
guard. This is why it reads quoting and escapes as bash does. The two repositories keep the same
file.
