import base64
import encodings.aliases
import re
import time
from datetime import UTC, datetime, timedelta

import pytest
from fakes import (
    SENT_BASE_S,
    HttpError,
    SentMailService,
    calendar_payload,
    jev_request_json,
    sent_mail_gmail,
)

from inbox_jev import gmail as gmail_module
from inbox_jev.body import (
    HOSTNAME_MAX,
    LABEL_MAX,
    LINK_DOMAINS_MAX,
    calendar_method,
    extract_body,
    extract_link_domains,
    link_domain,
)
from inbox_jev.classification import PreviousContact
from inbox_jev.classifier import (
    CATEGORY_INSTRUCTIONS,
    CATEGORY_Q,
    NEEDS_REPLY_Q,
    TAG_PREFIX,
    build_questions,
    build_state,
)
from inbox_jev.gmail import (
    BATCH_SIZE,
    SINGLE_RETRIES,
    Email,
    Gmail,
    ThreadMessage,
    is_not_found,
)
from inbox_jev.sent_scan import (
    NEVER_VERDICT_TTL,
    SENT_ADDRESS_READS,
    SENT_DOMAIN_READS,
    SENT_NEWER_LIMIT,
    DomainScan,
    DomainVerdict,
    MemoryScans,
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
    state = build_state(email, "me@acme.com", 100, PreviousContact.NEVER)
    assert state["previous_contact"] == "never" and type(state["previous_contact"]) is str


def _bare_email(headers, link_domains=None) -> Email:
    return Email("1", "t", ["INBOX"], headers, "", "body", 0, link_domains or [])


def test_build_state_adds_phishing_signals_only_when_they_say_something():
    """A fake reply gets `reply_without_thread`; a real reply with no Gmail
    Authentication-Results and no links gets none of the three keys."""
    state = build_state(_bare_email({"from": ["A <a@b.com>"], "subject": ["RE: Hi"]}), "m@x.com", 9)
    assert state["reply_without_thread"] is True
    state = build_state(
        _bare_email({"subject": ["RE: Hi"], "in-reply-to": ["<1@b.com>"]}), "m@x.com", 9
    )
    assert not {"reply_without_thread", "link_domains", "sender_auth"} & state.keys()
    assert "calendar_subject_without_invite" not in state


def test_build_state_flags_a_calendar_subject_without_an_invite():
    fake = _bare_email({"subject": ["Accepted: Kickoff @ Mon"]})
    assert build_state(fake, "m@x.com", 9)["calendar_subject_without_invite"] is True
    real = _bare_email({"subject": ["Accepted: Kickoff @ Mon"]})
    real.calendar_method = "REPLY"
    assert "calendar_subject_without_invite" not in build_state(real, "m@x.com", 9)


def test_build_state_carries_link_domains_and_sender_auth():
    headers = {
        "from": ["News <noreply@news-site.example>"],
        "subject": ["Weekly"],
        "authentication-results": [
            "mx.google.com; dkim=pass header.i=@news-site.example; spf=pass"
        ],
    }
    state = build_state(_bare_email(headers, ["brand.example", "gov.example"]), "m@x.com", 9)
    assert state["link_domains"] == ["brand.example", "gov.example"]
    assert state["sender_auth"] == {"spf": "pass", "dkim": "pass", "dmarc": "none"}
    assert "reply_without_thread" not in state


def test_category_instructions_explain_the_phishing_signals():
    for key in (
        "sender_auth",
        "reply_without_thread",
        "calendar_subject_without_invite",
        "link_domains",
    ):
        assert key in CATEGORY_INSTRUCTIONS


def _html(markup: str) -> dict:
    return {"mimeType": "text/html", "body": {"data": b64(markup)}}


def test_extract_link_domains_counts_registrable_domains_most_frequent_first():
    markup = (
        '<a href="https://links.notification.brand.example/ls/click?upn=TOKEN&amp;u=me%40x.com">'
        "Play</a>"
        '<a href="https://www.agency.gov.example/a">1</a>'
        '<a href="https://agency.gov.example/b?x=1">2</a>'
        '<a href="mailto:someone@agency.gov.example">mail</a>'
        '<a href="tel:+1555">call</a><a href="#top">top</a><a>no href</a>'
        '<area href="HTTP://Maps.Other.example/p">'
    )
    assert extract_link_domains(_html(markup)) == [
        "gov.example",
        "brand.example",
        "other.example",
    ]


def test_extract_link_domains_returns_domains_only_never_paths_or_queries():
    markup = '<a href="https://track.example/c/u=me%40x.com?token=SECRET">x</a>'
    (domain,) = extract_link_domains(_html(markup))
    assert domain == "track.example"


def test_extract_link_domains_reads_plain_text_urls_and_skips_attachments():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "text/plain",
                "body": {"data": b64("See https://docs.example.org/x, or (http://a.example).")},
            },
            {
                "mimeType": "text/html",
                "filename": "invite.html",
                "body": {"data": b64('<a href="https://evil.example">x</a>')},
            },
        ],
    }
    assert extract_link_domains(payload) == ["example.org", "a.example"]


