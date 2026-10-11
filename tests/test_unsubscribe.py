import httpx
from conftest import make_signals

from inbox_jev.unsubscribe import is_public_https, parse_mailto, unsubscribe


def allow(_url):
    return True


def client_returning(status, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, text="nope" if status >= 400 else "ok")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_one_click_success_posts_rfc8058_body():
    seen, sent = [], []
    r = unsubscribe(
        make_signals(), lambda *a: sent.append(a), client_returning(200, seen), url_guard=allow
    )
    assert (r.method, r.status, r.http_status) == ("one_click", "ok", 200)
    assert seen[0].method == "POST"
    assert seen[0].content == b"List-Unsubscribe=One-Click"
    assert sent == []


def test_one_click_failure_falls_back_to_mailto():
    sent = []
    r = unsubscribe(
        make_signals(), lambda *a: sent.append(a), client_returning(500), url_guard=allow
    )
    assert (r.method, r.status) == ("mailto", "ok")
    assert sent == [("unsub@shop.com", "unsubscribe", "unsubscribe")]


def test_one_click_failure_without_mailto_reports_failure():
    r = unsubscribe(
        make_signals(unsubscribe_mailto=[]), lambda *a: None, client_returning(500), url_guard=allow
    )
    assert (r.method, r.status, r.http_status) == ("one_click", "failed", 500)


def test_blocked_url_uses_mailto():
    sent = []
    r = unsubscribe(make_signals(), lambda *a: sent.append(a), url_guard=lambda u: False)
    assert r.method == "mailto" and sent


def test_no_one_click_and_no_mailto_is_manual():
    r = unsubscribe(make_signals(one_click=False, unsubscribe_mailto=[]), lambda *a: None)
    assert (r.method, r.status, r.target) == ("manual", "manual_pending", "https://shop.com/u/123")


def test_unauthenticated_sender_is_skipped():
    sent = []
    r = unsubscribe(make_signals(dmarc_pass=False), lambda *a: sent.append(a), url_guard=allow)
    assert r.status == "skipped" and sent == []


def test_parse_mailto():
    assert parse_mailto("mailto:u%2Bx@l.com?Subject=Remove%20me&body=bye") == (
        "u+x@l.com",
        "Remove me",
        "bye",
    )
    assert parse_mailto("mailto:u@l.com") == ("u@l.com", "unsubscribe", "unsubscribe")


def test_ssrf_guard_rejects_private_and_http():
    assert not is_public_https("http://example.com/u")
    assert not is_public_https("https://127.0.0.1/u")
    assert not is_public_https("https://10.0.0.5:9099/run-script")
    assert not is_public_https("https://localhost/u")
    assert not is_public_https("https:///nohost")
