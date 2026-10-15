r"""Claude Code `PreToolUse` hook: refuse the Bash commands the workflow forbids outright.

    python3 -I scripts/guard_main.py < call.json    exit 2 (the reason on stderr) to block the
                                                     call, 0 to leave it to the normal flow

`.claude/settings.json` runs it before every Bash tool call, with the call as JSON on stdin
(`tool_name`, `tool_input.command`, `cwd`). CLAUDE.md says the same in prose; the hook makes it
hold whatever a session or a subagent decides:

- `gh pr merge … --admin` bypasses the ruleset (CI, `qa-verdict`, resolved threads), and so does
  `--admin=<value>` for any value but a false one (`--admin=true`, `=1`);
- `git push` to `main`: production deploys from `main`, which changes only through a PR. A push
  with no refspec, or of `HEAD`, goes to the current branch, read from the directory the push
  runs in (the call's `cwd`, moved by a `cd` before it in the command or by `git -C`); `--tags`
  with no refspec pushes no branch, and `--all`, `--branches` or `--mirror` count as pushing
  `main`. `@` is `HEAD`, and a destination `heads/main` is `main`, as git reads them;
- `gh api` writes to a rulesets endpoint: `-X` / `--method` POST, PUT, PATCH or DELETE, or the
  POST that `gh` sends for a body (`-f`, `-F`, `--input`) without a method.

The command is split on `&&`, `||`, `;`, `|`, `|&`, a lone `&` (a background job, not the `&` of a
redirection: `2>&1`, `&>`, `>&`), newlines and parentheses (a subshell) and each part is judged
alone, so `git switch main && git push origin feat/x` passes. It splits only outside quotes,
escapes and comments, as bash does, so `gh pr merge 6 --subject 'Sync Q&A' --admin` is one command
and `git commit -m 'A & git push origin main later'` a commit. Backslashes are read as bash reads
them: an escaped character outside quotes is never a comment start, an operator, a redirection or
a quote (`--subject Fix\ #12 --admin` keeps its `--admin`), a backslash-newline outside `'…'` is a
line continuation (`git push \<newline>origin main` is one push), and `\\` before a newline is an
escaped backslash, so the newline still ends the command. A command substitution (`$(…)` or a
backquote, in `"…"` too) is judged as commands of its own, and the command around it stays whole;
a push destination that is one whole substitution (`git push -u origin "$(git branch
--show-current)"`, `HEAD:$(…)`, `refs/heads/$(…)`) is taken for the current branch, while one that
only contains one (`HEAD:backup/$(date +%F)`) is a branch of its own. An arithmetic expansion
(`$((1<<2))`, closed by `))`) runs nothing and is one word, unless it holds a `$(` or a backquote,
whose command runs (`$(( $(git push origin main) + 1 ))`) and is judged; `$((…) )` is a
substitution holding a subshell, as bash reads it. Redirections (`2>&1`, `> log`, `main>log`,
`<<EOF`) are dropped from a command's words wherever they stand, so they hide no switch or refspec
(`git push origin 2>&1` is a push of the current branch); a quoted or escaped `>` is a word. Where
the shell structure can't be read it splits on every operator, quotes or not, which refuses more,
never less: each line of a heredoc's body (its continuations joined), and a command with a quote,
`$(` or subshell left open (bash runs nothing past it). A heredoc line is refused like a command,
and the message then suggests passing a PR or commit body as a file (`--body-file`,
`git commit -F`). The current branch is read from git before the command runs, then moved by a
`git switch` or `git checkout` earlier in the command in the same worktree (its root, so a switch in
`src/` counts for a push from the root), as `cd` moves the directory: `git checkout main && git
push` from a feature branch is refused, and `git switch -c fix/x && git push -u origin HEAD` from
`main` passes. A switch counts as done only for what follows it through `&&`: after `;`, a newline,
a pipe, `||` or `&` it may have failed, so a push is judged against the branch before it as well
(`git checkout -b fix/x; git push -u origin HEAD` from `main` is refused). A switch whose branch the
command doesn't name (`git switch -`, `--detach`) leaves it unknown; `--track origin/x` switches to
`x`. Since `git checkout` also restores files, it counts as a switch only to a local branch of that
worktree, or to `main`: no name, several, anything after `--`, `-p` / `--patch`, a pathspec
(`'*.py'`), a deleted file or a remote-only branch switches nothing, which keeps the push judged
against the current branch (a branch created earlier in the same command isn't known yet, so a
checkout back to it is read as a file restore, which refuses more, never less). Only `git` and
`gh` in command position count (the first word, after `do`, `then`, `coproc` (its name
skipped in `coproc NAME { …; }`) and the other reserved words, `VAR=value` and the wrappers `sudo`,
`env`, `time`, `nohup`, `exec`, `timeout`, `nice` and `command` with their options, `sudo -u root`,
`timeout`'s duration and `nice -n 5` included; `command -v git` only looks `git` up and runs
nothing), so a comment, an `echo` or a heredoc line that mentions a forbidden command passes, while
a bare `git push origin main` line inside a heredoc is refused like a command, since the hook can't
tell whether the heredoc feeds a shell. It is a guardrail against a prose rule being read loosely,
not a security boundary: a command can be written to slip past a pattern (`bash -c '…'`, `sh -c`,
`eval`, `xargs git push`, an alias or a shell function, `env -C dir`, `env -S '…'`, a remote or
refspec in a variable such as `"$BR"`), and the ruleset on GitHub is what enforces the gates.
Standard library only; runs with the host's `python3 -I`.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterable
from functools import partial
from pathlib import Path
from typing import NamedTuple, TextIO

PROTECTED_BRANCH = "main"
HOOK_NAME = "scripts/guard_main.py"

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Reserved words that put the next word in command position (`if git push …; then git push …`).
_KEYWORDS = frozenset({"if", "then", "else", "elif", "while", "until", "do", "!", "{", "coproc"})
# What may follow `coproc NAME`: a name is allowed only before a compound command
# (`coproc NAME { git push; }`), so a word followed by one of these is the name, not the command.
_COMPOUND_OPENERS = frozenset({"{", "if", "while", "until", "for", "select", "case", "[["})
_RULESETS = re.compile(r"\brulesets\b")


class Wrapper(NamedTuple):
    """A word that runs the command after it (`sudo -n git push`), and how to step over its own
    words to reach that command."""

    options_with_value: frozenset[str] = frozenset()
    """Its options that take their value as the next word (`sudo -u root`, `nice -n 5`)."""
    leading_positionals: int = 0
    """Positionals it takes before the command (`timeout 60 git push`: the duration)."""
    lookup_flags: frozenset[str] = frozenset()
    """Short flags with which it only looks the command up and runs nothing (`command -v git`)."""


_WRAPPERS: dict[str, Wrapper] = {
    "sudo": Wrapper(
        frozenset(
            {
                "-u",
                "--user",
                "-g",
                "--group",
                "-C",
                "--close-from",
                "-D",
                "--chdir",
                "-p",
                "--prompt",
                "-r",
                "--role",
                "-t",
                "--type",
                "-U",
                "--other-user",
                "-T",
                "--command-timeout",
                "-R",
                "--chroot",
            }
        )
    ),
    "env": Wrapper(frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"})),
    "time": Wrapper(frozenset({"-f", "--format", "-o", "--output"})),
    "nohup": Wrapper(),
    "exec": Wrapper(frozenset({"-a"})),
    "timeout": Wrapper(frozenset({"-s", "--signal", "-k", "--kill-after"}), leading_positionals=1),
    "nice": Wrapper(frozenset({"-n", "--adjustment"})),
    "command": Wrapper(lookup_flags=frozenset({"v", "V"})),
}
# Options that take their value as the next word, so the value isn't read as a positional.
_GIT_OPTIONS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_PUSH_OPTIONS_WITH_VALUE = frozenset({"-o", "--push-option", "--receive-pack", "--exec", "--repo"})
# Push options that push every local branch, `main` among them (`--mirror` also deletes the
# remote's others), whatever the refspecs.
_PUSH_ALL_BRANCHES = frozenset({"--all", "--branches", "--mirror"})
# Refspec destinations that mean the current branch (`@` is git's shorthand for `HEAD`), and the
# prefixes git resolves a branch destination through, longest first.
_CURRENT_BRANCH_NAMES = frozenset({"HEAD", "@"})
_BRANCH_PREFIXES = ("refs/heads/", "heads/")
# `git switch` / `git checkout` options whose value is the branch they create and check out.
_SWITCH_CREATE_OPTIONS = {
    "switch": frozenset({"-c", "--create", "-C", "--force-create", "--orphan"}),
    "checkout": frozenset({"-b", "-B", "--orphan"}),
}
# Their options that check out no branch, or one the command doesn't name.
_SWITCH_UNKNOWN_OPTIONS = frozenset({"-", "--detach", "-d"})
# Their options that create a branch named after the remote-tracking one given
# (`--track origin/feat/x` → `feat/x`).
_SWITCH_TRACK_OPTIONS = frozenset({"-t", "--track"})
# `git checkout` options with which it restores files and switches no branch.
_CHECKOUT_FILE_OPTIONS = ("-p", "--patch", "--pathspec-from-file")
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
_PUSH_REASON = (
    f"pushing to `{PROTECTED_BRANCH}` is forbidden: it changes only through a squash-merged PR "
    "(`/ship`)"
)
# Added to the reason when the refused command is a line of a heredoc's body, which the hook reads
# as commands since it can't tell whether the heredoc feeds a shell.
_HEREDOC_HINT = (
    " (refused as a line of a heredoc's body, which may be fed to a shell: if it is the text of a "
    "PR, a comment or a commit message, write it to a file and pass that with `--body-file` "
    "(`gh pr create`, `gh pr comment`) or `-F` (`git commit -F`))"
)


class BranchSwitch(NamedTuple):
    """A `git switch` / `git checkout` that changes the branch: the one it checks out, or None when
    the command doesn't say (`git switch -`, `--detach`)."""

    branch: str | None