def test_extract_link_domains_is_capped():
    markup = "".join(f'<a href="https://d{i}.example">x</a>' for i in range(20))
    assert len(extract_link_domains(_html(markup))) == LINK_DOMAINS_MAX
    assert extract_link_domains(_html(markup), limit=2) == ["d0.example", "d1.example"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://a.b.example.co.uk/x", "example.co.uk"),
        ("http://192.0.2.7/login", "192.0.2.7"),
        ("https://[2001:db8::1]/", "2001:db8::1"),
        ("https://co.uk/", None),  # a public suffix: nobody's domain
        ("https://localhost/", None),
        ("https://[bad/", None),
        ("https://exa_mple.com/", None),
        ("ftp://files.example/", None),
        ("javascript:alert(1)", None),
        # Read as a browser reads them (WHATWG URL Standard).
        ("https:\\\\evil.example/x", "evil.example"),
        ("https:\\/evil.example/x", "evil.example"),
        ("https:/evil.example", "evil.example"),
        ("HTTPS:evil.example", "evil.example"),
        ("//evil.example/x", "evil.example"),
        (" \thttps://ev\nil.example/ ", "evil.example"),
        ("https://good.example\\@evil.example/", "good.example"),
        ("https://evil%2Eexample/", "evil.example"),
        ("/relative/path", None),
        ("http://0x7f.1/", "127.0.0.1"),
        ("http://2130706433/", "127.0.0.1"),
        ("http://010.0.0.1/", "8.0.0.1"),
        ("http://192.0.2.7./", "192.0.2.7"),
        ("http://256.1.1.1/", None),
        ("http://1.2.3.4.5/", None),
        ("http://09.1/", None),
        ("http://evil.1/", None),  # a numeric last label makes the host IPv4, and this isn't
        ("https://?q=1", None),  # no host
        # Mapped as a browser maps a host (UTS #46): NFKC, and IDNA's other full stops are dots.
        ("https://evil\u3002example/", "evil.example"),
        ("https://evil\uff0eexample/", "evil.example"),
        ("https://evil\uff61example/", "evil.example"),
        ("http://\uff11\uff12\uff17.0.0.1/", "127.0.0.1"),
        ("https://\uff45vil.example/", "evil.example"),
        # 15 characters that NFKC turns into 270: over LABEL_MAX once mapped.
        ("https://" + "\ufdfa" * 15 + ".example/", None),
        # UTS #46's ignored code points are dropped, as a browser drops them.
        ("https://ev\u200bil.example/", "evil.example"),
        ("https://evil\u00adexample.com/", "evilexample.com"),
        ("https://e\u034fv\u180bi\u180dl\u2060.\ufeffexample/", "evil.example"),
        ("https://evil\ufe00\ufe0f.example/", "evil.example"),
    ],
)
def test_link_domain(url, expected):
    assert link_domain(url) == expected


def test_link_domain_drops_hosts_longer_than_dns_allows():
    label = "a" * LABEL_MAX
    assert link_domain(f"https://{label}.example/") == f"{label}.example"
    assert link_domain(f"https://{label}a.example/") is None
    assert link_domain(f"https://{label}a.ok.example/") is None  # a long subdomain label too
    name = ".".join(["a" * 60] * 4) + ".example"  # every label fits, 251 characters
    assert len(name) <= HOSTNAME_MAX and link_domain(f"https://{name}/") == "a" * 60 + ".example"
    assert link_domain(f"https://aaa.{name}/") is None  # 255 characters
    # A long Unicode label is measured as the punycode DNS carries.
    cjk = "".join(chr(0x4E00 + 37 * i) for i in range(30))  # 30 characters, 72 in punycode
    assert len(cjk) <= LABEL_MAX and link_domain(f"https://{cjk}.example/") is None


def test_extract_link_domains_stays_small_with_huge_hosts():
    markup = "".join(f'<a href="https://{"a" * 100_000}{i}.com/">x</a>' for i in range(8))
    markup += '<a href="https://real.example/">x</a>'
    assert extract_link_domains(_html(markup)) == ["real.example"]


class _FullGetService:
    """messages().get(format="full") answering one message."""

    def __init__(self, msg):
        self.msg = msg

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, userId, id, format):
        assert (userId, id, format) == ("me", self.msg["id"], "full")
        return type("Req", (), {"execute": lambda _: self.msg})()


