# 0047. Calendar messages never count toward an auto rule

- Status: Accepted
- Date: 2026-10-14
- PR: #55

## Context

An `auto` rule ([0013](0013-sender-rules-and-auto-rule-guards.md),
[0036](0036-auto-rule-hits-count-emails.md)) is learned from three different emails of one sender
that Jev answered in the same category, sure enough. Google Calendar and Outlook send the response
to an invitation ("Accepted: …", "Declined: …") **from the responder's own address**, and
[0046](0046-meetings-booked-with-me-are-an-action.md) makes a plain response `notification`, which
Jev answers at up to 1.00 on real mail. Three such responses from a client, with no kept email from
them in between, would turn on an `auto` rule to `notification` on that **person's** address: their
next real email would be archived without asking Jev. Invitations and cancellations a person sends
(`action_required` since 0046) would likewise teach a keep rule on them, which then decides their
mail without Jev's category. Either way, calendar traffic says nothing about the rest of a sender's
mail.

## Decision

- A **calendar message** is an email with a `text/calendar` part whose Content-Type names an iTIP
  method (RFC 5546, carried per iMIP, RFC 6047: `text/calendar; method=REPLY`): an invitation or
  update (`REQUEST`), a response (`REPLY`), a cancellation (`CANCEL`), a proposed new time
  (`COUNTER`), any method. It is read from the message structure in the `full` read the email
  already gets (`body.calendar_method`, `Email.calendar_method`), never from the Subject, and costs
  no extra Gmail call. The method is sender-controlled text, so only an RFC 5545 token (letters,
  digits, `-`, at most 32 characters) counts; any other value is no method, which leaves the email
  counting as before (the safe direction) and keeps control characters out of `dry_classify`'s
  output.
- Such an email **never counts toward an auto rule** (`rules._counts_toward_rule`): it adds no hit,
  creates no rule and doesn't switch or restart a pending rule's category, whatever Jev answers.
- The **keep reset still applies** to it: a keep decision or a likely keep category restarts a
  pending archiving `auto` rule, as for any email. A reset can only send more mail to Jev, never
  archive any, so it stays on the safe side.
- Its own decision is unchanged: Jev still decides it as before (0046), and a manual, correction or
  active auto rule still decides it.

## Alternatives considered

- **Detect responses by Subject** ("Accepted:", "Declined:"): localised, editable by the sender and
  shared by cold pitches titled "Invitation : …"; the iTIP part is what the calendar itself writes.
- **Exclude only responses (`REPLY`)**: an invitation from a person (`REQUEST`) would still teach an
  `action_required` rule on them, deciding their next real email without Jev's category; keeping
  their mail is harmless, but it is learned from mail that says nothing about their other mail.
- **Neither count nor reset**: symmetric, but a reset only sends more mail to Jev; dropping it would
  let a sender's calendar traffic hide a sign that its mail may need to stay.
- **A Known gap only**: production had no such rule on 2026-10-14, but an "Accepted:" is common
  enough that a client's three RSVPs in a quiet week would silently archive them.

## Consequences

- A person who answers your invitations is never learned as `notification` from those answers; a
  sender whose only mail is calendar traffic never earns an auto rule, so Jev keeps deciding it (one
  Jev call per email).
- An email whose calendar part lacks the method parameter, or that carries the response only as text
  (a forwarded RSVP, a tool that sends no `.ics`), still counts as before (spec, Known gaps).
- `dry_classify.py` shows `calendar=<METHOD>` in its flags, so a real-mail check can see the
  detection.
