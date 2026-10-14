import re

import pytest
import yaml
from conftest import ROOT
from pydantic import ValidationError
from typer.testing import CliRunner

from inbox_jev import cli
from inbox_jev.config import (
    Account,
    CategoriesConfig,
    ConfigError,
    DigestLanguage,
    LogLevel,
    Settings,
    validate_accounts,
)


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


def test_digest_language_defaults_to_english():
    assert account("").digest_language is DigestLanguage.EN
    assert account("digest_language: es\n").digest_language is DigestLanguage.ES


@pytest.mark.parametrize("value", ["fr", "ES", "spanish", "''"])
def test_digest_language_rejects_unknown_languages(value):
    with pytest.raises(ValidationError, match="digest_language"):
        account(f"digest_language: {value}\n")


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
    assert Account.model_validate({"email": "me@hey.com"}).group_domains == []


@pytest.mark.parametrize(
    "value, expected",
    [
        ("@example.org", "example.org"),
        (" Example.ORG. ", "example.org"),
        ("eu.example.org", "eu.example.org"),
        ("acme.co.uk", "acme.co.uk"),
        # Only what the Public Suffix List lists is a suffix: short names under a ccTLD, or
        # under one sold as a generic name, are real domains.
        ("acme.io", "acme.io"),
        ("sap.de", "sap.de"),
        ("adv.ai", "adv.ai"),
        ("sch.ai", "sch.ai"),
        ("plc.io", "plc.io"),
        ("web.co", "web.co"),  # dropped from the PSL by the .co registry (2024-11-18)
        ("sch.gg", "sch.gg"),
        ("ato.gov.au", "ato.gov.au"),  # an agency's own domain under gov.au
        ("nsw.gov.au", "nsw.gov.au"),  # removed from the PSL at New South Wales' request
        ("x.blogspot.com", "x.blogspot.com"),  # a name under a PRIVATE suffix is someone's
        # IDN TLDs in punycode (.рф, .中国).
        ("XN--80AK6AA92E.xn--p1ai", "xn--80ak6aa92e.xn--p1ai"),
        ("example.xn--fiqs8s", "example.xn--fiqs8s"),
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
        "yahoo.com.ar",  # free mail under a country domain
        "eu.hotmail.co.uk",
    ],
)
def test_group_domains_rejects_public_domains(domain):
    with pytest.raises(ValidationError, match="public domains"):
        account(f"group_domains: ['{domain}']\n")


@pytest.mark.parametrize(
    "domain",
    [
        "co.uk",
        "com.ar",
        "ac.jp",
        "gouv.fr",
        "nhs.uk",
        "adv.br",
        "govt.nz",
        "asso.fr",
        "com.ai",
        "com.co",
        "com.io",
        "org.io",
        "gov.io",
        "org.me",
        "plc.uk",
        # Deeper suffixes and other generic-use ccTLDs, from the Public Suffix List.
        "wa.gov.au",
        "vic.gov.au",
        "kommune.no",
        "plc.ly",
        "co.gg",
        # PRIVATE section: platforms where anyone can get a subdomain.
        "blogspot.com",
        "github.io",
        # IDN suffixes in punycode (公司.香港, 個人.hk).
        "xn--55qx5d.xn--j6w193g",
        "xn--gmqw5a.hk",
    ],
)
def test_group_domains_rejects_public_suffixes(domain):
    with pytest.raises(ValidationError, match="is a public suffix"):
        account(f"group_domains: ['{domain}']\n")


def test_production_group_domain_still_loads():
    """The deployed accounts file has `group_domains: [litebox.ai]`: a domain, not a suffix."""
    assert account("group_domains: [litebox.ai]\n").group_domains == ["litebox.ai"]


@pytest.mark.parametrize(
    "domain", ["hello@example.org", "example .org", "", "@", "com", "a.b1", "example.xn--"]
)
def test_group_domains_rejects_non_domains(domain):
    with pytest.raises(ValidationError, match="isn't a domain"):
        account(f"group_domains: ['{domain}']\n")