class GitCommand(NamedTuple):
    """A `git` call: its subcommand, the arguments after it and the `-C` directories before it."""

    subcommand: str
    args: list[str]
    directories: list[str]


# Where the shell structure can't be read (a heredoc's body, a quote or a `$(` left open), the
# command is split on every operator, quotes or not: that refuses more, never less. `&&` before the
# lone `&`; a lone `&` touching `<`, `>` or another `&` is a redirection's (`2>&1`, `&>file`,
# `>&2`), not a background job.
_BLIND_SEGMENTS = re.compile(r"\|\||&&|;|\||\n|[()]|(?<![<>&])&(?![<>&])")
# What stands in a segment for a command substitution, whose commands are segments of their own.
_SUBSTITUTION = "$(…)"
# A backslash-newline outside single quotes is a line continuation: bash deletes both characters.
_CONTINUATION = "\\\n"
# A continuation in text whose quoting can't be read: one not preceded by an escaped backslash.
_BLIND_CONTINUATION = re.compile(r"(?<!\\)((?:\\\\)*)\\\n")
# `_Splitter._prev` after word text: an escaped character, a quoted string or a substitution, none
# of which is whitespace, an operator or a redirection.
_WORD_TEXT = "w"
# What `_Splitter._prev` holds where a word may start: a command's start, whitespace, an operator.
_WORD_BOUNDARIES = frozenset({"", " ", "\t", "\n", ";", "&", "|", "(", ")"})
# Characters that end an unquoted heredoc delimiter word.
_WORD_END = frozenset(" \t\n;&|()<>")
_TWO_CHAR_OPERATORS = frozenset({("&", "&"), ("|", "|"), ("|", "&")})
_ARITHMETIC = "$(("
# A redirection word as written: an optional fd (`2`, `{fd}`) or `&`, the operator, and its target
# when it is in the same word (`2>&1`, `>/dev/null`); without one, the target is the next word.
# What may come before a `<` / `>` in the same redirection word: an fd or `&`, and operator
# characters (`2>>`, `<<<`, `>&`).
_REDIRECTION_PREFIX = re.compile(r"(?:\d+|\{[A-Za-z_]\w*\}|&)?[<>]*&?")
_REDIRECTION = re.compile(
    r"^(?:\d+|\{[A-Za-z_]\w*\}|&)?(?:<<<|<<-?|<>|>>|>\||>&|<&|>|<)(?P<target>.*)\Z", re.DOTALL
)


