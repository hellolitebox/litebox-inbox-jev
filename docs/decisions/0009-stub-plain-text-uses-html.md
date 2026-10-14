# 0009. Use the HTML when the `text/plain` part is a stub

- Status: Accepted
- Date: 2026-10-13
- PR: #10

## Context

An event email sent through emBlue had a plain part saying only "your mail client doesn't support
HTML, copy and paste this URL" plus a ~250-character tracking URL. Jev classified it blind
(cold_outreach 0.51 / spam 0.26 / marketing 0.17 → Review). An Adobe email's plain part was just
`undefined`.

## Decision

Inside one `multipart/alternative`, replace the plain text with its HTML sibling's stripped text
when the plain text is under 300 characters and the HTML has at least 3× as much (and at least 60),
with URLs excluded from both counts. Plain text elsewhere in the MIME tree is never dropped.
Whitespace normalisation was also made linear on long whitespace runs.

## Alternatives considered

- Always prefer the HTML: noisier input, and drops the sender's own note above a forwarded HTML
  message.
- Compare all plain and HTML parts in the tree: the first version did; QA round 1 (finding 1) showed
  it dropped a forwarding note.
- No minimum HTML length: a URL-only plain part would be replaced by an empty or one-word HTML (QA
  round 1, finding 2).

## Consequences

- The emBlue email became newsletter 0.91 and was archived on its own confidence.
- Only emails classified after deploy are affected, unless they are reprocessed.
