"""Digest language: every language has every text, and each renders the whole digest."""

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from string import Formatter

import httpx
import pytest
from conftest import ROOT, assert_valid_block_kit, cell_style, cell_text
from fakes import FakeGmail, HttpError, email
from sqlalchemy.orm import Session
from typer.testing import CliRunner, Result

from inbox_jev import cli, digest, slack
from inbox_jev.config import Account, DigestLanguage, Settings
from inbox_jev.db import (
    MAX_ERROR_ATTEMPTS,
    Message,
    MessageStatus,
    Run,
    Unsubscribe,
    now,
    open_sessions,
)
from inbox_jev.gmail import Email, ThreadMessage
from inbox_jev.system_labels import INBOX, SENT
from inbox_jev.unsubscribe import UnsubscribeMethod, UnsubscribeStatus

ACCOUNT = "me@acme.com"


def _account(language: DigestLanguage, **fields) -> Account:
    return Account(email=ACCOUNT, digest_language=language, **fields)


def _totals(text: str) -> dict[str, int]:
    """The totals table's rows (label: count), from its code block; {} when it has none."""
    if "```" not in text:
        return {}
    block = text.split("```\n", 1)[1].split("\n```", 1)[0]
    rows = (line.rsplit(" ", 1) for line in block.split("\n") if not set(line) <= {"─"})
    return {label.strip(): int(n) for label, n in rows}


def _placeholders(template: str) -> set[str]:
    return {field for _, field, _, _ in Formatter().parse(template) if field}


def _templates(value: str | tuple[str, ...] | digest.PluralForms) -> dict[str, str]:
    """A message's templates by form: a plain text has one, a count has `one` and `other`, a
    list of names one per name."""
    if isinstance(value, str):
        return {"": value}
    return (
        {str(i): name for i, name in enumerate(value)} if isinstance(value, tuple) else dict(value)
    )


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


def test_every_language_names_every_weekday_and_month():
    for messages in digest.MESSAGES.values():
        assert (len(messages["weekdays"]), len(messages["months"])) == (7, 12)


def test_every_count_has_its_number_in_both_forms():
    for language, messages in digest.MESSAGES.items():
        for key, value in messages.items():
            if isinstance(value, dict):
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
                "me@acme.com · last 24h · mode: act",
                "```\nJev/Important  1\n────────────────\nProcessed      1\n```",
                "*⚡ Needs reply (1)*",
                "(no subject)",
                "*🖐 Manual unsubscribe (1)*",
                "• news.shop.com — link in the body",
                "*⚠️ Errors*: 1 email _(1 not retried again)_, 2 failed runs",
                "• Failed run _(2 runs, last ",
                f"_Not retried again after {MAX_ERROR_ATTEMPTS} attempts",
                "Broken &lt;thing&gt;",
            ],
        ),
        (
            DigestLanguage.ES,
            [
                "me@acme.com · últimas 24h · modo: act",
                "```\nJev/Important  1\n────────────────\nProcesados     1\n```",
                "*⚡ Piden respuesta (1)*",
                "(sin asunto)",
                "*🖐 Unsubscribe manual (1)*",
                "• news.shop.com — link en el cuerpo",
                "*⚠️ Errores*: 1 mail _(1 sin más reintentos)_, 2 corridas fallidas",
                "• Corrida fallida _(2 corridas, última ",
                f"_Sin más reintentos tras {MAX_ERROR_ATTEMPTS} intentos",
                "Broken &lt;thing&gt;",
            ],
        ),
    ],
)
def test_digest_renders_in_the_account_language(tmp_path, cfg, language, expected):
    with open_sessions(f"sqlite:///{tmp_path / 'digest.db'}") as sessions, sessions() as s:
        _seed(s, cfg)
        text = digest.build(s, cfg, _account(language), gmail=None).text
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
            ["*: 1 email _(0 not retried again)_, 1 failed run\n"],
            ["1 emails", "1 failed runs"],
        ),
        (
            DigestLanguage.EN,
            2,
            ["*: 2 emails _(0 not retried again)_, 2 failed runs\n"],
            ["2 email _(", "2 failed run\n"],
        ),
        (
            DigestLanguage.ES,
            1,
            ["*: 1 mail _(0 sin más reintentos)_, 1 corrida fallida\n"],
            ["1 mails", "1 corridas fallidas"],
        ),
        (
            DigestLanguage.ES,
            2,
            ["*: 2 mails _(0 sin más reintentos)_, 2 corridas fallidas\n"],
            ["2 mail _(", "2 corrida fallida\n"],
        ),
    ],
)
def test_counts_are_singular_for_one_and_plural_otherwise(
    tmp_path, cfg, language, n, expected, wrong
):
    with open_sessions(f"sqlite:///{tmp_path / 'digest.db'}") as sessions, sessions() as s:
        _seed_counts(s, n)
        text = digest.build(s, cfg, _account(language), gmail=None).text
    for fragment in expected:
        assert fragment in text, fragment
    for fragment in wrong:
        assert fragment not in text, fragment


def test_totals_table_is_aligned_most_first_and_safe_in_its_code_block(tmp_path, cfg):
    """Text version: counts right-aligned, categories most first; a label (here an old
    category key shown as is) can't close the code block, mention or link, and a long one is
    cut."""
    odd = "`<!channel>` & a category key far too long to fit"
    rows = [_row(f"n{i}", "x", category="notification") for i in range(12)]
    rows += [_row("o1", "x", category=odd), _row("i1", "x")]
    with open_sessions(f"sqlite:///{tmp_path / 'digest.db'}") as sessions, sessions() as s:
        s.add_all(rows)
        s.commit()
        text = digest.build(s, cfg, _account(DigestLanguage.EN), gmail=None).text
    cut = "'&lt;!channel&gt;' &amp; a category k…"
    width = slack.MAX_LABEL_WIDTH
    assert text.split("```\n")[1] == (
        f"{'Jev/Notifications':<{width}}  12\n"
        f"{'Jev/Important':<{width}}   1\n"  # a tie: by label
        f"{cut}   1\n"  # 28 characters once Slack decodes it
        + "─" * (width + 4)
        + f"\n{'Processed':<{width}}  14\n"
    )
    assert text.count("```") == 2
    assert "<!channel>" not in text


