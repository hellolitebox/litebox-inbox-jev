r"""Claude Code `PreToolUse` hook: refuse the Bash commands the workflow forbids (ADR 0057).

    python3 -I scripts/guard_main.py < call.json    exit 2 (the reason on stderr) to block the
                                                     call, 0 to leave it to the normal flow

`.claude/settings.json` runs it before every Bash tool call, with the call as JSON on stdin
(`tool_name`, `tool_input.command`, `cwd`). It refuses four things:

1. a write to a rulesets endpoint: `gh api …rulesets…` with `-X`/`--method` POST, PUT, PATCH or
   DELETE, or with a body (`-f`, `-F`, `--raw-field`, `--field`, `--input`), which `gh` sends as a
   POST. GitHub can't refuse it: the user's token is an admin's;
2. a force push, to any branch: `-f` (alone or in a cluster such as `-uf`), `--force`,
   `--force-with-lease`, `--force-if-includes`, or a `+` refspec. The ruleset "all branches: no
   force-push" refuses it on GitHub too; a branch out of date with `main` is merged, never rebased
   and forced;
3. `gh pr merge --admin` (any value but a false one);
4. a push to `main`: a destination `main`, `heads/main` or `refs/heads/main`, `--all`, `--mirror`
   or `--branches`, or no destination, `HEAD` or `@` from a checkout on `main` (the call's `cwd`,
   moved by `cd` or `git -C`).

GitHub's ruleset "main: PR + CI" refuses 3 and 4 itself (it has no bypass actors): here they are an
early, clear message. The hook reads words, not bash. The command's line continuations are joined,
its newlines and operators (`;`, `&&`, `||`, `|`, `&`, `(`, `)`) split it into commands (a heredoc's
body lines and a `$(…)` included), and `shlex` reads its quotes and escapes. A command `shlex`
can't read (an unclosed quote) is split on whitespace alone. Either way it finds more commands,
never fewer: when in doubt it refuses. It doesn't expand variables, aliases, `bash -c '…'`, `eval`
or `xargs`, and isn't a security boundary: the rulesets are. Standard library only; runs with the
host's `python3 -I`.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO

PROTECTED_BRANCH = "main"
HOOK_NAME = "scripts/guard_main.py"

SEPARATORS = frozenset({";", "&&", "||", "|", "|&", "&", "(", ")", ";;", "&;", "{", "}"})
# Reserved words that come before the command they run.
PREFIXES = frozenset({"if", "then", "else", "elif", "while", "until", "do", "!"})
# Commands that run another one, and their options that take a value (`sudo -u root`).
WRAPPERS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T"}),
    "env": frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "exec": frozenset({"-a"}),
    "nohup": frozenset(),
    "command": frozenset(),
    "builtin": frozenset(),
    "coproc": frozenset(),
}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
REDIRECTION = re.compile(r"^[0-9]*[<>&]+$")
# A backslash-newline outside an escaped backslash (`\\` before the newline is a backslash).
CONTINUATION = re.compile(r"(?<!\\)((?:\\\\)*)\\\n")
# `$(…)`, `$((…)` and backquotes, wherever they are (in double quotes too): their body runs.
SUBSTITUTIONS = (re.compile(r"\$\(\(?([^()]*)\)"), re.compile(r"`([^`]*)`"))

GIT_OPTIONS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
PUSH_OPTIONS_WITH_VALUE = frozenset({"-o", "--push-option", "--receive-pack", "--exec", "--repo"})
FORCE_OPTIONS = ("--force", "--force-with-lease", "--force-if-includes")
ALL_BRANCHES = ("--all", "--mirror", "--branches")
# `git -c key=value` that makes a plain push force, mirror or run something else.
FORCING_CONFIG = re.compile(r"^(alias\.|include|remote\.[^=]*\.(push|mirror)\b)", re.IGNORECASE)
# Environment that passes git config without `-c` (`GIT_CONFIG_COUNT`/`KEY_n`/`VALUE_n`, …).
CONFIG_ENV = re.compile(r"^GIT_CONFIG")
# A GraphQL query whose text comes from a variable or a substitution (`query="$Q"`): unseen. A
# query in single quotes may hold GraphQL's own `$var`s, and isn't matched.
# The only words with an expansion a `gh api` write or GraphQL call may hold: a plainly named
# field set from a plain variable (`-F owner="$OWNER"`, `n=$n`), never `query`. Anything else (an
# endpoint from a substitution, a query from a variable, nested quotes) could hide a ruleset write.
PLAIN_FIELD = re.compile(
    r'([A-Za-z_][A-Za-z0-9_]*)=(?:"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?"|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?)'
)
GRAPHQL_FIELD_OPTIONS = ("-f", "-F", "--raw-field", "--field")
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
BODY_OPTIONS = ("-f", "-F", "--raw-field", "--field", "--input")
FALSE_VALUES = frozenset({"false", "f", "0"})  # what Go's ParseBool, which gh uses, reads as false

HEREDOC_HINT = (
    " (if this is the text of a PR, a comment or a commit message, write it to a file and pass it "
    "with `--body-file` or `git commit -F`)"
)


def commands(command: str) -> list[list[str]]:
    """`command` as the commands it runs, each a list of words: its line continuations joined."""
    return _split(CONTINUATION.sub(r"\1", command))


def views(command: str, depth: int = 0) -> list[list[str]]:
    """Every command `command` may run, read several ways (continuations joined and not, and each
    substitution's body on its own), so that a reading that misses one is covered by another."""
    found: list[list[str]] = []
    for text in dict.fromkeys((CONTINUATION.sub(r"\1", command), command)):
        found += _split(text)
        if depth < 3:  # `$( `…` )`: a substitution in one is read as well
            expanded = _expanded_text(text)
            for pattern in SUBSTITUTIONS:
                for inner in pattern.findall(expanded):
                    found += views(inner, depth + 1)
    return found


# A heredoc header whose whole delimiter is quoted and ends its line (`… <<'EOF'`).
HEREDOC_HEADER = re.compile(r"<<-?[ \t]*(['\"])([A-Za-z_][A-Za-z0-9_]*)\1([^\n]*)\n", re.M)


def _expanded_text(text: str) -> str:
    """`text` without the bodies of heredocs that are certainly quoted heredocs (`<<'EOF'`), where
    bash expands nothing: a backquoted command there is text (a Markdown code span in a PR body),
    not a command. A heredoc is exempted only when the text before it, read from the start, leaves
    no quote open and holds no comment, `$`, backquote or backslash, so that a `<<'EOF'` inside a
    string or a comment can't hide what follows; one that isn't certain exempts nothing at all."""
    kept: list[str] = []
    position = 0
    for header in HEREDOC_HEADER.finditer(text):
        if header.start() < position:
            continue
        # The text before it, and the rest of its line (`>/dev/null && echo ok`), which stays in
        # the reading: both plain, or nothing is exempted.
        if not _plain(text[position : header.start()]) or not _plain(header.group(3)):
            return text
        closing = re.compile(rf"^[ \t]*{header.group(2)}[ \t]*$", re.M).search(text, header.end())
        if closing is None:
            return text
        kept.append(text[position : header.end()])
        position = closing.end()
    kept.append(text[position:])
    return "".join(kept)


def _plain(text: str) -> bool:
    """Whether `text` is plain enough to read its quotes with certainty: every quote closed, and no
    unquoted comment, no `$` outside single quotes, no backquote or backslash anywhere."""
    # An earlier heredoc (unquoted, or two on a line) has a body that can't be told apart, and in
    # `((…))` a `<<` is a shift, not a heredoc.
    if "<<" in text or "((" in text:
        return False
    quote = ""
    previous = "\n"
    for char in text:
        if char in "`\\":
            return False
        if quote == "'":
            quote = "" if char == "'" else quote
        elif quote == '"':
            if char == "$":
                return False
            quote = "" if char == '"' else quote
        elif char in "'\"":
            quote = char
        elif char == "$" or (char == "#" and (previous.isspace() or previous in ";&|()")):
            return False
        previous = char
    return not quote


def _split(text: str) -> list[list[str]]:
    text = text.replace("\n", " ; ")
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""  # `#` mid-word isn't a comment in bash; a comment is just words
        words = list(lexer)
    except ValueError:  # an unclosed quote: whitespace alone, which finds more commands
        words = re.findall(r"\|\||&&|[;|&(){}`]|[^\s;|&(){}`]+", text)
    out: list[list[str]] = [[]]
    for word in words:
        if word in SEPARATORS:
            out.append([])
        else:
            out[-1].append(word)
    return [c for c in out if c]


def program(words: list[str]) -> list[str]:
    """The words from the command actually run: leading `VAR=value`, reserved words and wrappers
    (`sudo -u root`, `env`, `timeout 5`, `nice -n 5`…) skipped, and redirections dropped. Empty for
    `command -v x`, which only looks `x` up."""
    words = [w for i, w in enumerate(words) if not _is_redirection(words, i)]
    i = 0
    while i < len(words):
        word = words[i]
        if word in PREFIXES or ASSIGNMENT.match(word):
            i += 1
        elif word in WRAPPERS:
            if word == "command" and i + 1 < len(words) and words[i + 1] in ("-v", "-V"):
                return []
            i += 1
            while i < len(words) and (words[i].startswith("-") or ASSIGNMENT.match(words[i])):
                i += 2 if words[i] in WRAPPERS[word] else 1
            if word == "timeout" and i < len(words) and re.match(r"^[0-9.]+[smhd]?$", words[i]):
                i += 1
        else:
            break
    return words[i:]


def _is_redirection(words: list[str], i: int) -> bool:
    """Whether `words[i]` is part of a redirection: the operator (`>`, `>&`, `<<`), the file
    descriptor before it (`2` in `2>/dev/null`) or the word it takes. An option (`--admin` after a
    quoted `'>'`) is never taken: it may not be one, and dropping it would hide it."""

    def operator(j: int) -> bool:
        return 0 <= j < len(words) and bool(REDIRECTION.match(words[j])) and not words[j].isdigit()

    word = words[i]
    if operator(i) or (word.isdigit() and operator(i + 1)):
        return True
    return operator(i - 1) and not word.startswith("-")


def _named(word: str, name: str) -> bool:
    return word == name or word.endswith("/" + name)


def _positionals(args: list[str], with_value: frozenset[str]) -> tuple[list[str], list[str]]:
    """`args`' options and positionals (an option in `with_value` takes the next word)."""
    options: list[str] = []
    positionals: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            positionals.extend(args[i + 1 :])
            break
        if arg.startswith("-") and arg != "-":
            options.append(arg)
            i += 2 if arg in with_value else 1
            continue
        positionals.append(arg)
        i += 1
    return options, positionals


def push_reason(args: list[str], branch_here: str | None) -> str | None:
    """Why `git push args` is refused, or None. `branch_here` is the checkout's branch."""
    options, positionals = _positionals(args, PUSH_OPTIONS_WITH_VALUE)
    forced = any(_abbreviates(o, FORCE_OPTIONS) for o in options) or any(
        not o.startswith("--") and "f" in o[1:] for o in options
    )
    refspecs = positionals[1:]
    if forced or any(r.startswith("+") for r in refspecs):
        return (
            "a force push is forbidden, to any branch: update a branch that is behind `main` "
            "with `git merge origin/main` and a normal push (ADR 0057)"
        )
    destinations = [r.rsplit(":", 1)[-1] for r in refspecs]
    pushes_here = (not refspecs and "--tags" not in options) or any(
        d in ("HEAD", "@") for d in destinations
    )
    names = {d.removeprefix("refs/heads/").removeprefix("heads/") for d in destinations}
    if (
        any(_abbreviates(o, ALL_BRANCHES) for o in options)
        or PROTECTED_BRANCH in names
        or any("*" in d for d in destinations)  # a glob refspec pushes every branch it matches
        or (pushes_here and branch_here == PROTECTED_BRANCH)
    ):
        return (
            f"pushing to `{PROTECTED_BRANCH}` is forbidden: it changes only through a "
            "squash-merged PR (`/ship`)"
        )
    return None


def _graphql_queries(args: list[str]) -> list[str]:
    """The values of `gh api graphql`'s `query` fields, as `shlex` read them (`-f query=…`,
    attached `-fquery=…`, `--field=query=…`)."""
    queries: list[str] = []
    for i, arg in enumerate(args):
        field = None
        if arg in GRAPHQL_FIELD_OPTIONS and i + 1 < len(args):
            field = args[i + 1]
        elif arg.startswith(("--raw-field=", "--field=")):
            field = arg.partition("=")[2]
        elif arg[:2] in ("-f", "-F") and len(arg) > 2 and not arg.startswith("--"):
            field = arg[2:]
        if field is not None and field.partition("=")[0] == "query":
            queries.append(field.partition("=")[2])
    return queries


def _abbreviates(option: str, longs: tuple[str, ...]) -> bool:
    """Whether `option` is one of `longs` or, as git reads long options, a prefix of one
    (`--force-w` is `--force-with-lease`), with or without `=value`."""
    name = option.partition("=")[0]
    return name.startswith("--") and len(name) > 3 and any(long.startswith(name) for long in longs)


def gh_reason(args: list[str]) -> str | None:
    """Why `gh args` is refused, or None."""
    args = _without_repo_flag(args)
    if args[:1] == ["alias"] and args[1:2] and args[1] in ("set", "import"):
        return "a `gh` alias can hide a ruleset write or an admin merge: setting one is forbidden"
    if args[:2] == ["pr", "merge"]:
        for arg in args[2:]:
            value = arg.partition("=")[2] if arg.startswith("--admin=") else None
            if arg == "--admin" or (value is not None and value.lower() not in FALSE_VALUES):
                return "`gh pr merge --admin` is forbidden: merges go through the gates"
    if args[:1] == ["api"] and _is_graphql(_gh_api_endpoint(args[1:])):
        queries = _graphql_queries(args)
        unseen = any(a.startswith("--input") or "=@" in a for a in args)
        if unseen or any("ruleset" in q.lower() for q in queries):
            return (
                "writing to the rulesets is forbidden: never weaken them (CLAUDE.md); a GraphQL "
                "query is read only when written in the command, `-f query='…'`"
            )
    if args[:1] == ["api"] and _gh_api_writes(args[1:]):
        endpoint = _gh_api_endpoint(args[1:])
        if "rulesets" in endpoint:
            return "writing to the rulesets is forbidden: never weaken them (CLAUDE.md)"
    return None


def gh_api_expansion_reason(command: str) -> str | None:
    """Why a `gh api` call in the raw `command` is refused for what it expands: a write (a method
    or a body) or a GraphQL call holding a `$` or a backquote anywhere but in a plain field
    (`PLAIN_FIELD`). Read from the raw text, quotes kept, since `shlex` would split a `$(…)` and
    merge nested quotes. A read from a variable endpoint (`gh api "repos/$R/pulls"`) passes."""
    for words in _raw_segments(command):
        for i in range(len(words) - 1):
            bare = [_unquoted(w) for w in words]
            if not (_named(bare[i], "gh") and "api" in bare[i + 1 : i + 4]):
                continue
            rest = words[bare.index("api", i + 1) + 1 :]
            writes = _gh_api_writes([w.strip("'\"") for w in rest])
            graphql = any("graphql" in w.lower() for w in rest)
            hidden = [w for w in rest if _expands(w) and not _plain_field(w)]
            if (writes or graphql) and hidden:
                return (
                    "writing to the rulesets is forbidden, and a `gh api` write or GraphQL call "
                    f"with an expansion outside a plain field (`{hidden[0][:40]}`) may be one: "
                    "write the endpoint and the query in the command (ADR 0057)"
                )
    return None


def _unquoted(word: str) -> str:
    """A raw word as the command sees it, quotes and backslashes removed (`'api'`, `a\\pi`,
    `$'api'`)."""
    return re.sub(r"[\"'\\]", "", _ansi_c_decoded(word))


ANSI_C = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.S)
LOCALE_QUOTE = re.compile(r'\$"')


