"""Issue #181 / pentest 3.2.4 — a spoofed X-Forwarded-For cannot pick the rate-limit key.

Moved here from the tests/ root (where no per-PR job collected it) by
trinity-enterprise#873, which also replaced the contract it pinned. The old
`routers.public._get_client_ip` trusted every RFC-1918 peer by default — so an
AGENT could choose its own key — and returned any string found in a header
(`real.client.1.2` was an asserted result). Identity is now resolved once per
request by `utils/client_ip.ClientAddressMiddleware`, and `_get_client_ip` is a
thin reader of the normalised peer. These tests drive the real middleware in
front of the real wrapper.
"""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.unit


def _client_ip_through_middleware(peer, headers, env):
    """What `_get_client_ip` returns for a request from `peer` carrying
    `headers`, after the production middleware ran. Returns the HTTP status
    instead when the middleware refuses the request."""
    from starlette.requests import Request

    from routers.public import _get_client_ip
    from utils import client_ip as ci

    config = ci.load_config(env)
    seen = {}

    async def app(scope, receive, send):
        seen["ip"] = _get_client_ip(Request(scope))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request"}

    scope = {"type": "http", "method": "GET", "path": "/", "headers":
             [(k.lower().encode(), v.encode()) for k, v in headers.items()],
             "client": (peer, 5555) if peer else None, "query_string": b""}
    asyncio.run(ci.ClientAddressMiddleware(app, config=config)(scope, receive, send))
    return seen.get("ip", sent[0]["status"])


BUNDLED = {"TRINITY_BUNDLED_PROXY_TRUST": "1", "TRINITY_PROXY_PREFIX": "172.30.0"}


def test_direct_connection_ignores_xff():
    assert _client_ip_through_middleware("1.2.3.4", {"X-Forwarded-For": "10.0.0.99"}, BUNDLED) == "1.2.3.4"


def test_pentest_bypass_scenario_rotating_xff_from_a_public_peer():
    """The PoC: 35 requests, each with a different forged X-Forwarded-For."""
    keys = {_client_ip_through_middleware("1.2.3.4", {"X-Forwarded-For": f"10.0.0.{i}"}, BUNDLED)
            for i in range(1, 36)}
    assert keys == {"1.2.3.4"}


@pytest.mark.parametrize("peer", ["172.17.0.2", "172.28.0.9", "192.168.1.1", "10.0.0.1", "127.0.0.1"])
def test_a_private_peer_is_no_longer_trusted_by_default(peer):
    """Replaced contract: being private never made a peer a proxy. An agent on
    the agent network is exactly such a peer."""
    got = _client_ip_through_middleware(peer, {"X-Real-IP": "5.6.7.8", "X-Forwarded-For": "5.6.7.8"},
                                        BUNDLED)
    assert got == peer


def test_x_real_ip_is_never_read_even_from_the_trusted_proxy():
    got = _client_ip_through_middleware("172.30.0.2", {"X-Real-IP": "5.6.7.8",
                                                       "X-Forwarded-For": "9.10.11.12"}, BUNDLED)
    assert got == "9.10.11.12"


def test_trusted_proxy_rightmost_untrusted_entry():
    got = _client_ip_through_middleware(
        "198.51.100.10", {"X-Forwarded-For": "6.6.6.6, 9.10.11.12, 198.51.100.11"},
        {"TRUSTED_PROXIES": "198.51.100.10,198.51.100.11"})
    assert got == "9.10.11.12"


def test_a_non_address_entry_is_refused_not_returned():
    """Replaced contract: `real.client.1.2` used to BE the key."""
    got = _client_ip_through_middleware(
        "172.30.0.2", {"X-Forwarded-For": "spoofed.1.1.1, real.client.1.2"}, BUNDLED)
    assert got == 400


def test_no_client_returns_unknown():
    assert _client_ip_through_middleware(None, {"X-Forwarded-For": "5.6.7.8"}, BUNDLED) == "unknown"


def test_per_token_limit_constants():
    """Per-token limit must be >= per-IP limit (a token covers all IPs)."""
    from routers import public as pub

    assert pub.MAX_CHAT_MESSAGES_PER_TOKEN > 0
    assert pub.MAX_CHAT_MESSAGES_PER_TOKEN >= pub.MAX_CHAT_MESSAGES_PER_IP
