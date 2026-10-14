# 0041. Check the accounts file against the categories file when a command starts

- Status: Accepted
- Date: 2026-10-14
- PR: #41

## Context

`accounts.yaml` and `categories.yaml` load separately, so neither model can check the category keys
the other names. An unknown `act_categories` key was only caught inside `triage._run_pass`, per
account and per `run`, after the `runs` row was stored: the run failed with `runs.error` and the
digest showed it, but `digest`, `unsubscribe-retry` and the other commands ran on. An unknown
`min_confidence` key was never caught: the category silently kept its default threshold. Several
settings also accepted values that only failed later or misbehaved (`LOG_LEVEL` typos failed in
`logging`, a negative `BODY_CHARS` cut the body from the end, thresholds above 1 or below 0 were
accepted).

## Decision

- `config.validate_accounts(accounts, cfg)` checks every account's `act_categories` and
  `min_confidence` keys against the categories and raises one `ConfigError` listing every unknown
  key of every account.
- Every CLI command that reads `accounts.yaml` (`run`, `digest`, `unsubscribe-retry`,
  `senders backfill`, `labels rename`) calls it once, on the whole file whatever `-a` names, before
  opening the database or Gmail, and exits 2 with that message. `scripts/dry_classify.py` calls it
  too, so `check_branch.sh` catches a mismatch before a deploy.
- The run-time check in `triage._run_pass` is removed: an unknown `act_categories` key there only
  matches no category, so that category is labeled and never acted on (fail closed), and a second
  check would only duplicate the first.
- `Settings`: `LOG_LEVEL` is one of the five `logging` level names (any case),
  `MAX_MESSAGES_PER_RUN` and `BODY_CHARS` at least 1, `MAX_UNSUBSCRIBES_PER_RUN` at least 0. Every
  threshold and probability in `categories.yaml` and an account's `min_confidence` values are
  between 0 and 1; `auto_rule.hits` is at least 1. No default changes.

## Alternatives considered

- Keep the per-run check and add `min_confidence` to it: a mismatch would still surface only in
  `run`, one account at a time, and other commands would run on a config nobody meant.
- Check only the accounts a command acts on (`-a`): the same file would pass or fail depending on
  the flag, and a typo in another account would wait for the next timer run.
- Keep the defensive check in `triage` as well: it can only fire for a caller that skips the CLI
  (tests), and its outcome without it is already the safe one.
- Validate in every command, including those that never read `accounts.yaml` (`rules`, `restore`,
  `stats`): it would add a failure mode (and make `auth add` need the file) to commands that don't
  use account settings.

## Consequences

- A mismatch stops the timers' `run` and `digest` alike: no `runs` row is stored and no digest is
  sent, so it shows up as failed units and their stderr, not in Slack (listed in
  [Known gaps](../how-it-decides.md#known-gaps)). `inbox-jev digest --dry-print` after editing
  either file shows it at once.
- The rollout order for a new category is unchanged: deploy the code first, then list the key.