def _ansi_c_decoded(text: str) -> str:
    """`text` with bash's `$'…'` strings decoded to the word bash passes (`$'\\x67\\x68'` is
    `gh`, and a NUL ends it: `$'gh\\0'` is `gh`), and the `$` of a `$"…"` dropped, so that a
    quote-blind reading sees the word bash runs."""
    return LOCALE_QUOTE.sub('"', ANSI_C.sub(lambda m: "'" + _bash_escapes(m.group(1)) + "'", text))


SIMPLE_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}
HEX_ESCAPE = {"x": (2, 16), "u": (4, 16), "U": (8, 16)}


def _bash_escapes(body: str) -> str:
    """A `$'…'` body as the shells decode it: the simple escapes, octal `\\nnn`, `\\xHH`,
    `\\uHHHH` and `\\UHHHHHHHH`, cut at the first NUL (a word can't hold one). Where bash and zsh
    (the Bash tool's shell here) may differ, the word is cut, which can only refuse more: a `\\x`,
    `\\u` or `\\U` with no digit, and a control escape (`\\cX`, `\\C-X`). An unknown escape keeps
    its backslash (bash), which the quote-blind reading then drops (zsh)."""
    out: list[str] = []
    i = 0
    while i < len(body):
        char = body[i]
        if char != "\\" or i + 1 == len(body):
            out.append(char)
            i += 1
            continue
        code = body[i + 1]
        i += 2
        if code in SIMPLE_ESCAPES:
            out.append(SIMPLE_ESCAPES[code])
            continue
        if code in "01234567":
            digits = re.match(r"[0-7]{0,2}", body[i:])
            assert digits is not None  # `{0,2}` always matches
            value, i = int(code + digits.group(0), 8) & 0xFF, i + len(digits.group(0))
        elif code in HEX_ESCAPE:
            width, base = HEX_ESCAPE[code]
            digits = re.match(rf"[0-9A-Fa-f]{{0,{width}}}", body[i:])
            assert digits is not None
            if not digits.group(0):  # no digit: bash keeps `\\x`, zsh ends the word there
                break
            value, i = int(digits.group(0), base), i + len(digits.group(0))
        elif code in "cC":  # bash's `\\cX`, zsh's `\\C-X`: either can be a NUL that ends it
            break
        else:
            out.append("\\" + code)
            continue
        if value == 0:
            break
        out.append(chr(value) if value <= 0x10FFFF else "\ufffd")
    return "".join(out)


