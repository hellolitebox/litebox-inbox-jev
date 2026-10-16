"""The `qa-verdict` status that scripts/qa_gate.py derives from a PR's QA review comments. The
merge-tree cases run against real git repositories."""

import subprocess
from pathlib import Path

import pytest
import qa_gate
from qa_gate import Review, evaluate, make_is_update_of, merges_cleanly, parse_review
from qa_gate import latest_review as latest_review_by

HEAD = "a" * 40
OLD = "b" * 40
MAIN = "c" * 40


MAINTAINER = "maintainer"


def review(verdict="APPROVED", head=HEAD, round_=1, author=MAINTAINER):
    body = (
        f"## QA review — round {round_}\n**Verdict: {verdict}**\nHead: {head}\n"
        "Checks: ruff ✅ · format ✅ · pytest ✅ (10 passed) · CI ✅\n"
    )
    return {
        "body": body,
        "html_url": f"https://github.com/x/y/pull/1#issuecomment-{round_}",
        "user": {"login": author},
    }


def latest_review(comments):
    """`qa_gate.latest_review` with only MAINTAINER trusted."""
    return latest_review_by(comments, lambda login: login == MAINTAINER)


def never(head, reviewed):
    return False


def clean(merge, content_parent, base_parent):
    return True


def test_no_review_is_pending():
    verdict = evaluate(None, HEAD, never)
    assert verdict.state == "pending"
    assert HEAD[:7] in verdict.description


def test_approved_head_is_success():
    verdict = evaluate(latest_review([review()]), HEAD, never)
    assert verdict.state == "success"


def test_changes_requested_is_failure():
    verdict = evaluate(latest_review([review("CHANGES_REQUESTED")]), HEAD, never)
    assert verdict.state == "failure"


def test_latest_review_wins():
    approved_then_changes = [review("APPROVED", round_=1), review("CHANGES_REQUESTED", round_=2)]
    assert evaluate(latest_review(approved_then_changes), HEAD, never).state == "failure"
    changes_then_approved = [review("CHANGES_REQUESTED", round_=1), review("APPROVED", round_=2)]
    assert evaluate(latest_review(changes_then_approved), HEAD, never).state == "success"


def test_other_comments_are_ignored():
    comments = [
        review("CHANGES_REQUESTED"),
        {
            "body": "## Dev reply — round 1\n1. ✅ fixed\n**Verdict: APPROVED** (quoted)\n",
            "user": {"login": MAINTAINER},
        },
        {"body": "Looks good to me, **Verdict: APPROVED**", "user": {"login": MAINTAINER}},
    ]
    assert evaluate(latest_review(comments), HEAD, never).state == "failure"


def test_a_review_by_someone_without_write_access_is_ignored():
    """The repo is public: anyone can post a comment that looks like a review (ADR 0054)."""
    forged = review("APPROVED", round_=2, author="stranger")
    assert latest_review([forged]) is None
    after_changes = [review("CHANGES_REQUESTED"), forged]
    assert evaluate(latest_review(after_changes), HEAD, never).state == "failure"
    no_author = {**review(round_=3), "user": None}
    assert latest_review([no_author]) is None


@pytest.mark.parametrize(
    ("permission", "trusted"), [("admin", True), ("write", True), ("read", False), ("none", False)]
)
def test_trust_is_write_access_asked_once_per_login(monkeypatch, permission, trusted):
    asked = []

    def gh_api(path, *args):
        asked.append(path)
        return {"permission": permission}

    monkeypatch.setattr(qa_gate, "gh_api", gh_api)
    is_trusted = qa_gate.make_is_trusted("o/r")
    assert is_trusted("ana") is trusted
    assert is_trusted("ana") is trusted
    assert asked == ["repos/o/r/collaborators/ana/permission"]


def _failing(stderr):
    def gh_api(path, *args):
        raise qa_gate.subprocess.CalledProcessError(1, ["gh", "api", path], stderr=stderr)

    return gh_api


def test_an_unknown_login_is_untrusted(monkeypatch):
    monkeypatch.setattr(qa_gate, "gh_api", _failing("gh: Not Found (HTTP 404)"))
    assert qa_gate.make_is_trusted("o/r")("ghost") is False


