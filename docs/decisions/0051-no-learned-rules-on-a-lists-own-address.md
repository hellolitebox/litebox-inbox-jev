# 0051. No learned rule decides mail sent as a mailing list's own address

- Status: Accepted (extends [0045](0045-senders-decided-per-email.md) to list addresses; amends
  [0012](0012-group-author-trust-opt-in.md) and [0013](0013-sender-rules-and-auto-rule-guards.md))
- Date: 2026-10-15
- PR: #62

## Context

Sender rules key on the resolved sender. Mail a Google Group relays from a domain in `group_domains`
resolves to its author ([0012](0012-group-author-trust-opt-in.md)), but only when
`signals.original_sender` accepts the relay: the group's own DMARC passes in Gmail's header, a
`Mailing-list` header names the `From` address and `X-Original-Sender` is a plain address. Whenever
that fails, the sender stays the group's own address (e.g. the user's group `hello@litebox.ai`):

- Gmail's `Authentication-Results` doesn't parse, which fails closed
  ([0043](0043-authentication-results-parsed-per-rfc-8601.md),
  [0050](0050-group-relayed-mail-carries-its-authors-authentication.md));
- no `X-Original-Sender`, or mail the group sends itself;
- any group outside `group_domains` that rewrites `From` to its own address (Gmail's "via"), and
  other list servers that do the same (Mailman's DMARC mitigation).

Then one correction to `Jev/Spam`, or three consistent confident Jev answers, made a rule on the
group's address, and that rule decided **everything** the group relays without asking Jev: one
phishing fix could send all of `hello@`'s mail to Spam. Found in PR #61's review; #61 documented it
as a Known gap and an operator procedure (`rules delete` the rule).

`group_domains` can't be the guard: it covers the whole domain, people included, and production has
correct auto rules on colleagues on that domain (`…@litebox.ai → important`).

## Decision

- **A list's own address** is read from the email's own headers (`signals.list_addresses`): the
  `mailto:` addresses of RFC 2369 `List-Post` (Google Groups, Mailman, groups.io, LISTSERV send it)
  and the address of Google Groups' `Mailing-list: list <address>`, lowercased, plain addresses
  only. An email is **sent as the list** (`Signals.sender_is_list`) when its resolved sender equals
  one of them: exact equality, nothing inferred. A trusted relay resolved to its author is not.
- Such an email is decided on its own, as for a sender in `decide_each_email`
  ([0045](0045-senders-decided-per-email.md); one helper for both, `rules.decided_per_email`):
  - no `auto` rule is learned or counted;
  - a correction is stored on the email (`corrected_category`) and decides it if it is processed
    again, but creates no sender rule; the digest marks it "(this email only)";
  - an existing `auto` or `correction` rule on the address is ignored when deciding it, in `run`,
    `unsubscribe-retry` and `dry_classify.py`. It stays in the DB and still decides mail from that
    address that isn't sent as the list (no list headers);
  - a **manual** rule still decides: `rules set` is the user's explicit choice for everything the
    address sends. `rules set` on an address stored as a list's own prints a note on stderr that the
    rule covers everything the list sends as itself, and stores it anyway.
- **Any list, not only Google Groups**: a newsletter or announcement list whose `From` is its own
  `List-Post` address gets the same treatment. A list posts many authors' mail under one address:
  even an announce list's `From` says nothing about who wrote the next issue. An ESP newsletter (no
  `List-Post`, no `Mailing-list`) keeps learning rules: its `From` is one publisher.
- The flag is stored per email (`messages.sender_is_list`, nullable), because `corrections.check`
  reads only stored rows. `senders backfill` and every pass over an email refresh it.
- **Unsubscribe**: unchanged in effect. Mail with `List-Post` was already a group for the
  unsubscribe check (condition 5); an email sent as the list that only Google Groups' `Mailing-list`
  names is now one too (`policy._group_list`).

## Alternatives considered

- **No rules on `group_domains` addresses**: would stop rules on colleagues on the same domain,
  which are right and in production.
- **A config list of group addresses** (like `decide_each_email`): exact, but the operator must know
  and maintain every group, and it does nothing for groups outside the account's domains. The
  headers already say which address is the list's.
- **List-Id mapped to an address** (`<hello.acme.com>` → `hello@acme.com`): ambiguous (where does
  the local part end?) and unnecessary, since Google Groups always sends `Mailing-list` and lists
  that accept posts send `List-Post`.
- **`Sender:` equal to `From`**: ESPs set `Sender` to bounce or on-behalf addresses; it doesn't
  identify a list.
- **Trust the list headers only with DMARC** (as `original_sender` does for the author): the guard
  only ever makes Jev decide instead of a learned rule, so a forged header can't make the tool more
  destructive. See Consequences for what it can do.
- **Refuse `rules set` on such an address**: a manual rule is explicit, and is the only way to
  decide all of a list's mail at once (e.g. archive an external community). A note is enough.

## Consequences

- One Jev call (and the sent-mail search) per email sent as a list, forever, and no learned shortcut
  for it: an announce list that always sends the same kind of mail needs a manual rule to skip Jev.
- A spammer can add `List-Post: <mailto:its own From>` to keep a correction rule from deciding its
  next emails. That is no more than changing its address, which already escapes the rule (Known
  gaps); a manual `@domain` rule still applies.
- Rules learned on list addresses before this change stay in the DB, inert for mail sent as the
  list; `rules list` shows them and `rules delete` removes them.
- Rows stored before the column existed are read as not sent as the list, so a correction on one of
  them (within the 7-day window) can still create a rule; that rule is ignored for the list's next
  emails anyway.
