import base64
import time
from datetime import UTC, datetime, timedelta

import pytest
from fakes import SENT_BASE_S, HttpError, SentMailService, sent_mail_gmail

from inbox_jev import gmail as gmail_module
from inbox_jev.classifier import CATEGORY_Q, NEEDS_REPLY_Q, TAG_PREFIX, build_questions, build_state
from inbox_jev.gmail import (
    BATCH_SIZE,
    NEVER_VERDICT_TTL,
    SENT_ADDRESS_READS,
    SENT_DOMAIN_READS,
    SENT_NEWER_LIMIT,
    SINGLE_RETRIES,
    DomainScan,
    DomainVerdict,
    Email,
    Gmail,
    MemoryScans,
    extract_body,
    is_not_found,
    scan_rules,
)


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def test_questions_follow_categories_yaml(cfg):
    q = build_questions(cfg)
    assert set(q) == {CATEGORY_Q, NEEDS_REPLY_Q, TAG_PREFIX + "invoice_to_pay"}
    assert list(q[CATEGORY_Q].criteria) == [c.key for c in cfg.categories]


def test_extract_body_prefers_plain():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64("Hola  mundo")}},
            {"mimeType": "text/html", "body": {"data": b64("<p>HTML</p>")}},
        ],
    }
    assert extract_body(payload) == "Hola mundo"


def test_extract_body_html_fallback_strips_tags_and_styles():
    html = (
        "<html><head><style>p{}</style></head>"
        "<body><p>Big &amp; sale</p><script>x()</script></body></html>"
    )
    payload = {"mimeType": "text/html", "body": {"data": b64(html)}}
    assert extract_body(payload) == "Big & sale"


def test_build_state_truncates_body():
    email = Email(
        id="1",
        thread_id="t",
        label_ids=["INBOX"],
        headers={"from": ["A <a@b.com>"], "subject": ["Hi"], "list-id": ["<l.b.com>"]},
        snippet="snip",
        body="x" * 5000,
        internal_date_ms=0,
    )
    state = build_state(email, "me@acme.com", 100)
    assert len(state["body"]) == 100
    assert state["is_mailing_list"] is True
    assert state["subject"] == "Hi"
    assert "previous_contact" not in state
    assert build_state(email, "me@acme.com", 100, "never")["previous_contact"] == "never"


def test_classifier_parses_api_response(cfg):
    import json

    import httpx2
    from typesafe_sdk import TypeSafeClient

    from inbox_jev.classifier import Classifier

    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "model": "jev-test",
                "answers": {
                    "category": {
                        "type": "choice",
                        "choice": "newsletter",
                        "confidence": 0.91,
                        "probabilities": {c.key: 0.0 for c in cfg.categories}
                        | {"newsletter": 0.91},
                    },
                    "needs_reply": {"type": "noul", "noul": 0.05},
                    "tag_invoice_to_pay": {"type": "noul", "noul": 0.02},
                },
                "usage": {"input_tokens": 10, "output_tokens": 3},
            },
        )

    clf = Classifier("key", "jev-latest", cfg)
    clf._client = TypeSafeClient(
        api_key="key", model="jev-latest", transport=httpx2.MockTransport(handler)
    )
    result = clf.classify({"subject": "Weekly digest", "body": "..."})
    assert (result.category, result.confidence, result.needs_reply) == ("newsletter", 0.91, 0.05)
    assert result.tags == {"invoice_to_pay": 0.02}
    body = seen[0]
    assert body["state"]["subject"] == "Weekly digest"
    assert body["questions"]["category"]["type"] == "choice"
    assert set(body["questions"]["category"]["criteria"]) == set(cfg.by_key)


def _alternative(plain: str, html: str) -> dict:
    return {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64(plain)}},
            {"mimeType": "text/html", "body": {"data": b64(html)}},
        ],
    }


def test_extract_body_skips_plain_stub_for_html():
    # emBlue-style: the plain part only says to open the HTML version in a browser.
    stub = (
        "Su Cliente de Mail NO soporta mensajes en formato HTML.\n"
        "COPIE y PEGUE la siguiente URL en su Navegador Web (Chrome / Internet Explorer / "
        "FireFox / Safari)\nhttps://app.example.com/OnlineV3/VON.aspx?data=" + "x" * 300
    )
    content = "Te invitamos al encuentro de socios del 21 de octubre. " * 10
    assert extract_body(_alternative(stub, f"<p>{content}</p>")) == content.strip()


