# 0038. Surveys, reviews and promotional calls to action are not a personal action

- Status: Accepted
- Date: 2026-10-13
- PR: #38

## Context
"GUILLERMO, te elegimos para que nos des tu opinión", an OSDE satisfaction survey from
`noreply@osde.com.ar`, stayed in Review. Jev: marketing 0.85–0.87 (below its 0.90), archive
categories about 0.93 (above the group's 0.90), but `needs_reply` 0.88 and important +
action_required about 0.05, so the needs-reply veto on the archive group fired
([0008](0008-needs-reply-veto-with-keep-signal.md); as a no-reply email the known domain doesn't
count, [0032](0032-machine-addresses-make-no-known-contact.md)). The question "Does the sender
expect a reply or an action from me personally?" left "give us your opinion" open: a company
asking for feedback reads as a request addressed to me.

## Decision
Say in the question and the keep criteria what counts:
- `needs_reply.question` lists yes examples (a person writing to me, or something only I can do:
  pay, sign, approve, authorize, confirm an appointment, renew an expiring document, secure my
  account) and no examples, qualified as company or bulk mail so that a person's own request
  for feedback or invitation still counts as a person writing to me (surveys, feedback, ratings
  or reviews, promotions, webinars or events, even if it asks me to reply or click).
- `action_required` excludes surveys, feedback or review requests and promotional calls to
  action; `important` excludes mass mail that only greets me by name. Otherwise the keep
  probability alone (≥ 0.05) could still trip the veto.

The veto, its thresholds and the policy are unchanged.

## Alternatives considered
- Raise `archive_group_reply_keep_probability`: weakens the veto for every email, including
  real actions from no-reply senders (EX-25).
- A sender rule for OSDE: fixes one sender; every company survey has the same shape.
- A separate `survey` category: more labels and one more criterion on every call, for what
  marketing already covers.

## Consequences
- Company surveys and review requests are archived through the group (never unsubscribed below
  0.90) instead of sitting in Review with `Jev/Needs Reply`.
- The question is longer, and it is sent with every classification (Jev bills input): about 60
  words, kept to examples.
- The risk is a real action written like a promotion losing `Jev/Needs Reply`; the yes list
  names the actions that must keep it, and the PR's real-mail check found none flipped.