class _UnbalancedError(Exception):
    """A quote, a `$(`, a backquote or a subshell left open: bash wouldn't run the command."""


class Segment(NamedTuple):
    """A simple command the hook judges alone, and whether it is a line of a heredoc's body."""

    text: str
    heredoc: bool = False
    ends_chain: bool = False
    """Whether what follows it runs whatever its exit status (after `;`, `|`, `||`, `&`, a
    newline, or where the structure can't be read), rather than only once it succeeded (`&&`)."""


class _Splitter:
    r"""Splits a command into segments the way bash reads it: only outside quotes.

    One pass over the command with a stack of segment buffers, one per command substitution
    (`$(…)`, a backquote) being read, so a substitution's commands become segments of their own
    and the command around it stays whole (`gh pr merge "$(cat n)" --admin`). A heredoc's body is
    data the hook can't read (it may be fed to a shell), so each of its lines is split blind.

    Backslashes are read as bash reads them: outside quotes `\x` is a literal `x` (never a comment
    start, an operator, a redirection's `<` / `>` or a quote) and a backslash-newline is deleted
    (a line continuation; `\\` before a newline is an escaped backslash, and the newline ends
    the line). In `"…"` it escapes only `$`, a backquote, `"`, `\` and a newline; in `'…'` nothing.
    """

    def __init__(self, command: str) -> None:
        self._text = command
        self._i = 0
        self._segments: list[Segment] = []
        self._buffers: list[list[str]] = [[]]
        self._heredocs: list[tuple[str, bool]] = []  # (delimiter, strip leading tabs)
        # The unquoted, unescaped character read last, `_WORD_TEXT` after an escape, a quoted
        # string or a substitution, "" at the start of a command: what `#` and `&` look back at.
        self._prev = ""
        # Whether an `&&` was read last, so a newline after it continues the list (`a &&\n b`).
        self._after_and = False

    def split(self) -> list[Segment]:
        self._unquoted(closer=None)
        self._flush()
        return self._segments

    def _peek(self, offset: int = 0) -> str:
        index = self._i + offset
        return self._text[index] if 0 <= index < len(self._text) else ""

    def _emit(self, text: str) -> None:
        self._buffers[-1].append(text)

    def _flush(self, *, ends_chain: bool = False) -> None:
        """End the segment being read; `ends_chain` when the next command runs whatever its exit
        status. An operator right after a subshell (`(a); b`) ends the subshell's last command."""
        buffer = self._buffers[-1]
        if buffer:
            self._segments.append(Segment("".join(buffer), ends_chain=ends_chain))
            buffer.clear()
        elif ends_chain and self._segments:
            self._segments[-1] = self._segments[-1]._replace(ends_chain=True)

    def _at_continuation(self) -> bool:
        return self._text.startswith(_CONTINUATION, self._i)

    def _peek_past_continuations(self) -> str:
        r"""The character after this one, line continuations skipped (`&\<newline>>f` is `&>f`)."""
        index = self._i + 1
        while self._text.startswith(_CONTINUATION, index):
            index += len(_CONTINUATION)
        return self._text[index] if index < len(self._text) else ""

    def _unquoted(self, closer: str | None) -> None:
        """Read unquoted text up to `closer` (`)` or a backquote), or to the end when None."""
        while self._i < len(self._text):
            char = self._peek()
            if char == closer:
                self._i += 1
                self._prev = _WORD_TEXT
                return
            if self._at_continuation():
                self._i += len(_CONTINUATION)  # deleted; what came before still counts
            elif self._quote_or_substitution(char):
                self._prev = _WORD_TEXT
                self._after_and = False
            elif not self._operator(char):
                self._emit(char)
                self._i += 1
                self._prev = char
                self._after_and = self._after_and and char in {" ", "\t"}
        if closer is not None:
            raise _UnbalancedError

    def _quote_or_substitution(self, char: str) -> bool:
        """Read the escape, quoted string, arithmetic expansion or substitution starting here; False
        when none does."""
        if self._arithmetic():
            return True
        after = self._peek(1)
        if char == "\\":
            self._emit(self._text[self._i : self._i + 2])
            self._i += 2
        elif char == "'":
            self._quoted("'")
        elif char == '"':
            self._double_quoted()
        elif char == "$" and after == "'":
            self._quoted("$'")
        elif char == "$" and after == "(":
            self._i += 2
            self._substitution(")")
        elif char == "`":
            self._i += 1
            self._substitution("`")
        else:
            return False
        return True

    def _operator(self, char: str) -> bool:
        """Act on the comment, heredoc or operator starting here; False when none does."""
        after = self._peek(1)
        if char == "#" and self._starts_word():
            self._skip_comment()
        elif char == "<" and after == "<":
            if self._peek(2) == "<":  # a here-string, not a heredoc
                self._emit("<<<")
                self._i += 3
                self._prev = "<"
            else:
                self._heredoc_operator()
                self._prev = _WORD_TEXT
        elif char == "\n":
            self._i += 1
            self._flush(ends_chain=not self._after_and)
            self._prev = char
            self._heredoc_bodies()
        elif char == "(":  # a subshell; `$(` is a substitution, read before this
            self._i += 1
            self._flush()
            self._prev = char
            self._unquoted(closer=")")
            self._flush()
            self._prev = ")"
        elif (char, after) in _TWO_CHAR_OPERATORS:
            self._i += 2
            self._flush(ends_chain=char != "&")
            self._prev = char
            self._after_and = char == "&"
        elif char in {";", "|", ")"} or (char == "&" and not self._is_redirection()):
            self._i += 1
            self._flush(ends_chain=char != ")")
            self._prev = char
        else:
            return False
        return True

    def _quoted(self, opener: str) -> None:
        """Copy a `'…'` (no escapes) or `$'…'` (backslash escapes) string."""
        start = self._i
        self._i += len(opener)
        while self._i < len(self._text):
            char = self._peek()
            if char == "\\" and opener == "$'":
                self._i += 2
            elif char == "'":
                self._i += 1
                self._emit(self._text[start : self._i])
                return
            else:
                self._i += 1
        raise _UnbalancedError

    def _double_quoted(self) -> None:
        """Copy a `"…"` string; the substitutions in it run, so their commands are segments."""
        self._emit('"')
        self._i += 1
        while self._i < len(self._text):
            char = self._peek()
            if self._at_continuation():
                self._i += len(_CONTINUATION)
            elif char == "\\":  # escapes the next character if special here, else is literal
                self._emit(self._text[self._i : self._i + 2])
                self._i += 2
            elif char == '"':
                self._emit('"')
                self._i += 1
                return
            elif self._arithmetic():
                continue
            elif char == "$" and self._peek(1) == "(":
                self._i += 2
                self._substitution(")")
            elif char == "`":
                self._i += 1
                self._substitution("`")
            else:
                self._emit(char)
                self._i += 1
        raise _UnbalancedError

    def _arithmetic(self) -> bool:
        """Copy the arithmetic expansion starting here as one word; False when none does.

        As bash reads it: `$((` opens one only if the parenthesis closing its inner `(` is followed
        by `)`, so `$((1<<2))` is arithmetic (its `<<` no heredoc) and `$((git push) )` a
        substitution holding a subshell. One that holds a `$(` or a backquote runs the command in
        it (`$(( $(git push) + 1 ))`), so it is left to the substitution reader, which judges it.
        """
        if not self._text.startswith(_ARITHMETIC, self._i):
            return False
        depth = 0
        index = self._i + len(_ARITHMETIC)
        while index < len(self._text):
            char = self._text[index]
            if char == "(":
                depth += 1
            elif char == ")" and depth:
                depth -= 1
            elif char == ")":
                if not self._text.startswith("))", index):
                    return False
                end = index + 2
                span = self._text[self._i : end]
                if "$(" in span[len(_ARITHMETIC) :] or "`" in span:
                    return False
                self._emit(span)
                self._i = end
                return True
            index += 1
        return False

    def _substitution(self, closer: str) -> None:
        """Read a command substitution into segments of its own; leave a placeholder for it."""
        self._buffers.append([])
        self._prev = ""  # a command starts here: `$(# a comment` …
        self._unquoted(closer)
        self._flush()
        self._buffers.pop()
        self._emit(_SUBSTITUTION)

    def _starts_word(self) -> bool:
        r"""Whether a word starts here: after whitespace or an operator, never after an escape
        (`Fix\ #12` is one word, `Fix #12`)."""
        return self._prev in _WORD_BOUNDARIES

    def _is_redirection(self) -> bool:
        r"""Whether the `&` here belongs to a redirection (`2>&1`, `&>f`, `>&2`, `<&3`); an escaped
        `\>` is a literal `>`, so `a\>&b` backgrounds `a` and runs `b`."""
        return self._prev in {"<", ">"} or self._peek_past_continuations() in {"<", ">"}

    def _skip_comment(self) -> None:
        end = self._text.find("\n", self._i)
        self._i = len(self._text) if end == -1 else end

    def _heredoc_operator(self) -> None:
        """Read `<<WORD`, `<<-WORD` or `<<'WORD'`; its body starts after the next newline."""
        self._emit("<<")
        self._i += 2
        strip_tabs = self._peek() == "-"
        if strip_tabs:
            self._emit("-")
            self._i += 1
        while self._peek() in {" ", "\t"}:
            self._i += 1
        start = self._i
        while self._i < len(self._text) and self._peek() not in _WORD_END:
            self._i += 1
        word = self._text[start : self._i]
        self._emit(word)
        self._heredocs.append(
            (word.replace("'", "").replace('"', "").replace("\\", ""), strip_tabs)
        )

    def _heredoc_bodies(self) -> None:
        r"""Consume the bodies of the heredocs opened on the line just read, split blind.

        A line ending in a line continuation is joined to the next before it is split, since a
        shell fed the body would join them (`git push \<newline>origin main`).
        """
        for delimiter, strip_tabs in self._heredocs:
            pending = ""
            while self._i < len(self._text):
                end = self._text.find("\n", self._i)
                end = len(self._text) if end == -1 else end
                line = self._text[self._i : end]
                self._i = min(end + 1, len(self._text))
                if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                    break
                joined = _BLIND_CONTINUATION.sub(r"\1", f"{pending}{line}\n")
                if joined.endswith("\n"):
                    pending = ""
                    self._extend_blind(joined.removesuffix("\n"), heredoc=True)
                else:  # the line ended in a continuation
                    pending = joined
            if pending:
                self._extend_blind(pending, heredoc=True)
        self._heredocs.clear()

    def _extend_blind(self, text: str, *, heredoc: bool) -> None:
        self._segments.extend(
            Segment(part, heredoc, ends_chain=True) for part in _BLIND_SEGMENTS.split(text)
        )


