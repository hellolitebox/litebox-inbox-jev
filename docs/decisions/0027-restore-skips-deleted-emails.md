# 0027. `restore` skips emails deleted in Gmail

- Status: Accepted
- Date: 2026-10-13
- PR: #22

## Context
`inbox-jev restore` modifies each row's email in Gmail, then marks the row `restored`. When an
email had been deleted in Gmail, the API answered 404 and the command stopped with an error.
That row was never marked restored, so every later `restore` over the same window stopped at the
same email: whatever came after it could not be undone without editing the DB. `restore` is the
undo for archive and spam, so it must not be blocked by an email the user already got rid of.
`senders backfill` and `unsubscribe-retry` already treat a 404 as "deleted" and carry on.

## Decision
- A 404 from Gmail skips that email: its row is left unchanged (not marked restored), a warning
  `restore: email deleted` is logged (account and id only), and the command reports
  `Skipped N deleted in Gmail.`
- Any other Gmail error (auth, quota, 5xx, network) still stops the command; emails restored
  before it stay restored, as before.
- The 404 test is one helper (`triage._not_found`) for `restore`, `backfill_senders` and
  `retry_unsubscribes`.

## Alternatives considered
- Mark the deleted email's row `restored`: nothing was undone in Gmail, and the flag would make
  `unsubscribe-retry` skip its failed unsubscribe for a reason the user never gave. The row stays
  as it is: `unsubscribe-retry` already holds rows whose email is gone.
- Skip every error: a token or quota problem would be reported as success.

## Consequences
- `restore` always gets through the window when emails were deleted, and running it again only
  skips them again.
