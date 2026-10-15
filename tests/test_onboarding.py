"""`inbox-jev init`: each setup step's checks, and the wizard end to end with no network."""

import json
import os
import stat
import subprocess
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path

import httpx
import httpx2
import pytest
import yaml
from typer.testing import CliRunner
from typesafe_sdk import RetryPolicy, TypeSafeClient

from inbox_jev import cli, onboarding
from inbox_jev.config import DigestLanguage, load_accounts
from inbox_jev.onboarding import (
    CheckUnavailableError,
    SetupError,
    account_entry,
    add_account,
    check_client_secrets,
    check_slack_token,
    check_typesafe_key,
    find_downloaded_client_secrets,
    find_slack_user,
    install_client_secrets,
    set_env_values,
    update_env_file,
)

ROOT = Path(__file__).resolve().parents[1]
DESKTOP_CLIENT = {
    "installed": {
        "client_id": "123.apps.googleusercontent.com",
        "client_secret": "GOCSPX-test",
        "redirect_uris": ["http://localhost"],
    }
}


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _write_json(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data))
    return path


# --- Google OAuth client ---------------------------------------------------------------------


def test_a_desktop_client_is_accepted(tmp_path):
    path = _write_json(tmp_path / "client.json", DESKTOP_CLIENT)
    assert check_client_secrets(path) == "123.apps.googleusercontent.com"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (json.dumps({"web": DESKTOP_CLIENT["installed"]}), "Web application client"),
        (json.dumps({"installed": {"client_id": "x"}}), "isn't a Desktop app client"),
        (json.dumps(["installed"]), "isn't a client JSON"),
        ("{not json", "isn't a client JSON"),
    ],
)
def test_a_client_file_that_cant_authorize_is_refused_with_the_reason(tmp_path, content, message):
    path = tmp_path / "client.json"
    path.write_text(content)
    with pytest.raises(SetupError, match=message):
        check_client_secrets(path)


def test_a_missing_client_file_is_refused(tmp_path):
    with pytest.raises(SetupError, match="doesn't exist"):
        check_client_secrets(tmp_path / "nope.json")


def test_the_newest_downloaded_client_is_found(tmp_path):
    old = _write_json(tmp_path / "client_secret_old.json", DESKTOP_CLIENT)
    new = _write_json(tmp_path / "client_secret_new.json", DESKTOP_CLIENT)
    os.utime(old, (1, 1))
    (tmp_path / "other.json").write_text("{}")
    assert find_downloaded_client_secrets(tmp_path) == new


def test_no_downloaded_client_is_none(tmp_path):
    assert find_downloaded_client_secrets(tmp_path) is None
    assert find_downloaded_client_secrets(tmp_path / "missing") is None


def test_an_installed_client_is_owner_only(tmp_path):
    source = _write_json(tmp_path / "client_secret_1.json", DESKTOP_CLIENT)
    destination = tmp_path / "secrets" / "client_secret.json"
    install_client_secrets(source, destination)
    assert json.loads(destination.read_text()) == DESKTOP_CLIENT
    assert _mode(destination) == 0o600
    assert _mode(destination.parent) == 0o700


def test_an_invalid_client_is_not_installed(tmp_path):
    source = _write_json(tmp_path / "client_secret_1.json", {"web": {}})
    destination = tmp_path / "secrets" / "client_secret.json"
    with pytest.raises(SetupError):
        install_client_secrets(source, destination)
    assert not destination.exists()


# --- .env ------------------------------------------------------------------------------------


def test_env_values_replace_their_lines_and_keep_the_rest():
    text = "# TypeSafe\nTYPESAFE_API_KEY=\nMODE=dry_run\n# MODE=act\nTYPESAFE_API_KEY=old\n"
    assert set_env_values(text, {"TYPESAFE_API_KEY": "ts_1", "SLACK_BOT_TOKEN": "xoxb-1"}) == (
        "# TypeSafe\nTYPESAFE_API_KEY=ts_1\nMODE=dry_run\n# MODE=act\nTYPESAFE_API_KEY=ts_1\n"
        "SLACK_BOT_TOKEN=xoxb-1\n"
    )


