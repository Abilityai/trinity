"""#736 — the outbound A2A client's TRANSPORT properties.

Driven through a real `httpx.AsyncClient` over `MockTransport`, deliberately
NOT through a monkeypatched `call_endpoint`. The properties here — a byte
ceiling that survives a lying `Content-Length`, a compressed body refused
rather than decoded, a redirect refused rather than followed, a connection
pinned to a validated IP while the SNI stays the registered hostname — are
*transport* properties and cannot be proven against a stub of the function that
implements them.

**A transport test whose mock does not stream is not a transport test.**
`httpx.Response(content=…)` decodes and buffers in the CONSTRUCTOR and marks
the stream consumed, so `aiter_raw()` — the call production makes — raises
`StreamConsumed` against it. The `_as_streaming` helper below is lifted from
`test_ent14_registry_fetch.py`, which exists because that exact mistake shipped
a green byte-ceiling test over a ceiling that was counting the wrong bytes.

Sync throughout with explicit `asyncio.run`: `tests/unit/pytest.ini` overrides
`pyproject.toml`, so `asyncio_mode = auto` does not apply here and a bare
`async def test_*` would be collected and never awaited.
"""
import asyncio
import gzip
import json
import os
import socket
import sys

import httpx
import pytest

_BACKEND = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")
)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services import a2a_client  # noqa: E402
from utils.url_validation import ValidatedPublicUrl  # noqa: E402

pytestmark = pytest.mark.unit

PEER = ValidatedPublicUrl(
    url="https://peer.example.com/a2a/bot",
    hostname="peer.example.com",
    port=443,
    addresses=("93.184.216.34",),
)
ORIGIN_ONLY = ValidatedPublicUrl(
    url="https://peer.example.com",
    hostname="peer.example.com",
    port=443,
    addresses=("93.184.216.34",),
)

CARD = {
    "protocolVersion": "0.3.0",
    "name": "Peer",
    "url": "https://peer.example.com/a2a/bot",
    "preferredTransport": "JSONRPC",
}


def _as_streaming(resp: httpx.Response) -> httpx.Response:
    """Rebuild a buffered mock response as a STREAMING one.

    See the module docstring. A caller needing a COMPRESSED body must build the
    streaming response itself — passing compressed bytes as `content=` would
    decode them here, before the code under test ever sees them, so that
    mistake is refused loudly rather than silently re-encoded.
    """
    if not resp.is_stream_consumed:
        return resp
    if "content-encoding" in resp.headers:
        raise AssertionError(
            "build a Content-Encoding response as a stream "
            "(httpx.Response(..., stream=httpx.ByteStream(raw))) — `content=` "
            "decodes it in the constructor, so the code under test would never "
            "see the compressed bytes"
        )
    return httpx.Response(
        resp.status_code, headers=resp.headers, stream=httpx.ByteStream(resp.content)
    )