def test_an_empty_window_is_inbox_zero_without_a_table(tmp_path, cfg):
    with open_sessions(f"sqlite:///{tmp_path / 'digest.db'}") as sessions, sessions() as s:
        built = digest.build(s, cfg, _account(DigestLanguage.ES), gmail=FakeGmail([]))
    assert "*Inbox en cero — hoy nada te necesita* ✨" in built.text
    assert "_Nada procesado._" in built.text and "```" not in built.text
    assert built.notification == "Inbox en cero — hoy nada te necesita · 0 procesados"
    types = [b["type"] for b in built.blocks()]
    assert types == [
        *("header", "context", "section"),  # title, account, inbox zero
        *("divider", "section", "section"),  # what Jev did: nothing processed
        *("divider", "context"),  # footer
    ]
    assert "table" not in types


def _build_rows(tmp_path, cfg, rows: list, gmail: FakeGmail | None, **account) -> slack.Digest:
    with open_sessions(f"sqlite:///{tmp_path / 'digest.db'}") as sessions, sessions() as s:
        s.add_all(rows)
        s.commit()
        return digest.build(s, cfg, _account(DigestLanguage.EN, **account), gmail=gmail)


def test_a_full_digest_is_valid_block_kit(tmp_path, cfg):
    with open_sessions(f"sqlite:///{tmp_path / 'digest.db'}") as sessions, sessions() as s:
        gmail = _seed_pending(s, cfg)
        s.add_all(
            [
                _corrected("c1", "news@paper.example", "newsletter", "spam"),
                Run(account=ACCOUNT, mode="act", error="token expired"),
            ]
        )
        s.commit()
        built = digest.build(s, cfg, _account(DigestLanguage.EN), gmail=gmail, preview=True)
    blocks = built.blocks()
    assert_valid_block_kit(blocks)
    assert blocks[0]["text"]["text"].startswith("[preview] 📬 Inbox Jev · ")
    assert built.notification == "[preview] 5 emails need you · 10 processed"
    texts = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    assert texts[0] == "*5 emails need you*"
    assert texts[1].startswith("*🔔 `Jev/Action` (1)*\n• *<https://mail.google.com/")
    assert any(t.startswith("*🧠 Corrections learned (1)*") for t in texts)
    assert any(t.startswith("*⚠️ Errors*") for t in texts)
    # Category labels are code; the header, To review, the total and Already handled aren't.
    (table,) = [b for b in blocks if b["type"] == "table"]
    styles = {cell_text(row[0]): cell_style(row[0]) for row in table["rows"]}
    assert styles == {
        "Category": "bold",
        "Jev/Important": "code",
        "Jev/Action": "code",
        "Jev/Newsletters": "code",
        "To review": None,
        "Processed": "bold",
        "Already handled": None,
    }


def test_a_label_is_code_with_its_backticks_as_quotes_and_escaped(tmp_path, cfg):
    """Labels come from categories.yaml: one with a backtick can't close its code span early,
    and `<`, `>`, `&` stay escaped inside it."""
    odd = "Jev/`Act` <x>&y"
    data = cfg.model_dump()
    for category in data["categories"]:
        if category["key"] == "action_required":
            category["label"] = odd
    odd_cfg = type(cfg).model_validate(data)
    assert digest.code(odd) == "`Jev/'Act' &lt;x&gt;&amp;y`"
    built = _build_rows(
        tmp_path, odd_cfg, [_row("a1", "Approve", category="action_required")], None
    )
    assert built.needs_you[0].title == "*🔔 `Jev/'Act' &lt;x&gt;&amp;y` (1)*"
    (table,) = [b for b in built.blocks() if b["type"] == "table"]
    assert (cell_text(table["rows"][1][0]), cell_style(table["rows"][1][0])) == (
        "Jev/'Act' <x>&y",  # rich text shows its text as is: no escaping there
        "code",
    )
    assert "\nJev/'Act' &lt;x&gt;&amp;y  1\n" in built.text  # the code block: no backticks


def test_needs_you_items_are_capped_with_a_link_to_the_rest(tmp_path, cfg):
    n = digest.NEEDS_YOU_ITEMS + 5
    rows = [_row(f"a{i}", f"Approve {i}", category="action_required") for i in range(n)]
    built = _build_rows(tmp_path, cfg, rows, None)
    (group,) = built.needs_you
    assert group.title == f"*🔔 `Jev/Action` ({n})*"
    assert (len(group.lines), group.hidden) == (digest.NEEDS_YOU_ITEMS, 5)
    assert group.link == (
        "https://mail.google.com/mail/u/me@acme.com/#search/label%3Ajev-action%20in%3Ainbox"
    )
    assert built.hero == f"*{n} emails need you*"
    section = next(b for b in built.blocks() if "Jev/Action" in b.get("text", {}).get("text", ""))
    assert section["text"]["text"].endswith(f"+5 more · <{group.link}|see all in Gmail>")


def test_an_item_shows_subject_sender_name_and_how_long_ago(tmp_path, cfg):
    row = _row(
        "a1",
        "A subject far longer than sixty characters, which is cut on a phone screen",
        category="action_required",
        from_header='"Ana Pérez" <ana@client.com>',
        received_at=now() - timedelta(hours=3, minutes=5),
    )
    (group,) = _build_rows(tmp_path, cfg, [row], None).needs_you
    assert group.lines == [
        "• *<https://mail.google.com/mail/u/?authuser=me@acme.com#all/a1|"
        "A subject far longer than sixty characters, which is cut on…>*\n"
        "      Ana Pérez · 3h ago"
    ]


def test_email_text_renders_inert_in_every_block(tmp_path, cfg):
    """A subject, sender or list can't mention anyone or add a link, in blocks or text: the
    `<!channel>` forms are escaped, and the plain `@here` / `@channel` / `#general` ones sit in
    verbatim mrkdwn, which Slack doesn't parse into mentions or channel links."""
    evil = "<!channel> <https://evil.example|x> <@U123>"
    plain = "Invoice @here @channel #general"
    rows = [
        _row("a1", evil, category="action_required", from_header=f'"{evil}" <a@b.example>'),
        _row("a2", plain, category="action_required", from_header=f'"{plain}" <c@d.example>'),
        _corrected("c1", "<!here>@x.example", "newsletter", "spam"),
    ]
    built = _build_rows(tmp_path, cfg, rows, None)
    blocks = built.blocks()
    assert_valid_block_kit(blocks)
    rendered = json.dumps(blocks, ensure_ascii=False) + built.text
    for live in ("<!channel>", "<!here>", "<https://evil", "<@U123>"):
        assert live not in rendered, live
    assert "&lt;!channel&gt; &lt;https://evil.example|x&gt;" in rendered
    (listed,) = [b["text"] for b in blocks if b["type"] == "section" and plain in b["text"]["text"]]
    assert listed["verbatim"] is True


