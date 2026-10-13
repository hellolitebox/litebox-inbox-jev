#!/usr/bin/env bash
# Run a branch's scripts/dry_classify.py against this deployment's DB and Gmail, read-only.
#
# Usage, on the server, from the deployment checkout (e.g. /opt/inbox-jev):
#   scripts/check_branch.sh <branch> --account you@example.com --query 'in:inbox newer_than:7d' --limit 30
#   scripts/check_branch.sh <branch> --account you@example.com --ids ID… [--ignore-rules]
#
# The branch is fetched into a throwaway worktree next to the deployment (never over it: the
# running app uses this checkout) and its src/, categories.yaml and dry_classify.py are mounted
# into the app image ahead of the installed package. The image's dependencies are the deployed
# ones, so a branch that changes pyproject.toml or uv.lock is refused: build a separate image.
# dry_classify.py never writes Gmail or the DB. Put the output in the PR, never email bodies.
# Run it as the user that owns the checkout (the one that runs `git pull`; with sudo if that
# is root): git refuses or leaves root-owned files behind otherwise.
set -euo pipefail

branch=${1:?usage: $0 <branch> --account EMAIL [dry_classify.py options]}
shift
deploy=$(cd "$(dirname "$0")/.." && pwd)
wt=/tmp/inbox-jev-check

cleanup() { git -C "$deploy" worktree remove --force "$wt" 2>/dev/null || true; }
trap cleanup EXIT
cleanup

git -C "$deploy" fetch origin "$branch"
if ! git -C "$deploy" diff --quiet HEAD FETCH_HEAD -- pyproject.toml uv.lock; then
  echo "$branch changes pyproject.toml or uv.lock: build a separate image instead" >&2
  exit 1
fi
git -C "$deploy" worktree add --detach "$wt" FETCH_HEAD

cd "$deploy"
docker compose run --rm --no-deps -e PYTHONPATH=/opt/branch \
  -v "$wt/src/inbox_jev:/opt/branch/inbox_jev:ro" \
  -v "$wt/categories.yaml:/opt/branch/categories.yaml:ro" \
  -v "$wt/scripts/dry_classify.py:/opt/branch/dry_classify.py:ro" \
  --entrypoint python app /opt/branch/dry_classify.py \
  --categories /opt/branch/categories.yaml "$@"
