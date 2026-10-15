"""Set a pull request's `qa-verdict` commit status from its latest QA review comment.

The QA reviewer posts `## QA review — round N` comments with a `**Verdict: …**` line and a
`Head: <sha>` line naming the commit it reviewed. This script turns the latest such comment into
a commit status on the PR's head, so the repo ruleset can require it:

- `success`: the latest review is APPROVED and names the head, or the head is that commit merged
  with the base branch (`gh pr update-branch`) and each merge's tree is exactly what git merges its
  two parents to, conflict-free, so the reviewed content is unchanged. A merge that adds edits of
  its own (an "evil merge"), or whose conflicts were resolved by hand, is new content: it needs a
  fresh review, as does a plain commit (ADR 0055);
- `failure`: the latest review is CHANGES_REQUESTED, or has no Verdict or Head line;
- `pending`: there is no review yet, or the head moved since the latest review.

Only comments by people with write access to the repo count: it is public, so anyone can post a
comment that looks like a review (ADR 0054).

Runs from `.github/workflows/qa-gate.yml` on every push to the PR and on every comment, in a
full-history checkout. Needs `gh` and a token with `statuses: write` (`GH_TOKEN`), and git 2.38+
(`git merge-tree --write-tree`), which fetches the PR head and the base branch the first time a
merge needs checking. Standard library only. Exits 0 once the status is posted, whatever its state:
the verdict is the status, and the job that runs this only says the gate ran. Exits 1 when a
`gh api` or `git` call fails.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

CONTEXT = "qa-verdict"
REVIEW_HEADING = "## QA review"
VERDICT_RE = re.compile(r"\*\*Verdict:\s*(APPROVED|CHANGES_REQUESTED)\*\*")
HEAD_RE = re.compile(r"^Head:\s*`?([0-9a-f]{7,40})`?\s*$", re.MULTILINE)
# Repo permissions that make a commenter's QA review count (ADR 0054).
# The endpoint's legacy `permission` field reports maintain as write and triage as read.
TRUSTED_PERMISSIONS = frozenset({"admin", "write"})
# How many `update-branch` merges on top of the reviewed commit still count as that commit.
MAX_UPDATE_DEPTH = 5

VerdictWord = Literal["APPROVED", "CHANGES_REQUESTED"]
State = Literal["success", "failure", "pending"]
# `merges_cleanly(merge, content_parent, base_parent)`: whether `merge`'s tree is what git merges
# its two parents to with no conflict (ADR 0055).
MergeCheck = Callable[[str, str, str], bool]
# `git merge-tree --write-tree` exits 1 when the merge has conflicts.
_MERGE_TREE_CONFLICTS = 1
_OID_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class Review:
    """What the gate needs from a QA review comment."""

    verdict: VerdictWord | None
    head: str | None
    url: str = ""


@dataclass(frozen=True)
class Verdict:
    """A commit status."""

    state: State
    description: str


def gh_api(*args: str) -> Any:
    """`gh api …` parsed as JSON."""
    out = subprocess.run(["gh", "api", *args], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def gh_api_list(path: str) -> list[dict[str, Any]]:
    """Every page of a list endpoint, oldest first."""
    pages = gh_api(path, "--paginate", "--slurp")
    return [item for page in pages for item in page]


def parse_review(body: str, url: str = "") -> Review:
    """The verdict and reviewed commit of a `## QA review` comment; `None` for a missing line."""
    verdict = VERDICT_RE.search(body)
    head = HEAD_RE.search(body)
    word: VerdictWord | None = None
    if verdict:
        word = "APPROVED" if verdict.group(1) == "APPROVED" else "CHANGES_REQUESTED"
    return Review(word, head.group(1) if head else None, url)


def latest_review(
    comments: list[dict[str, Any]], is_trusted: Callable[[str], bool]
) -> Review | None:
    """The newest comment that starts with the QA heading and whose author `is_trusted`;
    `comments` is oldest first. A review-looking comment by anyone else is ignored."""
    for comment in reversed(comments):
        body = comment.get("body") or ""
        login = (comment.get("user") or {}).get("login") or ""
        if body.startswith(REVIEW_HEADING) and login and is_trusted(login):
            return parse_review(body, comment.get("html_url") or "")
    return None


def evaluate(review: Review | None, head: str, is_update_of: Callable[[str, str], bool]) -> Verdict:
    """The status for `head` given the latest review.

    `is_update_of(head, reviewed)` says whether `head` is `reviewed` plus merges of the base
    branch, which keep the verdict.
    """
    short = head[:7]
    if review is None:
        return Verdict("pending", f"No QA review yet for {short}")
    if review.verdict is None or review.head is None:
        return Verdict("failure", "Latest QA review has no Verdict or Head line")
    if not (head.startswith(review.head) or is_update_of(head, review.head)):
        return Verdict("pending", f"QA reviewed {review.head[:7]}; head is {short}")
    if review.verdict == "APPROVED":
        return Verdict("success", f"QA approved {review.head[:7]}")
    return Verdict("failure", f"QA requested changes on {review.head[:7]}")


def make_is_update_of(
    repo: str, base: str, merges_cleanly: MergeCheck
) -> Callable[[str, str], bool]:
    """`is_update_of(head, reviewed)` over the GitHub API.

    True when walking down from `head` through merge commits whose other parent is on `base`
    reaches `reviewed` within `MAX_UPDATE_DEPTH` steps (the shape `gh pr update-branch` leaves),
    and `merges_cleanly` holds for every one of those merges: none adds content of its own.
    """

    def on_base(sha: str) -> bool:
        status = gh_api(f"repos/{repo}/compare/{base}...{sha}")["status"]
        return status in ("identical", "behind")

    def is_update_of(head: str, reviewed: str) -> bool:
        sha = head
        for _ in range(MAX_UPDATE_DEPTH):
            parents = [p["sha"] for p in gh_api(f"repos/{repo}/commits/{sha}")["parents"]]
            if len(parents) != 2:
                return False
            content = [p for p in parents if not on_base(p)]
            if len(content) != 1:
                return False
            (base_parent,) = (p for p in parents if p != content[0])
            if not merges_cleanly(sha, content[0], base_parent):
                return False
            sha = content[0]
            if sha.startswith(reviewed):
                return True
        return False

    return is_update_of


def make_is_trusted(repo: str) -> Callable[[str], bool]:
    """`is_trusted(login)` over the GitHub API: the user has write access to `repo` or more
    (`TRUSTED_PERMISSIONS`), asked once per login. A login GitHub doesn't know (404) is
    untrusted; any other failure of the call fails the run (`main` exits 1, to be re-run), so a
    transient error never decides a verdict. Not `author_association`, which shows a member whose
    org membership is private as a mere contributor."""
    known: dict[str, bool] = {}

    def is_trusted(login: str) -> bool:
        if login not in known:
            try:
                answer = gh_api(f"repos/{repo}/collaborators/{login}/permission")
            except subprocess.CalledProcessError as exc:
                if "HTTP 404" not in (exc.stderr or ""):
                    raise
                answer = {}
            known[login] = answer.get("permission") in TRUSTED_PERMISSIONS
        return known[login]

    return is_trusted


def git(*args: str) -> str:
    """`git …`'s stdout, stripped; raises `CalledProcessError` when it fails."""
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def merges_cleanly(merge: str, content_parent: str, base_parent: str) -> bool:
    """Whether `merge`'s tree is exactly what git merges its two parents to, with no conflict.

    A conflicted merge is never clean, even when its hand resolution looks right: what the
    resolution chose is content no review has seen. Needs the three commits locally. Raises
    `CalledProcessError` when git can't merge them at all (an unknown commit): an error is never
    read as a conflict, so it fails the run instead of deciding a verdict.
    """
    result = subprocess.run(
        ["git", "merge-tree", "--write-tree", content_parent, base_parent],
        capture_output=True,
        text=True,
        check=False,
    )
    merged_tree = result.stdout.split("\n", 1)[0]
    # Exit 1 is a conflict when a tree was written, else an error (an unknown commit).
    if result.returncode not in {0, _MERGE_TREE_CONFLICTS} or not _OID_RE.fullmatch(merged_tree):
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )
    if result.returncode == _MERGE_TREE_CONFLICTS:
        return False
    return merged_tree == git("rev-parse", f"{merge}^{{tree}}")