def test_env_values_are_written_literally():
    assert set_env_values("KEY=\n", {"KEY": r"a\1b"}) == "KEY=a\\1b\n"
    assert set_env_values("", {"KEY": "v"}) == "KEY=v\n"


@pytest.mark.parametrize("value", ["two words", "a#b", 'quo"te', "line\nbreak"])
def test_an_env_value_that_needs_quoting_is_refused(value):
    with pytest.raises(ValueError, match="KEY"):
        set_env_values("", {"KEY": value})


def test_a_new_env_file_starts_from_the_example_and_is_owner_only(tmp_path):
    (tmp_path / ".env.example").write_text("# comment\nTYPESAFE_API_KEY=\nMODE=dry_run\n")
    env = tmp_path / ".env"
    update_env_file(env, {"TYPESAFE_API_KEY": "ts_1"})
    assert env.read_text() == "# comment\nTYPESAFE_API_KEY=ts_1\nMODE=dry_run\n"
    assert _mode(env) == 0o600


# --- TypeSafe --------------------------------------------------------------------------------


def _typesafe(handler):
    return partial(
        TypeSafeClient, transport=httpx2.MockTransport(handler), retry=RetryPolicy(max_retries=0)
    )


def test_a_key_typesafe_accepts_passes():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx2.Response(200, json={"models": []})

    check_typesafe_key("ts_good", "jev-latest", _typesafe(handler))
    assert seen == ["/v1/models"]  # lists models: nothing is classified


@pytest.mark.parametrize("status", [401, 403])
def test_a_key_typesafe_refuses_fails(status):
    handler = lambda request: httpx2.Response(status, json={"error": "nope"})  # noqa: E731
    with pytest.raises(SetupError, match="refused the key") as raised:
        check_typesafe_key("ts_bad", "jev-latest", _typesafe(handler))
    assert not isinstance(raised.value, CheckUnavailableError)
    assert "ts_bad" not in str(raised.value)


def test_typesafe_unreachable_is_not_a_bad_key():
    def handler(request):
        raise httpx2.ConnectError("offline", request=request)

    with pytest.raises(CheckUnavailableError, match="can't be reached"):
        check_typesafe_key("ts_good", "jev-latest", _typesafe(handler))


def test_an_empty_key_fails_before_any_request():
    with pytest.raises(SetupError, match="couldn't be checked"):
        check_typesafe_key("", "jev-latest", _typesafe(lambda r: pytest.fail("no request")))


# --- Slack -----------------------------------------------------------------------------------


def _slack(routes):
    def handler(request):
        assert request.headers["authorization"] == "Bearer xoxb-1"
        answer = routes[request.url.path]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(200, json=answer)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_valid_slack_token_names_its_workspace():
    with _slack({"/api/auth.test": {"ok": True, "team": "Acme"}}) as http:
        assert check_slack_token(http, "xoxb-1") == "Acme"


def test_a_slack_token_slack_refuses_fails():
    with (
        _slack({"/api/auth.test": {"ok": False, "error": "invalid_auth"}}) as http,
        pytest.raises(SetupError, match="invalid_auth"),
    ):
        check_slack_token(http, "xoxb-1")


def test_slack_unreachable_is_reported_as_such():
    with (
        _slack({"/api/auth.test": httpx.ConnectError("offline")}) as http,
        pytest.raises(CheckUnavailableError),
    ):
        check_slack_token(http, "xoxb-1")


def test_a_slack_member_is_found_by_email():
    route = {"/api/users.lookupByEmail": {"ok": True, "user": {"id": "U123"}}}
    with _slack(route) as http:
        assert find_slack_user(http, "xoxb-1", "me@acme.com") == "U123"


