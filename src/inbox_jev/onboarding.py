"""`inbox-jev init`: a guided first setup, from a fresh install or checkout to a first dry run.

Every step checks what is already configured and keeps it, so running the wizard again adds an
account or fixes one step without redoing the others. Each install brings its own Google OAuth
client (ADR 0052): the wizard walks through creating it and checks the file it downloads, but
never ships or shares one. Secrets (`.env`, the OAuth client) are written owner-only; nothing
the wizard checks is logged.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import httpx
import typer
import yaml
from pydantic import ValidationError
from typesafe_sdk import (
    TypeSafeAPIConnectionError,
    TypeSafeAuthenticationError,
    TypeSafeClient,
    TypeSafeError,
    TypeSafePermissionDeniedError,
)

from . import gmail as gmail_mod
from . import schedule
from . import slack as slack_mod
from .classifier import describe_jev_error
from .config import (
    FREE_MAIL,
    Account,
    AccountsConfig,
    DigestLanguage,
    Mode,
    Settings,
    find_account,
)
from .files import write_private
from .paths import HOME_ENV, bundled, home
from .signals import ADDRESS, domain_of

ENV_FILE = ".env"
ENV_EXAMPLE = ".env.example"

GOOGLE_PAGES = {
    "project": "https://console.cloud.google.com/projectcreate",
    "gmail_api": "https://console.cloud.google.com/apis/library/gmail.googleapis.com",
    "auth": "https://console.cloud.google.com/auth/overview",
    "audience": "https://console.cloud.google.com/auth/audience",
    "client": "https://console.cloud.google.com/auth/clients/create",
}
GOOGLE_GUIDE = f"""
Gmail access goes through an OAuth client of your own, in a Google Cloud project you control
(inbox-jev doesn't ship one: your mail is read only with your credentials). Five minutes:

  1. Create or pick a project:   {GOOGLE_PAGES["project"]}
  2. Enable the Gmail API:       {GOOGLE_PAGES["gmail_api"]}
  3. Set up the consent screen:  {GOOGLE_PAGES["auth"]}
     Any app name (e.g. inbox-jev). Audience:
     - Internal, if every account is in your own Google Workspace;
     - External otherwise, and then "Publish app" on {GOOGLE_PAGES["audience"]}
       so it is In production. Don't leave it in Testing: Google expires a Testing app's tokens
       after 7 days and every run fails until you authorize again. It stays unverified, which
       is fine for your own accounts: when you authorize, Google warns that it hasn't verified
       the app; choose Advanced, then Go to <your app>.
  4. Create a client of type Desktop app and download its JSON:
                                 {GOOGLE_PAGES["client"]}
"""

SLACK_GUIDE = """
The digest is a Slack DM from a bot of yours: create an app at https://api.slack.com/apps, add the
bot scopes chat:write and users:read.email (the second lets this wizard find your member ID; it's
optional) under OAuth & Permissions, install it to your workspace and copy the Bot User OAuth Token
(xoxb-…).
"""

SLACK_AUTH_TEST_URL = "https://slack.com/api/auth.test"
SLACK_LOOKUP_BY_EMAIL_URL = "https://slack.com/api/users.lookupByEmail"

# What a dotenv value may hold unquoted: no whitespace, quotes or `#` (which starts a comment).
ENV_VALUE = re.compile(r"^[^\s\"'#]*$")
# The indentation of an accounts file's first list entry, to append a new one at the same depth.
FIRST_ENTRY = re.compile(r"^(?P<indent>[ \t]*)- ", re.MULTILINE)
ACCOUNTS_HEADER = (
    "# Written by `inbox-jev init`. Every option is explained in accounts.example.yaml.\n"
    "accounts:\n"
)


class SetupError(Exception):
    """A setup step can't go on; the message says what to fix."""


class CheckUnavailableError(SetupError):
    """A value couldn't be checked (the service was unreachable), not that it was wrong."""


# --- Google OAuth client ---------------------------------------------------------------------


def check_client_secrets(path: Path) -> str:
    """The client id of the Desktop app OAuth client in `path`, or SetupError saying why not."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise SetupError(f"{path} doesn't exist") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SetupError(f"{path} isn't a client JSON from Google Cloud: {exc}") from None
    if not isinstance(data, dict):
        raise SetupError(f"{path} isn't a client JSON from Google Cloud")
    if "web" in data:
        raise SetupError(
            f"{path} is a Web application client: create one of type Desktop app instead"
        )
    installed = data.get("installed")
    if not isinstance(installed, dict) or not all(
        isinstance(installed.get(key), str) and installed[key]
        for key in ("client_id", "client_secret")
    ):
        raise SetupError(f"{path} isn't a Desktop app client JSON (no installed client id)")
    client_id: str = installed["client_id"]
    return client_id


def find_downloaded_client_secrets(downloads: Path) -> Path | None:
    """The newest `client_secret_*.json` in `downloads` (Google's name for it), if any."""
    try:
        found = [p for p in downloads.glob("client_secret_*.json") if p.is_file()]
    except OSError:
        return None
    return max(found, key=lambda p: p.stat().st_mtime, default=None)


def install_client_secrets(source: Path, destination: Path) -> None:
    """Check `source` and copy it to `destination`, owner-only (it holds the client secret)."""
    check_client_secrets(source)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(destination, source.read_text())


def expiring_authorization(email: str, expires_at: datetime, at: datetime) -> str:
    """What to tell the user, at `at`, when Google ends `email`'s authorization: when, the two
    causes of a time-limited grant and the fix for each."""
    when = (
        f"will end inbox-jev's access to {email} on"
        if expires_at > at
        else f"already ended inbox-jev's access to {email}, on"
    )
    return (
        f"Google {when} {expires_at:%Y-%m-%d %H:%M} UTC (a time-limited grant). It has one of "
        "two causes, each with its own fix:\n"
        "  - The app's consent screen is External and in Testing, which Google limits to 7 days: "
        f'open {gmail_mod.AUDIENCE_URL} (the project of your OAuth client) and click "Publish '
        'app" so it is In production (it stays unverified, which is fine for your own '
        f"accounts), then run `inbox-jev auth add {email}`.\n"
        "  - Access was granted for a limited time on Google's consent screen: run "
        f"`inbox-jev auth add {email}` again and don't limit it.\n"
        "Check with `inbox-jev auth status`."
    )


def warn_if_expiring(tokens_dir: Path, email: str) -> bool:
    """Print `expiring_authorization` when `email`'s token expires; whether it does."""
    expires_at = gmail_mod.refresh_token_expiry(tokens_dir, email)
    if expires_at is None:
        return False
    message = expiring_authorization(email, expires_at, datetime.now(UTC))
    typer.secho(f"⚠️  {message}", fg=typer.colors.YELLOW, err=True)
    return True


# --- .env ------------------------------------------------------------------------------------


def set_env_values(text: str, values: Mapping[str, str]) -> str:
    """`text` (a .env file) with each `KEY=` line set to `values[KEY]`, or appended if absent.

    Every uncommented line of a key is replaced, since the last one wins; comments, other keys
    and the order are kept. A value that would need quoting is refused (ValueError).
    """
    for key, value in values.items():
        if not ENV_VALUE.match(value):
            raise ValueError(f"{key} can't contain spaces, quotes or #")
        assignment = f"{key}={value}"
        line = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        # a replacement string reads backslashes as escapes
        text, count = line.subn(assignment.replace("\\", "\\\\"), text)
        if not count:
            text = f"{text.rstrip(chr(10))}\n{assignment}\n" if text else f"{assignment}\n"
    return text


def update_env_file(path: Path, values: Mapping[str, str]) -> None:
    """Set `values` in the .env at `path`. A missing one starts from the `.env.example` next to
    it (a checkout's), else from the one the package ships."""
    if path.exists():
        text = path.read_text()
    else:
        example = path.with_name(ENV_EXAMPLE)
        text = (example if example.exists() else bundled(ENV_EXAMPLE)).read_text()
    write_private(path, set_env_values(text, values))


# --- TypeSafe --------------------------------------------------------------------------------


def check_typesafe_key(
    key: str, model: str, make_client: Callable[..., TypeSafeClient] = TypeSafeClient
) -> None:
    """Check `key` with one request that classifies nothing (listing the account's models).

    SetupError if TypeSafe refuses it, CheckUnavailableError if TypeSafe can't be reached.
    """
    try:
        with make_client(api_key=key, model=model) as client:
            client.models.list()
    except (TypeSafeAuthenticationError, TypeSafePermissionDeniedError) as exc:
        raise SetupError(f"TypeSafe refused the key ({describe_jev_error(exc)})") from None
    except TypeSafeAPIConnectionError as exc:
        raise CheckUnavailableError(
            f"TypeSafe can't be reached ({describe_jev_error(exc)})"
        ) from None
    except TypeSafeError as exc:  # a malformed key, or any other answer
        raise SetupError(f"the key couldn't be checked ({describe_jev_error(exc)})") from None


# --- Slack -----------------------------------------------------------------------------------


def _slack_call(http: httpx.Client, token: str, url: str, **params: str) -> dict[str, object]:
    try:
        resp = http.get(url, headers={"Authorization": f"Bearer {token}"}, params=params)
        resp.raise_for_status()
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CheckUnavailableError(f"Slack can't be reached ({type(exc).__name__})") from None
    if not isinstance(body, dict):
        raise CheckUnavailableError("Slack answered something that isn't JSON")
    return body


def check_slack_token(http: httpx.Client, token: str) -> str:
    """The workspace a bot `token` belongs to (Slack's `auth.test`), or SetupError."""
    body = _slack_call(http, token, SLACK_AUTH_TEST_URL)
    if not body.get("ok"):
        raise SetupError(f"Slack refused the token ({body.get('error')})")
    return str(body.get("team") or "your workspace")


def find_slack_user(http: httpx.Client, token: str, email: str) -> str | None:
    """The Slack member ID of `email`, or None when it can't be found (no such user, or a bot
    without the `users:read.email` scope)."""
    try:
        body = _slack_call(http, token, SLACK_LOOKUP_BY_EMAIL_URL, email=email)
    except CheckUnavailableError:
        return None
    user = body.get("user")
    if not body.get("ok") or not isinstance(user, dict) or not isinstance(user.get("id"), str):
        return None
    user_id: str = user["id"]
    return user_id


# --- accounts.yaml ---------------------------------------------------------------------------


def account_entry(
    email: str,
    *,
    slack_user_id: str | None,
    language: DigestLanguage,
    allow_domain: str | None,
) -> dict[str, object]:
    """The accounts file entry for a new account: what the wizard asked, nothing else."""
    entry: dict[str, object] = {"email": email}
    if slack_user_id:
        entry["slack_user_id"] = slack_user_id
    entry["digest_language"] = language.value
    if allow_domain:
        entry["allow_domains"] = [allow_domain]
    return entry


def add_account(path: Path, entry: Mapping[str, object]) -> bool:
    """Append `entry` to the accounts file at `path` (created if missing), keeping its comments.

    False when the account is already in it. A file with no accounts yet (empty, comments only,
    or a bare `accounts:`) gets the list started. The new text is parsed before it is written, so
    a file that doesn't validate, or that the append can't extend cleanly (e.g. `accounts: []`),
    is left untouched and SetupError carries the entry to paste by hand.
    """
    email = str(entry["email"])
    block = yaml.safe_dump([dict(entry)], sort_keys=False, allow_unicode=True)
    current = path.read_text() if path.exists() else ""
    try:
        data = yaml.safe_load(current)
        known = _accounts_in(data)
    except (yaml.YAMLError, ValidationError) as exc:
        raise SetupError(
            f"{path} isn't a valid accounts file; fix it or add this by hand:\n{block}"
        ) from exc
    if find_account(known, email):
        return False
    if data is None:  # nothing but comments, if anything: keep them and start the list
        kept = current.rstrip("\n") + "\n" if current.strip() else ""
        text = kept + ACCOUNTS_HEADER + _indent(block, "  ")
    else:
        first = FIRST_ENTRY.search(current)
        text = current.rstrip("\n") + "\n" + _indent(block, first["indent"] if first else "  ")
    _check_accounts(text, [*(acc.email for acc in known), email], path, block)
    path.write_text(text)
    return True


def _accounts_in(data: object) -> list[Account]:
    """The accounts in a parsed accounts file: none in one without any yet (empty, comments only,
    or `accounts:` alone). Raises ValidationError for anything else that doesn't validate."""
    if data is None or data == {"accounts": None}:
        return []
    return AccountsConfig.model_validate(data).accounts


def _is_listed(path: Path, email: str) -> bool:
    """Whether `email` is in the accounts file at `path`. False when the file is missing, has no
    accounts yet or doesn't validate: `add_account` then starts it, or says how to fix it."""
    try:
        accounts = _accounts_in(yaml.safe_load(path.read_text()))
    except (OSError, yaml.YAMLError, ValidationError):
        return False
    return find_account(accounts, email) is not None


def _indent(block: str, prefix: str) -> str:
    return "".join(prefix + line for line in block.splitlines(keepends=True))


def _check_accounts(text: str, expected: list[str], path: Path, block: str) -> None:
    try:
        accounts = AccountsConfig.model_validate(yaml.safe_load(text)).accounts
    except (yaml.YAMLError, ValidationError):
        accounts = []
    if [acc.email for acc in accounts] != expected:
        raise SetupError(f"couldn't add the account to {path}; add it by hand:\n{block}")


# --- The wizard ------------------------------------------------------------------------------


def _ok(message: str) -> None:
    typer.secho(f"  ✓ {message}", fg=typer.colors.GREEN)


def _problem(message: str) -> None:
    typer.secho(f"  ✗ {message}", fg=typer.colors.RED, err=True)


def _step(title: str) -> None:
    typer.secho(f"\n{title}", bold=True)


def wizard(downloads: Path | None = None) -> bool:
    """Run the setup steps in order, in the home (`paths.home`); True when the user wants a
    first dry run now. `downloads` is where Google's client JSON is looked for (~/Downloads by
    default)."""
    root = home()
    env_file = root / ENV_FILE
    typer.secho("inbox-jev setup", bold=True)
    typer.echo(f"Configuration and data go in {root} (set {HOME_ENV} to use another folder).")
    typer.echo("Each step keeps what is already configured. Ctrl-C stops; run it again later.")
    if not env_file.exists():
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        update_env_file(env_file, {})
        _ok(f"created {env_file}")
    settings = Settings.load()
    _typesafe_step(settings, env_file)
    _google_client_step(settings, downloads or Path.home() / "Downloads")
    slack_token = _slack_step(settings, env_file)
    _accounts_step(settings, slack_token)
    _schedule_step(root, digest=bool(slack_token))
    _step("Done")
    if settings.mode is Mode.DRY_RUN:
        typer.echo(
            f"MODE is dry_run in {env_file}: Jev's decisions are recorded, Gmail is untouched."
        )
    else:
        typer.echo(f"MODE is {settings.mode.value} in {env_file} (README.md → Modes).")
    typer.echo(
        "Next: `inbox-jev stats` and `inbox-jev digest --dry-print` show what it decided; set\n"
        "MODE=label when the categories look right (README.md → Modes)."
    )
    return typer.confirm("Run a first dry run on your 30 newest inbox emails now?", default=True)


def _schedule_step(root: Path, *, digest: bool) -> None:
    _step("5. Run it on a schedule (optional)")
    typer.echo(
        "  Only if this computer is where inbox-jev runs: a triage pass every 10 minutes while it\n"
        "  is on" + (", and the digest every day at 08:00" if digest else "") + ". Skip it if\n"
        "  you will deploy to a server (docs/operations.md): two copies must never triage the\n"
        "  same account."
    )
    if not typer.confirm("  Schedule it here?", default=False):
        typer.echo("  Later: `inbox-jev schedule install`.")
        return
    try:
        scheduler = schedule.detect()
        written = schedule.install(
            scheduler,
            schedule.jobs(digest=digest),
            root,
            command=schedule.executable(),
            places=schedule.Places.default(),
        )
    except schedule.ScheduleError as exc:
        _problem(f"couldn't schedule it: {exc}")
        return
    except (OSError, subprocess.CalledProcessError) as exc:
        _problem(
            f"couldn't schedule it: {schedule.failure(exc)}; `inbox-jev schedule install` "
            "tries again"
        )
        return
    _ok(f"scheduled with {scheduler.value}: {', '.join(p.name for p in written)}")
    if warning := schedule.linger_warning(scheduler):
        _problem(warning)


def _typesafe_step(settings: Settings, env_file: Path) -> None:
    _step("1. TypeSafe (classifies each email)")
    if settings.typesafe_api_key and typer.confirm(
        f"  {env_file} already has a TypeSafe API key. Keep it?", default=True
    ):
        return
    while True:
        key = typer.prompt("  API key (typesafe.ai dashboard → API keys)", hide_input=True).strip()
        try:
            check_typesafe_key(key, settings.typesafe_model)
        except CheckUnavailableError as exc:
            _problem(str(exc))
            if not typer.confirm("  Save it without checking?", default=False):
                continue
        except SetupError as exc:
            _problem(str(exc))
            continue
        else:
            _ok("key checked")
        break
    update_env_file(env_file, {"TYPESAFE_API_KEY": key})
    _ok(f"saved to {env_file}")


def _google_client_step(settings: Settings, downloads: Path) -> None:
    _step("2. Google OAuth client (lets inbox-jev ask for access to your Gmail)")
    destination = settings.google_client_secrets_file
    if destination.exists():
        try:
            check_client_secrets(destination)
        except SetupError as exc:
            _problem(str(exc))
        else:
            _ok(f"{destination} is a Desktop app client")
            return
    typer.echo(GOOGLE_GUIDE)
    if typer.confirm("  Open these pages in your browser?", default=True):
        for url in GOOGLE_PAGES.values():
            typer.launch(url)
    found = find_downloaded_client_secrets(downloads)
    while True:
        answer = typer.prompt(
            "  Path to the downloaded client JSON", default=str(found) if found else None
        )
        try:
            install_client_secrets(Path(answer.strip()).expanduser(), destination)
        except SetupError as exc:
            _problem(str(exc))
            continue
        break
    _ok(f"copied to {destination}")


def _slack_step(settings: Settings, env_file: Path) -> str:
    """The Slack bot token to send digests with ("" when the digest isn't wanted)."""
    _step("3. Slack daily digest (optional)")
    if settings.slack_bot_token and typer.confirm(
        f"  {env_file} already has a Slack bot token. Keep it?", default=True
    ):
        return settings.slack_bot_token
    if not typer.confirm("  Get a daily digest of what Jev did as a Slack DM?", default=False):
        return ""
    typer.echo(SLACK_GUIDE)
    with slack_mod.client() as http:
        while True:
            token = str(typer.prompt("  Bot token (xoxb-…)", hide_input=True)).strip()
            try:
                team = check_slack_token(http, token)
            except SetupError as exc:
                _problem(str(exc))
                continue
            break
    update_env_file(env_file, {"SLACK_BOT_TOKEN": token})
    _ok(f"token for {team} saved to {env_file}")
    return token


def _accounts_step(settings: Settings, slack_token: str) -> None:
    _step("4. Gmail accounts")
    while True:
        email = typer.prompt("  Gmail address to triage").strip().lower()
        if not ADDRESS.match(email):
            _problem(f"{email!r} isn't an email address")
            continue
        if _authorize(settings, email):
            _add_to_accounts_file(settings, email, slack_token)
        if not typer.confirm("  Add another account?", default=False):
            return


def _authorize(settings: Settings, email: str) -> bool:
    """Whether `email` has a token, after the browser flow if it had none."""
    if gmail_mod.token_path(settings.google_tokens_dir, email).exists():
        _ok(f"{email} is already authorized")
        warn_if_expiring(settings.google_tokens_dir, email)
        return True
    typer.echo(f"  Opening the browser: sign in as {email} and allow access.")
    try:
        gmail_mod.authorize(settings.google_client_secrets_file, settings.google_tokens_dir, email)
    except Exception as exc:  # the browser flow's errors are many; each is reported, then retried
        _problem(f"authorization failed: {type(exc).__name__}: {exc}")
        typer.echo(f"  Try again with `inbox-jev init` or `inbox-jev auth add {email}`.")
        return False
    _ok(f"{email} authorized")
    warn_if_expiring(settings.google_tokens_dir, email)
    return True


def _add_to_accounts_file(settings: Settings, email: str, slack_token: str) -> None:
    path = settings.accounts_file
    if _is_listed(path, email):
        _ok(f"already in {path}")
        return
    slack_user_id = _slack_user_id(slack_token, email) if slack_token else None
    language = _digest_language() if slack_user_id else DigestLanguage.EN
    domain = domain_of(email)
    allow_domain = (
        domain
        if domain not in FREE_MAIL
        and typer.confirm(
            f"  Always keep mail from {domain} (your colleagues) in the inbox?", default=True
        )
        else None
    )
    entry = account_entry(
        email, slack_user_id=slack_user_id, language=language, allow_domain=allow_domain
    )
    try:
        add_account(path, entry)
    except SetupError as exc:
        _problem(str(exc))
        return
    _ok(f"added to {path}")


def _digest_language() -> DigestLanguage:
    choices = ", ".join(lang.value for lang in DigestLanguage)
    while True:
        answer = str(typer.prompt(f"  Digest language ({choices})", default="en")).strip()
        try:
            return DigestLanguage(answer.lower())
        except ValueError:
            _problem(f"{answer!r} isn't one of {choices}")


def _slack_user_id(token: str, email: str) -> str | None:
    with slack_mod.client() as http:
        found = find_slack_user(http, token, email)
    if found:
        _ok(f"Slack member ID {found}")
        return found
    answer = typer.prompt(
        "  Slack member ID for the digest (Profile → ⋯ → Copy member ID; empty to skip)",
        default="",
        show_default=False,
    ).strip()
    return answer or None
