"""The workflows' contracts. Which workflow and code the QA gate runs with its write token (ADR
0056): the repo is public, so the gate's `pull_request_target` run gets a write token on anyone's
PR, forks' included, and these tests pin the triggers, the permissions and the one checkout of
`main`'s script. And the release builds with the uv and Python CI tested
(`.claude/rules/pins.md`)."""

import re

from conftest import ROOT

WORKFLOWS = ROOT / ".github" / "workflows"
QA_GATE = WORKFLOWS / "qa-gate.yml"


def top_level_keys(block: str) -> list[str]:
    """The keys one level under a workflow's top-level `block:` (`on:`, `permissions:`)."""
    lines = QA_GATE.read_text().splitlines()
    start = lines.index(f"{block}:")
    keys = []
    for line in lines[start + 1 :]:
        if line and not line.startswith(" "):
            break
        if re.match(r"^  [a-z_-]+:", line):
            keys.append(line.split(":")[0].strip())
    return keys


def test_the_qa_gate_runs_only_mains_workflow():
    """The status must never be posted by a run of a PR's own copy of the workflow: a
    `pull_request` run executes the PR's qa-gate.yml with `statuses: write`, so a PR could edit
    the steps and post `qa-verdict: success` without the gate running (ADR 0056). Both triggers
    run the default branch's definition."""
    assert top_level_keys("on") == ["pull_request_target", "issue_comment"]


def test_the_qa_gate_token_is_minimal():
    """The write token every PR's run gets can post a commit status and nothing else."""
    workflow = QA_GATE.read_text()
    permissions = re.search(r"^permissions:\n((?:  .*\n)+)", workflow, re.MULTILINE)
    assert permissions is not None
    granted = dict(re.findall(r"^  ([a-z-]+): (\w+)", permissions.group(1), re.MULTILINE))
    assert granted == {
        "contents": "read",
        "pull-requests": "read",
        "issues": "read",
        "statuses": "write",
    }
    # A job's own `permissions:` would replace the block above for that job, unseen by it.
    code = "\n".join(line for line in workflow.splitlines() if not line.lstrip().startswith("#"))
    assert re.findall(r"^[ \t]*permissions:", code, re.MULTILINE) == ["permissions:"]


def test_the_qa_gate_runs_mains_script_and_never_the_prs_code():
    """Under `pull_request_target` the token can write, also on a fork's PR, so the PR's code is
    never checked out: the one checkout is the default branch's script, and the head the script
    judges comes from the API. Dependabot's runs are not skipped (their `pull_request_target`
    token can post)."""
    workflow = QA_GATE.read_text()
    checkouts = re.findall(r"uses: actions/checkout@.*\n((?:\s+.*\n)*?)\s+- run:", workflow)
    assert len(checkouts) == 1
    assert "ref: ${{ github.event.repository.default_branch }}" in checkouts[0]
    assert "sparse-checkout: scripts/qa_gate.py" in checkouts[0]
    assert "fetch-depth: 0" in checkouts[0]
    code = "\n".join(line.split("#")[0] for line in workflow.splitlines())
    assert "github.event.pull_request.head" not in code
    assert "github.head_ref" not in code
    assert "refs/pull/" not in code
    assert "dependabot" not in code


def workflow_env(name: str) -> dict[str, str]:
    """A workflow's top-level `env:` block, quoted string values only."""
    workflow = (WORKFLOWS / name).read_text()
    env = re.search(r"^env:\n((?:(?:  .*)?\n)+)", workflow, re.MULTILINE)
    assert env is not None, f"{name} has no top-level env block"
    return dict(re.findall(r'^  ([A-Z_]+): "([^"]*)"', env.group(1), re.MULTILINE))


def test_the_release_builds_with_the_uv_and_python_ci_tested():
    """`release.yml` builds the package that goes to PyPI with its own copies of `UV_VERSION` and
    `PYTHON_VERSION`. Dependabot bumps neither, and nothing ran a release before a mismatch
    reached a tag: the published package would be built with a uv or Python CI never ran."""
    ci, release = workflow_env("ci.yml"), workflow_env("release.yml")
    for pin in ("UV_VERSION", "PYTHON_VERSION"):
        assert pin in ci, f"ci.yml sets no {pin}"
        assert release.get(pin) == ci[pin], (
            f"release.yml's {pin} is {release.get(pin)!r} but ci.yml's is {ci[pin]!r}: bump them "
            "together (.claude/rules/pins.md)"
        )
