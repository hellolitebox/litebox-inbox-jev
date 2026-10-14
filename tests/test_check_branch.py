"""scripts/check_branch.sh refuses a branch only when its runtime dependencies differ from the
deployment's (scripts/runtime_deps.py). Run against real git repositories, with a fake `docker`
first on PATH."""

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import runtime_deps
from conftest import ROOT

SCRIPTS = ROOT / "scripts"

PYPROJECT = """\
[project]
name = "inbox-jev"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["httpx>=0.27", "pyyaml>=6.0"]

[project.optional-dependencies]
dev = ["pytest>=8", "ruff==0.17.0"]

[tool.ruff]
line-length = 100
"""

FAKE_DOCKER = """#!/bin/sh
echo "$@" >> "$FAKE_HOME/docker.calls"
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


class Checkout:
    """A deployment checkout cloned from a local `origin`, with a fake `docker`."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        seed = tmp / "seed"
        (seed / "scripts").mkdir(parents=True)
        for name in ("check_branch.sh", "runtime_deps.py"):
            shutil.copy2(SCRIPTS / name, seed / "scripts")
        (seed / "pyproject.toml").write_text(PYPROJECT)
        (seed / "uv.lock").write_text("version = 1\n")
        git(seed, "init", "-q", "-b", "main")
        git(seed, "add", ".")
        git(seed, "commit", "-q", "-m", "deployed")
        self.origin = tmp / "origin.git"
        git(tmp, "clone", "-q", "--bare", str(seed), str(self.origin))
        self.work = seed
        git(seed, "remote", "add", "origin", str(self.origin))
        self.deploy = tmp / "opt"
        git(tmp, "clone", "-q", str(self.origin), str(self.deploy))
        self.bin = tmp / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(FAKE_DOCKER)
        (self.bin / "docker").chmod(0o755)
        (self.bin / "python3").symlink_to(sys.executable)

    def push_branch(self, name: str, files: dict[str, str]) -> None:
        """Push a branch off the deployed commit that rewrites `files` (path: content)."""
        git(self.work, "switch", "-q", "-c", name, "main")
        for path, content in files.items():
            (self.work / path).write_text(content)
        git(self.work, "commit", "-q", "-am", name)
        git(self.work, "push", "-q", "origin", name)
        git(self.work, "switch", "-q", "main")

    def check(self, branch: str) -> subprocess.CompletedProcess[str]:
        env = os.environ | {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_HOME": str(self.tmp),
            "TMPDIR": str(self.tmp),
        }
        return subprocess.run(
            [
                "bash",
                str(self.deploy / "scripts" / "check_branch.sh"),
                branch,
                "--account",
                "a@b.c",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def docker_calls(self) -> list[str]:
        calls = self.tmp / "docker.calls"
        return calls.read_text().splitlines() if calls.exists() else []


@pytest.fixture
def checkout(tmp_path):
    return Checkout(tmp_path)


@pytest.mark.parametrize(
    "pyproject",
    [
        PYPROJECT.replace("line-length = 100", "line-length = 120"),  # tool config
        PYPROJECT + "\n[tool.mypy]\nstrict = true\n",
    ],
    ids=["ruff-config", "mypy-config"],
)
def test_a_branch_that_changes_only_tool_config_is_checked(checkout, pyproject):
    checkout.push_branch("tooling", {"pyproject.toml": pyproject})

    result = checkout.check("tooling")

    assert result.returncode == 0, result.stderr
    [call] = checkout.docker_calls()
    assert "dry_classify.py" in call and "--account a@b.c" in call
    assert not (checkout.tmp / "inbox-jev-check").exists(), "the worktree is removed"


@pytest.mark.parametrize(
    ("files", "differs"),
    [
        ({"pyproject.toml": PYPROJECT.replace("httpx>=0.27", "httpx>=0.28")}, "dependencies"),
        ({"pyproject.toml": PYPROJECT.replace(">=3.11", ">=3.12")}, "requires-python"),
        (
            {"pyproject.toml": PYPROJECT.replace("[tool.ruff]", 'pg = ["psycopg"]\n\n[tool.ruff]')},
            "optional-dependencies",
        ),
        ({"uv.lock": "version = 1\n# a new package\n"}, None),
    ],
    ids=["dependencies", "requires-python", "non-dev-extra", "uv-lock"],
)
def test_a_branch_that_changes_runtime_dependencies_is_refused(checkout, files, differs):
    checkout.push_branch("deps", files)

    result = checkout.check("deps")

    assert result.returncode == 1
    assert "deps changes pyproject.toml or uv.lock: build a separate image instead" in result.stderr
    if differs:
        assert f"runtime dependencies differ: {differs}" in result.stderr
    assert checkout.docker_calls() == []


@pytest.mark.parametrize(
    "branch",
    [
        PYPROJECT.replace('version = "0.1.0"', 'version = "0.2.0"'),
        PYPROJECT.replace('"ruff==0.17.0"', '"ruff==0.18.0"'),
    ],
    ids=["version", "dev-extra"],
)
def test_version_and_dev_extra_are_not_runtime_dependencies(branch):
    # runtime_deps alone: a real branch that changes either also changes uv.lock (which records
    # the version and every dev specifier), so check_branch.sh still refuses it.
    assert runtime_deps.differences(tomllib.loads(PYPROJECT), tomllib.loads(branch)) == []


def test_runtime_spec_ignores_everything_but_what_the_image_installs():
    base = {"project": {"name": "x", "dependencies": ["a"], "requires-python": ">=3.11"}}
    other = {
        "project": base["project"] | {"version": "2", "optional-dependencies": {"dev": ["b"]}},
        "tool": {"ruff": {"line-length": 120}},
        "build-system": {"requires": ["hatchling"]},
    }
    assert runtime_deps.differences(base, other) == []


def test_differences_lists_each_changed_key():
    old = {"project": {"dependencies": ["a"], "requires-python": ">=3.11"}}
    new = {
        "project": {
            "dependencies": ["a", "b"],
            "requires-python": ">=3.12",
            "optional-dependencies": {"pg": ["psycopg"]},
        }
    }
    assert runtime_deps.differences(old, new) == [
        "requires-python",
        "dependencies",
        "optional-dependencies",
    ]


def test_main_exit_codes(tmp_path, capsys):
    same, changed, broken = tmp_path / "a.toml", tmp_path / "b.toml", tmp_path / "c.toml"
    same.write_text(PYPROJECT)
    changed.write_text(PYPROJECT.replace("pyyaml>=6.0", "pyyaml>=7.0"))
    broken.write_text("[project\n")

    assert runtime_deps.main([str(same), str(same)]) == 0
    assert runtime_deps.main([str(same), str(changed)]) == 1
    assert "differ: dependencies" in capsys.readouterr().err
    assert runtime_deps.main([str(same), str(broken)]) == 2
    assert runtime_deps.main([str(same), str(tmp_path / "missing.toml")]) == 2
    assert "can't read pyproject.toml" in capsys.readouterr().err
    assert runtime_deps.main([str(same)]) == 2
