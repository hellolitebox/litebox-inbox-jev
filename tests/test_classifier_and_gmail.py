import base64

from inbox_jev.classifier import CATEGORY_Q, NEEDS_REPLY_Q, TAG_PREFIX, build_questions, build_state
from inbox_jev.gmail import Email, extract_body


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
