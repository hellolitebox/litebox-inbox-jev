#!/usr/bin/env bash
# The checks CI's `test` job runs, in one command, so green here means green there and every
# document that says "run the checks" points at one place.
#
#   scripts/check.sh          uv.lock matches pyproject.toml, ruff format --check, ruff check,
#                             mdformat --check on every tracked or new .md, mypy, pytest with
#                             coverage
#   scripts/check.sh --fix    the same, fixing first: ruff check --fix-only, then ruff format (a
#                             lint fix can leave unformatted code), mdformat; ruff check then
#                             reports what no fix covers
#
# Runs from any cwd. Uses `uv` from PATH, or $UV when set (the agents' worktrees pass it), and
# syncs the checkout's own .venv from uv.lock first, exactly as CI does, so a dependency change is
# installed and a stale venv can't hide a failure. PYTEST_ADDOPTS is honoured: CI adds the
# job-summary coverage report through it. Exits with the first failing step's status.
set -euo pipefail
cd "$(dirname "$0")/.."

uv=${UV:-uv}
fix=0
case "${1:-}" in
  --fix) fix=1 ;;
  '') ;;
  *) echo "usage: $0 [--fix]" >&2; exit 2 ;;
esac

step() { printf '\n== %s\n' "$*"; }

step "uv lock --check"
"$uv" lock --check
step "uv sync --frozen --extra dev"
"$uv" sync --frozen --extra dev
if [ "$fix" = 1 ]; then
  step "ruff check --fix-only"
  "$uv" run --frozen ruff check --fix-only src tests scripts
  step "ruff format"
  "$uv" run --frozen ruff format src tests scripts
  step "mdformat"
  git ls-files -z -co --exclude-standard -- '*.md' | xargs -0 "$uv" run --frozen mdformat
else
  step "ruff format --check"
  "$uv" run --frozen ruff format --check src tests scripts
  step "mdformat --check"
  git ls-files -z -co --exclude-standard -- '*.md' | xargs -0 "$uv" run --frozen mdformat --check
fi
step "ruff check"
"$uv" run --frozen ruff check src tests scripts
step "mypy"
"$uv" run --frozen mypy
step "pytest --cov"
"$uv" run --frozen pytest -q --cov --cov-report=term-missing:skip-covered
