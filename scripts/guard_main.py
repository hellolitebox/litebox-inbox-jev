"""Claude Code `PreToolUse` hook: refuse the Bash commands the workflow forbids outright.

    python3 -I scripts/guard_main.py < call.json    exit 2 (the reason on stderr) to block the
                                                     call, 0 to leave it to the normal flow

`.claude/settings.json` runs it before every Bash tool call, with the call as JSON on stdin
(`tool_name`, `tool_input.command`, `cwd`). CLAUDE.md says the same in prose; the hook makes it
hold whatever a session or a subagent decides:

- `gh pr merge … --admin` bypasses the ruleset (CI, `qa-verdict`, resolved threads);
- `git push` to `main`: production deploys from `main`, which changes only through a PR. A push
  with no refspec, or of `HEAD`, goes to the current branch, read from the directory the push
  runs in (the call's `cwd`, moved by a `cd` before it in the command or by `git -C`);
- `gh api` writes to a rulesets endpoint: `-X` / `--method` POST, PUT, PATCH or DELETE, or the
  POST that `gh` sends for a body (`-f`, `-F`, `--input`) without a method.

The command is split on `&&`, `||`, `;`, `|`, newlines and parentheses (a subshell, a `$(…)`) and
each part is judged alone, so `git switch main && git push origin feat/x` passes. Only `git` and
`gh` in command position count (the first word, after `sudo`, `env`, `time`, `nohup`, `exec` and
`VAR=value`), so a comment, an `echo` or a heredoc line that mentions a forbidden command passes,
while a bare `git push origin main` line inside a heredoc is refused like a command: the hook
doesn't parse heredocs. It is a guardrail against a prose rule being read loosely, not a security
boundary: a command can be written to slip past a pattern, and the ruleset on GitHub is what
enforces the gates. Standard library only; runs with the host's `python3 -I`.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import NamedTuple, TextIO

PROTECTED_BRANCH = "main"
HOOK_NAME = "scripts/guard_main.py"

_SEGMENTS = re.compile(r"\|\||&&|;|\||\n|[()]")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_RULESETS = re.compile(r"\brulesets\b")
# Words that run the command after them, with their own options: `sudo -n git push`.
_WRAPPERS = frozenset({"sudo", "env", "time", "nohup", "exec"})
# Options that take their value as the next word, so the value isn't read as a positional.
_GIT_OPTIONS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_PUSH_OPTIONS_WITH_VALUE = frozenset({"-o", "--push-option", "--receive-pack", "--exec", "--repo"})
_GH_API_OPTIONS_WITH_VALUE = frozenset(
    {
        "-X",
        "--method",
        "-f",
        "--raw-field",
        "-F",
        "--field",
        "-H",
        "--header",
        "--hostname",
        "--input",
        "-p",
        "--preview",
        "-q",
        "--jq",
        "-t",
        "--template",
        "--cache",
    }
)
# `gh api` sends a POST when one of these gives it a body and no `-X` says otherwise.
_GH_API_BODY_OPTIONS = ("-f", "--raw-field", "-F", "--field", "--input")
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class GitCommand(NamedTuple):
    """A `git` call: its subcommand, the arguments after it and the `-C` directories before it."""

    subcommand: str
    args: list[str]
    directories: list[str]


def _words(segment: str) -> list[str]:
    """`segment` as shell words, unquoted; split on whitespace when a quote is left open."""
    try:
        return shlex.split(segment)
    except ValueError:  # the segment was cut inside a quote
        return [word for word in (raw.strip("'\"") for raw in segment.split()) if word]


def command_words(segment: str) -> list[str]:
    """`segment`'s words from its command on, unquoted; `[]` when it has none.

    Skips `VAR=value` assignments and the wrappers that run another command (`sudo`, `env`, `time`,
    `nohup`, `exec`) with their options, so `GIT_TRACE=1 env -i git push` is judged as `git push`.
    """
    words = _words(segment)
    wrapped = False
    for i, word in enumerate(words):
        if word in _WRAPPERS:
            wrapped = True
        elif not (_ASSIGNMENT.match(word) or (wrapped and word.startswith("-"))):
            return words[i:]
    return []


def _is_program(word: str, name: str) -> bool:
    return word == name or word.endswith("/" + name)


def positionals(args: list[str], options_with_value: frozenset[str]) -> list[str]:
    """The words of `args` that are neither options nor the value of one in `options_with_value`."""
    result: list[str] = []
    skip = False
    for word in args:
        if skip:
            skip = False
        elif word in options_with_value:
            skip = True
        elif not word.startswith("-"):
            result.append(word)
    return result


def git_subcommand(args: list[str]) -> GitCommand | None:
    """What `git args` runs, or None when `args` names no subcommand (`git -C dir`, `git`).

    Skips git's own options (`git -C dir -c a=b push …`), keeping the `-C` directories.
    """
    directories: list[str] = []
    i = 0
    while i < len(args):
        word = args[i]
        if word == "-C":
            directories.extend(args[i + 1 : i + 2])
            i += 2
        elif word in _GIT_OPTIONS_WITH_VALUE:
            i += 2
        elif word.startswith("-"):
            i += 1
        else:
            return GitCommand(word, args[i + 1 :], directories)
    return None


def push_targets(args: list[str], current_branch: Callable[[], str | None]) -> list[str]:
    """The branches a `git push` with `args` pushes to, as far as the command says.

    `origin main` → `["main"]`; `origin HEAD:main`, `refs/heads/main`, `:main` (a delete) → the
    same; `origin feat/x main` → both. No refspec, or `HEAD`, means the current branch, read lazily
    through `current_branch` (None when it can't be known, which then names no target).
    """
    positional = positionals(args, _PUSH_OPTIONS_WITH_VALUE)
    refspecs = positional[1:] or ["HEAD"]  # the first positional is the remote
    targets: list[str] = []
    for spec in refspecs:
        _, _, destination = spec.rpartition(":")
        destination = destination.removeprefix("+").removeprefix("refs/heads/")
        if destination == "HEAD":
            branch = current_branch()
            if branch is not None:
                targets.append(branch)
        else:
            targets.append(destination)
    return targets


def gh_api_method(args: list[str]) -> str:
    """The HTTP method `gh api args` sends: `-X` / `--method`, else POST with a body, else GET."""
    method = ""
    body = False
    for i, word in enumerate(args):
        if word in {"-X", "--method"}:
            method = args[i + 1] if i + 1 < len(args) else ""
        elif word.startswith(("-X", "--method=")):  # -XPUT, -X=PUT, --method=PUT
            method = word.removeprefix("-X").removeprefix("--method=").lstrip("=")
        elif word.startswith(_GH_API_BODY_OPTIONS):  # also -fkey=value, --field=key=value
            body = True
    return method.upper() or ("POST" if body else "GET")


def writes_rulesets(args: list[str]) -> bool:
    """Whether `gh api args` writes to a rulesets endpoint (the first positional after `api`)."""
    endpoint = next(iter(positionals(args, _GH_API_OPTIONS_WITH_VALUE)), "")
    return bool(_RULESETS.search(endpoint)) and gh_api_method(args) in _WRITE_METHODS


def current_branch(cwd: Path | None) -> str | None:
    """The branch checked out in `cwd`, or None when it can't be read (no repo, detached HEAD)."""
    if cwd is None:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def _directory(cwd: Path | None, path: str) -> Path | None:
    """Where `cd path` or `git -C path` lands from `cwd`, or None when only the shell could say."""
    if path.startswith(("-", "~")) or "$" in path:
        return None
    target = Path(path) if cwd is None or Path(path).is_absolute() else cwd / path
    return target if target.is_dir() else None


def _pushes_protected_branch(args: list[str], cwd: Path | None) -> bool:
    """Whether `git args` pushes to the protected branch, `-C` directories applied to `cwd`."""
    git = git_subcommand(args)
    if git is None or git.subcommand != "push":
        return False
    for directory in git.directories:
        cwd = _directory(cwd, directory)
    return PROTECTED_BRANCH in push_targets(git.args, partial(current_branch, cwd))


def reason_to_block(command: str, cwd: Path | None) -> str | None:
    """Why `command` must not run, or None when the hook has no opinion."""
    for segment in _SEGMENTS.split(command.replace("\\\n", " ")):
        words = command_words(segment)
        if not words:
            continue
        program, args = words[0], words[1:]
        if program == "cd":
            cwd = _directory(cwd, args[0]) if len(args) == 1 else None
        elif _is_program(program, "gh"):
            if args[:2] == ["pr", "merge"] and "--admin" in args:
                return (
                    "`gh pr merge --admin` bypasses the ruleset: fix the failing gate through the "
                    "loop"
                )
            if args[:1] == ["api"] and writes_rulesets(args[1:]):
                return "never weaken the ruleset: changes to it are the maintainer's, by hand"
        elif _is_program(program, "git") and _pushes_protected_branch(args, cwd):
            return (
                f"pushing to `{PROTECTED_BRANCH}` is forbidden: it changes only through a "
                "squash-merged PR (`/ship`)"
            )
    return None


def main(stdin: TextIO | None = None) -> int:
    """Read the tool call from `stdin` and exit 2 with the reason to block it, else 0."""
    try:
        call = json.load(stdin or sys.stdin)
    except json.JSONDecodeError:
        return 0
    if not isinstance(call, dict) or call.get("tool_name") != "Bash":
        return 0
    tool_input = call.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return 0
    cwd = call.get("cwd")
    reason = reason_to_block(command, Path(cwd) if isinstance(cwd, str) else None)
    if reason is None:
        return 0
    print(f"{HOOK_NAME} blocked the command (CLAUDE.md): {reason}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