def make_merges_cleanly(pr: int, base: str) -> MergeCheck:
    """`merges_cleanly` over the local repository, after fetching the PR head and `base` once.

    The fetch happens only when a merge needs checking: a head that is the reviewed commit, or a
    plain commit, never pays for it. Only objects are read: no PR code is checked out or run.
    """

    @functools.cache
    def fetch() -> None:
        git("fetch", "--no-tags", "--quiet", "origin", f"refs/pull/{pr}/head", f"refs/heads/{base}")

    def check(merge: str, content_parent: str, base_parent: str) -> bool:
        fetch()
        return merges_cleanly(merge, content_parent, base_parent)

    return check


def post_status(repo: str, sha: str, verdict: Verdict, url: str) -> None:
    """Create the `qa-verdict` commit status on `sha`, linking to the review comment."""
    fields = [
        "-f",
        f"state={verdict.state}",
        "-f",
        f"context={CONTEXT}",
        "-f",
        f"description={verdict.description}",
    ]
    if url:
        fields += ["-f", f"target_url={url}"]
    gh_api(f"repos/{repo}/statuses/{sha}", *fields)


# The value-less options in the commands this script runs. Any other option is assumed to take the
# next word as its value (`-f state=…`, `-H "Authorization: …"`, `--jq …`), so a new one is dropped
# with its value: an unknown option costs the error line an operand, never leaks a value.
_BARE_FLAGS = frozenset({"--paginate", "--slurp", "--no-tags", "--quiet", "--write-tree"})


def describe_command(cmd: Sequence[str]) -> str:
    """A failed command as the error line names it: the program, the subcommand and its operands
    (the API path, the remote and refs fetched). Every option is dropped, and so is the next word
    unless the option is in `_BARE_FLAGS` or carries its value inline (`--opt=value`)."""
    words: list[str] = []
    skip_next = False
    for word in cmd:
        if skip_next:
            skip_next = False
        elif word.startswith("-"):
            skip_next = word not in _BARE_FLAGS and "=" not in word
        else:
            words.append(word)
    return " ".join(words)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set a PR's qa-verdict status from QA's review.")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, type=int, help="pull request number")
    parser.add_argument("--dry-run", action="store_true", help="print the verdict, don't post it")
    args = parser.parse_args(argv)
    try:
        pr = gh_api(f"repos/{args.repo}/pulls/{args.pr}")
        head, base = pr["head"]["sha"], pr["base"]["ref"]
        comments = gh_api_list(f"repos/{args.repo}/issues/{args.pr}/comments")
        review = latest_review(comments, make_is_trusted(args.repo))
        is_update_of = make_is_update_of(args.repo, base, make_merges_cleanly(args.pr, base))
        verdict = evaluate(review, head, is_update_of)
        print(f"{CONTEXT} for {head[:7]}: {verdict.state} — {verdict.description}")
        if not args.dry_run:
            post_status(args.repo, head, verdict, review.url if review else "")
    except subprocess.CalledProcessError as e:
        print(f"{describe_command(e.cmd)} failed: {e.stderr.strip()}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
