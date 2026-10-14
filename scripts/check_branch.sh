#!/usr/bin/env bash
# Run a branch's scripts/dry_classify.py against this deployment's DB and Gmail, read-only.
#
# Usage, on the server, from the deployment checkout (e.g. /opt/inbox-jev):
#   scripts/check_branch.sh <branch> --account you@example.com --query 'in:inbox newer_than:7d' --limit 30
#   scripts/check_branch.sh <branch> --account you@example.com --ids ID… [--ignore-rules]
#   scripts/check_branch.sh <branch> --account you@example.com --reference --quiet [--accepted 7]
#
# The branch is fetched into a throwaway worktree next to the deployment (never over it: the
# running app uses this checkout) and its src/, categories.yaml and dry_classify.py are mounted
# into the app image ahead of the installed package. The image's dependencies are the deployed
# ones, so a branch whose runtime dependencies differ is refused (build a separate image): uv.lock
# differs, or pyproject.toml's requires-python, dependencies or a non-dev optional-dependency
# group (scripts/runtime_deps.py, with the host's python3). Tool config changes (ruff, mypy,
# coverage) pass.
# dry_classify.py never writes Gmail or the DB. Put the output in the PR, never email bodies.
# Run it as the user that owns the checkout (the one that runs `git pull`; with sudo if that
# is root): git refuses or leaves root-owned files behind otherwise.
set -euo pipefail
# shellcheck source=ops_common.sh
. "$(dirname "$0")/ops_common.sh"

branch=${1:?usage: $0 <branch> --account EMAIL [dry_classify.py options]}
shift
deploy=$OPS_ROOT
wt=${TMPDIR:-/tmp}/inbox-jev-check

cleanup() { git -C "$deploy" worktree remove --force "$wt" 2>/dev/null || true; }
trap cleanup EXIT
cleanup

# CATEGORIES_FILE as compose resolves it (empty counts as unset). A deployment's own file is the
# one its accounts.yaml is validated against, so the branch is checked with it.
categories=$(env_value CATEGORIES_FILE categories.yaml)
categories=${categories:-categories.yaml}
categories=${categories#./}
if [ "$categories" != categories.yaml ] && [ ! -f "$deploy/$categories" ]; then
  echo "CATEGORIES_FILE=$categories: $deploy/$categories not found" >&2
  exit 2
fi

git -C "$deploy" fetch origin "$branch"
if ! git -C "$deploy" diff --quiet HEAD FETCH_HEAD -- uv.lock ||
  ! python3 -I "$deploy/scripts/runtime_deps.py" \
    <(git -C "$deploy" show HEAD:pyproject.toml) <(git -C "$deploy" show FETCH_HEAD:pyproject.toml); then
  echo "$branch changes pyproject.toml or uv.lock: build a separate image instead" >&2
  exit 1
fi

if [ "$categories" = categories.yaml ]; then
  categories=/opt/branch/categories.yaml
else
  echo "CATEGORIES_FILE=$categories: checking with this deployment's $categories, not the" \
    "branch's categories.yaml (--categories /opt/branch/categories.yaml checks those)" >&2
  if ! git -C "$deploy" diff --quiet HEAD FETCH_HEAD -- categories.yaml; then
    rule=$(printf '!%.0s' {1..80})
    printf '%s\n' "$rule" \
      "WARNING: $branch changes categories.yaml, but this run uses $categories:" \
      "the branch's categories changes are NOT checked. To check them, add" \
      "--categories /opt/branch/categories.yaml" "$rule" >&2
  fi
  categories=/app/$categories
fi
git -C "$deploy" worktree add --detach "$wt" FETCH_HEAD

cd "$deploy"
docker compose run --rm --no-deps -e PYTHONPATH=/opt/branch \
  -v "$wt/src/inbox_jev:/opt/branch/inbox_jev:ro" \
  -v "$wt/categories.yaml:/opt/branch/categories.yaml:ro" \
  -v "$wt/scripts/dry_classify.py:/opt/branch/dry_classify.py:ro" \
  --entrypoint python app /opt/branch/dry_classify.py \
  --categories "$categories" "$@"
