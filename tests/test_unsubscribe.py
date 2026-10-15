import json
import socket
import ssl
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from conftest import make_signals

from inbox_jev import unsubscribe as unsub
from inbox_jev.unsubscribe import one_click, parse_mailto, resolve_public_https, unsubscribe

PUBLIC_IP = "93.184.216.34"


def allow(_url):
    return [PUBLIC_IP]


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
    [req] = seen
    assert req.method == "POST"
    _assert_multipart_one_click(req)
    assert sent == []


def _assert_multipart_one_click(req: httpx.Request) -> None:
    """RFC 8058's body as multipart/form-data: one plain field `List-Unsubscribe` = `One-Click`."""
    content_type = req.headers["content-type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=", 1)[1].encode()
    assert req.content == (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="List-Unsubscribe"\r\n\r\n'
        b"One-Click\r\n"
        b"--" + boundary + b"--\r\n"
    )


def _answering(*statuses, seen, body='{"message": "nope"}'):
    """A client whose server answers `statuses` in turn, recording each request in `seen`."""
    answers = iter(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(next(answers), text=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


# The body of the 415 a one-click endpoint answered in production (2026-10-14).
FASTIFY_415 = (
    '{"statusCode":415,"code":"FST_ERR_CTP_INVALID_MEDIA_TYPE","error":"Unsupported Media Type",'
    '"message":"Unsupported Media Type: application/x-www-form-urlencoded"}'
)


def test_one_click_retries_once_urlencoded_after_a_415(caplog):
    """A server that only parses one encoding answers 415 without acting: the same address gets
    the same request once more, urlencoded, with the same Host, User-Agent and pinning."""
    seen = []
    caplog.set_level("INFO", logger="inbox_jev.unsubscribe")
    with _answering(415, 200, seen=seen, body=FASTIFY_415) as client:
        r = one_click("https://shop.com/u/1", [PUBLIC_IP, "93.184.216.35"], client)
    assert (r.status, r.http_status, r.error) == ("ok", 200, None)
    first, second = seen
    _assert_multipart_one_click(first)
    assert second.headers["content-type"] == "application/x-www-form-urlencoded"
    assert second.content == b"List-Unsubscribe=One-Click"
    for req in seen:
        assert str(req.url) == f"https://{PUBLIC_IP}/u/1"
        assert req.headers["host"] == "shop.com"
        assert req.headers["user-agent"] == unsub.USER_AGENT
        assert req.extensions["sni_hostname"] == "shop.com"
    [record] = [x for x in caplog.records if "retried urlencoded" in x.getMessage()]
    assert (record.host, record.http_status) == ("shop.com", 200)


def test_one_click_refused_in_both_encodings_fails_with_a_readable_error():
    seen = []
    with _answering(415, 415, seen=seen, body=FASTIFY_415) as client:
        r = one_click("https://shop.com/u/1", [PUBLIC_IP], client)
    assert (r.status, r.http_status, len(seen)) == ("failed", 415, 2)
    assert r.error == (
        "HTTP 415 Unsupported Media Type: Unsupported Media Type: application/x-www-form-urlencoded"
    )


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422, 429, 500, 502, 503])
def test_one_click_never_retries_any_other_refusal(status):
    """Only a 415 says the server didn't act on the request; any other answer is the result."""
    seen = []
    with _answering(status, 200, seen=seen) as client:
        r = one_click("https://shop.com/u/1", [PUBLIC_IP, "93.184.216.35"], client)
    assert (r.status, r.http_status, len(seen)) == ("failed", status, 1)
    assert r.error.startswith(f"HTTP {status} ")


