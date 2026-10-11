# inbox-jev

Triage your Gmail inbox with [TypeSafe Jev](https://typesafe.ai). Every few minutes it reads
new inbox emails, asks Jev which category they belong to (with a calibrated confidence), and:

- **labels** them (`Jev/Importante`, `Jev/Transaccional`, `Jev/Marketing`, …),
- **archives** receipts, tool notifications and newsletters,
- **unsubscribes** from marketing (RFC 8058 one-click or `mailto:`) and **moves spam to Spam**,
- leaves anything it is **not sure about** in the inbox with `Jev/Revisar`,
- **learns** from your corrections (change a label or move an email back → sender rule),
- sends a **daily Slack digest** of what it did.

Everything is recorded in a database (SQLite by default) so it can be audited and undone.

## How it decides

1. **Allowlist** (`accounts.yaml`): matching senders/domains are always *important*, never touched.
2. **Sender rules**: learned from your corrections, set manually (`inbox-jev rules set`), or
   created automatically after 3 consistent high-confidence answers. They skip Jev.
3. **Jev**: one `Choice` over the categories in [`categories.yaml`](categories.yaml), one `Noul`
   for "needs a reply", plus one `Noul` per tag.
4. **Safety checks**:
   - *marketing* without `dmarc=pass` or without a `List-Unsubscribe` header is downgraded to
     *spam*. Unsubscribing from a spammer only confirms your address.
   - Below a category's `min_confidence` nothing destructive happens.
   - Unsubscribe URLs must be public `https` hosts (no redirects, no private IPs).
   - Each list is unsubscribed from at most once, and each run has a cap.

### Modes

| `MODE` | DB + digest | Gmail labels | archive / spam / unsubscribe |
|---|---|---|---|
| `dry_run` | ✅ | | |
| `label` | ✅ | ✅ | |
| `act` | ✅ | ✅ | ✅ |

Start with `dry_run` or `label` for a week or two. Switch to `act` when the labels look right.

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
| `inbox-jev run [-a EMAIL] [--mode M] [--limit N]` | One triage pass (what the timer runs) |
| `inbox-jev digest [-a EMAIL] [--hours 24] [--dry-print]` | Slack digest |
| `inbox-jev restore -a EMAIL --since 1d [--category marketing]` | Undo: back to inbox, remove labels |
| `inbox-jev rules list [--all]` / `rules set -a EMAIL @domain.com newsletter` / `rules delete` | Sender rules |
| `inbox-jev unsubscribe-retry` | Retry failed unsubscribes |
| `inbox-jev stats [--days 7]` | Counts per category |

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