@pytest.mark.parametrize(
    ("ago", "language", "expected"),
    [
        (timedelta(seconds=20), DigestLanguage.EN, "just now"),
        (timedelta(minutes=7), DigestLanguage.EN, "7 min ago"),
        (timedelta(hours=47, minutes=59), DigestLanguage.EN, "47h ago"),
        (timedelta(days=3, hours=1), DigestLanguage.EN, "3d ago"),
        (timedelta(seconds=20), DigestLanguage.ES, "recién"),
        (timedelta(minutes=7), DigestLanguage.ES, "hace 7 min"),
        (timedelta(hours=5), DigestLanguage.ES, "hace 5 h"),
        (timedelta(days=3), DigestLanguage.ES, "hace 3 d"),
        (-timedelta(minutes=5), DigestLanguage.EN, "just now"),  # a clock ahead of ours
    ],
)
def test_how_long_ago(ago, language, expected):
    at = datetime(2026, 10, 15, 11, 0, tzinfo=UTC)
    t = digest.MESSAGES[language]
    assert digest._ago(t, at - ago, at) == expected
    assert digest._ago(t, (at - ago).replace(tzinfo=None), at) == expected  # SQLite: naive UTC


def test_the_title_names_the_day_in_the_account_language(tmp_path, cfg):
    with open_sessions(f"sqlite:///{tmp_path / 'digest.db'}") as sessions, sessions() as s:
        english = digest.build(s, cfg, _account(DigestLanguage.EN), gmail=None)
        spanish = digest.build(s, cfg, _account(DigestLanguage.ES), gmail=None)
    today = now()
    en, es = digest.MESSAGES[DigestLanguage.EN], digest.MESSAGES[DigestLanguage.ES]
    weekday, month = today.weekday(), today.month - 1
    assert (
        english.title
        == f"📬 Inbox Jev · {en['weekdays'][weekday]}, {en['months'][month]} {today.day}"
    )
    assert (
        spanish.title
        == f"📬 Inbox Jev · {es['weekdays'][weekday]} {today.day} {es['months'][month]}"
    )


def test_cli_digest_uses_each_account_language(tmp_path, monkeypatch):
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text(
        "accounts:\n  - email: en@acme.com\n  - email: es@acme.com\n    digest_language: es\n"
    )
    _cli_settings(tmp_path, monkeypatch, accounts)
    result = CliRunner().invoke(cli.app, ["digest", "--dry-print"])
    assert result.exit_code == 0, result.output
    english, spanish = result.output.split("*📬 Inbox Jev · ")[1:]
    assert "en@acme.com · last 24h" in english and "_Nothing processed._" in english
    assert "es@acme.com · últimas 24h" in spanish and "_Nada procesado._" in spanish


def _cli_settings(tmp_path, monkeypatch, accounts: Path, **fields) -> Settings:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'cli.db'}",
        accounts_file=accounts,
        categories_file=ROOT / "categories.yaml",
        **fields,
    )
    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda cls: settings))
    monkeypatch.setattr(cli.logs, "setup", lambda level: None)
    return settings


# --- Only what is still pending ---------------------------------------------------------------

LATER = 1_700_000_000_000 + 60_000
EARLIER = 1_700_000_000_000 - 60_000


def _row(gmail_id: str, subject: str, **fields) -> Message:
    """A processed email of thread "t<gmail_id>", as `fakes.email` builds its Gmail side."""
    defaults = {
        "category": "important",
        "mode": "act",
        "decided_labels": [],
        "sender": "ana@client.com",
    }
    return Message(
        account=ACCOUNT,
        gmail_id=gmail_id,
        thread_id=f"t{gmail_id}",
        subject=subject,
        status=MessageStatus.DONE,
        **{**defaults, **fields},
    )


def _sent(gmail_id: str, thread_id: str, date_ms: int) -> Email:
    return Email(gmail_id, thread_id, [SENT], {}, "", "", date_ms)


def _seed_pending(session: Session, cfg) -> FakeGmail:
    """Emails the digest would list, some handled since in Gmail; the Gmail they are in now."""
    reply = {"decided_labels": [cfg.needs_reply.label]}
    session.add_all(
        [
            _row("a1", "Approve payment", category="action_required"),
            _row("a2", "Sign the contract", category="action_required"),
            _row("r1", "Can we meet?", **reply),
            _row("r2", "Answered already", **reply),
            _row("r3", "Old reply before it", **reply),
            _row("r4", "Deleted since", **reply),
            _row("l1", "Maybe promo", category="marketing", low_confidence=True, confidence=0.5),
            _row("l2", "Maybe news", category="newsletter", low_confidence=True, confidence=0.6),
            _row("l3", "Archived guess", category="marketing", low_confidence=True),
            Unsubscribe(
                account=ACCOUNT,
                list_key="deals.shop.com",
                sender="deals@shop.com",
                gmail_id="u1",
                method=UnsubscribeMethod.ONE_CLICK,
                status=UnsubscribeStatus.OK,
            ),
            Unsubscribe(
                account=ACCOUNT,
                list_key="news.shop.com",
                sender="promo@news.shop.com",
                gmail_id="u2",
                method=UnsubscribeMethod.MAILTO,
                status=UnsubscribeStatus.MANUAL_PENDING,
                target="https://news.shop.com/unsub?a=1|2",
            ),
            Unsubscribe(
                account=ACCOUNT,
                list_key="spam.example",
                sender="x@spam.example",
                gmail_id="u3",
                method=UnsubscribeMethod.ONE_CLICK,
                status=UnsubscribeStatus.FAILED,
                error="HTTP 500",
            ),
        ]
    )
    session.commit()
    archived = email("a2", "Bank <b@bank.com>", "Sign the contract")
    archived.label_ids = []
    archived_low = email("l3", "Shop <s@shop.com>", "Archived guess")
    gmail = FakeGmail(
        [
            email("a1", "Bank <b@bank.com>", "Approve payment"),
            archived,
            email("r1", "Ana <ana@client.com>", "Can we meet?"),
            email("r2", "Ana <ana@client.com>", "Answered already"),
            _sent("s2", "tr2", LATER),  # my reply after it: handled
            email("r3", "Ana <ana@client.com>", "Old reply before it"),
            _sent("s3", "tr3", EARLIER),  # an older message of mine: still pending
            email("l1", "Shop <s@shop.com>", "Maybe promo"),
            email("l2", "News <n@news.com>", "Maybe news"),
            archived_low,
        ]
    )
    review = _review_label_id(gmail, cfg)
    for low in ("l1", "l2"):
        gmail.emails[low].label_ids.append(review)
    archived_low.label_ids = [review]  # archived with the label still on: handled
    return gmail


