"""Digest language: every language has every text, and each renders the whole digest."""

from collections.abc import Callable
from functools import partial
from pathlib import Path
from string import Formatter

import pytest
from conftest import ROOT
from fakes import FakeGmail, HttpError, email
from sqlalchemy.orm import Session
from typer.testing import CliRunner, Result

from inbox_jev import cli, digest
from inbox_jev.config import DigestLanguage, Settings
from inbox_jev.db import (
    MAX_ERROR_ATTEMPTS,
    Message,
    MessageStatus,
    Run,
    Unsubscribe,
    now,
    open_sessions,
)
from inbox_jev.digest import plural
from inbox_jev.gmail import Email, ThreadMessage
from inbox_jev.system_labels import INBOX, SENT
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
    with open_sessions(f"sqlite:///{tmp_path / 'digest.db'}") as sessions, sessions() as s:
        _seed(s, cfg)
        text = digest.build(s, cfg, ACCOUNT, language=language, gmail=None)
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
    with open_sessions(f"sqlite:///{tmp_path / 'digest.db'}") as sessions, sessions() as s:
        _seed_counts(s, n)
        text = digest.build(s, cfg, ACCOUNT, language=language, gmail=None)
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
        return digest.build(s, cfg, ACCOUNT, language=language, gmail=gmail_for(gmail)), gmail


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            DigestLanguage.EN,
            [
                "· 4 already handled",  # archived, replied to, deleted, archived (Review)
                "*🔔 Jev/Action (1)*",
                "*⚡ Needs reply (2)*",
                "*🤔 Low confidence: 2* from the last 24h still in the inbox with Jev/Review (<",
                "|see in Gmail>)",
            ],
        ),
        (
            DigestLanguage.ES,
            [
                "· 4 ya resueltos",
                "*🔔 Jev/Action (1)*",
                "*⚡ Piden respuesta (2)*",
                "*🤔 Baja confianza: 2* de las últimas 24h siguen en el Inbox con Jev/Review (<",
                "|ver en Gmail>)",
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
        return digest.build(s, cfg, ACCOUNT, language=DigestLanguage.EN, gmail=gmail)


def test_low_confidence_without_the_review_label_is_handled(tmp_path, cfg):
    """Removing the review label (kept in the inbox) accepts or corrects the suggestion."""
    gmail = FakeGmail([email(i, "Shop <s@shop.com>", "Guess") for i in ("l1", "l2", "l3")])
    gmail.emails["l1"].label_ids.append(_review_label_id(gmail, cfg))
    rows = [_row(i, "Guess", low_confidence=True) for i in ("l1", "l2", "l3")]
    text = _build_low(tmp_path, cfg, rows, gmail)
    assert "*🤔 Low confidence: 1* from the last 24h still in the inbox with Jev/Review" in text
    assert "· 2 already handled" in text


def test_low_confidence_is_handled_when_the_review_label_is_gone(tmp_path, cfg):
    """The review label deleted from Gmail: no email can still have it."""
    gmail = FakeGmail([email("l1", "Shop <s@shop.com>", "Guess")])
    text = _build_low(tmp_path, cfg, [_row("l1", "Guess", low_confidence=True)], gmail)
    assert "Low confidence" not in text
    assert "· 1 already handled" in text


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
    assert "*⚡ Needs reply (1)*\n• <" in text
    assert "Can we talk?" in text
    assert "Low confidence" not in text
    assert "already handled" not in text


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
    assert "Low confidence" not in text
    assert "· 1 already handled" in text


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
    lines = [line for line in text.split("\n") if "Low confidence" in line]
    assert lines == [
        "*🤔 Low confidence: 1* from the last 24h still in the inbox with Jev/Review ("
        "<https://mail.google.com/mail/u/me@acme.com/#search/"
        "label%3Ajev-review%20in%3Ainbox|see in Gmail>)",
        "*🤔 Low confidence: 2* from the last 24h still in the inbox (dry run: not labeled)",
    ]
    assert "already handled" not in text


def test_review_search_link_points_at_the_review_label_in_the_inbox(tmp_path, cfg):
    text, _ = _build(tmp_path, cfg, DigestLanguage.EN, lambda g: g)
    link = (
        "<https://mail.google.com/mail/u/me@acme.com/#search/"
        "label%3Ajev-review%20in%3Ainbox|see in Gmail>"
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
    url = line.split("(<", 1)[1].split("|see in Gmail>)", 1)[0]
    assert not set(url) & set("<>|& ")  # nothing in the URL can end or break the Slack link
    assert "with A|B &lt;x&gt;&amp;y (" in line  # the label shown is escaped too


def test_unsubscribe_sections_stay_in_full(tmp_path, cfg):
    text, _ = _build(tmp_path, cfg, DigestLanguage.EN, lambda g: g)
    assert "*🚫 Unsubscribed (1)*\n• deals.shop.com (one_click)" in text
    assert (
        "*🖐 Manual unsubscribe (1)*\n• news.shop.com — "
        "<https://news.shop.com/unsub?a=1%7C2|link>" in text
    )
    assert "*❗ Unsubscribe failed (1)*\n• spam.example: HTTP 500" in text


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
    summary, note = text.split("\n")[1:3]
    assert note == t["pending_check_failed"]
    assert plural(t["handled"], 4) not in summary
    for subject in ("Approve payment", "Sign the contract", "Answered already", "Deleted since"):
        assert subject in text, subject  # listed as before the check existed
    assert t["low_confidence"].split("{n}")[0] + "3*" in text
    if failure == "raises":
        (warning,) = [r for r in caplog.records if r.levelname == "WARNING"]
        assert (warning.emails, warning.error_type) == (9, "HttpError")


def test_nothing_to_list_asks_gmail_nothing(tmp_path, cfg):
    gmail = FakeGmail([])
    with open_sessions(f"sqlite:///{tmp_path / 'digest.db'}") as sessions, sessions() as s:
        text = digest.build(s, cfg, ACCOUNT, language=DigestLanguage.EN, gmail=gmail)
        unavailable = digest.build(s, cfg, ACCOUNT, language=DigestLanguage.EN, gmail=None)
    assert gmail.thread_batches == []
    for out in (text, unavailable):
        assert "already handled" not in out
        assert digest.MESSAGES[DigestLanguage.EN]["pending_check_failed"] not in out


@pytest.mark.parametrize(
    ("language", "line"),
    [
        (DigestLanguage.EN, "*🧠 Corrections learned: 2*"),
        (DigestLanguage.ES, "*🧠 Correcciones aprendidas: 2*"),
    ],
)
def test_learned_corrections_are_a_count(tmp_path, cfg, language, line):
    with open_sessions(f"sqlite:///{tmp_path / 'digest.db'}") as sessions, sessions() as s:
        for i in range(2):
            s.add(
                _row(
                    f"c{i}",
                    "Corrected",
                    corrected_category="important",
                    correction_checked_at=now(),
                    sender="secret@sender.com",
                )
            )
        s.commit()
        text = digest.build(s, cfg, ACCOUNT, language=language, gmail=FakeGmail([]))
    assert line in text
    assert "secret@sender.com" not in text


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
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'cli.db'}",
        accounts_file=accounts,
        categories_file=ROOT / "categories.yaml",
    )
    with open_sessions(settings.database_url) as sessions, sessions() as s:
        s.add(_row("1", "Approve payment", category="action_required"))
        s.commit()
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli.logs, "setup", lambda level: None)
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
