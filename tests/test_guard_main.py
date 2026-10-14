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
    "git log main..HEAD",
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
    "ls",
    "",
]
CURRENT_BRANCH_PUSHES = [
    "git push",
    "git push origin",
    "git push origin HEAD",
    "git push -u origin HEAD",
]


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def repo(path: Path, branch: str) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", branch)
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