def _review_label_id(gmail: FakeGmail, cfg) -> str:
    return gmail.ensure_labels([cfg.review_label])[cfg.review_label]


def _build(tmp_path, cfg, language, gmail_for):
    with open_sessions(f"sqlite:///{tmp_path / 'digest.db'}") as sessions, sessions() as s:
        gmail = _seed_pending(s, cfg)
        return digest.build(s, cfg, _account(language), gmail=gmail_for(gmail)).text, gmail


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            DigestLanguage.EN,
            [
                "*5 emails need you*",
                # Already handled: archived, replied to, deleted, archived (Review)
                "```\nJev/Important    4\nJev/Action       2\nTo review        3\n"
                + "─" * 18
                + "\nProcessed        9\nAlready handled  4\n```",
                "*🔔 `Jev/Action` (1)*",
                "*⚡ Needs reply (2)*",
                "*🤔 To review: 2* from the last 24h, still in the inbox with `Jev/Review` · <",
                "|Review in Gmail>",
            ],
        ),
        (
            DigestLanguage.ES,
            [
                "*5 mails te necesitan*",
                "```\nJev/Important  4\nJev/Action     2\nA revisar      3\n"
                + "─" * 16
                + "\nProcesados     9\nYa resueltos   4\n```",
                "*🔔 `Jev/Action` (1)*",
                "*⚡ Piden respuesta (2)*",
                "*🤔 A revisar: 2* de las últimas 24h, siguen en el Inbox con `Jev/Review` · <",
                "|Revisar en Gmail>",
            ],
        ),
    ],
)
def test_digest_lists_only_what_is_still_pending(tmp_path, cfg, language, expected):
    text, gmail = _build(tmp_path, cfg, language, lambda g: g)
    for fragment in expected:
        assert fragment in text, fragment
    # still pending: in the inbox, no reply of mine after it (one before it doesn't count)
    for subject in ("Approve payment", "Can we meet?", "Old reply before it"):
        assert subject in text, subject
    # handled: archived, replied to after it, deleted from Gmail
    for subject in ("Sign the contract", "Answered already", "Deleted since"):
        assert subject not in text, subject
    # low confidence is a count and a search link, never a list
    for subject in ("Maybe promo", "Maybe news", "Archived guess"):
        assert subject not in text, subject
    assert digest.MESSAGES[language]["pending_check_failed"] not in text
    # one batched read, bounded to the threads the sections would list
    (batch,) = gmail.thread_batches
    assert sorted(batch) == ["ta1", "ta2", "tl1", "tl2", "tl3", "tr1", "tr2", "tr3", "tr4"]
    assert gmail.label_reads == 1  # the review label's id, once


def _build_low(tmp_path, cfg, rows: list[Message], gmail: FakeGmail) -> str:
    with open_sessions(f"sqlite:///{tmp_path / 'digest.db'}") as sessions, sessions() as s:
        s.add_all(rows)
        s.commit()
        return digest.build(s, cfg, _account(DigestLanguage.EN), gmail=gmail).text


def test_calendar_messages_are_counted_but_never_listed(tmp_path, cfg):
    """ADR 0049: invitations, updates and responses stay out of Action, Needs reply and Low
    confidence (and their threads aren't read), but count in the "What Jev did" table. A row whose
    `calendar_method` is None (any email stored before the column existed) is listed."""
    subjects = {
        "a1": "Approve payment",
        "c1": "Invitation: Vemos Lightspeed @ Thu Oct 15",
        "c2": "Updated invitation: Intradiem - Litebox @ Fri Oct 16",
        "c3": "Invitation: Maybe sync",
    }
    gmail = FakeGmail([email(i, "Ana <ana@client.com>", s) for i, s in subjects.items()])
    gmail.emails["c3"].label_ids.append(_review_label_id(gmail, cfg))
    request = {"calendar_method": "REQUEST"}
    rows = [
        _row("a1", subjects["a1"], category="action_required"),
        _row("c1", subjects["c1"], category="action_required", **request),
        _row("c2", subjects["c2"], decided_labels=[cfg.needs_reply.label], **request),
        _row("c3", subjects["c3"], category="action_required", low_confidence=True, **request),
    ]
    text = _build_low(tmp_path, cfg, rows, gmail)
    assert "Processed      4" in text and "To review      1" in text
    assert "*🔔 `Jev/Action` (1)*" in text and subjects["a1"] in text
    for gmail_id in ("c1", "c2", "c3"):
        assert subjects[gmail_id] not in text, gmail_id
    assert "Needs reply" not in text and "🤔 To review" not in text
    assert gmail.thread_batches == [["ta1"]]


def test_calendar_messages_stay_out_when_the_pending_check_fails(tmp_path, cfg):
    """ADR 0049 holds when Gmail can't be read and every email counts as pending: a calendar
    message is still neither in the To review line nor in the headline's count."""
    rows = [
        _row("l1", "Guess", low_confidence=True),
        _row("c1", "Invitation: Maybe sync", low_confidence=True, calendar_method="REQUEST"),
    ]
    with open_sessions(f"sqlite:///{tmp_path / 'digest.db'}") as sessions, sessions() as s:
        s.add_all(rows)
        s.commit()
        text = digest.build(s, cfg, _account(DigestLanguage.EN), gmail=None).text
    assert "*1 email needs you*" in text
    assert "*🤔 To review: 1* from the last 24h" in text and "To review  2" in text


def test_low_confidence_without_the_review_label_is_handled(tmp_path, cfg):
    """Removing the review label (kept in the inbox) accepts or corrects the suggestion."""
    gmail = FakeGmail([email(i, "Shop <s@shop.com>", "Guess") for i in ("l1", "l2", "l3")])
    gmail.emails["l1"].label_ids.append(_review_label_id(gmail, cfg))
    rows = [_row(i, "Guess", low_confidence=True) for i in ("l1", "l2", "l3")]
    text = _build_low(tmp_path, cfg, rows, gmail)
    assert "*🤔 To review: 1* from the last 24h, still in the inbox with `Jev/Review` · " in text
    assert _totals(text)["Already handled"] == 2


