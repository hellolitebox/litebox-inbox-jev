import base64
import time

import pytest
from test_triage import _HttpError

from inbox_jev import gmail as gmail_module
from inbox_jev.classifier import CATEGORY_Q, NEEDS_REPLY_Q, TAG_PREFIX, build_questions, build_state
from inbox_jev.gmail import (
    BATCH_SIZE,
    SINGLE_RETRIES,
    Email,
    Gmail,
    extract_body,
    is_not_found,
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
    assert is_not_found(_HttpError(404))
    assert not is_not_found(_HttpError(403))
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
        labels={"m7": ["INBOX", "Label_1"]}, batch_errors={"m60": _HttpError(404)}
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
        batch_errors={"m3": _HttpError(429), "m1": _HttpError(503)},
        single_errors={"m1": _HttpError(404)},
    )
    result = _gmail(monkeypatch, svc).get_label_ids(["m0", "m1", "m2", "m3"])
    assert svc.singles == ["m1", "m3"]
    assert svc.single_retries == [SINGLE_RETRIES, SINGLE_RETRIES]  # backoff on 429 / 5xx
    assert result == {"m0": [], "m1": None, "m2": [], "m3": ["INBOX"]}
    (warning,) = [r for r in caplog.records if r.levelname == "WARNING"]
    assert (warning.failed, warning.error_types) == (2, ["_HttpError"])


def test_get_label_ids_raises_when_the_single_retry_fails_too(monkeypatch):
    ids = [f"m{i}" for i in range(120)]
    svc = _FakeGmailService(
        batch_errors={"m70": _HttpError(503), "m55": _HttpError(429)},
        single_errors={"m70": _HttpError(500), "m55": _HttpError(429)},
    )
    with pytest.raises(_HttpError) as raised:
        _gmail(monkeypatch, svc).get_label_ids(ids)
    assert raised.value.resp.status == 429  # m55: the first failure in id order
    assert svc.singles == ["m55"]
    assert len(svc.batches) == 2  # the third batch is never sent


@pytest.mark.parametrize("error", [_HttpError(503), KeyError("response-7"), OSError("reset")])
def test_get_label_ids_asks_again_one_by_one_when_the_whole_batch_fails(monkeypatch, caplog, error):
    ids = [f"m{i}" for i in range(60)]
    svc = _FakeGmailService(
        labels={"m55": ["INBOX"]},
        batch_errors={"m2": _HttpError(429)},
        batch_raises={0: (5, error)},  # the first batch answers m0..m4, then fails as a whole
    )
    result = _gmail(monkeypatch, svc).get_label_ids(ids)
    assert len(svc.batches) == 2  # the next batch is still sent
    assert svc.singles == ["m2", *ids[5:BATCH_SIZE]]  # only the calls left without an answer
    assert result == {i: ["INBOX"] if i == "m55" else [] for i in ids}
    (warning,) = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warning.failed == BATCH_SIZE - 4
    assert warning.error_types == sorted({"_HttpError", type(error).__name__})


def test_get_label_ids_raises_when_the_whole_batch_and_the_single_retry_fail(monkeypatch):
    ids = [f"m{i}" for i in range(60)]
    svc = _FakeGmailService(
        batch_raises={0: (0, _HttpError(429))}, single_errors={"m0": _HttpError(403)}
    )
    with pytest.raises(_HttpError) as raised:
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
