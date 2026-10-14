---
paths:
  - "**/*.py"
  - "tests/**"
  - "pyproject.toml"
  - "uv.lock"
  - "Dockerfile"
  - ".github/workflows/**"
  - "categories.yaml"
  - "accounts.example.yaml"
  - ".env.example"
  - "docker-compose.yml"
  - "deploy/**"
---

# Python skills, by path

Before writing or judging code, load with the Skill tool the skills this table selects for the paths
you change, and apply them. A change that touches none of them (docs, ADRs, README, agents, skills,
workflows without Python) loads none. The repo's own conventions (CLAUDE.md, the rules in this
directory, the ruff config) win over a skill's generic advice, and never ask for tooling beyond what
CI runs (ruff, mypy, pytest): no structlog, tenacity, Celery… unless the change is about it. The PR
body's `Skills applied:` line names the skills loaded (or `none: docs-only`) and the concrete
changes they drove; if a skill isn't available, say so there and continue. `qa-reviewer` and
`dev-fixer` read this file every round: it is the one list. Whoever implements or reviews needs the
Skill tool: `qa-reviewer` declares it in `tools:`, `dev-fixer` inherits every tool, and a generic
agent spawned to do either must be given it.

| Skill | Load when the change touches | Use it for |
| -- | -- | -- |
| `python-anti-patterns` | any `.py` under `src/` or `scripts/` | the anti-pattern checklist, on every changed function |
| `python-design-patterns` | any `.py` | KISS, separation of concerns, duplication vs. abstraction, coupling |
| `python-project-structure` | any `.py` | module boundaries and public API (`policy` pure, I/O in `gmail`/`triage` and the modules split from them) |
| `python-type-safety` | any `.py` | names, enums vs. loose strings, `Any` where a type exists (mypy covers annotations and `None`) |
| `python-error-handling` | any `.py` | exception scope, partial failures in a run, errors that hide or abort |
| `python-resilience` | `gmail.py`, `body.py`, `sent_scan.py`, `classifier.py`, `unsubscribe.py`, `triage.py`, `jev_guard.py`, `scan_rows.py`, `unsubscribe_retry.py`, `digest.py` | retries, backoff, timeouts on Gmail/Jev/HTTP calls |
| `python-resource-management` | those I/O modules, `db.py` | sessions, HTTP clients, files: context managers and cleanup |
| `python-observability` | those I/O modules, `logs.py`, `cli.py` | structured logs (and never email bodies), what a failure leaves behind |
| `python-configuration` | `config.py`, `categories.yaml`, `accounts.example.yaml`, `.env.example`, `docker-compose.yml` | `Settings`/YAML validation, secrets, defaults |
| `python-testing-patterns` | `tests/` | test quality: behaviour over implementation, fixtures, fakes, no network |
| `python-performance-optimization` | `triage.py`, `jev_guard.py`, `scan_rows.py`, `unsubscribe_retry.py`, `gmail.py`, `body.py`, `sent_scan.py`, `db.py`, `rules.py`, `corrections.py` | API calls and DB queries per email/run, N+1, unbounded loops |
| `python-packaging` | `pyproject.toml`, `uv.lock`, `Dockerfile`, `.github/workflows/` | `pyproject.toml`, `uv.lock`, entry points, the Docker build |
| `python-background-jobs` | `triage.py`, `jev_guard.py`, `scan_rows.py`, `unsubscribe_retry.py`, `cli.py`, `deploy/` | the timer-driven runs: idempotency, overlap, retries |
| `async-python-patterns` | async code is added (the codebase is sync today) | event loop, blocking calls, cancellation |
| `temporal-python-testing` | Temporal is introduced | otherwise "n/a" |

`python-code-style` left this table on 2026-10-14 (ADR 0044): across the merged PRs its concrete
changes were docstrings, which the surrounding modules already show, and naming that ruff's `N` and
`FBT` rules now enforce.