def test_low_confidence_is_handled_when_the_review_label_is_gone(tmp_path, cfg):
    """The review label deleted from Gmail: no email can still have it."""
    gmail = FakeGmail([email("l1", "Shop <s@shop.com>", "Guess")])
    text = _build_low(tmp_path, cfg, [_row("l1", "Guess", low_confidence=True)], gmail)
    assert "🤔" not in text
    assert _totals(text)["Already handled"] == 1


def test_needs_reply_stays_listed_when_the_review_label_is_removed(tmp_path, cfg):
    """Removing the review label settles the category, not the reply: a low-confidence email
    that also needs a reply stays under Needs reply until I reply or it leaves the inbox."""
    gmail = FakeGmail([email("l1", "Ana <ana@client.com>", "Can we talk?")])
    row = _row(
        "l1",
        "Can we talk?",
        low_confidence=True,
        decided_labels=[cfg.review_label, cfg.needs_reply.label],
    )
    text = _build_low(tmp_path, cfg, [row], gmail)
    assert "*⚡ Needs reply (1)*\n• *<" in text
    assert "Can we talk?" in text
    assert "🤔" not in text
    assert "Already handled" not in _totals(text)


def test_needs_reply_low_confidence_is_handled_once_replied(tmp_path, cfg):
    """Replied to and without the review label: no section shows it, so it is handled."""
    gmail = FakeGmail(
        [email("l1", "Ana <ana@client.com>", "Can we talk?"), _sent("s1", "tl1", LATER)]
    )
    row = _row(
        "l1",
        "Can we talk?",
        low_confidence=True,
        decided_labels=[cfg.review_label, cfg.needs_reply.label],
    )
    text = _build_low(tmp_path, cfg, [row], gmail)
    assert "Can we talk?" not in text
    assert "🤔" not in text
    assert _totals(text)["Already handled"] == 1


def test_dry_run_low_confidence_is_counted_without_the_label_or_its_link(tmp_path, cfg):
    """`dry_run` never applies the review label: a link to its search would show nothing."""
    gmail = FakeGmail([email(i, "Shop <s@shop.com>", "Guess") for i in ("l1", "l2", "l3")])
    gmail.emails["l3"].label_ids.append(_review_label_id(gmail, cfg))
    rows = [
        _row("l1", "Guess", low_confidence=True, mode="dry_run"),
        _row("l2", "Guess", low_confidence=True, mode="dry_run"),
        _row("l3", "Guess", low_confidence=True),
    ]
    text = _build_low(tmp_path, cfg, rows, gmail)
    lines = [line for line in text.split("\n") if "🤔" in line]
    assert lines == [
        "*🤔 To review: 1* from the last 24h, still in the inbox with `Jev/Review` · "
        "<https://mail.google.com/mail/u/me@acme.com/#search/"
        "label%3Ajev-review%20in%3Ainbox|Review in Gmail>",
        "*🤔 To review: 2* from the last 24h, still in the inbox _(dry run: not labeled)_",
    ]
    assert "Already handled" not in _totals(text)


def test_review_search_link_points_at_the_review_label_in_the_inbox(tmp_path, cfg):
    text, _ = _build(tmp_path, cfg, DigestLanguage.EN, lambda g: g)
    link = (
        "<https://mail.google.com/mail/u/me@acme.com/#search/"
        "label%3Ajev-review%20in%3Ainbox|Review in Gmail>"
    )
    assert link in text


def test_review_search_link_is_escaped_for_slack(cfg):
    review = "A|B <x>&y"
    lines = digest._low_confidence(
        digest.MESSAGES[DigestLanguage.EN],
        cfg.model_copy(update={"review_label": review}),
        "o'neil&co@acme.com",
        24,
        [_row("l1", "x", low_confidence=True)],
    )
    (line,) = lines
    url = line.split("· <", 1)[1].split("|Review in Gmail>", 1)[0]
    assert not set(url) & set("<>|& ")  # nothing in the URL can end or break the Slack link
    assert "with `A|B &lt;x&gt;&amp;y` · " in line  # the label shown is escaped too


def test_unsubscribe_sections_stay_in_full(tmp_path, cfg):
    text, _ = _build(tmp_path, cfg, DigestLanguage.EN, lambda g: g)
    assert "*🚫 Unsubscribed (1)*\n• deals.shop.com _(one-click)_" in text
    assert (
        "*🖐 Manual unsubscribe (1)*\n• news.shop.com — "
        "<https://news.shop.com/unsub?a=1%7C2|link>" in text
    )
    assert "*❗ Unsubscribe failed (1)*\n• spam.example: HTTP 500" in text


@pytest.mark.parametrize(
    ("language", "method", "shown"),
    [
        (DigestLanguage.EN, UnsubscribeMethod.ONE_CLICK, "_(one-click)_"),
        (DigestLanguage.EN, UnsubscribeMethod.MAILTO, "_(email)_"),
        (DigestLanguage.EN, UnsubscribeMethod.MANUAL, "_(manual)_"),
        (DigestLanguage.ES, UnsubscribeMethod.ONE_CLICK, "_(un clic)_"),
        (DigestLanguage.ES, UnsubscribeMethod.MAILTO, "_(por mail)_"),
        (DigestLanguage.ES, UnsubscribeMethod.MANUAL, "_(manual)_"),
        # A method this version doesn't know shows as stored, its `_` made `-` so it can't end
        # the italic early, and escaped.
        (DigestLanguage.EN, "list_post<x>", "_(list-post&lt;x&gt;)_"),
    ],
)
def test_unsubscribed_lines_name_the_method_in_words_in_italic(language, method, shown):
    row = Unsubscribe(
        account=ACCOUNT,
        list_key="deals.shop.com",
        sender="news@deals.shop.com",
        gmail_id="u1",
        method=method,
        status=UnsubscribeStatus.OK,
    )
    (group,) = digest._unsubscribe_sections(digest.MESSAGES[language], [row])
    assert group.lines == [f"• deals.shop.com {shown}"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("this email only", "_(this email only)_"),
        ("one_click", "_(one-click)_"),  # a `_` inside would close the italic early
        ("_a_b_", "_(-a-b-)_"),
        ("<!here> & co", "_(&lt;!here&gt; &amp; co)_"),
    ],
)
def test_aside_is_italic_and_underscore_safe(text, expected):
    rendered = digest.aside(text)
    assert rendered == expected
    assert "_" not in rendered[2:-2]  # only the italic's own two underscores


