"""Where inbox-jev keeps its files (`paths.home`), the defaults it ships (`paths.bundled`) and
settings resolved under the home (`Settings.load`), ADR 0053."""

import re
import tomllib
from pathlib import Path

import pytest
import yaml
from sqlalchemy.engine import make_url

from inbox_jev import paths
from inbox_jev.config import Settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No inbox-jev settings in the environment and a home directory of its own."""
    for name in (
        paths.HOME_ENV,
        "XDG_CONFIG_HOME",
        "DATABASE_URL",
        "ACCOUNTS_FILE",
        "CATEGORIES_FILE",
        "GOOGLE_CLIENT_SECRETS_FILE",
        "GOOGLE_TOKENS_DIR",
        "TYPESAFE_API_KEY",
        "MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    return tmp_path


def test_the_variable_names_the_home(clean_env, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV, "~/somewhere")
    assert paths.home() == clean_env / "user" / "somewhere"


@pytest.mark.parametrize(
    ("marker", "content"),
    [("accounts.yaml", ""), ("pyproject.toml", '[project]\nname = "inbox-jev"\n')],
)
def test_a_deployment_or_checkout_is_its_own_home(clean_env, monkeypatch, marker, content):
    work = clean_env / "checkout"
    work.mkdir()
    (work / marker).write_text(content)
    monkeypatch.chdir(work)
    assert paths.home() == work.resolve()


def test_this_repo_is_a_checkout(clean_env, monkeypatch):
    monkeypatch.chdir(ROOT)
    assert paths.home() == ROOT


@pytest.mark.parametrize(
    ("name", "content"),
    [
        (".env", "DATABASE_URL=postgresql://app:pw@db/otherapp\n"),
        (".env.example", "DATABASE_URL=\n"),
        ("pyproject.toml", '[project]\nname = "otherapp"\n'),
        ("pyproject.toml", "project = 'not a table'\n"),
        ("pyproject.toml", "[project\nname = broken"),
        ("pyproject.toml", b"\xff\xfe"),
    ],
)
def test_another_projects_files_dont_claim_the_home(clean_env, monkeypatch, name, content):
    """A `.env` is in most project directories: running inbox-jev there must not read its
    settings, write inbox-jev's secrets into it or open a second database there."""
    work = clean_env / "otherapp"
    work.mkdir()
    target = work / name
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content)
    monkeypatch.chdir(work)
    assert paths.home() == clean_env / "user" / ".config" / "inbox-jev"


def test_anywhere_else_the_home_is_the_user_config_dir(clean_env, monkeypatch):
    elsewhere = clean_env / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert paths.home() == clean_env / "user" / ".config" / "inbox-jev"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(clean_env / "xdg"))
    assert paths.home() == clean_env / "xdg" / "inbox-jev"


@pytest.mark.parametrize("name", ["categories.yaml", "accounts.example.yaml", ".env.example"])
def test_a_checkout_reads_the_defaults_from_the_repo(name):
    assert paths.bundled(name) == ROOT / name


def test_the_wheel_ships_every_bundled_default():
    """pyproject.toml's force-include maps each repo default into `inbox_jev/defaults/`."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    shipped = project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert shipped == {
        name: f"inbox_jev/{paths.BUNDLED_DIR}/{name}"
        for name in ("categories.yaml", "accounts.example.yaml", ".env.example")
    }
    assert all((ROOT / name).is_file() for name in shipped)


def test_settings_load_the_homes_env_and_resolve_paths_under_it(clean_env, monkeypatch):
    home = clean_env / "home"
    home.mkdir()
    (home / ".env").write_text("TYPESAFE_API_KEY=ts_home\nMODE=label\n")
    monkeypatch.setenv(paths.HOME_ENV, str(home))
    monkeypatch.chdir(clean_env)
    s = Settings.load()
    assert (s.typesafe_api_key, s.mode.value) == ("ts_home", "label")
    assert s.accounts_file == home / "accounts.yaml"
    assert s.google_tokens_dir == home / "secrets" / "tokens"
    assert s.google_client_secrets_file == home / "secrets" / "client_secret.json"
    assert make_url(s.database_url).database == str(home / "data" / "inbox-jev.db")


def test_the_default_categories_fall_back_to_the_shipped_ones(clean_env):
    home = clean_env / "home"
    home.mkdir()
    s = Settings(_env_file=None).under(home)
    assert s.categories_file == paths.bundled("categories.yaml")
    (home / "categories.yaml").write_text("{}")
    assert Settings(_env_file=None).under(home).categories_file == home / "categories.yaml"


def test_categories_yaml_set_explicitly_still_falls_back(clean_env, monkeypatch):
    """`.env.example`, and so `init`'s `.env`, sets CATEGORIES_FILE=categories.yaml: an
    installed copy without its own file must still read the shipped one."""
    home = clean_env / "home"
    home.mkdir()
    monkeypatch.setenv("CATEGORIES_FILE", "categories.yaml")
    assert Settings(_env_file=None).under(home).categories_file == paths.bundled("categories.yaml")


def test_another_categories_file_never_falls_back(clean_env):
    home = clean_env / "home"
    s = Settings(_env_file=None, categories_file=Path("categories.local.yaml")).under(home)
    assert s.categories_file == home / "categories.local.yaml"


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:////var/lib/inbox-jev.db",
        "sqlite:///:memory:",
        "sqlite://",
        "postgresql+psycopg://user:secret@db.example.com/inbox",
        "sqlite+libsql://db.turso.io?authToken=secret",
    ],
)
def test_absolute_memory_and_remote_databases_are_kept_as_they_are(clean_env, url):
    assert Settings(_env_file=None, database_url=url).under(clean_env).database_url == url


def test_absolute_paths_are_kept(clean_env):
    s = Settings(_env_file=None, accounts_file=Path("/etc/accounts.yaml")).under(clean_env)
    assert s.accounts_file == Path("/etc/accounts.yaml")


def _env(workflow: str) -> dict[str, str]:
    text = (ROOT / ".github" / "workflows" / workflow).read_text()
    return dict(re.findall(r'^  ([A-Z_]+): "([^"]+)"$', text, flags=re.MULTILINE))


def test_the_release_builds_with_ci_s_python_and_uv():
    release, ci = _env("release.yml"), _env("ci.yml")
    assert (release["PYTHON_VERSION"], release["UV_VERSION"]) == (
        ci["PYTHON_VERSION"],
        ci["UV_VERSION"],
    )


def test_the_release_builds_only_main_with_no_token_and_no_cache():
    """Only a commit merged to main is published, the checkout keeps no token for the steps
    after it, and setup-uv restores no cache into the release build."""
    release = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
    steps = release["jobs"]["build"]["steps"]
    [checkout] = [s for s in steps if s.get("uses", "").startswith("actions/checkout@")]
    assert checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}
    [setup_uv] = [s for s in steps if s.get("uses", "").startswith("astral-sh/setup-uv@")]
    assert setup_uv["with"]["enable-cache"] is False
    [on_main] = [s for s in steps if "merge-base --is-ancestor" in s.get("run", "")]
    assert '"$GITHUB_SHA" origin/main' in on_main["run"]
    assert steps.index(on_main) < next(
        i for i, s in enumerate(steps) if "check.sh" in s.get("run", "")
    )