def test_extract_body_keeps_short_plain_when_html_is_similar():
    # A short real reply whose HTML version just adds markup and a signature.
    plain = "Dale, nos vemos el martes a las 10."
    html = f"<div>{plain}</div><div>-- <br>Guille</div>"
    assert extract_body(_alternative(plain, html)) == plain


def test_extract_body_keeps_long_plain_even_with_longer_html():
    plain = "Contenido real del mail. " * 20
    html = "<p>" + "Contenido real del mail con mucho más formato. " * 40 + "</p>"
    assert extract_body(_alternative(plain, html)) == plain.strip()


def test_extract_body_normalizes_nbsp_and_zwnj():
    payload = {"mimeType": "text/plain", "body": {"data": b64("a\u00a0\u200c b  \n\nc")}}
    assert extract_body(payload) == "a b\nc"


def test_extract_body_html_only_link_does_not_beat_plain():
    # HTML that is mostly one long tracking URL is not "more content" than the plain text.
    plain = "Tu pedido salió hoy y llega el jueves."
    html = f"<p>{plain} <a>https://track.example.com/{'y' * 400}</a></p>"
    assert extract_body(_alternative(plain, html)) == plain


EMBLUE_STUB = (
    "Su Cliente de Mail NO soporta mensajes en formato HTML.\n"
    "COPIE y PEGUE la siguiente URL en su Navegador Web\n"
    "https://app.example.com/OnlineV3/VON.aspx?data=" + "x" * 300
)
NEWSLETTER = "Gran oferta de verano 50% off en toda la tienda, solo por esta semana. " * 8


def test_extract_body_stub_check_works_inside_mixed_with_attachment():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            _alternative(EMBLUE_STUB, f"<p>{NEWSLETTER}</p>"),
            {"mimeType": "application/pdf", "filename": "agenda.pdf", "body": {"size": 10}},
        ],
    }
    assert extract_body(payload) == NEWSLETTER.strip()


def test_extract_body_keeps_note_above_forwarded_html_message():
    # The user's own short note must not be dropped in favour of a forwarded HTML newsletter.
    note = "Mirá esto, ¿lo vemos el lunes? Es para el cliente X."
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64(note)}},
            {
                "mimeType": "message/rfc822",
                "parts": [{"mimeType": "text/html", "body": {"data": b64(f"<p>{NEWSLETTER}</p>")}}],
            },
        ],
    }
    assert extract_body(payload) == note


def test_extract_body_keeps_note_next_to_inline_html_part():
    note = "Te paso lo que mandaron, fijate."
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64(note)}},
            {"mimeType": "text/html", "body": {"data": b64(f"<p>{NEWSLETTER}</p>")}},
        ],
    }
    assert extract_body(payload) == note


def test_extract_body_url_only_plain_beats_empty_or_trivial_html():
    plain = "https://zoom.us/j/123456789?pwd=abc"
    assert extract_body(_alternative(plain, "<img src=x>")) == plain
    assert extract_body(_alternative(plain, f"<a href='{plain}'>Unirse</a>")) == plain


def test_extract_body_normalize_is_linear_on_long_whitespace_runs():
    # U+2007 figure space isn't collapsed by the first pass; `\s+\n` used to be quadratic on it.
    html = "<p>pre</p>" + " " * 200_000 + "\n<p>" + NEWSLETTER + "</p>"
    start = time.perf_counter()
    body = extract_body(_alternative("undefined", html))
    assert time.perf_counter() - start < 2
    assert body.startswith("pre") and body.endswith(NEWSLETTER.strip())


def test_is_not_found_only_for_http_404():
    assert is_not_found(HttpError(404))
    assert not is_not_found(HttpError(403))
    assert not is_not_found(OSError("connection reset"))