def test_a_failed_permission_call_fails_the_run(monkeypatch):
    """A transient error never decides a verdict: the call's failure reaches `main` (exit 1)."""
    monkeypatch.setattr(qa_gate, "gh_api", _failing("gh: Bad Gateway (HTTP 502)"))
    with pytest.raises(qa_gate.subprocess.CalledProcessError):
        qa_gate.make_is_trusted("o/r")("ana")


def test_stale_head_is_pending():
    verdict = evaluate(latest_review([review(head=OLD)]), HEAD, never)
    assert verdict.state == "pending"
    assert OLD[:7] in verdict.description and HEAD[:7] in verdict.description


def test_update_branch_merge_keeps_the_verdict():
    calls = []

    def is_update_of(head, reviewed):
        calls.append((head, reviewed))
        return True

    assert evaluate(latest_review([review(head=OLD)]), HEAD, is_update_of).state == "success"
    assert calls == [(HEAD, OLD)]
    changes = latest_review([review("CHANGES_REQUESTED", head=OLD)])
    assert evaluate(changes, HEAD, is_update_of).state == "failure"


@pytest.mark.parametrize(
    "body",
    [
        "## QA review — round 1\n**Verdict: APPROVED**\nChecks: ok\n",
        "## QA review — round 1\nHead: " + HEAD + "\nChecks: ok\n",
        "## QA review — round 1\n**Verdict: MAYBE**\nHead: " + HEAD + "\n",
        "## QA review — round 1\n**Verdict: APPROVED**\nHead: not-a-sha\n",
    ],
)
def test_review_without_verdict_or_head_is_failure(body):
    verdict = evaluate(latest_review([{"body": body, "user": {"login": MAINTAINER}}]), HEAD, never)
    assert verdict.state == "failure"
    assert "no Verdict or Head" in verdict.description


def test_head_may_be_a_short_prefix_or_in_backticks():
    assert parse_review("Head: `" + HEAD + "`") == Review(None, HEAD)
    short = latest_review([review(head=HEAD[:7])])
    assert evaluate(short, HEAD, never).state == "success"
    assert evaluate(short, OLD, never).state == "pending"


def test_latest_review_keeps_the_comment_url():
    found = latest_review([review(round_=1), review(round_=2)])
    assert found is not None
    assert found.url.endswith("issuecomment-2")


def _fake_gh(commits, on_main):
    """A `gh_api` that answers `commits/<sha>` and `compare/main...<sha>` from two dicts."""

    def gh_api(path, *args):
        if path.startswith("repos/o/r/commits/"):
            sha = path.rsplit("/", 1)[1]
            return {"parents": [{"sha": p} for p in commits[sha]]}
        if path.startswith("repos/o/r/compare/main..."):
            sha = path.rsplit("...", 1)[1]
            return {"status": "behind" if sha in on_main else "ahead"}
        raise AssertionError(path)

    return gh_api


def test_is_update_of_accepts_merges_from_main(monkeypatch):
    merge = "d" * 40
    monkeypatch.setattr(qa_gate, "gh_api", _fake_gh({merge: [OLD, MAIN]}, {MAIN}))
    assert make_is_update_of("o/r", "main", clean)(merge, OLD)


def test_is_update_of_follows_repeated_updates(monkeypatch):
    merge1, merge2, main2 = "d" * 40, "e" * 40, "f" * 40
    commits = {merge2: [merge1, main2], merge1: [OLD, MAIN]}
    monkeypatch.setattr(qa_gate, "gh_api", _fake_gh(commits, {MAIN, main2}))
    assert make_is_update_of("o/r", "main", clean)(merge2, OLD)


def test_is_update_of_rejects_new_commits(monkeypatch):
    plain, merge = "d" * 40, "e" * 40
    commits = {plain: [OLD], merge: [plain, MAIN]}
    monkeypatch.setattr(qa_gate, "gh_api", _fake_gh(commits, {MAIN}))
    is_update_of = make_is_update_of("o/r", "main", clean)
    assert not is_update_of(plain, OLD), "a plain commit on top of the reviewed one is new work"
    assert not is_update_of(merge, OLD), "a merge whose content side is new work is new work"


