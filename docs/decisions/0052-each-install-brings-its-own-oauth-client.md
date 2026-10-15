# 0052. Each install brings its own Google OAuth client; `init` walks through it

- Status: Accepted
- Date: 2026-10-15
- PR: #63

## Context

inbox-jev is meant to be open source and self-hosted. Gmail access already goes through OAuth (the
browser flow of `inbox-jev auth add`), but with a client each install creates in its own Google
Cloud project: enable the Gmail API, configure the consent screen, create a *Desktop app* client and
save its JSON as `secrets/client_secret.json`. That, plus `.env` and `accounts.yaml` by hand, is the
hardest part of getting started.

The README also advised, for accounts outside the user's Google Workspace, an *External* consent
screen left in *Testing* with each account as a test user. Google gives a Testing app with scopes
beyond basic profile refresh tokens that expire after 7 days, so every run would fail with
`invalid_grant` once a week until the account was authorized again.

## Decision

- Each install keeps bringing its own OAuth client. The repo ships none and no shared Litebox client
  exists.

- `inbox-jev init` is a guided setup that makes this a five-minute step: it prints (and opens) the
  exact Cloud Console pages, finds the client JSON Google downloads
  (`~/Downloads/client_secret_*.json`), refuses a *Web application* client or a file that isn't one,
  and copies it owner-only. It also checks the TypeSafe key with a request that classifies nothing
  (listing models), checks a Slack bot token (`auth.test`) and finds each account's member ID
  (`users.lookupByEmail`, when the bot has `users:read.email`), runs the browser authorization,
  appends the account to `accounts.yaml` without losing its comments, and offers a first `dry_run`.
  Every step keeps what is already configured, so it can be run again to add an account.

- For *External* audiences the guide says to **publish the app (In production)** and stay
  unverified: Google then shows a one-time "unverified app" warning when authorizing, refresh tokens
  don't expire after 7 days, and the 100-user cap of an unverified app is far above a self-hosted
  install's needs. *Internal* stays the choice when every account is in the user's own Workspace.

- A time-limited grant is **detected and reported**, never just documented. Google's token response
  carries `refresh_token_expires_in` only for a time-limited grant, which has two causes: a Testing
  app (7 days), or a user who chose time-limited access on the consent screen (any app status). The
  token can't tell which, so every warning says "Google will end this authorization on DATE (a
  time-limited grant)" and gives both causes with their fixes: for a Testing app, publish it in
  Google Cloud, then `auth add` again (an old token keeps its limit); for a limited grant,
  `auth add` again without limiting it. `gmail.authorize` stores the date in the token file
  (`refresh_token_expires_at`, a key google-auth ignores, kept across refreshes). Then `auth add`
  and `init` print the warning, `auth status` lists every account's state and exits 1, and every
  digest opens with a ⚠️ alert until it is fixed. A token from before this has no date: its
  `invalid_grant` run error says that failures every 7 days mean a Testing app.

## Alternatives considered

- **A shared, verified OAuth client shipped with the repo** ("Sign in with Google", no Cloud Console
  at all). The client secret of a Desktop client isn't secret, so shipping it is allowed, but
  `gmail.modify` and `gmail.send` are restricted scopes: past 100 users the app needs Google's
  verification (verified domain, privacy policy, demo video) and, since email content is sent to a
  third party (TypeSafe), very likely a yearly third-party security assessment (CASA), at a
  recurring cost. The client's owner would also become the party Google, and every user, hold
  responsible for access to their mailboxes. Not worth it for a self-hosted tool; it can be
  revisited if a hosted version ever exists.
- **Create the client with `gcloud`.** `gcloud` can create a project and enable the Gmail API, but
  has no command for a Desktop OAuth client or a consent screen (only IAP's), so the step that
  matters stays manual either way, and requiring `gcloud` would add a dependency for less than half
  the step.
- **Keep the External + Testing advice.** Rejected: weekly token expiry is a failure mode the
  operator can't fix except by re-authorizing every week.
- **Ask the Google Cloud API for the app's publishing status.** It needs Cloud credentials and
  permissions on the project that inbox-jev otherwise never has; the token response already says
  what matters (whether, and when, access ends).
- **Infer Testing from the token's age** (an `invalid_grant` 7 days after authorizing). Only
  possible after the failure; the stored date warns a week ahead.
- **Alert only through the ops alerts** (`scripts/alert.sh`). They fire on failures, i.e. once
  access has ended; the digest reaches the inbox owner every day before that.

## Consequences

- A new user runs `uv sync` and `uv run inbox-jev init`; the manual steps stay documented in the
  README for those who prefer them, with the Testing advice corrected.
- `init` writes `.env` and the client JSON owner-only (`files.write_private`, the atomic writer the
  OAuth tokens already used, moved out of `gmail.py`); `accounts.yaml` keeps the default mode, since
  the container reads it as `APP_UID` once deployed.
- Deployments created with a Testing app keep failing weekly until their app is published and each
  account authorized once more (`inbox-jev auth add EMAIL`); tokens authorized before this change
  carry no date, so only the `invalid_grant` hint points there.
- The digest gains an `alerts` slot above the headline (`slack.Digest.alerts`), for problems that
  stop inbox-jev soon whatever else happened.
