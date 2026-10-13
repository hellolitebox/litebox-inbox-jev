# 0016. Reproducible build with `uv.lock`

- Status: Accepted
- Date: 2026-10-13
- PR: #16

## Context
There was no lockfile: `pip install .` resolved fresh versions on every build, so two deploys of
the same commit could run different dependencies (`typesafe-sdk` has no upper bound, and
`logs.py` copies one of its internal log formats). The Dockerfile also reinstalled every
dependency on any code change.

## Decision
Lock every dependency in `uv.lock`. The image installs with `uv sync --frozen` in a cached
dependency layer, with `uv` from a pinned `ghcr.io/astral-sh/uv` stage; CI checks
`uv lock --check`, builds the image (required `docker` check), smoke-tests it as a non-root uid,
and fails when the Dockerfile and CI disagree on the `uv` or Python version. Dependabot proposes
weekly updates (uv, GitHub Actions, Docker), which go through the QA loop.

## Alternatives considered
- `pip freeze` into a requirements file: no lock check in CI, and transitive pins drift by hand.
- Upper bounds in `pyproject.toml` only: still resolves differently over time.
- `[tool.uv] required-version` as the single uv pin: Dependabot's bundled uv refuses to lock
  under a mismatched pin, so every Python update would silently fail.

## Consequences
- Dependency changes need `uv lock`, with both files committed.
- `ruff` and `uv` are each pinned in two places and must be bumped together.
- Since [0029](0029-deploy-the-image-ci-built.md), the image CI builds is the one deployed, and
  the base images are pinned by digest as well.