def test_is_update_of_rejects_a_merge_with_content_of_its_own(monkeypatch):
    merge1, merge2, main2 = "d" * 40, "e" * 40, "f" * 40
    commits = {merge2: [merge1, main2], merge1: [OLD, MAIN]}
    monkeypatch.setattr(qa_gate, "gh_api", _fake_gh(commits, {MAIN, main2}))
    checked = []

    def evil_below_the_top(merge, content_parent, base_parent):
        checked.append((merge, content_parent, base_parent))
        return merge != merge1

    assert not make_is_update_of("o/r", "main", evil_below_the_top)(merge2, OLD)
    assert checked == [(merge2, merge1, main2), (merge1, OLD, MAIN)], "every merge is checked"


def test_is_update_of_rejects_merges_of_two_branches(monkeypatch):
    other, merge = "d" * 40, "e" * 40
    monkeypatch.setattr(qa_gate, "gh_api", _fake_gh({merge: [OLD, other]}, {MAIN}))
    assert not make_is_update_of("o/r", "main", clean)(merge, OLD)


def test_is_update_of_stops_at_max_depth(monkeypatch):
    shas = [f"{i:040x}" for i in range(1, qa_gate.MAX_UPDATE_DEPTH + 3)]
    commits = {shas[i]: [shas[i + 1], MAIN] for i in range(len(shas) - 1)}
    monkeypatch.setattr(qa_gate, "gh_api", _fake_gh(commits, {MAIN}))
    assert not make_is_update_of("o/r", "main", clean)(shas[0], shas[-1])


def test_main_reports_which_api_call_failed(monkeypatch, capsys):
    def failing_run(cmd, **kwargs):
        raise qa_gate.subprocess.CalledProcessError(1, cmd, stderr="HTTP 403: nope\n")

    monkeypatch.setattr(qa_gate.subprocess, "run", failing_run)
    assert qa_gate.main(["--repo", "o/r", "--pr", "7"]) == 1
    err = capsys.readouterr().err
    assert "repos/o/r/pulls/7" in err and "HTTP 403" in err


def test_main_posts_pending_and_succeeds_for_a_new_head(monkeypatch, capsys):
    """A push with no review for the new head: the status is `pending` and the job still passes
    (exit 0); only a failed `gh api` call fails it."""
    calls = []

    def gh_api(path, *args):
        calls.append((path, args))
        if path == "repos/o/r/pulls/7":
            return {"head": {"sha": HEAD}, "base": {"ref": "main"}}
        if path == "repos/o/r/issues/7/comments":
            return [[review(head=OLD)]]  # --paginate --slurp: a list of pages
        if path == f"repos/o/r/collaborators/{MAINTAINER}/permission":
            return {"permission": "admin"}
        if path.startswith("repos/o/r/commits/"):
            return {"parents": [{"sha": OLD}]}  # a plain commit: new work
        if path == f"repos/o/r/statuses/{HEAD}":
            return {}
        raise AssertionError(path)

    monkeypatch.setattr(qa_gate, "gh_api", gh_api)

    assert qa_gate.main(["--repo", "o/r", "--pr", "7"]) == 0
    path, fields = calls[-1]
    assert path == f"repos/o/r/statuses/{HEAD}"
    assert "state=pending" in fields and "context=qa-verdict" in fields
    assert "pending" in capsys.readouterr().out


def _gh_for_an_update_merge(calls):
    """A `gh_api` for PR 7: a trusted review of OLD, and a head that merges `main` into OLD."""

    def gh_api(path, *args):
        calls.append((path, args))
        if path == "repos/o/r/pulls/7":
            return {"head": {"sha": HEAD}, "base": {"ref": "main"}}
        if path == "repos/o/r/issues/7/comments":
            return [[review(head=OLD)]]
        if path == f"repos/o/r/collaborators/{MAINTAINER}/permission":
            return {"permission": "write"}
        if path == f"repos/o/r/commits/{HEAD}":
            return {"parents": [{"sha": OLD}, {"sha": MAIN}]}
        if path.startswith("repos/o/r/compare/main..."):
            return {"status": "behind" if path.endswith(MAIN) else "ahead"}
        if path == f"repos/o/r/statuses/{HEAD}":
            return {}
        raise AssertionError(path)

    return gh_api


