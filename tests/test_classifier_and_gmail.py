import base64
import time

from test_triage import _HttpError

from inbox_jev.classifier import CATEGORY_Q, NEEDS_REPLY_Q, TAG_PREFIX, build_questions, build_state
from inbox_jev.gmail import Email, extract_body, is_not_found


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
