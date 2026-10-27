# Operating inbox-jev

How inbox-jev runs in production: Docker plus systemd timers on one server, with the host-side
scripts in [`scripts/`](../scripts) for alerts, the staleness watchdog, off-site backups and
deploys. What the tool does is in the [README](../README.md); how it decides, in the
[spec](how-it-decides.md).

## Deploy (Docker + systemd timers)

A server runs the image CI builds, from a checkout of this repo. (On your own computer, the package
from PyPI and `inbox-jev schedule install` do the same with launchd or systemd user units: see the
README's Setup. Never both for the same account: `schedule install`, and `init`'s schedule step,
refuse on a host with `/etc/systemd/system/inbox-jev-triage.timer`. systemd user units run only
while their user is logged in unless lingering is on, `loginctl enable-linger $USER`; `install`
warns when it is off.) The image sets `INBOX_JEV_HOME=/app`, so every relative path in `.env` is
taken from `/app`, where compose mounts `accounts.yaml`, `secrets/` and `data/`.

```bash
git clone <repo> /opt/inbox-jev && cd /opt/inbox-jev
# copy .env, accounts.yaml, secrets/client_secret.json and secrets/tokens/*.json from your laptop,
# where `uv run inbox-jev init` set them up (the Google authorization needs a browser)
# (and your own categories file if .env's CATEGORIES_FILE names one: see "Your own categories")
# The container runs as APP_UID:APP_GID. Make sure .env sets both (see .env.example): an older
# .env without them falls back to 1000:1000 and the app gets PermissionError on data/ and secrets/.
# Pick ids no host user has. Read them from .env (no sourcing: other values may contain spaces):
APP_UID=$(grep -E '^APP_UID=' .env | tail -n1 | cut -d= -f2- | cut -d'#' -f1 | tr -d "\"' \r\t")
APP_GID=$(grep -E '^APP_GID=' .env | tail -n1 | cut -d= -f2- | cut -d'#' -f1 | tr -d "\"' \r\t")
if ! echo "$APP_UID:$APP_GID" | grep -Eq '^[0-9]+:[0-9]+$'; then
  echo "Set numeric APP_UID and APP_GID in .env first (see .env.example)"
elif getent passwd "$APP_UID" >/dev/null; then
  echo "uid $APP_UID belongs to a host user: pick another APP_UID in .env"
else
  mkdir -p data && sudo chown -R "$APP_UID:$APP_GID" data secrets && sudo chmod 700 data secrets
fi
GIT_SHA=$(git rev-parse HEAD) docker compose build   # or docker login ghcr.io && docker compose pull
docker compose run --rm app --version                       # the commit the image was built from
docker compose run --rm app run --mode dry_run --limit 20   # smoke test
sudo cp deploy/systemd/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now inbox-jev-triage.timer inbox-jev-digest.timer \
  inbox-jev-watchdog.timer inbox-jev-backup.timer
```

When a PR changes `deploy/systemd/`, copy the units and `daemon-reload` again after deploying it.

- Triage runs every 10 minutes, so a new email can sit in the inbox for up to ~10 minutes before it
  is handled; the digest runs daily at 08:00 America/Argentina/Buenos_Aires (edit
  `OnCalendar`/timezone in the `.timer` files).
- Run logs (JSON lines): `journalctl -u inbox-jev-triage -f`. Every line logged during an account's
  pass carries `run_id`, the id of its `runs` row. A run that fails for one account (for example an
  expired Gmail token, a missing `TYPESAFE_API_KEY`, or Jev rejecting the key or failing 3 calls in
  a row) is recorded in `runs.error` and logged once with its traceback ("account run failed", with
  that `run_id`), the other accounts still run, and the service exits non-zero so systemd shows the
  failure. `digest`, `unsubscribe-retry` and `senders backfill` also go on with the other accounts
  when one fails, and exit non-zero. Jev errors are stored and logged as type, HTTP status and
  request id only, never Jev's message (it can quote the email).
