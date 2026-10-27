# How inbox-jev decides

> **New here?** Read the [plain-language guide](guide.md) first (what gets unsubscribed, in plain
> words:
> [What it unsubscribes you from](guide.md#what-it-unsubscribes-you-from-and-what-it-never-does)).
> This document is the technical spec it simplifies.

This is the functional spec of the triage logic: what happens to one email, which signals are
checked, and why. Everything here describes the code on `main` (`src/inbox_jev/`) and the shipped
[`categories.yaml`](../categories.yaml). Numbers are the shipped defaults; where a setting can
change them, the setting is named. The categories file itself is a default too: a deployment can
point `CATEGORIES_FILE` at its own copy with other criteria, thresholds or categories
([Your own categories](operations.md#your-own-categories)), and the logic below applies to whatever
that file says.

- The design decisions behind these rules, with the alternatives that were rejected, are in
  [`docs/decisions/`](decisions/README.md).
- Every worked example below (`EX-NN`) has a test with the same ID in
  [`tests/test_spec_examples.py`](../tests/test_spec_examples.py) that runs the real code. A test
  fails if an ID exists on only one side.
- **Changing decision logic?** Update this document, the matching examples and tests, and add an ADR
  when it is a design decision, in the same PR (the rule in `.claude/rules/decision-logic.md`). When
  the change alters something the [guide](guide.md) explains, update the guide too.

Contents:

1. [The pass over an inbox](#1-the-pass-over-an-inbox)
2. [The per-email pipeline](#2-the-per-email-pipeline)
3. [Decision tables](#3-decision-tables)
4. [What it checks and how](#4-what-it-checks-and-how)
5. [Worked examples](#5-worked-examples)
6. [Glossary](#6-glossary)

## 1. The pass over an inbox

`inbox-jev run` (`triage.run_account`) handles one account:

1. First, the command checks the whole `accounts.yaml` against `categories.yaml`
   (`config.validate_accounts`): an `act_categories` or `min_confidence` key, of any account, that
   isn't a category stops `run` with exit 2 and one message listing them all, before the database or
   Gmail is opened, so no `runs` row is stored (and `digest` fails the same way; see Known gaps).
   The **mode** is `--mode`, else the account's `mode`, else `MODE` from `.env` (default `dry_run`).
   A `runs` row is then stored before anything else, so every failure below ends up in `runs.error`
   (shown in the digest) and the run still gets its `finished_at`. A Gmail token that can't be
   refreshed fails the account's run before anything is touched, with one of three `runs.error`
   texts, depending on how google-auth reports the failure:

   - `invalid_grant` (token expired or revoked): run `inbox-jev auth add EMAIL`, and if it happens
     every 7 days, publish the Google app first: it is in Testing
     ([operations](operations.md#gmail-authorization));
   - a failure google-auth marks retryable (a 5xx or `temporarily_unavailable` with a JSON error
     body): "temporary", it will retry next run;
   - any other refresh failure (e.g. a 5xx whose body isn't JSON, such as an HTML error page, which
     google-auth marks non-retryable): "not invalid_grant", it will retry next run, and run
     `inbox-jev auth add EMAIL` if it persists.

   In all three the run fails and the next one tries again, and the token endpoint's response is
   never stored (only the `invalid_grant` summary line is). A missing (or malformed)
   `TYPESAFE_API_KEY` also fails the run before Gmail is touched, with `runs.error`
   "TYPESAFE_API_KEY is not set: add it to .env; nothing was processed", even when every email would
   be decided by a rule ([ADR 0031](decisions/0031-run-needs-the-jev-key-accounts-isolated.md)).
   Commands that never ask Jev (`digest`, `rules`, `stats`, `restore`, `reprocess`, `retry-errors`,
   `unsubscribe-retry`, `labels`, `senders backfill`, `--version`) don't need it.

2. In `label` and `act`, the Jev labels are created in Gmail if missing.

3. In `label` and `act`, **corrections** are checked first (`corrections.check`, see
   [3.7](#37-corrections-user-edits-and-restore)).

4. Gmail lists inbox message ids with `newer_than:<LOOKBACK>` (default `2d`, `run --lookback`
   overrides it), at most `MAX_MESSAGES_PER_RUN` (default 200, `run --limit` overrides it).

5. A listed email is **skipped** when its stored row is `restored`, or is `done` in the current mode
   or a stronger one. So an email seen in `dry_run` is processed again by `label` or `act`, and one
   seen in `label` is processed again by `act`. Rows with `status = error` are retried while they
   are still listed, with a backoff: after the n-th failure the email waits 5 × 2<sup>n−1</sup>
   minutes (5, 10, 20 … 320). After **8** failures (`db.MAX_ERROR_ATTEMPTS`, about 10.5 hours after
   the first) it is no longer retried: it stays in the inbox untouched, with `status = error`, and
   the digest lists it (see [EX-13](#ex-13--an-email-that-keeps-failing)). A success resets the
   count. `inbox-jev retry-errors -a EMAIL` resets it for every `error` row of the account, so the
   next run retries those still inside its `--lookback`.

6. Each remaining email goes through the [pipeline](#2-the-per-email-pipeline). An exception rolls
   back that email, stores it as `status = error` (counting one failure) and the run continues. The
   error row keeps the email's From, Subject, sender, date and thread whenever they were read, so
   the digest names it. When reading the email itself failed, one metadata-only read
   (`gmail.get_headers`: From, Subject and the sender headers, no body, no retries) fills them in,
   unless the row already has them from an earlier pass or the failed read was a 404 (Gmail no
   longer has the email: that read would fail too); if that read fails too, the row is stored
   without them (logged `failed email: metadata unavailable`, error type only) and the digest shows
   "(no subject)" with the Gmail link. A Gmail token refresh that fails mid-run (permanent or
   transient) is the exception: it fails the whole run instead and isn't counted against the email.
   A failed Jev call (after the SDK's own retries of timeouts, connection errors, 408, 429 and 5xx)
   is sorted by `classifier.failure_kind`
   ([ADR 0030](decisions/0030-stop-asking-jev-when-it-is-down.md)):

   - **config** (401, 403, 404: key revoked, no access, unknown model): every call would fail the
     same way. The pass stops asking Jev at once, and the email that hit it is left as it was (not
     stored, no failure counted).
   - **rejected** (any other 4xx but 408 and 429, e.g. 400, 422): Jev refuses the request. The email
     is held, not stored yet, until the pass's next Jev call (flags-only calls count; an email
     decided by a rule makes none) says whose fault it is
     ([ADR 0035](decisions/0035-identical-refusals-stop-the-pass.md)):
     - the next call is refused **the same way** (same SDK error type and HTTP status,
       `jev_guard.MAX_IDENTICAL_REJECTIONS` = 2 in a row): Jev may refuse everything, e.g. a change
       on its side. The pass stops asking Jev, and neither email gives up: each is stored as `error`
       with one **ordinary** failure, retried after the usual backoff like a timeout
       ([EX-28](#ex-28--jev-refuses-every-request-the-same-way)). The emails after them are left as
       they were. Two emails Jev really refuses (e.g. one message delivered to two aliases) stop
       only the passes where both are due again, so the emails behind them are decided in between,
       and they give up after 8 failures
       ([EX-30](#ex-30--two-emails-jev-refuses-dont-hold-up-the-rest));
     - anything else (an answer, a refusal with another status, another failure), or no other call
       before the pass ends: it was this email. It is stored as `error` and gives up at once
       (`error_attempts` = 8), since retrying sends the same request; `retry-errors` makes the next
       run try it again ([EX-23](#ex-23--jev-refuses-one-email)).
   - **transient** (anything else): the email is stored as `error` with the usual backoff.

   After **3** failed Jev calls in a row (`jev_guard.MAX_CONSECUTIVE_JEV_FAILURES`; flags-only calls
   count, a successful call resets the count) the pass also stops asking Jev. Once stopped, for the
   rest of the pass: an email that needs Jev's decision is left as it is (no sent-mail search, not
   stored, no failure counted) for the next run; an allowlisted email or one with a keep rule is
   decided without the flags, as when its flags-only call fails; other rule decisions go on as
   usual. The run then fails, logs `stopped asking Jev` and stores one of three `runs.error` texts:
   "Jev rejected the API key or model (…): check TYPESAFE_API_KEY and TYPESAFE_MODEL; …", "Jev
   refused 2 requests in a row the same way (…): likely a change on Jev's side; emails that need Jev
   wait for the next run" or "Jev unavailable: 3 calls in a row failed (last: …); emails that need
   Jev wait for the next run" ([EX-21](#ex-21--jevs-api-key-is-revoked),
   [EX-22](#ex-22--jev-is-down-for-a-whole-pass),
   [EX-28](#ex-28--jev-refuses-every-request-the-same-way)). `inbox-jev run` exits 1 when any
   account's run failed; the other accounts still run.

   A Jev error is stored (`messages.error`, `runs.error`) and logged as its type, HTTP status and
   request id only (`classifier.describe_jev_error`): the SDK's own message comes from the response
   body, which on a 400/422 can quote the request, i.e. the email. The only SDK texts kept are a
   connection error's (redacted, no response exists) and the SDK's own client-side messages from an
   allowlist (e.g. `Choice question "category" has no criteria…`), which name a question at most. A
   failed call is handled outside the `except` that caught it, so an error raised while handling it
   (e.g. a locked database) never carries it in its traceback. The `typesafe_sdk` logger is never
   set below INFO, since its DEBUG lines are the raw requests.

7. One unsubscribe budget is shared by the whole run: `MAX_UNSUBSCRIBES_PER_RUN` (default 20)
   attempts.

## 2. The per-email pipeline

`triage.process`, one email at a time:

```mermaid
flowchart TD
    A["Fetch the full message (gmail.get)<br/>headers, body (extract_body), snippet"] --> B["Signals from headers (signals.extract)<br/>then resolve_relay with group_domains"]
    B --> C{"Row seen in an earlier label/act pass<br/>and mode is not dry_run?"}
    C -- yes --> D{"Changed Jev category label<br/>or pulled back to INBOX?"}
    D -- yes --> D2["Record correction<br/>(sender rule, source correction;<br/>on the email only for a sender in decide_each_email<br/>or an email sent as a list's own address)"]
    D -- no --> E
    D2 --> E{"User edit?<br/>starred, our label removed,<br/>another Jev label added"}
    E -- yes --> STOP["Hands off: mark done, change nothing in Gmail"]
    E -- no --> F
    C -- no --> F["Look up sender rule: the first active one decides (exact address, then @domain);<br/>if its category was removed from categories.yaml, no rule (never the @domain one behind it);<br/>a sender in decide_each_email, or an email sent as a list's own address:<br/>manual rules only, and its own correction decides the email"]
    F --> G{"Allowlisted?<br/>author in allow_senders/allow_domains,<br/>or group address in allow_senders"}
    G -- yes --> FL["Ask Jev for the flags only<br/>(Needs Reply, tags; no previous_contact);<br/>if the call fails, decide without them (warning)"]
    G -- no --> H{"Active sender rule?"}
    H -- no --> I["previous_contact (sent-mail search)<br/>then ask Jev (build_state);<br/>once the pass stopped asking Jev: left for the next run"]
    H -- yes --> HK{"Rule to a keep category?"}
    HK -- yes --> FL
    HK -- no --> J{"Rule would unsubscribe<br/>a category that spares known contacts?"}
    J -- yes --> K["previous_contact (sent-mail search)"]
    J -- no --> DEC
    I --> DEC
    K --> DEC
    FL --> DEC
    DEC["policy.decide<br/>allowlist / rule / threshold / archive group / Review<br/>+ Needs Reply and tag labels"] --> PL["policy.plan<br/>mode + act_categories"]
    PL --> AP["Gmail modify: add labels, remove INBOX (archive/spam),<br/>add SPAM, drop stale labels from an earlier pass"]
    AP --> DB["Store the row (category, confidence, probabilities, labels...)"]
    DB --> AR{"Jev decided (not allowlist or rule),<br/>mode is not dry_run, sender<br/>not in decide_each_email and<br/>not sent as a list's own address?"}
    AR -- yes --> AR2["rules.record_auto<br/>(count toward / reset an auto rule;<br/>each email counts once)"]
    AR -- no --> U
    AR2 --> U{"plan.unsubscribe and run budget left?"}
    U -- yes --> U2["Unsubscribe once per list<br/>(see the unsubscribe flow)"]
    U -- no --> LOG
    U2 --> LOG["Commit and log 'message processed'<br/>(incl. previous_contact, archive_group,<br/>timings_ms per stage)"]
```

The unsubscribe decision, across `policy.decide`, `policy.unsubscribe_gate`, `policy.plan`
(`policy.acts_on`), `triage._unsubscribe` and `unsubscribe.unsubscribe`:

```mermaid
flowchart TD
    S["Final category"] --> A{"action is archive_unsubscribe?<br/>(marketing, cold_outreach)"}
    A -- no --> N["No unsubscribe"]
    A -- yes --> B{"Decided by a rule, or Jev confidence<br/>at or above the category threshold?<br/>(not archive group, not Review)"}
    B -- no --> N
    B -- yes --> L{"Sender in decide_each_email<br/>and not decided by a manual rule?"}
    L -- yes --> N
    L -- no --> C{"Verified: DMARC pass<br/>and a List-Unsubscribe header?"}
    C -- no --> N1["Archive only<br/>(unsubscribe_skipped_unverified)"]
    C -- yes --> D{"Group or discussion list?<br/>List-Post, Google Groups target,<br/>relayed by a group, sent as a list,<br/>own-domain sender or list"}
    D -- yes --> N
    D -- no --> E{"Category spares known contacts<br/>and previous_contact is<br/>wrote_to_sender / wrote_to_their_domain?"}
    E -- yes --> N2["Archive only (known_contact_kept)"]
    E -- no --> F{"Mode act and category<br/>in act_categories (if set)?"}
    F -- no --> N
    F -- yes --> G{"Run budget left and the list<br/>not already settled?"}
    G -- no --> N
    G -- yes --> H{"One-click header and an https URL<br/>passing the public-host guard?"}
    H -- yes --> P["POST List-Unsubscribe=One-Click<br/>(multipart; once urlencoded after a 415)"]
    P -- "2xx/3xx" --> OK["ok"]
    P -- "failed, mailto present" --> M
    P -- "failed, no mailto" --> FL["failed (retried up to 3 attempts)"]
    H -- no --> M{"A mailto: target?"}
    M -- yes --> MS["Send the unsubscribe email from Gmail<br/>(first mailto: with an @)"]
    MS -- sent --> OK
    MS -- "send error" --> FL
    M -- "no, or none has an @" --> MP["manual_pending (listed in the digest)"]
```

L, C, D and E are one pure function, `policy.unsubscribe_gate`, which every path that can
unsubscribe calls: the Jev path and the rule path of `run` (through `policy._check_unsubscribe`,
only on a decision that would otherwise unsubscribe), the rule path's sent-mail search
(`triage._rule_needs_contact`) and `unsubscribe-retry`. It also refuses a category that doesn't
unsubscribe and an allowlisted sender (on `run` the allowlist has already decided `important`). F is
`policy.acts_on`, the one mode / `act_categories` check, used by `policy.plan`,
`triage._rule_needs_contact` and `unsubscribe-retry`. The list's state (G) is I/O and stays in
`triage` ([ADR 0026](decisions/0026-one-unsubscribe-gate-for-every-path.md)).

The gate reports the first check that refused (`Veto`), and `_check_unsubscribe` sets a flag only
for that one. So `unsubscribe_skipped_unverified` means "an unsubscribe was skipped because the
sender is unverified" and `known_contact_kept` means "an unsubscribe was skipped because of a known
contact": an unverified or group sender never gets `known_contact_kept`, and an archive-group or
Review decision never gets `unsubscribe_skipped_unverified`
([ADR 0023](decisions/0023-one-order-for-unsubscribe-checks.md)).

## 3. Decision tables

### 3.1 Categories

From `categories.yaml` (or the deployment's own file, `CATEGORIES_FILE`); the table shows the
shipped defaults, and brand names in the criteria are examples for Jev, which no code matches on. An
account can raise or lower any threshold with `min_confidence: {category: value}` in `accounts.yaml`
(a category key, a value between 0 and 1; every threshold and probability in `categories.yaml` is
between 0 and 1 too, or the file fails to load).

| Category | Label | `action` | `min_confidence` | Notes |
| -- | -- | -- | -- | -- |
| `important` | `Jev/Important` | keep | 0.0 | Allowlist target. Also the fallback for an answer naming an unknown category. Not mass mail that only greets you by name, nor a conversation between other people pasted into a message from an unrelated sender. |
| `action_required` | `Jev/Action` | keep | 0.0 | Own digest section, which leaves calendar messages out (they stay in the inbox, labeled; [ADR 0049](decisions/0049-digest-leaves-calendar-messages-out.md)). Includes a held message pending your approval, and a document, tax return or agreement to review or sign through a document, e-signature or client-portal service ([EX-39](#ex-39--a-tax-portal-notice-that-passes-dmarc)), and a new submission from a form on my own website or landing page (a lead, contact, quote or demo request relayed by a form or CRM tool such as HubSpot, Typeform, Webflow, Jotform or Google Forms: someone waits for a reply; [EX-44](#ex-44--a-sender-decided-per-email-keeps-its-leads-and-archives-its-fyis), [ADR 0045](decisions/0045-senders-decided-per-email.md)), and a meeting someone booked, rescheduled or cancelled with you through a scheduling link or booking page (HubSpot Meetings "You've been booked by", Calendly, Cal.com, Chili Piper, Google Calendar appointment schedules: someone expects you at that time; [EX-46](#ex-46--a-meeting-booked-through-a-scheduling-link-stays-in-the-inbox), [ADR 0046](decisions/0046-meetings-booked-with-me-are-an-action.md)), and a calendar invitation, update or cancellation a person sent you from their own calendar (Google Calendar, Outlook: it waits on your RSVP or changes your day; same EX-46 and ADR), and a response to an invitation you sent that proposes a new time or carries a note asking you something. Excludes surveys, feedback or review requests and promotional calls to action ([ADR 0038](decisions/0038-surveys-are-not-a-personal-action.md)), reminders of meetings already in your calendar and daily agendas, and a plain response to an invitation you sent (Accepted, Declined, Tentative). |
| `transactional` | `Jev/Transactional` | archive | 0.80 | Includes copies of documents you already signed or filed, and confirmations of bookings and reservations made by you or for you (travel, hotels, restaurants, an appointment with a provider); not a meeting someone booked with you through your scheduling link, which is `action_required`. |
| `notification` | `Jev/Notifications` | archive | 0.80 | Includes groups' held-spam reports, and form and CRM tools' dashboards, reports, deal or pipeline updates and account notices; not a new submission from a form on my own website (that is `action_required`). Also calendar and scheduling tools' daily agendas, reminders of meetings already in your calendar ("starts in 10 minutes") and account notices; not a meeting someone booked, rescheduled or cancelled with you through a scheduling link, nor an invitation, update or cancellation a person sent you from their own calendar (those are `action_required`, [EX-46](#ex-46--a-meeting-booked-through-a-scheduling-link-stays-in-the-inbox)). Also a plain response to an invitation you sent ("Accepted: …", "Declined: …", "Tentative: …", "<name> has accepted this invitation", Google Calendar or Outlook): nothing is left to do; but not one that proposes a new time or carries a note asking you something (that is `action_required`). |
| `newsletter` | `Jev/Newsletters` | archive | 0.80 |  |
| `marketing` | `Jev/Marketing` | archive_unsubscribe | 0.90 | `unsubscribe_known_contacts: true` (default). |
| `cold_outreach` | `Jev/Outreach` | archive_unsubscribe | 0.90 | `unsubscribe_known_contacts: false`. |
| `spam` | `Jev/Spam` | spam | 0.90 | Fraud only: scams, phishing, forged authentication. Passing SPF/DKIM is not proof of legitimacy. The signs of phishing are mismatches: links to a brand unrelated to the sender, a fake reply, a copied conversation between other people, made stronger by DMARC `none` or `fail`. A document, e-signature, tax-return or client-portal notice that passes DMARC and links to the service's own brand domains is not spam ([EX-39](#ex-39--a-tax-portal-notice-that-passes-dmarc), [ADR 0042](decisions/0042-phishing-signals-for-jev.md)). |

Extra labels:

| Label | When |
| -- | -- |
| `Jev/Needs Reply` | Jev's `needs_reply` ≥ 0.7 (`needs_reply.threshold`) **and** the decision doesn't archive or spam the email. Also on allowlist decisions and rules to a keep category, where Jev is asked for the flags only ([3.5](#35-sender-rules)). Never on rules to archive or spam categories (Jev isn't asked). |
| `Jev/Invoices` | Tag `invoice_to_pay` ≥ 0.8. Added whatever the category, archived or not. Also on allowlist and keep-rule decisions; never on rules to archive or spam categories. |
| `Jev/Review` | The decision is low confidence (below the threshold and no archive group), or Jev named a category that doesn't exist. |
| `Jev/Processed` | Every email labeled in `label` or `act` mode. |

With the shipped thresholds (0.0), a keep answer is always applied and never goes to Review.

### 3.2 Confidence outcomes (Jev path)

`threshold` = the account's `min_confidence` for the category, else the category's own.

| Situation | Outcome | Unsubscribe | Spam |
| -- | -- | -- | -- |
| Jev's category isn't in `categories.yaml` | `important`, confidence 0.0, `Jev/Review` (low confidence) | no | no |
| `confidence ≥ threshold` | The category's action: keep, archive, archive + unsubscribe checks, or spam | only after the [unsubscribe checks](#33-unsubscribe-conditions) | `spam` category only |
| Below threshold and the **archive group** holds (next table) | Archived under the top category, `archive_group = true`, no Review | **never** | **never** |
| Otherwise | Stays in the inbox with `Jev/Review` + the suggested category label (`low_confidence = true`) | no | no |

**Archive group** (`policy._archive_group_confident`). All of these must hold:

| # | Condition | Shipped value |
| -- | -- | -- |
| 1 | `archive_group_min_confidence` is set (remove it to disable the group) | 0.90 |
| 2 | Jev's top category has action `archive` or `archive_unsubscribe` (an email with spam or a keep category on top never uses the group) |  |
| 3 | Needs-reply veto doesn't apply. It applies when `needs_reply ≥ 0.7` **and** either `previous_contact` is `wrote_to_sender` / `wrote_to_their_domain` and the email can be replied to (not [`no_reply`](#48-machine-and-no-reply-addresses)), or the keep categories (`important` + `action_required`) together reach `archive_group_reply_keep_probability` | 0.05 |
| 4 | Floor = max(`archive_group_min_confidence`, the top category's `threshold`): the group never lowers a bar | 0.90 |
| 5 | The probabilities of the counted categories add up to the floor or more. A category counts when its action is `archive`/`archive_unsubscribe`/`spam`, it is in `act_categories` (when the account sets it), and it is the top category or its threshold (the account's `min_confidence`, else the category's own) is at most the floor. Spam only adds evidence: the email is archived under the top category, never moved to Spam ([ADR 0058](decisions/0058-spam-counts-toward-the-archive-group.md)) | 0.80 / 0.90 ≤ 0.90: every archive category and spam count |

`archive_group_reply_keep_probability` must be at most `1 - archive_group_min_confidence` (config
load fails otherwise): the group only fires when the counted categories (archive and spam) hold ≥
0.90, so the keep categories hold ≤ 0.10 and a higher value could never be reached. `0` means any
reply signal blocks the group.

When the veto applies, the email falls through to Review, and since it isn't archived it also gets
`Jev/Needs Reply`.

For a **no-reply** email (the sender is a no-reply address and so is every `Reply-To`, see
[4.8](#48-machine-and-no-reply-addresses)) the known contact doesn't count: nobody reads a reply, so
the reply signal is judged as for a stranger, by the keep categories alone. Jev's question asks for
"a reply or an action", so the keep-signal half of the veto still applies, and a kept no-reply email
still gets `Jev/Needs Reply` (the action may be real)
([EX-25](#ex-25--a-no-reply-sender-from-a-domain-you-write-to),
[ADR 0032](decisions/0032-machine-addresses-make-no-known-contact.md)).

What counts as "a reply or an action" is spelled out in the question itself (`needs_reply.question`,
[4.7](#47-what-jev-receives)): yes for a person writing to you or something only you can do (pay,
sign, approve, authorize, confirm an appointment, renew an expiring document, secure your account)
or a lead or contact request from a form on your own website; no for company or bulk mail (surveys,
feedback, ratings or reviews, promotions, webinars or events), even if it asks you to reply or
click, a scheduling tool's notice that someone booked, moved or cancelled a meeting with you (it is
already confirmed: you only attend), a calendar invitation, update or cancellation, or a response
proposing a new time, unless it also asks you a personal question (you answer it in your calendar,
not by email; both are `action_required` without `Jev/Needs Reply`,
[EX-46](#ex-46--a-meeting-booked-through-a-scheduling-link-stays-in-the-inbox)), a conversation
between other people that isn't addressed to you, and phishing
([EX-38](#ex-38--a-fake-reply-with-a-voicemail-lure)). A person's own "can you give feedback on
this?" or invitation is a person writing to you. `action_required` excludes the same calls to
action, and `important` excludes mass mail that only greets you by name, so a survey's keep
probability stays low too ([EX-33](#ex-33--a-satisfaction-survey-from-a-no-reply-sender),
[ADR 0038](decisions/0038-surveys-are-not-a-personal-action.md)).

### 3.3 Unsubscribe conditions

An unsubscribe is irreversible. During `run` it happens only when **all** of these hold:

| # | Condition | Where |
| -- | -- | -- |
| 1 | The final category's action is `archive_unsubscribe` | `policy._apply` |
| 2 | Decided by a sender rule, or by Jev at or above the category threshold. An archive-group or Review decision never unsubscribes | `policy.decide` |
| 3 | **Not a sender decided per email** ([3.5](#35-sender-rules)) unless a **manual** rule decided the email: Jev's answer, the email's own correction or a stored decision never unsubscribe a sender in `decide_each_email` (`Veto.DECIDED_EACH_EMAIL`; [EX-45](#ex-45--a-sender-decided-per-email-is-unsubscribed-only-on-a-manual-rule)). The email keeps its category and is archived | `policy.unsubscribe_gate` |
| 4 | **Verified**: `dmarc=pass` in Gmail's own `Authentication-Results` ([4.2](#42-dmarc)), for a trusted group's author in Google's ARC record of what the group received ([4.2](#42-dmarc)), and a `List-Unsubscribe` header with an `https:` or `mailto:` URI (an `http:`-only header counts as none, [4.3](#43-list-unsubscribe-one-click-and-mailto)). If not, the email keeps its category, is archived, and `unsubscribe_skipped_unverified` is set. It is never moved to Spam for this | `policy.unsubscribe_gate` |
| 5 | **Not a group or discussion list** (`_group_list`): no `List-Post` header; no unsubscribe target on `googlegroups.com` / `groups.google.com` (host or subdomain); not relayed by a Google Group (`group_author` found, trusted or not); not sent as a list's own address (`sender_is_list`, [3.5](#35-sender-rules)), which Google Groups' `Mailing-list` alone can name; sender domain and `List-Id` not on the account's own domain (or a subdomain) | `policy.unsubscribe_gate` |
| 6 | For a category with `unsubscribe_known_contacts: false` (`cold_outreach`): `previous_contact` is not `wrote_to_sender` / `wrote_to_their_domain` (`known_contact_kept` otherwise). Unknown (`None`, the search failed or wasn't run) does **not** block it: deliberate, so that one failed search doesn't stop every outreach unsubscribe ([ADR 0011](decisions/0011-never-unsubscribe-groups-or-known-contacts.md), re-confirmed by the maintainer on 2026-10-14; `unsubscribe-retry` holds the row instead) | `policy.unsubscribe_gate` |
| 7 | Mode is `act`, and the account has no `act_categories` or lists the category | `policy.acts_on`, from `policy.plan` |
| 8 | The run's budget (`MAX_UNSUBSCRIBES_PER_RUN`, 20) has attempts left | `triage.process` |
| 9 | The list isn't **settled**: no `unsubscribes` row for `(account, list_key)`, or its row is `failed` with fewer than 3 attempts | `triage._unsubscribe` |
| 10 | `dmarc_pass` again, inside the unsubscriber (defence in depth) | `unsubscribe.unsubscribe` |

The same checks run on the **rule path**, in the same order: a sender rule skips Jev, not the sender
checks. For a rule to a category that spares known contacts, `triage` runs the sent-mail search only
when the answer could change the outcome (`triage._rule_needs_contact`: `acts_on`,
`unsubscribe_gate` passes with only the unknown contact left to decide, list not settled).

A `failed` row is retried by `run` the next time an email from the same list passes all the checks
(until 3 attempts), and by `inbox-jev unsubscribe-retry`. Both store an attempt the same way
(`triage.record_attempt`: result, `attempts + 1`, time, an `unsubscribe` log line).

**`unsubscribe-retry`** (`unsubscribe_retry.retry_unsubscribes`) re-checks each `failed` row with
fewer than 3 attempts against the email's current state, with the same `acts_on` and
`unsubscribe_gate` as `run`. Differences from `run`:

| Check | `run` | `unsubscribe-retry` |
| -- | -- | -- |
| Mode not `act` | No unsubscribe | Every row held: `held: mode is …` |
| Stored message missing | n/a | held |
| Row restored or `corrected_category` set | Restored: never processed again. Corrected: the correction rule decides | `skipped` (before any Gmail call) |
| Run cap | 20 per run | 20 per call; extra rows held |
| Email deleted (404) | n/a | held; any other Gmail error is raised |
| `SPAM` or `TRASH` label in Gmail | n/a | `skipped` |
| Correction visible in live labels, or a user edit (starred, our label removed, another Jev label added) | Correction learned / hands off | `skipped` |
| Category | Current rule, else Jev | Current rule (if its category exists), else the stored category. For a sender in `decide_each_email`, or an email sent as a list's own address, only a manual rule counts ([3.5](#35-sender-rules)): an `auto` or `correction` rule is ignored, so a row it decided is held (`rule since removed`) and one to a keep category doesn't skip the row |
| Current rule points to a non-unsubscribing category | Rule decides | `skipped` |
| Stored category from a rule that was since deleted | Jev is asked again | held: `rule since removed` |
| Stored Jev category | Jev's fresh answer vs threshold | Stored confidence must still be ≥ the current threshold (account override included) and not `low_confidence`; else held |
| Category not unsubscribing or not in `act_categories` | No unsubscribe | held |
| Allowlisted sender | No Jev, `important` | `skipped` |
| Sender in `decide_each_email`, no manual rule | No unsubscribe | `skipped` (`sender decided email by email, no manual rule`) |
| Unverified, group or own list | No unsubscribe | `skipped` |
| Known contact (`cold_outreach`) | No unsubscribe | `skipped` |
| Sent-mail search fails (`None`) | **Unsubscribe goes ahead** | **held**: nothing in a manual retry is urgent |

`skipped` rows get `error = "retry: <reason>"` and are settled, so `run` won't retry them either.
Held rows stay `failed`.

### 3.4 Groups and mailing lists

| Case | Sender used for rules, allowlist, `previous_contact`, DB, digest | Jev state | Unsubscribe |
| -- | -- | -- | -- |
| Direct mail | `From` address | `from` header | Per [3.3](#33-unsubscribe-conditions) |
| Google Group on a domain in the account's `group_domains`, with the [trust checks](#45-x-original-sender-trust) passing | The author (`X-Original-Sender`); `relayed_by` = group address | adds `original_sender` and `sent_via_group` | **Never** |
| Google Group on any other domain (incl. `@googlegroups.com`, public providers), or trust checks failing | The group address (`From`), sent as the list: no learned rule decides it ([3.5](#35-sender-rules)) | `from` header only | **Never** if `X-Original-Sender` was a candidate, a `List-Post` is present, the target is Google Groups, or the group is on your own domain; else per 3.3 |
| Mail the group itself sends (held-spam report, moderation notice): no `X-Original-Sender` | The group address (sent as the list when `Mailing-list` or `List-Post` names it) | `from` header | Per 3.3; never for a group on your own domain |
| Other discussion lists (Mailman, groups.io, LISTSERV, Discourse) | `From`: the author when the list keeps it, else the list (sent as the list when it is the `List-Post` address) | `from` header | **Never** (`List-Post`) |
| ESP / sequence tool (Mailchimp, Instantly…) | `From` | `from` header | Per 3.3 (they don't send `List-Post`) |

- `group_domains` is opt-in and empty by default, not even the account's own domain. Entries are
  normalised (lowercase, no trailing dot, no leading `@`) and rejected, failing the accounts file at
  load, when they aren't a domain (`signals.DOMAIN`: letters, digits and hyphens, ending in a TLD of
  2+ letters or a punycode IDN TLD such as `xn--p1ai`), are a public suffix of the
  [Public Suffix List](#49-public-suffix-list) (`psl.is_public_suffix`, ICANN and PRIVATE sections:
  `co.uk`, `com.ar`, `gouv.fr`, `com.ai`, `wa.gov.au`, `kommune.no`, `plc.ly`, `github.io`,
  `blogspot.com`, `xn--55qx5d.xn--j6w193g` for `公司.香港`), or are (or end in) a public mail provider
  (`config.FREE_MAIL`, country domains included), `googlegroups.com` or `groups.google.com`. A name
  the list doesn't have as a suffix is a domain, however short or deep: `acme.io`, `sap.de`,
  `adv.ai`, `plc.io`, `sch.gg`, `ato.gov.au`. See
  [EX-27](#ex-27--a-public-suffix-in-group_domains-is-refused) and
  [EX-32](#ex-32--the-public-suffix-list-decides-what-a-domain-is).
- **Allowlist**: `allow_senders` / `allow_domains` are matched against the resolved sender. For a
  trusted group that is the author, so `allow_domains: [yourco.com]` does **not** trust strangers
  writing to `hello@yourco.com`; put the group's exact address in `allow_senders` to allowlist it.
  For an untrusted group the sender is the group address, so an `allow_domains` entry for the
  group's domain allowlists everything sent through it.
- Rules and corrections for a trusted group key on the author; untrusted groups stay keyed on the
  group address, never on the author they claim. An email whose sender is the group's or list's own
  address (trust checks failing, an untrusted group, a list's own notice) learns no rule and no
  learned rule decides it ([3.5](#35-sender-rules), "Mail sent as a list's own address"): that
  address carries every member's mail.
- `list_key` (once per list) is `List-Id`, else the sender domain.

### 3.5 Sender rules

| Rule source | Created by | Key | Overrides |
| -- | -- | -- | -- |
| `manual` | `inbox-jev rules set -a EMAIL SENDER CATEGORY` | address or `@domain`, lowercased; anything else (a bare domain, a display name) or a category not in `categories.yaml` is refused (exit 2) | Replaces any rule on the same key (`rules.set_rule`), including correction and auto rules |
| `correction` | A detected correction ([3.7](#37-corrections-user-edits-and-restore)) | exact resolved sender address | Replaces any rule on the same key |
| `auto` | `rules.record_auto` after consistent Jev answers | exact resolved sender address | Never changes manual/correction rules |

- Lookup (`rules.SenderRules.match`, through `triage.rule_for`): the first active rule decides, on
  the exact address, else on `@domain` (`policy.rule_keys`). Allowlist wins over rules.

- **Senders decided per email** (`decide_each_email` in `accounts.yaml`, per account; addresses or
  `@domain`, subdomains included, normalised and checked like `rules set`'s keys:
  `config.normalize_sender_key`; anything else fails the file at load): for platforms that send both
  mail you can archive and mail that needs you (a CRM's notifications address sends dashboards and
  website leads). For such a sender (the resolved sender, as rules key on):

  - only **manual** rules decide (`rules.SenderRules.deciding`): `auto` and `correction` rules are
    ignored, as if absent, so lookup goes on to the next rule (an ignored exact `auto` rule doesn't
    hide a manual `@domain` rule). A manual rule is your explicit choice for every email of the
    sender, so it still wins. `run`, `unsubscribe-retry` and `dry_classify.py` use the same lookup.
  - no `auto` rule is learned or counted (`triage._learn_auto_rule`), and a pending one is left as
    it is;
  - a correction sets `corrected_category` on the email but creates no rule
    ([3.7](#37-corrections-user-edits-and-restore)); the digest still lists it under "Corrections
    learned", marked "(this email only)".
  - it is **never unsubscribed unless a manual rule** to an unsubscribing category decides the email
    ([3.3](#33-unsubscribe-conditions), condition 3; `run` and `unsubscribe-retry` through
    `policy.unsubscribe_gate`): its address also carries the mail that needs you, and one
    unsubscribe (once per `List-Id`, else per sender domain) may turn that stream off for good.
    Jev's `marketing` answer archives the email, nothing more;
  - The ignored rules stay in the DB (`rules list` still shows them): removing the sender from the
    list makes them decide again. `rules delete` them to forget them.

  See [EX-44](#ex-44--a-sender-decided-per-email-keeps-its-leads-and-archives-its-fyis),
  [EX-45](#ex-45--a-sender-decided-per-email-is-unsubscribed-only-on-a-manual-rule) and
  [ADR 0045](decisions/0045-senders-decided-per-email.md).

- **Mail sent as a list's own address** (`Signals.sender_is_list`): the resolved sender equals an
  address the email's own headers give the list, exactly: a `mailto:` address of `List-Post`
  ([4.4](#44-list-post)) or the address in Google Groups' `Mailing-list: list <address>`. That
  happens when a group's author isn't recovered ([4.5](#45-x-original-sender-trust): Gmail's header
  doesn't parse, no `X-Original-Sender`, a group outside `group_domains` that rewrites `From`), for
  a list's own notices, and for an announcement list sending as itself. Such an address carries
  every member's mail, so the email is decided on its own, exactly like a sender in
  `decide_each_email` above (`rules.decided_per_email`; `run`, `unsubscribe-retry` and
  `dry_classify.py`): learned rules on the address are ignored, none is learned or counted, a
  correction stays on the email ("(this email only)" in the digest), and a manual rule still
  decides. `rules set` on an address stored as a list's own prints a note on stderr that the rule
  covers everything the list sends as itself. A trusted relay resolved to its author isn't sent as
  the list, and neither is mail from the same address without list headers (a person writing from a
  shared mailbox): a rule on the address still decides that. It is never unsubscribed: it is a group
  ([3.3](#33-unsubscribe-conditions), condition 5). Colleagues on a `group_domains` domain keep
  their rules: only the list's own address is concerned. An ESP's newsletter names no `List-Post`
  and keeps learning rules. Each row stores the flag (`messages.sender_is_list`) for
  `corrections.check`; rows stored before it existed are `NULL`, read as not sent as the list. See
  [EX-50](#ex-50--a-correction-on-mail-sent-as-your-group-teaches-no-rule) and
  [ADR 0051](decisions/0051-no-learned-rules-on-a-lists-own-address.md).

- A rule decision has source `rule` and confidence 1.0, and no `previous_contact` unless the
  unsubscribe needs it.

- **Rules to a keep category** (`important`, `action_required`), like the allowlist, still ask Jev,
  for the flags only: `Jev/Needs Reply` and the tag labels follow Jev's answer, the category stays
  the rule's (or `important`), Jev's own category is stored as `jev_category` and ignored, and the
  answer never counts toward an auto rule. No sent-mail search is run for it (`previous_contact` is
  left out of the Jev state). If that call fails (a Jev outage, an error on that email), the email
  is decided without Jev's answer, as if it hadn't been asked: labeled by the rule or the allowlist,
  without Needs Reply or tags, with a warning in the log (`flags-only Jev call failed`, account, id,
  error type, status and request id; no content). It isn't stored as `error`. Once the pass has
  stopped asking Jev ([§1](#1-the-pass-over-an-inbox)), such an email is decided the same way
  without a call. See [EX-19](#ex-19--jev-is-down-when-an-allowlisted-email-arrives). **Rules to an
  archive or spam category** skip Jev: their email leaves the inbox, where Needs Reply is never
  added. See [EX-15](#ex-15--a-client-with-a-keep-rule-asks-a-question) and
  [ADR 0020](decisions/0020-flags-for-allowlist-and-keep-rules.md).

- A rule whose category no longer exists in `categories.yaml` counts as no rule: when it is the
  first match, Jev decides. Lookup **never falls through** to the `@domain` rule behind a stale
  exact rule: the exact rule is your exception to the domain rule, so applying the domain rule would
  undo it (archive, maybe unsubscribe, the sender you singled out). A stale `@domain` rule with no
  exact rule also sends the email to Jev. It is logged once per run
  (`sender rule ignored: unknown category`) and left in the DB, active, for you to `rules delete`
  (or to apply again if the category comes back). `unsubscribe-retry` and `dry_classify.py` use the
  same lookup. An **auto** rule in that state is relearned like a pending one: auto rules are keyed
  on the exact address, so a stale one is always the first match and its sender goes to Jev; the
  next counted answer replaces its category with `hits = 1`. See
  [EX-14](#ex-14--a-rule-to-a-category-you-removed).

**Automatic rules** (`rules.record_auto`): only decisions Jev made (source `jev`, not allowlist or
rule, so never a flags-only answer), and only in `label` and `act`. Nothing is learned in `dry_run`
([EX-18](#ex-18--a-dry-run-trial-learns-no-rules)). Hits count **emails**, not answers: an email
processed again (`label` then `act`, or after `reprocess`) is sent to Jev again, but when its stored
row already holds a Jev decision from `label` or `act`, the new answer adds no hit; it can still
reset a pending rule, and a counting answer in another category than the pending rule's restarts it
at 0 hits without switching its category
([EX-29](#ex-29--an-email-answered-again-counts-once-toward-an-auto-rule),
[ADR 0036](decisions/0036-auto-rule-hits-count-emails.md)):

| Step | Condition | Effect |
| -- | -- | -- |
| Reset | The decision is a keep category, **or** the keep categories other than the decided one hold ≥ `auto_rule.max_keep_probability` (0.02) — even on a low-confidence answer — **and** the sender has a pending (inactive) `auto` rule to a non-keep category | `hits = 0` |
| Doesn't count | `low_confidence`, `archive_group`, a known contact (`previous_contact` is `wrote_to_sender` / `wrote_to_their_domain`) in a category that spares known contacts, the keep signal above, a calendar message (`Email.calendar_method` is set), or Jev's confidence < `auto_rule.min_confidence` (0.95) | nothing more |
| Already learned from | The answer would count, but the email's row held a Jev decision from `label` or `act` | a pending `auto` rule to another category: `hits = 0`, category unchanged; otherwise nothing |
| Counts, no rule yet |  | new `auto` rule, `hits = 1`, active if `auto_rule.hits` ≤ 1 |
| Counts, rule is manual/correction, or an active auto rule to an existing category |  | unchanged |
| Counts, same category |  | `hits += 1`; active at `auto_rule.hits` (3) |
| Counts, other category (incl. an active auto rule to a removed category) |  | category switches, `hits = 1`, active only if `auto_rule.hits` ≤ 1 |

Unverified senders (`unsubscribe_skipped_unverified`) can earn a rule: the rule path re-runs the
DMARC/`List-Unsubscribe`/group checks on each email. A known contact never earns a rule to a
category that spares known contacts (`cold_outreach`), whether the sender is verified, unverified or
a group: such a "pitch" is where Jev is most likely wrong, so the sender stays on the Jev path. Keep
categories can earn a rule too.

A **calendar message** (an email with an iTIP `text/calendar` part, any method: an invitation or
update, a response to an invitation, a cancellation, a proposed new time;
[4.6](#46-body-extraction)) never counts, whatever Jev answers: no hit, no new rule, no switch or
restart of a pending rule's category. Google Calendar and Outlook send a response from the
responder's own address, so three confident "Accepted:" answers would otherwise turn on a rule to
`notification` on a **person** and archive their next real email without asking Jev; and calendar
traffic says nothing about a sender's other mail. The keep reset above still applies to it: a reset
only sends more mail to Jev. See
[EX-47](#ex-47--rsvp-responses-from-a-person-never-teach-a-rule-on-them) and
[ADR 0047](decisions/0047-calendar-messages-teach-no-auto-rule.md).

### 3.6 Modes and `act_categories`

`policy.plan`, with `policy.acts_on` deciding whether a category is acted on:

| Mode | DB + digest | Gmail labels | Archive / spam / unsubscribe |
| -- | -- | -- | -- |
| `dry_run` | yes (no auto rules learned) | no | no |
| `label` | yes | yes (+ `Jev/Processed`) | no |
| `act` | yes | yes | yes |
| `act`, category not in the account's `act_categories` | yes | yes | no (treated as `label`) |

- `act_categories` is matched against the **final** category. Absent = every category acts; `[]` =
  none; an empty key (`act_categories:` with nothing) is rejected at load, and so is a key that
  isn't a category in `categories.yaml` (`config.validate_accounts`, run by every command that reads
  `accounts.yaml`). `triage.run_account` itself doesn't repeat that check: a key that matches no
  category only fails closed (the category it was meant for is labeled, not acted on).
- In `act`, archive removes `INBOX`; spam removes `INBOX` and adds `SPAM` (the row is `spammed`, not
  `archived`).
- When an email is processed again (weaker mode before, or `reprocess`), labels from the earlier
  pass that the new plan doesn't add are removed (never `SPAM`).
- `inbox-jev reprocess -a EMAIL --category KEY [--low-confidence-only]` sets act rows that are
  `done`, not archived, spammed or restored, back to `label`, so the next `act` run (within its
  `--lookback`) redoes them. A `KEY` not in `categories.yaml` is refused (exit 2).

### 3.7 Corrections, user edits and restore

**What counts as a correction** (`corrections.detect`, given the email's current labels):

| Current state in Gmail | Correction to |
| -- | -- |
| Exactly one Jev category label, different from the stored category | That category |
| Exactly one Jev category label, the stored one, the row was low confidence and `Jev/Review` was removed | The stored category ("accepted the suggestion") |
| Archived or spammed by us and back in `INBOX`, when neither row above applies | `important` |
| Two or more category labels, or none, and not pulled back | none |

A correction sets `corrected_category` on the row and a `correction` sender rule (active) on the
row's stored sender. For a sender in the account's `decide_each_email`, or an email sent as a list's
own address (the row's `sender_is_list`; [3.5](#35-sender-rules)), there is no rule: the correction
decides that email alone when it is processed again (a stronger mode, or `reprocess`;
`triage._own_correction`), and the sender's next emails go to Jev. It is checked in two places:

- `corrections.check`, at the start of every `label`/`act` run: rows that are `done`, not `dry_run`,
  not restored, not yet corrected, processed in the last **7 days**, and not checked in the last
  **hour**; never-checked rows first (newest first), then the ones checked longest ago, at most
  **100** per run, so older rows aren't starved when there are more. Deleted emails are skipped.
- In the pipeline, when an email from an earlier `label`/`act` pass is processed again.

**User edits = hands off** (`triage.user_edit`, only for rows that have labels from an earlier pass,
and never in `dry_run`):

| Edit | Result |
| -- | -- |
| Email is starred | Hands off, **even after a correction** |
| Row has a correction | Processed again: the new correction rule decides (for a sender in `decide_each_email` or an email sent as a list's own address, the email's own correction) |
| A label we added (other than `Jev/Processed`) was removed | Hands off |
| A Jev label we didn't add (category, `Jev/Needs Reply`, `Jev/Review`, tag) is present | Hands off |

"Hands off" means: the row is marked `done` in the current mode, nothing changes in Gmail and the
email isn't revisited in that mode. A starred **new** email (no earlier pass) is processed normally.

**Restore** (`inbox-jev restore -a EMAIL --since 1d [--category K]`, `triage.restore`): for
`label`/`act` rows in the window, moves archived/spammed emails back to the inbox, removes our
labels and `SPAM`, and marks them `restored`. Restored emails are never processed again, aren't
learned as corrections, and their failed unsubscribes are `skipped` by `unsubscribe-retry`.
Unsubscribes can't be undone. An email Gmail no longer has (HTTP 404) is skipped, its row left
unchanged, and counted in the command's output; any other Gmail error stops the command (emails
restored before it stay restored). See [EX-20](#ex-20--restore-with-an-email-deleted-in-gmail).
`--category` must be a category in `categories.yaml` or one this account's stored emails still carry
(so a category removed since can still be undone); anything else is refused (exit 2) before Gmail is
touched, instead of restoring nothing.

## 4. What it checks and how

### 4.1 `previous_contact`

`triage.previous_contact(gmail, account, sender)` returns `wrote_to_sender`,
`wrote_to_their_domain`, `never`, or `None` (unknown):

1. The sender (resolved: a trusted group's author) is lowercased and must match
   `^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$`. Otherwise `None` with no query (odd characters would
   change the search).
2. Unless the sender is a [machine address](#48-machine-and-no-reply-addresses) (writing to
   `noreply@` or a bounce address doesn't make anyone a contact, so it isn't searched): Gmail search
   `in:sent to:<sender>` (`maxResults=10`, `sent_scan.SENT_ADDRESS_READS`), then read the `To`, `Cc`
   and `Bcc` headers (`format=metadata`, no body) of the matches, newest first, one at a time. The
   first that went to the sender's own address → `wrote_to_sender` (`signals.wrote_to_address`):
   Gmail's `to:` also matches similar addresses, so a match alone isn't proof
   ([EX-34](#ex-34--an-email-to-a-similar-address-makes-no-known-contact)). Addresses compare
   lowercased (`signals.mailbox_key`); a consumer Gmail address (`gmail.com`, `googlemail.com`) also
   without dots and without a `+tag` in its local part, as Gmail delivers it. No other provider's
   rules are applied: Google Workspace and other domains don't promise that dots or `+tags` reach
   the same mailbox (`support+123@` can be a ticket), and merging two addresses that aren't one
   would make a stranger a known contact. An email with no recipient address in those headers at all
   counts (Gmail matched something they don't show), and so do 10 matches none of which went to the
   sender when Gmail has more (an older one might; logged as
   `sent-mail address check inconclusive`).
3. Unless the sender's domain is a free-mail provider (`config.FREE_MAIL`: Gmail, Outlook / Hotmail
   / Live / MSN, Yahoo, iCloud, AOL, Proton, Tuta, Fastmail, GMX / Web.de, Mail.com, Yandex, Mail.ru
   and large regional ones, with the big providers' country domains such as `yahoo.com.ar`,
   `hotmail.co.uk`, `outlook.com.br`, `live.com.mx`;
   [EX-26](#ex-26--writing-to-a-friend-on-country-free-mail-makes-no-known-contact)) or the
   account's own domain: the **domain scan** (below) reads the `To`, `Cc` and `Bcc` headers of the
   sent emails matching `in:sent to:<domain>`. The first email with a recipient on the domain (or a
   subdomain) that isn't a machine address → `wrote_to_their_domain` (`signals.wrote_to_person_at`).
   An email with no recipient on the domain at all (Gmail's `to:` matched something the headers
   don't show) also counts, as before. Until every match has been read without finding a person, the
   domain counts as written too (logged as `sent-mail domain scan not finished`).
4. Otherwise `never`. So replies to LinkedIn notifications (`m-…@bounce.linkedin.com`) no longer
   make every LinkedIn email a known contact
   ([EX-24](#ex-24--replies-to-notifications-dont-make-a-contact)).

An email deleted between a search and its read (404) is skipped. The `list` and each read retry a
429, 5xx or rate-limit 403 with backoff (googleapiclient, `gmail.SINGLE_RETRIES` = 3) before
failing. Each query's answer is cached in the `Gmail` object, so per account per run. Any exception
→ `None` (logged as a warning, not cached), except a database error saving the domain scan (below),
which is raised: the email fails and is retried (`run`), or `unsubscribe-retry` stops, in both cases
before any unsubscribe request.

**Domain scan** (`sent_scan.DomainScan`, kept per account and domain in the `sent_domain_scans`
table by `scan_rows.DomainScanRows`;
[EX-35](#ex-35--a-domain-written-to-only-at-machine-addresses-is-read-in-full),
[ADR 0040](decisions/0040-domain-scan-kept-across-runs.md)). The scan reads the matches newest
first, at most 25 per check (`sent_scan.SENT_DOMAIN_READS`, the read budget per domain and run), and
remembers the dates of the newest and oldest emails it read (Unix seconds, as Gmail's `after:` /
`before:` take them) and the ids read in those two seconds:

- No scan yet: `in:sent to:<domain>` (`maxResults=25`) from the newest match down.
- A scan, first: `in:sent to:<domain> after:<newest − 1 s>` (up to 500,
  `sent_scan.SENT_NEWER_LIMIT`; with more, the check stops and the domain counts as written). The
  new matches are read oldest first, so what was read stays one span without gaps.
- Then, unless the scan reached the end: `in:sent to:<domain> before:<oldest + 1 s>` and the next
  older matches, newest first, until the budget runs out.
- `verdict`: `person` once a match went to a person: final, no Gmail call for that domain after
  that. `never` (with `verdict_at`, when it was reached, also when a check reaches it without
  reading anything) once nothing older is left and nothing newer is unread: the domain isn't a known
  contact. Otherwise `scanning`: counts as written. `never` is checked again on every check by the
  `after:` search, so new sent mail to the domain is read (and a new email to a person there makes
  it `person`) the first time an email from it is decided.
- A `never` reached 30 days ago or more (`sent_scan.NEVER_VERDICT_TTL`) starts over from the newest
  match, as a new scan: the `after:` search only lists mail dated after the span, so sent mail that
  appears later with an older date (restored from Trash, imported) would otherwise never be read
  ([EX-36](#ex-36--a-never-verdict-is-redone-after-a-month)). While it re-reads, the domain counts
  as written.
- Each read is checked against the order the span relies on: walking down (`before:`, or the first
  scan), each email no newer than the one read before it and than the span's oldest; walking up
  (`after:`), no older than the one before it and than the span's newest. An email out of order
  means Gmail's list order or its date filters don't match the dates read, so the span may have
  gaps: the scan is dropped and starts over at the next check, the domain counts as written, and a
  warning with counts only is logged. That check never yields `never`; an email to a person still
  makes it `person` ([EX-37](#ex-37--a-scan-read-out-of-date-order-is-not-trusted)).
- The scan is saved with the email it was made for (same session and commit; an email whose
  processing fails loses it, and the next check reads those emails again). The save is an upsert run
  at once (SQLite and Postgres `ON CONFLICT`): when `run` and `unsubscribe-retry` start the same
  domain's scan, the last one saved wins (each is gap-free on its own), except that a `person` saved
  under the same rules stays; neither fails on the unique key, and a locked database fails the save
  during the search, before any unsubscribe. On a database without that upsert, `run` (in every
  mode) and `unsubscribe-retry` fail the account at their start, before any Gmail call, and
  `previous_contact` raises rather than answer "unknown" (no veto in `run`). A Gmail error keeps
  what was read before it. A scan made under other rules (`sent_scan.scan_rules`: the code's
  `SCAN_RULES_VERSION` and the bundled Public Suffix List's digest, which decide machine addresses)
  starts over. A domain without any match stores nothing.
- `unsubscribe-retry` uses the same table; `scripts/dry_classify.py` keeps scans in memory only (it
  never writes the DB), so it shows what a first run would decide.

**Cost** (Gmail quota units: `messages.list` and `messages.get` cost 5 each), per new sender and
run: the address search is one `list` plus one metadata `get` per match read, stopping at the first
that went to the sender: one read for a real contact, at most 10. A domain with a `person` verdict
costs nothing; otherwise one `list` (the first scan) or one or two (`after:`, then `before:` while
unfinished) plus at most 25 reads (135 units) per run. A real contact's domain usually costs one
read, once. A domain written to only at machine addresses N times is read in full over ⌈N / 25⌉ runs
in which an email from it is decided; after that, one `list` per run, and the same full re-read once
every 30 days.

**Staleness**: `person` never expires (deleting the sent email doesn't undo it; the safe side). For
`never`, sent mail dated after the span is listed on every check, but mail that enters the sent
folder later with an older date (restored from Trash, imported or migrated, appended over IMAP by
another client) falls inside the span already read and is only seen when the scan restarts, at most
30 days after the verdict (`NEVER_VERDICT_TTL`). A change to what counts as a machine address in
code needs `SCAN_RULES_VERSION` bumped; a Public Suffix List update restarts the scans by itself.

When it runs: on the Jev path for every email (in every mode, including `dry_run`), and on the rule
path only when `triage._rule_needs_contact` says it can change an unsubscribe. Never for allowlisted
senders or keep rules (their flags-only Jev call goes without it).

Consequences of each value:

| Value | Jev state | Needs-reply veto on the archive group | `cold_outreach` unsubscribe (`run`) | `unsubscribe-retry` | Auto rule |
| -- | -- | -- | -- | -- | -- |
| `wrote_to_sender` / `wrote_to_their_domain` | sent | any reply signal vetoes (a no-reply email: only with keep probability ≥ 0.05) | no (`known_contact_kept` when verified and not a group) | `skipped` | doesn't count in a category that spares known contacts, verified or not, group or not |
| `never` | sent | only with keep probability ≥ 0.05 | yes | proceeds | normal |
| `None` | key left out | only with keep probability ≥ 0.05 | **yes** (other checks still apply) | **held** | normal |

The value is logged on the "message processed" line; it isn't stored in the DB.

### 4.2 DMARC

`signals.dmarc_passed(Authentication-Results headers)`: Gmail prepends its own
`Authentication-Results`, and a sender can inject more (even ones claiming `mx.google.com`). Only
the **topmost** header whose authserv-id (the token before the first `;`) is exactly `mx.google.com`
is trusted; `mx.google.com.attacker.example` is another server. Its `dmarc` verdict must be `pass`
(and not contradicted by another `dmarc` verdict). If there is no such header, DMARC doesn't pass:
Gmail stamps every inbound email, so any other header could be the sender's own
([EX-17](#ex-17--an-unsubscribe-header-without-gmails-authentication-results)). Besides the
unsubscribe checks, `dmarc_pass` also gates the Google Groups author recovery
([4.5](#45-x-original-sender-trust)).

The header is read as RFC 8601 defines it (`signals._parse_auth_results`), as an **allowlist**: the
whole header must match the RFC's grammar, or nothing in it counts
([ADR 0043](decisions/0043-authentication-results-parsed-per-rfc-8601.md)).

- **Opaque pieces**: comments (`(...)`, nested), quoted strings and domain literals (`[...]`) are
  each read as one piece, with `\` escaping the next character in a comment or quoted string. This
  holds inside a comment too, so a quoted envelope sender such as
  `"x); dmarc=pass ("@attacker.example`
  ([EX-41](#ex-41--a-quoted-envelope-sender-cant-hide-a-real-dmarc-fail)) or an address literal such
  as `x@[t:;dmarc=pass;]` ([EX-43](#ex-43--an-address-literal-cant-forge-a-dmarc-pass)), in Gmail's
  SPF comment, `smtp.mailfrom` or `smtp.helo`, can't close the comment or start a resinfo.
- **Grammar**: what remains must be `authserv-id [version]; resinfo; …` (or `authserv-id; none`),
  each resinfo `method[/version]=result [reason=…] [ptype.property=value …]`. A value is a run
  without whitespace, `;`, `@` or the delimiters above (so `header.b`'s base64 and an envelope local
  part's `=` and `+` fit), an address `[local-part]@domain`, or a literal; a quoted string or
  literal may stand for the local part or domain. Only the `method=result` that starts each resinfo
  is a verdict: properties such as `smtp.mailfrom=`, which name the envelope sender the sender
  chooses, are never read, so `dmarc=pass@attacker.example` there is no verdict
  ([EX-40](#ex-40--an-envelope-sender-that-looks-like-a-dmarc-pass)).
- **Vocabulary**: an `spf`, `dkim`, `dmarc` or `arc` result must be an RFC 8601 word (`pass`,
  `fail`, `softfail`, `hardfail`, `neutral`, `none`, `temperror`, `permerror`, `policy`). `arc` is
  read only to trust a group's ARC record (below); other methods (`dara`) must parse but are
  ignored.
- **Fails closed**: a header with anything else (an unbalanced comment, quote or literal, a stray
  `]` or `\`, a property without a value, a stray word, an empty resinfo, `dmarc=passx`) has no
  verdicts at all, not even those before the break: DMARC doesn't pass, and every method is `none`
  for Jev ([EX-42](#ex-42--a-malformed-gmail-authentication-header-verifies-nothing)). The trusted
  header is still the topmost `mx.google.com` one, malformed or not; a later one is never read
  instead.
- **One SPF and one DMARC verdict**: Gmail writes one of each; two that disagree are not trusted
  (`none` for that method, so DMARC doesn't pass).

`signals.sender_auth` reads the same trusted header, parsed the same way, for Jev's `sender_auth`
([4.7](#47-what-jev-receives)): the `spf`, `dkim` and `dmarc` verdicts, lowercased, `none` for a
method the header doesn't mention. DKIM is `pass` when any signature passes, else its first verdict;
SPF and DMARC are their one verdict, or `none` when two disagree. Without a trusted header it is
unknown and the key is left out. It only informs Jev; no rule acts on it.

**Mail relayed by a trusted group** ([4.5](#45-x-original-sender-trust)): a Google Group re-sends
the author's message from the group's address, so Gmail's own header authenticates the group's copy
(for a group on `group_domains`, always a pass for your own domain), not the author. For such an
email, once `resolve_relay` has made the author the sender, Jev's `sender_auth` and `dmarc_pass` are
the **author's**, read by `signals.relay_author_auth` from the ARC record (RFC 8617) Google adds
when the group receives the message
([ADR 0050](decisions/0050-group-relayed-mail-carries-its-authors-authentication.md),
[EX-49](#ex-49--phishing-relayed-by-your-own-group-shows-its-authors-authentication)). Google Groups
adds two ARC sets with the author's verdicts and Gmail one more on delivery, a copy of its own
header; the record read is the `ARC-Authentication-Results` at instance N-1 of N, and only when:

- Gmail's own header (the trusted one above) has the verdict `arc=pass`: Gmail checked every
  `ARC-Seal` signature, and a sender can't sign for `google.com`. An `arc=pass` inside a comment, in
  another header, or next to another `arc` verdict that disagrees doesn't count;
- every `ARC-Seal`, `ARC-Message-Signature` and `ARC-Authentication-Results` parses (tag-lists per
  RFC 6376 3.2, a tag named twice is invalid; a strict `i=N;` before the authentication results),
  with exactly one of each per instance, numbered 1 to N, N ≥ 3;
- every seal's `cv` is `none` at instance 1 and `pass` above it;
- the seals at N, N-1 and N-2 are signed `d=google.com`, exactly;
- the record's authserv-id is exactly `mx.google.com`, and it parses with the grammar above;
- the record is about the author's message: its DMARC resinfo names exactly one `header.from`
  (lowercased, a final dot dropped), and it is exactly the domain of `X-Original-Sender`. A record
  of the group's own copy (`header.from` = the group's domain), which one more Google hop after the
  group puts at N-1 (a nested group, a set Google adds to its layout), names the group, not the
  author. DMARC's `header.from` is the domain of the `From` the message had when Google received it,
  which for the author's message is the author's address, the one Google Groups copies into
  `X-Original-Sender`; Google writes it inside the sealed record, so the sender can't choose it;
- the record agrees with Google Groups' second record: the one at N-2 parses to the same
  authserv-id, the same verdicts and the same `header.from`. One extra Google set after the group
  (Google's record of the group's copy, the group's pass) differs from the author's record below it,
  so it is not read, even for an author on the group's own domain, whose `header.from` the binding
  above can't tell from the group copy's.

Without such a record (no record, an untrusted one, a malformed one, one without `header.from` or
about another domain, one that differs from Google Groups' second record) the author's verdicts are
unknown: `sender_auth` is all `none` and the resolved `dmarc_pass` is false, never the group's pass.
The comment Gmail writes after `arc=pass` (a summary of the author's verdicts) and the
`X-Original-Authentication-Results` header Google Groups adds are never read: one is free text, the
other a header a sender can write too. Before the author is resolved, `dmarc_pass` is still the
group's, which is what the `X-Original-Sender` check needs ([4.5](#45-x-original-sender-trust)).
Untrusted groups and direct mail keep Gmail's own header.

### 4.3 `List-Unsubscribe`, one-click and `mailto:`

- `List-Unsubscribe` (first header) is parsed as RFC 2369 `<uri>` entries. Only `https://` and
  `mailto:` URIs are kept; `http://` and anything else is ignored. "Has unsubscribe" = at least one
  of them. So an `http://`-only header counts as no header: the sender is unverified (archived only,
  `unsubscribe_skipped_unverified`), the URL is never `POST`ed nor offered as a `manual_pending`
  link, and `is_mailing_list` in the Jev state depends on `List-Id` alone
  ([ADR 0025](decisions/0025-http-only-list-unsubscribe-is-none.md)).
- **One-click** = `List-Unsubscribe-Post` contains `List-Unsubscribe=One-Click` (spaces and case
  ignored) and there is at least one `https` URI.
- Order in `unsubscribe.unsubscribe`:
  1. `dmarc_pass` is false → `manual`/`skipped` (never reached from `run`, which checks first).
  2. With one-click: the first `https` URL that passes the guard gets a `POST` with form body
     `List-Unsubscribe=One-Click`, 15 s timeout, **no redirects**. The body is sent as
     `multipart/form-data`, the encoding RFC 8058 (section 3.2) says SHOULD be used; an answer of
     **415** (Unsupported Media Type) gets the same request **once** more, to the same address, as
     `application/x-www-form-urlencoded`, which it allows, and that answer (or error) is the result
     (logged as `one-click multipart refused (415), retried urlencoded`). A 415 is the one answer
     retried: the server refused the body's encoding without acting on it. Any other answer, or an
     error once the request was sent, may come from a server that acted, so it is the result
     ([ADR 0048](decisions/0048-one-click-multipart-then-urlencoded-on-415.md)). A 2xx or 3xx is
     `ok`. If it fails and a `mailto:` exists, go to step 3; if it fails and none exists, `failed`.
     A refused `POST` stores `HTTP <status> <reason>` (the standard reason phrase) as the error,
     plus the server's own message when its body is a JSON object with a `message`, `error` or
     `detail` string that says more: one line of printable ASCII without braces or links (any word
     with `://`), at most 120 characters. Only a body of at most 4 KB is read for it; any body that
     can't be read (huge, deeply nested, not UTF-8) leaves just `HTTP <status> <reason>`, so a sent
     `POST` is always recorded. The body itself is never stored: it is the sender's text. The digest
     shows `HTTP <status> <reason>` for a row whose error doesn't start with it (one stored before,
     which holds the raw body). URLs that fail the guard are skipped.
  3. The first `mailto:` with an `@` gets an email from your Gmail (subject and body from the URI,
     default `unsubscribe`). Sent → `ok`; any error → `failed` (its traceback is logged as
     `unsubscribe mail failed`).
  4. Otherwise `manual_pending` with the first `https` URL as target (listed in the digest).
- **URL guard** (`resolve_public_https`): scheme `https`, a hostname, and every address it resolves
  to is global (no private, loopback or link-local IPs); a DNS failure rejects it. The `POST` then
  connects to the validated addresses itself, in DNS order, with the URL's hostname as `Host` and
  for TLS (SNI and certificate check), so a second DNS answer can't redirect it to an internal host
  (DNS rebinding). An address that can't be connected to (refused, connect timeout, TLS failure) is
  skipped for the next one; once a request was sent, its answer is the result. The client ignores
  proxy environment variables (`HTTPS_PROXY`).
- Links in the body are never followed.

### 4.4 `List-Post`

Any non-empty `List-Post` header marks a discussion list (RFC 2369; set by Google Groups, Mailman,
groups.io, LISTSERV, Discourse; not by ESPs or sequence tools). Its `List-Unsubscribe` would remove
*you* from the list, so it is never used.

Its `mailto:` addresses (`signals.list_addresses`; query and percent-encoding removed, lowercased,
plain addresses only), with the address of Google Groups' `Mailing-list: list <address>`, are the
list's own addresses: an email whose resolved sender is one of them is sent as the list
([3.5](#35-sender-rules)). `List-Post: NO` (an announce-only list) and `https:` posting pages name
none. `List-Id` isn't used: it is a dotted identifier (`<hello.acme.com>`), not an address.

### 4.5 `X-Original-Sender` trust

`signals.original_sender` records a candidate author (`group_author`) only when:

- `dmarc=pass` for the message (so for the group's `From` domain);
- the first `Mailing-list` header starts with `list <address>` and that address equals the `From`
  address;
- `X-Original-Sender` is a valid address different from `From`.

`signals.resolve_relay` then moves the candidate into `sender` (and the group into `relayed_by`)
only when the `From` domain is one of the account's `group_domains` or a subdomain of one. Any
candidate, trusted or not, makes the email a group for the unsubscribe check. When a check fails,
the sender stays the group's address, which is the list's own address: no learned rule decides the
email and none is learned from it ([3.5](#35-sender-rules)).

The first check reads the group's own DMARC, as Gmail's header gives it; the author's authentication
never takes part in it. Once the author is the sender, `dmarc_pass` and Jev's `sender_auth` become
the author's, from Google's ARC record or unknown without one ([4.2](#42-dmarc)). A relayed author
is still never unsubscribed (condition 5 of [3.3](#33-unsubscribe-conditions)); one whose DMARC
doesn't pass stops at condition 4 first, so its row has `unsubscribe_skipped_unverified`.

### 4.6 Body extraction

`body.extract_body(payload)` walks the MIME tree:

- Parts with a filename (attachments) are skipped.
- Within each `multipart/alternative` that has a `text/plain` part, its plain text is used, **unless
  it is a stub**: plain text shorter than **300** characters (URLs removed before counting) next to
  HTML whose stripped text (URLs removed) is at least **3×** as long, with a minimum of 3 × 20 =
  **60** characters. An empty plain part always yields to its HTML. The check only compares an
  alternative's own plain and HTML versions, so a note you wrote above a forwarded HTML message is
  never dropped.
- At the top level, all text segments are joined. Only when there is no text at all are the
  remaining HTML parts (those not already resolved inside an alternative) stripped and used.
- HTML is stripped by dropping `<script>`, `<style>` and `<head>` content, joining text nodes with
  spaces and unescaping entities.
- Normalisation: runs of spaces, tabs, no-break spaces (U+00A0) and zero-width non-joiners (U+200C)
  become one space; trailing whitespace before a newline is removed; the result is stripped.

If the body is empty after all that, Jev gets Gmail's `snippet` (HTML-unescaped) instead.

The stripped text has no link targets, so `body.extract_link_domains(payload)` collects them
separately for Jev's `link_domains`: the `href` of every `<a>` and `<area>` in the HTML parts and
every `http(s)://` URL in the plain parts (trailing punctuation dropped), attachments skipped. Each
becomes its registrable domain ([4.9](#49-public-suffix-list)), or the address itself for an IP
host; other schemes (`mailto:`, `tel:`, anchors), malformed URLs and hosts nobody owns are dropped.
A URL is read as a browser reads it (WHATWG URL Standard): surrounding spaces and control
characters, tabs and newlines are removed, `\` counts as `/`, `https:/host`, `https:host` and
`https:\\host` reach the host, a scheme-relative `//host` is `https`, a percent-encoded host is
decoded, the host is mapped as IDNA (UTS #46) maps it: the code points it ignores (soft hyphen,
zero-width space, word joiner, variation selectors, byte order mark…) are dropped, then NFKC with
`。`, `．` and `｡` as dots (`ｅvil。example` is `evil.example`, and so is `evil.example` with a U+200B
inside, `１２７.0.0.1` is `127.0.0.1`), and a host whose last label is a number is read as IPv4 the way
browsers read it (`0x7f.1` and `2130706433` are `127.0.0.1`). A host longer than DNS allows, over
**253** characters or with a label over **63** (`HOSTNAME_MAX`, `LABEL_MAX`, measured in punycode),
is dropped, so no link can make the state longer than a domain. The distinct domains are sorted most
frequent first (ties in order of appearance) and capped at **8** (`LINK_DOMAINS_MAX`). Only domains
leave the module, never a path or query string: those can carry the recipient's address or a login
token. One caveat: on a private suffix of the list (`github.io`, `web.app`, `workers.dev`) the
registrable domain includes the site's own label, which can name a person (`jane.github.io`) or be a
per-recipient token (`<token>.workers.dev`); Jev already gets `recipient`, so this adds little.

`body.calendar_method(payload)` reads, from the same `full` payload, the iTIP method (RFC 5546) of a
calendar part: the first `text/calendar` part, anywhere in the tree (an attachment too), whose own
Content-Type carries a `method` parameter (iMIP, RFC 6047:
`text/calendar; charset="UTF-8"; method=REPLY`), uppercased. Google Calendar and Outlook write it on
invitations and updates (`REQUEST`), responses (`REPLY`), cancellations (`CANCEL`) and proposed new
times (`COUNTER`). The value comes from the sender, so only an RFC 5545 token (letters, digits and
`-`, at most 32 characters) is a method. An RFC 2231-encoded parameter (`method*=us-ascii''REPLY`)
counts by its percent-decoded value, and the charset it names is never used to decode (a sender's
`idna` or `undefined` would fail the read). Any other value (control or escape characters, a 100 KB
string) is no method, and the email counts toward rules like any other. It is stored on the email as
`Email.calendar_method` (None otherwise, and for a header-only read), never sent to Jev, and only
keeps the email out of auto rules ([3.5](#35-sender-rules)). `dry_classify.py` shows it as
`calendar=<METHOD>`.

### 4.7 What Jev receives

`classifier.build_state` sends:

| Key | Value |
| -- | -- |
| `recipient` | The account's address |
| `from`, `to`, `cc`, `subject` | First header value (raw; for a group, `from` shows the group) |
| `date` | Gmail's internal date, ISO 8601 UTC |
| `is_mailing_list` | `List-Id` present or a usable `List-Unsubscribe` (`https`/`mailto`) |
| `precedence` | `Precedence` header or null |
| `body` | Extracted body, else the snippet, first `BODY_CHARS` (3000) characters |
| `original_sender`, `sent_via_group` | Only for a trusted group relay: the author and the group |
| `previous_contact` | Only when known (`None` leaves the key out); always left out of the flags-only call for allowlist and keep-rule emails |
| `reply_without_thread` | `true` when the subject starts with a reply or forward prefix (`RE:`, `Fwd:`, `AW:`, `WG:`, `RV:`, `RES:`, `TR:`, `SV:`, `VS:`, `ANTW:`, `ODP:`, `ENC:`, any case, numbered or repeated) and the email has neither `In-Reply-To` nor `References` (`signals.reply_without_thread`); left out otherwise |
| `link_domains` | The domains the body's links point to, most frequent first, at most 8 ([4.6](#46-body-extraction)); left out when there is none |
| `sender_auth` | `{"spf", "dkim", "dmarc"}` verdicts from Gmail's own `Authentication-Results` ([4.2](#42-dmarc)); left out without one. For a trusted group relay, the author's, from Google's ARC record, all `none` without a trusted one, never the group's |

Questions (`build_questions`): one `Choice` over the categories, with each category's `criteria` and
the instruction to judge by who wrote it and why, explaining `previous_contact` ("a personal-looking
pitch from a sender I never wrote to is usually cold outreach") and the phishing signals (a passing
`sender_auth` only proves which domain sent the email, and DMARC `none` or `fail` strengthens other
signs; for an email sent via a group, `sender_auth` is the original sender's, for their own domain;
`reply_without_thread` is a reply subject answering nothing; links to a brand unrelated to the
sender are a sign of phishing, while a service linking to its own brand or portal domains, even ones
that differ from the sending domain, is normal,
[ADR 0042](decisions/0042-phishing-signals-for-jev.md)); one `Noul` for `needs_reply`, whose
instructions are `needs_reply.question` with its yes / no examples
([3.2](#32-confidence-outcomes-jev-path)); one `Noul` per tag. The answer gives the category, its
confidence, every category's probability, `needs_reply` and the tag probabilities.

### 4.8 Machine and no-reply addresses

One set of pure helpers in `signals.py`, used by both guards. When unsure, an address is **not** a
machine: a false "machine" hides a real contact, which archives (and unsubscribes) more, the
dangerous direction ([ADR 0032](decisions/0032-machine-addresses-make-no-known-contact.md)).

| Helper | Yes | No (examples) |
| -- | -- | -- |
| `is_no_reply_address` | Local part `noreply`, `no-reply`, `no_reply`, `donotreply`, `do-not-reply`, `do_not_reply`, alone or as one piece of a local part split on `.` `_` `+` `-` (`messages-noreply@`, `noreply-billing@`, `alerts.no-reply@`) | `norah@`, `noreplies@`, `reply@`, `notifications@`, `info@`, `jane@noreply.com` |
| `is_automated_address` | Any no-reply address; local part `bounce`/`bounces` alone, followed by `+` or `=`, or followed by `.` `_` `-` and a token with 3+ digits (`bounce-12345-67@`); `reply+<token>`; `reply-<token with 3+ digits>`; `mailer-daemon`, `postmaster`; a host with a `bounce`, `bounces` or `reply` label left of its registrable domain (`m-a175…@bounce.linkedin.com`, `reply+…@reply.github.com`, `x@bounce.linkedin.co.uk`, `x@bounce.sap.de`) | `reply@company.com`, `reply-team@`, `reply-all2@`, `replies@`, `bouncer@`, `bounce.house@`, `bounce-rentals@`, `sam@reply.io`, `ana@reply.com.ar`, `jane@reply.co.uk`, `kim@reply.wa.gov.au`, `lee@reply.blogspot.com`, `notifications@github.com` |
| `Signals.no_reply` | The (resolved) sender is a no-reply address **and** every `Reply-To` address is too (or there is none) | A `Reply-To` to anything else (a person, a help desk, a reply-by-email token) |

- **Registrable domain**: the host's public suffix in the
  [Public Suffix List](#49-public-suffix-list) plus one label (`psl.registrable_domain`):
  `linkedin.com`, `linkedin.co.uk`, `reply.com.ar`, `reply.wa.gov.au`, `reply.blogspot.com`,
  `sap.de`. A host that is itself a suffix has none, so none of its labels count. Short names under
  a ccTLD are names (`bounce.sap.de`, `bounce.adv.ai` are machine hosts), and a company named
  `reply` or `bounce` under any listed suffix, however deep, is a person (`kim@reply.wa.gov.au`,
  `lee@reply.blogspot.com`).
- `is_automated_address` is used by `previous_contact`: a machine sender isn't searched for itself,
  and the domain search ignores sent emails whose recipients on the domain are all machines. In the
  domain search the labels of the searched domain (the sender's) never count either, so a person at
  `reply.com.ar` counts for `reply.com.ar` whatever the list says.
- `no_reply` is used by the archive group's needs-reply veto
  ([3.2](#32-confidence-outcomes-jev-path)). `Jev/Needs Reply` itself is unchanged: Jev's question
  covers "a reply or an action", and a no-reply email can still ask for an action.

### 4.9 Public Suffix List

`inbox_jev.psl` answers "is this a public suffix" (`group_domains`,
[3.4](#34-groups-and-mailing-lists)) and "what is this host's registrable domain" (machine hosts,
[4.8](#48-machine-and-no-reply-addresses)) from the [Public Suffix List](https://publicsuffix.org)
shipped with the package (`src/inbox_jev/public_suffix_list.dat`; its header names the `VERSION` and
`COMMIT`). It is read once, on first use, and never fetched at runtime
([ADR 0037](decisions/0037-public-suffix-list-for-suffixes-and-registrable-domains.md)).

| Rule | Example | Effect |
| -- | -- | -- |
| Normal | `co.uk`, `wa.gov.au` | the name is a suffix; the longest matching rule wins |
| Wildcard `*.` | `*.ck` | every child is a suffix: `foo.ck` |
| Exception `!` | `!www.ck`, `!city.kawasaki.jp` | wins over any other rule; the suffix is the rule without its leftmost label: `www.ck` is a domain |
| None matches | `example.internal` | the TLD alone is the suffix (the default `*`) |

- **Both sections count, for both uses**: ICANN (registry suffixes: `com.ar`, `wa.gov.au`) and
  PRIVATE (platforms that hand out subdomains: `github.io`, `blogspot.com`). For `group_domains` a
  platform suffix is as public as a registry one; for machine hosts it makes the registrable domain
  longer, so fewer hosts are machines (the safe side).
- **IDN**: names are compared as lowercase ASCII, each Unicode label as its punycode A-label, so
  `公司.香港` and `xn--55qx5d.xn--j6w193g` are the same suffix.
- **Malformed names**: a name with an empty label (`.com`, `.example.com`, `a..com`) has no
  registrable domain, as the list's own test vectors require: `group_domains` refuses it (its format
  check already does) and a host like that never makes a machine address.
- **Freshness**: the weekly `Update public suffix list` workflow (`scripts/update_psl.py --check`)
  opens an issue labeled `psl-update` when a newer list is out; a session ships it as a normal PR.
  Until that merges, the shipped version decides.

## 5. Worked examples

Addresses are fictional. The account is `me@example.com` with `group_domains: [example.com]`, no
allowlist unless stated, shipped `categories.yaml`, `MODE=act`, no `act_categories`. Only the
probabilities that matter are shown; the rest are ~0.

### EX-01 — Tool digest split between notification and newsletter

- **Test**: `test_ex01_tool_digest_split_is_archived_by_the_group`
- **Input**: weekly product-analytics digest from `digest@analytics.example`. Jev: notification
  0.55, newsletter 0.42, marketing 0.02, important 0.01; `needs_reply` 0.05.
- **Outcome**: archived as `notification` with `Jev/Notifications`; `archive_group = true`; no
  Review, no unsubscribe; doesn't count toward an auto rule.
- **Why**: notification alone (0.55) is below 0.80, but the archive categories add up to 0.99 ≥ the
  floor max(0.90, 0.80). Jev is sure the email goes away, just not under which label. With
  `act_categories: [notification]`, only notification would count (0.55) and the email would go to
  Review.

### EX-02 — Automated payment approval

- **Test**: `test_ex02_payment_approval_stays_with_action_label`
- **Input**: `alerts@bank.example`, "Approve payment to Acme Supplies". Jev: action_required 0.93,
  transactional 0.05, important 0.02; `needs_reply` 0.85.
- **Outcome**: stays in the inbox with `Jev/Action` and `Jev/Needs Reply`; listed in the digest's
  `Jev/Action` section (and not repeated under "needs reply").
- **Why**: `action_required` is a keep category (threshold 0.0). Needs Reply is added because the
  email isn't archived. The answer doesn't count toward an auto rule (0.93 < 0.95, and important
  0.02 is a keep signal), so this sender keeps going through Jev.

### EX-03 — Bulk event newsletter with a "client doesn't support HTML" stub

- **Test**: `test_ex03_stub_plain_part_is_replaced_by_the_html`
- **Input**: `multipart/alternative` from an ESP: `text/plain` = "Your mail client does not support
  HTML messages. Copy and paste this URL into your browser:" plus a ~250-character tracking URL;
  `text/html` = the event's agenda and speakers. Jev (on the HTML text): newsletter 0.91, marketing
  0.09.
- **Outcome**: Jev gets the HTML's text as `body`; archived as `newsletter` on its own confidence
  (not through the group).
- **Why**: the plain part is under 300 characters once the URL is removed, and its HTML sibling has
  more than 3× (and ≥ 60) characters of text. Before this rule Jev classified the stub blind (split
  cold_outreach / spam / marketing → Review). A short real reply next to similar HTML is kept as is.

### EX-04 — Cold-outreach sequence from a stranger, RFC 8058 headers

- **Test**: `test_ex04_stranger_sequence_is_archived_and_unsubscribed`
- **Input**: `sam@growthagency.example`, "Re: quick question about your pipeline". DMARC pass,
  `List-Unsubscribe: <https://unsub.sequencer.example/u/abc>, <mailto:unsub@growthagency.example>`,
  `List-Unsubscribe-Post: List-Unsubscribe=One-Click`, no `List-Post`. `previous_contact` = `never`
  (both searches empty). Jev: cold_outreach 0.96.
- **Outcome**: archived with `Jev/Outreach`; one-click `POST` to the https URL; `unsubscribes` row
  `one_click` / `ok`; the `mailto:` isn't used.
- **Why**: cold_outreach ≥ 0.90, verified sender, not a group, a stranger, act mode, budget left,
  list not settled. Sequence tools stop the sequence on unsubscribe.

### EX-05 — The same sequence from someone you wrote to

- **Test**: `test_ex05_known_contact_sequence_is_archived_only`
- **Input**: as EX-04, but your sent mail has an email to `sam@growthagency.example`
  (`previous_contact` = `wrote_to_sender`; `wrote_to_their_domain` behaves the same).
- **Outcome**: archived with `Jev/Outreach`; **no** unsubscribe (`known_contact_kept`); the answer
  doesn't count toward an auto rule.
- **Why**: `cold_outreach` has `unsubscribe_known_contacts: false`. A "pitch" from a known contact
  is where Jev is most likely wrong, and an opt-out can cover all of that company's mail. Keeping
  the sender on the Jev path means the relationship is seen on every email.

### EX-06 — A pitch relayed by your own Google Group

- **Test**: `test_ex06_pitch_via_own_group_uses_author_and_never_unsubscribes`
- **Input**: `From: "'Dana' via Hello" <hello@example.com>`,
  `X-Original-Sender: dana@pitchco.example`,
  `Mailing-list: list hello@example.com; contact hello+owners@example.com`,
  `List-Id: <hello.example.com>`, `List-Post: <mailto:hello@example.com>`,
  `List-Unsubscribe: <mailto:googlegroups-manage+1+unsubscribe@googlegroups.com>`, DMARC pass. The
  account also has `allow_domains: [example.com]`. Jev: cold_outreach 0.97.
- **Outcome**: sender = `dana@pitchco.example` (Jev gets `original_sender` and `sent_via_group`;
  `previous_contact` searches for Dana; the DB, digest and any auto rule use Dana). Archived with
  `Jev/Outreach`; **never** unsubscribed.
- **Why**: `example.com` is in `group_domains` and the trust checks pass, so the author is
  recovered. `allow_domains` matches the author, not the group, so the stranger isn't allowlisted.
  The `List-Unsubscribe` would remove *you* from `hello@`: relayed mail, `List-Post`, a Google
  Groups target and an own-domain list are each enough to block it.

### EX-07 — Google Groups held-spam report

- **Test**: `test_ex07_held_spam_report_is_archived_as_notification`
- **Input**: the group's "Moderator's spam report" for `hello@example.com`, sent by the group itself
  (no `X-Original-Sender`). Jev: notification 0.90, action_required 0.06.
- **Outcome**: archived with `Jev/Notifications` (an `archive` category: no unsubscribe).
- **Why**: the `notification` criteria include automated held-spam reports and `action_required`
  excludes them. A sender rule would be the wrong fix: the sender is the group, which also carries
  real mail. (If `allow_domains` covered `example.com`, the group address itself would be
  allowlisted and the report would stay as `important`.)

### EX-08 — Google Groups pending-approval notice

- **Test**: `test_ex08_pending_approval_notice_stays_actionable`
- **Input**: "A message to hello@example.com is pending your approval", sent by the group. Jev:
  action_required 0.96, notification 0.03; `needs_reply` 0.75.
- **Outcome**: stays in the inbox with `Jev/Action` and `Jev/Needs Reply`.
- **Why**: `action_required` explicitly covers a held message pending your approval: it is the only
  way a non-member (a prospect writing to `hello@`) gets delivered.

### EX-09 — Mass "free consultation" pitch that "expects a reply"

- **Test**: `test_ex09_mass_pitch_expecting_reply_is_group_archived`
- **Input**: "Get Your Free Consultation Today!" from a bulk sender, `previous_contact` = `never`.
  Jev: cold_outreach 0.67, marketing 0.31, important 0.00, action_required 0.00; `needs_reply` 0.79.
- **Outcome**: archived as `cold_outreach` through the archive group; no Review, no
  `Jev/Needs Reply`, no unsubscribe.
- **Why**: archive categories sum to 0.98 ≥ 0.90. `needs_reply` ≥ 0.7, but the sender is not a known
  contact and the keep categories (0.00) are below 0.05, so the veto doesn't apply: every pitch
  "expects a reply". The group never unsubscribes (0.67 < 0.90).

### EX-10 — A known vendor's sales follow-up that expects a reply

- **Test**: `test_ex10_known_vendor_follow_up_stays_in_review`
- **Input**: same answer as EX-09 (cold_outreach 0.67, marketing 0.31; `needs_reply` 0.79), but from
  `rep@vendor.example`, a domain you have written to (`previous_contact` = `wrote_to_their_domain`).
- **Outcome**: stays in the inbox with `Jev/Review`, `Jev/Outreach` and `Jev/Needs Reply`.
- **Why**: for a known contact any reply signal vetoes the archive group, and cold_outreach alone is
  below 0.90. The same holds for a stranger when important + action_required reach 0.05.

### EX-11 — Cold outreach that can't prove who it is

- **Test**: `test_ex11_unverified_cold_outreach_is_archived_never_unsubscribed`
- **Input**: `founder@devshop.example`, no `List-Unsubscribe` (or DMARC not `pass`). Jev:
  cold_outreach 0.97.
- **Outcome**: archived with `Jev/Outreach`, `unsubscribe_skipped_unverified = true`; never
  unsubscribed and never moved to Spam. It still counts toward an auto rule, because
  `previous_contact` is `never`. From a known contact it would be archived the same way, with
  `known_contact_kept = false` (the unverified check is what stopped the unsubscribe), and it
  wouldn't count toward an auto rule. A `List-Unsubscribe` with only an `http://` URL is treated
  like no header.
- **Why**: unsubscribing from an unverified sender only confirms your address; being unverified
  isn't fraud. A rule for it is safe: the rule path re-runs the sender checks on every email. Known
  contacts stay on the Jev path whatever the headers say.

### EX-12 — A correction becomes a sender rule

- **Test**: `test_ex12_pulling_back_to_inbox_becomes_a_rule`
- **Input**: in act mode, `news@weekly.example` is archived as `newsletter` (0.97). You move it back
  to the inbox. A week later another email from that sender arrives.
- **Outcome**: the next run records a correction to `important` and an active `correction` rule for
  `news@weekly.example`. The new email is decided by the rule and stays in the inbox with
  `Jev/Important`; since `important` is a keep category, Jev is asked only for its flags (Needs
  Reply, tags) and its own category is ignored.
- **Why**: pulling an archived email back to the inbox is a correction to `important`; changing the
  category label would be a correction to that category.

### EX-13 — An email that keeps failing

- **Test**: `test_ex13_failing_email_backs_off_then_gives_up`
- **Input**: in act mode, every call to Jev for `billing@vendor.example`'s "Invoice 7" fails. The
  timer runs every 10 minutes.
- **Outcome**: the first run stores the email as `error` (1 failure) and the run carries on with the
  other emails. A run less than 5 minutes later doesn't retry it; later runs retry it 5, 10, 20, 40,
  80, 160 and 320 minutes after each failure. After the 8th failure it is never retried again: it
  stays in the inbox with no Jev labels, and that day's digest lists it under "Sin más reintentos".
  If Jev answers on a retry before that, the email is processed normally and its failure count goes
  back to 0. `inbox-jev retry-errors` makes the next run try it again.
- **Why**: retrying every 10 minutes for the whole `LOOKBACK` (up to 288 times in 2 days) costs Jev
  calls and log noise for an email that fails the same way; the backoff still rides out an outage of
  several hours ([ADR 0017](decisions/0017-error-retry-backoff-and-cap.md)).

### EX-14 — A rule to a category you removed

- **Test**: `test_ex14_rule_to_removed_category_goes_through_jev`
- **Input**: a manual rule `@deals.example` → `promotions`, a category since removed from
  `categories.yaml`. Two emails from `sale@deals.example` arrive in one run. Jev: newsletter 0.93,
  marketing 0.07. Also `@vendor.example` → `marketing` (manual) with the exception
  `billing@vendor.example` → `promotions` (manual, same removed category); `billing@` sends "Invoice
  88". Jev: important 0.96.
- **Outcome**: the two `sale@` emails go through Jev and are archived as `newsletter`. "Invoice 88"
  goes through Jev too and stays in the inbox as `important`: the `@vendor.example` rule doesn't
  apply. The run logs `sender rule ignored: unknown category` once for `@deals.example` and once for
  `billing@vendor.example`. The rules stay in the DB, active.
- **Why**: before, `decide` raised and the emails were stored as `error`, retried with the backoff
  until they gave up. Treating the rule as absent decides them the way they would be without it;
  leaving it in place keeps a manual or correction rule you may still want if the category comes
  back. Falling through to `@vendor.example` would apply the very rule the exact one overrode,
  without Jev, in `act` ([ADR 0019](decisions/0019-rules-to-removed-categories-ignored.md)).

### EX-15 — A client with a keep rule asks a question

- **Test**: `test_ex15_keep_rule_still_gets_needs_reply`
- **Input**: `ana@client.example` has a `correction` rule → `important`; `news@weekly.example` has
  an active `auto` rule → `newsletter`. Ana writes "Signed contract by Friday?". Jev: cold_outreach
  0.55, important 0.45; `needs_reply` 0.92.
- **Outcome**: Ana's email stays in the inbox with `Jev/Important` and `Jev/Needs Reply`, and is
  listed in the digest's Needs reply section ("Piden respuesta" in `es`). Jev's cold_outreach answer
  is ignored (no `Jev/Outreach`), the answer counts toward no rule, and no sent-mail search runs.
  The newsletter is archived by its rule without a Jev call.
- **Why**: a keep rule (or the allowlist) is exactly the mail that needs a reply most, and its email
  stays in the inbox, so Needs Reply and the tags are worth one Jev call. An archive rule takes the
  email out of the inbox, where Needs Reply is never added, so it still skips Jev
  ([ADR 0020](decisions/0020-flags-for-allowlist-and-keep-rules.md)).

### EX-16 — The archive group respects a stricter category threshold

- **Test**: `test_ex16_archive_group_respects_category_thresholds`
- **Input**: `categories.yaml` raises `marketing`'s `min_confidence` to 0.95 (shipped: 0.90).
  `hello@brand.example` sends an announcement. Jev: newsletter 0.50, marketing 0.45, important 0.01.
- **Outcome**: stays in the inbox with `Jev/Review` and `Jev/Newsletters`. With
  `min_confidence: {marketing: 0.90}` on the account, marketing counts again and the email is
  archived as `newsletter` through the group.
- **Why**: a member whose threshold is above the floor (0.90) doesn't count, whether that threshold
  comes from the account or from the category; before, only the account's counted
  ([ADR 0021](decisions/0021-archive-group-member-threshold.md)). With the shipped thresholds (0.80
  and 0.90) every archive category counts.

### EX-17 — An unsubscribe header without Gmail's Authentication-Results

- **Test**: `test_ex17_no_gmail_authentication_results_is_unverified`
- **Input**: `promo@shop.example` with `List-Unsubscribe: <https://shop.example/u/1>` and one-click,
  and a single `Authentication-Results: mail.shop.example; dmarc=pass …` (no `mx.google.com`
  header). Jev: marketing 0.97.
- **Outcome**: archived with `Jev/Marketing`, `unsubscribe_skipped_unverified = true`; no one-click
  `POST`, no `unsubscribes` row.
- **Why**: Gmail stamps every inbound email with its own header; one that is missing means the only
  verdict was written by someone else, possibly the sender
  ([ADR 0022](decisions/0022-dmarc-only-from-gmail.md)).

### EX-18 — A dry-run trial learns no rules

- **Test**: `test_ex18_dry_run_learns_no_rules`
- **Input**: three issues from `news@weekly.example` in the inbox, each newsletter 0.97. A `dry_run`
  pass, then an `act` pass.
- **Outcome**: after `dry_run` there are no sender rules. The `act` pass asks Jev again for the
  three (dry-run rows are processed again), archives them, and only then the `auto` rule reaches 3
  hits and activates.
- **Why**: a rule learned in `dry_run` would apply without Jev, and act, as soon as the mode
  changes, on answers nobody could correct (dry runs add no labels)
  ([ADR 0024](decisions/0024-no-auto-rules-in-dry-run.md)).

### EX-19 — Jev is down when an allowlisted email arrives

- **Test**: `test_ex19_flags_only_jev_failure_still_labels`
- **Input**: `acme.example` is on `allow_domains`, and `ana@client.example` has a `correction` rule
  → `important`. Each sends an email while every Jev call fails.
- **Outcome**: both are stored `done` (not `error`) with `Jev/Important`, without `Jev/Needs Reply`
  or tags, and stay in the inbox. The log has `flags-only Jev call failed` for each (account, id,
  error type; no subject or body). No error is counted for them.
- **Why**: the category was already decided by the allowlist or the rule; only the optional flags
  depend on Jev. Failing the email would make "always important" depend on Jev, and after 8 failed
  passes it would never be labeled
  ([ADR 0020](decisions/0020-flags-for-allowlist-and-keep-rules.md)).

### EX-20 — Restore with an email deleted in Gmail

- **Test**: `test_ex20_restore_skips_emails_deleted_in_gmail`
- **Input**: in act mode, `news@weekly.example`'s issue (newsletter 0.97) and `promo@shop.example`'s
  sale (marketing 0.97) are archived. You delete the newsletter in Gmail, then run
  `inbox-jev restore -a me@example.com --since 1d`.
- **Outcome**: the sale goes back to the inbox without Jev labels and its row is `restored`. The
  deleted newsletter is skipped (Gmail answers 404), its row is left as it was, and the command says
  `Restored 1 emails. Skipped 1 deleted in Gmail.` Running it again restores nothing more and skips
  the newsletter again.
- **Why**: before, the first deleted email stopped the command with an error, and since its row was
  never marked restored, every later attempt stopped at the same email: nothing after it in the
  window could be undone. Skipping only 404s keeps any other Gmail failure visible
  ([ADR 0027](decisions/0027-restore-skips-deleted-emails.md)).

### EX-21 — Jev's API key is revoked

- **Test**: `test_ex21_revoked_jev_key_stops_jev_but_not_rules`
- **Input**: in act mode, Jev answers 401 to every call. The inbox has, in this order: a pitch from
  `sam@growthagency.example` (no rule), Lea's "Budget review" from the allowlisted `acme.example`,
  `news@weekly.example`'s issue (manual rule → newsletter) and Ana's "Signed contract by Friday?"
  (no rule).
- **Outcome**: Jev is called once, for Sam's pitch. The 401 stops the pass from asking Jev: Sam's
  and Ana's emails are left untouched in the inbox, not stored and not counted as failures. Lea's
  email still gets `Jev/Important` (no flags, no Jev call) and the newsletter is archived by its
  rule. The run fails with "Jev rejected the API key or model (TypeSafeAuthenticationError (HTTP
  401)): check TYPESAFE_API_KEY and TYPESAFE_MODEL; …", `inbox-jev run` exits 1 and the digest lists
  the failed run. Once the key is fixed, the next run decides Sam's and Ana's emails as if nothing
  happened.
- **Why**: before, every email in the window was sent to Jev, failed and spent one of its 8 attempts
  on a problem that wasn't the email's, and the run exited 0, every 10 minutes. Rules and the
  allowlist don't need Jev, so they keep working
  ([ADR 0030](decisions/0030-stop-asking-jev-when-it-is-down.md)).

### EX-22 — Jev is down for a whole pass

- **Test**: `test_ex22_jev_down_stops_after_three_failures`
- **Input**: in act mode, five emails with no rule arrive while every Jev call times out (after the
  SDK's own retries).
- **Outcome**: the first three are stored as `error` with one failure each (the usual backoff).
  After the third failure in a row the pass stops asking Jev: the other two aren't sent, stored or
  counted. The run fails with "Jev unavailable: 3 calls in a row failed (last:
  TypeSafeAPITimeoutError: …); emails that need Jev wait for the next run". When Jev is back, the
  next run decides the two that waited; the first three follow their backoff.
- **Why**: with Jev down, each email costs a call plus its retries (up to 30 s) and a failure;
  `MAX_MESSAGES_PER_RUN` of them per pass made each pass slow and burned attempts on emails that had
  nothing wrong with them. Three in a row tells an outage from one bad email
  ([ADR 0030](decisions/0030-stop-asking-jev-when-it-is-down.md)).

### EX-23 — Jev refuses one email

- **Test**: `test_ex23_jev_refuses_one_email`
- **Input**: in act mode, Jev answers 422 to `billing@vendor.example`'s "Invoice 7" (its error
  message quotes part of the email), and normally to the other email, which comes next.
- **Outcome**: Jev's answer to the next email shows the refusal was this email's. "Invoice 7" is
  stored as `error` with `TypeSafeUnprocessableEntityError (HTTP 422, request_id=…)`, never Jev's
  message, and gives up at once (8 failures): later runs don't send it again, and the digest lists
  it under "Sin más reintentos" by its subject and sender ("Invoice 7 — billing@vendor.example").
  The other email is processed and the run doesn't fail. `inbox-jev retry-errors` makes the next run
  try it again.
- **Why**: Jev would refuse the same request on every retry, so the backoff only adds calls; and its
  message can echo the email, which must never be stored or logged
  ([ADR 0030](decisions/0030-stop-asking-jev-when-it-is-down.md)).

### EX-24 — Replies to notifications don't make a contact

- **Test**: `test_ex24_replies_to_notifications_make_no_known_contact`
- **Input**: "Guillermo, you have 107 new lead suggestions" from `messages-noreply@social.example`
  (the real case was LinkedIn). Your sent mail has 12 emails from 2021 to
  `m-a175…@bounce.social.example` (replies to notifications), none to a person at `social.example`.
  Jev: notification 0.62, newsletter 0.36, important 0.01; `needs_reply` 0.75.
- **Outcome**: `previous_contact` = `never`: the sender isn't searched (a machine address), and the
  domain search reads the `To`/`Cc`/`Bcc` of the 12 matches and finds only machine addresses. The
  email is archived as `notification` through the archive group, without Review or
  `Jev/Needs Reply`. With one more sent email to `jane@social.example` it would be
  `wrote_to_their_domain` again (one read: the newest match).
- **Why**: before, any sent email matching `to:social.example` made every email from that domain a
  known contact, so a reply signal of 0.75 kept a routine digest in Review. Writing to a bounce
  address is replying to a machine, not knowing someone
  ([ADR 0032](decisions/0032-machine-addresses-make-no-known-contact.md)).

### EX-25 — A no-reply sender from a domain you write to

- **Test**: `test_ex25_no_reply_sender_reply_signal_is_judged_like_a_strangers`
- **Input**: `noreply@vendor.example`, a domain you have written to (`previous_contact` =
  `wrote_to_their_domain`), no `Reply-To`. Jev as EX-24: notification 0.62, newsletter 0.36,
  important 0.01; `needs_reply` 0.75.
- **Outcome**: archived as `notification` through the archive group. The same email with
  `Reply-To: Ana <ana@vendor.example>` stays in Review with `Jev/Notifications` and
  `Jev/Needs Reply`, as EX-10. With action_required 0.06 (transactional 0.32) instead it also stays
  in Review, with `Jev/Needs Reply`.
- **Why**: a reply to a no-reply address reaches no one, so knowing the domain says nothing about
  whether this email needs one; its reply signal is judged like a stranger's, by the keep categories
  (an action may still be needed). A `Reply-To` elsewhere makes it replyable again
  ([ADR 0032](decisions/0032-machine-addresses-make-no-known-contact.md)).

### EX-26 — Writing to a friend on country free mail makes no known contact

- **Test**: `test_ex26_writing_to_a_friend_on_country_free_mail_makes_no_known_contact`
- **Input**: a sequence pitch from `growth.lead@hotmail.com.ar` ("Escalá tus ventas"), DMARC pass,
  `List-Unsubscribe: <https://unsub.sequencer.example/u/xyz>` with one-click. Your sent mail has
  emails to your cousin `primo@hotmail.com.ar`, none to the sender. Jev: cold_outreach 0.96.
- **Outcome**: `previous_contact` = `never`: only the address is searched, not the domain. Archived
  with `Jev/Outreach` and unsubscribed (one-click `POST`), as EX-04.
- **Why**: `hotmail.com.ar` is free mail like `hotmail.com`: anyone can get an address there, so
  having written to one person on it says nothing about the others. Before, it was searched as a
  company domain, so `wrote_to_their_domain` made every `hotmail.com.ar` sender a known contact: no
  unsubscribe, the reply veto on the archive group, and no auto rule
  ([ADR 0033](decisions/0033-free-mail-country-domains-and-named-public-suffixes.md)).

### EX-27 — A public suffix in group_domains is refused

- **Test**: `test_ex27_a_public_suffix_in_group_domains_is_refused`
- **Input**: an account `me@exemple.gouv.fr` with `group_domains: [gouv.fr]`; then
  `group_domains: [exemple.gouv.fr]` and a Google Group email from
  `"'Dana' via Info" <info@autre.gouv.fr>` (`X-Original-Sender: dana@pitchco.example`, DMARC pass),
  and the same from `info@exemple.gouv.fr`. Also `group_domains: [com.ai]` and
  `group_domains: [adv.ai]`.
- **Outcome**: `gouv.fr` fails the accounts file at load ("is a public suffix, not a domain"). With
  `exemple.gouv.fr`, the group on `autre.gouv.fr` stays keyed on the group address (untrusted); only
  the own `exemple.gouv.fr` group is resolved to Dana. `com.ai` is refused too, but `adv.ai` loads:
  `.ai` is sold as a generic name, and `adv` isn't a public suffix there.
- **Why**: `gouv.fr` is a suffix every French public body registers under, not a domain anyone
  controls: trusting it would trust every `*.gouv.fr` group's claim about its author. Before, only
  `co.uk`-style suffixes from a short list were refused
  ([ADR 0033](decisions/0033-free-mail-country-domains-and-named-public-suffixes.md)). Only what the
  Public Suffix List lists is a suffix, so a real company under a ccTLD sold as a generic name isn't
  refused ([ADR 0037](decisions/0037-public-suffix-list-for-suffixes-and-registrable-domains.md)).

### EX-28 — Jev refuses every request the same way

- **Test**: `test_ex28_jev_refusing_everything_gives_no_email_up`
- **Input**: in act mode, a change on Jev's side makes it answer 422 to every request. The inbox
  has, in this order: `billing@vendor.example`'s "Invoice 7" and Sam's pitch (no rule), Lea's
  "Budget review" from the allowlisted `acme.example` and `news@weekly.example`'s issue (no rule).
- **Outcome**: Jev is called twice, for "Invoice 7" and Sam's pitch, and refuses both the same way
  (`TypeSafeUnprocessableEntityError`, HTTP 422). The pass stops asking Jev: both emails are stored
  as `error` with one ordinary failure each (`error_attempts` = 1, retried after 5 minutes), the
  issue isn't sent and isn't stored, and Lea's email gets `Jev/Important` without flags. The run
  fails with "Jev refused 2 requests in a row the same way (TypeSafeUnprocessableEntityError (HTTP
  422)): likely a change on Jev's side; emails that need Jev wait for the next run". Once Jev is
  fixed, the next run (10 minutes later, past the backoff) decides the three that waited, with no
  `retry-errors`.
- **Why**: before, each pass gave up on up to 3 emails that hit the refusal before it stopped (8
  failures each at once), so every 10-minute pass buried 3 more emails until someone ran
  `retry-errors`. One refused email still gives up at once (EX-23): the next call tells the two
  cases apart ([ADR 0035](decisions/0035-identical-refusals-stop-the-pass.md)).

### EX-29 — An email answered again counts once toward an auto rule

- **Test**: `test_ex29_an_email_answered_again_counts_once_toward_an_auto_rule`
- **Input**: `news@weekly.example`'s issues 1 and 2 arrive while the account is in `label`
  (newsletter 0.97 each). The account switches to `act`; then issue 3 arrives (newsletter 0.97).
- **Outcome**: after `label` the pending `auto` rule has 2 hits. The first `act` pass asks Jev again
  for issues 1 and 2 (their rows are from a weaker mode) and archives them, but the rule stays at 2
  hits, inactive. Issue 3 brings it to 3 and activates it.
- **Why**: an auto rule means "3 different emails from this sender were the same category". Before,
  the re-answer counted again, so 2 emails seen in `label` and again in `act` made an active rule:
  half the evidence it asks for ([ADR 0036](decisions/0036-auto-rule-hits-count-emails.md)).

### EX-30 — Two emails Jev refuses don't hold up the rest

- **Test**: `test_ex30_two_emails_jev_refuses_dont_hold_up_the_rest`
- **Input**: in act mode, passes every 10 minutes. The inbox has, newest first, two copies of
  `forms@survey.example`'s "Your response" (one message delivered to two of your aliases), which Jev
  refuses with a 422 every time, then `news@weekly.example`'s "Issue 41" (newsletter 0.97). Jev
  answers everything else.
- **Outcome**: the first pass asks Jev about both copies, is refused the same way twice and stops:
  each copy gets one ordinary failure, the issue waits and the run fails. The next two passes do the
  same (both copies are due again after 5, then 10 minutes): 3 failures each. Their third failure
  waits 20 minutes, so the fourth pass skips them, asks Jev about the issue and archives it, and the
  run succeeds. The copies are retried later and give up after 8 failures.
- **Why**: two identical refusals can't tell "Jev refuses everything" from "two bad emails in a
  row". Leaving both untouched (as before) made every pass stop on them forever, so no email behind
  them that needs Jev was decided until they left the lookback. An ordinary failure with backoff
  bounds that: while Jev really refuses everything, each pass charges only the two emails it asked
  about ([ADR 0035](decisions/0035-identical-refusals-stop-the-pass.md)).

### EX-31 — An email answered differently restarts a pending auto rule

- **Test**: `test_ex31_an_email_answered_differently_restarts_a_pending_auto_rule`
- **Input**: as EX-29, issues 1 and 2 from `news@weekly.example` are newsletter 0.97 in `label`.
  When the account switches to `act`, Jev answers issue 1 marketing 0.97 (issue 2 still newsletter
  0.97); then issue 3 arrives (newsletter 0.97).
- **Outcome**: after `label` the pending `auto` rule is newsletter with 2 hits. The `act` pass
  archives issue 1 as marketing; the re-answer adds no hit and doesn't switch the rule, but restarts
  it: newsletter, 0 hits. Issue 3 brings it to 1.
- **Why**: Jev disagreeing with itself about the same email is a sign the sender isn't consistent.
  Switching the rule to marketing at 1 hit would count the email a second time; leaving the hits as
  they were would ignore the disagreement
  ([ADR 0036](decisions/0036-auto-rule-hits-count-emails.md)).

### EX-32 — The Public Suffix List decides what a domain is

- **Test**: `test_ex32_the_public_suffix_list_decides_what_a_domain_is`
- **Input**: "Your weekly report" from `updates@acme.de`. Your sent mail has 12 emails to
  `m-77x…@bounce.acme.de` (replies to its notifications), none to a person at `acme.de`. Jev as
  EX-24: notification 0.62, newsletter 0.36, important 0.01; `needs_reply` 0.75. Also
  `kim@reply.wa.gov.au`, whom you wrote to, and accounts with `group_domains: [wa.gov.au]`,
  `[blogspot.com]`, `[sch.gg]`.
- **Outcome**: `bounce.acme.de` is a machine host (`acme.de` is the registrable domain), so
  `previous_contact` = `never` and the email is archived as `notification` through the archive
  group, without Review or `Jev/Needs Reply`. `kim@reply.wa.gov.au` is a person and is searched for
  herself: `wrote_to_sender`. `wa.gov.au` and `blogspot.com` fail the accounts file at load ("is a
  public suffix"); `sch.gg` loads.
- **Why**: before, a length rule guessed the registrable domain: `acme` (4 letters under a
  two-letter TLD) was taken as a suffix like `co.uk`, so `bounce.acme.de` was a company called
  "bounce" and the replies made `acme.de` a known contact (Review, as EX-24 before its fix); and
  `reply` under the three-level `wa.gov.au` was a machine label. `group_domains` refused only
  suffixes known by name, so `wa.gov.au` and `blogspot.com` loaded while `sch.gg` didn't. The
  vendored list answers each from what registries and platforms publish
  ([ADR 0037](decisions/0037-public-suffix-list-for-suffixes-and-registrable-domains.md)).

### EX-33 — A satisfaction survey from a no-reply sender

- **Test**: `test_ex33_satisfaction_survey_is_not_vetoed_by_needs_reply`
- **Input**: "GUILLERMO, we picked you to give us your opinion" from `noreply@health.example` (a
  no-reply sender, `previous_contact` = `wrote_to_their_domain`). Jev: marketing 0.87, newsletter
  0.05, transactional 0.03, action_required 0.04, important 0.01.
- **Outcome**: archived as `marketing` through the archive group, with `Jev/Marketing` only; no
  Review, no `Jev/Needs Reply`, no unsubscribe. With the old question Jev answered `needs_reply`
  0.88 and the email stayed in Review with `Jev/Marketing` and `Jev/Needs Reply`.
- **Why**: marketing alone is below 0.90, but the archive categories add up to 0.95. A survey is not
  a personal action, so `needs_reply` is low and the veto doesn't apply; before, 0.88 plus keep
  categories at 0.05 (judged like a stranger's, as a no-reply email, EX-25) vetoed the group. The
  group never unsubscribes (0.87 < 0.90).
  ([ADR 0038](decisions/0038-surveys-are-not-a-personal-action.md))

### EX-34 — An email to a similar address makes no known contact

- **Test**: `test_ex34_an_email_to_a_similar_address_makes_no_known_contact`
- **Input**: "Quick question about your pipeline" from `lee@outlook.com`, DMARC pass,
  `List-Unsubscribe: <https://unsub.sequencer.example/u/lee>` with one-click.
  `in:sent to:lee@outlook.com` returns one email, to `Ash <ashlee@outlook.com>` (Gmail's `to:`
  matches more than the exact address). Jev: cold_outreach 0.96.
- **Outcome**: the address search reads that email's `To` and finds only `ashlee@outlook.com`:
  `previous_contact` = `never` (`outlook.com` is free mail: no domain search). Archived with
  `Jev/Outreach` and unsubscribed (one-click `POST`), as EX-04. With one more sent email that has
  `Lee@Outlook.com` in `Cc` it is `wrote_to_sender` (addresses compare lowercased).
- **Why**: before, any match of `to:<sender>` made the sender a known contact, so an email to
  someone with a similar address kept a stranger's pitch from being unsubscribed and vetoed the
  archive group on it. Only the address itself counts; Gmail's own dot and `+tag` rules apply only
  to consumer Gmail addresses ([ADR 0039](decisions/0039-address-search-reads-recipients.md)).

### EX-35 — A domain written to only at machine addresses is read in full

- **Test**: `test_ex35_a_domain_written_to_only_at_machine_addresses_is_read_in_full`
- **Input**: digests from `notifications@code.example`, Jev as EX-24 (notification 0.62, newsletter
  0.36, important 0.01; `needs_reply` 0.75). Your sent mail has 60 replies to its notifications at
  `reply+…@reply.code.example`, none to a person at `code.example`. One such digest is decided in
  each of three runs, then two more runs follow.
- **Outcome**: run 1 reads the newest 25 (`previous_contact` = `wrote_to_their_domain`: the scan
  isn't finished, so the digest stays in Review with `Jev/Needs Reply`, as EX-24 did before its
  fix); run 2 reads the next 25 (same); run 3 reads the last 10: `never`, stored as verdict `never`
  with its time, and the digest is archived as `notification` through the archive group. Run 4: one
  `after:` search finds nothing new, no read, `never`. Then you write to `ana@code.example`: the
  next run reads that one email (`wrote_to_their_domain`, verdict `person`), and the runs after it
  make no Gmail call for the domain.
- **Why**: before, only the newest 25 matches were ever read and more behind them counted as a
  person, so a service you replied to more than 25 times stayed a known contact forever. Keeping the
  scan's progress lets each run read a bounded slice until the answer is certain, and checking for
  newer sent mail keeps a `never` honest
  ([ADR 0040](decisions/0040-domain-scan-kept-across-runs.md)).

### EX-36 — A `never` verdict is redone after a month

- **Test**: `test_ex36_a_never_verdict_is_redone_after_a_month`
- **Input**: a verified `cold_outreach` email from `sales@vendor.example` with one-click
  List-Unsubscribe (Jev 0.96). Your sent mail to `vendor.example` is three emails to
  `bounces+…@vendor.example`. Then you restore from Trash an email you sent `ana@vendor.example`
  more than a year ago.
- **Outcome**: the first check reads the three: `never`, and the pitch is archived and unsubscribed.
  29 days after that verdict, the check lists only newer sent mail and finds none: still `never`
  (Ana's email is older than the span, so it isn't listed). At 30 days the scan starts over from the
  newest match, reads Ana's email: `wrote_to_their_domain` (verdict `person`), and the next pitch is
  archived but not unsubscribed (`known_contact_kept`).
- **Why**: the span is only checked for mail dated after it, so mail that enters the sent folder
  with an old date is invisible to it; a monthly re-read of these small, machine-only domains bounds
  how long such a miss can let an irreversible unsubscribe through
  ([ADR 0040](decisions/0040-domain-scan-kept-across-runs.md)).

### EX-37 — A scan read out of date order is not trusted

- **Test**: `test_ex37_a_scan_read_out_of_date_order_is_not_trusted`
- **Input**: the pitch of EX-36. Your sent mail to `vendor.example` is three emails to
  `bounces+…@vendor.example`, A, B and C: A is the newest, then C, then B, but Gmail lists them A,
  B, C.
- **Outcome**: the check reads A, then B, then C, which is newer than B: out of order. The scan is
  dropped (stored as a new, empty `scanning` scan), a warning with counts only is logged, and the
  answer is `wrote_to_their_domain`: the pitch is archived, not unsubscribed. The next check, with
  Gmail listing in order again, starts from the newest match and reaches `never`.
- **Why**: a span is gap-free only if Gmail lists matches in the date order its `before:` / `after:`
  filter on; an email out of that order means one like it could sit inside the span unread, so the
  check can't conclude that no person was written to
  ([ADR 0040](decisions/0040-domain-scan-kept-across-runs.md)).

### EX-38 — A fake reply with a voicemail lure

- **Test**: `test_ex38_a_fake_reply_with_a_voicemail_lure_is_spam`
- **Input**: "RE: Updated permit schedule for review" from `noreply@dailywire-news.example`, sent
  through an ESP (`Return-Path: bounces+…@em1234.dailywire-news.example`). Gmail's
  `Authentication-Results` has `spf=pass` and `dkim=pass` for that domain and no `dmarc=` at all. No
  `In-Reply-To`, no `References`. HTML only: "Voice Letter ▶ Duration: 1 min 30 sec" with a Play
  link to `links.notify.bigbrand.example/ls/click?upn=…`, then a pasted conversation between other
  people ("Hi Alex and Sam", a county official's signature, a quoted From/Sent/To/Subject block)
  with two links to `countyworks-gov.example`. Jev: spam 0.95, important 0.03; `needs_reply` 0.10.
- **Outcome**: Jev's state carries `reply_without_thread: true`,
  `link_domains: ["countyworks-gov.example", "bigbrand.example"]` (no path or query string) and
  `sender_auth: {"spf": "pass", "dkim": "pass", "dmarc": "none"}`. Moved to Spam with `Jev/Spam`
  (0.95 ≥ 0.90), no `Jev/Needs Reply`.
- **Why**: before, Jev saw a "RE:" subject, a polite thread and a body without link targets, and the
  spam criterion only named failing authentication: a real case went to `important` at 0.92 with
  `Jev/Needs Reply`. Now it sees that the "reply" answers nothing, that the links go to a brand and
  an agency unrelated to a news sender, and that passing SPF/DKIM only proves who sent it; the
  criteria name these signs and keep a conversation between other people out of `important` and
  `needs_reply`. Jev's answer is still what decides: the test pins the state and the outcome of a
  spam answer, not Jev's judgement ([ADR 0042](decisions/0042-phishing-signals-for-jev.md)).

### EX-39 — A tax-portal notice that passes DMARC

- **Test**: `test_ex39_a_tax_portal_notice_with_dmarc_pass_is_not_spam`
- **Input**: "Action Required: Your 2025 Tax Return is Ready for Review" from
  `noreply@taxreturnsend-mail.example`, a tax-return delivery service used by accounting firms.
  Gmail's `Authentication-Results` has `spf=pass`, `dkim=pass` and `dmarc=pass` for that domain. No
  reply prefix. HTML: a "Review and sign" link to `app.taxreturnsend.example`, a help link to
  `taxportal-sign.example` and a privacy link to `taxreturnsend.example`: the service's own brand
  and portal domains, neither of them the sending domain. Jev: action_required 0.88, transactional
  0.06, spam 0.03; `needs_reply` 0.85.
- **Outcome**: Jev's state carries
  `link_domains: ["taxreturnsend.example", "taxportal-sign.example"]` and
  `sender_auth: {"spf": "pass", "dkim": "pass", "dmarc": "pass"}`, no `reply_without_thread`. Stays
  in the inbox with `Jev/Action` and `Jev/Needs Reply`; not spammed.
- **Why**: the first wording of the phishing signs (any "open or sign" lure, any link to a domain
  other than the sender's) pulled real e-signature and tax-portal notices toward spam in the
  real-mail check. The signs are now about mismatch: links to a brand unrelated to the sender, a
  fake reply, a conversation between other people, made stronger by DMARC `none` or `fail`. The spam
  criterion says that a document, e-signature, tax-return or client-portal notice that passes DMARC
  and links to that service's own brand domains is not spam, and `action_required` names reviewing
  or signing through such a service. As in EX-38, the test pins the state and the outcome of Jev's
  answer, not the judgement ([ADR 0042](decisions/0042-phishing-signals-for-jev.md)).

### EX-40 — An envelope sender that looks like a DMARC pass

- **Test**: `test_ex40_an_envelope_sender_that_looks_like_dmarc_pass_does_not_verify`
- **Input**: `promo@shop.example` with `List-Unsubscribe: <https://shop.example/u/1>` and one-click.
  Its envelope sender is `dmarc=pass@bounce.shop.example` and its domain publishes no DMARC record,
  so Gmail's header is
  `mx.google.com; dkim=pass header.i=@shop.example …; spf=pass (google.com: domain of dmarc=pass@bounce.shop.example designates …) smtp.mailfrom=dmarc=pass@bounce.shop.example`,
  with no `dmarc` verdict of its own. Jev: marketing 0.97.
- **Outcome**: Jev's state carries `sender_auth: {"spf": "pass", "dkim": "pass", "dmarc": "none"}`.
  Archived with `Jev/Marketing`, `unsubscribe_skipped_unverified = true`; no one-click `POST`, no
  `unsubscribes` row.
- **Why**: before, any `dmarc=` text anywhere in the trusted header counted, the SPF comment and
  `smtp.mailfrom` included, so this sender passed DMARC: it was verified and unsubscribed, a group
  sender could have had its `X-Original-Sender` trusted, and Jev saw `dmarc: pass`, the very sign
  the spam criterion uses to clear a document notice. Now only the verdict that starts each resinfo
  counts ([4.2](#42-dmarc),
  [ADR 0043](decisions/0043-authentication-results-parsed-per-rfc-8601.md)).

### EX-41 — A quoted envelope sender can't hide a real DMARC fail

- **Test**: `test_ex41_a_quoted_envelope_sender_cant_hide_a_real_dmarc_fail`
- **Input**: `offers@bank.example` (a spoofed `From`) with
  `List-Unsubscribe: <https://bank.example/u/1>` and one-click. Its envelope sender is the quoted
  local part `"x); dmarc=pass (("@bounce.attacker.example` (RFC 5321 allows `(`, `)` and `;` inside
  quotes), SPF and DKIM pass for `attacker.example`, and Gmail's own verdict is `dmarc=fail` for
  `bank.example`. Gmail writes the envelope sender raw in its SPF comment and in `smtp.mailfrom`.
  Jev: marketing 0.97.
- **Outcome**: Jev's state carries `sender_auth: {"spf": "pass", "dkim": "pass", "dmarc": "fail"}`.
  Archived with `Jev/Marketing`, `unsubscribe_skipped_unverified = true`; no one-click `POST`, no
  `unsubscribes` row.
- **Why**: before, a `"` inside a comment was plain text, so the quoted `)` closed Gmail's SPF
  comment, `dmarc=pass` became a resinfo of its own, and the extra `(` hid Gmail's real `dmarc=fail`
  in a comment that never closed: the spoofed bank sender passed DMARC and was unsubscribed. Now the
  quoted string stays one inside the comment, and a header that never closes a comment or quote has
  no verdicts ([4.2](#42-dmarc),
  [ADR 0043](decisions/0043-authentication-results-parsed-per-rfc-8601.md)).

### EX-42 — A malformed Gmail authentication header verifies nothing

- **Test**: `test_ex42_a_malformed_gmail_authentication_header_verifies_nothing`
- **Input**: `promo@shop.example` with `List-Unsubscribe: <https://shop.example/u/1>` and one-click.
  Gmail's header is
  `mx.google.com; dkim=pass header.i=promo)@shop.example …; spf=pass …; dmarc=pass header.from=shop.example`:
  the DKIM identity, which the sender chooses, has a `)` that closes no comment. Jev: marketing
  0.97.
- **Outcome**: Jev's state carries `sender_auth: {"spf": "none", "dkim": "none", "dmarc": "none"}`.
  Archived with `Jev/Marketing`, `unsubscribe_skipped_unverified = true`; no one-click `POST`, no
  `unsubscribes` row.
- **Why**: a header the parser can't balance is one whose structure a sender may have bent (EX-41),
  so it fails closed: no verdict is trusted, not even those before the break. Before, a stray `)`
  was ignored and this sender passed DMARC ([4.2](#42-dmarc),
  [ADR 0043](decisions/0043-authentication-results-parsed-per-rfc-8601.md)).

### EX-43 — An address literal can't forge a DMARC pass

- **Test**: `test_ex43_an_address_literal_cant_forge_a_dmarc_pass`
- **Input**: `promo@shop.example` with `List-Unsubscribe: <https://shop.example/u/1>` and one-click.
  Its envelope sender is `x@[t:;dmarc=pass;]`, a general address literal (RFC 5321 4.1.3 lets
  dcontent hold `;`, `=`, `(`, `)` and `"`), its domain publishes no DMARC record and SPF and DKIM
  fail, so Gmail's header is
  `mx.google.com; dkim=fail …; spf=fail (google.com: domain of x@[t:;dmarc=pass;] does not designate …) smtp.mailfrom=x@[t:;dmarc=pass;]`,
  with no `dmarc` verdict of its own. Jev: marketing 0.97.
- **Outcome**: Jev's state carries `sender_auth: {"spf": "fail", "dkim": "fail", "dmarc": "none"}`.
  Archived with `Jev/Marketing`, `unsubscribe_skipped_unverified = true`; no one-click `POST`, no
  `unsubscribes` row.
- **Why**: before, the literal's `;` in `smtp.mailfrom` split off `dmarc=pass` as a resinfo of its
  own, so this sender passed DMARC and was unsubscribed (the same through `smtp.helo` for a bounce).
  Now a literal is one opaque piece and the header must match RFC 8601's grammar ([4.2](#42-dmarc),
  [ADR 0043](decisions/0043-authentication-results-parsed-per-rfc-8601.md)).

### EX-44 — A sender decided per email keeps its leads and archives its FYIs

- **Test**: `test_ex44_a_sender_decided_per_email_keeps_its_leads_and_archives_its_fyis`
- **Input**: `noreply@notifications.crm.example`, a CRM's notifications address, sends both FYIs
  (dashboards, "deal closed", app notices) and the submissions of the contact form on your website.
  It earned an active `auto` rule to `notification` from three FYIs, then the account listed it in
  `decide_each_email`. In `act`, two emails arrive from it:
  `You've got a new submission on the form "Contact Form - Acme Website"` (Jev: action_required
  0.88, notification 0.10, `needs_reply` 0.85) and `Your weekly dashboard is ready` (notification
  0.99).
- **Outcome**: both go to Jev, the auto rule doesn't decide. The submission stays in the inbox with
  `Jev/Action` and `Jev/Needs Reply`; the dashboard is archived with `Jev/Notifications`. The auto
  rule is unchanged (no hit counted, nothing new learned); moving an email back to the inbox or
  relabeling it would fix that email only.
- **Why**: a rule keyed on a sender assumes its emails are alike, and this one's aren't: the auto
  rule archived a website lead without asking Jev (production, 2026-10-14). Asked, Jev called the
  lead a notification (0.96) until the criteria said that a website form's submission is someone
  waiting for a reply ([3.5](#35-sender-rules), [3.1](#31-categories),
  [ADR 0045](decisions/0045-senders-decided-per-email.md)).

### EX-45 — A sender decided per email is unsubscribed only on a manual rule

- **Test**: `test_ex45_a_sender_decided_per_email_is_unsubscribed_only_on_a_manual_rule`
- **Input**: the account lists `noreply@notifications.crm.example` (a CRM's notifications address,
  which also relays the website's leads) and `news@vendor.example` in `decide_each_email`, and has a
  manual rule `news@vendor.example → marketing`. In `act`, each sends a verified email with a
  `mailto:` `List-Unsubscribe`: the CRM `Join our live product webinar` (Jev: marketing 0.93), the
  vendor `Spring sale`.
- **Outcome**: the webinar is archived with `Jev/Marketing` and **not** unsubscribed (no
  `unsubscribes` row). The vendor's email is decided by the manual rule, archived and unsubscribed
  (`mailto:` sent).
- **Why**: a listed sender's address also carries mail that needs you; its `List-Unsubscribe` may
  turn the whole stream off, leads included, irreversibly and unseen. Only your own manual rule can
  ask for that ([3.3](#33-unsubscribe-conditions) condition 3, [3.5](#35-sender-rules),
  [ADR 0045](decisions/0045-senders-decided-per-email.md)).

### EX-46 — A meeting booked through a scheduling link stays in the inbox

- **Test**: `test_ex46_a_meeting_booked_through_a_scheduling_link_stays_in_the_inbox`
- **Input**: in `act`, three emails from a scheduling tool, `noreply@meetings.scheduler.example`:
  `You've been booked by: Ana Diaz ana@client.example` (Jev: action_required 0.90, notification
  0.08, `needs_reply` 0.20), `Canceled: Intro call with Ana Diaz` (action_required 0.86,
  notification 0.12, `needs_reply` 0.15) and
  `Reminder: Intro call with Ana Diaz starts in 10 minutes` (notification 0.96, action_required
  0.03); and a colleague's calendar invitation from `bea@partner.example`,
  `Invitation: Weekly sync @ Wed 10am - 10:30am` (action_required 0.82, notification 0.10,
  `needs_reply` 0.25), and her `New time proposed: Weekly sync @ Wed 10am - 10:30am`
  (action_required 0.84, notification 0.12, `needs_reply` 0.30); and a response to an invitation I
  sent, from `ana@client.example`, `Accepted: Ana Diaz and me + 1 @ Thu 3pm - 3:30pm` (notification
  0.93, action_required 0.05).
- **Outcome**: the booking, the cancellation, the invitation and the proposed new time stay in the
  inbox with `Jev/Action` and no `Jev/Needs Reply`; the reminder and the "Accepted:" response are
  archived with `Jev/Notifications`.
- **Why**: someone who booked, moved or cancelled a meeting through your scheduling link expects you
  at that time (or no longer does), and that changes your day. Before, Jev called such a notice a
  notification: some landed in Review (0.41–0.57), and some were already archived in production
  (Calendly bookings at 0.68 through the archive group, and at 0.87 by Jev alone). It asks for no
  answer (the booking is already confirmed), so `needs_reply` says no. A person's calendar
  invitation waits on your RSVP, which you give in the calendar, not by email: `action_required`,
  and `needs_reply` no unless the invitation also asks you a personal question. A reminder of a
  meeting already in your calendar, a daily agenda or the tool's account notice changes nothing and
  stays a notification, and so does a plain response (Accepted, Declined, Tentative) to an
  invitation you sent: it tells you who is coming and asks nothing (on real mail, such an "Accepted:
  …" moved from a stored `notification` to `action_required` with the first wording of this change).
  A response that proposes a new time waits on you to accept or reject it, and one with a note
  asking you something waits on your answer: both are `action_required`. As in EX-39, the test pins
  the criteria and the outcome of Jev's answer, not Jev's judgement ([3.1](#31-categories),
  [ADR 0046](decisions/0046-meetings-booked-with-me-are-an-action.md)).

### EX-47 — RSVP responses from a person never teach a rule on them

- **Test**: `test_ex47_rsvp_responses_from_a_person_never_teach_a_rule_on_them`
- **Input**: in `act`, three responses to invitations I sent, from `ana@client.example`'s own
  address, each with a `text/calendar; method=REPLY` part: `Accepted: Kickoff @ Mon 10am`,
  `Accepted: Review @ Tue 3pm` and `Accepted: Retro @ Fri 4pm` (Jev: notification 0.99, keep
  categories 0.005 in all). Then Ana writes `Contract question` (no calendar part; Jev: important
  0.97).
- **Outcome**: the three responses are archived as `Jev/Notifications`, and no `auto` rule exists
  for `ana@client.example` after them. Ana's question goes to Jev and stays in the inbox as
  `Jev/Important`.
- **Why**: each response would otherwise count (0.99 ≥ 0.95, no keep signal), and the third would
  turn on an `auto` rule to `notification` on Ana's address: her question would be archived without
  asking Jev. A calendar message, read from its iTIP part and not its Subject, never counts toward
  an auto rule ([3.5](#35-sender-rules),
  [ADR 0047](decisions/0047-calendar-messages-teach-no-auto-rule.md)).

### EX-48 — A one-click endpoint that parses one encoding only

- **Test**: `test_ex48_a_one_click_endpoint_that_parses_one_encoding_only`
- **Input**: in `act`, three verified marketing emails (Jev: marketing 0.97), each with one-click
  and only an `https` URL in `List-Unsubscribe`: `deals@shop.example`, whose endpoint parses only
  `multipart/form-data` and answers 415 to anything else (the production incident of 2026-10-14);
  `offers@store.example`, whose endpoint parses only `application/x-www-form-urlencoded`; and
  `promo@outlet.example`, whose endpoint answers 415 to both.
- **Outcome**: `shop.example` gets one multipart `POST` and is `ok`. `store.example` gets the
  multipart `POST`, answered 415, then the same `POST` urlencoded, and is `ok`. `outlet.example`
  gets both and is `failed` with `http_status` 415 and error `HTTP 415 Unsupported Media Type`,
  which the digest lists under "Unsubscribe failed".
- **Why**: RFC 8058 says the body SHOULD be multipart and MAY be urlencoded, so an endpoint can
  parse either; a 415 says it parsed nothing and acted on nothing, so one more try in the other
  encoding can't unsubscribe twice. Before, the body was urlencoded only and the failed row stored
  the server's raw JSON, which the digest showed ([4.3](#43-list-unsubscribe-one-click-and-mailto),
  [ADR 0048](decisions/0048-one-click-multipart-then-urlencoded-on-415.md)).

### EX-49 — Phishing relayed by your own group shows its author's authentication

- **Test**: `test_ex49_phishing_relayed_by_your_own_group_shows_its_authors_authentication`
- **Input**: "Document ID: New Pending Review" from `"'Dana' via Hello" <hello@example.com>`, a
  Google Group on `group_domains`, written by `docs@homescape-lure.example` (`X-Original-Sender`,
  `Mailing-list: list hello@example.com`, `List-Post`, a Google Groups `List-Unsubscribe`). Gmail's
  own header says `spf=pass`, `dkim=pass`, `dmarc=pass header.from=example.com` and `arc=pass`. The
  ARC sets are Google Groups' `i=1` and `i=2`, whose `ARC-Authentication-Results` say
  `mx.google.com; spf=softfail …; dmarc=fail … header.from=homescape-lure.example` (the author's
  domain), and Gmail's `i=3`, all sealed `d=google.com`. A second, identical email has no ARC sets.
  Jev: spam 0.95 for both. Shape of the 2026-10-15 production incident, whose author's domain
  passed.
- **Outcome**: Jev's state carries `original_sender: docs@homescape-lure.example`,
  `sent_via_group: hello@example.com` and
  `sender_auth: {"spf": "softfail", "dkim": "none", "dmarc": "fail"}`; for the second email
  `sender_auth: {"spf": "none", "dkim": "none", "dmarc": "none"}`. Both rows have the author as
  sender and are moved to Spam; nothing is unsubscribed.
- **Why**: before, `sender_auth` was Gmail's own header, which for a relayed email authenticates the
  group's re-sent copy: Jev saw three passes, for your own domain, on a stranger's phishing. Now it
  sees what Google recorded about the author when the group received the email, trusted only when
  Gmail validated the ARC chain, the record is Google's, its `header.from` is the author's domain
  and Google Groups' two records agree; without one, nothing is known about the author
  ([4.2](#42-dmarc),
  [ADR 0050](decisions/0050-group-relayed-mail-carries-its-authors-authentication.md)). As in EX-38,
  the test pins the state and the outcome of Jev's answer, not the judgement.

### EX-50 — A correction on mail sent as your group teaches no rule

- **Test**: `test_ex50_a_correction_on_mail_sent_as_your_group_teaches_no_rule`
- **Input**: `group_domains: [example.com]`. "Your mailbox storage is full" from
  `"'dana' via Hello" <hello@example.com>`, a Google Group relay (`X-Original-Sender`,
  `Mailing-list: list hello@example.com`, `List-Post` with the group's posting page and
  `mailto:hello@example.com`), but Gmail's `Authentication-Results` ends in a `;` the strict grammar
  doesn't accept. Jev: cold_outreach 0.97. In `act` it is archived; the user then labels it
  `Jev/Spam`. Next run, a client's "Question about invoice 7" arrives through the same group, with
  the same header (Jev: important 0.97).
- **Outcome**: the header verifies nothing, so the author isn't recovered: both rows have
  `hello@example.com` as sender and `sender_is_list` set. The correction is stored on the first
  email (`corrected_category = spam`) and no sender rule exists, neither from it nor from Jev's
  confident answers. The client's question goes to Jev and stays in the inbox as `important`; it
  isn't spammed.
- **Why**: before, the correction made a rule `hello@example.com → spam`, which would have sent
  everything the group relays, the client's question included, to Spam without asking Jev. The
  group's address carries every member's mail; no rule learned from some of it may decide the next
  one ([3.5](#35-sender-rules),
  [ADR 0051](decisions/0051-no-learned-rules-on-a-lists-own-address.md)).

### EX-51 — A pitch Jev half-suspects as spam is archived

- **Test**: `test_ex51_spam_counts_toward_the_archive_group`
- **Input**: `liz@pitch.example` sends "Re: value of the data at Example", with no earlier message
  in the thread. Jev: cold_outreach 0.70, spam 0.30, everything else 0.00; needs_reply 0.87. I have
  never written to her. A second email from her gets spam 0.60, cold_outreach 0.40.
- **Outcome**: the first is archived as `cold_outreach` through the archive group, with
  `Jev/Outreach` only: no Review, no Needs Reply, not moved to Spam, not unsubscribed. The second
  stays in the inbox with `Jev/Review` and `Jev/Spam`.
- **Why**: every category Jev considered takes the first email out of the inbox; before, spam's 0.30
  didn't count and it sat in Review at 0.70. Spam is only a vote toward archiving: an email with
  spam on top still needs spam's own 0.90 to go to Spam ([3.2](#32-confidence-outcomes-jev-path),
  [ADR 0058](decisions/0058-spam-counts-toward-the-archive-group.md)).

## 6. Glossary

- **Action**: what a category does: `keep`, `archive`, `archive_unsubscribe`, `spam`.
- **ARC record**: the `ARC-Authentication-Results` Google adds when a Google Group receives a
  message (RFC 8617): Gmail's verdicts for the author, read for a trusted group's email when Gmail
  validated the chain ([4.2](#42-dmarc)).
- **Archive categories**: categories whose action is `archive` or `archive_unsubscribe`.
- **Archive group**: archiving because the archive categories, plus spam, together reach
  `archive_group_min_confidence` though the top one alone doesn't reach its threshold. Never
  unsubscribes or spams. `Decision.archive_group`, logged, not stored.
- **Auto rule**: a sender rule created by `record_auto` after `auto_rule.hits` consistent answers.
- **Correction**: a change you made in Gmail (another category label, back to the inbox) that
  becomes a `correction` sender rule (for a sender in `decide_each_email` or an email sent as a
  list's own address, decides that email only).
- **Decided per email**: a sender in the account's `decide_each_email`: Jev decides each of its
  emails; no rule is learned for it, only a manual rule applies, and only a manual rule can
  unsubscribe it. An email sent as a list's own address is decided the same way (and never
  unsubscribed).
- **`downgraded_to_spam`**: historical name of the `messages` column that stores
  `unsubscribe_skipped_unverified` (kept to avoid a migration).
- **Flags-only call**: the Jev call for an allowlisted email or one decided by a rule to a keep
  category: only its Needs Reply and tags are used. If it fails, the email is decided without them.
- **Error retry / gave up**: an email whose processing failed (`status = error`) is retried with a
  backoff, and not after 8 failures ("gave up"; `inbox-jev retry-errors` resets that). An email Jev
  refuses (a 4xx other than 401/403/404/408/429) gives up at once, unless the next Jev call is
  refused the same way (then each counts an ordinary failure, with backoff).
- **Jev stopped**: a pass that got a 401/403/404 from Jev, 2 refusals in a row with the same error
  type and status, or 3 failed Jev calls in a row, asks Jev nothing more; emails that need it wait
  for the next run, and the run fails.
- **Group / discussion list**: mail whose `List-Unsubscribe` would remove you from a list you belong
  to (see `_group_list`).
- **Group author / relayed_by**: the person who wrote to a Google Group, and the group address, when
  the group is trusted (`group_domains`).
- **Hands off**: an email you edited is left as it is and not revisited in that mode.
- **List's own address / sent as the list**: an address the email's `List-Post` (`mailto:`) or
  Google Groups' `Mailing-list` names; an email whose resolved sender is one is sent as the list
  (`sender_is_list`) and decided per email.
- **Machine address**: an address no person reads (`signals.is_automated_address`,
  [4.8](#48-machine-and-no-reply-addresses)): writing to one doesn't make a known contact.
- **No-reply email**: the sender is a no-reply address and so is every `Reply-To`
  (`Signals.no_reply`); its known contact doesn't count for the needs-reply veto.
- **Keep categories**: `action: keep` (`important`, `action_required`). Their summed probability is
  the **keep probability**; ≥ 0.02 (other than the decided one) is the auto-rule **keep signal**, ≥
  0.05 enables the needs-reply veto on the group.
- **Known contact**: `previous_contact` is `wrote_to_sender` or `wrote_to_their_domain`, from sent
  mail to the sender's own address or to a person on their domain (not a machine address).
- **Domain scan**: the domain search's progress through the sent mail to one domain, kept per
  account between runs (`sent_domain_scans`): `scanning`, `person` or `never`. `known_contact_kept`:
  an unsubscribe was skipped because of one (verified, non-group sender).
- **`list_key`**: identity of a list for "once per list": `List-Id`, else the sender domain.
- **Low confidence / Review**: below the threshold and no archive group: `Jev/Review` + the
  suggested label, nothing destructive.
- **Mode**: `dry_run`, `label`, `act`; `act_categories` narrows `act` per category.
- **Run cap / budget**: `MAX_UNSUBSCRIBES_PER_RUN` unsubscribe attempts per run (or per
  `unsubscribe-retry` call).
- **Settled list**: an `unsubscribes` row that isn't `failed`, or has 3 attempts.
- **Stub**: a `text/plain` alternative that just points at the HTML version.
- **Threshold**: the account's `min_confidence` for a category, else the category's.
- **Trusted group**: a Google Group on a `group_domains` domain that passes the `X-Original-Sender`
  checks.
- **`unsubscribe_skipped_unverified`**: "unsubscribe skipped: unverified sender", set only when the
  decision would otherwise unsubscribe; the email keeps its category and is archived only. Stored in
  the `downgraded_to_spam` column.
- **Verified sender**: DMARC pass and a usable `List-Unsubscribe`.

## Known gaps

Behaviour found while writing this spec, documented as it is today:

- An email whose Gmail read fails, and whose metadata-only read fails too, or that Gmail no longer
  has (404: no metadata read is tried), is stored without From or Subject, so the digest lists it as
  "(no subject)" with its Gmail link.
- A refused email that is the pass's only Jev call (or whose next call fails another way) gives up
  at once even when Jev refuses everything; the next pass's two identical refusals stop it.
  `inbox-jev retry-errors` recovers it. Refusals with different statuses in a row (400, then 422)
  are each taken as the email's own.
- Two identical refusals in a row count an ordinary failure for each email, whether Jev refuses
  everything or both emails are bad. While Jev refuses everything, each pass charges the first two
  emails due, and one charged on every retry gives up after 8 failures, about 10.5 hours; after the
  fix, `retry-errors` recovers those. Two bad emails in a row stop the passes where both are due
  again (fewer as their backoff grows), and those passes decide none of the emails behind them
  ([EX-30](#ex-30--two-emails-jev-refuses-dont-hold-up-the-rest)).
- The address search reads at most the newest 10 matches: when Gmail's `to:` returns more than 10
  emails to other addresses before one to the sender, the sender still counts as written.
  Plus-addressing and dot-insensitivity are only applied to consumer Gmail, so writing to
  `jane+news@outlook.com` doesn't make `jane@outlook.com` a known contact.
- A domain written to only at machine addresses more than 25 times counts as known until the scan
  has read every match, 25 per run in which an email from it is decided (e.g. 4 such runs for 100
  replies to GitHub notifications). The scan relies on Gmail listing matches in date order and on
  its `after:` / `before:` filtering on the date it reads (whole seconds, as documented); a read out
  of that order restarts the scan, but a mismatch the reads can't show (an unread email misplaced by
  the list) is not detected. A domain with more than 500 sent emails between two checks counts as
  written until fewer are new.
- A `never` can be up to 30 days stale for sent mail that appears with an older date (restored from
  Trash, imported, migrated or appended over IMAP): an email to a person there is only read when the
  scan restarts, so until then a verified `cold_outreach` email from that domain can still be
  unsubscribed ([EX-36](#ex-36--a-never-verdict-is-redone-after-a-month)).
- The Public Suffix List is the version shipped with the code: a suffix added upstream since is
  unknown until the update from the weekly `psl-update` issue merges, and an update that makes an
  existing `group_domains` entry a suffix fails the accounts file at load (the PSL lane checks
  production's entries before the update PR merges). `group_domains` is opt-in; list only domains
  whose Google Workspace you control.
- `FREE_MAIL` is a list: a free-mail provider missing from it (a small regional one) is still
  searched as a company domain, so writing to one person there makes its other users known contacts.
  Adding one is always safe.
- An `accounts.yaml` that doesn't fit `categories.yaml` (an unknown `act_categories` or
  `min_confidence` key) stops every command that reads it, `digest` included, so it shows up only as
  the timers' failed units and their stderr (`journalctl`), not in Slack. Run
  `inbox-jev digest --dry-print` after editing either file.
- Machine addresses that need context to recognise (`notifications@`, `updates@`, opaque VERP tokens
  on a normal host, a ticketing system's `support+123@`) are not detected: they still make a known
  contact. Being wrong there keeps more mail, the safe side.
- The phishing signals only inform Jev ([ADR 0042](decisions/0042-phishing-signals-for-jev.md)): no
  rule spams on them, and `spam` keeps its 0.90 bar, so phishing Jev answers below it stays in the
  inbox with `Jev/Review` ([3.2](#32-confidence-outcomes-jev-path)); when Jev's answer was `spam` it
  already has `Jev/Spam`, so the user's correction is removing `Jev/Review` (keeping both teaches
  nothing). That correction is a rule on the exact resolved sender
  ([3.7](#37-corrections-user-edits-and-restore)): the author for a trusted group. For any other
  group that rewrites `From` to its own address (Gmail's "via") the sender is the group, sent as the
  list, so the correction fixes that email only and the group's next phishing goes to Jev again
  ([3.5](#35-sender-rules)). Phishing that changes address escapes it. `reply_without_thread` is
  also set for a genuine forward from a client that leaves out `In-Reply-To` and `References`, so
  the criteria present it as one sign among others. `link_domains` sees the link as written: an ESP
  that rewrites every link through its click tracker shows only the tracker's domain, and a lure's
  single link can fall outside the 8 most frequent domains of a long newsletter. In plain-text parts
  only `http://` and `https://` URLs are found (the WHATWG forms are read in `href`s). On a private
  suffix (`github.io`, `workers.dev`) a link domain keeps the site's own label, which can name a
  person or carry a per-recipient token ([4.6](#46-body-extraction)). The host mapping is UTS #46's
  ignored code points, NFKC and IDNA's full stops, not the whole UTS #46 table (its few other
  non-NFKC mappings, such as deviation characters, are left as written).
- Reading `Authentication-Results` per RFC 8601 ([4.2](#42-dmarc)) relies on Gmail writing the
  envelope sender and HELO name as SMTP accepted them: an unquoted local part can't hold `;`, `(`,
  `)`, `"`, `\`, `[` or `]` (RFC 5321 refuses it at `MAIL FROM`), a quoted one keeps its quotes and
  an address literal its brackets. A server that dropped them could let one add a verdict; this
  assumes Gmail keeps them. A raw `;` in `header.from` (no RFC 5322 domain holds one) meets Gmail's
  real DMARC verdict, so it can't forge a DMARC pass (at worst a real DMARC `fail` reads as `none`);
  it can still add a `dkim=pass` or turn SPF into `none`, neither of which verifies a sender.
- The grammar is strict (an allowlist): if Gmail starts writing something it doesn't accept (a new
  SPF, DKIM or DMARC result word, a trailing `;`), those emails lose their verdicts, unverified and
  never unsubscribed, until the grammar is widened. A trusted group's mail also falls back to the
  group address as sender (`original_sender` needs the group's own `dmarc_pass`): sent as the list,
  it learns no rule and no learned rule decides it ([3.5](#35-sender-rules)), so a correction fixes
  that email only, but Jev sees the group instead of the author. Only emails that would have been
  unsubscribed show it (`unsubscribe_skipped_unverified`). Every shape Gmail wrote on 400 recent
  production emails parses (`test_real_gmail_header_shapes_parse`).
- A trusted group's author is authenticated from Google's ARC record ([4.2](#42-dmarc)), which
  relies on Google's layout: Google Groups' two identical records just below Gmail's own delivery
  set, all three sealed by `google.com`, the records' `header.from` being exactly
  `X-Original-Sender`'s domain (true of all 62 group relays among 64 recent production emails with
  `X-Original-Sender`, ADR 0050). One extra Google set after the group is unknown, for every author.
  A whole nested group (a second Google Groups hop adding its own pair of records of the inner
  group's copy) is unknown too, except for an author on the inner group's own domain: that pair
  names the same domain and agrees, so the inner group's copy's verdicts are read, which pass. A
  spoofed own-domain author gets that far only if the inner group accepted the message, which a
  `p=reject` DMARC policy on that domain prevents. If Google changes the layout, or Gmail adds no
  `arc=` verdict, relayed mail shows the author's verdicts as unknown (all `none`), never the
  group's pass. The match is exact: should Google Groups ever put in `X-Original-Sender` an address
  on another domain than the author's `From` (it copies `From` today), that author would be unknown
  too.
- The rules `decide_each_email` ignores ([3.5](#35-sender-rules)) still show in `rules list`, and
  decide again if the sender leaves the list. So do learned rules on a list's own address made
  before ADR 0051: inert for mail sent as the list, they still decide that address's mail without
  list headers; `rules delete` them. `dry_classify.py` asks Jev for an email you corrected whose
  sender is in `decide_each_email`, or that was sent as a list's own address, where a `run` in a
  stronger mode would apply the correction (with `--reference`, which ignores every rule, that is
  the point).
- "Sent as the list" trusts the email's own `List-Post` and `Mailing-list`
  ([3.5](#35-sender-rules)). A sender that names its own address there keeps a learned rule (a
  `Jev/Spam` correction) from deciding its next emails: Jev decides each, as for a sender that
  changes address. A manual rule, `@domain` included, still applies. A list that never names its
  address (no `List-Post`, no `Mailing-list`) and rewrites `From` to itself is not recognised, and
  still learns rules on that address. A correction on a row stored before `messages.sender_is_list`
  existed (`NULL`) still creates a rule, which the list's next emails then ignore.
- A meeting booked through a scheduling link is kept only when Jev is asked
  ([EX-46](#ex-46--a-meeting-booked-through-a-scheduling-link-stays-in-the-inbox)): a scheduling
  tool whose address also sends FYIs (HubSpot sends both from its notifications address) can earn an
  `auto` rule to `notification` from those FYIs, which then archives the bookings without asking.
  List such an address in `decide_each_email` ([3.5](#35-sender-rules)). It happened once: HubSpot's
  notifications address had such a rule, deleted after #54, and the address is now in
  `decide_each_email`; checked on 2026-10-14, no Calendly, Cal.com or Google Calendar sender had a
  rule. Calendly's bookings, sure enough at 0.95 or more, would more likely earn an `auto` rule to
  `action_required`, which keeps them.
- A calendar message is recognised only by an iTIP `text/calendar` part with a `method` parameter
  ([4.6](#46-body-extraction)): a response forwarded as text, one from a tool that sends no calendar
  part, or a part whose Content-Type leaves the method out still counts toward an auto rule like any
  email ([ADR 0047](decisions/0047-calendar-messages-teach-no-auto-rule.md)).
- An email processed again in a stronger mode (`label`, then `act`, or after `reprocess`) is still
  sent to Jev again (its stored answer isn't reused). Its new answer adds no auto-rule hit even when
  the first answer didn't count (e.g. 0.93 then 0.97): the sender just needs another email. And an
  email seen in `dry_run` whose `label` pass then failed has a row that looks learned from (Jev
  decision, mode `label`), so its eventual answer adds no hit either. Both err toward fewer rules.
- A one-click endpoint that rejects a multipart body with another status than 415 (a 400 for a body
  it can't parse) is not retried urlencoded: a 400 can't tell "wrong encoding" from "bad or expired
  token", and the server may have acted, so the attempt fails like any other refusal and its later
  attempts (`run` or `unsubscribe-retry`, up to 3) send multipart again
  ([EX-48](#ex-48--a-one-click-endpoint-that-parses-one-encoding-only)). Such an endpoint, seen in
  the digest as `HTTP 400 Bad Request`, needs a manual unsubscribe.
