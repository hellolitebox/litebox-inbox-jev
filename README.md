# inbox-jev

Triage your Gmail inbox with [TypeSafe Jev](https://typesafe.ai). Every few minutes it reads
new inbox emails, asks Jev which category they belong to (with a calibrated confidence), and:

- **labels** them (`Jev/Important`, `Jev/Transactional`, `Jev/Marketing`, …),
- **archives** receipts, tool notifications, newsletters and cold sales outreach (`Jev/Outreach`),
- **unsubscribes** from marketing (RFC 8058 one-click or `mailto:`) and **moves scams/phishing to Spam**,
- leaves anything it is **not sure about** in the inbox with `Jev/Review`,
- **learns** from your corrections (change a label or move an email back → sender rule),
- sends a **daily Slack digest** of what it did.

Everything is recorded in a database (SQLite by default) so it can be audited and undone.

## How it decides

1. **Allowlist** (`accounts.yaml`): matching senders/domains are always *important*, never touched.
2. **Sender rules**: learned from your corrections, set manually (`inbox-jev rules set`), or
   created automatically after 3 consistent high-confidence answers. They skip Jev. An answer
   doesn't count toward an automatic rule when it was a group archive (see below) or when the
   keep categories (`important`, `action_required`) together get `auto_rule.max_keep_probability`
   (0.02) or more, and a keep answer restarts a pending archive rule, so senders that mix routine
   and action-needed mail (for example `noreply@github.com`) keep going through Jev.
3. **Jev**: one `Choice` over the categories in [`categories.yaml`](categories.yaml), one `Noul`
   for "needs a reply", plus one `Noul` per tag. Jev also gets `previous_contact`: whether you
   ever sent an email to the sender (`wrote_to_sender`), to someone else at their domain
   (`wrote_to_their_domain`, skipped for free mail providers and your own domain) or `never`.
   It comes from one or two Gmail searches over your sent mail, cached per run, and is the
   strongest hint that a personal-looking email is cold outreach.
4. **Safety checks**:
   - *marketing* without `dmarc=pass` or without a `List-Unsubscribe` header keeps its category
     and label and is archived, but never unsubscribed (unsubscribing from an unverified sender
     only confirms your address) and never moved to Spam. The `downgraded_to_spam` column keeps
     its historical name and now means "unsubscribe skipped: unverified sender".
   - Below a category's `min_confidence` nothing destructive happens, with one exception:
     when Jev's top answer is an archive category and all archive categories together reach
     `archive_group_min_confidence` (0.90 by default), the email is archived under the top
     category. Jev is sure it goes away, just not which label fits (for example notification
     0.55 / newsletter 0.42). This never unsubscribes or moves to Spam, and it can only add
     confidence, never lower the bar: the floor is at least the top category's own threshold
     (including an account-level `min_confidence`); a category whose account-level
     `min_confidence` is above that floor doesn't count toward the sum; with `act_categories`,
     only the archive categories listed there count; and an email Jev thinks expects a reply
     (`needs_reply` at or above its threshold) stays in Review with `Jev/Needs Reply` when I
     have written to the sender or their domain, or when the keep categories (`important`,
     `action_required`) together reach `archive_group_reply_keep_probability` (0.05, at most
     `1 - archive_group_min_confidence`). Otherwise the reply signal is ignored for the group:
     every pitch from a stranger "expects a reply".
   - Categories with `action: keep` other than `important` (by default `action_required`:
     automated emails that need you to approve, pay, verify or fix something, including a held
     message pending your approval) stay in the inbox and get their own section in the digest.
   - Unsubscribe URLs must be public `https` hosts (no redirects, no private IPs).
   - Each list is unsubscribed from at most once, and each run has a cap.

### Modes

| `MODE` | DB + digest | Gmail labels | archive / spam / unsubscribe |
|---|---|---|---|
| `dry_run` | ✅ | | |
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

- `act_categories` is matched against the final category. Listing `marketing` archives every
  confident marketing email but only unsubscribes from the verified ones (DMARC +
  `List-Unsubscribe`); listing `spam` only spams what Jev itself calls spam.
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
uv sync --extra dev                        # or: python -m venv .venv && pip install -e '.[dev]'
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
| `inbox-jev run [-a EMAIL] [--mode M] [--limit N] [--lookback 30d]` | One triage pass (what the timer runs) |
| `inbox-jev digest [-a EMAIL] [--hours 24] [--dry-print]` | Slack digest |
| `inbox-jev restore -a EMAIL --since 1d [--category marketing]` | Undo: back to inbox, remove labels |
| `inbox-jev reprocess -a EMAIL --category KEY [--low-confidence-only]` | Have the next `act` run redo that category's emails still in the inbox (pair with `run --lookback`) |
| `inbox-jev rules list [--all]` / `rules set -a EMAIL @domain.com newsletter` / `rules delete` | Sender rules |
| `inbox-jev unsubscribe-retry` | Retry failed unsubscribes |
| `inbox-jev stats [--days 7]` | Counts per category |
| `inbox-jev labels rename OLD NEW [-a EMAIL]` | Rename a label in Gmail and the DB (see below) |

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
docker compose build
docker compose run --rm app run --mode dry_run --limit 20   # smoke test
sudo cp deploy/systemd/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now inbox-jev-triage.timer inbox-jev-digest.timer
```

- Triage runs every 10 minutes; the digest runs daily at 08:00 (edit `OnCalendar`/timezone in
  the `.timer` files).
- Run logs (JSON lines): `journalctl -u inbox-jev-triage -f`.
- Browse the DB: `docker compose --profile viewer up -d viewer`, then
  `ssh -L 8001:localhost:8001 <host>` and open http://localhost:8001. It is bound to localhost
  on the server, never public.

## Database

`DATABASE_URL` is any SQLAlchemy URL. Tables are created on first run.

| Table | Contents |
|---|---|
| `messages` | Every processed email: category, confidence, probabilities, labels, archived/spammed, corrections |
| `unsubscribes` | One row per (account, list): method, target, status, HTTP status, error |
| `sender_rules` | manual / correction / auto rules |
| `runs` | Each triage pass: processed, errors |

To use a hosted DB, install its driver and change the URL. For example, Postgres on Neon:
`pip install psycopg[binary]` and `DATABASE_URL=postgresql+psycopg://…`.

## Privacy

Email headers and the first `BODY_CHARS` characters of each body are sent to the TypeSafe API.
Email bodies are not stored in the database, only sender, subject and the classification.

## License

MIT
