---
paths:
  - "categories.yaml"
  - "src/inbox_jev/policy.py"
  - "src/inbox_jev/triage.py"
  - "src/inbox_jev/unsubscribe_retry.py"
  - "src/inbox_jev/jev_guard.py"
  - "src/inbox_jev/rules.py"
  - "src/inbox_jev/corrections.py"
  - "src/inbox_jev/signals.py"
  - "src/inbox_jev/psl.py"
  - "src/inbox_jev/public_suffix_list.dat"
  - "src/inbox_jev/unsubscribe.py"
  - "src/inbox_jev/classifier.py"
  - "src/inbox_jev/body.py"
  - "src/inbox_jev/gmail.py"
  - "src/inbox_jev/sent_scan.py"
  - "src/inbox_jev/scan_rows.py"
  - "src/inbox_jev/config.py"
  - "src/inbox_jev/cli.py"
  - "docs/how-it-decides.md"
  - "docs/guide.md"
  - "docs/decisions/**"
  - "tests/test_spec_examples.py"
  - "tests/test_safety_gates.py"
---

# Decision logic: spec, examples, ADR, real mail

[`docs/how-it-decides.md`](../../docs/how-it-decides.md) is the functional spec of how an email is
decided and [`docs/decisions/`](../../docs/decisions/README.md) holds the ADRs saying why. The files
this rule covers are the decision logic: `categories.yaml` (categories, actions, thresholds,
criteria, archive group, needs_reply, tags, auto_rule), `policy.py`, `triage.py` (pipeline,
`previous_contact`, user edits, the `run` unsubscribe path), `unsubscribe_retry.py`, `jev_guard.py`
(when a pass stops asking Jev, whose fault a refusal is), `rules.py`, `corrections.py`,
`signals.py`, `psl.py` and the vendored `public_suffix_list.dat`, `unsubscribe.py`, the classifier
state and questions (`classifier.py`), body extraction (`body.py`), the sent-mail search and its
cache (`Gmail.has_written_to`, `sent_scan.py`, `scan_rows.py`), `config.py` (`FREE_MAIL`,
`PUBLIC_GROUP_HOSTS`, `group_domains` normalisation and rejection, `decide_each_email` and
`Account.decides_each_email`, the `act_categories` and `archive_group_reply_keep_probability`
validators, the `Settings` defaults `MODE`, `LOOKBACK`, `MAX_MESSAGES_PER_RUN`,
`MAX_UNSUBSCRIBES_PER_RUN`, `BODY_CHARS`) and the `cli.py` commands that change decisions or stored
state (`restore`, `reprocess`, `rules set` / `rules delete`, `unsubscribe-retry`).

## Every change to it, in the same PR

- Update `docs/how-it-decides.md` (flowcharts, tables, numbers, "Known gaps") so it matches the
  code.
- Add or update the worked examples (`EX-NN`) it affects, and their tests in
  `tests/test_spec_examples.py` (`test_exNN_…`, docstring citing `EX-NN`). A **new** rule or guard
  that changes an outcome (what is labeled, archived, spammed, unsubscribed or learned) gets a
  **new** `EX-NN` showing it. `test_doc_examples_match_tests` fails when an ID exists on only one
  side, or when the doc's "**Test**:" name isn't the test's name.
- Add an ADR (`docs/decisions/NNNN-title.md`, plus the index) when the change is a design decision:
  a new rule or guard, a changed threshold or default, a trade-off between safety and coverage.
  Supersede an old ADR instead of rewriting it.
- Update [`docs/guide.md`](../../docs/guide.md), the plain-language guide for the inbox owner and
  colleagues who aren't engineers, when the change alters something it explains (labels and what
  happens to them, how sure Jev must be, the safety nets, how corrections are learned, the digest,
  what is sent to Jev or stored). Keep its style: plain words, no code identifiers, file paths,
  section numbers or `EX-NN` IDs.

A pure refactor says "no behaviour change" in the PR body instead, and the diff must bear it out. A
PSL-lane update of `public_suffix_list.dat` alone is exempt. QA rejects a PR that misses any of
this.

## Plan first

A change to decision logic starts with the ADR draft (Context, Decision, Alternatives considered),
agreed with the user before any code: the ADR is the plan, and writing the alternatives is where the
unknowns surface. Prototype the risky part first: run the real-mail check below on the branch early,
not once the PR is polished.

## What keeps decisions safe

- Anything keyed on the sender (rules, corrections, `previous_contact`, allowlist) uses
  `triage.signals_for()` (resolved signals), never `Email.extract_signals()` directly. Rules for
  trusted groups (`group_domains`) key on the author; untrusted groups stay keyed on the group
  address, never on the author they claim.
- Every path that can unsubscribe goes through the same checks and respects what the user decided
  since (sender rules, corrections, restore, star): unsubscribes are irreversible. The sender checks
  (category, allowlist, verified, group, known contact) are `policy.unsubscribe_gate` and the mode /
  `act_categories` check is `policy.acts_on`: call them, never re-implement them. Once-per-list,
  confidence and the run cap stay with the caller, and an attempt is stored with
  `triage.record_attempt`. `run` and `unsubscribe-retry` (`unsubscribe_retry.retry_unsubscribes`)
  both do; `tests/test_safety_gates.py` runs the same scenarios through every path, so a new path
  goes there.

## Real mail before merging

A change to classification (`categories.yaml` criteria, `classifier.py` state, body extraction) or
to a destructive action (archive, spam, unsubscribe, `unsubscribe-retry`) is checked against real
mail before merging, not only with unit tests: `scripts/check_branch.sh` on the server, read-only
(`docs/development.md` → "Checking a branch against real mail"). The PR's "Verification" gets the
per-email check on recent mail and, for a classification change, the `--reference --quiet` summary
with the branch's accuracy next to the stored baseline. Never post email bodies.