def _factory(handler, *, record=None):
    """A `client_factory` returning a real AsyncClient over MockTransport.

    Mirrors production's own client settings, so a test can observe them
    (`trust_env`, `follow_redirects`) rather than take them on trust.
    """
    def _make(timeout):
        def _handle(request: httpx.Request) -> httpx.Response:
            if record is not None:
                record.append(request)
            return _as_streaming(handler(request))

        return httpx.AsyncClient(
            transport=httpx.MockTransport(_handle),
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    return _make


def _json(payload, status=200, headers=None):
    return httpx.Response(status, json=payload, headers=headers or {})


def _rpc_ok(text="hello from the peer", state="completed", task_id="t-1"):
    return {
        "jsonrpc": "2.0",
        "id": "x",
        "result": {
            "id": task_id,
            "contextId": "c-1",
            "kind": "task",
            "status": {"state": state},
            "artifacts": [{"artifactId": "a", "parts": [{"kind": "text", "text": text}]}],
        },
    }


def _two_hop(card=None, rpc=None, record=None):
    """The normal shape: a card GET then an RPC POST."""
    card = card if card is not None else CARD

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _json(card)
        return _json(rpc if rpc is not None else _rpc_ok())

    return _factory(_handler, record=record)


def _call(**kwargs):
    defaults = dict(
        endpoint_url=PEER.url,
        credential="tok",
        message="hi",
        validated=PEER,
    )
    defaults.update(kwargs)
    return asyncio.run(a2a_client.call_endpoint(**defaults))


# --------------------------------------------------------------------------- #
# 1. THE ROUND TRIP WORKS. Listed first on purpose.
#
# The rest of this file proves refusals. A suite of twenty refusals and zero
# successful calls proves a feature that is switched off — so the first test is
# the one that shows an answer coming back, through the real dialect selection,
# the real same-origin pin, the real credential attachment and the real
# response allowlist.
# --------------------------------------------------------------------------- #
def test_a_full_round_trip_returns_the_peers_answer():
    a2a_client.clear_dialect_cache()
    seen = []
    result = _call(client_factory=_two_hop(record=seen))

    assert result.state == "completed"
    assert result.text == "hello from the peer"
    assert result.task_id == "t-1"
    assert result.context_id == "c-1"
    assert result.protocol_version == "0.3"

    # Two hops: the card, then the RPC. The card is the registered endpoint's
    # own (ent#838) — where a per-agent server like Trinity publishes it.
    assert [r.method for r in seen] == ["GET", "POST"]
    assert seen[0].url.path == "/a2a/bot/.well-known/agent-card.json"

    # v0.3 slash method name — a remote Trinity speaks this, and #738
    # federation is the reason "target v1.0 only" was rejected.
    body = json.loads(seen[1].content)
    assert body["method"] == "message/send"
    assert body["jsonrpc"] == "2.0"
    assert body["params"]["message"]["parts"][0]["text"] == "hi"


def test_the_credential_rides_the_rpc_and_never_the_card():
    """The card fetch is uncredentialed — we know nothing about the peer until
    we have read it, and it is the one hop made before the same-origin pin can
    be evaluated."""
    seen = []
    _call(client_factory=_two_hop(record=seen))
    card_req, rpc_req = seen
    assert "authorization" not in {k.lower() for k in card_req.headers.keys()}
    assert rpc_req.headers["authorization"] == "Bearer tok"


# --------------------------------------------------------------------------- #
# 2. Connect-time IP pinning (SV-1/SV-2)
# --------------------------------------------------------------------------- #
def test_both_hops_connect_to_the_validated_ip_with_the_registered_sni():
    """The whole rebinding defence in one assertion.

    The socket target is the address the validator approved; `Host` and
    `sni_hostname` carry the registered name, so TLS still authenticates it. If
    a future refactor drops the pin, the URL host reverts to the hostname and
    this fails.
    """
    seen = []
    _call(client_factory=_two_hop(record=seen))
    for request in seen:
        assert request.url.host == "93.184.216.34", "connection is not pinned"
        assert request.headers["host"] == "peer.example.com"
        assert request.extensions.get("sni_hostname") == "peer.example.com"


def test_the_pin_uses_the_caller_supplied_resolution_not_a_second_lookup(monkeypatch):
    """Passing an already-validated endpoint must not trigger another resolve.

    Resolving twice means the address the caller vetted and the address the
    socket reaches came from two different `getaddrinfo` calls — reopening the
    TOCTOU window the pin exists to close.
    """
    def _boom(*a, **k):
        raise AssertionError("re-resolved an already-validated endpoint")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    result = _call(client_factory=_two_hop())
    assert result.state == "completed"


def test_ipv6_addresses_are_bracketed_in_the_pinned_url():
    v6 = ValidatedPublicUrl(
        url="https://peer.example.com/a2a/bot",
        hostname="peer.example.com",
        port=443,
        addresses=("2606:2800:220:1:248:1893:25c8:1946",),
    )
    seen = []
    _call(validated=v6, client_factory=_two_hop(record=seen))
    assert seen[0].url.host == "2606:2800:220:1:248:1893:25c8:1946"


# --------------------------------------------------------------------------- #
# 3. Proxy neutrality (SV-5)
# --------------------------------------------------------------------------- #
def test_proxy_env_does_not_change_the_connection_target(monkeypatch):
    """Every other control reasons about the TARGET IP. A proxy makes the target
    irrelevant, because the socket goes to the proxy instead."""
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.internal:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://attacker.internal:3128")
    seen = []
    _call(client_factory=_two_hop(record=seen))
    assert all(r.url.host == "93.184.216.34" for r in seen)


def test_the_production_client_sets_trust_env_false():
    """The MockTransport factory mirrors production; this pins the real one, so
    the mirror cannot drift into asserting a property production dropped."""
    client = a2a_client._http_client(httpx.Timeout(5.0))
    try:
        assert client.trust_env is False
        assert client.follow_redirects is False
    finally:
        asyncio.run(client.aclose())


def test_the_ca_context_still_honours_ssl_cert_file(monkeypatch, tmp_path):
    """`trust_env=False` also switches OFF httpx's SSL_CERT_FILE/SSL_CERT_DIR
    handling, which would silently break every install behind a TLS-inspecting
    proxy. The two concerns are separated deliberately: we refuse the
    environment's PROXIES, we still honour its TRUST STORE."""
    import ssl

    calls = {}

    def _fake_ctx(cafile=None, capath=None, **kw):
        calls["cafile"] = cafile
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "ca.pem"))
    monkeypatch.setattr(a2a_client.ssl, "create_default_context", _fake_ctx)
    a2a_client._build_ssl_context()
    assert calls["cafile"] == str(tmp_path / "ca.pem")


# --------------------------------------------------------------------------- #
# 4. Body ceilings and encodings — the tests that need real streaming
# --------------------------------------------------------------------------- #
def test_an_oversized_card_is_aborted_mid_stream():
    big = json.dumps({"protocolVersion": "0.3.0", "pad": "x" * (a2a_client.A2A_CARD_MAX_BYTES + 5000)})

    def _handler(request):
        return httpx.Response(200, content=big.encode())

    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_factory(_handler))
    assert exc.value.reason == "card_too_large"


def test_a_lying_content_length_does_not_defeat_the_ceiling():
    """`Content-Length` is an early abort, never the gate: it is absent on
    chunked responses and trivially lied about."""
    big = b"x" * (a2a_client.A2A_CARD_MAX_BYTES + 5000)

    def _handler(request):
        return httpx.Response(
            200,
            headers={"content-length": "10", "content-type": "application/json"},
            stream=httpx.ByteStream(big),
        )

    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_factory(_handler))
    assert exc.value.reason == "card_too_large"


def test_a_compressed_body_is_refused_not_decoded():
    """A body whose WIRE size passes the ceiling can inflate ~1030:1 past it
    before a decoded-byte total is ever consulted — 458 MB of transient
    allocation from a 199 KiB response, measured elsewhere in this codebase. So
    the ceiling counts wire bytes and compression is refused outright;
    `Accept-Encoding: identity` is the polite half and cannot bind a hostile
    server."""
    raw = gzip.compress(json.dumps(CARD).encode())

    def _handler(request):
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            stream=httpx.ByteStream(raw),
        )

    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_factory(_handler))
    assert exc.value.reason == "card_encoding"


def test_identity_encoding_is_requested_on_every_hop():
    seen = []
    _call(client_factory=_two_hop(record=seen))
    assert all(r.headers["accept-encoding"] == "identity" for r in seen)


def test_an_oversized_rpc_response_is_aborted():
    big = json.dumps({"jsonrpc": "2.0", "result": {"pad": "x" * (a2a_client.A2A_RPC_MAX_BYTES + 5000)}})

    def _handler(request):
        if request.method == "GET":
            return _json(CARD)
        return httpx.Response(200, content=big.encode())

    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_factory(_handler))
    assert exc.value.reason == "rpc_too_large"


