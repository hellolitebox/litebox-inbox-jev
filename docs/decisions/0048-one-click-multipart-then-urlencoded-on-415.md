# 0048. One-click sends multipart, and urlencoded once after a 415

- Status: Accepted
- Date: 2026-10-15
- PR: #57

## Context

RFC 8058 section 3.2: the one-click `POST` body `List-Unsubscribe=One-Click` "SHOULD be sent as
'multipart/form-data' [RFC7578] or MAY be sent as 'application/x-www-form-urlencoded'". `one_click`
sent only the urlencoded form. On 2026-10-14 a sender's endpoint (a Fastify server) answered
`415 Unsupported Media Type` to it, so the unsubscribe failed, and the row's `error` stored the
server's raw JSON body, which the Slack digest then showed. A failed one-click was never sent again
in the same attempt: an answer may come from a server that acted on the request, and sending it
twice risks a double action on a URL the sender controls.

## Decision

- The body is sent as **`multipart/form-data`** (one plain field, `List-Unsubscribe` = `One-Click`),
  the encoding the RFC prefers and the one browsers send for its example form.
- An answer of **415** gets the same request **once** more, to the same address, as
  `application/x-www-form-urlencoded`, with the same `Host`, `User-Agent`, SNI pinning, timeout and
  no redirects; its answer (or error) is the result. A 415 is the server saying it refused the
  body's media type before handling it, so nothing was unsubscribed and the retry can't act twice.
  No other answer or error is retried: a 400 can't tell "can't parse" from "bad token", and a 5xx or
  a read timeout may come after the server acted. A connection that fails before sending still moves
  to the next address, as before ([0018](0018-one-click-pinned-to-validated-ip.md)).
- A refused `POST` stores **`HTTP <status> <reason>`** (the standard reason phrase, not the
  server's), plus the server's own message when its body is a JSON object with a `message`, `error`
  or `detail` string: one line of printable ASCII, braces and links (words with `://`) removed, at
  most 120 characters, read only from a body of at most 4 KB. Describing the answer never raises: a
  body it can't read, or a bug, leaves `HTTP <status> <reason>`, because the request was sent and
  must be recorded, or the next run would `POST` again. The raw body is never stored. The digest
  shows `HTTP <status> <reason>` for a row whose error doesn't start with it (rows stored before,
  which hold the raw body), still escaped for Slack.

## Alternatives considered

- **Urlencoded first, multipart after a 415**: keeps today's request for endpoints that work, but
  every endpoint like the incident's pays a refused request first, and the RFC says SHOULD
  multipart.
- **Multipart only**: an endpoint written against the RFC's MAY (urlencoded only) would start
  failing; the 415 fallback costs one request only where it is needed.
- **Retry on 400 as well**: some frameworks answer 400 to a body they can't parse, but so does a
  server rejecting a bad or expired token after looking it up; a second `POST` there is noise at
  best. It stays a failure (spec, Known gaps).
- **Keep the body, truncated**: the digest escapes it, but it is sender text of any shape (a whole
  HTML page) in our database and Slack; the status says what happened, and a JSON message field is
  the only part meant to be read.
- **Rewrite stored rows**: a migration for one display concern; the digest's fallback covers them,
  and a retry overwrites the error anyway.

## Consequences

- Most one-click endpoints now receive multipart; one that parses only urlencoded receives two
  requests per attempt (a 415, then the one that works), logged as
  `one-click multipart refused (415), retried urlencoded`.
- A failed row's error is short and readable in the digest; an old row's raw body is no longer shown
  there.
