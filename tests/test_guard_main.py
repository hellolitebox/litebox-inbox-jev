"""scripts/guard_main.py, the PreToolUse hook: blocks `--admin` merges, pushes to `main` and
ruleset writes, and nothing else. The current-branch cases run against real git repositories."""

import io
import json
import subprocess
import sys
from pathlib import Path

import guard_main
import pytest
from conftest import ROOT

GUARD = ROOT / "scripts" / "guard_main.py"
SETTINGS = ROOT / ".claude" / "settings.json"

BLOCKED = [
    "gh pr merge 12 --squash --delete-branch --admin",
    "gh pr merge --admin 12",
    "n=12; gh pr merge $n --squash --admin",
    "gh pr merge 12 --squash \\\n  --admin",
    "echo x; (gh pr merge 12 --admin)",
    "git push origin main",
    "git push -u origin main",
    "git push origin HEAD:main",
    "git push origin 'HEAD:main'",
    "git push --force origin +HEAD:main",
    "git push origin +main",
    "git push -f origin +refs/heads/main",
    "git push origin refs/heads/main",
    "git push origin :main",
    "git push --delete origin main",
    "git -C /tmp/x push origin main",
    "git --no-pager push origin main",
    "git push -o ci.skip origin main",
    "git push origin feat/x main",
    "git fetch origin && git push origin main",
    "cd /tmp && git push origin main",
    "sudo git push origin main",
    "sudo -n /usr/bin/git push origin main",
    "sudo -u root git push origin main",
    "sudo -n -u root -g staff env -u HOME git push origin main",
    "time -f %e git push origin main",
    "time -p git push origin main",
    "exec -a name git push origin main",
    "timeout 60 git push origin main",
    "timeout -s KILL -k 5 60 git push origin main",
    "timeout --signal=KILL 1m git push origin main",
    "nice git push origin main",
    "nice -n 5 git push origin main",
    "nice --adjustment 5 git push origin main",
    "nice --adjustment=5 git push origin main",
    "command git push origin main",
    "command -p git push origin main",
    "sudo -u root timeout 60 nice -n 5 git push origin main",
    "git push origin HEAD:heads/main",
    "git push origin +HEAD:heads/main",
    "git push origin @:main",
    "sleep 1 & git push origin main",
    "git fetch & gh pr merge 12 --admin",
    "git push origin main 2>&1",
    "git push origin main &> push.log",
    "git push origin main >&2",
    "git push --all origin",
    "git push --mirror origin",
    "git push --branches",
    "git push origin --tags main",
    "env GIT_TRACE=1 git push origin main",
    'GIT_SSH_COMMAND="ssh -i k" git push origin main',
    "exec git push origin main",
    "(git push origin main)",
    'echo "$(git push origin main)"',
    "gh api -X PUT repos/o/r/rulesets/1 --input r.json",
    "gh api repos/o/r/rulesets --method DELETE",
    "gh api --method=PATCH repos/o/r/rulesets/1",
    "gh api -XPUT repos/o/r/rulesets/1 --input r.json",
    "gh api repos/o/r/rulesets --input r.json",
    "gh api repos/o/r/rulesets -f name=x -F enforcement=active",
    "gh api --input r.json orgs/o/rulesets",
    # Operators inside quotes don't split the command (QA round 3 on #6).
    "gh pr merge 6 --subject 'Sync Q&A' --admin",
    "gh pr merge 6 --body 'R&D & ops' --admin",
    "gh pr merge 6 --squash --subject 'feat(x): y' --admin",
    "gh pr merge 6 --body 'a; b' --admin",
    'gh pr merge 6 --body "a | b && c" --admin',
    "gh api 'repos/o/r/rulesets/1?a=1&b=2' -X PUT",
    "gh api 'repos/o/r/rulesets?a=1&b=2' -f name=x",
    "git commit -m 'x' & git push origin main",
    # A substitution's commands are judged, and the command around it stays whole.
    'gh pr merge "$(cat pr.txt)" --admin',
    "echo `git push origin main`",
    'echo "`git push origin main`"',
    # A heredoc's body is split blind, and what follows it is read again.
    "cat <<EOF\nit's\nEOF\ngit push origin main",
    "git commit -F - <<'EOF' && git push origin main\ndon't\nEOF",
    "git commit -m \"$(cat <<'EOF'\nfix: don't\nEOF\n)\" && git push origin main",
    "bash <<EOF\ngit push origin main\nEOF",
    # A quote in a comment opens nothing.
    "true # it's fine\ngit push origin main",
    # A reserved word puts the next word in command position.
    "for b in a; do git push origin main; done",
    "if true; then gh pr merge 6 --admin; fi",
    "! git push origin main",
    "{ git push origin main; }",
    # A quote left open: bash won't run it, and the hook splits blind.
    "gh pr merge 12 --admin 'oops",
    "echo 'x; git push origin main",
    # An escaped character is never a comment start, an operator, a redirection or a quote, and a
    # backslash-newline is a line continuation unless the backslash is itself escaped (QA round 4).
    "gh pr merge 6 --subject Fix\\ #12 --admin",
    "echo a\\;# ; gh pr merge 1 --admin",
    "echo a\\>&gh pr merge 1 --admin",
    "echo x 2\\>&gh pr merge 1 --admin",
    "echo C:\\\\\ngit push origin main",
    "git push \\\norigin main",
    "gh pr merge 1 --ad\\\nmin",
    "git push origin ma\\\nin",
    "echo \\#x ; git push origin main",
    "echo \\' ; git push origin main",
    'echo \\" ; git push origin main',
    "echo a # b \\\ngit push origin main",
    "git push origin &\\\n>f main",
    "echo 'a\\' ; git push origin main",
    'echo "a\\\\" ; git push origin main',
    'echo "\\\\$(git push origin main)"',
    'gh pr merge 1 --subject "a\\\nb" --admin',
    "bash <<EOF\ngit push \\\norigin main\nEOF",
    "echo 'x\\\ngit push origin main",
    'echo "x; git push origin main',
    "bash << EOF\ngit push origin main\nEOF",
    "bash <<EOF\ngit push origin main \\",
    # A heredoc delimiter after `\<`: that is a redirection from a file, not a heredoc.
    "cat \\<<EOF\ngit push origin main",
    # `coproc` puts the next word in command position, its name before a compound command none.
    "coproc git push origin main",
    "coproc pusher { git push origin main; }",
    "coproc { gh pr merge 1 --admin; }",
    # After word text (an escape, a quoted string) a `#` is mid-word, no comment (QA round 5).
    "echo \\;#; gh pr merge 1 --admin",
    "echo ''#; gh pr merge 1 --admin",
    'echo "x"#; gh pr merge 1 --admin',
    # `$((…) )` is a substitution holding a subshell, not arithmetic.
    "echo $((git push origin main) )",
    "echo $( (git push origin main) )",
    # Arithmetic holding a substitution runs its command (QA round 1 on #8).
    "echo $(( $(git push origin main) + 1 ))",
    "echo $(( `git push origin main` + 1 ))",
    'echo "$(( $(git push origin main) ))"',
    # A redirection is no argument, wherever it stands, and a quoted `>` is no redirection.
    "2>/dev/null git push origin main",
    "git push origin main>log",
    "git push origin main 2>>log",
    "gh pr merge 1 --body '>' --admin",
    "gh pr merge 1 --body \\> --admin",
    "gh pr merge 1 >out --admin",
    # `--admin=<value>` turns it on unless the value is false (QA round 1 on inbox-jev#4).
    "gh pr merge 12 --admin=true",
    "gh pr merge 12 --admin=1",
    "gh pr merge 12 --admin=TRUE",
    "gh pr merge 12 --squash --admin=t",
]
ALLOWED = [
    "gh pr merge 12 --squash --delete-branch",
    "gh pr view 12 --json mergeStateStatus",
    "git push origin feat/x",
    "git push -u origin chore/claude-md-diet",
    "git push origin HEAD:feat/x",
    'git push origin HEAD:"$BR"',
    "git push origin feat/x:feat/x",
    "git switch main && git pull && git push origin feat/x",
    "git push origin main-fix",
    "git push origin feat/main-thing",
    "git push -o ci.skip origin feat/x",
    "sudo -u root git push origin feat/x",
    "git log main..HEAD",
    "command -v git",
    "command -V git && git status",
    "command -pv git",
    "timeout 60 git push origin feat/x",
    "nice -n 5 git push origin feat/x",
    "git push origin HEAD:heads/feat/x",
    "git push origin @:feat/x",
    "git push origin feat/x 2>&1 | tail -1",
    "git push origin feat/x &> push.log",
    "sleep 1 & git push origin feat/x",
    "gh api repos/o/r/rulesets",
    "gh api repos/o/r/rulesets/1 --jq .name",
    "gh api -X GET repos/o/r/rulesets -f per_page=5",
    "gh api -X POST repos/o/r/issues -f title=x",
    "gh api -X POST repos/o/r/issues/1/comments -f body='the rulesets stay as they are'",
    "gh api -f body='never weaken the rulesets' -X POST repos/o/r/issues/1/comments",
    "echo main && git status",
    "true # git push origin main",
    "# gh pr merge 12 --admin",
    'echo "git push origin main is forbidden"',
    "grep -rn 'git push origin main' docs/",
    "cat <<'EOF'\nNever run: git push origin main.\n"
    "- gh pr merge 12 --admin bypasses the gate\nEOF",
    "python3 - <<'EOF'\nx = {\"gh api -X PUT rulesets\": 1}\nprint(x)\nEOF",
    'git commit -m "$(cat <<EOF\nfix: stop a git push origin main\nEOF\n)"',
    "git commit -m 'A & git push origin main later'",
    "git commit -m 'fix A & B'",
    "git commit -m 'feat(x): y; z | w'",
    "curl 'https://x.test/?a=1&b=2'",
    "gh api 'repos/o/r/rulesets?per_page=5&page=2'",
    "echo \\; git push origin main",
    "echo $'it\\'s' && git status",
    # Escapes keep text inside its word or its quotes (QA round 4).
    "echo a\\ \\& git push origin main",
    "echo a\\|git push origin main",
    'echo "\\$(git push origin main)"',
    'echo "\\`git push origin main\\`"',
    'echo "a \\" ; git push origin main"',
    "git commit -m 'C:\\\\' && git push origin feat/x",
    "echo $'a\\' ; git push origin main'",
    "git commit -m 'x\\\n& git push origin main'",
    "git push \\\n  origin feat/x",
    "echo a \\\n# git push origin main",
    # A here-string is no heredoc: the lines after it are read as commands, quotes included.
    "grep x <<< \"$v\"\ngit commit -m 'A & git push origin main'",
    "coproc pusher { git push origin feat/x; }",
    "coproc git push origin feat/x",
    # Arithmetic runs nothing, and its `<<` opens no heredoc (QA round 5).
    "echo $((1<<2))\ngit commit -m 'A & git push origin main'",
    "echo \"$((1<<2))\"\ngit commit -m 'A & git push origin main'",
    "echo $(( (1<<2) + 1 ))\ngit commit -m 'A & git push origin main'",
    "echo $(echo $((1<<2)))\ngit commit -m 'A & git push origin main'",
    "gh pr merge 12 --admin=false --squash",
    "gh pr merge 12 --squash --admin=0",
    "git push origin feat/x>log 2>&1",
    "ls",
    "",
]
CURRENT_BRANCH_PUSHES = [
    "git push",
    "git push origin",
    "git push origin HEAD",
    "git push origin @",
    "git push -u origin HEAD",
    "timeout 60 git push -u origin HEAD",
    "command git push",
    # A destination made by a substitution is taken for the current branch (QA round 4).
    'git push origin "$(git branch --show-current)"',
    'git push -u origin "$(git rev-parse --abbrev-ref HEAD)"',
    "git push -u origin `git branch --show-current`",
    'git push origin HEAD:"$(git branch --show-current)"',
    "git push origin $(git branch --show-current):refs/heads/$(git branch --show-current)",
    "echo C:\\\\\ngit push",
    # A redirection is no refspec (QA round 1 on #8).
    "git push origin 2>&1",
    "git push 2>/dev/null",
    "git push origin >push.log 2>&1",
    "git push origin &>push.log",
]


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def repo(path: Path, branch: str) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", branch)
    return path


