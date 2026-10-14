# 0002. Run the container as a configurable uid, not 1000

- Status: Accepted
- Date: 2026-10-11
- PR: #4

## Context

The image ran as uid 1000. On the production VPS uid 1000 is a real login user, who could then read
`secrets/` (Gmail OAuth tokens) and `data/` (the database).

## Decision

`docker-compose.yml` runs the `app` service as `${APP_UID:-1000}:${APP_GID:-1000}`. `.env.example`
sets `APP_UID`/`APP_GID` to 10100, and the README deploy steps check that no host user owns the uid
before `chown` and `chmod 700` of `data/` and `secrets/`.

## Alternatives considered

- Hard-code another uid in the image: could still collide on some host, and changes existing setups.
- Keep 1000 and rely on file modes: the host user with that uid *is* the owner.

## Consequences

- The default stays 1000, so existing setups don't change. An `.env` without the keys falls back to
  1000 and the app gets `PermissionError` on directories owned by the new uid.
- Every deploy must pick a uid that no host user has.