@pytest.mark.parametrize(("clean_merge", "state"), [(True, "success"), (False, "pending")])
def test_main_keeps_a_trusted_approval_only_across_a_clean_merge(monkeypatch, clean_merge, state):
    """The trusted review selects the verdict (ADR 0054); the merge check decides whether the
    head still carries it (ADR 0055)."""
    calls, checked = [], []
    monkeypatch.setattr(qa_gate, "gh_api", _gh_for_an_update_merge(calls))

    def make_merges_cleanly(pr, base):
        def check(merge, content_parent, base_parent):
            checked.append((pr, base, merge, content_parent, base_parent))
            return clean_merge

        return check

    monkeypatch.setattr(qa_gate, "make_merges_cleanly", make_merges_cleanly)
    assert qa_gate.main(["--repo", "o/r", "--pr", "7"]) == 0
    assert checked == [(7, "main", HEAD, OLD, MAIN)]
    path, fields = calls[-1]
    assert path == f"repos/o/r/statuses/{HEAD}" and f"state={state}" in fields


def test_main_fails_the_run_when_git_fails(monkeypatch, capsys):
    """A git error is never read as a verdict: the job fails (exit 1) and names the command and the
    refs it was fetching."""
    calls = []
    monkeypatch.setattr(qa_gate, "gh_api", _gh_for_an_update_merge(calls))

    def failing_git(*args):
        raise subprocess.CalledProcessError(128, ["git", *args], stderr="fatal: no remote\n")

    monkeypatch.setattr(qa_gate, "git", failing_git)
    assert qa_gate.main(["--repo", "o/r", "--pr", "7"]) == 1
    assert (
        "git fetch origin refs/pull/7/head refs/heads/main failed: fatal: no remote"
        in capsys.readouterr().err
    )
    assert not any(path.startswith("repos/o/r/statuses/") for path, _ in calls), "nothing posted"


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        (
            [
                "git",
                "fetch",
                "--no-tags",
                "--quiet",
                "origin",
                "refs/pull/7/head",
                "refs/heads/main",
            ],
            "git fetch origin refs/pull/7/head refs/heads/main",
        ),
        (
            ["gh", "api", "repos/o/r/issues/7/comments", "--paginate", "--slurp"],
            "gh api repos/o/r/issues/7/comments",
        ),
        (
            [
                "gh",
                "api",
                "repos/o/r/statuses/abc",
                "-f",
                "state=success",
                "-f",
                "context=qa-verdict",
            ],
            "gh api repos/o/r/statuses/abc",
        ),
        (
            ["gh", "api", "repos/o/r/statuses/abc", "-F", "state=success"],
            "gh api repos/o/r/statuses/abc",
        ),
        (
            ["gh", "api", "-f", "state=success", "repos/o/r/statuses/abc"],
            "gh api repos/o/r/statuses/abc",
        ),
        (["git", "merge-tree", "--write-tree", "aaa", "bbb"], "git merge-tree aaa bbb"),
        (["gh", "api", "--method=POST", "repos/o/r/x"], "gh api repos/o/r/x"),
        (["gh", "api", "--jq", ".[0].body", "repos/o/r/x"], "gh api repos/o/r/x"),
    ],
)
def test_describe_command_names_the_operands_not_the_options(cmd, expected):
    assert qa_gate.describe_command(cmd) == expected


@pytest.mark.parametrize("flag", ["--paginate", "--slurp", "--no-tags", "--quiet", "--write-tree"])
def test_describe_command_keeps_the_operand_after_a_value_less_option(flag):
    assert qa_gate.describe_command(["git", "x", flag, "operand"]) == "git x operand"


def test_describe_command_never_prints_a_header_value():
    """An option the script doesn't use today still has its value dropped, so a header added
    later can't put a token in the error line."""
    cmd = ["gh", "api", "-H", "Authorization: token x", "repos/o/r/x"]
    described = qa_gate.describe_command(cmd)
    assert "token x" not in described
    assert described == "gh api repos/o/r/x"


