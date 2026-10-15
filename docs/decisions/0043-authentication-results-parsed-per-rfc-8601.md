# 0043. Authentication-Results is parsed per RFC 8601

- Status: Accepted (`arc` verdicts read, and ARC-Authentication-Results parsed with this grammar:
  amended by [0050](0050-group-relayed-mail-carries-its-authors-authentication.md))
- Date: 2026-10-14
- PR: #50

## Context

`dmarc_passed` (ADR 0022) and `sender_auth` (ADR 0042) read Gmail's trusted `Authentication-Results`
with a regular expression, `dmarc=(\w+)` or `(spf|dkim|dmarc)=(\w+)`, matched anywhere in the
header. Part of that header is chosen by the sender: Gmail names the envelope sender in the SPF
comment (`(google.com: domain of <envelope sender> designates …)`) and in `smtp.mailfrom=`, and a
local part may contain `=`. With an envelope sender of `dmarc=pass@attacker.example` on a domain
without a DMARC record (the 2026-10-14 incident's shape), the header has no DMARC verdict of
Gmail's, yet `dmarc_passed` returned true and `sender_auth` said `dmarc: pass`; `dkim=pass@…` turned
a real `dkim=fail` into a pass the same way. DMARC gates every unsubscribe (ADR 0010) and the Google
Groups author recovery (ADR 0012), and the spam criterion clears a document notice whose sender
passes DMARC. The trusted header was also recognised by `startswith("mx.google.com")`, which accepts
`mx.google.com.attacker.example`.

## Decision

Parse the header as RFC 8601 2.2 defines it, in one place (`signals._parse_auth_results`) that both
`dmarc_passed` and `sender_auth` use, as an **allowlist**: the whole header must match the RFC's
grammar, and anything else fails closed. Three rounds of QA each found a new way for sender-chosen
text to add a resinfo (the local part, a quoted local part, then an RFC 5321 address literal such as
`x@[t:;dmarc=pass;]` in `smtp.mailfrom` or `smtp.helo`), so patching each case is not enough.

1. **Opaque pieces.** Comments (nested, with `\` pairs), quoted strings (with `\` pairs) and domain
   literals (`[...]`, whose dtext and dcontent exclude `[`, `]` and `\` but include `;`, `(`, `)`
   and `"`) are each read as one piece, wherever they are, **inside a comment too**: Gmail writes
   the envelope sender raw in its SPF comment, so a `"` or `[` there starts a piece a `)` can't end.
   Comments become whitespace, quoted strings `""` and literals `[]`.
2. **Strict grammar.** What remains must be `authserv-id [version] ( *(";" resinfo) / ";" "none" )`,
   each resinfo `method[/version]=result [reason=value] *(ptype.property=pvalue)`, and each pvalue a
   value, an address `[local-part]@domain` or a literal, where a quoted string or literal may stand
   for the local part or the domain. A value is any run without whitespace, `;`, `@`, `(`, `)`, `"`,
   `\`, `[` or `]`: wider than RFC 2045's token, because every real Gmail header has a base64
   `header.b` (`/`, `+`, `=`) and envelope local parts hold `=` and `+` (VERP, SRS). It can never
   hold a `;`, so it can't end a resinfo.
3. **Vocabulary.** An SPF, DKIM or DMARC result must be an RFC 8601 result word (`pass`, `fail`,
   `softfail`, `hardfail`, `neutral`, `none`, `temperror`, `permerror`, `policy`). Other methods
   (`arc`, Gmail's `dara`) must parse but are ignored.
4. **Fail closed.** A header with anything outside 1 to 3 (an unbalanced comment, quote or literal,
   a stray `]` or a `\` outside a piece, a property without a value, a stray word, an empty resinfo,
   `dmarc=passx`) has no verdicts at all, not even those before the break. `dmarc_passed` is then
   false and `sender_auth` reports every method as `none` (EX-42). The topmost `mx.google.com`
   header stays the one trusted, malformed or not, so a malformed Gmail header never lets a later
   one be read.
5. **One verdict per method.** Gmail writes one SPF and one DMARC resinfo; two that disagree are not
   trusted (`none` for that method). DKIM may have several (one per signature) and passes when any
   does.

Only the `method=result` that starts each resinfo is a verdict; properties (`smtp.mailfrom=`,
`header.from=`, `header.i=`) are never read. The trusted header is the topmost one whose authserv-id
is exactly `mx.google.com`.

A generated test (`test_no_sender_chosen_text_forges_a_verdict`) puts about 41,000 sender-chosen
values in Gmail-shaped headers: RFC 5321 local parts (quoted, with `(`, `)`, `;`, `=`, `\` pairs,
`dmarc=pass`), envelope domains and HELO names that are IPv4, IPv6 or general address literals
(`[t:` + `(`, `)`, `;`, `=`, `"`, `dmarc=pass` + `]`), DKIM `header.i` with stray quotes,
parentheses and backslashes, and From domains. The verdicts are always Gmail's real ones or none at
all, never a forged pass and never a word the sender chose. The shapes of the topmost Gmail header
on 400 recent production emails all parse (`test_real_gmail_header_shapes_parse`).

## Alternatives considered

- Keep the regex and anchor it after `;`: still matches inside a quoted local part, a literal or a
  comment holding `;`.
- Read verdicts after dropping comments and quoted strings, and fail closed only on unbalanced text
  (this PR's first two rounds): a denylist of what can break the structure. Each round found another
  construct (a quoted local part in a comment, an address literal) that it didn't know.
- A strict RFC 2045 token for values: rejects every real Gmail header (`header.b` is base64 with `/`
  and `=`).

## Consequences

- Production behaviour changes only for forged headers: Gmail's real verdicts start their resinfos,
  so every genuine `dmarc=pass` still passes. A sender whose envelope address or comment imitates a
  verdict is no longer verified (archived only, never unsubscribed, EX-40), never has its group
  author trusted, and Jev sees its real verdicts.
- A Gmail header that doesn't parse verifies nothing: the sender is archived only, never
  unsubscribed, and Jev sees `none` for every method. Every shape Gmail wrote on 400 recent
  production emails parses, so this only happens when a sender bent it, or if Gmail starts writing
  something new (a new result word for SPF, DKIM or DMARC, a trailing `;`): then those emails lose
  their verdicts (unverified, never unsubscribed) until the grammar is widened. That is the safe
  direction.
- The parse relies on Gmail writing the envelope sender as SMTP accepted it (an unquoted local part
  has none of `;`, `(`, `)`, `"`, `\`, `[` or `]`, a quoted one keeps its quotes, a literal its
  brackets); see Known gaps in the spec. A raw `;` in `header.from` (no RFC 5322 domain holds one)
  meets Gmail's real DMARC verdict, so it can't forge a DMARC pass: at worst a real DMARC `fail`
  reads as `none`. It can still add a `dkim=pass` (DKIM passes when any signature passes) or turn
  SPF into `none` (two SPF verdicts that disagree). Neither verifies a sender, and any sender can
  get a real DKIM pass by signing with its own domain.
- A header without a trusted `mx.google.com` authserv-id, lookalikes included, is not read (as ADR
  0022 already intended).