def segments(command: str) -> list[Segment]:
    """`command` cut into the simple commands the hook judges one by one.

    Splits on `&&`, `||`, `;`, `|`, `|&`, a lone `&`, newlines and parentheses only outside quotes,
    escapes and comments, and gives each command substitution's commands their own segments. A
    command bash would refuse for an open quote, `$(` or subshell is split blind
    (`_BLIND_SEGMENTS`), both with its line continuations joined and as written, since which
    backslash-newlines are continuations depends on the quoting the hook couldn't read.
    """
    try:
        return _Splitter(command).split()
    except _UnbalancedError:
        parts = _BLIND_SEGMENTS.split(command)
        joined = _BLIND_CONTINUATION.sub(r"\1", command)
        if joined != command:
            parts += _BLIND_SEGMENTS.split(joined)
        return [Segment(part, ends_chain=True) for part in parts]


def _raw_words(segment: str) -> list[str]:
    """`segment` split on unquoted, unescaped whitespace, each word as written (quotes kept).

    An unquoted `<` or `>` also starts a word unless only an fd (`2`, `{fd}`) or `&` comes before it
    in its word, as bash reads `main>log` as the word `main` and the redirection `>log`. Raises
    ValueError when a quote is left open, as `shlex.split` does.
    """
    words: list[str] = []
    word: list[str] = []
    quote = ""
    i = 0
    while i < len(segment):
        char = segment[i]
        if char == "\\" and quote != "'":
            word.append(segment[i : i + 2])
            i += 2
            continue
        if quote:
            quote = "" if char == quote else quote
        elif char in {"'", '"'}:
            quote = char
        elif char in {"<", ">"} and word and not _REDIRECTION_PREFIX.fullmatch("".join(word)):
            before = "".join(word)
            carried = "&" if before.endswith("&") and char == ">" else ""  # `a&>f`
            words.append(before.removesuffix(carried))
            word = [carried] if carried else []
        elif char.isspace():
            if word:
                words.append("".join(word))
                word = []
            i += 1
            continue
        word.append(char)
        i += 1
    if quote:
        raise ValueError("a quote is left open")
    if word:
        words.append("".join(word))
    return words


