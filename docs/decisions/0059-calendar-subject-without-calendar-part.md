# 0059. A calendar subject without a calendar part is a phishing signal for Jev

- Status: Accepted
- Date: 2026-10-27
- PR: #18

## Context

On 2026-10-23 `litebox@klmnro.com` sent "Accepted: 9564698766 x Tap Play @ Fri Oct 23, 2026 4pm -
4:45pm (EDT) (ggonzaleza@litebox.ai) 4:08:46 PM iMsr3G": a lookalike of a Google Calendar RSVP from
an unrelated domain, with a random suffix. Jev split it notification 0.48 / spam 0.48 and it sat in
Review. The tell is a mismatch Jev can't see: a real calendar response or invitation carries an iTIP
part (`text/calendar; method=REPLY|REQUEST|CANCEL`, `body.calendar_method`), and this one had none.

Since `calendar_method` has been stored (2026-10-15), 35 emails had a subject that starts like a
calendar message: 34 had the iTIP part (action_required 20, important 11, notification 3), and the
one without it is this phishing email. A search of the last year also turned up a real person's "Re:
Invitation from an unknown sender: …", a hand-written reply with no iTIP part, so a subject that
only contains a calendar phrase isn't enough.

`reply_without_thread` (a "Re:" subject with no earlier message) is the same kind of signal: a
mismatch between what the subject claims and what the message is, passed to Jev as a fact, never a
verdict on its own.

## Decision

Jev's state gets `calendar_subject_without_invite: true` when the Subject **starts** (after
whitespace, case-insensitive) with a calendar client's prefix and the email has no iTIP part:

- Google Calendar (en/es): `Invitation:`, `Updated invitation:`,
  `Invitation from an unknown sender:`, `Accepted:`, `Declined:`, `Tentatively accepted:`,
  `Canceled event:` / `Cancelled event:`, `Invitación:` (or `Invitacion:`),
  `Invitación actualizada:`, `Aceptado:` / `Aceptada:`, `Rechazado:` / `Rechazada:`,
  `Aceptado provisionalmente:`, `Evento cancelado:`;
- Outlook: `Accepted:`, `Declined:`, `Tentative:`, `Canceled:` / `Cancelled:`. Not `Updated:`: too
  generic for a subject prefix.

A subject that starts with `Re:`, `Fwd:` or any other prefix never matches. The flag is absent
otherwise, like `reply_without_thread`. The spam criteria in `categories.yaml` and Jev's
instructions name it next to `reply_without_thread`. It changes no threshold and decides nothing by
itself.

## Alternatives considered

- A rule that sends it to Spam: text the sender chooses would become a verdict, which the decision
  rules forbid; and a client that puts the invite in an attachment we fail to read would be spammed.
- Match the phrase anywhere in the subject: catches hand-written replies ("Re: Invitation from an
  unknown sender: …") and product mail ("Kite Bot accepted their PostHog invitation").
- Do nothing: the next lookalike from a fresh domain gets the same 50/50 split; a sender rule from
  the correction only covers `litebox@klmnro.com`.

## Consequences

- One more boolean in Jev's state (no body text, no new data leaves the server).
- A real calendar email whose iTIP part we can't parse would carry the flag and lean toward spam:
  still subject to spam's 0.90 threshold, and below it Review. The real-mail check runs the 34
  stored calendar emails to confirm none gets the flag.
- Outlook-style prefixes (`Accepted:`, `Declined:`, `Canceled:`) also start some transactional
  subjects ("Canceled: Your order #1234"). The flag is then a false hint, still subject to spam's
  0.90 threshold, and below it Review. None in this account's history.
- Code: `signals.calendar_subject_without_invite`, `classifier.build_state` and its instructions,
  the spam criteria in `categories.yaml`, the spec's state table and EX-52, the README and the
  guide's list of clues.