def test_one_click_retry_after_a_415_is_never_redirected_and_its_error_is_the_result():
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(415)
        if len(seen) == 2:
            return httpx.Response(302, headers={"Location": "https://127.0.0.1/admin"})
        pytest.fail("the redirect was followed")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        r = one_click("https://shop.com/u", [PUBLIC_IP], client)
    assert (r.status, r.http_status, len(seen)) == ("ok", 302, 2)

    seen.clear()

    def refused_then_timeout(request):
        seen.append(request.url.host)
        if len(seen) == 1:
            return httpx.Response(415)
        raise httpx.ConnectError("refused", request=request)

    with httpx.Client(transport=httpx.MockTransport(refused_then_timeout)) as client:
        r = one_click("https://shop.com/u", [PUBLIC_IP, "93.184.216.35"], client)
    assert (r.status, r.error) == ("failed", "ConnectError: refused")
    assert seen == [PUBLIC_IP, PUBLIC_IP]  # no third request, to the next address or any other


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (
            500,
            "<html><body>Oops <script>x</script></body></html>",
            "HTTP 500 Internal Server Error",
        ),
        (404, "", "HTTP 404 Not Found"),
        (400, '{"message": "Bad Request"}', "HTTP 400 Bad Request"),
        (400, '["not", "an object"]', "HTTP 400 Bad Request"),
        (
            400,
            json.dumps({"message": 42, "error": "token\u0007 expired\n<!channel>"}),
            "HTTP 400 Bad Request: token expired <!channel>",
        ),
        (410, '{"detail": "{gone} ' + "x" * 200 + '"}', "HTTP 410 Gone: gone " + "x" * 115),
        (499, json.dumps({"message": "\u00e9\u2028\u00e9", "detail": 7}), "HTTP 499"),
        (
            403,
            json.dumps({"message": "confirm at https://evil.example/x?t=1 or ftp://a.b now"}),
            "HTTP 403 Forbidden: confirm at or now",
        ),
        (403, json.dumps({"error": "http://only.a/link"}), "HTTP 403 Forbidden"),
    ],
)
def test_a_refused_one_click_stores_the_status_and_at_most_a_short_plain_message(
    status, body, error
):
    """Never the raw body: the status and its standard reason, plus a JSON body's own message
    when it has one, on one line of printable ASCII without braces, at most 120 characters."""
    with _answering(status, seen=[], body=body) as client:
        r = one_click("https://shop.com/u", [PUBLIC_IP], client)
    assert (r.status, r.http_status, r.error) == ("failed", status, error)