def _without_redirections(raw_words: list[str]) -> list[str]:
    """`raw_words` without their redirections (`2>&1`, `> f`, `&>>log`, `<<EOF`): only an unquoted,
    unescaped operator is one, so `--body '>'` keeps its words."""
    result: list[str] = []
    skip = False
    for word in raw_words:
        if skip:
            skip = False
            continue
        match = _REDIRECTION.match(word)
        if match is None:
            result.append(word)
        else:
            skip = not match.group("target")  # `> f`: the target is the next word
    return result


def _words(segment: str) -> list[str]:
    """`segment` as shell words, unquoted and without redirections; split on whitespace when a
    quote is left open."""
    try:
        return [" ".join(shlex.split(word)) for word in _without_redirections(_raw_words(segment))]
    except ValueError:  # the segment was cut inside a quote, or after a lone backslash
        raw = _without_redirections(segment.split())
        return [word for word in (part.strip("'\"") for part in raw) if word]


def command_words(segment: str) -> list[str]:
    """`segment`'s words from its command on, unquoted; `[]` when it has none.

    Skips reserved words (`do`, `then`, `!`…), `VAR=value` assignments and the wrappers that run
    another command (`_WRAPPERS`) with their options, those options' values and their leading
    positionals, so `do git push`, `GIT_TRACE=1 env -i git push`, `sudo -u root git push`,
    `coproc git push`, `coproc NAME { git push` and `timeout -s KILL 60 git push` are judged as
    `git push`. `command -v git` (or `-V`, `-pv`) runs nothing, so it has none.
    """
    words = _words(segment)
    wrapper: Wrapper | None = None  # the wrapper whose words are being stepped over
    leading = 0  # its leading positionals still to skip
    skip = False
    for i, word in enumerate(words):
        if skip:
            skip = False
        elif wrapper is None and word in _KEYWORDS:
            # `coproc NAME { …; }`: the name before a compound command runs nothing.
            skip = word == "coproc" and _is_coproc_name(words, i + 1)
        elif word in _WRAPPERS:
            wrapper = _WRAPPERS[word]
            leading = wrapper.leading_positionals
        elif wrapper is not None and word.startswith("-"):
            if _is_short_cluster(word) and not wrapper.lookup_flags.isdisjoint(word[1:]):
                return []
            skip = word in wrapper.options_with_value
        elif leading:
            leading -= 1
        elif not _ASSIGNMENT.match(word):
            return words[i:]
    return []


