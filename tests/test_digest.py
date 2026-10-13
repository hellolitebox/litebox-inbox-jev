"""Digest language: every language has every text, and each renders the whole digest."""

from string import Formatter

import pytest
from conftest import ROOT
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from inbox_jev import cli, digest
from inbox_jev.config import DigestLanguage, Settings
from inbox_jev.db import (
    MAX_ERROR_ATTEMPTS,
    Message,
    MessageStatus,
    Run,
    Unsubscribe,
    make_session_factory,
)
from inbox_jev.unsubscribe import UnsubscribeMethod, UnsubscribeStatus

ACCOUNT = "me@acme.com"


def _placeholders(template: str) -> set[str]:
    return {field for _, field, _, _ in Formatter().parse(template) if field}


def _templates(value: str | digest.PluralForms) -> dict[str, str]:
    """A message's templates by form: a plain text has one, a count has `one` and `other`."""
    return {"": value} if isinstance(value, str) else dict(value)


def test_every_language_has_every_message():
    assert set(digest.MESSAGES) == set(DigestLanguage)
    expected = digest.DigestMessages.__required_keys__
    for language, messages in digest.MESSAGES.items():
        assert set(messages) == expected, language


def test_every_language_uses_the_same_placeholders():
    english = digest.MESSAGES[DigestLanguage.EN]
    for language, messages in digest.MESSAGES.items():
        for key, value in messages.items():
            forms, english_forms = _templates(value), _templates(english[key])
            assert forms.keys() == english_forms.keys(), (language, key)
            for form, template in forms.items():
                expected = _placeholders(english_forms[form])
                assert _placeholders(template) == expected, (language, key, form)


def test_every_count_has_its_number_in_both_forms():
    for language, messages in digest.MESSAGES.items():
        for key, value in messages.items():
            if not isinstance(value, str):
                for form, template in value.items():
                    assert _placeholders(template) == {"n"}, (language, key, form)


def _seed(session: Session, cfg) -> None:
    session.add_all(
        [
            Message(
                account=ACCOUNT,
                gmail_id="1",
                thread_id="t1",
                sender="ana@client.com",
                subject="",  # renders the language's "no subject"
                status=MessageStatus.DONE,
                category="important",
                mode="act",
                decided_labels=[cfg.needs_reply.label],
            ),
            Message(
                account=ACCOUNT,
                gmail_id="2",
                thread_id="t2",
                sender="bob@vendor.com",
                subject="Broken <thing>",
                status=MessageStatus.ERROR,
                error="boom",
                error_attempts=MAX_ERROR_ATTEMPTS,
                mode="act",
            ),
            Run(account=ACCOUNT, mode="act", error="token expired"),
            Run(account=ACCOUNT, mode="act", error="token expired"),
            Unsubscribe(
                account=ACCOUNT,
                list_key="news.shop.com",
                sender="promo@news.shop.com",
                gmail_id="3",
                method=UnsubscribeMethod.MAILTO,
                status=UnsubscribeStatus.MANUAL_PENDING,
            ),
        ]
    )
    session.commit()


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            DigestLanguage.EN,
            [
                "(last 24h, mode: act)",
                "1 processed — ",
                "*⚡ Needs reply (1)*",
                "(no subject)",
                "*🖐 Manual unsubscribe (1)*",
                "• news.shop.com — link in the body",
                "*⚠️ Errors*: 1 email (1 not retried again), 2 failed runs",
                "• Failed run (2 runs, last ",
                f"_Not retried again after {MAX_ERROR_ATTEMPTS} attempts",
                "Broken &lt;thing&gt;",
            ],
        ),
        (
            DigestLanguage.ES,
            [
                "(últimas 24h, modo: act)",
                "1 procesado — ",
                "*⚡ Piden respuesta (1)*",
                "(sin asunto)",
                "*🖐 Unsubscribe manual (1)*",
                "• news.shop.com — link en el cuerpo",
                "*⚠️ Errores*: 1 mail (1 sin más reintentos), 2 corridas fallidas",
                "• Corrida fallida (2 corridas, última ",
                f"_Sin más reintentos tras {MAX_ERROR_ATTEMPTS} intentos",
                "Broken &lt;thing&gt;",
            ],
        ),
    ],
)
def test_digest_renders_in_the_account_language(tmp_path, cfg, language, expected):
    sessions = make_session_factory(f"sqlite:///{tmp_path / 'digest.db'}")
    with sessions() as s:
        _seed(s, cfg)
        text = digest.build(s, cfg, ACCOUNT, language=language)
    for fragment in expected:
        assert fragment in text, fragment
    other = next(lang for lang in DigestLanguage if lang != language)
    assert digest.MESSAGES[other]["no_subject"] not in text


