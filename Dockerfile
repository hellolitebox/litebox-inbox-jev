# Pinned uv installer, as a named stage so Dependabot's docker ecosystem bumps the tag (it
# ignores images referenced only in COPY --from=). Keep it equal to the setup-uv `version` in
# .github/workflows/ci.yml; CI fails when they differ. Both images are pinned by digest too, so
# the same Dockerfile always builds from the same bytes; Dependabot bumps tag and digest together.
FROM ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 AS uv

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

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

# The commit the image was built from: `inbox-jev --version` prints it and scripts/deploy.sh
# checks it against the checkout. CI passes both args; compose passes GIT_SHA when it is set in
# the environment (scripts/deploy.sh --build sets it).
# Declared last so the layers above stay cached across commits.
ARG GIT_SHA=unknown
ARG IMAGE_SOURCE=""
ENV INBOX_JEV_REVISION=$GIT_SHA
LABEL org.opencontainers.image.revision=$GIT_SHA \
      org.opencontainers.image.source=$IMAGE_SOURCE

USER app
ENTRYPOINT ["inbox-jev"]
CMD ["--help"]
