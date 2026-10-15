"""`--version` and the `starting` announce name the release and the commit the image was built
from."""

import logging
from contextlib import nullcontext
from importlib.metadata import version

from typer.testing import CliRunner

import inbox_jev
from inbox_jev import cli
from inbox_jev import unsubscribe as unsub
from inbox_jev.config import Settings


def test_version_prints_release_and_revision(monkeypatch):
    monkeypatch.setenv(cli.REVISION_ENV, "0123abc0123abc0123abc0123abc0123abc01234")
    result = CliRunner().invoke(cli.app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("inbox-jev ")
    assert result.output.rstrip().endswith("(0123abc0123abc0123abc0123abc0123abc01234)")


def test_one_version_source_for_package_cli_and_user_agent(monkeypatch):
    """pyproject.toml's version, via the installed metadata, is the only version string."""
    monkeypatch.delenv(cli.REVISION_ENV, raising=False)
    release = version("inbox-jev")
    assert inbox_jev.__version__ == release
    assert unsub.USER_AGENT.startswith(f"inbox-jev/{release} ")
    assert CliRunner().invoke(cli.app, ["--version"]).output == f"inbox-jev {release}\n"


def test_version_without_revision_needs_no_configuration(monkeypatch):
    monkeypatch.delenv(cli.REVISION_ENV, raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    result = CliRunner().invoke(cli.app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("inbox-jev ")
    assert "(" not in result.output


def test_version_does_not_shadow_subcommands():
    # The app callback must leave the subcommands' own options to them: `run` still rejects
    # a bad --lookback (exit 2) instead of the callback swallowing the arguments. (Not asserted
    # on `--help` text: rich wraps it to the terminal width and can split an option name.)
    result = CliRunner().invoke(cli.app, ["run", "--lookback", "30"])
    assert result.exit_code == 2, result.output
    assert "invalid lookback" in result.output
    before = CliRunner().invoke(cli.app, ["--version", "run", "--lookback", "30"])
    assert before.exit_code == 0 and before.output.startswith("inbox-jev "), before.output


def _stub_digest(monkeypatch):
    """`digest` with no accounts: settings, categories and DB never touched."""
    settings = Settings(_env_file=None, typesafe_api_key="x", database_url="sqlite:///:memory:")
    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda cls: settings))
    monkeypatch.setattr(cli, "load_categories", lambda path: None)
    monkeypatch.setattr(cli, "open_sessions", lambda url: nullcontext())
    monkeypatch.setattr(cli, "_accounts", lambda s, cfg, only: [])


def test_digest_announces_the_revision_unless_previewing(monkeypatch, caplog):
    _stub_digest(monkeypatch)
    monkeypatch.setattr(cli.logs, "setup", lambda level: None)  # keep the test's logging as is
    monkeypatch.setenv(cli.REVISION_ENV, "0123abc")
    with caplog.at_level(logging.INFO, logger="inbox_jev"):
        assert CliRunner().invoke(cli.app, ["digest"]).exit_code == 0
        assert [r.revision for r in caplog.records if r.msg == "starting"] == ["0123abc"]
        caplog.clear()
        assert CliRunner().invoke(cli.app, ["digest", "--dry-print"]).exit_code == 0
        assert not [r for r in caplog.records if r.msg == "starting"]