def _expands(word: str) -> bool:
    """Whether a raw word holds a `$` or a backquote outside single quotes."""
    quote = ""
    for char in word:
        if quote == "'":
            quote = "" if char == "'" else quote
        elif char == "'" and not quote:
            quote = "'"
        elif char in "$`":
            return True
    return False


def _plain_field(word: str) -> bool:
    match = PLAIN_FIELD.fullmatch(word)
    return match is not None and match.group(1) != "query"


def _gh_api_writes(args: list[str]) -> bool:
    """Whether `gh api args` writes: `-X`/`--method` POST, PUT, PATCH or DELETE, or a body (`-f`,
    `-F`, `--input`…), which `gh` sends as a POST without a method."""
    method = ""
    for i, arg in enumerate(args):
        if arg in ("-X", "--method") and i + 1 < len(args):
            method = args[i + 1]
        elif arg.startswith(("-X", "--method=")):
            method = arg.removeprefix("--method=").removeprefix("-X")
    has_body = any(a == o or a.startswith(o) for a in args for o in BODY_OPTIONS)
    return method.upper() in WRITE_METHODS or (has_body and method.upper() in ("", "POST"))


FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _raw_segments(command: str) -> list[list[str]]:
    """`command`'s commands as their raw words, quotes kept: split at unquoted whitespace into
    words, and at unquoted operators and newlines into commands. A `$(…)` or a backquote stays
    inside the word that holds it, however it is quoted inside, so `gh api $(echo x) -f …` stays
    one call."""
    segments: list[list[str]] = [[]]
    current: list[str] = []
    for char, top_level in _scan(command):
        if top_level and (char.isspace() or char in ";&|()"):
            if current:
                segments[-1].append("".join(current))
                current = []
            if char in ";&|()\n":
                segments.append([])
        else:
            current.append(char)
    if current:
        segments[-1].append("".join(current))
    return [s for s in segments if s]