class _FakeGmailService:
    """Stands in for googleapiclient's Gmail service: messages().list/get and batch requests.

    `batch_errors` / `single_errors` map a message id to the exception its call fails with inside
    a batch / on its own; any other call answers with `labels[id]` (default: no labels).
    `batch_raises` maps a batch's index to `(answered, exc)`: that batch request answers its first
    `answered` calls, then `execute()` raises `exc`, as googleapiclient does when the batch
    request as a whole fails.
    """

    def __init__(
        self, labels=None, batch_errors=None, single_errors=None, batch_raises=None, pages=None
    ):
        self.labels = labels or {}
        self.batch_errors = batch_errors or {}
        self.single_errors = single_errors or {}
        self.batch_raises = batch_raises or {}
        self.pages = pages or []
        self.batches: list[list[str]] = []
        self.singles: list[str] = []
        self.single_retries: list[int] = []
        self.list_calls: list[dict] = []

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, userId, id, format):
        assert (userId, format) == ("me", "minimal")
        return _FakeGet(self, id)

    def list(self, **kw):
        self.list_calls.append(kw)
        page = self.pages.pop(0)
        return type("Req", (), {"execute": lambda self: page})()

    def new_batch_http_request(self, callback):
        return _FakeBatch(self, callback)

    def answer(self, message_id):
        return {"id": message_id, "labelIds": self.labels.get(message_id, [])}


class _FakeGet:
    def __init__(self, svc, message_id):
        self.svc, self.id = svc, message_id

    def execute(self, num_retries=0):
        self.svc.singles.append(self.id)
        self.svc.single_retries.append(num_retries)
        if self.id in self.svc.single_errors:
            raise self.svc.single_errors[self.id]
        return self.svc.answer(self.id)


class _FakeBatch:
    def __init__(self, svc, callback):
        self.svc, self.callback, self.ids = svc, callback, []

    def add(self, request, request_id):
        assert request.id == request_id and request_id not in self.ids  # googleapiclient rejects it
        self.ids.append(request_id)

    def execute(self):
        answered, raised = self.svc.batch_raises.get(len(self.svc.batches), (len(self.ids), None))
        self.svc.batches.append(self.ids)
        for i in self.ids[:answered]:
            exc = self.svc.batch_errors.get(i)
            self.callback(i, None if exc else self.svc.answer(i), exc)
        if raised is not None:
            raise raised


def _gmail(monkeypatch, svc) -> Gmail:
    monkeypatch.setattr(gmail_module, "build", lambda *args, **kwargs: svc)
    return Gmail(creds=None)


def test_get_label_ids_batches_calls_and_maps_deleted_emails_to_none(monkeypatch):
    ids = [f"m{i}" for i in range(120)]
    svc = _FakeGmailService(
        labels={"m7": ["INBOX", "Label_1"]}, batch_errors={"m60": HttpError(404)}
    )
    result = _gmail(monkeypatch, svc).get_label_ids([*ids, "m7"])  # a repeat is asked once
    assert [len(b) for b in svc.batches] == [BATCH_SIZE, BATCH_SIZE, 20]
    assert [i for b in svc.batches for i in b] == ids
    assert svc.singles == []  # a 404 is an answer, not a failure
    assert result["m7"] == ["INBOX", "Label_1"]
    assert result["m60"] is None  # deleted in Gmail
    assert result["m0"] == [] and len(result) == 120


def test_get_label_ids_asks_failed_batch_calls_again_one_by_one(monkeypatch, caplog):
    svc = _FakeGmailService(
        labels={"m3": ["INBOX"]},
        batch_errors={"m3": HttpError(429), "m1": HttpError(503)},
        single_errors={"m1": HttpError(404)},
    )
    result = _gmail(monkeypatch, svc).get_label_ids(["m0", "m1", "m2", "m3"])
    assert svc.singles == ["m1", "m3"]
    assert svc.single_retries == [SINGLE_RETRIES, SINGLE_RETRIES]  # backoff on 429 / 5xx
    assert result == {"m0": [], "m1": None, "m2": [], "m3": ["INBOX"]}
    (warning,) = [r for r in caplog.records if r.levelname == "WARNING"]
    assert (warning.failed, warning.error_types) == (2, ["HttpError"])


def test_get_label_ids_raises_when_the_single_retry_fails_too(monkeypatch):
    ids = [f"m{i}" for i in range(120)]
    svc = _FakeGmailService(
        batch_errors={"m70": HttpError(503), "m55": HttpError(429)},
        single_errors={"m70": HttpError(500), "m55": HttpError(429)},
    )
    with pytest.raises(HttpError) as raised:
        _gmail(monkeypatch, svc).get_label_ids(ids)
    assert raised.value.resp.status == 429  # m55: the first failure in id order
    assert svc.singles == ["m55"]
    assert len(svc.batches) == 2  # the third batch is never sent


