# How Inbox Jev sorts your email

A plain-language guide for the people whose inbox it sorts. You don't need to be an engineer to read
it. For the exact rules, see [For the technical details](#for-the-technical-details) at the end.

## What it does

Every 10 minutes, Inbox Jev looks at the new emails in your Gmail inbox. Each of these 10-minute
rounds is called a "pass". For each email it asks Jev, an AI classifier from TypeSafe, what kind of
email it is and how sure it is. It then puts a label on the email (they all start with `Jev/`) and,
depending on the kind, leaves it in your inbox, archives it, moves it to Spam, or archives it and
unsubscribes you from that mailing list. Unsubscribing sometimes means sending a short "unsubscribe"
email from your account (see [Safety nets](#safety-nets)). Once a day it sends you a short Slack
message with what still needs you.

Nothing is ever deleted. Archiving only takes an email out of the inbox (it stays in "All Mail"),
and anything moved to Spam can be moved back.

## The labels

| Label in Gmail | What goes there | What happens to it |
| -- | -- | -- |
| `Jev/Important` | Written by a real person to you: clients, teammates, partners, candidates, questions or requests for you. Not mass mail that only uses your name, and not someone else's conversation pasted into an email from an unrelated sender. | Stays in the inbox |
| `Jev/Action` | Automated email that needs you to do something: approve a payment or a held message, update an expiring card, verify your account, act on a security alert, review or sign a document or tax return through a signing or client-portal service. Not surveys or promotions. | Stays in the inbox |
| `Jev/Transactional` | Records of something you did or bought: receipts, paid invoices, order and booking confirmations, shipping updates, password resets, codes, copies of documents you already signed. | Archived |
| `Jev/Notifications` | Automatic "for your information" alerts and summaries from tools you use (GitHub, Slack, Google Drive, Jira, monitoring…). | Archived |
| `Jev/Newsletters` | Newsletters, blog updates and industry news you subscribed to. | Archived |
| `Jev/Marketing` | Promotions from real companies: sales, discounts, launches, webinars. | Archived, and unsubscribed when it is safe (see [Safety nets](#safety-nets)) |
| `Jev/Outreach` | Unsolicited sales pitches written to look personal: agencies, freelancers, "quick question" follow-up sequences. | Archived, and unsubscribed when it is safe |
| `Jev/Spam` | Fraud: scams, phishing, fake invoices or account alerts, fake voicemail or document notices. | Moved to Spam |

"What happens to it" is what full mode does; an account can also run in a gentler mode (see
[Safety nets](#safety-nets)). A few more labels can appear next to those:

- `Jev/Needs Reply`: the sender seems to expect an answer or an action from you personally. It is
  only added to emails that stay in your inbox.
- `Jev/Invoices`: the email contains a bill you need to pay.
- `Jev/Review`: Jev wasn't sure enough, so the email stays in your inbox with its best guess. See
  below.
- `Jev/Processed`: a marker that Inbox Jev has seen the email. You can ignore it.

## How it decides

**First, who sent it.** If the sender is on your allowlist (a list of people and domains you always
want kept, set up by whoever runs Inbox Jev), the email is labeled `Jev/Important` and stays, full
stop. If you have taught it something about this sender before (see
[How it learns from you](#how-it-learns-from-you)), it follows that.

**Then, have you written to them?** Inbox Jev searches your sent mail to see whether you have ever
written to this person, or to someone at their company. It only looks at who your emails went to,
never at what they said. Replies to automatic addresses (like `noreply@` or a notification's reply
address) don't count, and writing to one person at Gmail or Outlook doesn't make everyone there a
contact. People you have written to are treated more carefully, as described below.

**Then, what does Jev think it is?** Jev reads the sender, the subject and the beginning of the
email, and answers with the kind of email and how sure it is. It also gets a few clues that help it
spot phishing:

- whether the subject says "RE:" or "FW:" while the email doesn't actually answer any earlier
  message;
- which websites the links really go to (just the site names, never the full links);
- whether Gmail's own checks confirm which domain (web address) the email was sent from.

Passing those checks only proves where the email was sent from, not that it is honest: phishing
often comes from the attacker's own domain. What gives phishing away is a mismatch, like a notice
that claims to be from one brand while its action link goes to another, or a "reply" to a
conversation you were never in. Jev is also told that real document, e-signature and tax-portal
services that pass the checks and link to their own websites are not spam: they belong in
`Jev/Action` or `Jev/Transactional`.

**How sure is sure enough?** Each kind has its own bar:

- `Jev/Important` and `Jev/Action` have no bar: when one of them is Jev's best answer, the email
  stays in your inbox with that label.
- Receipts, notifications and newsletters are archived when Jev is about 80% sure.
- Marketing, outreach and spam need about 90%, because those can unsubscribe you or move mail to
  Spam.

**When it isn't sure.** Below the bar, nothing is moved. The email stays in your inbox with
`Jev/Review` and the label Jev would have picked, so you can confirm or change it.

There is one exception, with its own exception:

- **Torn between two "archive" kinds.** Say a tool's weekly summary is half notification, half
  newsletter. If Jev is about 90% sure that together it belongs in one of them, it is archived
  anyway, under its best guess. It is never unsubscribed or moved to Spam this way.
- **Unless it may need your reply.** If Jev thinks the sender expects a reply, and you have written
  to them before (or the email looks at all personal), it stays in your inbox for review instead.

**Learning by itself.** When Jev has been very sure (about 95%) that three different emails from a
sender are the same kind, without a confident different answer in between, and nothing suggested
they could be personal, Inbox Jev remembers that and stops asking Jev what kind that sender's emails
are. This never happens for outreach from someone you have written to.

## Safety nets

- **Nothing is deleted.** Archived emails are in "All Mail", spam is in the Spam folder. Note that
  Gmail itself empties the Spam folder after 30 days.
- **Unsubscribing is the only thing that can't be undone**, so it has the most checks:
  - only for `Jev/Marketing` and `Jev/Outreach`, and only when Jev is about 90% sure (or you taught
    it that sender);
  - only when the sender proves who it is: Gmail confirms the email really comes from the domain it
    claims, and it carries the standard unsubscribe instruction in the email's hidden technical
    details. Links inside the email are never clicked;
  - it either uses the one-click unsubscribe the sender provides, or sends a short "unsubscribe"
    email from your account to the address the sender gives, which you'll see in your Sent folder;
  - never for an outreach email from someone you have written to, or someone at their company: it is
    archived, nothing more. (Marketing from a company you've written to can still be unsubscribed,
    since that only stops their promotions; this can be changed per category.)
  - never through a group or discussion list, since that would take you off the group;
  - never for anyone on your allowlist;
  - once per mailing list, and at most 20 per pass (every 10 minutes).
- **It respects what you did.** Inbox Jev handles each email once. If you star an email it has
  labeled, it won't touch that email again, even if the account later moves to full mode. Changing
  its `Jev/` labels in a way that isn't one of the fixes in
  [How it learns from you](#how-it-learns-from-you), for example removing them all, has the same
  effect. A fix it learns from is different: if the account later moves to full mode, that email is
  handled as the kind you chose. Starring a brand-new email before Inbox Jev has seen it doesn't
  protect it.
- **Gentler modes.** Whoever runs Inbox Jev can set an account to a trial mode (it only records what
  it would do, nothing changes in Gmail) or a labels-only mode (it labels but never moves anything).
  Full mode can also be turned on for some kinds only, for example archiving receipts but not yet
  unsubscribing from marketing.
- **Undo in bulk.** Whoever runs Inbox Jev can undo everything it did over a period (for example
  "the last day"), or only for one kind. Archived and spammed emails go back to the inbox, and its
  `Jev/` labels are removed from every email it handled in that period, including the ones it left
  in the inbox. Those emails are never processed again. Spam that Gmail has already emptied (after
  30 days) can't come back. Unsubscribes can't be undone this way: you would subscribe again on the
  sender's website.

## How it learns from you

You teach Inbox Jev by fixing its work in Gmail, within a week of the email arriving:

- **Change the label**: remove the `Jev/` label it put and add the right one (for example, swap
  `Jev/Marketing` for `Jev/Newsletters`). Keep just one of the eight labels in the table above.
- **Move it back to the inbox** from the archive or from Spam. That means "this sender is
  important".
- **Remove `Jev/Review`** from an email it wasn't sure about. That means "your guess was right".

Inbox Jev picks that up within about an hour, and from then on emails from that exact address get
the kind you chose. The email you fixed stays where you put it, unless the account later moves to
full mode. Be careful with one thing: teaching it that a sender is `Jev/Marketing` or `Jev/Outreach`
means their future emails can be unsubscribed, and `Jev/Spam` means they go straight to Spam.

Rules can also be set by hand from the command line, by sender or by whole domain (see the
[README](../README.md#commands)).

## When it gets it wrong

**Why didn't it mark this phishing email as spam?** Spam needs Jev to be about 90% sure. Below that,
the email stays in your inbox with `Jev/Review`; and a well-made phishing email can fool Jev into
thinking it is real. Don't click anything in it. Use Gmail's "Report phishing", and change its
`Jev/` label to `Jev/Spam` so the next email from that address goes to Spam (phishing often changes
address, so also tell whoever runs Inbox Jev).

**Why is a newsletter in my inbox?** Usually Jev wasn't sure enough: look for `Jev/Review`. Swap the
label for `Jev/Newsletters` (or just remove `Jev/Review` if the guess was right) and future issues
will be archived. It can also stay because the account is in labels-only mode or full mode isn't on
for newsletters yet, or because you edited or starred that email earlier.

**Why was something archived that I wanted?** Move it back to the inbox. That tells Inbox Jev the
sender is important, and their future emails will stay. If a whole company or person should never be
touched, ask whoever runs Inbox Jev to add them to your allowlist.

**Why did it unsubscribe me from X?** Jev was at least about 90% sure it was marketing or outreach,
and the sender passed all the checks in [Safety nets](#safety-nets). The daily digest lists every
unsubscribe. To get those emails again, subscribe on the sender's website, then move their next
email back to the inbox (or label it what it is) so the following ones aren't archived again. It
never unsubscribes from the same list twice.

**Why does the digest show only a few emails?** On purpose: it only lists what still needs you.
Emails it archived itself are only counted in the totals. An email that would be listed but that you
have since archived, replied to or confirmed is counted as "already handled" instead.
`Jev/Important` emails only appear if they need a reply, and each section shows at most 15 emails.

**Why is an email in my inbox with no `Jev/` labels?** It arrived in the last 10 minutes, the
account is in trial mode, it was put back with a bulk undo (see [Safety nets](#safety-nets)), or
Inbox Jev couldn't process it (for example, Jev was down). It tries again with longer and longer
pauses, and after about 10 hours gives up and lists the email in the digest's errors.

## The daily Slack digest

Every morning (08:00 Buenos Aires time by default) you get one Slack message per account, covering
the last 24 hours, in English or Spanish:

- **Totals**: how many emails were processed, how many of each kind, how many are waiting in Review,
  and how many were "already handled": archived, replied to or sorted out by you since, so they
  aren't listed.
- **Action**: the `Jev/Action` emails still in your inbox.
- **Needs reply**: emails waiting for your answer that you haven't replied to yet.
- **Unsubscribed**: lists you were taken off. **Manual unsubscribe**: lists where it couldn't do it
  automatically, with the link for you to click if you want. **Unsubscribe failed**: attempts that
  didn't work; it may try again later.
- **Low confidence**: how many emails from the period still have `Jev/Review`, with a link to see
  them in Gmail.
- **Corrections learned**: how many of your fixes it picked up.
- **Errors**: emails it couldn't process, and passes that failed (for example, when Gmail access
  needs to be renewed).

## Privacy

- **What Jev sees**: for each email, your address, the sender, recipients, subject and date, the
  first 3,000 characters of the text, whether it is a mailing list (and its "bulk mail" marker, when
  it has one), whether you have written to the sender before, and the clues listed above (link site
  names, Gmail's checks, a fake "RE:"). For an email sent through one of your company's own groups
  (when whoever runs Inbox Jev has set that up), also who actually wrote it and the group's address.
  Never attachments.
- **What is stored**: a record per email with the sender, subject, date, the kind Jev chose, how
  sure it was and what was done; the unsubscribes, with the link or address each one used; the rules
  it learned; and, for each company domain, how far the sent-mail search got (dates and Gmail's
  internal message ids, never addresses or content). The text of your emails is never stored.
- **Emails sent from your account**: only the short "unsubscribe" emails described in
  [Safety nets](#safety-nets). Inbox Jev never replies to anyone.
- **What is never written to the logs**: the text of your emails, or anything Jev says back when it
  refuses an email (it could quote it).
- **Your sent mail** is only searched to see who you wrote to; Inbox Jev reads the recipients of
  those emails, never their content.

## For the technical details

- [How Inbox Jev decides](how-it-decides.md): the exact rules, numbers and worked examples. It is
  the reference this guide simplifies.
- [Design decisions](decisions/README.md): why each rule is the way it is, and what was considered
  instead.
- [README](../README.md): setup, settings and commands.