def _scan(command: str) -> Iterator[tuple[str, bool]]:
    """Each character of `command`, and whether it stands at the top level: outside quotes, an
    escape, a `$(…)` and a backquote, where whitespace and operators separate."""
    quote, escaped, depth, backquoted, opening = "", False, 0, False, False
    for i, char in enumerate(command):
        top_level = False
        if opening:  # the `(` of the `$(` that opened it
            opening = False
        elif escaped:
            escaped = False
        elif depth:
            depth += {"(": 1, ")": -1}.get(char, 0)
        elif backquoted:
            backquoted = char != "`"
        elif quote == "'":
            quote = "" if char == "'" else quote
        elif char == "\\":
            escaped = True
        elif command.startswith("$(", i):
            depth, opening = 1, True
        elif char == "`":
            backquoted = True
        elif char in "'\"" and (not quote or quote == char):
            quote = "" if quote else char
        else:
            top_level = not quote
        yield char, top_level


def _without_repo_flag(args: list[str]) -> list[str]:
    """`gh`'s arguments without `-R`/`--repo OWNER/REPO`, which can stand before the subcommand's
    own (`gh pr -R o/r merge`)."""
    out: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in ("-R", "--repo"):
            skip = True
        elif not arg.startswith(("--repo=", "-R=")):
            out.append(arg)
    return out