@pytest.mark.parametrize(
    "answer",
    [
        {"ok": False, "error": "missing_scope"},
        {"ok": False, "error": "users_not_found"},
        {"ok": True, "user": {}},
        httpx.ConnectError("offline"),
    ],
)
def test_a_slack_member_that_cant_be_found_is_none(answer):
    with _slack({"/api/users.lookupByEmail": answer}) as http:
        assert find_slack_user(http, "xoxb-1", "me@acme.com") is None


# --- accounts.yaml ---------------------------------------------------------------------------


def test_an_account_entry_holds_only_what_was_asked():
    assert account_entry(
        "me@acme.com", slack_user_id=None, language=DigestLanguage.EN, allow_domain=None
    ) == {"email": "me@acme.com", "digest_language": "en"}
    assert account_entry(
        "me@acme.com", slack_user_id="U1", language=DigestLanguage.ES, allow_domain="acme.com"
    ) == {
        "email": "me@acme.com",
        "slack_user_id": "U1",
        "digest_language": "es",
        "allow_domains": ["acme.com"],
    }


def test_the_first_account_creates_the_file(tmp_path):
    path = tmp_path / "accounts.yaml"
    entry = {"email": "me@acme.com", "digest_language": "es", "allow_domains": ["acme.com"]}
    assert add_account(path, entry) is True
    [acc] = load_accounts(path)
    assert (acc.email, acc.digest_language, acc.allow_domains) == (
        "me@acme.com",
        DigestLanguage.ES,
        ["acme.com"],
    )


def test_an_account_is_appended_keeping_the_files_comments(tmp_path):
    path = tmp_path / "accounts.yaml"
    example = (ROOT / "accounts.example.yaml").read_text()
    path.write_text(example)
    assert add_account(path, {"email": "new@acme.com", "digest_language": "en"}) is True
    assert path.read_text().startswith(example.rstrip("\n"))
    assert [acc.email for acc in load_accounts(path)] == ["you@example.com", "new@acme.com"]


def test_an_account_already_in_the_file_is_left_alone(tmp_path):
    path = tmp_path / "accounts.yaml"
    path.write_text("accounts:\n- email: me@acme.com\n")
    assert add_account(path, {"email": "me@acme.com"}) is False
    assert path.read_text() == "accounts:\n- email: me@acme.com\n"


def test_an_account_list_at_the_files_root_indent_is_extended(tmp_path):
    path = tmp_path / "accounts.yaml"
    path.write_text("accounts:\n- email: a@acme.com\n")
    add_account(path, {"email": "b@acme.com", "allow_domains": ["acme.com"]})
    assert [acc.email for acc in load_accounts(path)] == ["a@acme.com", "b@acme.com"]


@pytest.mark.parametrize(
    "content", ["", "# my accounts\n", "accounts:\n", "# my accounts\naccounts:\n"]
)
def test_a_file_with_no_accounts_yet_gets_the_first_one(tmp_path, content):
    path = tmp_path / "accounts.yaml"
    path.write_text(content)
    assert add_account(path, {"email": "me@acme.com"}) is True
    assert path.read_text().startswith(content)
    assert [acc.email for acc in load_accounts(path)] == ["me@acme.com"]


@pytest.mark.parametrize(
    "content",
    ["accounts: [unclosed\n", "accounts:\n  - name: no email\n", "- email: me@acme.com\n"],
)
def test_an_invalid_accounts_file_is_left_untouched_with_the_entry_to_paste(tmp_path, content):
    path = tmp_path / "accounts.yaml"
    path.write_text(content)
    with pytest.raises(SetupError, match="isn't a valid accounts file; fix it") as raised:
        add_account(path, {"email": "new@acme.com"})
    assert "email: new@acme.com" in str(raised.value)
    assert path.read_text() == content


def test_a_file_the_append_cant_extend_is_left_untouched(tmp_path):
    path = tmp_path / "accounts.yaml"
    path.write_text("accounts: []\n")
    with pytest.raises(SetupError, match="add it by hand") as raised:
        add_account(path, {"email": "me@acme.com"})
    assert "email: me@acme.com" in str(raised.value)
    assert path.read_text() == "accounts: []\n"


