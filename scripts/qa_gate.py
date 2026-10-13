"""Set a pull request's `qa-verdict` commit status from its latest QA review comment.

The QA reviewer posts `## QA review — round N` comments with a `**Verdict: …**` line and a
`Head: <sha>` line naming the commit it reviewed. This script turns the latest such comment into
a commit status on the PR's head, so the repo ruleset can require it:

- `success`: the latest review is APPROVED and names the head, or the head is that commit merged
  with the base branch (`gh pr update-branch`), so the reviewed content is unchanged;
- `failure`: the latest review is CHANGES_REQUESTED, or has no Verdict or Head line;
- `pending`: there is no review yet, or the head moved since the latest review.

Runs from `.github/workflows/qa-gate.yml` on every push to the PR and on every comment. Needs
`gh` and a token with `statuses: write` (`GH_TOKEN`). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

CONTEXT = "qa-verdict"
REVIEW_HEADING = "## QA review"
VERDICT_RE = re.compile(r"\*\*Verdict:\s*(APPROVED|CHANGES_REQUESTED)\*\*")
HEAD_RE = re.compile(r"^Head:\s*`?([0-9a-f]{7,40})`?\s*$", re.MULTILINE)
# How many `update-branch` merges on top of the reviewed commit still count as that commit.
MAX_UPDATE_DEPTH = 5

VerdictWord = Literal["APPROVED", "CHANGES_REQUESTED"]
State = Literal["success", "failure", "pending"]


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


def latest_review(comments: list[dict[str, Any]]) -> Review | None:
    """The newest comment that starts with the QA heading; `comments` is oldest first."""
    for comment in reversed(comments):
        body = comment.get("body") or ""
        if body.startswith(REVIEW_HEADING):
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


def make_is_update_of(repo: str, base: str) -> Callable[[str, str], bool]:
    """`is_update_of(head, reviewed)` over the GitHub API.

    True when walking down from `head` through merge commits whose other parent is on `base`
    reaches `reviewed` within `MAX_UPDATE_DEPTH` steps: the shape `gh pr update-branch` leaves.
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
            sha = content[0]
            if sha.startswith(reviewed):
                return True
        return False

    return is_update_of


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set a PR's qa-verdict status from QA's review.")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, type=int, help="pull request number")
    parser.add_argument("--dry-run", action="store_true", help="print the verdict, don't post it")
    args = parser.parse_args(argv)
    try:
        pr = gh_api(f"repos/{args.repo}/pulls/{args.pr}")
        head, base = pr["head"]["sha"], pr["base"]["ref"]
        review = latest_review(gh_api_list(f"repos/{args.repo}/issues/{args.pr}/comments"))
        verdict = evaluate(review, head, make_is_update_of(args.repo, base))
        print(f"{CONTEXT} for {head[:7]}: {verdict.state} — {verdict.description}")
        if not args.dry_run:
            post_status(args.repo, head, verdict, review.url if review else "")
    except subprocess.CalledProcessError as e:
        print(f"gh api {e.cmd[2]} failed: {e.stderr.strip()}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