def commit(path: Path, *files: str) -> None:
    for name in files:
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(name)
    git(path, "add", "-A")
    git(
        path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@x.io",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )


def repo_with_branches(path: Path, branch: str, *others: str) -> Path:
    """A repository on `branch`, with one commit and the local branches `others`."""
    repo(path, branch)
    commit(path, "src/a.py")
    for other in others:
        git(path, "branch", other)
    return path


@pytest.mark.parametrize("command", BLOCKED)
def test_blocked(command):
    assert guard_main.reason_to_block(command, None) is not None


@pytest.mark.parametrize("command", ALLOWED)
def test_allowed(command):
    assert guard_main.reason_to_block(command, None) is None


@pytest.mark.parametrize("command", CURRENT_BRANCH_PUSHES)
def test_push_of_the_current_branch_is_blocked_on_main(command, tmp_path):
    assert guard_main.reason_to_block(command, repo(tmp_path / "r", "main")) is not None


@pytest.mark.parametrize("command", CURRENT_BRANCH_PUSHES)
def test_push_of_the_current_branch_passes_on_a_feature_branch(command, tmp_path):
    assert guard_main.reason_to_block(command, repo(tmp_path / "r", "feat/x")) is None


@pytest.mark.parametrize("command", ["git push --tags", "git push origin --tags"])
def test_tags_without_a_refspec_push_no_branch_even_on_main(command, tmp_path):
    on_main = repo(tmp_path / "r", "main")
    assert guard_main.reason_to_block(command, on_main) is None
    assert guard_main.reason_to_block("git push origin --tags HEAD", on_main) is not None


def test_cd_in_the_command_moves_the_branch_lookup(tmp_path):
    on_main = repo(tmp_path / "a", "main")
    on_feature = repo(tmp_path / "b", "feat/x")
    assert guard_main.reason_to_block(f"cd {on_feature} && git push", on_main) is None
    assert guard_main.reason_to_block(f"cd {on_main} && git push", on_feature) is not None
    assert guard_main.reason_to_block("cd b && git push", tmp_path) is None
    assert guard_main.reason_to_block("cd a && git push", tmp_path) is not None


def test_cd_in_a_subshell_moves_the_branch_lookup(tmp_path):
    on_main = repo(tmp_path / "a", "main")
    on_feature = repo(tmp_path / "b", "feat/x")
    assert guard_main.reason_to_block(f"(cd {on_feature} && git push)", on_main) is None
    assert guard_main.reason_to_block(f"(cd {on_main} && git push)", on_feature) is not None


def test_git_dash_c_moves_the_branch_lookup_for_that_push_only(tmp_path):
    on_main = repo(tmp_path / "a", "main")
    on_feature = repo(tmp_path / "b", "feat/x")
    assert guard_main.reason_to_block(f"git -C {on_feature} push", on_main) is None
    assert guard_main.reason_to_block(f"git -C {on_main} push", on_feature) is not None
    assert guard_main.reason_to_block("git -C b push", tmp_path) is None
    assert guard_main.reason_to_block("git -C a push", tmp_path) is not None
    assert guard_main.reason_to_block("git -C a -C ../b push", tmp_path) is None
    assert guard_main.reason_to_block(f"git -C {on_main} fetch && git push", on_feature) is None


@pytest.mark.parametrize(
    "command",
    [
        "git push origin HEAD:backup/$(date +%F)",
        'git push origin "feat/$(date +%s)"',
        "git push origin HEAD:refs/heads/backup/$(date +%F)",
    ],
)
def test_destination_only_containing_a_substitution_is_its_own_branch(command, tmp_path):
    assert guard_main.reason_to_block(command, repo(tmp_path / "r", "main")) is None


def test_substitution_refspec_on_a_feature_branch_passes(tmp_path):
    on_feature = repo(tmp_path / "r", "feat/x")
    command = 'git push -u origin "$(git branch --show-current)"'
    assert guard_main.reason_to_block(command, on_feature) is None
    assert guard_main.reason_to_block(command, None) is None  # no branch known


@pytest.mark.parametrize(
    "command",
    [
        "git checkout main; git push",
        "git switch main && git pull && git push",
        "git checkout -q main && git push -u origin HEAD",
        "git switch -c main && git push",
        "git checkout -B main origin/main && git push",
        "git switch --create=main && git push",
        "git switch -cmain && git push",
        "git -C . switch main && git push",
        # `main` counts even where it isn't a local branch yet (git creates it from origin/main).
        "git checkout main && git push",
        "git checkout --track origin/main && git push",
        "git switch -t origin/main && git push",
        # A redirection hides no switch (QA round 1 on #8).
        "git checkout main 2>&1 && git push",
        "git checkout main >/dev/null 2>&1; git push",
        "git switch main 2>/dev/null; git push",
    ],
)
def test_a_switch_to_main_earlier_in_the_command_is_seen_by_the_push(command, tmp_path):
    assert guard_main.reason_to_block(command, repo(tmp_path / "r", "feat/x")) is not None


@pytest.mark.parametrize(
    "command",
    [
        "git switch -c fix/x && git push -u origin HEAD",
        "git checkout -b fix/x origin/main && git push",
        "git switch feat/y && git push",
        "git checkout feat/y && git push origin HEAD",
        "git checkout feat/y 2>&1 && git push origin HEAD",
        "git switch --orphan=gh-pages && git push",
        "git checkout --track origin/feat/y && git push",
        "git checkout -b fix/x &&\n  git push -u origin HEAD",
        'git switch -c fix/x && git commit -m "$(cat msg)" && git push -u origin HEAD',
        "git switch -c fix/x; git switch -c fix/y && git push",  # pushed only if fix/y is
        # A branch the command doesn't name is unknown: only an explicit `main` is refused.
        "git switch - && git push",
        "git checkout - && git push",
        "git switch --detach v1 && git push",
    ],
)
def test_a_switch_away_from_main_earlier_in_the_command_is_seen_by_the_push(command, tmp_path):
    on_main = repo_with_branches(tmp_path / "r", "main", "feat/y")
    assert guard_main.reason_to_block(command, on_main) is None


@pytest.mark.parametrize(
    "command",
    [
        "git checkout -b fix/x; git push -u origin HEAD",
        "git switch -c fix/x\ngit push -u origin HEAD",
        "git switch -c fix/x || git push -u origin HEAD",
        "git switch -c fix/x | cat; git push -u origin HEAD",
        "(git switch -c fix/x); git push -u origin HEAD",
        "git switch -c a && git switch -c b; git push",
        "git switch -c fix/x; git switch -c fix/y; git push",
    ],
)
def test_a_switch_that_may_have_failed_leaves_the_old_branch_possible(command, tmp_path):
    """After `;`, a newline, a pipe or `||` the push runs even if the switch failed (`fix/x`
    taken), so the branch before the switch is judged too (QA round 1 on #8)."""
    assert guard_main.reason_to_block(command, repo(tmp_path / "r", "main")) is not None


@pytest.mark.parametrize(
    "command",
    [
        "git checkout README.md && git push",
        "git checkout -- feat/y && git push",
        "git checkout feat/y -- a.txt && git push",
        "git checkout feat/y a.txt && git push",
        # Forms the hook can't read as a branch keep the current one (QA round 1 on #8).
        "git checkout && git push",
        "git checkout -p && git push",
        "git checkout --patch feat/y && git push",
        "git checkout src/b.py && git push",  # deleted from the worktree
        "git checkout '*.py' && git push",
        "git checkout ':(top)src' && git push",
        "git checkout remote-only && git push",  # no local branch of that name
        "git switch && git push",
        "git switch feat/y extra && git push",
    ],
)
def test_a_checkout_of_files_switches_no_branch(command, tmp_path):
    on_main = repo_with_branches(tmp_path / "r", "main", "feat/y")
    commit(on_main, "README.md", "src/b.py")
    (on_main / "src" / "b.py").unlink()
    assert guard_main.reason_to_block(command, on_main) is not None


def test_a_switch_moves_the_branch_of_its_own_directory_only(tmp_path):
    on_main = repo(tmp_path / "a", "main")
    on_feature = repo(tmp_path / "b", "feat/x")

    def blocked(command: str, cwd: Path) -> bool:
        return guard_main.reason_to_block(command, cwd) is not None

    assert not blocked(f"git -C {on_main} switch feat/y && git push", on_main)
    assert blocked(f"git -C {on_feature} switch feat/y && git push", on_main)
    assert blocked(f"cd {on_feature} && git switch main && git push", on_main)
    assert not blocked(f"git switch main; cd {on_feature} && git push", on_main)
    assert not blocked("cd a && git switch feat/y && cd ../a && git push", tmp_path)


def test_a_switch_moves_the_branch_of_its_whole_worktree(tmp_path):
    """A switch in a subdirectory moves the branch of the repository (QA round 1 on #8)."""
    on_feature = repo_with_branches(tmp_path / "r", "feat/x", "main")
    command = "cd src && git checkout main && cd .. && git push"
    assert guard_main.reason_to_block(command, on_feature) is not None
    assert guard_main.reason_to_block("git -C src switch main && git push", on_feature) is not None
    assert guard_main.reason_to_block("cd src && git push", on_feature) is None


@pytest.mark.parametrize(
    ("args", "branch"),
    [
        (["switch", "feat/x"], guard_main.BranchSwitch("feat/x")),
        (["switch", "-c", "x", "origin/main"], guard_main.BranchSwitch("x")),
        (["switch", "-c"], guard_main.BranchSwitch(None)),
        (["switch"], None),
        (["switch", "-"], guard_main.BranchSwitch(None)),
        (["checkout", "-"], guard_main.BranchSwitch(None)),
        (["checkout"], None),
        (["checkout", "main"], guard_main.BranchSwitch("main")),
        (["checkout", "feat/x"], None),  # not known as a branch without a repository
        (["checkout", "-t", "origin/feat/x"], guard_main.BranchSwitch("feat/x")),
        (["switch", "--track=direct", "origin/feat/x"], guard_main.BranchSwitch("feat/x")),
        (["checkout", "--pathspec-from-file=f"], None),
        (["checkout", "-bx"], guard_main.BranchSwitch("x")),
        (["checkout", "--orphan=x"], guard_main.BranchSwitch("x")),
        (["checkout", "-d", "v1"], guard_main.BranchSwitch(None)),
        (["checkout", "--", "a"], None),
        (["status"], None),
        (["push", "origin", "main"], None),
    ],
)
def test_branch_switch(args, branch):
    git = guard_main.git_subcommand(args)
    assert git is not None
    assert guard_main.branch_switch(git, None) == branch


def test_unknown_directory_names_no_branch(tmp_path):
    assert guard_main.current_branch(None) is None
    assert guard_main.current_branch(tmp_path) is None  # not a repository
    assert guard_main.reason_to_block("git push", tmp_path) is None
    on_main = repo(tmp_path / "r", "main")
    assert guard_main.reason_to_block('cd "$WT" && git push', on_main) is None
    assert guard_main.reason_to_block('git -C "$WT" push', on_main) is None
    assert guard_main.reason_to_block("cd missing && git push", on_main) is None


def test_detached_head_names_no_branch(tmp_path):
    path = repo(tmp_path / "r", "main")
    git(
        path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@x.io",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    git(path, "checkout", "-q", "--detach")
    assert guard_main.current_branch(path) is None


def test_command_words_skip_assignments_and_wrappers():
    assert guard_main.command_words("GIT_TRACE=1 sudo -n env -i git push") == ["git", "push"]
    assert guard_main.command_words('X="a b" git status') == ["git", "status"]
    assert guard_main.command_words("sudo -u root git push") == ["git", "push"]
    assert guard_main.command_words("time -p git push") == ["git", "push"]
    assert guard_main.command_words("timeout -k 5 60 git push") == ["git", "push"]
    assert guard_main.command_words("timeout 60 nice -n 5 git push") == ["git", "push"]
    assert guard_main.command_words("command -p git push") == ["git", "push"]
    assert guard_main.command_words("command -v git") == []
    assert guard_main.command_words("command -V git") == []
    assert guard_main.command_words("- git push origin main") == [
        "-",
        "git",
        "push",
        "origin",
        "main",
    ]
    assert guard_main.command_words("echo 'a b") == ["echo", "a", "b"]  # a quote left open
    assert guard_main.command_words('"') == []
    assert guard_main.command_words("X=1") == []
    assert guard_main.command_words("  ") == []


@pytest.mark.parametrize(
    ("command", "segments"),
    [
        ("a && b", ["a ", " b"]),
        ("a || b; c | d |& e", ["a ", " b", " c ", " d ", " e"]),
        ("a & b", ["a ", " b"]),
        ("a 2>&1", ["a 2>&1"]),
        ("a &> f", ["a &> f"]),
        ("a >&2", ["a >&2"]),
        ("a <&3", ["a <&3"]),
        ("a\nb", ["a", "b"]),
        ("(a) && b", ["a", " ", " b"]),
        ("a 'b & c; d' \"e | (f)\"", ["a 'b & c; d' \"e | (f)\""]),
        ("a \\& b", ["a \\& b"]),
        ("a $'b\\' & c'", ["a $'b\\' & c'"]),
        ('a "$(b; c)" d', ["b", " c", 'a "$(…)" d']),
        ("a `b` c", ["b", "a $(…) c"]),
        ("a $(b (c) d) e", ["b ", "c", " d", "a $(…) e"]),
        ("a # b'c & d\ne", ["a ", "e"]),
        ("a#b & c", ["a#b ", " c"]),
        ("a <<<'b & c'", ["a <<<'b & c'"]),
        ("a <<-'E'\n\tb & c\n\tE\nd", ["a <<-'E'", "\tb ", " c", "d"]),
        ("a <<E\nb\n", ["a <<E", "b"]),  # a heredoc left open runs to the end
        ("a 'b & c", ["a 'b ", " c"]),  # a quote left open: split blind
        ("a $(b", ["a $", "b"]),
        ("a \\\n&& b", ["a ", " b"]),
        ("a $((1<<2)) b\nc", ["a $((1<<2)) b", "c"]),  # arithmetic is one word
        ("a $((b) ) c", ["b", " ", "a $(…) c"]),  # `) )`: a substitution and a subshell
        ("a $((1", ["a $", "", "1"]),  # left open: blind
        ("a $(( $(b) + 1 )) c", ["b", " $(…) + 1 ", "a $(…) c"]),  # a command in arithmetic
        ("a $(( `b` )) c", ["b", " $(…) ", "a $(…) c"]),
    ],
)
def test_segments_split_outside_quotes_only(command, segments):
    assert [segment.text for segment in guard_main.segments(command)] == segments


@pytest.mark.parametrize(
    ("command", "segments"),
    [
        ("a\\ #b", ["a\\ #b"]),  # an escaped space doesn't start a word
        ("a \\#b", ["a \\#b"]),  # nor is an escaped `#` a comment
        ("a\\>&b", ["a\\>", "b"]),  # an escaped `>` is no redirection
        ("a\\;b", ["a\\;b"]),
        ("a \\\nb", ["a b"]),  # a continuation is deleted
        ("a\\\n#b", ["a#b"]),  # and the word goes on through it
        ("a \\\n#b\nc", ["a ", "c"]),  # after whitespace, a comment
        ("a \\\\\nb", ["a \\\\", "b"]),  # an escaped backslash, then a newline
        ("a &\\\n> f", ["a &> f"]),  # a redirection through a continuation
        ("a >\\\n&2", ["a >&2"]),
        ('a "b\\\nc" d', ['a "bc" d']),  # in double quotes too
        ("a 'b\\\nc' d", ["a 'b\\\nc' d"]),  # but not in single quotes
        ("a $'b\\\nc' d", ["a $'b\\\nc' d"]),
        ('a "\\$(b)" c', ['a "\\$(b)" c']),  # an escaped `$` opens no substitution
        ('a "\\\\$(b)" c', ["b", 'a "\\\\$(…)" c']),  # an escaped backslash does
        ("a # b \\\nc", ["a ", "c"]),  # a comment ends at the newline, continuation or not
        ("$(# c\nb) d", ["b", "$(…) d"]),  # a substitution starts a command
        ("a <<E\nb \\\nc\nE", ["a <<E", "b c"]),  # a continuation in a heredoc's body
        ("a 'b\\\nc", ["a 'b\\", "c", "a 'bc"]),  # blind: as written and joined
    ],
)
def test_segments_read_backslashes_as_bash_does(command, segments):
    assert [segment.text for segment in guard_main.segments(command)] == segments


def test_segments_mark_the_lines_of_a_heredocs_body():
    assert guard_main.segments("cat <<E && b\nc ; d\nE\ne") == [
        guard_main.Segment("cat <<E "),
        guard_main.Segment(" b", ends_chain=True),
        guard_main.Segment("c ", heredoc=True, ends_chain=True),
        guard_main.Segment(" d", heredoc=True, ends_chain=True),
        guard_main.Segment("e"),
    ]


@pytest.mark.parametrize(
    ("command", "ends"),
    [
        ("a && b", [False, False]),
        ("a; b", [True, False]),
        ("a || b | c |& d & e", [True, True, True, True, False]),
        ("a\nb", [True, False]),
        ("a &&\n b", [False, False]),  # the list goes on past the newline
        ("a && \\\nb", [False, False]),
        ("(a); b", [True, False]),
        ("(a) && b", [False, False, False]),
        ('a "$(b)" && c', [False, False, False]),  # a substitution ends no chain
        ("a 'b; c", [True, True]),  # blind
    ],
)
def test_segments_say_whether_what_follows_runs_whatever_their_status(command, ends):
    assert [segment.ends_chain for segment in guard_main.segments(command)] == ends


def test_a_refusal_in_a_heredoc_suggests_a_body_file():
    body = (
        "gh pr create --title t --body \"$(cat <<'EOF'\nRollback:\ngit push origin main\nEOF\n)\""
    )
    reason = guard_main.reason_to_block(body, None)
    assert reason is not None
    assert "--body-file" in reason
    assert "-F" in reason
    plain = guard_main.reason_to_block("git push origin main", None)
    assert plain is not None
    assert "--body-file" not in plain


@pytest.mark.parametrize(
    ("segment", "words"),
    [
        ("git push origin 2>&1", ["git", "push", "origin"]),
        ("git push origin > log 2> err", ["git", "push", "origin"]),
        ("2>/dev/null git push", ["git", "push"]),
        ("git push origin main>log", ["git", "push", "origin", "main"]),
        ("a&>log b", ["a", "b"]),
        ("a {fd}>f 3<&- 2>>e <<<'x y' <<-EOF b", ["a", "b"]),
        ("echo '>' \\> \">\" b", ["echo", ">", ">", ">", "b"]),
        ("echo 'a b' c\\ d", ["echo", "a b", "c d"]),
        ("echo 'a b", ["echo", "a", "b"]),  # a quote left open: on whitespace
        ("echo 'a > b c", ["echo", "a", "c"]),
        ("echo a\\", ["echo", "a\\"]),  # a lone backslash at the end
    ],
)
def test_command_words_drop_redirections(segment, words):
    assert guard_main.command_words(segment) == words


def test_coproc_name_before_a_compound_command_is_no_command():
    assert guard_main.command_words("coproc NAME { git push") == ["git", "push"]
    assert guard_main.command_words("coproc git push") == ["git", "push"]
    assert guard_main.command_words("coproc NAME for x in y") == ["for", "x", "in", "y"]
    assert guard_main.command_words("coproc NAME") == ["NAME"]


def test_git_subcommand_skips_git_options_and_keeps_dash_c():
    assert guard_main.git_subcommand(
        ["-C", "/x", "-c", "a=b", "push", "origin"]
    ) == guard_main.GitCommand("push", ["origin"], ["/x"])
    assert guard_main.git_subcommand(["-C", "a", "-C", "b", "status"]) == guard_main.GitCommand(
        "status", [], ["a", "b"]
    )
    assert guard_main.git_subcommand(["--no-pager", "log"]) == guard_main.GitCommand("log", [], [])
    assert guard_main.git_subcommand(["-C"]) is None
    assert guard_main.git_subcommand([]) is None


@pytest.mark.parametrize(
    ("args", "method"),
    [
        ([], "GET"),
        (["-X", "PUT", "x"], "PUT"),
        (["-XPUT", "x"], "PUT"),
        (["-X=patch", "x"], "PATCH"),
        (["--method", "DELETE", "x"], "DELETE"),
        (["--method=POST", "x"], "POST"),
        (["x", "-f", "a=b"], "POST"),
        (["x", "-fa=b"], "POST"),
        (["x", "--field=a=b"], "POST"),
        (["x", "--input", "r.json"], "POST"),
        (["-X", "GET", "x", "-f", "a=b"], "GET"),
        (["x", "-X"], "GET"),
    ],
)
def test_gh_api_method(args, method):
    assert guard_main.gh_api_method(args) == method


def call(command: str, cwd: str = "/") -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd})