def test_env_example_lists_every_setting():
    """Each `Settings` field appears in .env.example (set or commented out), so a new setting
    can't ship undocumented."""
    text = (ROOT / ".env.example").read_text()
    listed = set(re.findall(r"^#?([A-Z][A-Z0-9_]*)=", text, re.MULTILINE))
    assert {name.upper() for name in Settings.model_fields} - listed == set()


def test_settings_defaults_are_unchanged():
    s = Settings(_env_file=None)
    assert (s.max_messages_per_run, s.max_unsubscribes_per_run, s.body_chars) == (200, 20, 3000)
    assert s.log_level is LogLevel.INFO


@pytest.mark.parametrize(("value", "level"), [("debug", "DEBUG"), (" Warning ", "WARNING")])
def test_log_level_is_case_insensitive(value, level):
    assert Settings(_env_file=None, log_level=value).log_level == level


@pytest.mark.parametrize("value", ["verbose", "WARN", ""])
def test_log_level_rejects_unknown_levels(value):
    # "WARN" is an alias `logging` accepts, but only the five canonical names are documented.
    with pytest.raises(ValidationError, match="log_level"):
        Settings(_env_file=None, log_level=value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_messages_per_run", 0),
        ("max_messages_per_run", -1),
        ("max_unsubscribes_per_run", -1),
        ("body_chars", 0),
        ("body_chars", -100),  # would cut the body from the end
    ],
)
def test_settings_reject_out_of_range_ints(field, value):
    with pytest.raises(ValidationError, match=field):
        Settings(_env_file=None, **{field: value})


def test_zero_unsubscribes_per_run_is_allowed():
    """MAX_UNSUBSCRIBES_PER_RUN=0 is the documented way to never unsubscribe."""
    assert Settings(_env_file=None, max_unsubscribes_per_run=0).max_unsubscribes_per_run == 0


def _with(cfg, path, value):
    """`cfg` as a dict with the key at `path` (a dotted path, indexes as ints) set to `value`."""
    data = cfg.model_dump()
    *parents, last = [int(k) if k.isdigit() else k for k in path.split(".")]
    node = data
    for key in parents:
        node = node[key]
    node[last] = value
    return data


@pytest.mark.parametrize(
    "path",
    [
        "categories.0.min_confidence",
        "tags.0.threshold",
        "needs_reply.threshold",
        "auto_rule.min_confidence",
        "auto_rule.max_keep_probability",
        "archive_group_min_confidence",
        "archive_group_reply_keep_probability",
    ],
)
@pytest.mark.parametrize("value", [-0.1, 1.5])
def test_probabilities_must_be_between_0_and_1(cfg, path, value):
    with pytest.raises(ValidationError, match="less than or equal to 1|greater than or equal"):
        CategoriesConfig.model_validate(_with(cfg, path, value))


def test_auto_rule_needs_at_least_one_hit(cfg):
    with pytest.raises(ValidationError, match="hits"):
        CategoriesConfig.model_validate(_with(cfg, "auto_rule.hits", 0))


@pytest.mark.parametrize("value", [-0.1, 1.5])
def test_account_min_confidence_must_be_between_0_and_1(value):
    with pytest.raises(ValidationError, match="min_confidence"):
        account(f"min_confidence: {{marketing: {value}}}\n")


def test_validate_accounts_accepts_known_keys(cfg):
    ok = account("act_categories: [marketing]\nmin_confidence: {newsletter: 0.9}\n")
    validate_accounts([ok, account("")], cfg)  # no error


def test_validate_accounts_lists_every_unknown_key_of_every_account(cfg):
    a = account("act_categories: [marketing, notifications]\nmin_confidence: {markting: 0.9}\n")
    b = Account.model_validate({"email": "b@acme.com", "act_categories": ["promo"]})
    with pytest.raises(ConfigError) as err:
        validate_accounts([a, b], cfg)
    message = str(err.value)
    assert "me@acme.com: unknown act_categories ['notifications']" in message
    assert "me@acme.com: unknown min_confidence ['markting']" in message
    assert "b@acme.com: unknown act_categories ['promo']" in message
    assert "known: important," in message
