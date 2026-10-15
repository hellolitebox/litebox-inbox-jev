# How Inbox Jev sorts your email

A plain-language guide for the people whose inbox it sorts. You don't need to be an engineer to read
it. For the exact rules, see [For the technical details](#for-the-technical-details) at the end.

## What it does

Every 10 minutes, Inbox Jev looks at the new emails in your Gmail inbox. Each of these 10-minute
rounds is called a "pass". For each email it asks Jev, an AI classifier from TypeSafe, what kind of
email it is and how sure it is. It then puts a label on the email (they all start with `Jev/`) and,
depending on the kind, leaves it in your inbox, archives it, moves it to Spam, or archives it and
unsubscribes you from that mailing list. Unsubscribing sometimes means sending a short "unsubscribe"
email from your account (see
[What it unsubscribes you from](#what-it-unsubscribes-you-from-and-what-it-never-does)). Once a day
it sends you a short Slack message with what still needs you.

Nothing is ever deleted. Archiving only takes an email out of the inbox (it stays in "All Mail"),
and anything moved to Spam can be moved back.

## The labels

| Label in Gmail | What goes there | What happens to it |
| -- | -- | -- |
| `Jev/Important` | Written by a real person to you: clients, teammates, partners, candidates, questions or requests for you. Not mass mail that only uses your name, and not someone else's conversation pasted into an email from an unrelated sender. | Stays in the inbox |
| `Jev/Action` | Automated email that needs you to do something: approve a payment or a held message, update an expiring card, verify your account, act on a security alert, review or sign a document or tax return through a signing or client-portal service, a message from the contact form on your website, someone booking, moving or cancelling a meeting with you through your scheduling link (HubSpot, Calendly and the like), or a person's calendar invitation, update or cancellation waiting on your answer, or a reply to your own invitation that proposes a new time or asks you something. Not surveys or promotions, and not reminders of meetings already in your calendar. | Stays in the inbox |
| `Jev/Transactional` | Records of something you did or bought: receipts, paid invoices, order confirmations, confirmations of bookings made by you or for you (flights, hotels, restaurants, an appointment with a provider), shipping updates, password resets, codes, copies of documents you already signed. | Archived |
| `Jev/Notifications` | Automatic "for your information" alerts and summaries from tools you use (GitHub, Slack, Google Drive, Jira, monitoring…), including your daily agenda, "your meeting starts in 10 minutes" reminders, and the notices that someone accepted, declined or said "maybe" to an invitation you sent. | Archived |
| `Jev/Newsletters` | Newsletters, blog updates and industry news you subscribed to. | Archived |
| `Jev/Marketing` | Promotions from real companies: sales, discounts, launches, webinars. | Archived, and unsubscribed when it is safe (see [What it unsubscribes you from](#what-it-unsubscribes-you-from-and-what-it-never-does)) |
| `Jev/Outreach` | Unsolicited sales pitches written to look personal: agencies, freelancers, "quick question" follow-up sequences. | Archived, and unsubscribed when it is safe |
| `Jev/Spam` | Fraud: scams, phishing, fake invoices or account alerts, fake voicemail or document notices. | Moved to Spam |

"What happens to it" is what full mode does; an account can also run in a gentler mode (see
[Safety nets](#safety-nets)). A few more labels can appear next to those:

- `Jev/Needs Reply`: the sender seems to expect an answer or an action from you personally. It is
  only added to emails that stay in your inbox. A meeting someone booked with you doesn't get it
  (the booking is already confirmed, you only need to be there), nor does a calendar invitation or a
  reply proposing a new time (you answer it in your calendar), unless it also asks you something
  personally.
- `Jev/Invoices`: the email contains a bill you need to pay.
- `Jev/Review`: Jev wasn't sure enough, so the email stays in your inbox with its best guess. See
  below.
- `Jev/Processed`: a marker that Inbox Jev has seen the email. You can ignore it.

**These kinds are a starting point.** The descriptions above are the defaults Inbox Jev comes with,
written for one person's inbox. Whoever runs Inbox Jev can change them to suit your inbox: for
example, keep newsletters in the inbox, treat the messages from your website's contact form as
ordinary notifications, or add a new kind (say, job applications). The company names in the
descriptions (HubSpot, Calendly and the like) are only examples that help Jev recognise a kind of
email; an email isn't treated differently just because it comes from one of them.

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
- whether Gmail's own checks confirm which domain (web address) the email was sent from. For an
  email that reached you through one of your company's own groups, these are the checks on the
  person who wrote to the group, as Google recorded them when the group received it, not on the
  group: the group always passes, because it is yours. When that record is missing, can't be trusted
  or is about someone other than that person, Jev is told the checks are unknown.

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
are. This never happens for outreach from someone you have written to. Calendar emails never count
toward it: an invitation, a cancellation, or the "Accepted" or "Declined" notice someone's calendar
sends when they answer your invitation. Those notices come from the person's own address, so three
of them could otherwise teach Inbox Jev to archive everything that person writes to you.

**Senders that send both kinds.** Some senders send both important and unimportant mail from the
same address. A sales tool's notifications address, for example, sends you weekly dashboards
(archive them) but also the message someone left in the contact form on your website (answer it!).
Whoever runs Inbox Jev can list such senders for your account. For a listed sender, Jev decides each
email on its own: Inbox Jev never learns a habit for that sender, neither by itself nor from your
fixes, and only a rule set by hand still applies. For senders on this list, Inbox Jev never
unsubscribes you unless there is a rule set by hand from the command line (`rules set`, usually by
whoever runs Inbox Jev); changing a label doesn't count for these senders. Their promotions are
archived, nothing more, because unsubscribing could also stop the emails that need you. A new
message from a contact form on your own website is treated as something that needs you: it stays in
your inbox as `Jev/Action`.

## Safety nets

- **Nothing is deleted.** Archived emails are in "All Mail", spam is in the Spam folder. Note that
  Gmail itself empties the Spam folder after 30 days.
- **Unsubscribing is the only thing that can't be undone**, so it has the most checks: only for
  marketing and outreach, only when the sender proves who it is, never for a pitch from someone you
  know, a group, your allowlist or a sender that sends both kinds, and links inside the email are
  never clicked. The full list, and how to control it, is in
  [What it unsubscribes you from](#what-it-unsubscribes-you-from-and-what-it-never-does).
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

## What it unsubscribes you from (and what it never does)

**Only two labels can lead to an unsubscribe: `Jev/Marketing` and `Jev/Outreach`.** Newsletters,
notifications, receipts and everything else are never unsubscribed: they are archived, kept in the
inbox or moved to Spam as usual. An email Jev wasn't sure about (`Jev/Review`) is never unsubscribed
either.

**All of these must be true**, or the email is only archived:

01. Jev is at least about 90% sure it is marketing or outreach (whoever runs Inbox Jev can change
    that bar), or Inbox Jev already knows that sender as marketing or outreach (from a fix of yours,
    a rule set by hand, or by learning it itself; see
    [How it learns from you](#how-it-learns-from-you)).
02. The sender proves who it is: Gmail confirms the email really comes from the company it claims.
03. The email carries the standard unsubscribe instruction in its hidden technical details (the one
    Gmail uses for its own "Unsubscribe" button). Links inside the email are never clicked.
04. It doesn't come through a group or discussion list, or a mailing list of your own company:
    unsubscribing there would take you off the group.
05. The sender isn't on your allowlist.
06. For outreach only: you have never written to that person (or, for a company address, to anyone
    at that company), as far as your Sent mail shows; writing to an automated "no-reply" address
    doesn't count. If Gmail's search of your Sent mail fails at that moment, it goes ahead. A pitch
    from someone you know is just archived. Marketing has no such exception: unsubscribing only
    stops a company's promotions, not your conversations with its people.
07. The sender isn't on your list of senders that send both kinds (see
    [How it decides](#how-it-decides)), unless there is a rule for it set by hand from the command
    line (`rules set`, usually by whoever runs Inbox Jev); changing a label doesn't count for these
    senders.
08. It is the first time for that mailing list: Inbox Jev unsubscribes from each list once (when the
    sender doesn't say which list, all of that company's email counts as one). If an attempt fails,
    it tries again when the next email from that list arrives (or when whoever runs Inbox Jev asks
    it to retry), up to 3 attempts in all.
09. It hasn't already tried 20 unsubscribes in this pass (a safety limit; whoever runs Inbox Jev can
    change it). Past that, the email is just archived, and the list is unsubscribed only when a
    later email from it arrives.
10. The account is in full mode for that kind of email (not trial or labels-only mode).

**How it unsubscribes:**

- If the sender offers a one-click unsubscribe, it uses that. The standard allows two ways of
  writing that request; Inbox Jev uses the recommended one, and if the sender's site answers that it
  doesn't accept that way, it asks once more the other way. It never repeats a request the site may
  have acted on.
- If that doesn't work, or there is none, and the sender gives an address for it, it sends a short
  "unsubscribe" email from your account to that address. You'll see it in your Sent folder.
- If the sender only offers a link to a web page, Inbox Jev does **not** open it. The link appears
  in the daily digest under "Manual unsubscribe", so you can do it yourself in one click if you
  want.

**Where to see what it did:** the [daily digest](#the-daily-slack-digest) lists the lists you were
taken off ("Unsubscribed"), the ones waiting for you, with their link ("Manual unsubscribe"), and
the attempts that didn't work ("Unsubscribe failed", with a short reason such as the error code the
sender's site answered, never the text of its reply), or their Spanish names if your digest is in
Spanish. Inbox Jev can't undo an unsubscribe: to get those emails again, subscribe again on the
sender's website.

**How do I stop it from ever unsubscribing me from someone?** Ask whoever runs Inbox Jev to add them
to your allowlist (by address or whole company): their emails then always stay in your inbox as
`Jev/Important`. Or change the label of one of their emails to `Jev/Newsletters` or `Jev/Important`:
from then on, every email from that exact address gets that label, so it is never unsubscribed
(newsletters are archived, important email stays in the inbox).

**How do I make it unsubscribe me from a newsletter?** Change the label of one of its emails to
`Jev/Marketing`. From the next email on, every email from that address is treated as marketing:
archived without asking Jev and, if all the checks above pass, unsubscribed. This doesn't work for
senders that send both kinds: there, ask whoever runs Inbox Jev for a rule set by hand. Or simply
unsubscribe yourself, with Gmail's "Unsubscribe" button or the link in the email.

**Why didn't it unsubscribe me from this marketing email?** One of the checks above failed. Most
often Jev was less than 90% sure (look for `Jev/Review`), the email doesn't carry the standard
unsubscribe instruction, or the sender only offers a web page: then it is in the digest under
"Manual unsubscribe". It also never unsubscribes twice from the same list.

**Where do I see what it unsubscribed me from?** In the daily digest's "Unsubscribed" section, and
the unsubscribe emails it sent are in your Sent folder.

## How it learns from you

You teach Inbox Jev by fixing its work in Gmail, within a week of the email arriving:

- **Change the label**: remove the `Jev/` label it put and add the right one (for example, swap
  `Jev/Marketing` for `Jev/Newsletters`). Keep just one of the eight labels in the table above.
- **Move it back to the inbox** from the archive or from Spam. That means "this sender is
  important".
- **Remove `Jev/Review`** from an email it wasn't sure about. That means "your guess was right".

Inbox Jev picks that up within about an hour, and from then on emails from that exact address get
the kind you chose (except for the senders that send both kinds, described in
[How it decides](#how-it-decides): there, your fix applies to that one email only). The email you
fixed stays where you put it, unless the account later moves to full mode. Be careful with one
thing: teaching it that a sender is `Jev/Marketing` or `Jev/Outreach` means their future emails can
be unsubscribed, and `Jev/Spam` means they go straight to Spam.

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
or Inbox Jev already knew that sender as marketing or outreach, and the sender passed all the checks
in [What it unsubscribes you from](#what-it-unsubscribes-you-from-and-what-it-never-does). The daily
digest lists every unsubscribe. To get those emails again, subscribe on the sender's website, then
move their next email back to the inbox (or label it what it is) so the following ones aren't
archived again. It never unsubscribes from the same list twice.

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
the last 24 hours, in English or Spanish. It is built to be read on your phone in a few seconds,
most important first:

- **The headline** says how many emails need you, or "Inbox zero" when nothing does.
- **Action** and **Needs reply**: the `Jev/Action` emails still in your inbox, and the emails
  waiting for your answer that you haven't replied to yet. Each shows its subject (tap it to open
  the email in Gmail), who sent it and how long ago. A long list shows the first 10 and a link to
  see the rest in Gmail. Calendar invitations, updates and replies are left out of these lists and
  of To review: they stay in your inbox with their label, and your calendar already shows them. They
  still count in the table below.
- **To review**: how many emails from the period still have `Jev/Review`, with a link to review them
  in Gmail.
- **What Jev did**: a small table with how many emails of each kind it sorted, how many it left for
  review, the total, and how many were "already handled": archived, replied to or sorted out by you
  since, so they aren't listed above.
- **Unsubscribed**: lists you were taken off. **Manual unsubscribe**: lists where it couldn't do it
  automatically, with the link for you to click if you want. **Unsubscribe failed**: attempts that
  didn't work; it may try again later.
- **Corrections learned**: each fix of yours it picked up, with the sender and the change (for
  example `Jev/Important → Jev/Spam`). "(this email only)" means that sender is one it decides email
  by email, so the fix applies to that email and not to the sender's next ones.
- **Errors**, only when something went wrong: emails it couldn't process, and passes that failed
  (for example, when Gmail access needs to be renewed).

The notification on your phone shows just the headline and how many emails were processed.

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
  [What it unsubscribes you from](#what-it-unsubscribes-you-from-and-what-it-never-does). Inbox Jev
  never replies to anyone.
- **What is never written to the logs**: the text of your emails, or anything Jev says back when it
  refuses an email (it could quote it).
- **Your sent mail** is only searched to see who you wrote to; Inbox Jev reads the recipients of
  those emails, never their content.

## For the technical details

- [How Inbox Jev decides](how-it-decides.md): the exact rules, numbers and worked examples. It is
  the reference this guide simplifies.
- [Design decisions](decisions/README.md): why each rule is the way it is, and what was considered
  instead.
- [README](../README.md): setup, settings and commands; [Operating inbox-jev](operations.md) for the
  server side.