def _seed_counts(session: Session, n: int) -> None:
    """`n` processed emails, `n` failed emails and `n` failed runs."""
    for i in range(n):
        session.add_all(
            [
                Message(
                    account=ACCOUNT,
                    gmail_id=f"done{i}",
                    thread_id=f"td{i}",
                    sender="ana@client.com",
                    subject="Hi",
                    status=MessageStatus.DONE,
                    category="important",
                    mode="act",
                ),
                Message(
                    account=ACCOUNT,
                    gmail_id=f"error{i}",
                    thread_id=f"te{i}",
                    sender="bob@vendor.com",
                    subject="Broken",
                    status=MessageStatus.ERROR,
                    error="boom",
                    mode="act",
                ),
                Run(account=ACCOUNT, mode="act", error="token expired"),
            ]
        )
    session.commit()


@pytest.mark.parametrize(
    ("language", "n", "expected", "wrong"),
    [
        (
            DigestLanguage.EN,
            1,
            ["1 processed — ", "*: 1 email (0 not retried again), 1 failed run\n"],
            ["1 emails", "1 failed runs"],
        ),
        (
            DigestLanguage.EN,
            2,
            ["2 processed — ", "*: 2 emails (0 not retried again), 2 failed runs\n"],
            ["2 email (", "2 failed run\n"],
        ),
        (
            DigestLanguage.ES,
            1,
            ["1 procesado — ", "*: 1 mail (0 sin más reintentos), 1 corrida fallida\n"],
            ["1 procesados", "1 mails", "1 corridas fallidas"],
        ),
        (
            DigestLanguage.ES,
            2,
            ["2 procesados — ", "*: 2 mails (0 sin más reintentos), 2 corridas fallidas\n"],
            ["2 procesado —", "2 mail (", "2 corrida fallida\n"],
        ),
    ],
)
def test_counts_are_singular_for_one_and_plural_otherwise(
    tmp_path, cfg, language, n, expected, wrong
):
    sessions = make_session_factory(f"sqlite:///{tmp_path / 'digest.db'}")
    with sessions() as s:
        _seed_counts(s, n)
        text = digest.build(s, cfg, ACCOUNT, language=language)
    for fragment in expected:
        assert fragment in text, fragment
    for fragment in wrong:
        assert fragment not in text, fragment


def test_cli_digest_uses_each_account_language(tmp_path, monkeypatch):
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text(
        "accounts:\n  - email: en@acme.com\n  - email: es@acme.com\n    digest_language: es\n"
    )
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'cli.db'}",
        accounts_file=accounts,
        categories_file=ROOT / "categories.yaml",
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli.logs, "setup", lambda level: None)
    result = CliRunner().invoke(cli.app, ["digest", "--dry-print"])
    assert result.exit_code == 0, result.output
    english, spanish = result.output.split("*📬 Inbox Jev — ")[1:]
    assert english.startswith("en@acme.com* (last 24h") and "0 processed — nothing" in english
    assert spanish.startswith("es@acme.com* (últimas 24h") and "0 procesados — nada" in spanish