def _is_coproc_name(words: list[str], i: int) -> bool:
    """Whether `words[i]`, right after `coproc`, is the name of a compound command it runs."""
    return i + 1 < len(words) and words[i + 1] in _COMPOUND_OPENERS


def _is_short_cluster(word: str) -> bool:
    """Whether `word` is one or more short options (`-v`, `-pv`), not a long one or `-`/`--`."""
    return len(word) > 1 and word.startswith("-") and not word.startswith("--")


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


def branch_switch(git: GitCommand, cwd: Path | None) -> BranchSwitch | None:
    """The branch `git` checks out in `cwd` (its `-C` directories applied), or None when it changes
    no branch, or the hook can't tell that it does (the current branch then stays as it was).

    `switch X` → `X`; `switch -c X` / `checkout -b X` (`-C`, `-B`, `--orphan`, the `--create=X` and
    `-cX` forms too) → `X`, whatever start point follows; `--track origin/X` without one → `X`.
    `-` or `--detach` → an unknown branch. `checkout X` is a switch only when `X` is a local branch
    in `cwd`, or `main`: `checkout` also restores files, so no name, more than one, anything after
    `--`, `-p` / `--patch`, a pathspec (`'*.py'`) or any other word switches nothing.
    """
    create_options = _SWITCH_CREATE_OPTIONS.get(git.subcommand)
    if create_options is None:
        return None
    created = _created_branch(git.args, create_options)
    if created is not None:
        return created
    args, paths = _split_at(git.args, "--")
    names = positionals(args, frozenset())
    checkout = git.subcommand == "checkout"
    if checkout and (paths or any(word.startswith(_CHECKOUT_FILE_OPTIONS) for word in args)):
        return None
    if not _SWITCH_UNKNOWN_OPTIONS.isdisjoint(args):
        return BranchSwitch(None)
    if len(names) != 1:
        return None
    name = names[0]
    if any(word.partition("=")[0] in _SWITCH_TRACK_OPTIONS for word in args):
        return BranchSwitch(name.partition("/")[2] or name)
    if checkout and name != PROTECTED_BRANCH and not _is_local_branch(cwd, name):
        return None
    return BranchSwitch(name)


