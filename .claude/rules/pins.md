---
paths:
  - "pyproject.toml"
  - "uv.lock"
  - "Dockerfile"
  - ".github/workflows/ci.yml"
  - ".github/workflows/release.yml"
  - ".pre-commit-config.yaml"
  - ".mdformat.toml"
---

# Pinned twice: bump both halves together

- Dependencies are locked in `uv.lock`: after changing `pyproject.toml`, run `uv lock` and commit
  both (CI runs `uv lock --check`; the image installs with `uv sync --frozen`). Dependabot opens
  weekly update PRs, which take the Dependabot lane in `/ship`.
- `ruff`: the `dev` extra of `pyproject.toml` and `rev` in `.pre-commit-config.yaml` (Dependabot
  only updates `pyproject.toml`).
- `mdformat` with its plugins `mdformat-gfm` and `mdformat-frontmatter`: the `dev` extra, and the
  mdformat hook's `rev` and `additional_dependencies`. Its settings are in `.mdformat.toml` (prose
  wrapped at 100 columns, consecutive list numbers, unpadded tables).
- `uv`: the `ghcr.io/astral-sh/uv` stage tag in the `Dockerfile`, and `UV_VERSION` in `ci.yml`
  (every job's `setup-uv`) and in `release.yml` (the build that publishes to PyPI). Dependabot only
  updates the Dockerfile tag; CI's `test` job fails when it differs from `ci.yml`'s, and
  `release.yml`'s isn't checked, so bump it by hand with the other two (`/ship`, "Dependabot lane").
  Don't add `[tool.uv] required-version`: Dependabot's bundled uv would refuse to lock and no
  dependency PRs would be opened.
- Python: the base image (`python:3.x-slim`) must equal `PYTHON_VERSION` in `ci.yml` (the `docker`
  job checks it) and in `release.yml` (unchecked: bump it by hand), and `requires-python` must equal
  `MIN_PYTHON_VERSION` (the `test-min-python` job runs the tests on it and checks the two match).
- Base images are pinned by tag **and** digest, and GitHub Actions by commit SHA with the version as
  a comment; Dependabot bumps both halves, so keep that shape when editing by hand.
- The `docker` job builds the image, smoke-tests it as a non-root uid and on `main` pushes it to
  GHCR: that image is what `scripts/deploy.sh` deploys (ADR 0029).
- `[tool.coverage.report] fail_under`: raise it when coverage grows, never lower it to get a PR
  through.