@pytest.mark.parametrize("error", [HttpError(503), KeyError("response-7"), OSError("reset")])
def test_get_label_ids_asks_again_one_by_one_when_the_whole_batch_fails(monkeypatch, caplog, error):
    ids = [f"m{i}" for i in range(60)]
    svc = _FakeGmailService(
        labels={"m55": ["INBOX"]},
        batch_errors={"m2": HttpError(429)},
        batch_raises={0: (5, error)},  # the first batch answers m0..m4, then fails as a whole
    )
    result = _gmail(monkeypatch, svc).get_label_ids(ids)
    assert len(svc.batches) == 2  # the next batch is still sent
    assert svc.singles == ["m2", *ids[5:BATCH_SIZE]]  # only the calls left without an answer
    assert result == {i: ["INBOX"] if i == "m55" else [] for i in ids}
    (warning,) = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warning.failed == BATCH_SIZE - 4
    assert warning.error_types == sorted({"HttpError", type(error).__name__})


def test_get_label_ids_raises_when_the_whole_batch_and_the_single_retry_fail(monkeypatch):
    ids = [f"m{i}" for i in range(60)]
    svc = _FakeGmailService(
        batch_raises={0: (0, HttpError(429))}, single_errors={"m0": HttpError(403)}
    )
    with pytest.raises(HttpError) as raised:
        _gmail(monkeypatch, svc).get_label_ids(ids)
    assert raised.value.resp.status == 403  # e.g. an auth error isn't hidden by the fallback
    assert svc.singles == ["m0"]
    assert len(svc.batches) == 1


def test_get_label_ids_without_ids_sends_nothing(monkeypatch):
    svc = _FakeGmailService()
    assert _gmail(monkeypatch, svc).get_label_ids([]) == {}
    assert svc.batches == []


def test_list_inbox_ids_and_list_ids_share_paging(monkeypatch):
    svc = _FakeGmailService(
        pages=[
            {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "t"},
            {"messages": [{"id": "c"}]},
            {"messages": [{"id": "x"}]},
        ]
    )
    gmail = _gmail(monkeypatch, svc)
    assert gmail.list_inbox_ids("2d", 3) == ["a", "b", "c"]
    assert gmail.list_ids("from:shop.com", 5) == ["x"]
    first, second, third = svc.list_calls
    assert (first["labelIds"], first["q"], first["maxResults"]) == (["INBOX"], "newer_than:2d", 3)
    assert (second["pageToken"], second["maxResults"]) == ("t", 1)
    assert (third["labelIds"], third["q"]) == (None, "from:shop.com")


LINKEDIN_REPLIES = {f"s{i}": {"To": f"m-a17{i}@bounce.linkedin.com"} for i in range(12)}


def test_has_written_to_address_reads_the_newest_match(monkeypatch):
    svc = SentMailService({"s1": {"To": "Jane <jane@corp.com>"}})
    gmail = sent_mail_gmail(monkeypatch, svc)
    assert gmail.has_written_to("jane@corp.com")
    assert not gmail.has_written_to("bob@corp.com")
    assert svc.queries == ["in:sent to:jane@corp.com", "in:sent to:bob@corp.com"]
    assert svc.reads == ["s1"]  # bob@ has no match: nothing to read
    assert svc.retries == [SINGLE_RETRIES] * 3


def test_has_written_to_address_ignores_a_loose_match(monkeypatch):
    # Gmail's to:ana@corp.com also matched diana@corp.com: the recipients say who it went to.
    svc = SentMailService({"s0": {"To": "Diana <diana@corp.com>"}, "s1": {"Cc": "ANA@Corp.com"}})
    assert sent_mail_gmail(monkeypatch, svc).has_written_to("ana@corp.com")
    assert svc.reads == ["s0", "s1"]
    svc = SentMailService({"s0": {"To": "Diana <diana@corp.com>"}})
    assert not sent_mail_gmail(monkeypatch, svc).has_written_to("ana@corp.com")