def test_every_digest_aside_is_italic():
    """The digest's own parenthetical notes are `_(…)_`, in every language; a count in a title,
    `(n)`, is part of the title, not an aside."""
    for language, messages in digest.MESSAGES.items():
        for key in (
            "low_confidence_dry_run",
            "correction_accepted",
            "this_email_only",
            "errors",
            "failed_run",
        ):
            template = messages[key]
            assert isinstance(template, str)
            asides = re.findall(r".?\([^)]*\).?", template)
            assert asides, (language, key)
            for found in asides:
                assert found.startswith("_(") and found.endswith(")_"), (language, key, found)


@pytest.mark.parametrize("failure", ["raises", "unavailable"])
@pytest.mark.parametrize("language", list(DigestLanguage))
def test_digest_lists_everything_when_the_pending_check_fails(
    tmp_path, cfg, caplog, failure, language
):
    def gmail_for(gmail):
        gmail.threads_error = HttpError(401)
        return gmail if failure == "raises" else None

    text, _ = _build(tmp_path, cfg, language, gmail_for)
    t = digest.MESSAGES[language]
    assert text.split("```\n")[2].startswith(t["pending_check_failed"])  # right after the table
    assert t["already_handled"] not in _totals(text)
    for subject in ("Approve payment", "Sign the contract", "Answered already", "Deleted since"):
        assert subject in text, subject  # listed as before the check existed
    assert t["low_confidence"].split("{n}")[0] + "3*" in text
    if failure == "raises":
        (warning,) = [r for r in caplog.records if r.levelname == "WARNING"]
        assert (warning.emails, warning.error_type) == (9, "HttpError")


def test_nothing_to_list_asks_gmail_nothing(tmp_path, cfg):
    gmail = FakeGmail([])
    with open_sessions(f"sqlite:///{tmp_path / 'digest.db'}") as sessions, sessions() as s:
        text = digest.build(s, cfg, _account(DigestLanguage.EN), gmail=gmail).text
        unavailable = digest.build(s, cfg, _account(DigestLanguage.EN), gmail=None).text
    assert gmail.thread_batches == []
    for out in (text, unavailable):
        assert "Already handled" not in _totals(out)
        assert digest.MESSAGES[DigestLanguage.EN]["pending_check_failed"] not in out


def _corrected(gmail_id: str, sender: str, category: str, corrected: str, **fields) -> Message:
    fields = {"correction_checked_at": now(), **fields}
    return _row(
        gmail_id,
        "Corrected",
        sender=sender,
        category=category,
        corrected_category=corrected,
        **fields,
    )


def _build_corrections(tmp_path, cfg, account: Account, rows: list[Message]) -> str:
    with open_sessions(f"sqlite:///{tmp_path / 'digest.db'}") as sessions, sessions() as s:
        s.add_all(rows)
        s.commit()
        return digest.build(s, cfg, account, gmail=FakeGmail([])).text


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            DigestLanguage.EN,
            "*🧠 Corrections learned (3)*\n"
            "• `noreply@crm.example`: `Jev/Notifications` → `Jev/Action` _(this email only)_\n"
            "• `shop@deals.example`: `Jev/Marketing` _(suggestion accepted)_\n"
            "• `news@paper.example`: `Jev/Newsletters` → `Jev/Spam`",
        ),
        (
            DigestLanguage.ES,
            "*🧠 Correcciones aprendidas (3)*\n"
            "• `noreply@crm.example`: `Jev/Notifications` → `Jev/Action` _(solo este mail)_\n"
            "• `shop@deals.example`: `Jev/Marketing` _(sugerencia aceptada)_\n"
            "• `news@paper.example`: `Jev/Newsletters` → `Jev/Spam`",
        ),
    ],
)
def test_corrections_are_listed_one_per_line(tmp_path, cfg, language, expected):
    """Newest first, with the category labels; a sender decided email by email made no sender
    rule, and the line says so."""
    account = _account(language, decide_each_email=["@crm.example"])
    earlier = now() - timedelta(minutes=5)
    rows = [
        _corrected("c1", "news@paper.example", "newsletter", "spam", correction_checked_at=earlier),
        _corrected("c2", "shop@deals.example", "marketing", "marketing", low_confidence=True),
        _corrected("c3", "noreply@crm.example", "notification", "action_required"),
    ]
    rows[1].correction_checked_at = earlier + timedelta(minutes=1)
    assert expected in _build_corrections(tmp_path, cfg, account, rows)


def test_a_correction_on_mail_sent_as_a_list_is_this_email_only(tmp_path, cfg):
    """A correction on an email sent as a list's own address made no sender rule (ADR 0051);
    one from the same address without the flag (direct, or stored before it) did."""
    earlier = now() - timedelta(minutes=5)
    rows = [
        _corrected("c1", "hello@acme.example", "newsletter", "spam", correction_checked_at=earlier),
        _corrected("c2", "hello@acme.example", "newsletter", "spam", sender_is_list=True),
    ]
    text = _build_corrections(tmp_path, cfg, _account(DigestLanguage.EN), rows)
    assert (
        "• `hello@acme.example`: `Jev/Newsletters` → `Jev/Spam` _(this email only)_\n"
        "• `hello@acme.example`: `Jev/Newsletters` → `Jev/Spam`\n"
    ) in text


def test_corrections_are_escaped_and_capped(tmp_path, cfg):
    """A sender can't mention or link in Slack; past MAX_ITEMS the rest is a count. A category
    no longer in categories.yaml shows its key."""
    rows = [
        _corrected(f"c{i}", f"x{i}@a.example", "gone_category", "spam")
        for i in range(digest.MAX_ITEMS + 2)
    ]
    rows[-1].sender = "<!channel>&co@a.example"  # the newest: listed first
    text = _build_corrections(tmp_path, cfg, _account(DigestLanguage.EN), rows)
    section = text.split("*🧠 Corrections learned (17)*\n", 1)[1].split("\n")
    assert len([line for line in section if line.startswith("• ")]) == digest.MAX_ITEMS
    assert section[digest.MAX_ITEMS] == "+2 more"
    assert "<!channel>" not in text
    assert "• `&lt;!channel&gt;&amp;co@a.example`: `gone_category` → `Jev/Spam`" in text


