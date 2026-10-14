# 0018. One-click unsubscribe connects to the addresses the SSRF guard validated

- Status: Accepted
- Date: 2026-10-13
- PR: #20

## Context

The one-click `POST` goes to a URL chosen by the sender of the email. `is_public_https` resolved its
host and rejected private, loopback and link-local addresses, then httpx resolved the host again to
connect. A sender controlling the DNS of that host could answer the first lookup with a public
address and the second with an internal one (DNS rebinding), so the `POST` would reach a host on the
server's network despite the guard.

## Decision

The guard (`resolve_public_https`) returns the addresses it validated, in DNS order, and `one_click`
connects to those IPs, trying the next one only when the connection fails (refused, connect timeout,
TLS failure) before the request is sent: the URL's host is replaced with the IP, the original host
goes in the `Host` header and in httpx's `sni_hostname` request extension. httpcore uses
`sni_hostname` as the TLS `server_hostname`, so SNI and certificate verification still use the
hostname: a certificate that isn't valid for it fails the request. Redirects stay disabled, and the
client ignores proxy environment variables (`trust_env=False`): httpcore's proxy tunnel ignores
`sni_hostname`, so a pinned request through `HTTPS_PROXY` would fail TLS, and the proxy would choose
the destination.

## Alternatives considered

- A custom httpx transport or resolver that pins DNS: more code, tied to httpcore internals.
- Re-checking the peer address after connecting: the request has already been sent by then.
- An egress proxy that blocks private ranges: right for a larger deployment, but another moving part
  for a single-VPS tool.

## Consequences

- A host with several addresses keeps working when one is down (or an IPv6 address comes first on a
  server without IPv6 egress), as with `socket.create_connection`. A request that was sent (an HTTP
  answer, a read timeout) isn't repeated on another address; it fails and is retried like any failed
  one-click (`MAX_UNSUB_ATTEMPTS`).
- A test does a real TLS handshake on loopback with a throwaway CA to check that a certificate for
  another name is rejected.
