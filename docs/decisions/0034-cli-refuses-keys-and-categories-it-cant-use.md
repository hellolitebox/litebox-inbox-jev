# 0034. The CLI refuses rule keys and categories it can't use

- Status: Accepted
- Date: 2026-10-13
- PR: #35

## Context

`inbox-jev rules set` stored any SENDER, lowercased: `example.com` (no `@`) or
`Ana <ana@example.com>` became a manual rule that no sender ever matches (`policy.rule_keys` looks
up the exact address, then `@domain`), so the user thought a sender was handled while Jev kept
deciding. `restore --category` took any text: a typo restored nothing and said "Restored 0 emails",
which reads like there was nothing to undo. `reprocess` checked its category deep in
`triage.mark_for_reprocess` and turned any `ValueError` into a usage error.

## Decision

- `rules set` takes an address (`signals.ADDRESS`) or `@` plus a domain (`signals.DOMAIN`, IDN TLDs
  in punycode included), lowercased; anything else exits 2. Its CATEGORY must be in
  `categories.yaml`, as before.
- `reprocess --category` must be in `categories.yaml` (checked before the DB is touched).
- `restore --category` must be in `categories.yaml` **or** carried by a stored email of that
  account; else it exits 2 before Gmail is touched.

## Alternatives considered

- **`restore --category` only from `categories.yaml`**: a category removed since (its emails
  archived under it) could then no longer be undone by category, and `restore` is the safety net;
  the stored rows are what it restores anyway.
- **Also refuse `@<free mail>` rules** (`@gmail.com`): a manual rule is an explicit decision and may
  be intended; left as is.

## Consequences

- Malformed input fails loudly instead of being stored or silently matching nothing.
- `rules delete` keeps accepting any text: a key that was never stored just says "no such rule", and
  rules stored before this check can still be deleted.
