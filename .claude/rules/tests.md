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
- Tests assert behaviour, not implementation. Coverage fails CI under `fail_under` in
  `pyproject.toml`: raise it when coverage grows, never lower it to get a PR through.
- `tests/test_spec_examples.py` holds one `test_exNN_…` per `EX-NN` in `docs/how-it-decides.md`,
  docstring citing it; `tests/test_safety_gates.py` runs the unsubscribe scenarios through every
  path (`.claude/rules/decision-logic.md`).
