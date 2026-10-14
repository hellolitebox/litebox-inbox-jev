"""CLI input checks: `rules set` takes an address or @domain, `restore` and `reprocess` take a
category from categories.yaml (restore also one its stored emails still carry)."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from inbox_jev import cli, triage
from inbox_jev.db import Message, SenderRule

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(monkeypatch, tmp_path, make_sessions):
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("CATEGORIES_FILE", str(ROOT / "categories.yaml"))
    monkeypatch.setattr(cli.logs, "setup", lambda level: None)  # keep the test's logging as is
    return make_sessions(url)


def _invoke(*args):
    return CliRunner().invoke(cli.app, list(args))


@pytest.mark.parametrize(
    ("sender", "key"),
    [
        ("Ana@Example.com", "ana@example.com"),
        ("@Example.co.uk", "@example.co.uk"),
        ("@XN--80AK6AA92E.xn--p1ai", "@xn--80ak6aa92e.xn--p1ai"),  # an IDN TLD in punycode
    ],
)
def test_rules_set_stores_an_address_or_domain_lowercased(db, sender, key):
    result = _invoke("rules", "set", "-a", "me@acme.com", sender, "marketing")
    assert result.exit_code == 0, result.output
    assert f"{key} → marketing" in result.output
    with db() as s:
        assert [r.sender_key for r in s.query(SenderRule)] == [key]


@pytest.mark.parametrize(
    "sender",
    [
        "example.com",
        "Ana <ana@example.com>",
        "@",
        "@exa mple.com",
        "ana@",
        "@@x.com",
        "@example.xn--",
    ],
)
def test_rules_set_refuses_anything_else(db, sender):
    result = _invoke("rules", "set", "-a", "me@acme.com", sender, "marketing")
    assert result.exit_code == 2
    assert "neither an address" in result.output
    with db() as s:
        assert s.query(SenderRule).count() == 0


def test_rules_delete_normalises_the_sender_like_rules_set(db):
    assert _invoke("rules", "set", "-a", "me@acme.com", "@Example.com", "marketing").exit_code == 0
    result = _invoke("rules", "delete", "-a", "me@acme.com", "  @EXAMPLE.com ")
    assert result.exit_code == 0, result.output
    assert "Deleted rule for @example.com" in result.output
    with db() as s:
        assert s.query(SenderRule).count() == 0


def test_rules_delete_refuses_a_sender_that_is_no_rule_key(db):
    result = _invoke("rules", "delete", "-a", "me@acme.com", "example.com")
    assert result.exit_code == 2 and "neither an address" in result.output


@pytest.mark.parametrize("legacy", ["example.com", "acme billing"])
def test_rules_delete_removes_a_legacy_rule_that_is_no_rule_key(db, legacy):
    """A rule stored as typed before `rules set` checked its input can still be deleted."""
    with db() as s:
        s.add(
            SenderRule(
                account="me@acme.com", sender_key=legacy, category="marketing", source="manual"
            )
        )
        s.commit()
    result = _invoke("rules", "delete", "-a", "me@acme.com", f" {legacy.upper()} ")
    assert result.exit_code == 0, result.output
    assert f"Deleted rule for {legacy}" in result.output
    with db() as s:
        assert s.query(SenderRule).count() == 0


def test_rules_delete_of_a_missing_rule_is_a_usage_error(db):
    result = _invoke("rules", "delete", "-a", "me@acme.com", "ana@example.com")
    assert result.exit_code == 2 and "no such rule" in result.output


def test_rules_set_refuses_an_unknown_category(db):
    result = _invoke("rules", "set", "-a", "me@acme.com", "ana@example.com", "markting")
    assert result.exit_code == 2 and "unknown category 'markting'" in result.output


def _no_gmail(monkeypatch):
    def load_credentials(tokens_dir, email):
        raise AssertionError("Gmail must not be touched")

    monkeypatch.setattr(cli.gmail_mod, "load_credentials", load_credentials)


def test_restore_refuses_an_unknown_category_before_gmail(db, monkeypatch):
    _no_gmail(monkeypatch)
    result = _invoke("restore", "-a", "me@acme.com", "--category", "markting")
    assert result.exit_code == 2
    assert "unknown category 'markting'" in result.output


def test_restore_accepts_a_removed_category_its_emails_still_carry(db, monkeypatch):
    with db() as s:
        s.add(
            Message(
                account="me@acme.com",
                gmail_id="1",
                thread_id="t1",
                sender="a@b.com",
                category="promotions",  # since removed from categories.yaml
                mode="act",
            )
        )
        s.commit()
    monkeypatch.setattr(cli.gmail_mod, "load_credentials", lambda d, e: None)
    monkeypatch.setattr(cli.gmail_mod, "Gmail", lambda creds: "gmail")
    seen = []

    def restore(session, gmail, account, since, category):
        seen.append(category)
        return triage.RestoreResult(restored=1)

    monkeypatch.setattr(triage, "restore", restore)
    result = _invoke("restore", "-a", "me@acme.com", "--category", "promotions")
    assert result.exit_code == 0, result.output
    assert seen == ["promotions"]
    # Another account's stored emails don't make the category valid here.
    other = _invoke("restore", "-a", "you@acme.com", "--category", "promotions")
    assert other.exit_code == 2 and seen == ["promotions"]


def test_reprocess_refuses_an_unknown_category(db):
    result = _invoke("reprocess", "-a", "me@acme.com", "--category", "markting")
    assert result.exit_code == 2
    assert "unknown category 'markting'" in result.output