GH_API_OPTIONS_WITH_VALUE = frozenset(
    {
        "-X",
        "--method",
        "-f",
        "--raw-field",
        "-F",
        "--field",
        "--input",
        "-H",
        "--header",
        "-q",
        "--jq",
        "-t",
        "--template",
        "--cache",
        "-p",
        "--preview",
        "--hostname",
    }
)


def _is_graphql(endpoint: str) -> bool:
    """Whether `gh api`'s endpoint is GraphQL: `graphql`, `/graphql` or the full URL."""
    path = re.sub(r"^[a-z]+://[^/]+", "", endpoint, flags=re.I).strip("/").partition("?")[0]
    return path.lower().endswith("graphql")


def _gh_api_endpoint(args: list[str]) -> str:
    """`gh api`'s endpoint: its first positional (a body or a header is an option's value)."""
    _, positionals = _positionals(args, GH_API_OPTIONS_WITH_VALUE)
    return positionals[0] if positionals else ""


def current_branch(cwd: Path | None) -> str | None:
    """The branch checked out in `cwd`, or None (detached, not a repo, no cwd)."""
    if cwd is None or not cwd.is_dir():
        return None
    try:
        found = subprocess.run(
            ["git", "-C", str(cwd), "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return found.stdout.strip() or None


def reason_to_block(command: str, cwd: Path | None) -> str | None:
    """Why `command`, run from `cwd`, is refused, or None to let it run."""
    expansion = gh_api_expansion_reason(command)
    if expansion is not None:
        return expansion + (HEREDOC_HINT if "<<" in command or "`" in command else "")
    blind = _blind_reason(command, cwd)
    if blind is not None:
        return blind + HEREDOC_HINT
    for words in views(command):
        reason = _environment_reason(words)
        run = program(words)
        if reason is None and run and run[0] == "cd" and len(run) > 1 and cwd is not None:
            cwd = cwd / Path(run[1]).expanduser()
            continue
        if reason is None and run and _named(run[0], "gh"):
            reason = gh_reason(run[1:])
        elif reason is None and run and _named(run[0], "git"):
            reason = _git_reason(run[1:], cwd)
        if reason is not None:
            return reason + (HEREDOC_HINT if "<<" in command or "`" in command else "")
    return None


# What the quote-blind reading refuses: the rules GitHub can't back (a ruleset write, a gh alias)
# or doesn't yet everywhere (a force push), never the early messages for `--admin` and `main`.
CRITICAL = ("rulesets is forbidden", "alias", "force push", "can force", "exec path", "set -a")


def _blind_reason(command: str, cwd: Path | None) -> str | None:
    """The critical refusals on a reading that ignores quoting altogether: every quote and
    backslash dropped, every operator, newline and backquote a command boundary. No quoting the
    other readings misread (a comment holding a quote, `$'…'`, nested quotes) can then hide a
    command from it. A certainly quoted heredoc's body is still text (`_expanded_text`)."""
    text = _ansi_c_decoded(_expanded_text(CONTINUATION.sub(r"\1", command)))
    text = re.sub(r"[\"'\\]", "", text)
    words = re.findall(r"\|\||&&|[;|&(){}`\n]|[^\s;|&(){}`]+", text)
    segments: list[list[str]] = [[]]
    for word in words:
        if word in SEPARATORS or word in ("`", "\n"):
            segments.append([])
        else:
            segments[-1].append(word)
    for segment in segments:
        run = program(segment)
        reason = _environment_reason(segment)
        if reason is None and run and _named(run[0], "gh"):
            reason = gh_reason(run[1:])
        elif reason is None and run and _named(run[0], "git"):
            reason = _git_reason(run[1:], cwd)
        if reason is not None and any(c in reason for c in CRITICAL):
            return reason
    return None


def _environment_reason(words: list[str]) -> str | None:
    """Why the environment a command sets is refused: a `GIT_CONFIG*` assignment or export
    wherever it stands (`set -a` exports a bare assignment to the next `git`), or another git exec
    path, which can swap `git push` for something else."""
    if any(CONFIG_ENV.match(w) and ASSIGNMENT.match(w) for w in words) or (
        words[:1] in (["export"], ["declare"], ["typeset"])
        and any(CONFIG_ENV.match(w) for w in words)
    ):
        return "git config passed through the environment (`GIT_CONFIG_*`) is forbidden"
    run = program(words)
    if run[:1] == ["set"] and any(
        w == "allexport" or (w.startswith("-") and not w.startswith("--") and "a" in w)
        for w in run[1:]
    ):
        return "`set -a` exports every later assignment, `GIT_CONFIG_*` included: it is forbidden"
    if any(w.startswith(("GIT_EXEC_PATH=", "--exec-path=")) for w in words):
        return "another git exec path can swap `git push` for something else: it is forbidden"
    return None


def _git_reason(args: list[str], cwd: Path | None) -> str | None:
    i = 0
    forcing_config = False
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-C" and i + 1 < len(args) and cwd is not None:
            cwd = cwd / Path(args[i + 1]).expanduser()
        if args[i] == "-c" and i + 1 < len(args) and FORCING_CONFIG.match(args[i + 1]):
            forcing_config = True
        if args[i].startswith("--config-env"):  # its value comes from a variable: unseen
            forcing_config = True
        i += 2 if args[i] in GIT_OPTIONS_WITH_VALUE else 1
    if forcing_config and i < len(args):
        return (
            "a `git -c` that sets a push refspec, a mirror or an alias can force a push: it is "
            "forbidden (ADR 0057)"
        )
    if args[i : i + 1] == ["config"] and any(FORCING_CONFIG.match(a) for a in args[i + 1 :]):
        return (
            "setting a push refspec, a mirror, an include or an alias with `git config` can force "
            "a later push: it is forbidden (ADR 0057)"
        )
    if args[i : i + 1] != ["push"]:
        return None
    return push_reason(args[i + 1 :], current_branch(cwd))


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