- Failures post a Slack alert and a watchdog checks that passes keep succeeding (see
  [Alerts](#alerts)); the DB is backed up off-site every night (see
  [Off-site backups](#off-site-backups)).
- Browse the DB: `docker compose --profile viewer up -d viewer`, then
  `ssh -L 8001:localhost:8001 <host>` and open http://localhost:8001. It is bound to localhost on
  the server, never public.

## Configuration checks

Settings and both YAML files are checked when a command starts, so a typo stops it with a message
instead of misbehaving later:

- `.env`: `MODE` is `dry_run`, `label` or `act`; `LOOKBACK` a Gmail `newer_than` value (`2d`, `3m`,
  `1y`); `LOG_LEVEL` one of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (any case);
  `MAX_MESSAGES_PER_RUN` and `BODY_CHARS` at least 1; `MAX_UNSUBSCRIBES_PER_RUN` at least 0 (0 never
  unsubscribes). The "Host-side ops scripts" settings (`ALERT_*`, `WATCHDOG_*`, `BACKUP_*`,
  `DEPLOY_BACKUPS_KEEP`) aren't the app's: the scripts that read them check them when they run (a
  count that isn't a whole number in range exits 2, which the watchdog and backup services turn into
  an alert).
- the categories file (`CATEGORIES_FILE`, `categories.yaml` by default): every `min_confidence`,
  `threshold`, `max_keep_probability` and `archive_group_*` value is between 0 and 1,
  `auto_rule.hits` at least 1, plus the checks in its comments.
- `accounts.yaml`: an account's `min_confidence` values are between 0 and 1, and its
  `act_categories` and `min_confidence` keys must be categories in the categories file. That
  cross-check covers the whole file, whatever `-a` names, in every command that reads it (`run`,
  `digest`, `unsubscribe-retry`, `senders backfill`, `labels rename`): the command exits 2 before
  opening the database or Gmail, with one message listing every unknown key of every account. No
  `runs` row is stored and no digest is sent while it fails, so after editing either file run
  `inbox-jev digest --dry-print` once to check them.

## Updating a deployment

CI builds the image once per commit, smoke-tests it and pushes it to GHCR on `main`
(`ghcr.io/<owner>/<repo>:sha-<short commit>`, and `:latest`), so production runs the image CI
tested. Once, give the server read access to the registry: `docker login ghcr.io` with a GitHub
token that has the `read:packages` scope (the package is private, like the repo). Then:

```bash
cd /opt/inbox-jev
scripts/deploy.sh            # or: scripts/deploy.sh --hold  when the PR has one-off steps
# with --hold: run the PR's deploy notes (reprocess, senders backfill, rules review…), then
sudo systemctl start inbox-jev-triage.timer
```

The script stops the triage timer and waits for a running pass, backs up the SQLite DB
(`data/inbox-jev.db.bak-<stamp>`, keeping the `DEPLOY_BACKUPS_KEEP` newest, 10 by default; older
ones are deleted, and so are older `.env.bak-<stamp>` copies), fetches and checks out `origin/main`
(detached: the checkout only ever holds what is deployed), pulls the image for that commit and
writes its tag to `.env` as `IMAGE_TAG` (the timers' `docker compose run` reads it), checks that
`inbox-jev --version` inside the image reports the checkout's commit, and starts the timer. If a
step fails after the timer was stopped, it says so and leaves the timer stopped for you to fix and
start by hand. Without registry access, `scripts/deploy.sh --build` builds the image on the server
instead. Rollback: `scripts/deploy.sh <previous sha>` (CI keeps an image for every commit on `main`
whose CI passed; for a commit older than the registry, `scripts/deploy.sh --build <sha>`).

New `act_categories` keys go into `accounts.yaml` only after the code that knows them is deployed
(see [Rolling out a category change](#rolling-out-a-category-change)).

## Rolling out `act` and reprocessing

To roll out `act` gradually, set `act_categories` on the account in `accounts.yaml`, for example
`[transactional, notification, newsletter, cold_outreach]`. Only those categories are archived,
spammed or unsubscribed, and the rest just get their labels. Emails still in the inbox that were
seen in a weaker mode (`dry_run`, or `label` when running `act`) are processed once more when you
switch, so they are covered too. Labels from the previous pass that no longer apply are removed.
Widening `act_categories` later only affects new emails, unless you mark the old ones with
`inbox-jev reprocess` (below).

- `act_categories` is matched against the final category. Listing `marketing` or `cold_outreach`
  archives every confident email of that category but only unsubscribes from the verified ones
  (DMARC + `List-Unsubscribe`, not a group, and for `cold_outreach` not a known contact); listing
  `spam` only spams what Jev itself calls spam.
- `act_categories: []` acts on nothing. Leaving the key empty (for example with every entry
  commented out) is rejected; remove the key entirely to act on every category.
- Emails you restored with `inbox-jev restore` are never processed again. If you changed a Jev
  category label since the last pass, that is learned as a correction first. Otherwise, if you
  removed one of Jev's labels (for example `Jev/Needs Reply`), added one Jev hadn't set (for example
  `Jev/Needs Reply`, or a second category label), or starred the email, it is left alone: not
  archived, relabeled or revisited. A starred email is left alone even after a correction. Starring
  only counts for emails seen in an earlier pass; new starred emails are processed normally.

To redo emails Jev already handled in `act` mode but left in the inbox (for example after adding a
category to `act_categories`, or after a category's meaning changed), mark them and run again with a
window wide enough to reach the oldest one:

```bash
inbox-jev reprocess -a you@example.com --category spam --low-confidence-only
inbox-jev run -a you@example.com --lookback 1y --limit 500
```

`reprocess` only marks rows that are done, still in the inbox (not archived, spammed or restored)
and were handled in `act` mode, and sets them back to `label`. The next `act` run sends them through
the normal path: emails you edited are left alone, stale labels are removed.

To clean up an existing inbox, run one pass with a longer window:
`inbox-jev run --lookback 30d --limit 500`. `--lookback` takes Gmail `newer_than` values (`2d`,
`3m`, `1y`). Every email reprocessed by that run counts as processed today, so that day's digest
shows the whole backlog, including re-listed "needs reply" and low-confidence emails.

## Phishing that reaches the inbox

Jev gets phishing signals (`reply_without_thread`, `calendar_subject_without_invite`,
`link_domains`, `sender_auth`) and the spam criteria look for mismatches, but no rule spams on them,
and `spam` needs 0.90, like the unsubscribing categories
([§4.7](how-it-decides.md#47-what-jev-receives),
[ADR 0042](decisions/0042-phishing-signals-for-jev.md)). Phishing Jev is less sure of stays in the
inbox with `Jev/Review` by design. Don't lower `spam`'s `min_confidence` to catch it: genuine
document, e-signature and tax-portal notices sit close to it, and a lower bar moves them to Spam.

- **The user's fix**: "Report phishing" in Gmail and, within 7 days, change the email's `Jev/` label
  to `Jev/Spam`, or, if Jev already suggested `Jev/Spam` (it is there next to `Jev/Review`), remove
  `Jev/Review` (it still counts once the email is in Spam). Leaving both labels on teaches nothing.
  The next pass learns a `correction` rule to `spam` on the exact resolved sender
  ([§3.7](how-it-decides.md#37-corrections-user-edits-and-restore)): its next emails skip Jev and
  are moved to Spam in `act` on an account that acts on `spam` (`act_categories`), only labeled
  otherwise. A sender in `decide_each_email` gets no rule
  ([ADR 0045](decisions/0045-senders-decided-per-email.md)), and neither does an email sent as a
  list's own address ([ADR 0051](decisions/0051-no-learned-rules-on-a-lists-own-address.md)).
- **Group-relayed phishing**: for a group on the account's `group_domains`, the sender is the author
  (`X-Original-Sender`), so the rule is on the author, never the group, and Jev's `sender_auth` is
  the author's from Google's ARC record, or all `none`, never the group's pass
  ([ADR 0050](decisions/0050-group-relayed-mail-carries-its-authors-authentication.md)). For any
  other group that rewrites `From` to its own address (Gmail's "via"), the sender is the group
  address, which its `List-Post` or `Mailing-list` names as the list's own: the correction fixes
  that email only, and no learned rule decides the group's mail
  ([ADR 0051](decisions/0051-no-learned-rules-on-a-lists-own-address.md)). List the group's domain
  in `group_domains` if it is a Workspace you control, so rules key on the authors
  ([§3.4](how-it-decides.md#34-groups-and-mailing-lists)). Learned rules on a group address from
  before ADR 0051 are inert for its list mail; find them with `inbox-jev rules list --all` and
  `inbox-jev rules delete -a you@example.com group@example.com`. To send all of a group's mail
  somewhere on purpose, set a manual rule (`rules set` warns that it covers the whole list).
- **Phishing that rotates addresses** on its own domain:
  `inbox-jev rules set -a you@example.com @attacker.example spam`. Never for a public mail provider,
  an ESP's shared sending domain or a domain you also get real mail from: a `@domain` rule covers
  every address on that exact domain (not its subdomains) that has no rule of its own.
- **Authentication fails closed**: Gmail's `Authentication-Results` is parsed strictly per RFC 8601
  ([ADR 0043](decisions/0043-authentication-results-parsed-per-rfc-8601.md)) and a group author's
  ARC record only in Google's current layout. If Gmail or Google Groups change what they write,
  those emails lose their verdicts: unverified, so never unsubscribed, and `none` for Jev. A trusted
  group's mail also falls back to the group address as sender, since the author is only recovered
  when the group's own DMARC passes (`signals.original_sender`): that address is the list's own, so
  no learned rule decides those emails and a correction fixes only the one email (ADR 0051), but Jev
  and the digest see the group instead of the author. The symptom is unsubscribes stopping for
  senders that used to pass: only emails that would have been unsubscribed get the `messages` row's
  `downgraded_to_spam` column ("unsubscribe skipped: unverified") set, the others show nothing. See
  "Known gaps" in the spec.

## Changing categories

### Your own categories

The shipped [`categories.yaml`](../categories.yaml) is a default starting point that reflects one
inbox owner's preferences: website form leads and meetings booked with them are `action_required`,
newsletters are archived, and so on. Tune it, in your own copy (below), to how you want your mail
sorted. The brand names in its criteria (HubSpot, Calendly, Typeform, SafeSend…) are examples that
help Jev recognise a kind of email, not rules: no code matches on them, and you can drop or replace
them.

An installed copy (`uv tool install inbox-jev`) reads the defaults from the package, so an upgrade
brings their changes, until you put your own `categories.yaml` in its configuration folder
(`~/.config/inbox-jev/`, or `INBOX_JEV_HOME`): from then on it reads yours. Start from the shipped
one (`categories.yaml` in this repo, at the version you run).

Don't edit the tracked file on a deployment: `scripts/deploy.sh` checks out each release over it.
Keep your own copy next to it instead:

```bash
cd /opt/inbox-jev
cp categories.yaml categories.local.yaml        # gitignored, like accounts.yaml
$EDITOR categories.local.yaml
echo 'CATEGORIES_FILE=categories.local.yaml' >> .env   # or edit the existing line
docker compose run --rm app digest --dry-print  # checks both files, sends nothing
```

`CATEGORIES_FILE` is a file name in the checkout: `docker-compose.yml` mounts it read-only at the
same path under `/app` and the app reads it from there, so the same line works on the host and in
the container. Only a plain file name in the checkout (like `categories.local.yaml`) is supported,
and the file must exist before you set it: `docker compose` refuses to start when it's missing. The
image's own `categories.yaml` is then unused. Like `accounts.yaml`, the copy isn't in git: back it
up yourself.

- **Keep the `important` category.** The allowlist and the emails you pull back to the inbox use it,
  and validation refuses a file without it. Every other category can be renamed, removed or added.
- **Keep `accounts.yaml` in sync.** Its `act_categories` and `min_confidence` keys must be
  categories in your file, or every command refuses to start (see
  [Configuration checks](#configuration-checks)). Rename or remove a category in both files, in the
  order given in [Rolling out a category change](#rolling-out-a-category-change).
- **Validate** after every edit with `docker compose run --rm app digest --dry-print`, and check how
  your criteria sort real mail, read-only, with
  `scripts/check_branch.sh main --account you@example.com --query 'in:inbox newer_than:7d'`: it runs
  `main`'s code with your file (see
  [Checking a branch against real mail](development.md#checking-a-branch-against-real-mail)).
- **Before an upgrade**, see what changed in the defaults
  (`git fetch origin && git diff HEAD origin/main -- categories.yaml`) and merge what the release
  needs and the improvements you want into your copy before running `scripts/deploy.sh`: the shipped
  criteria get better as mistakes are found, and a release may add a category or a setting that its
  code expects. `deploy.sh`'s smoke test doesn't read the categories file, so run
  `docker compose run --rm app digest --dry-print` right after the deploy (afterwards,
  `diff categories.local.yaml categories.yaml` shows what you haven't taken).

### Rolling out a category change

- **Deploy first** (`scripts/deploy.sh`), **then** add a new key to `act_categories` (or
  `min_confidence`) in the server's `accounts.yaml`: a key the running code's `categories.yaml`
  doesn't have makes every command that reads `accounts.yaml` refuse to start, `run` and `digest`
  included (see [Configuration checks](#configuration-checks)). Until it is listed, the new category
  is only labeled and stays in the inbox, which is safe. On rollback, remove the key from
  `accounts.yaml` **first**, then deploy the old code.
- When a category's meaning changes (for example `spam` narrowed to fraud), review
  `inbox-jev rules list --all` for rules with that category (auto, correction or manual) and
  `rules delete` the ones that no longer fit: a rule skips Jev, so it keeps the old meaning.
- When you remove a category, its rules are ignored (each logged once per run as
  `sender rule ignored: unknown category`) and their senders go through Jev; `rules delete` them.

### Adding a category / label

Add an entry to the categories file (`categories.yaml`, or your own copy). No code changes are
needed: the Gmail label is created on the next run and Jev starts choosing it.

```yaml
- key: recruiting
  label: Jev/Recruiting
  criteria: "Candidates, recruiters or job platforms"
  action: keep            # keep | archive | archive_unsubscribe | spam
  min_confidence: 0.8
```

### Renaming a label

Changing a `label:` in `categories.yaml` alone makes the next run create a new, empty label and
leaves the old one on past emails. To rename instead, stop the triage timer, update
`categories.yaml`, then run `inbox-jev labels rename "Jev/Old" "Jev/New"` for each label before the
next pass. Gmail keeps the label id, so emails keep it, and the DB history is rewritten so `restore`
and the digest still match. It is safe to re-run. If a triage pass already created NEW as an empty
label, it is deleted and OLD is renamed onto it; if NEW already has emails, the command refuses. It
also refuses labels with children (`Jev/Old/...`): the API renames only that label, not its
sublabels, so rename the children first (or use the Gmail web UI).

## Daily digest

One Slack DM per account (`slack_user_id`), by default at 08:00. It lists only what still needs you.
Before listing an email it reads its Gmail thread (read-only, one batched call for all of them): an
email no longer in the inbox (archived, deleted) or with a message you sent in its thread dated
after it (you replied) is dropped. A low-confidence email without the review label any more (you
accepted or corrected the suggestion) leaves the low-confidence count, but stays under Needs reply
if it is there and you haven't replied. The totals line says how many emails no section shows any
more ("Already handled: 4"). If that read fails (token, Gmail API), the digest is still sent,
listing them all, with a line saying the check failed.

It is one Slack message laid out with Block Kit (`src/inbox_jev/slack.py`), read top to bottom on a
phone:

1. A header, "📬 Inbox Jev · Thursday, Oct 15" (the UTC date), and a small grey line with the
   account, the period and the mode. Under it, only when Google will end the account's Gmail
   authorization (a time-limited grant, see [Gmail authorization](#gmail-authorization)), a ⚠️ alert
   with the date, how many days are left (or that it already ended) and the fixes.
2. One bold sentence: how many emails need you ("5 emails need you"), or "Inbox zero — nothing needs
   you today ✨".
3. What needs you, pending ones only: one group per keep category other than `important` (🔔, e.g.
   `Jev/Action`) and ⚡ Needs reply. Each email is its subject in bold, linking to it in Gmail
   (subjects cut at 60 characters), and under it the sender's name (or address) and how long ago it
   arrived. At most 10 per group, then "+N more · see all in Gmail", a link to that label's search
   in the inbox. Calendar messages (an iTIP part: invitations, updates, cancellations, responses)
   are never listed there nor counted under To review, but still count in the table
   ([ADR 0049](decisions/0049-digest-leaves-calendar-messages-out.md)); rows stored before the
   `messages.calendar_method` column existed are listed as before.
4. 🤔 To review: how many of the period's low-confidence emails are still in the inbox with the
   review label, linking to the Gmail search `label:jev-review in:inbox` (which also shows older
   ones), and one line without a link for those a `dry_run` pass left unlabeled.
5. 📊 What Jev did: a native Block Kit table, one row per category with its count (most first, ties
   by label), then To review, the Processed total in bold, and Already handled under it (not part of
   the total: it counts emails the digest would have listed, not processed ones). Category labels
   longer than 28 characters are cut. An empty period has no table, only "Nothing processed".
   Category labels show as inline code wherever the digest names one (the 🔔 group titles, the review
   label, the table's category rows, the corrections, where the sender is code too), a backtick in
   one as `'`; the text version's table, a code block already, shows them plain.
6. Unsubscribes (🚫 done, 🖐 waiting for a manual click with its link, ❗ failed), in full.
7. 🧠 Corrections learned: the ones picked up in the period, newest first, one per line with the
   sender and the category labels (`• noreply@example.com: Jev/Important → Jev/Spam`, the sender and
   each label as code, so an underscore in an address can't start an italic; or "(suggestion
   accepted)" when the correction kept a low-confidence guess), marked "(this email only)" for a
   sender in `decide_each_email`, which gets no sender rule (at most 15, then "+N more").
8. ⚠️ Errors, only when there are any: failed runs (identical ones grouped, e.g. an expired Gmail
   token, with the fix: `run inbox-jev auth add EMAIL`, or "Jev unavailable") and emails that
   stopped being retried. An email that fails is retried with a backoff (5, 10, 20 … minutes) and
   given up after 8 failures, about 10.5 hours (at once when Jev refuses that email with a 4xx; when
   Jev refuses two emails in a row the same way, the run fails and each counts an ordinary failure);
   `inbox-jev retry-errors` retries them again. Failed emails are listed by subject and sender
   (details: [docs/how-it-decides.md §1](how-it-decides.md#1-the-pass-over-an-inbox)).
9. A footer with a tip.

Links are plain mrkdwn links, not buttons: a url button still sends the app an interaction payload,
and this app has no interactivity Request URL. Everything taken from an email is escaped for Slack
(`<!channel>`, `<@U…>` and fake links stay text), and every mrkdwn text is sent `verbatim`, so Slack
doesn't turn a plain `@here`, `@channel` or `#general` in a subject into a mention or channel link;
the table's cells are raw text, and the text version is posted without `link_names`. The layout
keeps to Block Kit's limits (50 blocks, 3,000 characters per section, 150 per header, 100 table
rows), counting what it leaves out as "+N more" (the table sums its last categories in one "+N more"
row, so the total still adds up). The message's `text`, which push notifications and screen readers
show, is a one-line summary ("5 emails need you · 87 processed"). If Slack rejects the blocks
(`invalid_blocks`), a warning is logged with Slack's details (which name the block, not its text)
and the digest is sent again as plain mrkdwn text, so a bad block never loses it.

Preview it with `inbox-jev digest --dry-print` (the text version) or `--dry-print --blocks` (the
Block Kit JSON, to paste into [Block Kit Builder](https://app.slack.com/block-kit-builder)); both
read Gmail too, read-only, for the pending check. `inbox-jev digest --preview -a EMAIL` sends one
digest marked "[preview]" to that account's own `slack_user_id`, to see a layout change in Slack.

The digest's language is set per account with `digest_language` in `accounts.yaml`: `en` (default)
or `es`; any other value is rejected when the file is loaded. Category and label names come from
`categories.yaml` and are shown as written there. The texts live in one table per language
(`MESSAGES` in `src/inbox_jev/digest.py`); to add a language, add a table there and its code to
`DigestLanguage` in `config.py` (a test checks every table has every text with the same
placeholders). Counts inside a sentence ("1 email", "2 emails") have a `one` and an `other` form.

## Gmail authorization

`inbox-jev auth status` lists each account's authorization: missing, or ending on a date Google set,
with the fixes; it exits 1 when any account has one of those problems.

Google ends an authorization on a set date only for a time-limited grant
([ADR 0052](decisions/0052-each-install-brings-its-own-oauth-client.md)), which has two causes:

- the OAuth client's consent screen is *External* and in *Testing*: Google limits every grant to 7
  days;
- the user chose time-limited access on Google's consent screen, whatever the app's status.

Its token response then says when (`refresh_token_expires_in`), which `auth add` (and `init`) stores
in the token file as `refresh_token_expires_at`. From then on:

- `auth add` and `init` print "Google will end this authorization on DATE (a time-limited grant)"
  with both causes and their fixes, and `auth status` repeats it ("already ended" once the date has
  passed);
- every digest starts with a ⚠️ alert until the account is authorized again without a limit;
- once it has ended, each run fails with `invalid_grant`; the run error (in the digest and the
  alerts) says to run `auth add`, and that weekly failures mean a Testing app.

The fix depends on the cause:

- **A Testing app**: first, in the project of the OAuth client, Google Auth Platform → Audience
  (<https://console.cloud.google.com/auth/audience>) → **Publish app**, so it is *In production*. It
  stays unverified, which is fine for your own accounts (Google shows a one-time warning when
  authorizing: Advanced → Go to the app). An *Internal* app (every account in your Workspace) has no
  7-day limit either. A token authorized while the app was in Testing keeps its limit after
  publishing: only a new authorization drops it, so then do as below.
- **Either cause**: `inbox-jev auth add EMAIL` once per account (on a laptop: it needs a browser),
  without limiting the access on the consent screen, copy `secrets/tokens/EMAIL.json` to the server
  as in [Deploy](#deploy-docker--systemd-timers), and check with `inbox-jev auth status`.

A token authorized before this was recorded has no date in its file, so nothing warns ahead: the
weekly `invalid_grant` is then the sign.

## Alerts

Production failures post to Slack, so they aren't only in the journal. The scripts run on the host
(bash, curl, the host's `python3` standard library, `journalctl`), never in the app image, so an
alert still goes out when the image is broken or can't be pulled. They read their settings from
`.env` (the "Host-side ops scripts" section of `.env.example`) and post with the digest's
`SLACK_BOT_TOKEN` (scope `chat:write`), which they hand to curl on stdin, never on a command line or
in a log.

- **A failed run.** The triage, digest, backup and watchdog services have
  `OnFailure=inbox-jev-alert@%n.service`, which runs `scripts/alert.sh <unit>`. The alert has the
  unit, host, time, the unit's result and exit status, and the failed run's last `ALERT_LOG_LINES`
  (20) app log lines, reduced to time, level, `msg`, `account`, `run_id`, the run counts and the
  exception's type (the `exc_type` field the app's log formatter writes). `msg` is kept only for the
  app's own loggers (`inbox_jev…`), whose messages are fixed texts; a library's line
  (googleapiclient, urllib3, the Jev SDK) keeps only its logger name and level, since its message
  can quote a request URL or a response. Tracebacks (whose message can quote a Gmail or HTTP
  response), senders, labels and anything else stay in the journal. A unit that keeps failing (an
  expired Gmail token fails every pass) is alerted about once, then again only after
  `ALERT_REPEAT_HOURS` (6).
- **Staleness.** `inbox-jev-watchdog.timer` runs `scripts/watchdog.sh` every 30 minutes. It alerts
  when no triage pass succeeded (systemd's "Finished" job message) in the last
  `WATCHDOG_MAX_AGE_MINUTES` (60): the timer is stopped (a `deploy.sh --hold` left that way), passes
  hang until their timeout, or every pass fails. Once per incident, then a "succeed again" message
  when passes are back. When the passes fail, `alert.sh` has already alerted about the first
  failure, so the watchdog stays quiet while that alert is younger than `ALERT_REPEAT_HOURS` and
  reminds once after that. It also posts "succeeds again" for each unit `alert.sh` alerted about
  once its last run succeeded (for the watchdog itself: its next run that gets that far).
- `ALERT_SLACK_CHANNEL` is a channel ID (`C…`, invite the bot) or a user ID (`U…`, a DM from the
  bot). Unset, alerts are off: each would-be alert logs a line instead. The dedupe state lives in
  `/var/lib/inbox-jev` (systemd's `StateDirectory=`).

Test an alert by hand: `sudo /opt/inbox-jev/scripts/alert.sh inbox-jev-triage.service` (it describes
the unit's last run; delete `/var/lib/inbox-jev/alerted-inbox-jev-triage.service` to send it again).

## Off-site backups

`inbox-jev-backup.timer` runs `scripts/backup.sh` every night at 04:15
America/Argentina/Buenos_Aires. It snapshots `data/inbox-jev.db` with sqlite3's `.backup` (the
online backup API: a consistent copy, WAL included, while a pass may be writing; sqlite3 runs as
`APP_UID`, so a `-wal` or `-shm` file it creates stays the app's), checks it with
`PRAGMA quick_check`, gzips it, uploads it with `rclone copyto` to `BACKUP_RCLONE_REMOTE` as
`inbox-jev-<UTC stamp>.db.gz`, and deletes the remote's backups older than `BACKUP_RETENTION_DAYS`
(30): only `inbox-jev-*.db.gz` at the remote's top level, never in a subfolder (keep a copy by hand
in one, e.g. `pre-migration/`). A failure posts an alert. It is off while `BACKUP_RCLONE_REMOTE` is
unset. Only SQLite is backed up: with a hosted `DATABASE_URL`, use the provider's backups.

One-time setup, as root on the server (rclone's config is root's: the service runs as root):

```bash
sudo apt-get install -y rclone      # or https://rclone.org/install/ for a newer one
# rclone's Google sign-in redirects to localhost:53682 on the server: forward that port from
# your laptop first, in another terminal:  ssh -L 53682:localhost:53682 root@<server>
sudo rclone config create gdrive drive scope=drive.file
# open the URL it prints in your laptop's browser and allow access. drive.file only lets rclone
# see the files it creates, not the rest of the Drive.
sudo rclone mkdir gdrive:inbox-jev-backups
echo 'BACKUP_RCLONE_REMOTE=gdrive:inbox-jev-backups' | sudo tee -a /opt/inbox-jev/.env
sudo systemctl start inbox-jev-backup.service && journalctl -u inbox-jev-backup -n 20
```

Restore: `rclone copyto gdrive:inbox-jev-backups/<file>.db.gz ./restore.db.gz`, `gunzip` it, stop
the triage timer, replace `data/inbox-jev.db` (removing its `-wal` and `-shm`), give it to
`APP_UID:APP_GID`, and start the timer.

## Database

`DATABASE_URL` is any SQLAlchemy URL. Tables are created on first run.

| Table | Contents |
| -- | -- |
| `messages` | Every processed email: category, confidence, probabilities, labels, archived/spammed, corrections |
| `unsubscribes` | One row per (account, list): method, target, status, HTTP status, error |
| `sender_rules` | manual / correction / auto rules |
| `sent_domain_scans` | The sent-mail domain search's progress per (account, domain): span read, verdict (`scanning` / `person` / `never`) and when |
| `runs` | Each triage pass: processed, errors, and why the run failed (`error`) |

SQLite databases are opened in WAL mode with a 30 s busy timeout, so the digest, the viewer and
manual commands can read while a triage pass writes, and concurrent writers wait instead of failing
with "database is locked". The `-wal` and `-shm` files next to the DB are part of it. A read-only
reader needs `data/` writable (SQLite creates `-shm`) or immutable mode: the viewer opens it with
`datasette -i`, and `scripts/dry_classify.py` runs with `data/` mounted read-write.

New columns are added on startup (`create_all` plus a small "add missing columns" step, with their
default for existing rows); there is no other migration tool.

To use a hosted DB, install its driver and change the URL. For example, Postgres on Neon:
`uv add 'psycopg[binary]'` (commit `pyproject.toml` and `uv.lock`, then rebuild the image with
`docker compose build`) and `DATABASE_URL=postgresql+psycopg://…`. SQLite and Postgres are the
databases supported: the sent-mail domain scan is saved with `INSERT … ON CONFLICT`, so on any other
database every `run` (`dry_run` included) and `unsubscribe-retry` fail each account at their start,
before reading its mail or sending anything.