def git(cwd: Path, *args: str, check: bool = True) -> str:
    """`git …` in `cwd` with a committer identity (CI's runners have none configured)."""
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(cwd: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (cwd / name).write_text(text)
    git(cwd, "add", *files)
    git(cwd, "commit", "-q", "-m", message)
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def diverged(tmp_path, monkeypatch):
    """A repo where `feat` (the reviewed commit) and `main` both moved since they forked; `a.txt`
    has lines far enough apart for each side to edit its own without a conflict. The developer's
    own git config is left out, so a setting there (signing, a merge driver) can't change the
    outcome."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.chdir(tmp_path)
    git(tmp_path, "init", "-q", "-b", "main")
    lines = [f"line {i}" for i in range(10)]
    commit(tmp_path, {"a.txt": "\n".join(lines) + "\n", "b.txt": "b\n"}, "base")
    git(tmp_path, "switch", "-q", "-c", "feat")
    reviewed = commit(tmp_path, {"a.txt": "\n".join(["feat 0", *lines[1:]]) + "\n"}, "feat")
    git(tmp_path, "switch", "-q", "main")
    base = commit(tmp_path, {"a.txt": "\n".join([*lines[:9], "main 9"]) + "\n"}, "main")
    git(tmp_path, "switch", "-q", "feat")
    return tmp_path, reviewed, base


def _conflicting_edits(repo):
    """`feat` and `main` each edit `b.txt`'s one line; returns (reviewed, base), on `main`."""
    reviewed = commit(repo, {"b.txt": "feat\n"}, "feat edits b")
    git(repo, "switch", "-q", "main")
    base = commit(repo, {"b.txt": "main\n"}, "main edits b")
    return reviewed, base


def test_a_clean_merge_of_main_is_clean(diverged):
    repo, reviewed, base = diverged
    git(repo, "merge", "-q", "--no-edit", "main")
    merge = git(repo, "rev-parse", "HEAD")
    assert merges_cleanly(merge, reviewed, base)
    assert merges_cleanly(merge, base, reviewed), "the parents' order doesn't matter"


def test_a_merge_with_edits_of_its_own_is_not_clean(diverged):
    repo, reviewed, base = diverged
    git(repo, "merge", "-q", "--no-commit", "main")
    (repo / "b.txt").write_text("an edit no review has seen\n")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "--no-edit")
    assert not merges_cleanly(git(repo, "rev-parse", "HEAD"), reviewed, base)


def test_a_conflicted_merge_resolved_by_hand_is_not_clean(diverged):
    """Even a resolution that keeps both sides needs a fresh review: the gate can't tell a
    faithful resolution from new content."""
    repo, _, _ = diverged
    reviewed, base = _conflicting_edits(repo)
    git(repo, "switch", "-q", "feat")
    git(repo, "merge", "-q", "main", check=False)
    assert (repo / ".git" / "MERGE_HEAD").exists(), "the merge stopped on the conflict"
    (repo / "b.txt").write_text("feat\nmain\n")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "--no-edit")
    assert not merges_cleanly(git(repo, "rev-parse", "HEAD"), reviewed, base)


def test_a_merge_committed_with_its_conflict_markers_is_not_clean(diverged):
    """The merge's tree is exactly what `git merge-tree` writes for the conflict, markers
    included, so only the conflict itself tells it apart from a clean merge."""
    repo, _, _ = diverged
    reviewed, base = _conflicting_edits(repo)
    result = subprocess.run(
        ["git", "merge-tree", "--write-tree", reviewed, base],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, "the merge conflicts"
    marker_tree = result.stdout.split("\n", 1)[0]
    merge = git(repo, "commit-tree", marker_tree, "-p", reviewed, "-p", base, "-m", "merge main")
    assert git(repo, "rev-parse", f"{merge}^{{tree}}") == marker_tree
    assert not merges_cleanly(merge, reviewed, base)


def test_merges_cleanly_raises_when_git_fails(diverged):
    """An unknown commit makes `git merge-tree` fail, which is not a conflict."""
    _, reviewed, _ = diverged
    with pytest.raises(subprocess.CalledProcessError):
        merges_cleanly(reviewed, reviewed, "0" * 40)


def test_make_merges_cleanly_fetches_once_and_only_when_asked(monkeypatch):
    calls = []
    monkeypatch.setattr(qa_gate, "git", lambda *args: calls.append(args) or "")
    monkeypatch.setattr(qa_gate, "merges_cleanly", lambda *shas: True)
    check = qa_gate.make_merges_cleanly(7, "main")
    assert calls == []
    assert check("m1", "r", "b") and check("m2", "m1", "b2")
    assert calls == [
        ("fetch", "--no-tags", "--quiet", "origin", "refs/pull/7/head", "refs/heads/main")
    ]
