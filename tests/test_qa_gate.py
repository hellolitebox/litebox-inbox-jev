"""The `qa-verdict` status that scripts/qa_gate.py derives from a PR's QA review comments."""

import pytest
import qa_gate
from qa_gate import Review, evaluate, latest_review, make_is_update_of, parse_review

HEAD = "a" * 40
OLD = "b" * 40
MAIN = "c" * 40


def review(verdict="APPROVED", head=HEAD, round_=1):
    body = (
        f"## QA review — round {round_}\n**Verdict: {verdict}**\nHead: {head}\n"
        "Checks: ruff ✅ · format ✅ · pytest ✅ (10 passed) · CI ✅\n"
    )
    return {"body": body, "html_url": f"https://github.com/x/y/pull/1#issuecomment-{round_}"}


def never(head, reviewed):
    return False


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
        {"body": "## Dev reply — round 1\n1. ✅ fixed\n**Verdict: APPROVED** (quoted)\n"},
        {"body": "Looks good to me, **Verdict: APPROVED**"},
    ]
    assert evaluate(latest_review(comments), HEAD, never).state == "failure"


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
    verdict = evaluate(latest_review([{"body": body}]), HEAD, never)
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
    assert make_is_update_of("o/r", "main")(merge, OLD)


def test_is_update_of_follows_repeated_updates(monkeypatch):
    merge1, merge2, main2 = "d" * 40, "e" * 40, "f" * 40
    commits = {merge2: [merge1, main2], merge1: [OLD, MAIN]}
    monkeypatch.setattr(qa_gate, "gh_api", _fake_gh(commits, {MAIN, main2}))
    assert make_is_update_of("o/r", "main")(merge2, OLD)


def test_is_update_of_rejects_new_commits(monkeypatch):
    plain, merge = "d" * 40, "e" * 40
    commits = {plain: [OLD], merge: [plain, MAIN]}
    monkeypatch.setattr(qa_gate, "gh_api", _fake_gh(commits, {MAIN}))
    is_update_of = make_is_update_of("o/r", "main")
    assert not is_update_of(plain, OLD), "a plain commit on top of the reviewed one is new work"
    assert not is_update_of(merge, OLD), "a merge whose content side is new work is new work"


def test_is_update_of_rejects_merges_of_two_branches(monkeypatch):
    other, merge = "d" * 40, "e" * 40
    monkeypatch.setattr(qa_gate, "gh_api", _fake_gh({merge: [OLD, other]}, {MAIN}))
    assert not make_is_update_of("o/r", "main")(merge, OLD)


def test_is_update_of_stops_at_max_depth(monkeypatch):
    shas = [f"{i:040x}" for i in range(1, qa_gate.MAX_UPDATE_DEPTH + 3)]
    commits = {shas[i]: [shas[i + 1], MAIN] for i in range(len(shas) - 1)}
    monkeypatch.setattr(qa_gate, "gh_api", _fake_gh(commits, {MAIN}))
    assert not make_is_update_of("o/r", "main")(shas[0], shas[-1])


def test_main_reports_which_api_call_failed(monkeypatch, capsys):
    def failing_run(cmd, **kwargs):
        raise qa_gate.subprocess.CalledProcessError(1, cmd, stderr="HTTP 403: nope\n")

    monkeypatch.setattr(qa_gate.subprocess, "run", failing_run)
    assert qa_gate.main(["--repo", "o/r", "--pr", "7"]) == 1
    err = capsys.readouterr().err
    assert "repos/o/r/pulls/7" in err and "HTTP 403" in err
