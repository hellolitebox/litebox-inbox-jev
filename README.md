# inbox-jev

Triage your Gmail inbox with [TypeSafe Jev](https://typesafe.ai). Every few minutes it reads
new inbox emails, asks Jev which category they belong to (with a calibrated confidence), and:

- **labels** them (`Jev/Important`, `Jev/Action`, `Jev/Marketing`, …),
- **archives** receipts, tool notifications, newsletters, marketing and cold sales outreach,
- **unsubscribes** from verified marketing and cold-outreach sequences (RFC 8058 one-click or
  `mailto:`) and **moves scams/phishing to Spam**,
- keeps what needs you in the inbox: personal email (`Jev/Important`) and automated email that
  needs an action (`Jev/Action`: approve a payment, update a card, verify a domain…),
- leaves anything it is **not sure about** in the inbox with `Jev/Review`,
- **learns** from your corrections (change a label or move an email back → sender rule),
- sends a **daily Slack digest** of what it did.

Everything is recorded in a database (SQLite by default) so it can be audited and undone.

## Categories and labels

Defaults from [`categories.yaml`](categories.yaml) (editable, see
[Adding a category](#adding-a-category--label)):

| Category | Label | Action | `min_confidence` |
|---|---|---|---|
| `important` | `Jev/Important` | keep | — |
| `action_required` | `Jev/Action` | keep | — |
| `transactional` | `Jev/Transactional` | archive | 0.80 |
| `notification` | `Jev/Notifications` | archive | 0.80 |
| `newsletter` | `Jev/Newsletters` | archive | 0.80 |
| `marketing` | `Jev/Marketing` | archive + unsubscribe | 0.90 |
| `cold_outreach` | `Jev/Outreach` | archive + unsubscribe (never from known contacts) | 0.90 |
| `spam` (scams, phishing) | `Jev/Spam` | move to Spam | 0.90 |

Extra labels: `Jev/Needs Reply` (Jev thinks the sender expects a reply or a personal action, not
a company survey, review request or promotion; only on emails that stay in the inbox),
`Jev/Invoices` (tag: an invoice to pay), `Jev/Review` (low confidence) and `Jev/Processed`
(technical marker).

## How it decides

The full functional spec, with flowcharts, decision tables, the exact checks and worked examples,
is in **[docs/how-it-decides.md](docs/how-it-decides.md)**. The reasons behind each rule, and the
alternatives that were rejected, are in **[docs/decisions/](docs/decisions/README.md)**. In short,
for each new inbox email:

1. **Allowlist** (`allow_domains` / `allow_senders` in `accounts.yaml`): always *important*, never
   archived. Jev is asked only for "needs a reply" and the tags.
2. **Sender rules** decide the category. They come from your corrections, from
   `inbox-jev rules set`, or are created automatically after 3 consistent answers at ≥ 0.95 in
   `label`/`act` (never in `dry_run`, never from a group archive, a known contact's pitch, or an
   answer with some *keep* probability, so senders that mix routine and action-needed mail keep
   going through Jev). Rules to archive or spam categories skip Jev; rules to keep categories
   still ask it for "needs a reply" and the tags, like the allowlist (if that call fails, the
   email is still labeled, just without those flags). A rule whose category was removed from
   `categories.yaml` is ignored (with a warning in the log): Jev decides, even when a `@domain`
   rule also matches.
3. **Jev** gets the headers that matter, the start of the body (`BODY_CHARS`; the HTML when the
   `text/plain` part is just a "view this in your browser" stub) and `previous_contact`: whether
   you ever wrote to the sender or a person at their domain, from a search over your sent mail
   (emails sent only to machine addresses, like `noreply@` or a notification's
   `…@bounce.linkedin.com` reply address, don't count). It answers
   with a category and its probabilities, "needs a reply" and the tags.
4. **Policy** turns the answer into actions, and the **mode** decides how much of that is applied:
   - at or above a category's `min_confidence`, its action applies;
   - below it, the email is still archived when Jev's top answer is an archive category and the
     archive categories together reach 0.90 (the *archive group*: never unsubscribes or spams;
     an email that "expects a reply" stays unless it's a stranger's pitch or a no-reply
     notification with no keep signal);
   - otherwise nothing destructive happens: `Jev/Review` plus the suggested label.

### Unsubscribe safety

An unsubscribe is irreversible, so it only happens in `act` mode (and `act_categories`), for
*marketing* and *cold_outreach* at their own 0.90 confidence, when the sender passes DMARC and
sends a `List-Unsubscribe` header (links in the body are never followed), never through a group
or discussion list (`List-Post`, Google Groups, your own domain), never for cold outreach from
someone you have written to, once per list and at most `MAX_UNSUBSCRIBES_PER_RUN` per run. It
uses RFC 8058 one-click, else `mailto:`, else leaves a manual link in the digest. Sender rules and
`inbox-jev unsubscribe-retry` go through the same checks. Details:
[docs/how-it-decides.md §3.3](docs/how-it-decides.md#33-unsubscribe-conditions).

### Groups and mailing lists

Google Groups rewrites `From` to the group. For groups on a domain listed in the account's
`group_domains` (opt-in, empty by default; only domains whose Google Workspace you control), the
author from `X-Original-Sender` is used as the sender, so rules, corrections, the allowlist,
`previous_contact` and the digest apply to the person. On those groups `allow_domains` matches
the author, not the group: allowlist a group by its exact address in `allow_senders`. Other
groups stay keyed on the group address. Group emails stored before this existed can be fixed
with `inbox-jev senders backfill` (safe to run again). An entry that is a public suffix
(`co.uk`, `wa.gov.au`, `github.io`, from the [Public Suffix List](https://publicsuffix.org)
shipped with the package; a weekly check opens an issue when a newer one is out) fails the accounts file. Details:
[docs/how-it-decides.md §3.4](docs/how-it-decides.md#34-groups-and-mailing-lists).

### Modes

| `MODE` | DB + digest | Gmail labels | archive / spam / unsubscribe |
|---|---|---|---|
| `dry_run` | ✅ (learns no sender rules) | | |
| `label` | ✅ | ✅ | |
| `act` | ✅ | ✅ | ✅ |

Start with `dry_run` or `label` for a week or two. Switch to `act` when the labels look right.

To roll out `act` gradually, set `act_categories` on the account in `accounts.yaml`, for example
`[transactional, notification, newsletter, cold_outreach]`. Only those categories are archived,
spammed or unsubscribed, and the rest just get their labels. Emails still in the inbox that were
seen in a weaker mode (`dry_run`, or `label` when running `act`) are processed once more when you
switch, so they are covered too. Labels from the previous pass that no longer apply are removed.
Widening `act_categories` later only affects new emails, unless you mark the old ones with
`inbox-jev reprocess` (below).

- `act_categories` is matched against the final category. Listing `marketing` or
  `cold_outreach` archives every confident email of that category but only unsubscribes from the
  verified ones (DMARC + `List-Unsubscribe`, not a group, and for `cold_outreach` not a known
  contact); listing `spam` only spams what Jev itself calls spam.
- `cold_outreach` used to be archive-only. Since it became `archive_unsubscribe`, an account that
  already lists it in `act_categories` starts unsubscribing on deploy, with no separate opt-in.
  That includes emails still in the inbox from a weaker mode within `--lookback` (re-run on the
  first `act` pass), rows marked with `inbox-jev reprocess --category cold_outreach`, and any
  existing manual, correction or active auto rule with `cold_outreach` (they unsubscribe verified
  senders straight away; review them with `inbox-jev rules list --all`). Remove it from
  `act_categories` first if you want to hold that back.
- `act_categories: []` acts on nothing. Leaving the key empty (for example with every entry
  commented out) is rejected; remove the key entirely to act on every category.
- Emails you restored with `inbox-jev restore` are never processed again. If you changed a Jev
  category label since the last pass, that is learned as a correction first. Otherwise, if you
  removed one of Jev's labels (for example `Jev/Needs Reply`), added one Jev hadn't set (for
  example `Jev/Needs Reply`, or a second category label), or starred the email, it is left alone:
  not archived, relabeled or revisited. A starred email is left alone even after a correction.
  Starring only counts for emails seen in an earlier pass; new starred emails are processed
  normally.

To redo emails Jev already handled in `act` mode but left in the inbox (for example after adding
a category to `act_categories`, or after a category's meaning changed), mark them and run again
with a window wide enough to reach the oldest one:

```bash
inbox-jev reprocess -a you@example.com --category spam --low-confidence-only
inbox-jev run -a you@example.com --lookback 1y --limit 500
```

`reprocess` only marks rows that are done, still in the inbox (not archived, spammed or
restored) and were handled in `act` mode, and sets them back to `label`. The next `act` run sends
them through the normal path: emails you edited are left alone, stale labels are removed.

To clean up an existing inbox, run one pass with a longer window:
`inbox-jev run --lookback 30d --limit 500`. `--lookback` takes Gmail `newer_than` values (`2d`,
`3m`, `1y`). Every email reprocessed by that run counts as processed today, so that day's digest
shows the whole backlog, including re-listed "needs reply" and low-confidence emails.

### Rolling out a category change

- **Deploy first** (`scripts/deploy.sh`), **then** add a new key to `act_categories` (or
  `min_confidence`) in the server's `accounts.yaml`: a key the running code's `categories.yaml`
  doesn't have makes every command that reads `accounts.yaml` refuse to start, `run` and
  `digest` included (see [Configuration checks](#configuration-checks)). Until it is listed, the
  new category is only labeled and stays in the inbox, which is safe. On rollback, remove the key
  from `accounts.yaml` **first**, then deploy the old code.
- When a category's meaning changes (for example `spam` narrowed to fraud), review
  `inbox-jev rules list --all` for rules with that category (auto, correction or manual) and
  `rules delete` the ones that no longer fit: a rule skips Jev, so it keeps the old meaning.
- When you remove a category, its rules are ignored (each logged once per run as `sender rule
  ignored: unknown category`) and their senders go through Jev; `rules delete` them.

### Adding a category / label

Add an entry to `categories.yaml`. No code changes are needed: the Gmail label is created on the
next run and Jev starts choosing it.

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
`categories.yaml`, then run `inbox-jev labels rename "Jev/Old" "Jev/New"` for each label before
the next pass. Gmail keeps the label id, so emails keep it, and the DB history is rewritten so
`restore` and the digest still match. It is safe to re-run. If a triage pass already created
NEW as an empty label, it is deleted and OLD is renamed onto it; if NEW already has emails, the
command refuses. It also refuses labels with children (`Jev/Old/...`): the API renames only that
label, not its sublabels, so rename the children first (or use the Gmail web UI).

## Setup

### 1. Credentials
- **TypeSafe**: API key from the TypeSafe dashboard → `TYPESAFE_API_KEY`. `inbox-jev run` fails
  each account's run without it (nothing is touched); the other commands don't need it.
- **Google**:
  1. In Google Cloud Console, enable the Gmail API.
  2. Configure the OAuth consent screen. Use *Internal* if all accounts are in your Workspace;
     otherwise use *External* in testing mode and add each account as a test user.
  3. Create an OAuth client of type **Desktop app** and save its JSON as
     `secrets/client_secret.json`.
- **Slack** (optional): a bot with the `chat:write` scope → `SLACK_BOT_TOKEN`. Add each person's
  member ID as `slack_user_id` in `accounts.yaml`.

```bash
cp .env.example .env                       # fill it in
cp accounts.example.yaml accounts.yaml     # your accounts + allowlists
uv sync --extra dev                        # exact versions from uv.lock
uv run pre-commit install                  # gitleaks + ruff before every commit
uv run inbox-jev auth add you@example.com  # opens the browser; token → secrets/tokens/
```

### Configuration checks

Settings and both YAML files are checked when a command starts, so a typo stops it with a
message instead of misbehaving later:

- `.env`: `MODE` is `dry_run`, `label` or `act`; `LOOKBACK` a Gmail `newer_than` value (`2d`,
  `3m`, `1y`); `LOG_LEVEL` one of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (any case);
  `MAX_MESSAGES_PER_RUN` and `BODY_CHARS` at least 1; `MAX_UNSUBSCRIBES_PER_RUN` at least 0 (0
  never unsubscribes). The "Host-side ops scripts" settings (`ALERT_*`, `WATCHDOG_*`,
  `BACKUP_*`, `DEPLOY_BACKUPS_KEEP`) aren't the app's: the scripts that read them check them when
  they run (a count that isn't a whole number in range exits 2, which the watchdog and backup
  services turn into an alert).
- `categories.yaml`: every `min_confidence`, `threshold`, `max_keep_probability` and
  `archive_group_*` value is between 0 and 1, `auto_rule.hits` at least 1, plus the checks in
  its comments.
- `accounts.yaml`: an account's `min_confidence` values are between 0 and 1, and its
  `act_categories` and `min_confidence` keys must be categories in `categories.yaml`. That
  cross-check covers the whole file, whatever `-a` names, in every command that reads it (`run`,
  `digest`, `unsubscribe-retry`, `senders backfill`, `labels rename`): the command exits 2 before
  opening the database or Gmail, with one message listing every unknown key of every account. No
  `runs` row is stored and no digest is sent while it fails, so after editing either file run
  `inbox-jev digest --dry-print` once to check them.

### 2. Try it locally
```bash
uv run inbox-jev run --mode dry_run --limit 30
uv run inbox-jev stats
uv run inbox-jev digest --dry-print
```

## Commands

| Command | What it does |
|---|---|
| `inbox-jev auth add EMAIL` | Authorize a Gmail account (opens the browser; token → `secrets/tokens/`) |
| `inbox-jev run [-a EMAIL] [--mode M] [--limit N] [--lookback 30d]` | One triage pass (what the timer runs) |
| `inbox-jev digest [-a EMAIL] [--hours 24] [--dry-print]` | Slack digest |
| `inbox-jev restore -a EMAIL --since 1d [--category marketing]` | Undo: back to inbox, remove labels (emails deleted in Gmail are skipped) |
| `inbox-jev reprocess -a EMAIL --category KEY [--low-confidence-only]` | Have the next `act` run redo that category's emails still in the inbox (pair with `run --lookback`) |
| `inbox-jev rules list [-a EMAIL] [--all]` / `rules set -a EMAIL @domain.com newsletter` / `rules delete -a EMAIL SENDER` | Sender rules (`rules set` and `rules delete` take an address or `@domain`, any case; `rules delete` also removes a rule stored as typed before `rules set` checked its input) |
| `inbox-jev retry-errors -a EMAIL` | Have the next `run` retry emails that failed, including those that stopped retrying after 8 failures (pair with `run --lookback`) |
| `inbox-jev unsubscribe-retry [-a EMAIL]` | Retry failed unsubscribes after re-checking them against the current rules, corrections and [unsubscribe checks](docs/how-it-decides.md#33-unsubscribe-conditions) |
| `inbox-jev senders backfill [-a EMAIL]` | Store the author, not the group, for group emails saved before authors were recovered |
| `inbox-jev stats [-a EMAIL] [--days 7]` | Counts per category |
| `inbox-jev labels rename OLD NEW [-a EMAIL]` | Rename a label in Gmail and the DB (see [Renaming a label](#renaming-a-label)) |
| `inbox-jev --version` | The release and, inside the image, the commit it was built from |

## Deploy (Docker + systemd timers)

```bash
git clone <repo> /opt/inbox-jev && cd /opt/inbox-jev
# copy .env, accounts.yaml, secrets/client_secret.json and secrets/tokens/*.json from your laptop
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

- Triage runs every 10 minutes, so a new email can sit in the inbox for up to ~10 minutes before
  it is handled; the digest runs daily at 08:00 America/Argentina/Buenos_Aires (edit
  `OnCalendar`/timezone in the `.timer` files).
- Run logs (JSON lines): `journalctl -u inbox-jev-triage -f`. Every line logged during an
  account's pass carries `run_id`, the id of its `runs` row. A run that fails for one account
  (for example an expired Gmail token, a missing `TYPESAFE_API_KEY`, or Jev rejecting the key
  or failing 3 calls in a row) is recorded in `runs.error` and logged once with its traceback
  ("account run failed", with that `run_id`), the other accounts still run, and the service
  exits non-zero so systemd shows the failure. `digest`, `unsubscribe-retry` and
  `senders backfill` also go on with the other accounts when one fails, and exit non-zero.
  Jev errors are stored and logged as type, HTTP status and request id only, never Jev's
  message (it can quote the email).
- Failures post a Slack alert and a watchdog checks that passes keep succeeding (see
  [Alerts](#alerts)); the DB is backed up off-site every night (see
  [Off-site backups](#off-site-backups)).
- Browse the DB: `docker compose --profile viewer up -d viewer`, then
  `ssh -L 8001:localhost:8001 <host>` and open http://localhost:8001. It is bound to localhost
  on the server, never public.

## Daily digest

One Slack DM per account (`slack_user_id`), by default at 08:00. It lists: totals per category
and how many are in Review; one section per keep category other than `important` (e.g.
`Jev/Action`); emails that need a reply; unsubscribes done, pending a manual click, or failed;
low-confidence emails; corrections learned; and errors. Errors are failed runs (identical ones
grouped, e.g. an expired Gmail token, with the fix: `run inbox-jev auth add EMAIL`, or "Jev
unavailable") and emails that stopped being retried. An email that fails is retried with a
backoff (5, 10, 20 … minutes) and given up after 8 failures, about 10.5 hours (at once when Jev
refuses that email with a 4xx; when Jev refuses two emails in a row the same way, the run fails
and each counts an ordinary failure); `inbox-jev retry-errors` retries them again. Failed
emails are listed by subject and sender
(details: [docs/how-it-decides.md §1](docs/how-it-decides.md#1-the-pass-over-an-inbox)).
Everything taken from an email is escaped for Slack, so a subject can't mention `@channel` or
fake a link. Preview it with `inbox-jev digest --dry-print`.

The digest's language is set per account with `digest_language` in `accounts.yaml`: `en`
(default) or `es`; any other value is rejected when the file is loaded. Category and label names
come from `categories.yaml` and are shown as written there. The texts live in one table per
language (`MESSAGES` in `src/inbox_jev/digest.py`); to add a language, add a table there and
its code to `DigestLanguage` in `config.py` (a test checks every table has every text with the
same placeholders). Counts inside a sentence ("1 email", "2 emails") have a `one` and an
`other` form.

## Updating a deployment

CI builds the image once per commit, smoke-tests it and pushes it to GHCR on `main`
(`ghcr.io/<owner>/<repo>:sha-<short commit>`, and `:latest`), so production runs the image CI
tested. Once, give the server read access to the registry: `docker login ghcr.io` with a
GitHub token that has the `read:packages` scope (the package is private, like the repo). Then:

```bash
cd /opt/inbox-jev
scripts/deploy.sh            # or: scripts/deploy.sh --hold  when the PR has one-off steps
# with --hold: run the PR's deploy notes (reprocess, senders backfill, rules review…), then
sudo systemctl start inbox-jev-triage.timer
```

The script stops the triage timer and waits for a running pass, backs up the SQLite DB
(`data/inbox-jev.db.bak-<stamp>`, keeping the `DEPLOY_BACKUPS_KEEP` newest, 10 by default; older
ones are deleted, and so are older `.env.bak-<stamp>` copies), fetches and checks out `origin/main` (detached: the checkout
only ever holds what is deployed), pulls the image for that commit and writes its tag to `.env`
as `IMAGE_TAG` (the timers' `docker compose run` reads it), checks that `inbox-jev --version`
inside the image reports the checkout's commit, and starts the timer. If a step fails after the
timer was stopped, it says so and leaves the timer stopped for you to fix and start by hand.
Without registry access, `scripts/deploy.sh --build` builds the image on the server instead.
Rollback: `scripts/deploy.sh <previous sha>` (CI keeps an image for every commit on `main`
whose CI passed;
for a commit older than the registry, `scripts/deploy.sh --build <sha>`).

New `act_categories` keys go into `accounts.yaml` only after the code that knows them is
deployed (see [Rolling out a category change](#rolling-out-a-category-change)).

## Alerts

Production failures post to Slack, so they aren't only in the journal. The scripts run on the
host (bash, curl, the host's `python3` standard library, `journalctl`), never in the app image,
so an alert still goes out when the image is broken or can't be pulled. They read their settings
from `.env` (the "Host-side ops scripts" section of `.env.example`) and post with the digest's
`SLACK_BOT_TOKEN` (scope `chat:write`), which they hand to curl on stdin, never on a command line
or in a log.

- **A failed run.** The triage, digest, backup and watchdog services have
  `OnFailure=inbox-jev-alert@%n.service`, which runs `scripts/alert.sh <unit>`. The alert has the
  unit, host, time, the unit's result and exit status, and the failed run's last
  `ALERT_LOG_LINES` (20) app log lines, reduced to time, level, `msg`, `account`, `run_id`, the
  run counts and the exception's type (the `exc_type` field the app's log formatter writes).
  `msg` is kept only for the app's own loggers (`inbox_jev…`), whose messages are fixed texts; a
  library's line (googleapiclient, urllib3, the Jev SDK) keeps only its logger name and level,
  since its message can quote a request URL or a response. Tracebacks (whose message can quote a
  Gmail or HTTP response), senders, labels and anything else stay in the journal. A unit that
  keeps failing (an expired Gmail token fails every pass) is alerted about once, then again only
  after `ALERT_REPEAT_HOURS` (6).
- **Staleness.** `inbox-jev-watchdog.timer` runs `scripts/watchdog.sh` every 30 minutes. It
  alerts when no triage pass succeeded (systemd's "Finished" job message) in the last
  `WATCHDOG_MAX_AGE_MINUTES` (60): the timer is stopped (a `deploy.sh --hold` left that way),
  passes hang until their timeout, or every pass fails. Once per incident, then a "succeed again"
  message when passes are back. When the passes fail, `alert.sh` has already alerted about the
  first failure, so the watchdog stays quiet while that alert is younger than
  `ALERT_REPEAT_HOURS` and reminds once after that. It also posts "succeeds again" for each unit
  `alert.sh` alerted about once its last run succeeded (for the watchdog itself: its next run
  that gets that far).
- `ALERT_SLACK_CHANNEL` is a channel ID (`C…`, invite the bot) or a user ID (`U…`, a DM from the
  bot). Unset, alerts are off: each would-be alert logs a line instead. The dedupe state lives in
  `/var/lib/inbox-jev` (systemd's `StateDirectory=`).

Test an alert by hand: `sudo /opt/inbox-jev/scripts/alert.sh inbox-jev-triage.service` (it
describes the unit's last run; delete `/var/lib/inbox-jev/alerted-inbox-jev-triage.service` to
send it again).

## Off-site backups

`inbox-jev-backup.timer` runs `scripts/backup.sh` every night at 04:15
America/Argentina/Buenos_Aires. It snapshots `data/inbox-jev.db` with sqlite3's `.backup` (the
online backup API: a consistent copy, WAL included, while a pass may be writing; sqlite3 runs as
`APP_UID`, so a `-wal` or `-shm` file it creates stays the app's), checks it with
`PRAGMA quick_check`, gzips it, uploads it with `rclone copyto` to `BACKUP_RCLONE_REMOTE` as
`inbox-jev-<UTC stamp>.db.gz`, and deletes the remote's backups older than
`BACKUP_RETENTION_DAYS` (30): only `inbox-jev-*.db.gz` at the remote's top level, never in a
subfolder (keep a copy by hand in one, e.g. `pre-migration/`). A failure posts an alert. It is off while `BACKUP_RCLONE_REMOTE`
is unset. Only SQLite is backed up: with a hosted `DATABASE_URL`, use the provider's backups.

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

Restore: `rclone copyto gdrive:inbox-jev-backups/<file>.db.gz ./restore.db.gz`, `gunzip` it,
stop the triage timer, replace `data/inbox-jev.db` (removing its `-wal` and `-shm`), give it to
`APP_UID:APP_GID`, and start the timer.

## Database

`DATABASE_URL` is any SQLAlchemy URL. Tables are created on first run.

| Table | Contents |
|---|---|
| `messages` | Every processed email: category, confidence, probabilities, labels, archived/spammed, corrections |
| `unsubscribes` | One row per (account, list): method, target, status, HTTP status, error |
| `sender_rules` | manual / correction / auto rules |
| `sent_domain_scans` | The sent-mail domain search's progress per (account, domain): span read, verdict (`scanning` / `person` / `never`) and when |
| `runs` | Each triage pass: processed, errors, and why the run failed (`error`) |

SQLite databases are opened in WAL mode with a 30 s busy timeout, so the digest, the viewer
and manual commands can read while a triage pass writes, and concurrent writers wait instead of
failing with "database is locked". The `-wal` and `-shm` files next to the DB are part of it.
A read-only reader needs `data/` writable (SQLite creates `-shm`) or immutable mode: the
viewer opens it with `datasette -i`, and `scripts/dry_classify.py` runs with `data/` mounted
read-write.

New columns are added on startup (`create_all` plus a small "add missing columns" step, with
their default for existing rows); there is no other migration tool.

To use a hosted DB, install its driver and change the URL. For example, Postgres on Neon:
`uv add 'psycopg[binary]'` (commit `pyproject.toml` and `uv.lock`, then rebuild the image with
`docker compose build`) and `DATABASE_URL=postgresql+psycopg://…`. SQLite and Postgres are the
databases supported: the sent-mail domain scan is saved with `INSERT … ON CONFLICT`, so on any
other database every `run` (`dry_run` included) and `unsubscribe-retry` fail each account at
their start, before reading its mail or sending anything.

## Privacy

Email headers and the first `BODY_CHARS` characters of each body are sent to the TypeSafe API.
Email bodies are not stored in the database or logged, only sender, subject and the
classification. `previous_contact` runs Gmail searches over your sent mail and reads the To/Cc/Bcc
headers (never the body) of matching sent emails: up to 10 for an address, up to 25 per run for a
domain. Its result is logged on each "message processed" line but not stored in the DB. That line
also carries `timings_ms`, the milliseconds each stage took (Gmail fetch, sender rule and
sent-mail search, Jev, applying the decision): numbers only, no email content. For a domain, the
search's progress is stored (`sent_domain_scans`): the domain, the dates and Gmail ids of the sent
emails read at either end of the span, and the verdict; never addresses or content.

## License

MIT