@pytest.mark.parametrize(
    ("sent_to", "sender", "same"),
    [
        ("Jane.Doe+news@gmail.com", "janedoe@gmail.com", True),
        ("janedoe@googlemail.com", "jane.doe@gmail.com", True),
        ("jane+sales@corp.com", "jane@corp.com", False),  # Workspace or not: compared as written
        ("j.ane@corp.com", "jane@corp.com", False),
    ],
)
def test_has_written_to_address_applies_gmails_own_address_rules_only(
    monkeypatch, sent_to, sender, same
):
    svc = SentMailService({"s0": {"Bcc": sent_to}})
    svc.list = _match_everything(svc.list)
    assert sent_mail_gmail(monkeypatch, svc).has_written_to(sender) is same


def _match_everything(list_call):
    """A `SentMailService.list` whose `to:` matches every sent email (Gmail's own notion)."""
    return lambda userId, q, maxResults, **kw: list_call(userId, "in:sent to:@", maxResults, **kw)


def test_has_written_to_address_with_more_loose_matches_than_it_reads_counts(monkeypatch, caplog):
    caplog.set_level("INFO", logger="inbox_jev.gmail")
    sent = {f"s{i}": {"To": f"x{i}ana@corp.com"} for i in range(SENT_ADDRESS_READS + 1)}
    svc = SentMailService(sent)
    assert sent_mail_gmail(monkeypatch, svc).has_written_to("ana@corp.com")
    assert len(svc.reads) == SENT_ADDRESS_READS
    assert "sent-mail address check inconclusive" in caplog.text


def test_has_written_to_address_skips_deleted_matches_and_counts_hidden_recipients(monkeypatch):
    svc = SentMailService({"s0": {"To": "ana@corp.com"}}, get_errors={"s0": HttpError(404)})
    assert not sent_mail_gmail(monkeypatch, svc).has_written_to("ana@corp.com")
    # No recipient in the headers at all: Gmail matched something they don't show.
    svc = SentMailService({"s0": {"To": "undisclosed-recipients:;", "X": "ana@corp.com"}})
    assert sent_mail_gmail(monkeypatch, svc).has_written_to("ana@corp.com")


def test_has_written_to_domain_ignores_sends_to_machine_addresses(monkeypatch):
    # The LinkedIn case: ~12 replies to notifications, all to bounce.linkedin.com.
    svc = SentMailService(LINKEDIN_REPLIES)
    gmail = sent_mail_gmail(monkeypatch, svc)
    assert not gmail.has_written_to("linkedin.com")
    assert svc.reads == list(LINKEDIN_REPLIES)  # each read once, metadata only
    assert not gmail.has_written_to("linkedin.com")  # cached: no new calls
    assert svc.queries == ["in:sent to:linkedin.com"]
    # The list and every read rely on googleapiclient's backoff for 429/5xx.
    assert svc.retries == [SINGLE_RETRIES] * (1 + len(LINKEDIN_REPLIES))


def test_has_written_to_counts_a_person_at_a_company_named_reply_under_a_cc_tld(monkeypatch):
    svc = SentMailService({"s0": {"To": "Ana <ana@reply.com.ar>"}})
    assert sent_mail_gmail(monkeypatch, svc).has_written_to("reply.com.ar")


def test_has_written_to_domain_stops_at_the_first_person(monkeypatch):
    sent = {
        "s0": {"To": "m-1@bounce.linkedin.com"},
        "s1": {"To": "x@y.com", "Cc": "jane@linkedin.com"},
    }
    sent |= {f"s{i}": {"To": "jane@linkedin.com"} for i in range(2, 9)}
    svc = SentMailService(sent)
    assert sent_mail_gmail(monkeypatch, svc).has_written_to("linkedin.com")
    assert svc.reads == ["s0", "s1"]


GITHUB_REPLIES = {f"g{i}": {"To": f"reply+{i}@reply.github.com"} for i in range(60)}


def _check(monkeypatch, svc, scans, domain="github.com"):
    """One run's domain check: a new `Gmail` (its own cache) on the same stored scans."""
    svc.queries, svc.reads = [], []
    return sent_mail_gmail(monkeypatch, svc).has_written_to(domain, scans)