# --------------------------------------------------------------------------- #
# 5. Redirects — a failure, not a hop
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_card_redirect_is_a_failure(status):
    def _handler(request):
        return httpx.Response(status, headers={"location": "https://elsewhere.example/"})

    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_factory(_handler))
    assert exc.value.reason == "card_redirect"


def test_an_rpc_redirect_is_a_failure():
    def _handler(request):
        if request.method == "GET":
            return _json(CARD)
        return httpx.Response(307, headers={"location": "http://169.254.169.254/"})

    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_factory(_handler))
    assert exc.value.reason == "rpc_redirect"


# --------------------------------------------------------------------------- #
# 6. The card is a hint, not an authority
# --------------------------------------------------------------------------- #
def test_a_cross_origin_card_url_is_refused():
    """The single control that stops a hostile card redirecting a credentialed
    POST — and the reason #736 can ship while signed cards (ent#159) are
    blocked: it removes the card's authority rather than trying to verify it."""
    card = dict(CARD, url="https://attacker.example.com/a2a/bot")
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_two_hop(card=card))
    assert exc.value.reason == "card_origin_mismatch"


def test_a_card_url_on_a_different_port_is_cross_origin():
    card = dict(CARD, url="https://peer.example.com:8443/a2a/bot")
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_two_hop(card=card))
    assert exc.value.reason == "card_origin_mismatch"


def test_default_port_equivalence_is_honoured():
    """**Trinity's own card emits no explicit port.** Treating `https://h` and
    `https://h:443` as different origins would make Trinity unreachable by its
    own rule, breaking #738 federation on day one."""
    validated = ValidatedPublicUrl(
        url="https://peer.example.com:443/a2a/bot",
        hostname="peer.example.com",
        port=443,
        addresses=("93.184.216.34",),
    )
    card = dict(CARD, url="https://peer.example.com/a2a/bot")
    result = _call(validated=validated, client_factory=_two_hop(card=card))
    assert result.state == "completed"


def test_a_registered_path_that_the_card_contradicts_is_refused():
    """The registry field is documented as "endpoint OR Agent Card URL", so a
    registered URL may carry a path. Two candidate targets and no principled
    way to choose is refused by name, not resolved by luck."""
    card = dict(CARD, url="https://peer.example.com/a2a/someone-else")
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_two_hop(card=card))
    assert exc.value.reason == "card_url_ambiguous"


def _card_at(paths, *, status_elsewhere=404, record=None):
    """A peer that publishes its card only at `paths`; any other GET answers
    `status_elsewhere`."""
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if request.url.path in paths:
                return _json(CARD)
            return httpx.Response(status_elsewhere)
        return _json(_rpc_ok())
    return _factory(_handler, record=record)


def test_a_per_agent_card_is_found_under_the_registered_endpoint():
    """ent#838: a Trinity instance publishes `/a2a/<agent>/.well-known/agent-card.json`
    and nothing at its origin — origin-only discovery could never reach it."""
    seen = []
    result = _call(client_factory=_card_at({"/a2a/bot/.well-known/agent-card.json"}, record=seen))
    assert result.state == "completed"
    assert [r.url.path for r in seen if r.method == "GET"] == ["/a2a/bot/.well-known/agent-card.json"]


def test_an_origin_card_is_still_found_when_the_endpoint_has_none():
    seen = []
    result = _call(client_factory=_card_at({"/.well-known/agent-card.json"}, record=seen))
    assert result.state == "completed"
    assert [r.url.path for r in seen if r.method == "GET"] == [
        "/a2a/bot/.well-known/agent-card.json", "/.well-known/agent-card.json"]


def test_only_a_404_moves_on_to_the_origin_card():
    """A 5xx (or any other failure) on the endpoint's own card is the answer —
    the origin is not a second chance at a peer that is failing."""
    seen = []
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_card_at({"/.well-known/agent-card.json"}, status_elsewhere=503, record=seen))
    assert exc.value.reason == "card_http_error" and exc.value.remote_status == 503
    assert [r.url.path for r in seen] == ["/a2a/bot/.well-known/agent-card.json"]


def test_a_registered_card_url_is_fetched_as_given_and_its_url_is_the_target():
    card_url = ValidatedPublicUrl(
        url="https://peer.example.com/a2a/bot/.well-known/agent-card.json",
        hostname="peer.example.com", port=443, addresses=("93.184.216.34",),
    )
    seen = []
    result = _call(endpoint_url=card_url.url, validated=card_url,
                   client_factory=_card_at({"/a2a/bot/.well-known/agent-card.json"}, record=seen))
    assert result.state == "completed"
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/a2a/bot/.well-known/agent-card.json"), ("POST", "/a2a/bot")]


# --------------------------------------------------------------------------- #
# 7. Dialect (FR-12)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("version", [None, "", "0.3.0", "0.3", "0.2.9"])
def test_v03_and_unknown_versions_use_slash_method_names(version):
    a2a_client.clear_dialect_cache()
    card = dict(CARD)
    if version is None:
        card.pop("protocolVersion")
    else:
        card["protocolVersion"] = version
    seen = []
    _call(client_factory=_two_hop(card=card, record=seen))
    assert json.loads(seen[1].content)["method"] == "message/send"


def test_a_v1_card_is_refused_rather_than_guessed_at():
    """The v1.0 arm is documented and deliberately not claimed: there is no
    v1.0 peer to verify it against, and §FR-6 already used "untestable ⇒ do not
    ship it" to reject SSE. Guessing a vocabulary while holding a credential is
    the version of that mistake with a blast radius."""
    a2a_client.clear_dialect_cache()
    card = dict(CARD, protocolVersion="1.0")
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_two_hop(card=card))
    assert exc.value.reason == "unsupported_protocol_version"


