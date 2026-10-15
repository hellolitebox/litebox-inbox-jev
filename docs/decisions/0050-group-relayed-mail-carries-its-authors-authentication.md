# 0050. Mail relayed by a trusted group carries its author's authentication, from Google's ARC record

- Status: Accepted (amends [0042](0042-phishing-signals-for-jev.md) and
  [0043](0043-authentication-results-parsed-per-rfc-8601.md))
- Date: 2026-10-15
- PR: #60

## Context

On 2026-10-15 a phishing email reached the inbox through the account's own Google Group
`hello@litebox.ai` (a `group_domains` group): `"'VMC Adobe' via Hello" <hello@litebox.ai>`,
"Document ID: New Pending Review", written by an unrelated company's address, a link to a marketing
click tracker and another company's signature. Jev answered spam 0.51, action_required 0.31: it
stayed in Review.

Jev's `sender_auth` (ADR 0042) and `dmarc_pass` are read from Gmail's own `Authentication-Results`,
the topmost one stamped `mx.google.com` (ADR 0022, 0043). A Google Group re-sends the author's
message from the group's address, so for relayed mail that header authenticates the group's copy:
SPF, DKIM and DMARC pass for `litebox.ai`. Jev got `original_sender` (the author) next to
`sender_auth` all `pass`, which reads as "the author's domain passed", or worse, as internal mail.

Where the author's verdicts are. Six recent relayed production emails (dumped read-only on
2026-10-15, bodies never read) all have the same layout:

- three ARC sets (RFC 8617), each an `ARC-Seal`, `ARC-Message-Signature` and
  `ARC-Authentication-Results`, all signed `d=google.com`: `i=1` (`cv=none`) and `i=2` (`cv=pass`),
  added by Google Groups when the group received the message, whose `ARC-Authentication-Results` are
  `mx.google.com`'s verdicts for the **author's** domain; and `i=3` (`cv=pass`), added by Gmail on
  delivery, a copy of its own `Authentication-Results` for the group;
- Gmail's own `Authentication-Results` with
  `arc=pass (i=2 spf=pass spfdomain=… dmarc=pass fromdomain=…)`: Gmail validated every seal, and
  summarised the author's verdicts in a comment;
- an `X-Original-Authentication-Results: mx.google.com; …` that Google Groups adds, with the
  author's verdicts.

A direct email has at most one ARC set (Gmail's delivery set, `i=1`) and no `arc=` verdict.

## Decision

For an email relayed by a **trusted** group (`resolve_relay` moved the `X-Original-Sender` author
into `sender`), Jev's `sender_auth` and the resolved `dmarc_pass` are the **author's**, read from
Google's ARC record (`signals.relay_author_auth`):

- The record read is the `ARC-Authentication-Results` at instance **N-1** of N: the one just below
  Gmail's delivery set, which is Google's record of the message the group received.
- It is trusted only when all of these hold, else the author's verdicts are **unknown**:
  - Gmail's own `Authentication-Results` (the one ADR 0043 trusts) has the verdict `arc=pass` (one
    resinfo, not a comment; two `arc` verdicts that disagree are none). Gmail checked every
    `ARC-Seal`'s signature; a sender can write ARC headers but can't sign for `google.com`, so a
    forged set fails the chain.
  - Each kind of ARC header parses (the seals' and signatures' tag-lists per RFC 6376 3.2, a tag
    named twice is invalid) and has exactly one header per instance, numbered 1 to N, N ≥ 3.
  - Every seal's `cv` is `none` at instance 1 and `pass` above it.
  - The seals at N, N-1 and N-2 are signed `d=google.com`, exactly.
  - The record's authserv-id is exactly `mx.google.com`, after a strict `i=N;` prefix, and it parses
    with ADR 0043's grammar.
  - The record is **bound to the author**: its DMARC resinfo names exactly one `header.from`, and it
    equals the domain of `X-Original-Sender`, compared exactly (lowercased, a final dot dropped).
    DMARC's `header.from` is the RFC 5322 `From` domain of the message Google received, which for
    the author's message is the author's own domain, and Google writes it inside the sealed record.
    The chain checks above prove the record is Google's; this one proves it is about the author's
    message and not, after one more Google hop (a nested group, a set Google adds), about the
    group's own copy, whose `header.from` is the group's domain. No `header.from`, or more than one,
    is unbound.
  - The record **agrees with Google Groups' second record**: the `ARC-Authentication-Results` at N-2
    parses with the same grammar to the same authserv-id, the same verdicts in the same order and
    the same DMARC `header.from`. Google Groups writes two identical records of the message it
    received, and nothing else in the layout does. The binding above can't tell the author's record
    from the group copy's when the author is on the **group's own domain** (both name it): with one
    extra Google set after the group, the group's pass would be read for a spoofed own-domain author
    (CEO fraud), the case where it matters most (QA round 2). That set differs from the author's
    record below it, so it is not read. The comparison is of what is parsed, not of the raw text: it
    covers everything that is read (comments, such as the `header.b` and SPF explanations, are never
    read, ADR 0043), and on real mail the raw texts are identical anyway (Consequences).
- With a trusted record, Jev gets its `spf`, `dkim` and `dmarc` verdicts, combined as ADR 0043 says
  (DKIM passes when any signature does; SPF and DMARC must agree). Without one, Jev gets all three
  as `none`: unknown, never the group's pass. Jev's instructions now say that for an email sent via
  a group, `sender_auth` is the original sender's, for their own domain.