def test_domain_scan_goes_on_across_runs_until_every_match_is_read(monkeypatch, caplog):
    caplog.set_level("INFO", logger="inbox_jev.gmail")
    svc, scans = SentMailService(dict(GITHUB_REPLIES)), MemoryScans()
    assert _check(monkeypatch, svc, scans)  # 25 of 60 read: counts as written for now
    assert svc.reads == [f"g{i}" for i in range(SENT_DOMAIN_READS)]
    assert "sent-mail domain scan not finished" in caplog.text
    scan = scans.load("github.com")
    assert scan is not None
    assert (scan.verdict, scan.read, scan.reached_end) == (DomainVerdict.SCANNING, 25, False)
    assert (scan.newest_s, scan.oldest_s) == (SENT_BASE_S, SENT_BASE_S - 24 * 60)

    assert _check(monkeypatch, svc, scans)  # the next 25, older ones
    assert svc.queries == [
        f"in:sent to:github.com after:{SENT_BASE_S - 1}",  # nothing new since
        f"in:sent to:github.com before:{SENT_BASE_S - 24 * 60 + 1}",
    ]
    assert svc.reads == [f"g{i}" for i in range(25, 50)]

    assert not _check(monkeypatch, svc, scans)  # the last 10: never wrote to a person there
    assert svc.reads == [f"g{i}" for i in range(50, 60)]
    scan = scans.load("github.com")
    assert scan is not None and scan.verdict is DomainVerdict.NEVER and scan.verdict_at
    assert (scan.read, scan.reached_end) == (60, True)

    assert not _check(monkeypatch, svc, scans)  # done: one search for newer mail, no read
    assert (len(svc.queries), svc.reads) == (1, [])


def test_domain_scan_reads_new_sent_mail_since_it_finished(monkeypatch):
    svc, scans = SentMailService(dict(GITHUB_REPLIES)), MemoryScans()
    while _check(monkeypatch, svc, scans):
        pass
    finished = scans.load("github.com")
    assert finished is not None and finished.verdict is DomainVerdict.NEVER
    # Another reply to a notification: read, still never.
    svc.sent = {"n1": {"To": "reply+n1@reply.github.com"}, **svc.sent}
    svc.dates["n1"] = SENT_BASE_S + 3600
    assert not _check(monkeypatch, svc, scans)
    assert svc.reads == ["n1"]
    assert scans.load("github.com").verdict_at == finished.verdict_at  # type: ignore[union-attr]
    # Then an email to a person there: known from now on, without asking Gmail again.
    svc.sent = {"n2": {"To": "Octo <octo@github.com>"}, **svc.sent}
    svc.dates["n2"] = SENT_BASE_S + 7200
    assert _check(monkeypatch, svc, scans)
    assert svc.reads == ["n2"]
    assert scans.load("github.com").verdict is DomainVerdict.PERSON  # type: ignore[union-attr]
    assert _check(monkeypatch, svc, scans)
    assert (svc.queries, svc.reads) == ([], [])


def test_domain_scan_reads_new_mail_oldest_first_within_the_budget(monkeypatch):
    svc, scans = SentMailService({"g0": {"To": "reply+0@reply.github.com"}}), MemoryScans()
    assert not _check(monkeypatch, svc, scans)
    new = {f"n{i}": {"To": f"reply+n{i}@reply.github.com"} for i in range(30)}
    svc.sent = new | svc.sent
    svc.dates |= {f"n{i}": SENT_BASE_S + 60 * (30 - i) for i in range(30)}
    assert _check(monkeypatch, svc, scans)  # 25 of 30 new ones read: not done
    assert svc.reads == [f"n{i}" for i in range(29, 4, -1)]
    assert scans.load("github.com").verdict is DomainVerdict.SCANNING  # type: ignore[union-attr]
    assert not _check(monkeypatch, svc, scans)
    assert svc.reads == [f"n{i}" for i in range(4, -1, -1)]


def test_domain_scan_keeps_emails_sent_in_the_same_second_apart(monkeypatch):
    # 30 replies sent in one second: the boundary second is listed again and its emails skipped.
    svc = SentMailService(
        dict(list(GITHUB_REPLIES.items())[:30]), dates=dict.fromkeys(GITHUB_REPLIES, SENT_BASE_S)
    )
    scans = MemoryScans()
    assert _check(monkeypatch, svc, scans)
    assert not _check(monkeypatch, svc, scans)
    assert sorted(svc.reads) == [f"g{i}" for i in range(25, 30)]  # each of the rest, once
    assert scans.load("github.com").read == 30  # type: ignore[union-attr]


