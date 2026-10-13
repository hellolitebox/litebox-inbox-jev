import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from inbox_jev import cli
from inbox_jev.config import Account, CategoriesConfig, Settings


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


def categories(cfg, **update):
    return CategoriesConfig.model_validate({**cfg.model_dump(), **update})


def test_reply_keep_probability_above_group_ceiling_is_rejected(cfg):
    with pytest.raises(ValidationError, match="at most 1 - archive_group_min_confidence"):
        categories(cfg, archive_group_min_confidence=0.9, archive_group_reply_keep_probability=0.2)


@pytest.mark.parametrize(("floor", "keep"), [(0.9, 0.1), (0.9, 0.05), (0.7, 0.3), (None, 0.5)])
def test_reply_keep_probability_within_ceiling_is_accepted(cfg, floor, keep):
    # 0.1 == 1 - 0.9 only up to float rounding: the boundary itself must be accepted.
    c = categories(
        cfg, archive_group_min_confidence=floor, archive_group_reply_keep_probability=keep
    )
    assert c.archive_group_reply_keep_probability == keep


def test_group_domains_are_opt_in():
    # The account's own domain isn't trusted implicitly.
    assert account("").group_domains == []
    assert account("group_domains: [Sister.org]\n").group_domains == ["sister.org"]
    # An unlisted public provider (not in FREE_MAIL) trusts nothing either.
    assert Account.model_validate({"email": "me@mac.com"}).group_domains == []


@pytest.mark.parametrize(
    "value, expected",
    [
        ("@example.org", "example.org"),
        (" Example.ORG. ", "example.org"),
        ("eu.example.org", "eu.example.org"),
        ("acme.co.uk", "acme.co.uk"),
    ],
)
def test_group_domains_are_normalised(value, expected):
    assert account(f"group_domains: ['{value}']\n").group_domains == [expected]


@pytest.mark.parametrize(
    "domain",
    [
        "googlegroups.com",
        "googlegroups.com.",
        "GMail.com",
        "groups.google.com",
        "eu.googlegroups.com",  # ends in a public host
        "mail.yahoo.com",
    ],
)
def test_group_domains_rejects_public_domains(domain):
    with pytest.raises(ValidationError, match="public domains"):
        account(f"group_domains: ['{domain}']\n")


@pytest.mark.parametrize("domain", ["com", "co.uk", "com.ar", "ac.jp"])
def test_group_domains_rejects_public_suffixes(domain):
    with pytest.raises(ValidationError, match="group_domains entry"):
        account(f"group_domains: ['{domain}']\n")


@pytest.mark.parametrize("domain", ["hello@example.org", "example .org", "", "@"])
def test_group_domains_rejects_non_domains(domain):
    with pytest.raises(ValidationError, match="isn't a domain"):
        account(f"group_domains: ['{domain}']\n")