- **`dmarc_pass`** keeps two meanings at two steps, each about the address it describes. In
  `signals.extract` it is still the `From` address's (the group's), and `original_sender` still
  trusts `X-Original-Sender` only on that pass (ADR 0012): unchanged, the group check never looks at
  the author. Once `resolve_relay` makes the author the sender, `dmarc_pass` becomes the author's
  (pass only with a trusted record whose DMARC passes), so nothing that later reads "the sender
  passed DMARC" can be given the group's pass. Relayed mail is never unsubscribed either way (the
  group check, condition 5 of the unsubscribe gate); for a relayed author that doesn't pass, the
  gate now stops at condition 4 (unverified) instead, so `unsubscribe_skipped_unverified` is set on
  its row. Nothing else reads that flag to decide.
- Untrusted groups and direct mail are unchanged: `sender_auth` is Gmail's own header, as before.

## Alternatives considered

- **Read the `arc=pass (…)` comment in Gmail's own header.** It is exactly the summary wanted, but
  it is a comment: free text, never read since ADR 0043, and it carries domains the sender chose
  (`spfdomain`). Rejected.
- **Read `X-Original-Authentication-Results`.** Google Groups adds it, but nothing shows Google
  strips a copy the sender wrote, and it is covered by no signature Gmail checks for us. Rejected.
- **Verify the ARC chain ourselves.** RSA signatures over canonicalised headers, DNS key lookups: a
  large, error-prone piece of code, where Gmail already did it and says so with `arc=pass` in the
  one header we trust. Rejected.
- **Trust N-1 without binding it to the author.** The first version of this PR. One more Google hop
  after the group puts Google's record of the group's own copy at N-1, with every chain check
  passing: the group's pass is then reported as the author's, the incident this ADR fixes. QA proved
  it on the branch (round 1). Rejected.
- **Compare N-1 and N-2 as raw text** (after taking off `i=N;` and collapsing whitespace) instead of
  as parsed. Equally strict on what is read, and true of every genuine relay seen; but it would also
  reject on a comment that differs, which says nothing about the verdicts. Parsing both with the one
  strict grammar keeps a single notion of what a record says. Rejected.
- **Bind on the organisational domain** (DMARC's relaxed alignment) instead of the exact domain.
  Google Groups copies the author's `From` address into `X-Original-Sender`, so the two domains are
  the same on genuine relayed mail, which the real-mail check on the branch confirms; the exact
  match is the stricter one and no looser one is needed. Rejected unless real mail shows otherwise.
- **Read instance 1, or the lowest Google-sealed instance.** A sender can seal its own ARC set (with
  its own domain's key, a valid chain) before sending to the group: the lowest instances are then
  the sender's. N-1 is always Google's when N and N-1 are sealed `d=google.com` and the chain
  passes. Rejected.
- **Leave `dmarc_pass` as the group's after resolving.** No outcome changes today (relayed mail is
  vetoed as a group anyway), but it leaves a resolved `Signals` saying the author passed DMARC
  because the group did, the very confusion of this incident, waiting for the next reader. Rejected.
- **Leave `sender_auth` out for relayed mail without a trusted record.** Jev would then read the
  absence as "no Authentication-Results", while all `none` says what we know: nothing about the
  author. Kept as all `none`.

## Consequences

- The incident's email itself: its author's domain passed SPF, DKIM and DMARC in Google's record, so
  Jev still sees three passes, now for the author's own domain and named as such. What changes is
  that a group pass no longer stands in for an author that fails or has no DMARC, and Jev is told
  whose verdicts they are; the mismatch between the claimed brand and the author's domain is what
  the phishing criteria (ADR 0042) weigh.
- An author whose verdicts are unknown (no ARC sets, a chain Gmail didn't pass, a layout Google
  changes) shows `none` everywhere: weaker evidence of legitimacy, the safe side for phishing. A
  genuine document notice relayed by a group without a record could lean more toward spam; the
  real-mail check on the branch measures it.
- The author's verdicts depend on Google keeping this layout (Google Groups' two identical records
  just below Gmail's own set). Real mail backs it: across the 64 most recent production emails with
  `X-Original-Sender` (checked read-only on 2026-10-15), 58 have N=3 and 4 have N=4 (a set the
  author's own provider sealed first), and in all 62 the records at N-1 and N-2 are identical once
  `i=N;` is taken off and whitespace collapsed; the other 2 have N=1 and are not full group relays
  (unknown, as before). If the layout changes, relayed mail falls back to unknown, never to the
  group's pass: a record at N-1 that isn't about the author's message names another `header.from`,
  or differs from the one at N-2, and is not read. So one extra Google set after the group is
  unknown for every author, the group's own domain included: the residual QA round 2 found is closed
  in code.
- What the two checks can't tell apart is a whole second Google Groups hop (a nested group) that
  adds its own pair of identical records of the inner group's copy: that pair names the inner
  group's domain, so it is read only for an author on that same domain, and only when the inner
  group accepted that author's message. A domain whose DMARC policy is `p=reject` keeps a spoofed
  own-domain author out at the inner group, so its copy's pass is then the author's own. Any other
  nested relay is unknown.
