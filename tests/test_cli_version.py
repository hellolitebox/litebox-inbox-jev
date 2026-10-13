"""`inbox-jev --version` names the release and the commit the image was built from."""

from typer.testing import CliRunner

from inbox_jev import cli


def test_version_prints_release_and_revision(monkeypatch):
    monkeypatch.setenv(cli.REVISION_ENV, "0123abc0123abc0123abc0123abc0123abc01234")
    result = CliRunner().invoke(cli.app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("inbox-jev ")
    assert result.output.rstrip().endswith("(0123abc0123abc0123abc0123abc0123abc01234)")


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
