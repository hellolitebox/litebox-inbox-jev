---
paths:
  - "tests/**"
---

# Tests

- No network: use the fakes in `tests/fakes.py`, injected
  (`Context(…, gmail=…, classifier=…, unsubscriber=…)`), not monkeypatched into modules. The fakes
  are typed and checked by mypy against the protocols they stand in for.
- Open the DB with the `make_sessions` fixture: pytest turns `ResourceWarning` (and the
  unraisable-exception warning a finalizer's `ResourceWarning` becomes) into errors, so an engine,
  file or client left open fails the run.
- **Prove the test fails without the fix.** For a bug fix, or a test added for a QA finding about a
  missing test, run the new test against the code without the fix (stash it, or check out the parent
  commit) and see it fail. The PR body ("Verification") or the dev reply says so and how, e.g.
  "`test_x` fails on `main` with `AssertionError: …`". A test that passes without the fix proves
  nothing.
- **Every test protects something named** (ADR 0056): a decision rule (an `EX-NN`), a contract with
  another system (Gmail, Jev, Slack, a List-Unsubscribe endpoint), an incident that happened, or a
  risk the PR states. Tests assert behaviour, not implementation or incidental text; one that only
  re-runs a helper another test covers is removed when its code is touched.
- **Fakes model the other system's contract**: partial and paginated answers, errors inside a 2xx,
  timeouts, rate limits, an answer lost after a write, not only the happy path. Parsers and
  calculations get invariant tests (a pass's settled emails plus its unsettled ones are the emails
  it listed; no count is negative); each scheduled job (the `run` and `digest` timers, and the ops
  scripts the other timers run) has an end-to-end test per outcome that matters (result, exit code,
  what is changed in Gmail, what is posted, what alerts).
- **The coverage floor** (`fail_under` in `pyproject.toml`) catches forgotten tests; it is not
  raised by rule. Removing a redundant test may lower coverage: lower the floor in the same PR and
  say why. Lowering it to get untested code through is refused.
- `tests/test_spec_examples.py` holds one `test_exNN_…` per `EX-NN` in `docs/how-it-decides.md`,
  docstring citing it; `tests/test_safety_gates.py` runs the unsubscribe scenarios through every
  path (`.claude/rules/decision-logic.md`).