# --- The wizard ------------------------------------------------------------------------------


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """An empty checkout (its pyproject.toml and .env.example) as the current directory, no
    settings in the environment, no browser and no network; `calls` records what the wizard did
    outside."""
    for name in (
        "TYPESAFE_API_KEY",
        "SLACK_BOT_TOKEN",
        "ACCOUNTS_FILE",
        "GOOGLE_CLIENT_SECRETS_FILE",
        "GOOGLE_TOKENS_DIR",
        "MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env.example").write_text((ROOT / ".env.example").read_text())
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "inbox-jev"\n')
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(onboarding.Path, "home", classmethod(lambda cls: tmp_path))
    calls: dict[str, list] = {"keys": [], "launched": [], "authorized": []}

    def check_key(key, model):
        calls["keys"].append(key)
        if key == "bad":
            raise SetupError("TypeSafe refused the key")

    def authorize(client_secrets, tokens_dir, email):
        calls["authorized"].append(email)
        tokens_dir.mkdir(parents=True, exist_ok=True)
        (tokens_dir / f"{email}.json").write_text("{}")
        return tokens_dir / f"{email}.json"

    def slack_client():
        def handler(request):
            if request.url.path == "/api/auth.test":
                return httpx.Response(200, json={"ok": True, "team": "Acme"})
            return httpx.Response(200, json={"ok": True, "user": {"id": "U42"}})

        return httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(onboarding, "check_typesafe_key", check_key)
    monkeypatch.setattr(onboarding.gmail_mod, "authorize", authorize)
    monkeypatch.setattr(onboarding.slack_mod, "client", slack_client)
    monkeypatch.setattr(onboarding.typer, "launch", lambda url: calls["launched"].append(url))
    _write_json(downloads / "client_secret_123.json", DESKTOP_CLIENT)
    return tmp_path, calls