def test_a_correction_sender_can_not_open_an_italic(tmp_path, cfg):
    """A sender starting with `_` is inside a code span, so it can't pair with the closing `_`
    of the line's own aside and italicise the labels in between."""
    account = _account(DigestLanguage.EN, decide_each_email=["_bounce@x.example"])
    rows = [_corrected("c1", "_bounce@x.example", "newsletter", "spam")]
    text = _build_corrections(tmp_path, cfg, account, rows)
    line = "• `_bounce@x.example`: `Jev/Newsletters` → `Jev/Spam` _(this email only)_"
    assert line in text.split("\n")


def test_corrections_outside_the_window_or_none_add_no_section(tmp_path, cfg):
    old = _corrected("c1", "a@b.example", "newsletter", "spam")
    old.correction_checked_at = now() - timedelta(hours=25)
    text = _build_corrections(tmp_path, cfg, _account(DigestLanguage.EN), [old])
    assert "Corrections learned" not in text


def test_is_pending():
    m = _row("m", "x")
    mine = ThreadMessage("m", (INBOX,), 100)
    pending = partial(digest.is_pending, m)
    assert pending([mine])
    assert pending([ThreadMessage("s", (SENT,), 99), mine])
    assert pending([mine, ThreadMessage("s", (SENT,), 100)])  # same time: not after
    assert not pending([mine, ThreadMessage("s", (SENT,), 101)])
    assert not pending([ThreadMessage("m", (), 100)])  # archived
    assert not pending([ThreadMessage("other", (INBOX,), 100)])  # deleted
    assert not pending(None)  # thread deleted


def test_is_left_for_review_needs_the_review_label_unless_dry_run():
    labeled = ThreadMessage("m", (INBOX, "Label_R"), 100)
    unlabeled = ThreadMessage("m", (INBOX,), 100)
    low = _row("m", "x", low_confidence=True)
    assert digest.is_left_for_review(low, [labeled], review_label_id="Label_R")
    assert not digest.is_left_for_review(low, [unlabeled], review_label_id="Label_R")
    assert not digest.is_left_for_review(low, [labeled], review_label_id=None)  # label deleted
    assert not digest.is_left_for_review(
        low, [ThreadMessage("m", ("Label_R",), 100)], review_label_id="Label_R"
    )  # archived
    assert digest.is_pending(low, [unlabeled])  # the label doesn't matter to the thread check
    dry = _row("m", "x", low_confidence=True, mode="dry_run")
    assert digest.is_left_for_review(dry, [unlabeled], review_label_id=None)


def _cli_digest(tmp_path, monkeypatch, connect: Callable[[Path, str], FakeGmail]) -> Result:
    """`inbox-jev digest --dry-print` for one account with one pending "Approve payment" email,
    opening Gmail with `connect`."""
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text("accounts:\n  - email: me@acme.com\n")
    settings = _cli_settings(tmp_path, monkeypatch, accounts)
    with open_sessions(settings.database_url) as sessions, sessions() as s:
        s.add(_row("1", "Approve payment", category="action_required"))
        s.commit()
    monkeypatch.setattr(cli.gmail_mod, "connect", connect)
    result = CliRunner().invoke(cli.app, ["digest", "--dry-print"])
    assert result.exit_code == 0, result.output
    return result


def test_cli_digest_reads_gmail_and_closes_it(tmp_path, monkeypatch):
    clients: list[FakeGmail] = []

    def connect(tokens_dir: Path, email_address: str) -> FakeGmail:
        clients.append(FakeGmail([email("1", "Bank <b@bank.com>", "Approve payment")]))
        return clients[-1]

    result = _cli_digest(tmp_path, monkeypatch, connect)
    assert "Approve payment" in result.output
    (client,) = clients
    assert (client.thread_batches, client.closes) == ([["t1"]], 1)


def test_cli_digest_is_sent_when_gmail_cannot_be_opened(tmp_path, monkeypatch, caplog):
    def connect(tokens_dir: Path, email_address: str) -> FakeGmail:
        raise FileNotFoundError("No token for me@acme.com")

    result = _cli_digest(tmp_path, monkeypatch, connect)
    assert "Approve payment" in result.output
    assert digest.MESSAGES[DigestLanguage.EN]["pending_check_failed"] in result.output
    (warning,) = [r for r in caplog.records if r.levelname == "WARNING"]
    assert (warning.account, warning.error_type) == ("me@acme.com", "FileNotFoundError")


def _two_accounts(tmp_path, monkeypatch, *, slack_user: bool = True) -> None:
    """me@acme.com (Slack U1 when `slack_user`), and another account, U2, never previewed."""
    accounts = tmp_path / "accounts.yaml"
    user = "    slack_user_id: U1\n" if slack_user else ""
    other = "  - email: other@acme.com\n    slack_user_id: U2\n"
    accounts.write_text("accounts:\n  - email: me@acme.com\n" + user + other)
    _cli_settings(tmp_path, monkeypatch, accounts, slack_bot_token="xoxb-test")
    monkeypatch.setattr(cli.gmail_mod, "connect", lambda tokens_dir, email: FakeGmail([]))


def test_cli_dry_print_blocks_prints_block_kit_json(tmp_path, monkeypatch):
    _two_accounts(tmp_path, monkeypatch)
    result = CliRunner().invoke(cli.app, ["digest", "--dry-print", "--blocks", "-a", "me@acme.com"])
    assert result.exit_code == 0, result.output
    blocks = json.loads(result.output)["blocks"]
    assert_valid_block_kit(blocks)
    assert blocks[0]["type"] == "header"


@pytest.mark.parametrize(
    ("args", "slack_user"),
    [
        (["--blocks"], True),  # --blocks only prints
        (["--preview"], True),  # which account's DM?
        (["--preview", "-a", ""], True),  # an empty one would mean every account
        (["--preview", "--dry-print", "-a", "me@acme.com"], True),
        (["--preview", "-a", "me@acme.com"], False),  # nowhere to send it
    ],
)
def test_cli_digest_refuses_flags_that_dont_go_together(tmp_path, monkeypatch, args, slack_user):
    _two_accounts(tmp_path, monkeypatch, slack_user=slack_user)
    opened = []
    monkeypatch.setattr(cli.slack_mod, "client", lambda: opened.append(True))
    result = CliRunner().invoke(cli.app, ["digest", *args])
    assert result.exit_code == 2, result.output
    assert opened == []


