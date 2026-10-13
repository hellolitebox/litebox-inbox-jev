# 0029. Deploy the image CI built

- Status: Accepted
- Date: 2026-10-13
- PR: #24

## Context
ADR 0016 made the dependencies reproducible (`uv.lock`), and CI builds the image and smoke-tests
it as a non-root uid. But the deployment ran `git pull && docker compose build` on the VPS: a
second build, at another time, from `python:3.12-slim` resolved by tag, so the image in
production was never the one CI had tested. The deploy itself was eight lines of bash pasted
over SSH, the one-off steps lived in free text, the version was fixed at `0.1.0` and nothing
recorded which commit was running. GitHub Actions were pinned by tag, which a maintainer can
move.

## Decision
- CI's `docker` job runs after the tests, builds the image once per commit, smoke-tests it
  (including that `inbox-jev --version` reports that commit) and, on `main`, pushes the same
  image to GHCR as `ghcr.io/<repo>:sha-<short>` and `:latest`. Each `main` push runs in its own
  concurrency group, so runs never queue against or cancel each other and every `main` commit
  whose tests pass has an image; when two overlap, `:latest` may briefly point at the older
  commit, which deploys never use (they pin `sha-<short>`).
- `docker-compose.yml` runs `${IMAGE}:${IMAGE_TAG}` and pulls a missing image rather than
  rebuilding it; `scripts/deploy.sh [<ref>]` stops the timer, waits for a running pass, backs
  up the DB, checks out the ref (detached; `origin/main` by default), pulls the image for that
  commit, writes `IMAGE_TAG` to `.env`, checks the image's reported commit against the checkout
  and starts the timer (`--hold` leaves it stopped for one-off steps; `--build` builds locally
  when the registry is unreachable; a failure after the stop leaves the timer stopped and says
  so). Rollback is `scripts/deploy.sh <previous sha>`: the same path, the older image.
- The image carries its commit (`INBOX_JEV_REVISION`, `org.opencontainers.image.revision`) and
  `inbox-jev --version` prints it without needing any configuration.
- The base image and the `uv` stage are pinned by digest as well as tag; GitHub Actions are
  pinned by commit SHA with the version as a comment. Dependabot updates all of them.

## Alternatives considered
- Keep building on the VPS, pin only the digest: the build is reproducible in bytes but still a
  second build, and the VPS keeps needing a build toolchain and the registry pull anyway.
- Release tags and a `CHANGELOG`: more ceremony than a one-maintainer tool needs today; the
  commit SHA in the image is the version, and `main` is the release branch.
- Multi-arch images: the VPS is `amd64`; `docker compose build` remains for anything else.

## Consequences
- The VPS needs read access to GHCR (`docker login ghcr.io` with a token that has
  `read:packages`); private packages count against the org's storage quota.
- A Dockerfile-only change still builds and smoke-tests in CI, and is what gets deployed, so a
  deploy-time surprise needs a CI surprise first.
- Dependabot PRs for the digest pins arrive weekly alongside the tag bumps.