# --------------------------------------------------------------------------- #
# 8. Errors on HTTP 200 (SV-6) — the silent-success path
# --------------------------------------------------------------------------- #
def test_a_jsonrpc_error_on_http_200_is_a_failure_not_an_answer():
    """A2A carries errors in the BODY with a 200 transport status — Trinity's
    own inbound server does exactly this. A client that checks only the status
    code hands the agent an error object as if it were an answer."""
    err = {"jsonrpc": "2.0", "id": "x",
           "error": {"code": -32601, "message": "Method not found: message/send"}}
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_two_hop(rpc=err))
    assert exc.value.reason == "remote_error"
    assert exc.value.remote_code == -32601


def test_a_body_with_neither_result_nor_error_is_a_failure():
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_two_hop(rpc={"jsonrpc": "2.0", "id": "x"}))
    assert exc.value.reason == "rpc_invalid"


def test_a_non_terminal_remote_state_returns_a_pollable_handle():
    """This is what makes the 30s RPC timeout safe rather than lossy."""
    body = _rpc_ok(text="", state="working", task_id="remote-42")
    result = _call(client_factory=_two_hop(rpc=body))
    assert result.state == "working"
    assert result.task_id == "remote-42"


# --------------------------------------------------------------------------- #
# 9. Credential redaction on the way back (F4/H1/H2)
# --------------------------------------------------------------------------- #
NON_PATTERN_CREDENTIAL = "Zq7bT2mWpXk9Lr4vNc8yHd3Ge6Ju1Ai5Ro0Sf2Bz"


def test_a_non_pattern_credential_reflected_by_the_peer_is_redacted():
    """`sanitize_text` matches PATTERNS (`sk-`, `ghp_`, `trinity_mcp_`); a
    partner's credential is an arbitrary operator-supplied string matching none
    of them. A leak test written with a `trinity_mcp_`-shaped secret exercises
    only the case that already worked, which is why this one uses 40 random
    characters."""
    body = _rpc_ok(text=f"Thanks! Your token was {NON_PATTERN_CREDENTIAL} by the way.")
    result = _call(credential=NON_PATTERN_CREDENTIAL, client_factory=_two_hop(rpc=body))
    assert NON_PATTERN_CREDENTIAL not in (result.text or "")
    assert "***" in (result.text or "")


def test_a_remote_error_message_is_redacted_too():
    err = {"jsonrpc": "2.0", "id": "x",
           "error": {"code": -32000, "message": f"bad token {NON_PATTERN_CREDENTIAL}"}}
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(credential=NON_PATTERN_CREDENTIAL, client_factory=_two_hop(rpc=err))
    assert NON_PATTERN_CREDENTIAL not in str(exc.value)


def test_a_secret_straddling_the_truncation_boundary_is_still_redacted():
    """Sanitise BEFORE truncating, over a 2x window. A bare `[:cap]` slice can
    cut a secret in half so the redaction no longer matches, publishing the
    surviving prefix (learnings, ent#224)."""
    filler = "a" * (a2a_client.A2A_MAX_RESPONSE_CHARS - 10)
    text, truncated = a2a_client.sanitize_outbound_text(
        filler + NON_PATTERN_CREDENTIAL + "tail", NON_PATTERN_CREDENTIAL
    )
    assert truncated
    assert NON_PATTERN_CREDENTIAL not in text
    assert NON_PATTERN_CREDENTIAL[:20] not in text


def test_an_oversized_response_is_truncated_with_a_visible_marker():
    body = _rpc_ok(text="y" * (a2a_client.A2A_MAX_RESPONSE_CHARS + 4000))
    result = _call(client_factory=_two_hop(rpc=body))
    assert result.truncated is True
    assert "truncated by Trinity" in result.text


def test_url_userinfo_echoed_by_the_peer_is_redacted():
    body = _rpc_ok(text="see https://user:hunter2@internal.example/x")
    result = _call(client_factory=_two_hop(rpc=body))
    assert "hunter2" not in result.text


# --------------------------------------------------------------------------- #
# 10. Caps and deadlines
# --------------------------------------------------------------------------- #
def test_an_oversized_outbound_message_is_refused_before_any_egress():
    seen = []
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(message="z" * (a2a_client.A2A_MAX_MESSAGE_CHARS + 1),
              client_factory=_two_hop(record=seen))
    assert exc.value.reason == "message_too_long"
    assert seen == [], "refused input must not reach the wire"


def test_a_tarpit_trips_the_wall_clock_deadline(monkeypatch):
    """httpx's `read` timeout is PER-READ, so a peer trickling one byte at a
    time resets it forever — and the byte cap does not help, because the
    attacker simply stays under it. Only a wall-clock deadline bounds this."""
    monkeypatch.setattr(a2a_client, "A2A_TOTAL_DEADLINE", 0.15)

    def _factory_slow(timeout):
        class _NeverConnects:
            """A client whose connect never returns — the tarpit's shape,
            reduced to the property under test."""

            async def __aenter__(self_inner):
                await asyncio.sleep(10)
                return self_inner

            async def __aexit__(self_inner, *a):
                return False

        return _NeverConnects()

    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_factory_slow)
    assert exc.value.reason == "timeout"


def test_the_rpc_timeout_stays_below_the_mcp_gateway_abort():
    """The MCP client imposes its own 30-60s gateway timeout, and `client.ts`
    aborts at 40s. If the backend's RPC timeout were higher, the agent would see
    `fetch failed` while the credentialed call completed anyway — a side effect
    with no handle to poll, since `task_id` only comes back on success."""
    assert a2a_client.A2A_RPC_TIMEOUT <= 30.0
    assert a2a_client.A2A_TOTAL_DEADLINE <= 45.0


