# 0046. A meeting someone booked with me through a scheduling link is an action

- Status: Accepted
- Date: 2026-10-14
- PR: #55

## Context

Scheduling tools tell you when someone books time with you through your link or booking page:
HubSpot Meetings ("You've been booked by: <name> <email>"), Calendly ("New Event: …"), Cal.com,
Chili Piper, Google Calendar appointment schedules. Someone will be waiting for you at that time;
you prepare and attend, and you may want to reply. Jev called these `notification`: some at
0.41–0.57 landed in Review, and some were already archived in production (Calendly bookings at 0.68
through the archive group's sum, and at 0.87 by Jev alone; HubSpot's "booked by" notices through an
`auto` rule to `notification` that was deleted after #54). Nothing in the criteria told them apart
from the same tools' FYIs: `notification` named "Google Calendar" and `transactional` named
"bookings", and both read as covering them. Invitations a person sends from their own calendar had
no stated home either: on real mail Jev split them between `action_required` and a low-confidence
`notification`.

[0045](0045-senders-decided-per-email.md) made a website form's lead `action_required` for the same
reason (someone is waiting for you), next to which this wording sits.

## Decision

- **`action_required`** includes a meeting that someone booked, rescheduled or cancelled with me
  through a scheduling link or booking page, naming the tools above as examples. A cancellation or a
  reschedule by the other person changes your day as much as the booking, so it is kept too.
- **`notification`** keeps calendar and scheduling tools' daily agendas, reminders of meetings
  already in my calendar ("starts in 10 minutes") and account notices, and excludes the bookings;
  `action_required`'s exclusions name reminders of meetings already in my calendar and daily
  agendas. **`transactional`** says "confirmations of bookings and reservations made by me or for me
  (travel, hotels, restaurants, an appointment with a provider)", and not a meeting someone booked
  with me through my scheduling link, so a booking someone else made with me isn't read as my own
  reservation and a trip a colleague booked for me still is.
- **Calendar invitations from people** are `action_required` too: an invitation, update or
  cancellation a person sent me from their own calendar (Google Calendar, Outlook) waits on my RSVP
  or changes my day. `notification` excludes them and keeps only the calendar's digests and
  reminders. They get no `Jev/Needs Reply` either: the RSVP is given in the calendar, not by email
  reply, unless the invitation itself asks a personal question.
- **Responses to invitations I sent** stay `notification`: a plain "Accepted: …", "Declined: …",
  "Tentative: …" or "<name> has accepted this invitation" (Google Calendar, Outlook) tells me who is
  coming and leaves nothing to do, so it is archived. `notification` names them and
  `action_required` excludes them, because the first wording ("a calendar invitation … from their
  own calendar") pulled an "Accepted: …" response into `action_required` on real mail. The exception
  is a response that **proposes a new time** (it waits on me to accept or reject the proposal, like
  an update) or **carries a note asking me something** (a person waits on my answer): both are
  `action_required`. A plain decline is an FYI: the calendar already shows it, and if it frees my
  slot I don't need to act.
- **`needs_reply`: no.** The booking is already confirmed by the tool; nobody waits for an answer,
  only for you to attend. The question lists such a notice under "No", so it isn't mistaken for
  "confirm an appointment" (a yes, for a request that does wait on you). The email still stays in
  the inbox through its `keep` category, and its own digest section shows it.
- Criteria only: no code change, no hardcoded sender.

## Alternatives considered

- **`important`**: the notice is automated, written by a tool, not a person; `important` is "written
  by a real person specifically to me", and stretching it would blur the allowlist's target.
- **`needs_reply` yes**: would add `Jev/Needs Reply` to every booking, a label meant for mail that
  waits on you; the booking doesn't. A person who books and also writes a question in the booking
  form still gets `Jev/Action`, and Jev can still say yes when the notice carries a personal
  question. The same holds for a calendar invitation: the RSVP is a click in the calendar.
- **Calendar invitations from people as `notification`**: an unanswered invitation would be
  archived, and the organizer would wait for an RSVP you never saw. As `important` instead: the
  email is written by the calendar, not by the person, and the allowlist targets `important`.
- **Keep every response to my invitations as `action_required`**: most are an accepted or declined
  click with nothing to do, and keeping them would fill the inbox with every meeting I organize.
  **Archive every response, including proposed new times**: the other person would wait on an answer
  to the proposal that I never saw.
- **Keep cancellations and reschedules as `notification`**: a slot freed or moved by the other
  person is something you need to know before the original time; archiving it would leave you
  waiting for someone who isn't coming.
- **Also keep reminders of meetings already in the calendar**: the calendar already shows them; the
  reminder adds nothing, and keeping them would fill the inbox every day.
- **A sender rule per scheduling tool**: tools mix bookings with FYIs on one address (HubSpot), and
  addresses differ per account; [0045](0045-senders-decided-per-email.md) rejected hardcoding for
  the same reasons.

## Consequences

- Bookings, reschedules and cancellations, and people's calendar invitations and their updates, stay
  in the inbox as `Jev/Action`, without `Jev/Needs Reply`. Plain responses to my own invitations are
  archived as `Jev/Notifications`; a proposed new time or a response with a question stays.
- A scheduling tool whose address also sends FYIs can still earn an `auto` rule to `notification`
  that archives its bookings without asking Jev; list such an address in `decide_each_email` (spec,
  Known gaps). This already happened with HubSpot, whose rule was deleted after #54 and whose
  address is now in `decide_each_email`. On 2026-10-14 no Calendly, Cal.com or Google Calendar
  sender had a rule; Calendly's bookings at 0.95 or more would more likely earn one to
  `action_required`.
- About 300 more words of criteria and needs-reply question (625 words at the merge base, 922 now,
  about +47%), sent with every classification.
- Plain responses to my invitations come from the responder's own address, so confident
  `notification` answers on them could teach an `auto` rule that archives that person's mail:
  [0047](0047-calendar-messages-teach-no-auto-rule.md) keeps calendar messages out of auto rules.