def test_domain_scan_with_too_much_new_mail_counts_as_written(monkeypatch):
    svc, scans = SentMailService({"g0": {"To": "reply+0@reply.github.com"}}), MemoryScans()
    assert not _check(monkeypatch, svc, scans)
    svc.sent = {
        f"n{i}": {"To": "reply+n@reply.github.com"} for i in range(SENT_NEWER_LIMIT + 1)
    } | (svc.sent)
    svc.dates |= dict.fromkeys((f"n{i}" for i in range(SENT_NEWER_LIMIT + 1)), SENT_BASE_S + 60)
    assert _check(monkeypatch, svc, scans)
    assert svc.reads == []


def test_domain_scan_made_under_other_rules_starts_over(monkeypatch):
    scans = MemoryScans()
    scans.save(DomainScan("github.com", "0-old", DomainVerdict.PERSON))
    svc = SentMailService({"g0": {"To": "reply+0@reply.github.com"}})
    assert not _check(monkeypatch, svc, scans)
    assert svc.queries == ["in:sent to:github.com"]
    assert scans.load("github.com").rules == scan_rules()  # type: ignore[union-attr]


def test_domain_scan_keeps_what_it_read_before_an_error(monkeypatch):
    svc = SentMailService(dict(GITHUB_REPLIES), get_errors={"g3": HttpError(500)})
    scans = MemoryScans()
    with pytest.raises(HttpError):
        _check(monkeypatch, svc, scans)
    assert scans.load("github.com").read == 3  # type: ignore[union-attr]
    svc.get_errors = {}
    assert _check(monkeypatch, svc, scans)
    assert svc.reads[0] == "g3"  # goes on from there


def test_domain_scan_without_any_match_stores_nothing(monkeypatch):
    scans = MemoryScans()
    assert not _check(monkeypatch, SentMailService({}), scans)
    assert scans.load("github.com") is None


def _age_verdict(scans, by, domain="github.com"):
    """Move the stored scan's verdict_at `by` into the past."""
    scan = scans.load(domain)
    scan.verdict_at -= by
    scans.save(scan)
    return scan


def test_domain_scan_never_starts_over_once_its_ttl_is_over(monkeypatch):
    svc = SentMailService(dict(list(GITHUB_REPLIES.items())[:10]))
    scans = MemoryScans()
    assert not _check(monkeypatch, svc, scans)
    # An old email to a person enters the sent mail inside the span already read (restored
    # from Trash, imported): the after: search can't list it.
    svc.sent["old"] = {"To": "Octo <octo@github.com>"}
    svc.dates["old"] = SENT_BASE_S - 5 * 60
    _age_verdict(scans, NEVER_VERDICT_TTL - timedelta(days=1))
    assert not _check(monkeypatch, svc, scans)  # still within the TTL: trusted
    assert (svc.queries, svc.reads) == ([f"in:sent to:github.com after:{SENT_BASE_S - 1}"], [])

    _age_verdict(scans, timedelta(days=1))  # now exactly NEVER_VERDICT_TTL old
    assert _check(monkeypatch, svc, scans)  # read again from the newest match down
    assert svc.queries == ["in:sent to:github.com"]
    assert "old" in svc.reads
    assert scans.load("github.com").verdict is DomainVerdict.PERSON  # type: ignore[union-attr]


def test_domain_scan_redone_after_its_ttl_gets_a_new_verdict_time(monkeypatch):
    svc = SentMailService(dict(list(GITHUB_REPLIES.items())[:10]))
    scans = MemoryScans()
    assert not _check(monkeypatch, svc, scans)
    old = _age_verdict(scans, NEVER_VERDICT_TTL + timedelta(days=1))
    assert not _check(monkeypatch, svc, scans)
    assert len(svc.reads) == 10
    redone = scans.load("github.com")
    assert redone is not None and redone.verdict is DomainVerdict.NEVER and redone.read == 10
    assert redone.verdict_at is not None and old.verdict_at is not None
    assert datetime.now(UTC) - redone.verdict_at < timedelta(minutes=1)


def test_domain_scan_stores_a_verdict_reached_without_a_read(monkeypatch):
    # The last check read the oldest match but stopped at the budget before seeing that nothing
    # was older; this one only lists, and must still store `never` and when it was reached.
    scans = MemoryScans()
    scans.save(
        DomainScan(
            "github.com",
            scan_rules(),
            newest_s=SENT_BASE_S,
            newest_ids=["g0"],
            oldest_s=SENT_BASE_S,
            oldest_ids=["g0"],
            reached_end=True,
            read=1,
        )
    )
    svc = SentMailService({"g0": GITHUB_REPLIES["g0"]})
    assert not _check(monkeypatch, svc, scans)
    assert svc.reads == []
    scan = scans.load("github.com")
    assert scan is not None and scan.verdict is DomainVerdict.NEVER and scan.verdict_at


