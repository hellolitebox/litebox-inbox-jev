# 0053. Installable from PyPI, with a home folder and a schedule on the user's computer

- Status: Accepted (extends [0052](0052-each-install-brings-its-own-oauth-client.md))
- Date: 2026-10-15
- PR: #64

## Context

[0052](0052-each-install-brings-its-own-oauth-client.md) made setup a guided `inbox-jev init`, but
it still started from a clone of the repo and `uv sync`, and ended pointing at a Docker deployment
on a server to run it every 10 minutes. For an open-source tool, the common case is one person on
their own computer, who expects `pipx install`/`uv tool install` and a command that works from any
directory. Three things tied the code to a checkout:

- every path (`.env`, `accounts.yaml`, `categories.yaml`, `secrets/`, the SQLite file) was relative
  to the current directory;
- the default `categories.yaml` and the `*.example` files lived only at the repo root, outside the
  package;
- scheduling existed only as the server's systemd units around `docker compose run`.

## Decision

- **Published on PyPI as `inbox-jev`**, by a tag-triggered `Release` workflow with PyPI's trusted
  publishing (no stored token), which checks the tag against `pyproject.toml`, runs the checks and
  smoke-tests the wheel installed outside the checkout before publishing. A `pypi` GitHub
  environment gates the publish.
- **A home folder** (`paths.home`): `INBOX_JEV_HOME` when set; else the current directory when it is
  an inbox-jev deployment or checkout (it has `accounts.yaml`, or a `pyproject.toml` whose
  `[project].name` is `inbox-jev`); else `~/.config/inbox-jev` (`$XDG_CONFIG_HOME/inbox-jev`). Only
  markers of inbox-jev count: most project directories have a `.env`, and adopting another project's
  would read its `DATABASE_URL`, make `init` write inbox-jev's secrets into it and open a second
  database next to the scheduled one, where the same mail is acted on twice. `Settings.load` reads
  the home's `.env` and takes every relative path, and a relative SQLite file, from the home. The
  image sets `INBOX_JEV_HOME=/app`, so a deployment behaves as before; a checkout too.
- **Defaults shipped in the wheel** (`force-include` into `inbox_jev/defaults/`, read by
  `paths.bundled`; a checkout reads the repo's own files). An install without its own
  `categories.yaml` uses the shipped one, so upgrades bring the new defaults; a `categories.yaml` in
  the home, or a `CATEGORIES_FILE` naming another file, replaces it. `init` starts `.env` from the
  shipped `.env.example`.
- **A schedule on the user's computer**: `inbox-jev schedule install|status|remove` (and an optional
  `init` step, off by default) writes a LaunchAgent on macOS or systemd user units on Linux: triage
  every 10 minutes, the digest daily at 08:00 when Slack is set up, both with `INBOX_JEV_HOME` set.
  Neither scheduler starts a job while its previous run is going, so passes don't overlap.
  `schedule.install` itself refuses on a host with the server's
  `/etc/systemd/system/inbox-jev-triage.timer`, so neither the command nor `init` can install around
  it: two copies would triage the same mail. On Linux, user timers run only while the user is logged
  in unless lingering is on (`loginctl enable-linger`); `install` warns when it is off.

## Alternatives considered

- **npm** (the first idea): the tool is Python, so an npm package would only wrap a Python install
  and add Node as a second runtime. PyPI with `uv tool`/`pipx` gives the same one-line install.
- **Homebrew**: a formula for a Python app with this many dependencies needs a resource block per
  dependency and its own release cadence; it can come later on top of the PyPI package.
- **A single binary** (PyInstaller, shiv): one artifact per OS and architecture to build and sign,
  for a tool whose users run Python already (or get it through `uv`).
- **Docker only**: right for a server, too heavy for one person's laptop, and `init` needs a browser
  for the Google authorization anyway.
- **Always `~/.config/inbox-jev`**, ignoring the current directory: breaks every checkout and
  deployment without `INBOX_JEV_HOME`, including the image's `scripts/check_branch.sh` runs.
- **Any `.env` or `.env.example` marks the home**: simpler, but most projects have a `.env`, so an
  installed copy run from one would adopt it (above).
- **`os.chdir` to the home at startup**: fewer lines, but it would also move relative paths the user
  types (the client JSON in `init`) and hide where files are read from.
- **Cron** for scheduling: it skips a job missed while the computer sleeps (launchd's calendar jobs
  and systemd's `Persistent=` timers run it on wake, which matters for the daily digest), and each
  line needs its environment spelled out; it remains the suggestion where neither launchd nor
  systemd exists.

## Consequences

- `uv tool install inbox-jev && inbox-jev init` sets up a laptop end to end;
  `inbox-jev schedule install` makes it run.
- A command with no accounts file says to run `inbox-jev init` (exit 2) instead of a traceback.
- On macOS the jobs' output goes to `<home>/logs/triage.log` and `digest.log`, which grow by a few
  lines per pass and aren't rotated; systemd user units log to the journal.
- The first release makes the source public (the sdist and the wheel carry it), whatever the repo's
  visibility. It needs the one-time PyPI and GitHub setup in `docs/development.md`, "Releasing".