def test_main_blocks_with_exit_2_and_the_reason_on_stderr(capsys):
    assert guard_main.main(io.StringIO(call("git push origin main"))) == 2
    captured = capsys.readouterr()
    assert "guard_main.py" in captured.err
    assert "main" in captured.err
    assert captured.out == ""


def test_main_allows_with_exit_0_and_no_output(capsys):
    assert guard_main.main(io.StringIO(call("git push origin feat/x"))) == 0
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "stdin",
    [
        "not json",
        json.dumps({"tool_name": "Read", "tool_input": {"file_path": "main"}}),
        json.dumps({"tool_name": "Bash"}),
        json.dumps({"tool_name": "Bash", "tool_input": {"command": 5}}),
        json.dumps(["git push origin main"]),
    ],
)
def test_main_has_no_opinion_on_other_input(stdin, capsys):
    assert guard_main.main(io.StringIO(stdin)) == 0
    assert capsys.readouterr() == ("", "")


def test_script_runs_as_the_hook_does():
    result = subprocess.run(
        [sys.executable, "-I", str(GUARD)],
        input=call("gh pr merge 3 --admin"),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "--admin" in result.stderr


def test_settings_wire_the_hook_to_bash_calls():
    settings = json.loads(SETTINGS.read_text())
    bash_hooks = [
        hook
        for group in settings["hooks"]["PreToolUse"]
        if group["matcher"] == "Bash"
        for hook in group["hooks"]
    ]
    assert any(hook["type"] == "command" and GUARD.name in hook["command"] for hook in bash_hooks)