def test_domain_scan_read_out_of_date_order_starts_over(monkeypatch, caplog):
    # Gmail lists C after B though C is newer: C lies inside the span B made, so a later
    # before: search would never list an email like it. The scan must not end `never`.
    caplog.set_level("WARNING", logger="inbox_jev.gmail")
    sent = {
        "a": {"To": "reply+a@reply.github.com"},
        "b": {"To": "reply+b@reply.github.com"},
        "c": {"To": "reply+c@reply.github.com"},
    }
    dates = {"a": SENT_BASE_S, "b": SENT_BASE_S - 600, "c": SENT_BASE_S - 300}
    svc, scans = SentMailService(sent, dates=dates, ordered=False), MemoryScans()
    assert _check(monkeypatch, svc, scans)  # counts as written
    assert svc.reads == ["a", "b", "c"]  # stopped at the first one out of order
    assert "out of date order: restarted" in caplog.text
    assert "github.com" not in caplog.text and "reply+" not in caplog.text  # counts only
    restarted = scans.load("github.com")
    assert restarted == DomainScan("github.com", scan_rules())  # nothing kept

    svc.ordered = True  # in order again: the next check starts from the newest match
    assert not _check(monkeypatch, svc, scans)
    assert svc.queries == ["in:sent to:github.com"]


def test_domain_scan_newer_mail_out_of_date_order_starts_over(monkeypatch):
    svc, scans = SentMailService({"g0": GITHUB_REPLIES["g0"]}), MemoryScans()
    assert not _check(monkeypatch, svc, scans)
    # Two new emails listed oldest first (reversed when read, so newest first: out of order).
    svc.sent = {"n1": {"To": "reply+1@reply.github.com"}, "n2": {"To": "reply+2@reply.github.com"}}
    svc.sent |= {"g0": GITHUB_REPLIES["g0"]}
    svc.dates |= {"n1": SENT_BASE_S + 60, "n2": SENT_BASE_S + 120}
    svc.ordered = False
    assert _check(monkeypatch, svc, scans)
    assert svc.reads == ["n2", "n1"]
    assert scans.load("github.com") == DomainScan("github.com", scan_rules())


def test_domain_scan_out_of_order_email_to_a_person_still_counts(monkeypatch):
    sent = {"a": {"To": "reply+a@reply.github.com"}, "b": {"To": "reply+b@reply.github.com"}}
    sent["c"] = {"To": "Octo <octo@github.com>"}
    dates = {"a": SENT_BASE_S, "b": SENT_BASE_S - 600, "c": SENT_BASE_S - 300}
    svc, scans = SentMailService(sent, dates=dates, ordered=False), MemoryScans()
    assert _check(monkeypatch, svc, scans)
    assert scans.load("github.com").verdict is DomainVerdict.PERSON  # type: ignore[union-attr]


def test_has_written_to_domain_skips_emails_deleted_since_the_search(monkeypatch):
    svc = SentMailService(LINKEDIN_REPLIES, get_errors={"s0": HttpError(404)})
    assert not sent_mail_gmail(monkeypatch, svc).has_written_to("linkedin.com")
    assert len(svc.reads) == len(LINKEDIN_REPLIES)


def test_has_written_to_domain_raises_other_read_errors_uncached(monkeypatch):
    svc = SentMailService(LINKEDIN_REPLIES, get_errors={"s3": HttpError(500)})
    gmail = sent_mail_gmail(monkeypatch, svc)
    with pytest.raises(HttpError):
        gmail.has_written_to("linkedin.com")
    svc.get_errors = {}
    assert not gmail.has_written_to("linkedin.com")  # asked again: a failure isn't cached
    assert svc.queries[0] == "in:sent to:linkedin.com"
    assert svc.reads == ["s0", "s1", "s2", "s3", "s3", *list(LINKEDIN_REPLIES)[4:]]


def test_has_written_to_domain_without_sent_mail(monkeypatch):
    svc = SentMailService({})
    assert not sent_mail_gmail(monkeypatch, svc).has_written_to("corp.com")
    assert svc.reads == []
