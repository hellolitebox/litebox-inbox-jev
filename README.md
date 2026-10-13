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

For each new inbox email, in order:

1. **Allowlist** (`allow_domains` / `allow_senders` in `accounts.yaml`): always *important*, never
   touched, Jev isn't called.
2. **Sender rules** skip Jev. They come from your corrections, from `inbox-jev rules set`, or are
   created automatically after 3 consistent answers at ≥ 0.95. An answer doesn't count toward an
   automatic rule when it was a group archive (below), when the keep categories other than the
   decided one (by default `important` + `action_required`) together get
   `auto_rule.max_keep_probability` (0.02) or more, or when it was cold outreach from a known
   contact. A keep answer, or that keep signal, restarts a pending archive rule. So senders that
   mix routine and action-needed mail (for example `noreply@github.com`) keep going through Jev.
3. **Jev** gets the headers that matter, the start of the body (`BODY_CHARS`), and:
   - one `Choice` over the categories, one `Noul` for "needs a reply" and one per tag;
   - `previous_contact`: whether you ever sent an email to the sender (`wrote_to_sender`), to
     someone else at their domain (`wrote_to_their_domain`, skipped for free mail providers and
     your own domain) or `never`. It comes from one or two Gmail searches over your sent mail,
     cached per run, and is the strongest hint that a personal-looking email is cold outreach;
   - for group mail, `original_sender` and `sent_via_group` (see
     [Groups and mailing lists](#groups-and-mailing-lists)).

   The body is the `text/plain` part. With no `text/plain` at all it is the HTML stripped of
   tags, and when that is empty too, Gmail's snippet. A plain part that is a stub inside a
   `multipart/alternative` ("your mail client doesn't support HTML, open this URL", or even
   `undefined`) is replaced by the stripped HTML: under 300 characters (URLs excluded) next to
   HTML with at least 3× as much text (and ≥ 60 characters). Bulk senders such as emBlue or
   Constant Contact do this, and Jev would otherwise classify blind. Your own note above a
   forwarded message is never replaced.
4. **Policy** turns the answer into actions (see the safety rules below), and the **mode**
   decides how much of that is applied.

### Confidence

- At or above a category's `min_confidence`, its action applies.
- Below it nothing destructive happens: the email stays in the inbox with `Jev/Review` plus the
  suggested category label. Thresholds are asymmetric: *marketing*, *cold_outreach* and *spam*
  (0.90) need more certainty than the archive-only categories (0.80). Below 0.90, *marketing* and
  *cold_outreach* are archived only through the archive group (next item), which never
  unsubscribes.
- **Archive group.** When Jev's top answer is an archive category and all archive categories
  together reach `archive_group_min_confidence` (0.90), the email is archived under the top
  category: Jev is sure it goes away, just not which label fits (for example notification 0.55 /
  newsletter 0.42). This never unsubscribes or spams, and it can only add confidence, never lower
  the bar:
  - the floor is at least the top category's own threshold (including an account-level
    `min_confidence`), and a category whose account-level `min_confidence` is above that floor
    doesn't count toward the sum;
  - with `act_categories`, only the archive categories listed there count;
  - an email Jev thinks expects a reply (`needs_reply` at or above its threshold) stays in Review
    with `Jev/Needs Reply` when you have written to the sender or their domain, or when
    `important` + `action_required` together reach `archive_group_reply_keep_probability` (0.05,
    at most `1 - archive_group_min_confidence`). Otherwise the reply signal is ignored for the
    group: every pitch from a stranger "expects a reply".
- Keep categories other than `important` (by default `action_required`, including a held group
  message pending your approval) stay in the inbox and get their own section in the digest.

### Unsubscribe safety

An unsubscribe is irreversible, so a triage pass (`inbox-jev run`, including the sender-rule path
below) unsubscribes only when **all** of these hold:

- `MODE=act` and, if the account sets `act_categories`, the final category is listed there;
- the category is `archive_unsubscribe` (*marketing*, *cold_outreach*) at or above its own
  `min_confidence` (a group archive never unsubscribes);
- the sender passes DMARC (Gmail's topmost `Authentication-Results` header) and sends a
  `List-Unsubscribe` header. Otherwise the email keeps its category and is archived, never
  unsubscribed (that would only confirm your address to an unverified sender) and never moved to
  Spam. Links in the body are never followed. The `downgraded_to_spam` column keeps its historical
  name and means "unsubscribe skipped: unverified sender";
- it isn't relayed by a group or discussion list, whose `List-Unsubscribe` would remove *you*
  from the group: any email with a `List-Post` header (Google Groups, Mailman, groups.io,
  LISTSERV, Discourse; ESPs and sequence tools don't send it), an unsubscribe target on a Google
  Groups host, a list or sender on your own domain, or any relayed group email;
- for *cold_outreach* (`unsubscribe_known_contacts: false`), you have never written to the sender
  or their domain: a "pitch" from a known contact is where Jev is most likely wrong, and the
  opt-out can cover all of that company's mail. When the sent-mail check fails (unknown), the
  unsubscribe goes ahead under the other checks;
- the list wasn't unsubscribed from already (once per list), and the run's cap
  (`MAX_UNSUBSCRIBES_PER_RUN`) isn't reached.

The method then depends on the `List-Unsubscribe` header:

- an `https` URL is used only with RFC 8058 one-click (`List-Unsubscribe-Post:
  List-Unsubscribe=One-Click`), and only when the URL is a public `https` host (no redirects, no
  private IPs). If it fails, or no URL passes that check, a `mailto:` is tried next;
- otherwise the first `mailto:` gets an email sent from your Gmail (no URL check applies);
- otherwise nothing is sent: the row is `manual_pending` and listed in the digest for you to click.

All of this also applies when a sender rule (auto, manual or correction) decided the category:
before unsubscribing, the rule path re-runs the same checks, including the sent-mail search, so a
relationship that starts after the rule exists is covered too.

`inbox-jev unsubscribe-retry` re-checks each `failed` row against the email's current state
before trying again:

- act mode and the run cap, as above;
- the email wasn't restored (`restore`) or corrected (`corrected_category`). In Gmail it isn't
  reported as spam, in the trash, back in the inbox, under another Jev label, starred, missing a
  Jev label we set, or carrying one we didn't (the edits `run` leaves alone);
- the category is the current sender rule's (manual, correction or auto), else the stored one. It
  must be `archive_unsubscribe` and in `act_categories`. If a rule decided the stored category
  and has since been removed (`rules delete`), the row is held: `run` would ask Jev again. A
  category Jev decided must still be at or above its current `min_confidence` (account override
  included) and not low-confidence;
- not allowlisted, `can_unsubscribe` (DMARC, `List-Unsubscribe`, not a group or own list) and,
  for *cold_outreach*, the sent-mail search finds no contact. Unlike `run`, a failed search holds
  the row: nothing in a manual retry is urgent.

Rows the user or the sender ruled out (restored, corrected, reported as spam, trashed, starred
or relabeled, a rule to a keep category, allowlisted, group, unverified sender, known contact)
become `skipped`, so `run` won't retry them either. Rows held back by settings or a transient
state (mode, `act_categories`, a removed rule, confidence, the cap, a failed search, a deleted
email or no stored message) stay `failed`.

### Groups and mailing lists

Google Groups rewrites `From` to the group ("'Jane' via Hello <hello@yourco.com>") and keeps the
author in `X-Original-Sender`. For groups on a domain listed in the account's `group_domains`,
the author is used as the sender, so rules, corrections, the allowlist, `previous_contact` and
the digest apply to the person, not to everyone who writes to the group. The author is trusted
only when:

- the group's domain is in `group_domains`. This is opt-in: empty by default, not even the
  account's own domain. Anyone can write these headers from a domain they own or from a public
  provider address, and anyone can create an `@googlegroups.com` group, so other groups stay
  keyed on the group address. `group_domains` rejects public providers, `googlegroups.com` and
  bare suffixes such as `co.uk`; list only domains whose Google Workspace you control;
- the group's domain passes DMARC and `Mailing-list` names the same group.

For a group on a trusted domain (in `group_domains`), the allowlist is checked against the
author, so `allow_domains: [yourco.com]` doesn't trust strangers who write to `hello@yourco.com`;
allowlist the group itself by putting its exact address in `allow_senders`. For any other group
the sender is the group address, so `allow_domains` matching the group's domain allowlists every
email sent through it. Group
emails stored before this existed can be fixed with `inbox-jev senders backfill` (safe to run
again; it skips emails deleted in Gmail and stops on any other Gmail error).

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
| `inbox-jev auth add EMAIL` | Authorize a Gmail account (opens the browser; token → `secrets/tokens/`) |
| `inbox-jev run [-a EMAIL] [--mode M] [--limit N] [--lookback 30d]` | One triage pass (what the timer runs) |
| `inbox-jev digest [-a EMAIL] [--hours 24] [--dry-print]` | Slack digest |
| `inbox-jev restore -a EMAIL --since 1d [--category marketing]` | Undo: back to inbox, remove labels |
| `inbox-jev reprocess -a EMAIL --category KEY [--low-confidence-only]` | Have the next `act` run redo that category's emails still in the inbox (pair with `run --lookback`) |
| `inbox-jev rules list [-a EMAIL] [--all]` / `rules set -a EMAIL @domain.com newsletter` / `rules delete -a EMAIL SENDER` | Sender rules |
| `inbox-jev unsubscribe-retry [-a EMAIL]` | Retry failed unsubscribes after re-checking them against the current rules, corrections and [unsubscribe checks](#unsubscribe-safety) |
| `inbox-jev senders backfill [-a EMAIL]` | Store the author, not the group, for group emails saved before authors were recovered |
| `inbox-jev stats [-a EMAIL] [--days 7]` | Counts per category |
| `inbox-jev labels rename OLD NEW [-a EMAIL]` | Rename a label in Gmail and the DB (see [Renaming a label](#renaming-a-label)) |

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

- Triage runs every 10 minutes, so a new email can sit in the inbox for up to ~10 minutes before
  it is handled; the digest runs daily at 08:00 America/Argentina/Buenos_Aires (edit
  `OnCalendar`/timezone in the `.timer` files).
- Run logs (JSON lines): `journalctl -u inbox-jev-triage -f`.
- Browse the DB: `docker compose --profile viewer up -d viewer`, then
  `ssh -L 8001:localhost:8001 <host>` and open http://localhost:8001. It is bound to localhost
  on the server, never public.

## Daily digest

One Slack DM per account (`slack_user_id`), by default at 08:00. It lists: totals per category
and how many are in Review; one section per keep category other than `important` (e.g.
`Jev/Action`); emails that need a reply; unsubscribes done, pending a manual click, or failed;
low-confidence emails; corrections learned; and run errors. Preview it with
`inbox-jev digest --dry-print`.

## Updating a deployment

```bash
cd /opt/inbox-jev
sudo systemctl stop inbox-jev-triage.timer        # no pass while you change things
# wait for a running pass: the oneshot service is "activating" while it runs, not "active"
while [ "$(systemctl show -p ActiveState --value inbox-jev-triage.service)" = activating ]; do
  sleep 2
done
# SQLite only (the default DATABASE_URL). data/ belongs to APP_UID with mode 700: needs sudo
sudo cp -p data/inbox-jev.db "data/inbox-jev.db.bak-$(date +%Y%m%d%H%M)"
git pull && docker compose build
# one-off steps from the PR's deploy notes (reprocess, senders backfill, rules review…)
sudo systemctl start inbox-jev-triage.timer
```

New `act_categories` keys go into `accounts.yaml` only after the code that knows them is
deployed (see [Rolling out a category change](#rolling-out-a-category-change)).

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
Email bodies are not stored in the database or logged, only sender, subject and the
classification. `previous_contact` runs Gmail searches over your sent mail; its result is logged on each
"message processed" line but not stored in the DB.

## License

MIT