def _is_local_branch(cwd: Path | None, name: str) -> bool:
    """Whether `name` is a branch of the repository in `cwd`; False when that can't be read."""
    if cwd is None:
        return False
    try:
        subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
            capture_output=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


def _created_branch(args: list[str], options: frozenset[str]) -> BranchSwitch | None:
    """The branch an option in `options` creates (`-c X`, `--create=X`, `-cX`), or None."""
    for i, word in enumerate(args):
        option, equals, value = word.partition("=")
        if word in options:
            return BranchSwitch(args[i + 1] if i + 1 < len(args) else None)
        if equals and option in options:
            return BranchSwitch(value)
        if word[:2] in options and len(word) > 2:
            return BranchSwitch(word[2:])
    return None


def _split_at(words: list[str], separator: str) -> tuple[list[str], list[str]]:
    """`words` before and after the first `separator` (all before, none after, without one)."""
    if separator not in words:
        return words, []
    i = words.index(separator)
    return words[:i], words[i + 1 :]


def push_targets(args: list[str], current_branches: Callable[[], Iterable[str]]) -> list[str]:
    """The branches a `git push` with `args` pushes to, as far as the command says.

    `origin main` → `["main"]`; `origin HEAD:main`, `refs/heads/main`, `HEAD:heads/main`, `:main`
    (a delete) → the same; `origin feat/x main` → both. No refspec, or `HEAD` (`@`), means the
    current branch, read lazily through `current_branches`: every branch that may be checked out
    (none when it can't be known), except with `--tags`, which then pushes only tags. A
    destination that is one whole command substitution (`"$(git branch --show-current)"`,
    `HEAD:$(…)`, `refs/heads/$(…)`) is taken for the current branch too, which is what such a
    substitution almost always prints; one that only contains a substitution
    (`HEAD:backup/$(date +%F)`) names a branch of its own, never `main`. `--all`,
    `--branches` and `--mirror` → `["main"]`.
    """
    if not _PUSH_ALL_BRANCHES.isdisjoint(args):
        return [PROTECTED_BRANCH]
    positional = positionals(args, _PUSH_OPTIONS_WITH_VALUE)
    refspecs = positional[1:]  # the first positional is the remote
    if not refspecs:
        if "--tags" in args:
            return []
        refspecs = ["HEAD"]
    targets: list[str] = []
    for spec in refspecs:
        _, _, destination = spec.rpartition(":")
        destination = _branch_name(destination.removeprefix("+"))
        if destination in _CURRENT_BRANCH_NAMES or destination == _SUBSTITUTION:
            targets.extend(current_branches())
        else:
            targets.append(destination)
    return targets