def _answers(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def test_the_wizard_sets_up_an_empty_checkout(checkout):
    root, calls = checkout
    answers = _answers(
        "bad",  # TypeSafe key: refused, asked again
        "ts_good",
        "y",  # open the Google pages
        "",  # the client JSON found in Downloads
        "y",  # Slack digest
        "xoxb-1",
        "Me@Acme.com",
        "es",  # digest language
        "y",  # keep acme.com mail
        "n",  # another account
        "n",  # no schedule here
        "n",  # dry run now
    )
    result = CliRunner().invoke(cli.app, ["init"], input=answers)
    assert result.exit_code == 0, result.output
    assert calls["keys"] == ["bad", "ts_good"]
    assert calls["launched"] == list(onboarding.GOOGLE_PAGES.values())
    assert calls["authorized"] == ["me@acme.com"]
    env = (root / ".env").read_text()
    assert "\nTYPESAFE_API_KEY=ts_good\n" in env
    assert "\nSLACK_BOT_TOKEN=xoxb-1\n" in env
    assert "\nMODE=dry_run\n" in env  # the example's comments and defaults are kept
    assert _mode(root / ".env") == 0o600
    client = root / "secrets" / "client_secret.json"
    assert json.loads(client.read_text()) == DESKTOP_CLIENT
    assert yaml.safe_load((root / "accounts.yaml").read_text()) == {
        "accounts": [
            {
                "email": "me@acme.com",
                "slack_user_id": "U42",
                "digest_language": "es",
                "allow_domains": ["acme.com"],
            }
        ]
    }
    assert "ts_good" not in result.output and "xoxb-1" not in result.output


def test_running_it_again_keeps_everything_and_asks_no_secret(checkout):
    root, calls = checkout
    first = _answers("ts_good", "n", "", "n", "me@gmail.com", "n", "n", "n")
    assert CliRunner().invoke(cli.app, ["init"], input=first).exit_code == 0
    calls["keys"].clear()
    calls["authorized"].clear()
    before = {p: p.read_text() for p in (root / ".env", root / "accounts.yaml")}

    # keep the key, no Slack, the same account, no more accounts, no schedule, no dry run
    again = _answers("y", "n", "me@gmail.com", "n", "n", "n")
    result = CliRunner().invoke(cli.app, ["init"], input=again)
    assert result.exit_code == 0, result.output
    assert calls == {"keys": [], "launched": [], "authorized": []}
    assert "already authorized" in result.output
    assert f"already in {(root / 'accounts.yaml').resolve()}" in result.output
    assert {p: p.read_text() for p in before} == before


def test_a_free_mail_account_is_not_allowlisted_and_has_no_digest(checkout):
    root, _ = checkout
    answers = _answers("ts_good", "n", "", "n", "me@gmail.com", "n", "n", "n")
    assert CliRunner().invoke(cli.app, ["init"], input=answers).exit_code == 0
    [acc] = load_accounts(root / "accounts.yaml")
    assert (acc.allow_domains, acc.slack_user_id) == ([], None)


def test_a_failed_authorization_adds_no_account(checkout, monkeypatch):
    root, _ = checkout

    def deny(client_secrets, tokens_dir, email):
        raise RuntimeError("Authorized other@acme.com, expected me@acme.com")

    monkeypatch.setattr(onboarding.gmail_mod, "authorize", deny)
    answers = _answers("ts_good", "n", "", "n", "me@acme.com", "n", "n", "n")
    result = CliRunner().invoke(cli.app, ["init"], input=answers)
    assert result.exit_code == 0, result.output
    assert "authorization failed" in result.output
    assert "inbox-jev auth add me@acme.com" in result.output
    assert not (root / "accounts.yaml").exists()


def test_the_wizard_starts_an_empty_accounts_file(checkout):
    root, _ = checkout
    (root / "accounts.yaml").write_text("")
    answers = _answers("ts_good", "n", "", "n", "me@gmail.com", "n", "n", "n")
    result = CliRunner().invoke(cli.app, ["init"], input=answers)
    assert result.exit_code == 0, result.output
    assert [acc.email for acc in load_accounts(root / "accounts.yaml")] == ["me@gmail.com"]


def test_the_wizard_prints_the_entry_for_an_accounts_file_it_cant_read(checkout):
    root, _ = checkout
    (root / "accounts.yaml").write_text("accounts: [unclosed\n")
    answers = _answers("ts_good", "n", "", "n", "me@gmail.com", "n", "n", "n")
    result = CliRunner().invoke(cli.app, ["init"], input=answers)
    assert result.exit_code == 0, result.output
    assert "fix it or add this by hand" in result.output
    assert "email: me@gmail.com" in result.output
    assert (root / "accounts.yaml").read_text() == "accounts: [unclosed\n"


def test_an_invalid_client_file_is_asked_for_again(checkout):
    root, _ = checkout
    web = _write_json(root / "web.json", {"web": DESKTOP_CLIENT["installed"]})
    answers = _answers("ts_good", "n", str(web), str(root / "Downloads" / "client_secret_123.json"))
    answers += _answers("n", "me@gmail.com", "n", "n", "n")
    result = CliRunner().invoke(cli.app, ["init"], input=answers)
    assert result.exit_code == 0, result.output
    assert "Web application client" in result.output
    assert check_client_secrets(root / "secrets" / "client_secret.json")


def test_an_unreachable_typesafe_can_be_saved_unchecked(checkout, monkeypatch):
    root, _ = checkout

    def offline(key, model):
        raise CheckUnavailableError("TypeSafe can't be reached (connection)")

    monkeypatch.setattr(onboarding, "check_typesafe_key", offline)
    answers = _answers("ts_good", "y", "n", "", "n", "me@gmail.com", "n", "n", "n")
    assert CliRunner().invoke(cli.app, ["init"], input=answers).exit_code == 0
    assert "\nTYPESAFE_API_KEY=ts_good\n" in (root / ".env").read_text()


def test_init_ends_with_a_dry_run_when_asked(checkout, monkeypatch):
    runs = []
    monkeypatch.setattr(cli, "run", lambda **kwargs: runs.append(kwargs))
    answers = _answers("ts_good", "n", "", "n", "me@gmail.com", "n", "n", "y")
    result = CliRunner().invoke(cli.app, ["init"], input=answers)
    assert result.exit_code == 0, result.output
    assert runs == [{"account": None, "mode": cli.Mode.DRY_RUN, "limit": 30, "lookback": None}]


def test_slack_retries_a_refused_token_and_asks_for_a_member_id_it_cant_find(checkout, monkeypatch):
    root, _ = checkout

    def slack_client():
        def handler(request):
            if request.url.path == "/api/auth.test":
                ok = request.headers["authorization"] == "Bearer xoxb-good"
                return httpx.Response(200, json={"ok": ok, "error": "invalid_auth", "team": "A"})
            return httpx.Response(200, json={"ok": False, "error": "missing_scope"})

        return httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(onboarding.slack_mod, "client", slack_client)
    answers = _answers(
        "ts_good",
        "n",  # don't open the Google pages
        "",  # the client JSON found in Downloads
        "y",  # Slack digest
        "xoxb-bad",  # refused, asked again
        "xoxb-good",
        "not-an-address",  # asked again
        "me@acme.com",
        "U77",  # the member ID the lookup couldn't find
        "fr",  # not a digest language, asked again
        "es",
        "n",  # don't keep all acme.com mail
        "y",  # another account
        "other@gmail.com",
        "",  # no member ID for it
        "n",  # no more accounts
        "n",  # no schedule here
        "n",  # no dry run
    )
    result = CliRunner().invoke(cli.app, ["init"], input=answers)
    assert result.exit_code == 0, result.output
    assert "invalid_auth" in result.output
    assert "isn't an email address" in result.output
    assert "isn't one of en, es" in result.output
    accounts = {acc.email: acc for acc in load_accounts(root / "accounts.yaml")}
    assert accounts["me@acme.com"].slack_user_id == "U77"
    assert accounts["me@acme.com"].digest_language == DigestLanguage.ES
    assert accounts["me@acme.com"].allow_domains == []
    assert accounts["other@gmail.com"].slack_user_id is None


# --- An authorization Google will end (a time-limited grant) ---------------------------------

ENDS = "2099-10-22T10:00:00+00:00"


def _token(tokens: Path, email: str, ends: str | None = None) -> None:
    tokens.mkdir(parents=True, exist_ok=True)
    data = {"refresh_token": "r"} | ({"refresh_token_expires_at": ends} if ends else {})
    (tokens / f"{email}.json").write_text(json.dumps(data))


def test_the_warning_names_the_date_and_both_causes_with_their_fixes():
    ends = datetime.fromisoformat(ENDS)
    text = onboarding.expiring_authorization("me@acme.com", ends, ends - timedelta(days=3))
    assert text.startswith(
        "Google will end inbox-jev's access to me@acme.com on 2099-10-22 10:00 UTC "
        "(a time-limited grant)"
    )
    assert "in Testing" in text and "Publish app" in text  # cause 1 and its fix
    assert "https://console.cloud.google.com/auth/audience" in text
    assert "granted for a limited time" in text and "don't limit it" in text  # cause 2 and its fix
    assert "`inbox-jev auth add me@acme.com`" in text and "`inbox-jev auth status`" in text


def test_the_warning_says_when_the_authorization_already_ended(auth_env):
    _token(auth_env, "testing@acme.com", "2020-01-02T03:04:00+00:00")
    result = CliRunner().invoke(cli.app, ["auth", "status", "-a", "testing@acme.com"])
    assert result.exit_code == 1
    assert (
        "Google already ended inbox-jev's access to testing@acme.com, on 2020-01-02 03:04 UTC"
        in result.output
    )
    assert "will end" not in result.output


@pytest.fixture
def auth_env(tmp_path, monkeypatch):
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text(
        "accounts:\n  - email: ok@acme.com\n  - email: testing@acme.com\n"
        "  - email: missing@acme.com\n"
    )
    tokens = tmp_path / "tokens"
    monkeypatch.setenv("ACCOUNTS_FILE", str(accounts))
    monkeypatch.setenv("CATEGORIES_FILE", str(ROOT / "categories.yaml"))
    monkeypatch.setenv("GOOGLE_TOKENS_DIR", str(tokens))
    monkeypatch.setattr(cli.logs, "setup", lambda level: None)
    return tokens


def test_auth_status_flags_missing_and_ending_authorizations(auth_env):
    _token(auth_env, "ok@acme.com")
    _token(auth_env, "testing@acme.com", ENDS)
    result = CliRunner().invoke(cli.app, ["auth", "status"])
    assert result.exit_code == 1
    assert "ok@acme.com: authorized, with no expiry date from Google" in result.output
    assert "end inbox-jev's access to testing@acme.com on 2099-10-22" in result.output
    assert "missing@acme.com: not authorized: run `inbox-jev auth add missing@acme.com`" in (
        result.output
    )


def test_auth_status_passes_when_nothing_ends(auth_env):
    _token(auth_env, "ok@acme.com")
    result = CliRunner().invoke(cli.app, ["auth", "status", "-a", "ok@acme.com"])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(("ends", "warned"), [(ENDS, True), (None, False)])
def test_auth_add_warns_when_google_will_end_the_authorization(auth_env, monkeypatch, ends, warned):
    def authorize(client_secrets, tokens_dir, email):
        _token(tokens_dir, email, ends)
        return tokens_dir / f"{email}.json"

    monkeypatch.setattr(cli.gmail_mod, "authorize", authorize)
    result = CliRunner().invoke(cli.app, ["auth", "add", "testing@acme.com"])
    assert result.exit_code == 0, result.output
    assert ("Publish app" in result.output) is warned


def test_the_wizard_warns_about_an_authorization_google_will_end(checkout, monkeypatch):
    def authorize(client_secrets, tokens_dir, email):
        _token(tokens_dir, email, ENDS)
        return tokens_dir / f"{email}.json"

    monkeypatch.setattr(onboarding.gmail_mod, "authorize", authorize)
    answers = _answers("ts_good", "n", "", "n", "me@gmail.com", "n", "n", "n")
    result = CliRunner().invoke(cli.app, ["init"], input=answers)
    assert result.exit_code == 0, result.output
    assert "end inbox-jev's access to me@gmail.com on 2099-10-22" in result.output
    assert "Publish app" in result.output


# --- An installed copy, and the schedule step (ADR 0053) -------------------------------------


def test_an_installed_copy_sets_up_its_own_home(checkout, monkeypatch):
    root, _ = checkout
    home = root / "config" / "inbox-jev"
    monkeypatch.setenv("INBOX_JEV_HOME", str(home))
    answers = _answers("ts_good", "n", "", "n", "me@gmail.com", "n", "n", "n")
    result = CliRunner().invoke(cli.app, ["init"], input=answers)
    assert result.exit_code == 0, result.output
    assert f"Configuration and data go in {home}" in result.output
    assert _mode(home) == 0o700
    env = (home / ".env").read_text()
    assert "TYPESAFE_API_KEY=ts_good" in env and "MODE=dry_run" in env  # the shipped example
    assert check_client_secrets(home / "secrets" / "client_secret.json")
    assert [acc.email for acc in load_accounts(home / "accounts.yaml")] == ["me@gmail.com"]
    assert not (root / ".env").exists() and not (root / "accounts.yaml").exists()


def test_the_wizard_schedules_it_when_asked(checkout, monkeypatch):
    root, _ = checkout
    installed = []

    def install(scheduler, jobs, home, *, command, places):
        installed.append((scheduler, [j.name for j in jobs], home))
        return [Path("io.github.inbox-jev.triage.plist")]

    monkeypatch.setattr(
        onboarding.schedule, "detect", lambda: onboarding.schedule.Scheduler.LAUNCHD
    )
    monkeypatch.setattr(onboarding.schedule, "executable", lambda: Path("/bin/inbox-jev"))
    monkeypatch.setattr(onboarding.schedule, "install", install)
    answers = _answers("ts_good", "n", "", "n", "me@gmail.com", "n", "y", "n")
    result = CliRunner().invoke(cli.app, ["init"], input=answers)
    assert result.exit_code == 0, result.output
    assert installed == [(onboarding.schedule.Scheduler.LAUNCHD, ["triage"], root.resolve())]
    assert "scheduled with launchd" in result.output


def test_a_schedule_that_cant_be_set_up_doesnt_stop_the_wizard(checkout, monkeypatch):
    def unsupported():
        raise onboarding.schedule.ScheduleError("only macOS (launchd) and Linux with systemd")

    monkeypatch.setattr(onboarding.schedule, "detect", unsupported)
    answers = _answers("ts_good", "n", "", "n", "me@gmail.com", "n", "y", "n")
    result = CliRunner().invoke(cli.app, ["init"], input=answers)
    assert result.exit_code == 0, result.output
    assert "couldn't schedule it: only macOS" in result.output


def test_a_failing_scheduler_command_says_to_try_again(checkout, monkeypatch):
    def install(scheduler, jobs, home, *, command, places):
        raise subprocess.CalledProcessError(5, ["launchctl"], stderr="Bootstrap failed: 5\n")

    monkeypatch.setattr(
        onboarding.schedule, "detect", lambda: onboarding.schedule.Scheduler.LAUNCHD
    )
    monkeypatch.setattr(onboarding.schedule, "executable", lambda: Path("/bin/inbox-jev"))
    monkeypatch.setattr(onboarding.schedule, "install", install)
    answers = _answers("ts_good", "n", "", "n", "me@gmail.com", "n", "y", "n")
    result = CliRunner().invoke(cli.app, ["init"], input=answers)
    assert result.exit_code == 0, result.output
    assert "(Bootstrap failed: 5)" in result.output
    assert "`inbox-jev schedule install` tries again" in result.output


def test_the_wizard_wont_schedule_on_a_server_deployment(checkout, monkeypatch):
    """The server's triage timer refuses the schedule inside `schedule.install`, so the wizard
    can't install a second triager next to it."""
    root, _ = checkout
    server_timer = root / "inbox-jev-triage.timer"
    server_timer.write_text("")
    places = onboarding.schedule.Places(
        launch_agents=root / "LaunchAgents",
        systemd_user=root / "systemd",
        server_timer=server_timer,
    )
    ran: list[list[str]] = []
    monkeypatch.setattr(
        onboarding.schedule, "detect", lambda: onboarding.schedule.Scheduler.LAUNCHD
    )
    monkeypatch.setattr(onboarding.schedule, "executable", lambda: Path("/bin/inbox-jev"))
    monkeypatch.setattr(onboarding.schedule.Places, "default", classmethod(lambda cls: places))
    monkeypatch.setattr(onboarding.schedule, "_run", lambda args: ran.append(list(args)))
    answers = _answers("ts_good", "n", "", "n", "me@gmail.com", "n", "y", "n")
    result = CliRunner().invoke(cli.app, ["init"], input=answers)
    assert result.exit_code == 0, result.output
    assert "triage the same mail twice" in result.output
    assert ran == []
    assert not places.launch_agents.exists()


def test_a_command_with_no_accounts_file_says_to_run_init(tmp_path, monkeypatch):
    monkeypatch.setenv("INBOX_JEV_HOME", str(tmp_path))
    monkeypatch.delenv("ACCOUNTS_FILE", raising=False)
    monkeypatch.setattr(cli.logs, "setup", lambda level: None)
    result = CliRunner().invoke(cli.app, ["auth", "status"])
    assert result.exit_code == 2
    assert "run `inbox-jev init`" in result.output