def test_gmail_get_reads_body_and_link_domains(monkeypatch):
    msg = {
        "id": "1",
        "threadId": "t",
        "snippet": "a &amp; b",
        "payload": _html('<p>Hi</p><a href="https://site.example/x">x</a>'),
    }
    email = _gmail(monkeypatch, _FullGetService(msg)).get("1")
    assert (email.body, email.snippet, email.link_domains) == ("Hi x", "a & b", ["site.example"])


@pytest.mark.parametrize("method", ["REPLY", "REQUEST", "CANCEL", "COUNTER"])
def test_calendar_method_reads_the_imip_part_of_a_google_calendar_email(method):
    """A response (REPLY), an invitation or update (REQUEST), a cancellation, a proposed new
    time: the method comes from the text/calendar part's Content-Type, inside the
    multipart/alternative next to the plain and HTML versions."""
    assert calendar_method(calendar_payload(method)) == method


def test_calendar_method_reads_quoted_and_lowercase_parameters():
    """Outlook-style parameters: quoted, lowercase, in another order."""
    payload = calendar_payload(
        "cancel", content_type='text/calendar; method="cancel"; charset=utf-8'
    )
    assert calendar_method(payload) == "CANCEL"


def test_calendar_method_is_none_without_an_imip_part():
    """No calendar part, a Subject that only looks like an RSVP, or a text/calendar part whose
    Content-Type names no method: not a calendar message."""
    plain = {
        "mimeType": "multipart/alternative",
        "headers": [{"name": "Subject", "value": "Accepted: Kickoff @ Mon 10am"}],
        "parts": [
            _html("<p>Accepted</p>"),
            {"mimeType": "text/plain", "body": {"data": b64("ok")}},
        ],
    }
    assert calendar_method(plain) is None
    no_method = calendar_payload("REPLY", content_type='text/calendar; charset="UTF-8"')
    assert calendar_method(no_method) is None
    no_content_type = calendar_payload("REPLY")
    calendar_part = no_content_type["parts"][0]["parts"][2]
    calendar_part["headers"] = [
        h for h in calendar_part["headers"] if h["name"].lower() != "content-type"
    ]
    assert calendar_method(no_content_type) is None


def test_calendar_method_reads_a_content_type_that_is_not_the_first_header():
    """The part's Content-Type is found among its headers, wherever it sits."""
    payload = calendar_payload("REPLY")
    calendar_part = payload["parts"][0]["parts"][2]
    calendar_part["headers"].reverse()
    assert calendar_part["headers"][0]["name"].lower() != "content-type"
    assert calendar_method(payload) == "REPLY"


def test_calendar_method_decodes_an_rfc2231_parameter():
    """`method*=us-ascii''REPLY` (RFC 2231) is the same method as `method=REPLY`."""
    payload = calendar_payload("REPLY", content_type="text/calendar; method*=us-ascii''REPLY")
    assert calendar_method(payload) == "REPLY"


@pytest.mark.parametrize(
    "value",
    [
        '"REPLY\x00EVIL"',
        '"REPLY\x1b]0;pwned\x07\x1b[2J"',
        "A" * 100_000,
        '"RE PLY"',
        "A" * 33,
        "us-ascii''REPLY%00EVIL",
    ],
    ids=["nul", "escape-sequences", "100kb", "space", "33-chars", "rfc2231-nul"],
)
def test_calendar_method_is_none_for_a_value_that_is_not_an_itip_token(value):
    """The method is sender-controlled text: only an RFC 5545 iana-token (letters, digits, `-`,
    at most 32) is a method, so a NUL, terminal escape sequences or 100 KB never reach
    `Email.calendar_method` or `dry_classify`'s output; the email then counts like any other."""
    param = f"method*={value}" if value.startswith("us-ascii") else f"method={value}"
    payload = calendar_payload("REPLY", content_type=f"text/calendar; {param}")
    assert calendar_method(payload) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("idna''REPLY", "REPLY"),
        ("undefined''REPLY", "REPLY"),
        ("rot13''ERCYL", "ERCYL"),
        ("utf-16''%FF%FER%00E%00", None),
        ("us-ascii''%2541", None),
    ],
    ids=["idna", "undefined", "rot13", "utf-16", "no-double-decoding"],
)
def test_calendar_method_ignores_an_rfc2231_charset(value, expected):
    """The charset named in `method*=<charset>''<value>` is the sender's choice and is never used
    to decode: `idna` and `undefined` would raise and fail the whole fetch. The value counts as
    it is, so `rot13''ERCYL` is the token `ERCYL`, not `REPLY` (any method only keeps the email
    out of auto rules), and percent-decoding happens once (`%2541` is `%41`, not `A`)."""
    payload = calendar_payload("REPLY", content_type=f"text/calendar; method*={value}")
    assert calendar_method(payload) == expected