# --------------------------------------------------------------------------- #
# 11. tasks/get
# --------------------------------------------------------------------------- #
def test_get_task_polls_the_same_endpoint():
    a2a_client.clear_dialect_cache()
    seen = []
    result = asyncio.run(
        a2a_client.get_task(
            endpoint_url=PEER.url, credential="tok", task_id="remote-42",
            validated=PEER, client_factory=_two_hop(record=seen, rpc=_rpc_ok(task_id="remote-42")),
        )
    )
    assert result.task_id == "remote-42"
    body = json.loads(seen[-1].content)
    assert body["method"] == "tasks/get"
    assert body["params"]["id"] == "remote-42"


def test_a_poll_after_an_origin_only_registration_hits_the_endpoint_the_card_named():
    """The cache stores the RESOLVED TARGET, not just the dialect.

    An operator may legitimately register the ORIGIN (the registry field is
    documented as "endpoint OR Agent Card URL"). The first call learns the real
    endpoint from the card's `url`; a poll that re-derived the target from the
    registered URL would POST to `/` instead — a different endpoint, silently,
    and only for origin-registered peers.
    """
    a2a_client.clear_dialect_cache()
    seen = []
    _call(endpoint_url=ORIGIN_ONLY.url, validated=ORIGIN_ONLY,
          client_factory=_two_hop(record=seen))
    assert seen[1].url.path == "/a2a/bot"

    seen2 = []
    asyncio.run(a2a_client.get_task(
        endpoint_url=ORIGIN_ONLY.url, credential="tok", task_id="t-1",
        validated=ORIGIN_ONLY, client_factory=_two_hop(record=seen2),
    ))
    assert [r.method for r in seen2] == ["POST"], "poll re-fetched the card"
    assert seen2[0].url.path == "/a2a/bot", "poll went to the wrong endpoint"


def test_a_cached_dialect_saves_the_poll_a_second_card_fetch():
    """Without the cache every call AND every poll pays a second full egress
    just to re-read one field — and a poll would burn the caller's own rate
    budget doing it."""
    a2a_client.clear_dialect_cache()
    seen = []
    _call(client_factory=_two_hop(record=seen))
    assert [r.method for r in seen] == ["GET", "POST"]

    seen2 = []
    asyncio.run(
        a2a_client.get_task(
            endpoint_url=PEER.url, credential="tok", task_id="t-1",
            validated=PEER, client_factory=_two_hop(record=seen2),
        )
    )
    assert [r.method for r in seen2] == ["POST"], "poll re-fetched the card"


def test_a_card_url_embedding_credentials_is_refused():
    """`_same_origin` compares `hostname`, which strips userinfo, so
    `https://u:p@peer.example.com/a2a` would compare EQUAL to the registered
    origin. `_pinned_url` happens to drop userinfo when it rebuilds the
    authority — but that is a coincidence of one helper, not a decision, and a
    card declaring credentials in its own URL is anomalous either way."""
    card = dict(CARD, url="https://user:pw@peer.example.com/a2a/bot")
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_two_hop(card=card))
    assert exc.value.reason == "card_url_invalid"


def test_the_pinned_url_never_carries_userinfo():
    """Belt for the same property at the other end: whatever the authority
    contained, the URL the socket sees is scheme + IP + port + path."""
    pinned = a2a_client._pinned_url("https://user:pw@peer.example.com/a2a", "93.184.216.34")
    assert "user" not in pinned and "pw" not in pinned
    assert pinned == "https://93.184.216.34/a2a"


# --------------------------------------------------------------------------- #
# 13. Review findings (#736 review pass)
# --------------------------------------------------------------------------- #
def test_a_transport_error_echoing_the_credential_is_scrubbed():
    """The one credential-leak path layer 1.5 did not cover.

    `sanitize_outbound_text` guards the RESPONSE. It does not guard the
    `A2ACallError.detail` built from an httpx exception — and that detail
    reaches the calling LLM through the 502 body and the backend log. h11
    rejects an illegal header value by **echoing it** (verified:
    `LocalProtocolError: Illegal header value b'Bearer …'`), which is exactly
    why `models._validate_pat_secret` exists (ent#109). A stored credential
    carrying a stray line break — the routine paste artifact — therefore turned
    the transport error into a credential disclosure.
    """
    secret = "qKt7Zr2wXn4vLp9sDf1gHj6bYcVmA0eRtUiOpQwZ"

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _json(CARD)
        raise httpx.LocalProtocolError(f"Illegal header value b'Bearer {secret}'")

    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(credential=secret, client_factory=_factory(_handler))
    assert exc.value.reason == "rpc_unreachable"
    assert secret not in exc.value.detail, "the credential reached the error detail"


def test_a_poll_does_not_inherit_a_sibling_endpoints_cached_target():
    """Two endpoints on ONE host are two different trust relationships.

    The negotiated `(dialect, rpc_url)` used to be cached under
    `hostname:port`, so after any call to `https://host/a2a/alice` a poll for
    the separately-registered `https://host/a2a/bob` reused Alice's resolved
    target — delivering **Bob's credential to Alice's endpoint** and polling
    the wrong agent. A multi-tenant peer with per-path agents is exactly the
    deployment the registered-path rule exists to support.
    """
    a2a_client.clear_dialect_cache()
    alice = ValidatedPublicUrl(url="https://peer.example.com/a2a/alice",
                               hostname="peer.example.com", port=443,
                               addresses=("93.184.216.34",))
    bob = ValidatedPublicUrl(url="https://peer.example.com/a2a/bob",
                             hostname="peer.example.com", port=443,
                             addresses=("93.184.216.34",))

    def _peer_at(path):
        return _two_hop(card=dict(CARD, url=f"https://peer.example.com{path}"))

    _call(endpoint_url=alice.url, credential="ALICE", validated=alice,
          client_factory=_peer_at("/a2a/alice"))

    seen = []
    asyncio.run(a2a_client.get_task(
        endpoint_url=bob.url, credential="BOB", task_id="t-1", validated=bob,
        client_factory=_two_hop(card=dict(CARD, url="https://peer.example.com/a2a/bob"),
                                record=seen),
    ))
    posts = [r for r in seen if r.method == "POST"]
    assert posts, "the poll issued no request"
    assert posts[0].url.path == "/a2a/bob", "Bob's poll reused Alice's cached target"
    assert posts[0].headers["authorization"] == "Bearer BOB"


