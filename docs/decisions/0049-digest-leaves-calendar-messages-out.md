# 0049. The digest leaves calendar messages out of its lists

- Status: Accepted
- Date: 2026-10-15
- PR: #59

## Context

Since [0046](0046-meetings-booked-with-me-are-an-action.md), a calendar invitation, update or
cancellation a person sends is `action_required`: it stays in the inbox with `Jev/Action`, which the
inbox owner wants. The daily digest lists every pending `Jev/Action` email and every email waiting
for a reply, so each invitation ("Invitation: Vemos Lightspeed @ Thu Oct 15…", "Updated invitation:
Intradiem - Litebox @ Fri Oct 16…", one per update) also shows up there. The owner asked to keep
them in the inbox but not see them in the digest: the calendar already shows them, and they crowd
out the emails that need an answer.

## Decision

- Each email's row stores the iTIP method of its calendar part (`messages.calendar_method`, from
  `Email.calendar_method`, the same detection as
  [0047](0047-calendar-messages-teach-no-auto-rule.md): any method, never the Subject).
- The digest **never lists a calendar message**: not under a kept-on-purpose section (`Jev/Action`),
  Needs reply, nor the Low-confidence count, and it doesn't read their threads for the pending
  check.
- They still **count in the summary line**: the processed total and the per-category and "to review"
  counts are totals of what the pass did, so they keep adding up.
- A failed calendar message is still listed under Errors: a failure needs the owner whatever the
  email is.
- Rows stored before the column existed have no method and are listed as before. The column is
  nullable, so `_add_missing_columns` adds it to a deployed database at startup with no backfill;
  within a day (the digest's window) every listed row has it.
- Nothing about the decision changes: the label, keeping it in the inbox, and the auto-rule rule of
  0047 are as before.

## Alternatives considered

- **Classify invitations as `notification` (archive)**: removes them from the digest but also from
  the inbox, which the owner explicitly wants to keep (0046's reason: it waits on an RSVP).
- **A separate, collapsed "Calendar (n)" line**: still noise for something the calendar shows; the
  owner asked for them not to appear. Easy to add later from the stored column.
- **Detect invitations in the digest by Subject** ("Invitation:", "Updated invitation:"): no schema
  change, but localised, editable by the sender and shared by cold pitches; the stored iTIP method
  is what the calendar itself writes.
- **Leave them out of the summary counts too**: the totals would no longer add up to the processed
  count, and the summary is about what the pass did, not what needs the owner.

## Consequences

- The digest is shorter on days with meetings, and an invitation that needs an RSVP is only seen in
  the inbox and the calendar, not in Slack.
- A response that proposes a new time or asks something (`action_required` under 0046) is a calendar
  message too, so it is also left out of the digest; it stays in the inbox with `Jev/Action`.
- `calendar_method` is set by any `text/calendar` part with a method, not only by notices a calendar
  generates: a person's email that attaches an invitation (an `.ics` with `method=REQUEST`) and also
  asks something in its text ("invite attached; can you send the contract first?") is left out of
  Needs reply too. It stays in the inbox with its label; we accept this because such emails are rare
  next to the daily invitation noise.
- One nullable column more on `messages`; a rollback to an older version ignores it.