def _branch_name(destination: str) -> str:
    """The branch a push destination names: `refs/heads/main` and `heads/main` (which git DWIMs to
    `refs/heads/main`) are `main`; anything else is returned as it is."""
    for prefix in _BRANCH_PREFIXES:
        if destination.startswith(prefix):
            return destination.removeprefix(prefix)
    return destination


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


def _git_directory(cwd: Path | None, git: GitCommand) -> Path | None:
    """Where `git` runs: `cwd` with its `-C` directories applied."""
    for directory in git.directories:
        cwd = _directory(cwd, directory)
    return cwd


def _toplevel(cwd: Path) -> Path | None:
    """The root of the worktree `cwd` is in, or None when it isn't in one."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    root = result.stdout.strip()
    return Path(root).resolve() if root else None


class _Branches:
    """The branches that may be checked out in each worktree as the command goes: the one git
    reports, unless a `git switch` / `git checkout` earlier in the command moved it.

    A worktree is keyed by its root, so `cd src && git checkout main && cd .. && git push` sees the
    switch. A switch is certain for what runs after it only through `&&`: once a `;`, a newline, a
    pipe, `||` or `&` follows, it may have failed (`git checkout -b x; git push` with `x` taken), so
    the branches from before it stay possible too (`settle`).
    """

    def __init__(self) -> None:
        self._possible: dict[Path | None, frozenset[str | None]] = {}
        self._before: dict[Path | None, frozenset[str | None]] = {}  # switched since `settle`
        self._keys: dict[Path, Path | None] = {}

    def _key(self, cwd: Path | None) -> Path | None:
        if cwd is None:
            return None
        resolved = cwd.resolve()
        if resolved not in self._keys:
            self._keys[resolved] = _toplevel(resolved) or resolved
        return self._keys[resolved]

    def _branches(self, key: Path | None, cwd: Path | None) -> frozenset[str | None]:
        if key not in self._possible:
            self._possible[key] = frozenset({current_branch(cwd)})
        return self._possible[key]

    def switch(self, cwd: Path | None, branch: str | None) -> None:
        key = self._key(cwd)
        self._before[key] = self._before.get(key, frozenset()) | self._branches(key, cwd)
        self._possible[key] = frozenset({branch})

    def settle(self) -> None:
        """What follows runs even if the switches since the last `settle` failed."""
        for key, before in self._before.items():
            self._possible[key] |= before
        self._before.clear()

    def current(self, cwd: Path | None) -> list[str]:
        """The branches that may be checked out in `cwd`, unknown ones left out."""
        return sorted(b for b in self._branches(self._key(cwd), cwd) if b is not None)


def reason_to_block(command: str, cwd: Path | None) -> str | None:
    """Why `command` must not run, or None when the hook has no opinion."""
    branches = _Branches()
    for segment in segments(command):
        reason, cwd = _judge(segment, cwd, branches)
        if reason is not None:
            return reason + (_HEREDOC_HINT if segment.heredoc else "")
        if segment.ends_chain:
            branches.settle()
    return None


def _judge(
    segment: Segment, cwd: Path | None, branches: _Branches
) -> tuple[str | None, Path | None]:
    """Why `segment` must not run (or None), and the directory the next segment runs in."""
    words = command_words(segment.text)
    if not words:
        return None, cwd
    program, args = words[0], words[1:]
    if program == "cd":
        return None, _directory(cwd, args[0]) if len(args) == 1 else None
    git = git_subcommand(args) if _is_program(program, "git") else None
    if git is None:
        return _reason_for(program, args), cwd
    directory = _git_directory(cwd, git)
    switch = branch_switch(git, directory)
    if switch is not None:
        branches.switch(directory, switch.branch)
    elif git.subcommand == "push" and PROTECTED_BRANCH in push_targets(
        git.args, partial(branches.current, directory)
    ):
        return _PUSH_REASON, cwd
    return None, cwd


def _reason_for(program: str, args: list[str]) -> str | None:
    """Why the simple command `program args` (not a `git` one) must not run, or None."""
    if _is_program(program, "gh"):
        if args[:2] == ["pr", "merge"] and any(map(_is_admin_flag, args)):
            return (
                "`gh pr merge --admin` bypasses the ruleset: fix the failing gate through the loop"
            )
        if args[:1] == ["api"] and writes_rulesets(args[1:]):
            return "never weaken the ruleset: changes to it are the maintainer's, by hand"
    return None


def _is_admin_flag(word: str) -> bool:
    """Whether `word` turns on `gh pr merge`'s `--admin`: bare, or `--admin=<value>` with any
    value but the false ones gh's flag parser reads (`=true`, `=1`, `=TRUE`, an invalid one)."""
    option, equals, value = word.partition("=")
    return option == "--admin" and not (equals and value.lower() in {"false", "f", "0"})


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