@pytest.mark.parametrize("declared", [
    "https://[::1",              # unterminated IPv6 authority — urlsplit raises
    "https://peer.example.com:notaport/a2a/bot",
])
def test_a_malformed_card_url_is_refused_by_name_not_raised(declared):
    """Every field of the card is peer-controlled, so a malformed one must
    produce a named refusal, never an unhandled `ValueError` — which escapes
    the client, the orchestrator's `except A2ACallError`, and the router's
    error map, and surfaces as a peer-triggerable HTTP 500."""
    card = dict(CARD, url=declared)
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_two_hop(card=card))
    assert exc.value.reason in {"card_url_invalid", "card_origin_mismatch"}


def test_same_origin_agrees_with_the_validators_host_canonicalisation():
    """One normalisation, used everywhere — the SV-7 rule, which the card
    comparison was left out of.

    `_validate_public_https_url` canonicalises the host to its IDNA A-label and
    resolves THAT. `_same_origin` compared the raw strings, so an IDN peer
    declaring the punycode form its own server emits — the normal case — was
    refused as a cross-origin card against a registered U-label URL.
    """
    assert a2a_client._same_origin(
        "https://xn--e1afmkfd.com/a2a", "https://пример.com/a2a"
    )
    # …and the reverse direction.
    assert a2a_client._same_origin(
        "https://пример.com/a2a", "https://xn--e1afmkfd.com/a2a"
    )
    # Still not equal when the hosts genuinely differ.
    assert not a2a_client._same_origin(
        "https://xn--e1afmkfd.com/a2a", "https://other.example.com/a2a"
    )


def test_the_card_fetch_uses_its_own_shorter_timeout():
    """`A2A_CARD_FETCH_TIMEOUT` was declared, commented "whole card fetch", and
    never applied — both hops ran on the RPC client's 30 s budget.

    The arithmetic the total deadline was built on (10 s card + 30 s RPC + slack
    = 45 s) only holds if the card really gets 10 s: otherwise a slow card can
    eat 30 s of the 45 s window and leave the CREDENTIALED send 15 s. A cap that
    is declared and unwired is worse than one that was never claimed.
    """
    seen = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.extensions.get("timeout")))
        return _json(CARD if request.method == "GET" else _rpc_ok())

    _call(client_factory=_factory(_handler))
    card_timeout, rpc_timeout = seen[0][1], seen[1][1]
    assert card_timeout["read"] == a2a_client.A2A_CARD_FETCH_TIMEOUT
    assert rpc_timeout["read"] == a2a_client.A2A_RPC_TIMEOUT
    assert a2a_client.A2A_CARD_FETCH_TIMEOUT < a2a_client.A2A_RPC_TIMEOUT
    # The deadline must still cover both hops end to end.
    assert (a2a_client.A2A_CARD_FETCH_TIMEOUT + a2a_client.A2A_RPC_TIMEOUT
            <= a2a_client.A2A_TOTAL_DEADLINE)


# --------------------------------------------------------------------------- #
# 9. x402 / payment over the real transport (#3185)
#
# The pure functions (codec, bounded block, outcome classification) are proven
# in `test_3185_a2a_payment_outcome.py`. What can ONLY be proven here is what
# goes ON THE WIRE and what comes back off it: the envelope a `payment_token`
# endpoint produces, the fact that an `api_key` endpoint's envelope did not
# change by one byte, and that a 402 survives the refusal order that used to
# report it as an outage.
# --------------------------------------------------------------------------- #
import base64  # noqa: E402

PAYMENT_TOKEN_OBJ = {
    "x402Version": 2,
    "scheme": "exact",
    "network": "base-sepolia",
    "payload": {"signature": "0x" + "ab" * 32, "authorization": {"nonce": "0x" + "cd" * 16}},
}
PAYMENT_TOKEN = base64.urlsafe_b64encode(
    json.dumps(PAYMENT_TOKEN_OBJ, separators=(",", ":")).encode()
).decode().rstrip("=")

REQS = {
    "x402Version": 2,
    "error": "payment_required",
    "resource": {"url": "https://peer.example.com/a2a/bot", "description": "One review"},
    "accepts": [{"scheme": "exact", "network": "base-sepolia", "planId": "plan_42"}],
}


def _required_header(obj=None) -> str:
    return base64.b64encode(json.dumps(obj if obj is not None else REQS).encode()).decode()


def _streaming_response(status, body: bytes, headers=None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, stream=httpx.ByteStream(body))


def _card_then(rpc_response, record=None):
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _json(CARD)
        return rpc_response(request) if callable(rpc_response) else rpc_response

    return _factory(_handler, record=record)


# --- what we SEND ---------------------------------------------------------- #
def test_a_payment_token_rides_the_message_metadata_and_the_header():
    """§6.4 / T2: BOTH carriages on ONE request. Waiting for a 402 before
    trying the other would be an automatic retry, which AC2 forbids."""
    seen = []
    a2a_client.clear_dialect_cache()
    _call(credential=PAYMENT_TOKEN, credential_kind="payment_token",
          client_factory=_two_hop(record=seen))
    rpc = seen[1]
    body = json.loads(rpc.content)
    metadata = body["params"]["message"]["metadata"]
    assert metadata["x402.payment.status"] == "payment-submitted"
    # The DECODED payload — the in-band rail carries the object, not the blob.
    assert metadata["x402.payment.payload"] == PAYMENT_TOKEN_OBJ
    assert rpc.headers["payment-signature"] == PAYMENT_TOKEN
    # The Bearer header is still there: a peer may authenticate the caller and
    # charge separately, and removing it would change an unrelated contract.
    assert rpc.headers["authorization"] == f"Bearer {PAYMENT_TOKEN}"


