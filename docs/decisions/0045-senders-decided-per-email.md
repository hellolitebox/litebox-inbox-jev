# 0045. Senders decided per email, and website form leads are an action

- Status: Accepted
- Date: 2026-10-14
- PR: #54

## Context

In production on 2026-10-14 a HubSpot email, "You've got a new submission on the HubSpot Form
"Contact Form - … Website 2024"", from `noreply@notifications.hubspot.com`, was archived as
`notification`. A visitor was waiting for an answer. Two causes:

1. An active `auto` rule `noreply@notifications.hubspot.com → notification`, learned from three
   HubSpot FYIs (dashboards, "Closed Won", app-scope notices), decided it without asking Jev. The
   keep guards of [0013](0013-sender-rules-and-auto-rule-guards.md) didn't help: each FYI was a
   clean 0.95+ notification with no keep signal. The address sends both kinds of mail, and the FYIs
   came first.
2. Asked directly (`dry_classify.py --ignore-rules`), Jev said notification 0.96: nothing in the
   criteria said that a website form's submission is a lead someone expects an answer to.

Fixing it by hand would have made it worse: a correction is a sender rule, so moving the lead back
to the inbox teaches "all HubSpot mail is important" and keeps every dashboard in the inbox.

## Decision

- **`decide_each_email`**, per account in `accounts.yaml`: addresses or `@domain` entries
  (subdomains included), stripped and lowercased, and checked like `rules set`'s keys (the same
  `config.normalize_sender_key`); anything else fails the file at load. For a listed sender:
  - no `auto` rule is learned or counted;
  - a correction is recorded on the email (`corrected_category`), which it decides if it is
    processed again, but creates no sender rule; the digest still lists it under "Corrections
    learned", marked "(this email only)";
  - existing `auto` and `correction` rules are ignored when deciding, in `run`, `unsubscribe-retry`
    and `dry_classify.py` (`rules.SenderRules.deciding`). They stay in the DB and decide again if
    the sender leaves the list;
  - **manual** rules still decide. `rules set` is an explicit choice for every email of the sender,
    not an inference from some of them; someone who lists a sender and also sets a manual rule for
    it gets the rule.
  - **never unsubscribed except on a manual rule**: a new veto in `policy.unsubscribe_gate`
    (`Veto.DECIDED_EACH_EMAIL`, right after the allowlist), so `run` and `unsubscribe-retry` share
    it. Jev's `marketing` or `cold_outreach` answer, the email's own correction and a stored
    decision archive the email under that category but never unsubscribe it; `unsubscribe-retry`
    skips such a row (`retry: sender decided email by email, no manual rule`). A manual rule to an
    unsubscribing category still unsubscribes, through the usual sender checks. A listed sender's
    address also carries the mail that needs you, and its `List-Unsubscribe` (once per `List-Id`,
    else per sender domain) may turn off that whole stream: a HubSpot webinar promotion from
    `noreply@notifications.hubspot.com` could stop the website-form leads, irreversibly and with
    nothing to show it.
- Nothing is hardcoded: which platforms mix FYIs and mail that needs you depends on the account. The
  docs give examples (a CRM's notifications address, form builders such as Typeform, Webflow or
  Jotform).
- **Criteria**: a new submission from a form on my or my company's own website or landing page (a
  lead, contact, quote or demo request) relayed by a form or CRM tool is `action_required`, and is
  excluded from `notification`, which keeps those tools' dashboards, reports, pipeline updates and
  account notices. The `needs_reply` question lists it as a yes. Their promotions stay `marketing`.

## Alternatives considered

- **Learn from subjects or content instead of senders**: a rule keyed on a subject pattern would fit
  this case, but the rules engine keys on senders everywhere (corrections, unsubscribes, the
  allowlist); a second key is a much larger change for one class of sender.
- **Stricter auto-rule guards** (more hits, a higher confidence): the HubSpot FYIs already cleared
  0.95 three times; any threshold is beaten by a sender whose first emails happen to be FYIs.
- **No auto rules at all**: every email costs a Jev call and the sent-mail searches; most senders
  are uniform and the rules are right for them.
- **Let Jev's word unsubscribe a listed sender**, as for any other sender, and only document it:
  before listing, the auto rule to `notification` shielded such a sender, so listing it would newly
  expose it to unsubscribes whose blast radius (a whole notifications stream) is exactly what
  listing is meant to protect. Skipping a promotion's unsubscribe costs one archived email per
  promotion.
- **Ignore manual rules too**: would make `rules set` silently do nothing for a listed sender. A
  manual rule is what the user explicitly asked for.
- **Hardcode known multi-purpose platforms**: wrong for an open-source tool (the list depends on
  what each account uses) and can't be changed without a release.
- **Only the criteria change**: Jev would then answer correctly, but the auto rule would keep
  deciding without asking it, and a correction would still teach the wrong rule.

## Consequences

- A listed sender costs one Jev call (and the sent-mail search) per email, forever. Lists are meant
  to be short.
- A listed sender's FYIs are archived only when Jev is sure enough each time; one it isn't sure
  about goes to Review instead of following a rule.
- Rules learned before a sender was listed are inert but still shown by `rules list`; deleting them
  (`rules delete`) avoids surprises if the sender is later removed from the list.
- A listed sender's promotions are archived as `marketing` but never unsubscribed unless a manual
  rule says so; they keep arriving (and being archived). To stop them, unsubscribe by hand or set a
  manual rule for the address that sends them, when it isn't the one that carries the mail that
  needs you.
- `unsubscribe-retry` settles a listed sender's `failed` row decided by Jev as `skipped`; removing
  the sender from the list doesn't bring it back (as for an allowlisted sender).
- The criteria are longer, and they are sent with every classification: about 60 words.
