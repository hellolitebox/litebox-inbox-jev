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

Extra labels: `Jev/Needs Reply` (Jev thinks the sender expects a reply; only on emails that stay
in the inbox), `Jev/Invoices` (tag: an invoice to pay), `Jev/Review` (low confidence) and
`Jev/Processed` (technical marker).

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
   you ever wrote to the sender or their domain, from a search over your sent mail. It answers
   with a category and its probabilities, "needs a reply" and the tags.
4. **Policy** turns the answer into actions, and the **mode** decides how much of that is applied:
   - at or above a category's `min_confidence`, its action applies;
   - below it, the email is still archived when Jev's top answer is an archive category and the
     archive categories together reach 0.90 (the *archive group*: never unsubscribes or spams;
     an email that "expects a reply" stays unless it's a stranger's pitch with no keep signal);
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
with `inbox-jev senders backfill` (safe to run again). Details:
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

- **Deploy first** (`git pull && docker compose build`), **then** add a new key to
  `act_categories` in the server's `accounts.yaml`: a key the running code doesn't know makes
  every triage run fail. Until it is listed, the new category is only labeled and stays in the
  inbox, which is safe. On rollback, remove the key from `accounts.yaml` **first**, then deploy
  the old code.
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
- **TypeSafe**: API key from the TypeSafe dashboard → `TYPESAFE_API_KEY`.
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
| `inbox-jev rules list [-a EMAIL] [--all]` / `rules set -a EMAIL @domain.com newsletter` / `rules delete -a EMAIL SENDER` | Sender rules |
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
sudo systemctl enable --now inbox-jev-triage.timer inbox-jev-digest.timer
```

- Triage runs every 10 minutes, so a new email can sit in the inbox for up to ~10 minutes before
  it is handled; the digest runs daily at 08:00 America/Argentina/Buenos_Aires (edit
  `OnCalendar`/timezone in the `.timer` files).
- Run logs (JSON lines): `journalctl -u inbox-jev-triage -f`. A run that fails for one account
  (for example an expired Gmail token) is recorded in `runs.error`, the other accounts still
  run, and the service exits non-zero so systemd shows the failure.
- Browse the DB: `docker compose --profile viewer up -d viewer`, then
  `ssh -L 8001:localhost:8001 <host>` and open http://localhost:8001. It is bound to localhost
  on the server, never public.

## Daily digest

One Slack DM per account (`slack_user_id`), by default at 08:00. It lists: totals per category
and how many are in Review; one section per keep category other than `important` (e.g.
`Jev/Action`); emails that need a reply; unsubscribes done, pending a manual click, or failed;
low-confidence emails; corrections learned; and errors. Errors are failed runs (identical ones
grouped, e.g. an expired Gmail token, with the fix: `run inbox-jev auth add EMAIL`) and emails
that stopped being retried. An email that fails is retried with a backoff (5, 10, 20 … minutes)
and given up after 8 failures, about 10.5 hours; `inbox-jev retry-errors` retries them again
(details: [docs/how-it-decides.md §1](docs/how-it-decides.md#1-the-pass-over-an-inbox)).
Everything taken from an email is escaped for Slack, so a subject can't mention `@channel` or
fake a link. Preview it with `inbox-jev digest --dry-print`.

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
(`data/inbox-jev.db.bak-<stamp>`), fetches and checks out `origin/main` (detached: the checkout
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

## Database

`DATABASE_URL` is any SQLAlchemy URL. Tables are created on first run.

| Table | Contents |
|---|---|
| `messages` | Every processed email: category, confidence, probabilities, labels, archived/spammed, corrections |
| `unsubscribes` | One row per (account, list): method, target, status, HTTP status, error |
| `sender_rules` | manual / correction / auto rules |
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
`docker compose build`) and `DATABASE_URL=postgresql+psycopg://…`.

## Privacy

Email headers and the first `BODY_CHARS` characters of each body are sent to the TypeSafe API.
Email bodies are not stored in the database or logged, only sender, subject and the
classification. `previous_contact` runs Gmail searches over your sent mail; its result is logged on each
"message processed" line but not stored in the DB.

## License

MIT