def test_an_api_key_endpoint_sends_byte_identical_bytes_to_today():
    """The additive-safety proof. Every endpoint registered before #3185 has no
    `credential_kind`, so this is the envelope the whole installed base gets.

    Literal byte equality is impossible (the rpc id and messageId are uuid4s),
    so the assertion is: the same header SET, the same `Authorization` value,
    and NO `metadata` key anywhere on the message."""
    seen = []
    a2a_client.clear_dialect_cache()
    _call(client_factory=_two_hop(record=seen))
    rpc = seen[1]
    body = json.loads(rpc.content)
    assert "metadata" not in body["params"]["message"]
    assert "payment-signature" not in {k.lower() for k in rpc.headers.keys()}
    assert rpc.headers["authorization"] == "Bearer tok"
    assert set(body["params"]["message"]) == {"role", "parts", "messageId"}


def test_an_opaque_payment_token_degrades_to_the_header_only():
    """Decision 23: a token we cannot decode is still SENT — the x402 header
    path is exactly what works today. What must not happen is announcing
    undecodable bytes in-band as `x402.payment.payload`."""
    seen = []
    a2a_client.clear_dialect_cache()
    _call(credential="opaque-not-base64-json", credential_kind="payment_token",
          client_factory=_two_hop(record=seen))
    rpc = seen[1]
    assert "metadata" not in json.loads(rpc.content)["params"]["message"]
    assert rpc.headers["payment-signature"] == "opaque-not-base64-json"


def test_the_deprecated_header_can_be_switched_off_in_one_place(monkeypatch):
    """S2: the fallback must be REMOVABLE, so the constant is the only thing a
    future PR has to flip (and the removal note says when)."""
    monkeypatch.setattr(a2a_client, "A2A_SEND_PAYMENT_SIGNATURE_HEADER", False)
    seen = []
    a2a_client.clear_dialect_cache()
    _call(credential=PAYMENT_TOKEN, credential_kind="payment_token",
          client_factory=_two_hop(record=seen))
    rpc = seen[1]
    assert "payment-signature" not in {k.lower() for k in rpc.headers.keys()}
    # The in-band rail still carries it.
    assert json.loads(rpc.content)["params"]["message"]["metadata"]["x402.payment.payload"]


def test_the_card_fetch_never_carries_the_payment_token():
    seen = []
    a2a_client.clear_dialect_cache()
    _call(credential=PAYMENT_TOKEN, credential_kind="payment_token",
          client_factory=_two_hop(record=seen))
    card = seen[0]
    assert "payment-signature" not in {k.lower() for k in card.headers.keys()}
    assert "authorization" not in {k.lower() for k in card.headers.keys()}


def test_the_poll_path_sends_the_header_only_because_it_has_no_message(monkeypatch):
    """F2: `tasks/get` sends `{"id": …}` — there is no message to hang metadata
    on, which is also why the header constant cannot be flipped yet."""
    seen = []
    a2a_client.clear_dialect_cache()
    asyncio.run(a2a_client.get_task(
        endpoint_url=PEER.url, credential=PAYMENT_TOKEN,
        credential_kind="payment_token", task_id="t-1",
        validated=PEER, client_factory=_two_hop(record=seen),
    ))
    rpc = seen[-1]
    body = json.loads(rpc.content)
    assert body["method"] == "tasks/get"
    assert body["params"] == {"id": "t-1"}
    assert rpc.headers["payment-signature"] == PAYMENT_TOKEN


# --- what we READ: the HTTP 402/403 rail ----------------------------------- #
def test_a_402_with_the_payment_required_header_becomes_a_payment_required_outcome():
    a2a_client.clear_dialect_cache()
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_card_then(
            _streaming_response(402, b"{}", {"payment-required": _required_header()})))
    assert exc.value.reason == "payment_required"
    assert exc.value.remote_status == 402
    assert exc.value.payment["summary"]["plan_id"] == "plan_42"


def test_a_402_body_is_read_even_though_other_error_bodies_are_not():
    """The ONE departure from "never read an error body", and the reason: the
    price lives nowhere else."""
    a2a_client.clear_dialect_cache()
    body = json.dumps({"detail": "Payment required",
                       "payment_required": REQS,
                       "credits_per_request": 5}).encode()
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_card_then(_streaming_response(402, body)))
    assert exc.value.reason == "payment_required"
    assert exc.value.payment["summary"]["credits_per_request"] == 5


def test_a_gzipped_402_is_still_a_402_and_not_an_outage():
    """S8/F1 — the defect the first plan pass had. The encoding guard runs
    before the status check, so a CDN-compressed "pay me" reported
    `rpc_encoding`: an outage, for a priced endpoint working perfectly.

    The body is never decoded (that rule is absolute). The HEADER is read, and
    when there is no header the status alone still carries the outcome."""
    a2a_client.clear_dialect_cache()
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_card_then(_streaming_response(
            402, gzip.compress(b'{"detail":"pay"}'),
            {"content-encoding": "gzip", "payment-required": _required_header()})))
    assert exc.value.reason == "payment_required"
    assert exc.value.payment["summary"]["plan_id"] == "plan_42"


def test_an_oversized_402_body_is_dropped_rather_than_reported_as_too_large():
    """S8: the declared-length check used the 1 MiB answer cap, so a 100 KiB
    "pay me" tripped `rpc_too_large` and the agent never learned payment was
    required. The body is refused; the OUTCOME survives, flagged truncated."""
    a2a_client.clear_dialect_cache()
    huge = json.dumps({"detail": "pay up", "padding": "x" * 200_000}).encode()
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_card_then(_streaming_response(402, huge)))
    assert exc.value.reason == "payment_required"
    assert exc.value.payment["truncated"] is True