def test_cli_preview_sends_one_marked_digest_to_the_account_s_own_dm(tmp_path, monkeypatch):
    _two_accounts(tmp_path, monkeypatch)
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        cli.slack_mod, "client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    result = CliRunner().invoke(cli.app, ["digest", "--preview", "-a", "me@acme.com"])
    assert result.exit_code == 0, result.output
    (message,) = posted
    assert message["channel"] == "U1"
    assert message["text"].startswith("[preview] Inbox zero")
    assert message["blocks"][0]["text"]["text"].startswith("[preview] 📬 Inbox Jev · ")


def _failed_unsubscribe(list_key: str, http_status: int | None, error: str | None) -> Unsubscribe:
    return Unsubscribe(
        account=ACCOUNT,
        list_key=list_key,
        sender=f"news@{list_key}",
        gmail_id=list_key,
        method=UnsubscribeMethod.ONE_CLICK,
        status=UnsubscribeStatus.FAILED,
        http_status=http_status,
        error=error,
    )


def test_failed_unsubscribes_show_a_readable_reason_never_a_stored_body():
    """Rows stored before the error was `HTTP <status> <reason>` hold the server's raw body
    (sender-controlled JSON or HTML): the digest shows the status instead. A readable error, and
    one without a status (a connection or mailto failure), is shown as stored, escaped."""
    legacy_json = (
        '{"statusCode":415,"code":"FST_ERR_CTP_INVALID_MEDIA_TYPE","error":"Unsupported Media '
        'Type","message":"Unsupported Media Type: application/x-www-form-urlencoded"}'
    )
    rows = [
        _failed_unsubscribe("a.example", 415, legacy_json),
        _failed_unsubscribe("b.example", 500, "<html><body><!channel> gone</body></html>"),
        _failed_unsubscribe("c.example", 499, ""),
        _failed_unsubscribe("d.example", 400, "HTTP 400 Bad Request: token <!here> expired"),
        _failed_unsubscribe("e.example", None, "ConnectError: refused <x>"),
    ]
    (group,) = digest._unsubscribe_sections(digest.MESSAGES[DigestLanguage.EN], rows)
    shown = "\n".join(
        [
            "*❗ Unsubscribe failed (5)*",
            "• a.example: HTTP 415 Unsupported Media Type",
            "• b.example: HTTP 500 Internal Server Error",
            "• c.example: HTTP 499",
            "• d.example: HTTP 400 Bad Request: token &lt;!here&gt; expired",
            "• e.example: ConnectError: refused &lt;x&gt;",
        ]
    )
    wording = slack.Wording(more="+{n} more", see_all="see all", nothing="—")
    rendered = slack.Digest("t", "c", "h", "n", wording, unsubscribes=[group])
    assert shown in rendered.text
    assert shown in [b["text"]["text"] for b in rendered.blocks() if b["type"] == "section"]


def test_unsubscribe_groups_count_what_they_leave_out():
    rows = [_failed_unsubscribe(f"l{i}.example", 500, None) for i in range(digest.MAX_ITEMS + 3)]
    (group,) = digest._unsubscribe_sections(digest.MESSAGES[DigestLanguage.EN], rows)
    assert group.title == f"*❗ Unsubscribe failed ({digest.MAX_ITEMS + 3})*"
    assert (len(group.lines), group.hidden) == (digest.MAX_ITEMS, 3)
    assert digest._unsubscribe_sections(digest.MESSAGES[DigestLanguage.EN], []) == []


# --- A Gmail authorization Google will end ----------------------------------------------------


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            DigestLanguage.EN,
            ("*⚠️ Gmail access ends ", "(in 3 days)", "time-limited grant", "Publish app"),
        ),
        (
            DigestLanguage.ES,
            ("*⚠️ El acceso a Gmail termina el ", "(en 3 días)", "tiempo limitado", "Publish app"),
        ),
    ],
)
def test_an_ending_authorization_is_the_first_thing_the_digest_says(
    tmp_path, cfg, language, expected
):
    expires = now() + timedelta(days=2, hours=20)
    with open_sessions(f"sqlite:///{tmp_path / 'digest.db'}") as sessions, sessions() as s:
        built = digest.build(
            s, cfg, _account(language), gmail=FakeGmail([]), authorization_expires=expires
        )
    [alert] = built.alerts
    assert all(part in alert for part in expected)
    assert f"`inbox-jev auth add {ACCOUNT}`" in alert
    assert "https://console.cloud.google.com/auth/audience" in alert
    assert "7 days" not in alert and "7 días" not in alert  # the date and days left say it
    paragraphs = built.text.split("\n\n")
    assert paragraphs[1] == alert  # right under the title and context, before the headline
    blocks = built.blocks()
    assert (blocks[2]["type"], blocks[2]["text"]["text"]) == ("section", alert)
    assert_valid_block_kit(blocks)


def test_an_ended_authorization_says_so(tmp_path, cfg):
    with open_sessions(f"sqlite:///{tmp_path / 'digest.db'}") as sessions, sessions() as s:
        built = digest.build(
            s,
            cfg,
            _account(DigestLanguage.EN),
            gmail=FakeGmail([]),
            authorization_expires=now() - timedelta(hours=1),
        )
    assert "(already ended)" in built.alerts[0]


def test_an_authorization_with_no_end_has_no_alert(tmp_path, cfg):
    with open_sessions(f"sqlite:///{tmp_path / 'digest.db'}") as sessions, sessions() as s:
        built = digest.build(s, cfg, _account(DigestLanguage.EN), gmail=FakeGmail([]))
    assert built.alerts == []
    assert "⚠️ Gmail access" not in built.text


def test_cli_digest_reads_when_each_authorization_ends(tmp_path, monkeypatch):
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text("accounts:\n  - email: a@acme.com\n  - email: b@acme.com\n")
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    ends = (now() + timedelta(days=5)).isoformat()
    (tokens / "a@acme.com.json").write_text(json.dumps({"refresh_token_expires_at": ends}))
    _cli_settings(tmp_path, monkeypatch, accounts, google_tokens_dir=tokens)
    result = CliRunner().invoke(cli.app, ["digest", "--dry-print"])
    assert result.exit_code == 0, result.output
    first, second = result.output.split("*📬 Inbox Jev · ")[1:]
    assert "⚠️ Gmail access ends" in first and "(in 5 days)" in first
    assert "⚠️ Gmail access" not in second
