import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from inbox_jev import cli
from inbox_jev.config import Account, Settings


def account(text):
    return Account.model_validate(yaml.safe_load("email: me@acme.com\n" + text))


def test_act_categories_explicit_null_is_rejected():
    # every entry commented out: YAML loads the key as null
    with pytest.raises(ValidationError, match="act_categories is empty"):
        account("act_categories:\n  # - transactional\n")


def test_act_categories_absent_means_all_and_empty_list_means_none():
    assert account("").act_categories is None
    assert account("act_categories: []\n").act_categories == []
    assert account("act_categories: [marketing]\n").act_categories == ["marketing"]


@pytest.mark.parametrize("value", ["30", "30days", "3w", "30d OR from:x", ""])
def test_settings_rejects_invalid_lookback(value):
    with pytest.raises(ValidationError, match="invalid lookback"):
        Settings(_env_file=None, lookback=value)


@pytest.mark.parametrize("value", ["2d", "3m", "1y", "30d"])
def test_settings_accepts_gmail_newer_than_lookback(value):
    assert Settings(_env_file=None, lookback=value).lookback == value


def test_cli_rejects_invalid_lookback_before_running():
    result = CliRunner().invoke(cli.app, ["run", "--lookback", "30"])
    assert result.exit_code == 2
    assert "invalid lookback" in result.output