def test_a_402_declaring_an_oversized_length_is_not_read_at_all():
    a2a_client.clear_dialect_cache()
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_card_then(_streaming_response(
            402, b'{"detail":"pay"}',
            {"content-length": str(a2a_client.A2A_ERROR_BODY_MAX_BYTES + 1)})))
    assert exc.value.reason == "payment_required"
    assert exc.value.payment["truncated"] is True


def test_a_403_to_a_payment_token_endpoint_is_payment_rejected_over_the_wire():
    a2a_client.clear_dialect_cache()
    body = json.dumps({"detail": "Payment verification failed",
                       "error": "BCK.X402.0059"}).encode()
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(credential=PAYMENT_TOKEN, credential_kind="payment_token",
              client_factory=_card_then(_streaming_response(403, body)))
    assert exc.value.reason == "payment_rejected"
    assert exc.value.remote_status == 403


def test_a_403_to_an_api_key_endpoint_is_rpc_forbidden_over_the_wire():
    a2a_client.clear_dialect_cache()
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_card_then(_streaming_response(403, b"nope")))
    assert exc.value.reason == "rpc_forbidden"
    assert exc.value.remote_status == 403


def test_a_402_that_echoes_the_token_back_is_redacted_on_the_way_out():
    """The expired-token case: the peer quotes what we sent it."""
    a2a_client.clear_dialect_cache()
    signature = PAYMENT_TOKEN_OBJ["payload"]["signature"]
    body = json.dumps({"detail": f"signature {signature} is spent",
                       "payment_required": {**REQS, "error": f"token {PAYMENT_TOKEN} spent"}}).encode()
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(credential=PAYMENT_TOKEN, credential_kind="payment_token",
              client_factory=_card_then(_streaming_response(402, body)))
    rendered = json.dumps(exc.value.payment) + exc.value.detail
    assert PAYMENT_TOKEN not in rendered
    assert signature not in rendered


@pytest.mark.parametrize("status", [400, 401, 404, 429, 500, 503])
def test_every_other_status_keeps_todays_reason_and_gains_remote_status(status):
    """Decision 8/18: one line, and every 4xx/5xx becomes diagnosable. The
    body of these is still NOT read."""
    a2a_client.clear_dialect_cache()
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_card_then(_streaming_response(status, b'{"detail":"x"}')))
    assert exc.value.reason == "rpc_http_error"
    assert exc.value.remote_status == status
    assert exc.value.payment is None


def test_a_card_402_stays_a_card_http_error():
    """Decision 10: the card hop is uncredentialed by design, so a gated card
    cannot be paid for from here. The body-reading branch is keyed on the RPC
    hop precisely so this contract is untouched."""
    a2a_client.clear_dialect_cache()

    def _handler(request: httpx.Request) -> httpx.Response:
        return _streaming_response(402, b"{}", {"payment-required": _required_header()})

    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_factory(_handler))
    assert exc.value.reason == "card_http_error"
    assert exc.value.remote_status == 402
    assert exc.value.payment is None


def test_a_402_redirect_is_still_refused_as_a_redirect():
    """The redirect guard stays FIRST: a 3xx is an SSRF bypass question, which
    outranks "what does this cost"."""
    a2a_client.clear_dialect_cache()
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_card_then(_streaming_response(
            302, b"", {"location": "https://elsewhere.example.com/"})))
    assert exc.value.reason == "rpc_redirect"


# --- what we READ: the in-band rail ---------------------------------------- #
def _inband(status_value, extra=None, state="input-required"):
    metadata = {"x402.payment.status": status_value}
    metadata.update(extra or {})
    return {"jsonrpc": "2.0", "id": "x", "result": {
        "id": "t-77", "contextId": "c-1", "kind": "task",
        "status": {"state": state,
                   "message": {"role": "agent", "parts": [
                       {"kind": "text", "text": "please pay"}], "metadata": metadata}},
    }}


def test_an_in_band_payment_required_task_is_refused_not_handed_over_as_a_prompt():
    a2a_client.clear_dialect_cache()
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_card_then(
            _json(_inband("payment-required", {"x402.payment.required": REQS}))))
    assert exc.value.reason == "payment_required"
    assert exc.value.task_id == "t-77"
    assert exc.value.payment["summary"]["plan_id"] == "plan_42"


def test_an_in_band_payment_failure_is_payment_rejected():
    a2a_client.clear_dialect_cache()
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call(client_factory=_card_then(_json(_inband(
            "payment-failed",
            {"x402.payment.error": {"code": "BCK.X402.0059", "reason": "spent"}}))))
    assert exc.value.reason == "payment_rejected"


def test_the_poll_path_runs_the_same_in_band_check(monkeypatch):
    """Decision 7: without this, polling a priced task hands the agent "pay me"
    as an `input-required` prompt it will poll forever."""
    a2a_client.clear_dialect_cache()
    with pytest.raises(a2a_client.A2ACallError) as exc:
        asyncio.run(a2a_client.get_task(
            endpoint_url=PEER.url, credential="tok", task_id="t-77",
            validated=PEER,
            client_factory=_card_then(
                _json(_inband("payment-required", {"x402.payment.required": REQS}))),
        ))
    assert exc.value.reason == "payment_required"


def test_payment_completed_is_recorded_on_the_result_and_receipts_are_dropped():
    """Decision 31/9: money leaving must be visible to the OPERATOR, so it
    rides `A2AResult.payment_status` (activity + audit). The receipts are not
    surfaced — no consumer, and the response allowlist does not grow."""
    a2a_client.clear_dialect_cache()
    reply = _inband("payment-completed",
                    {"x402.payment.receipts": [{"txHash": "0xfeedface"}]},
                    state="completed")
    result = _call(client_factory=_card_then(_json(reply)))
    assert result.state == "completed"
    assert result.payment_status == "payment-completed"
    assert "0xfeedface" not in (result.text or "")


def test_an_ordinary_answer_reports_no_payment_status():
    a2a_client.clear_dialect_cache()
    result = _call(client_factory=_two_hop())
    assert result.payment_status is None