@pytest.mark.parametrize(
    "body",
    [
        b"[" * 20_000,  # deeply nested: json raises RecursionError
        b'{"a": ' * 20_000 + b"1" + b"}" * 20_000,
        json.dumps({"message": "huge " + "x" * 5000}).encode(),  # over the parse limit
        b'"just a string"',
        b"42",
        b"null",
        b'{"message": "caf\xe9 \xff"}',  # not UTF-8
        b"\xff\xfe\x00\x01",
    ],
    ids=[
        "nested-arrays",
        "nested-objects",
        "huge-object",
        "string",
        "number",
        "null",
        "latin-1",
        "binary",
    ],
)
def test_a_refused_one_click_with_a_hostile_body_still_returns_a_result(body):
    """The request was sent, so whatever the body, a Result comes back and can be recorded."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500, content=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        r = one_click("https://shop.com/u", [PUBLIC_IP, "93.184.216.35"], client)
    assert (r.status, r.http_status, r.error, len(seen)) == (
        "failed",
        500,
        "HTTP 500 Internal Server Error",
        1,
    )


def test_a_bug_describing_a_refusal_still_returns_the_status(monkeypatch, caplog):
    """The safety net after the POST: a failure in building the error keeps the attempt."""

    def broken(resp: httpx.Response) -> str:
        raise RuntimeError("bug")

    monkeypatch.setattr(unsub, "describe_http_failure", broken)
    with _answering(502, seen=[]) as client:
        r = one_click("https://shop.com/u", [PUBLIC_IP], client)
    assert (r.status, r.http_status, r.error) == ("failed", 502, "HTTP 502 Bad Gateway")
    [record] = [x for x in caplog.records if "describing a refused" in x.getMessage()]
    assert (record.host, record.http_status, record.exc_info[0]) == ("shop.com", 502, RuntimeError)


def test_one_click_failure_falls_back_to_mailto():
    sent = []
    r = unsubscribe(
        make_signals(), lambda *a: sent.append(a), client_returning(500), url_guard=allow
    )
    assert (r.method, r.status) == ("mailto", "ok")
    assert sent == [("unsub@shop.com", "unsubscribe", "unsubscribe")]


def test_one_click_failure_without_mailto_reports_failure():
    r = unsubscribe(
        make_signals(unsubscribe_mailto=()), lambda *a: None, client_returning(500), url_guard=allow
    )
    assert (r.method, r.status, r.http_status) == ("one_click", "failed", 500)


def test_blocked_url_uses_mailto():
    sent = []
    r = unsubscribe(make_signals(), lambda *a: sent.append(a), url_guard=lambda u: [])
    assert r.method == "mailto" and sent


def test_no_one_click_and_no_mailto_is_manual():
    r = unsubscribe(make_signals(one_click=False, unsubscribe_mailto=()), lambda *a: None)
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
    assert resolve_public_https("http://example.com/u") == []
    assert resolve_public_https("https://127.0.0.1/u") == []
    assert resolve_public_https("https://10.0.0.5:9099/run-script") == []
    assert resolve_public_https("https://localhost/u") == []
    assert resolve_public_https("https:///nohost") == []
    assert resolve_public_https("https://shop.com:99999/u") == []  # bad port


def _resolver(monkeypatch, answers):
    """Fake DNS: hostname -> list of addresses, in order; records each lookup."""
    lookups = []

    def getaddrinfo(host, port, *args, **kwargs):
        lookups.append(host)
        if host not in answers:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in answers[host]]

    monkeypatch.setattr(unsub.socket, "getaddrinfo", getaddrinfo)
    return lookups


def test_ssrf_guard_returns_the_validated_address(monkeypatch):
    _resolver(monkeypatch, {"shop.com": [PUBLIC_IP, "93.184.216.35", PUBLIC_IP]})
    assert resolve_public_https("https://shop.com/u/123") == [PUBLIC_IP, "93.184.216.35"]


def test_ssrf_guard_rejects_host_with_any_private_address(monkeypatch):
    _resolver(
        monkeypatch,
        {"shop.com": [PUBLIC_IP, "10.0.0.5"], "gone.com": [], "v6.com": [PUBLIC_IP, "fe80::1%en0"]},
    )
    assert resolve_public_https("https://shop.com/u") == []
    assert resolve_public_https("https://v6.com/u") == []  # link-local with a zone id
    assert resolve_public_https("https://gone.com/u") == []
    assert resolve_public_https("https://nxdomain.com/u") == []


def test_one_click_connects_to_the_validated_address_not_a_second_lookup(monkeypatch):
    """DNS rebinding: the host resolves to a public IP for the guard, then to an internal one.

    The POST must go to the address the guard returned, with the hostname kept for the Host
    header and for TLS (SNI and certificate check), and DNS must not be consulted again.
    """
    answers = {"shop.com": [PUBLIC_IP]}
    lookups = _resolver(monkeypatch, answers)
    seen = []
    client = client_returning(200, seen)
    original = unsub.resolve_public_https

    def guard_then_rebind(url):
        address = original(url)
        answers["shop.com"] = ["127.0.0.1"]  # any later lookup would hit localhost
        return address

    r = unsubscribe(
        make_signals(unsubscribe_https=("https://shop.com:8443/u/123?list=a&b=c",)),
        lambda *a: None,
        client,
        url_guard=guard_then_rebind,
    )
    assert (r.status, r.target) == ("ok", "https://shop.com:8443/u/123?list=a&b=c")
    [req] = seen
    assert str(req.url) == f"https://{PUBLIC_IP}:8443/u/123?list=a&b=c"
    assert req.headers["host"] == "shop.com:8443"
    assert req.extensions["sni_hostname"] == "shop.com"
    assert lookups == ["shop.com"]


def test_one_click_pins_ipv6_and_idna_hosts():
    seen = []
    r = one_click("https://bücher.example/u", ["2001:db8::1"], client_returning(200, seen))
    assert r.status == "ok"
    assert str(seen[0].url) == "https://[2001:db8::1]/u"
    assert seen[0].headers["host"] == "xn--bcher-kva.example"
    assert seen[0].extensions["sni_hostname"] == "xn--bcher-kva.example"


def test_one_click_does_not_follow_redirects():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/admin"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        r = one_click("https://shop.com/u", [PUBLIC_IP], client)
    assert (r.status, r.http_status) == ("ok", 302) and len(seen) == 1


def test_one_click_tries_the_next_address_when_one_is_unreachable():
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "2001:db8::1":
            raise httpx.ConnectError("Network is unreachable", request=request)
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        r = one_click("https://shop.com/u", ["2001:db8::1", PUBLIC_IP], client)
    assert (r.status, r.http_status) == ("ok", 200)
    assert seen == ["2001:db8::1", PUBLIC_IP]


def test_one_click_stops_at_the_first_answer_or_a_request_already_sent():
    seen = []

    def answers_500(request):
        seen.append(request.url.host)
        return httpx.Response(500, text="nope")

    with httpx.Client(transport=httpx.MockTransport(answers_500)) as client:
        r = one_click("https://shop.com/u", [PUBLIC_IP, "93.184.216.35"], client)
    assert (r.status, r.http_status, seen) == ("failed", 500, [PUBLIC_IP])

    seen.clear()

    def read_timeout(request):
        seen.append(request.url.host)
        raise httpx.ReadTimeout("timed out", request=request)

    with httpx.Client(transport=httpx.MockTransport(read_timeout)) as client:
        r = one_click("https://shop.com/u", [PUBLIC_IP, "93.184.216.35"], client)
    assert r.status == "failed" and r.error.startswith("ReadTimeout")
    assert seen == [PUBLIC_IP]


def test_one_click_reports_the_last_connect_error_when_every_address_fails():
    def refused(request):
        raise httpx.ConnectError(f"refused by {request.url.host}", request=request)

    with httpx.Client(transport=httpx.MockTransport(refused)) as client:
        r = one_click("https://shop.com/u", [PUBLIC_IP, "93.184.216.35"], client)
    assert (r.status, r.error) == ("failed", "ConnectError: refused by 93.184.216.35")


def test_production_client_ignores_proxy_env_and_redirects(monkeypatch):
    """A proxy from HTTPS_PROXY would tunnel without `sni_hostname` and pick the destination."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    with unsub._client() as client:
        assert client.trust_env is False and client.follow_redirects is False
        assert not client._mounts  # no proxy transport was mounted from the environment