def test_calendar_method_never_raises_whatever_the_rfc2231_charset():
    """Every codec Python knows, plus unknown names, against values that are valid, junk,
    non-ASCII or broken percent-escapes: the result is None or an uppercase iTIP token."""
    charsets = {*encodings.aliases.aliases, *encodings.aliases.aliases.values()}
    charsets |= {"idna", "undefined", "rot13", "zlib", "uu", "base64", "nope", "", "%00"}
    values = ["REPLY", "x", "%FF%FER%00E%00", "%00", "%", "%zz", "%E2%80%8B", "A" * 40, ""]
    for charset in sorted(charsets):
        for value in values:
            ctype = f"text/calendar; method*={charset}'en'{value}"
            method = calendar_method(calendar_payload("REPLY", content_type=ctype))
            assert method is None or re.fullmatch(r"[A-Z0-9-]{1,32}", method), (charset, value)


def test_gmail_get_reads_the_calendar_method(monkeypatch):
    msg = {"id": "1", "threadId": "t", "snippet": "", "payload": calendar_payload("REPLY")}
    email = _gmail(monkeypatch, _FullGetService(msg)).get("1")
    assert (email.calendar_method, email.body) == ("REPLY", "Ana accepted")
    plain = {"id": "2", "threadId": "t", "snippet": "", "payload": _html("<p>Hi</p>")}
    assert _gmail(monkeypatch, _FullGetService(plain)).get("2").calendar_method is None


def test_previous_contact_wire_values_are_the_ones_jev_is_told_about():
    """Jev's state carries these strings and its instructions explain them: renaming a member
    must not change what Jev sees."""
    assert [c.value for c in PreviousContact] == [
        "wrote_to_sender",
        "wrote_to_their_domain",
        "never",
    ]
    for contact in ("(wrote_to_sender)", "(wrote_to_their_domain)", "(never)"):
        assert contact in CATEGORY_INSTRUCTIONS


def test_classifier_parses_api_response(cfg):
    import httpx2
    from typesafe_sdk import TypeSafeClient

    from inbox_jev.classifier import Classifier

    seen = []

    def handler(request):
        seen.append(jev_request_json(request))
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
    html = "<p>pre</p>" + "\u2007" * 200_000 + "\n<p>" + NEWSLETTER + "</p>"
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
    caplog.set_level("INFO", logger="inbox_jev.sent_scan")
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
    caplog.set_level("INFO", logger="inbox_jev.sent_scan")
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
    caplog.set_level("WARNING", logger="inbox_jev.sent_scan")
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


def test_gmail_closes_its_service_connections(monkeypatch):
    closed = []

    class Service:
        def users(self) -> None:
            return None

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(gmail_module, "build", lambda *args, **kwargs: Service())
    with Gmail(creds=None) as gmail:
        assert closed == []
    assert closed == [True]
    gmail.close()  # again: harmless
    assert closed == [True, True]


class _FakeThreadService(_FakeGmailService):
    """`_FakeGmailService` answering threads.get from `threads` (thread id -> messages)."""

    def __init__(self, threads, **kwargs):
        super().__init__(**kwargs)
        self.threads_by_id = threads

    def threads(self):
        return self

    def get(self, userId, id, format):
        assert (userId, format) == ("me", "minimal")
        return _FakeGet(self, id)

    def answer(self, thread_id):
        return {"id": thread_id, "messages": self.threads_by_id.get(thread_id, [])}


def test_get_threads_batches_and_reads_ids_labels_and_dates(monkeypatch):
    ids = [f"t{i}" for i in range(60)]
    svc = _FakeThreadService(
        threads={
            "t1": [
                {"id": "m1", "labelIds": ["INBOX"], "internalDate": "100"},
                {"id": "m2", "labelIds": ["SENT"], "internalDate": "200"},
                {"id": "m3", "internalDate": "300"},  # no labels at all
            ]
        },
        batch_errors={"t5": HttpError(404), "t7": HttpError(429)},
    )
    result = _gmail(monkeypatch, svc).get_threads([*ids, "t1"])  # a repeat is asked once
    assert [len(b) for b in svc.batches] == [BATCH_SIZE, 10]
    assert svc.singles == ["t7"]  # a rate-limited call is asked again on its own
    assert result["t1"] == [
        ThreadMessage("m1", ("INBOX",), 100),
        ThreadMessage("m2", ("SENT",), 200),
        ThreadMessage("m3", (), 300),
    ]
    assert result["t5"] is None  # deleted in Gmail
    assert result["t0"] == [] and result["t7"] == [] and len(result) == 60


def test_get_threads_raises_when_the_single_retry_fails_too(monkeypatch):
    svc = _FakeThreadService(
        threads={}, batch_errors={"t0": HttpError(503)}, single_errors={"t0": HttpError(401)}
    )
    with pytest.raises(HttpError):
        _gmail(monkeypatch, svc).get_threads(["t0", "t1"])
