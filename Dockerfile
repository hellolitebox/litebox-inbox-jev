# Pinned uv installer, as a named stage so Dependabot's docker ecosystem bumps the tag (it
# ignores images referenced only in COPY --from=). Keep it equal to the setup-uv `version` in
# .github/workflows/ci.yml; CI fails when they differ.
FROM ghcr.io/astral-sh/uv:0.13.0 AS uv

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1 \
    PATH=/app/.venv/bin:$PATH
WORKDIR /app

# uv from the pinned stage above; dependency versions come from uv.lock, so two builds of the
# same commit install exactly the same packages.
COPY --from=uv /uv /uvx /bin/

# Dependencies first: this layer is reused until pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the code (non-editable, like a normal install).
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable && useradd --uid 1000 --create-home app
COPY categories.yaml ./

USER app
ENTRYPOINT ["inbox-jev"]
CMD ["--help"]