# A real TLS handshake on loopback (no external network): proves that connecting to a pinned IP
# still verifies the certificate against the URL's hostname. `cryptography` comes with
# google-auth's dependencies (it is in uv.lock); only these tests skip without it.


def _cert_files(tmp_path, hostname):
    """A throwaway CA and a certificate for `hostname` it signed: (ca.pem, cert.pem, key.pem)."""
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    def name(cn):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    start = datetime.now(UTC) - timedelta(days=1)
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca = (
        x509.CertificateBuilder(subject_name=name("test ca"), issuer_name=name("test ca"))
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(start)
        .not_valid_after(start + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    cert = (
        x509.CertificateBuilder(subject_name=name(hostname), issuer_name=ca.subject)
        .public_key(key.public_key())
        .serial_number(2)
        .not_valid_before(start)
        .not_valid_after(start + timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(  # required by Python 3.13+'s strict verification
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    (tmp_path / "ca.pem").write_bytes(ca.public_bytes(pem))
    (tmp_path / "cert.pem").write_bytes(cert.public_bytes(pem))
    (tmp_path / "key.pem").write_bytes(
        key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return tmp_path / "ca.pem", tmp_path / "cert.pem", tmp_path / "key.pem"


@pytest.fixture
def tls_server(tmp_path):
    """HTTPS server on 127.0.0.1 with a certificate for unsub.test; yields (port, ca, hosts)."""
    ca, cert, key = _cert_files(tmp_path, "unsub.test")
    hosts = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            hosts.append(self.headers["Host"])
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], ca, hosts
    finally:
        server.shutdown()
        server.server_close()


def test_pinned_one_click_verifies_tls_against_the_hostname(tls_server):
    port, ca, hosts = tls_server
    verify = ssl.create_default_context(cafile=str(ca))
    with httpx.Client(verify=verify, timeout=5, trust_env=False) as client:
        ok = one_click(f"https://unsub.test:{port}/u", ["127.0.0.1"], client)
    assert (ok.status, ok.http_status) == ("ok", 200), ok.error
    assert hosts == [f"unsub.test:{port}"]

    with httpx.Client(verify=verify, timeout=5, trust_env=False) as client:
        bad = one_click(f"https://other.test:{port}/u", ["127.0.0.1"], client)
    assert bad.status == "failed" and "ConnectError" in bad.error
    assert hosts == [f"unsub.test:{port}"]  # the mismatched certificate stopped the request


def test_a_failed_mailto_send_is_a_failed_attempt_logged_with_its_traceback(caplog):
    """Whatever `send_mail` raises (a Gmail error or a bug) is stored as a failed attempt, and
    its traceback is logged so a bug doesn't pass for an ordinary failure."""

    def send(to, subject, body):
        raise TypeError("send() got an unexpected keyword argument")

    r = unsubscribe(make_signals(one_click=False), send)
    assert (r.method, r.status, r.target) == (
        "mailto",
        "failed",
        "mailto:unsub@shop.com?subject=unsubscribe",
    )
    assert r.error == "TypeError: send() got an unexpected keyword argument"
    [record] = [x for x in caplog.records if x.getMessage() == "unsubscribe mail failed"]
    assert record.exc_info is not None and record.exc_info[0] is TypeError
    assert record.target == "unsub@shop.com"


def test_one_click_without_a_client_opens_its_own_and_closes_it(monkeypatch):
    own = client_returning(200)
    monkeypatch.setattr(unsub, "_client", lambda: own)
    r = unsubscribe(make_signals(), lambda *a: None, url_guard=allow)
    assert (r.method, r.status) == ("one_click", "ok")
    assert own.is_closed


def test_one_click_leaves_a_given_client_open_and_opens_none_when_every_url_is_blocked(
    monkeypatch,
):
    monkeypatch.setattr(unsub, "_client", lambda: pytest.fail("no client is needed"))
    given = client_returning(200)
    assert unsubscribe(make_signals(), lambda *a: None, given, url_guard=allow).status == "ok"
    assert not given.is_closed
    given.close()
    r = unsubscribe(make_signals(), lambda *a: None, url_guard=lambda u: [])
    assert r.method == "mailto"
