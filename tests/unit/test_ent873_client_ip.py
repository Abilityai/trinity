"""trinity-enterprise#873 — the client-address resolver and its ASGI middleware.

`utils/client_ip.py` decides, once per request, which address a request is
attributed to. Each rule below is pinned by a behaviour assertion, paired with
the control that shows the same headers do nothing from an untrusted peer.
The route-level proof (real `POST /api/token`, real limiter keys) lives in
`test_ent873_signin_client_ip.py`; this file owns the edges.

Addresses: `172.30.0.2` bundled nginx, `.3` bundled cloudflared, `.1` the bridge
gateway (never trusted); clients from the documentation ranges.
"""
from __future__ import annotations

import asyncio

import pytest

from utils import client_ip as ci

pytestmark = pytest.mark.unit

FRONTEND, CLOUDFLARED, GATEWAY = "172.30.0.2", "172.30.0.3", "172.30.0.1"
CLIENT, OTHER, FORGED, AGENT = "198.51.100.40", "203.0.113.9", "10.1.2.3", "172.28.0.77"

BUNDLED = ci.load_config({"TRINITY_BUNDLED_PROXY_TRUST": "1", "TRINITY_PROXY_PREFIX": "172.30.0"})


def _h(*pairs):
    return [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in pairs]


def _resolve(peer, *pairs, config=BUNDLED):
    return ci.resolve(config, peer, _h(*pairs))


# ---------------------------------------------------------------------------
# Untrusted peers — headers never count
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("peer", [CLIENT, AGENT, GATEWAY, "127.0.0.1", "10.0.0.5"])
def test_untrusted_peer_is_its_own_address_whatever_it_sends(peer):
    r = _resolve(peer, ("X-Forwarded-For", FORGED), ("X-Real-IP", FORGED),
                 ("CF-Connecting-IP", FORGED), ("Forwarded", f"for={FORGED}"))
    assert (r.address, r.provenance) == (peer, "peer")


def test_malformed_identity_from_an_untrusted_peer_is_ignored_not_refused():
    r = _resolve(CLIENT, ("X-Forwarded-For", "garbage, ,"), ("CF-Connecting-IP", "x"),
                 ("CF-Connecting-IP", "y"))
    assert (r.address, r.provenance) == (CLIENT, "peer")


def test_no_forwarding_trust_without_the_bundled_flag():
    r = _resolve(FRONTEND, ("X-Forwarded-For", CLIENT), config=ci.load_config({}))
    assert (r.address, r.provenance) == (FRONTEND, "peer")


@pytest.mark.parametrize("peer", ["", "testclient", "garbage"])
def test_a_peer_that_is_not_an_address_supplies_no_identity(peer):
    assert _resolve(peer, ("X-Forwarded-For", CLIENT)) is None


# ---------------------------------------------------------------------------
# Trusted nginx — the authenticated right-hand suffix
# ---------------------------------------------------------------------------

def test_the_rightmost_untrusted_entry_wins_and_the_left_prefix_is_ignored():
    r = _resolve(FRONTEND, ("X-Forwarded-For", f"{FORGED}, {OTHER}, {CLIENT}"))
    assert (r.address, r.provenance) == (CLIENT, "xff")


def test_private_and_loopback_clients_are_accepted_but_never_skipped():
    """Being private does not make an address a proxy: the walk stops there."""
    assert _resolve(FRONTEND, ("X-Forwarded-For", f"{CLIENT}, 10.20.30.40")).address == "10.20.30.40"
    assert _resolve(FRONTEND, ("X-Forwarded-For", f"{CLIENT}, 127.0.0.1")).address == "127.0.0.1"


def test_the_bridge_gateway_is_a_client_never_a_hop():
    """A Caddy that still dials the ordinary listener arrives through the
    gateway; it is attributed to the gateway, never walked past."""
    r = _resolve(FRONTEND, ("X-Forwarded-For", f"{CLIENT}, {GATEWAY}"))
    assert (r.address, r.provenance) == (GATEWAY, "xff")


def test_repeated_fields_are_one_list_read_from_the_last_field_backwards():
    r = _resolve(FRONTEND, ("X-Forwarded-For", f"{FORGED}, {OTHER}"), ("X-Forwarded-For", CLIENT))
    assert r.address == CLIENT
    # And trusted hops at the end of the last field continue into the one before.
    r = _resolve(FRONTEND, ("X-Forwarded-For", OTHER), ("X-Forwarded-For", FRONTEND))
    assert r.address == OTHER


@pytest.mark.parametrize("raw,canonical", [
    ("2001:DB8:0:0:0:0:0:1", "2001:db8::1"),
    ("::ffff:198.51.100.40", "198.51.100.40"),
    ("240.1.2.3", "240.1.2.3"),            # Cloudflare pseudo-IPv4
    (" \t198.51.100.40\t ", "198.51.100.40"),
])
def test_addresses_are_canonicalised(raw, canonical):
    assert _resolve(FRONTEND, ("X-Forwarded-For", f"{FORGED},{raw}")).address == canonical


def test_a_mapped_ipv6_peer_is_recognised_as_the_trusted_proxy():
    r = _resolve("::ffff:172.30.0.2", ("X-Forwarded-For", CLIENT))
    assert (r.address, r.provenance) == (CLIENT, "xff")


def test_no_forwarded_field_keeps_the_proxy_with_fallback_provenance():
    r = _resolve(FRONTEND)
    assert (r.address, r.provenance) == (FRONTEND, "proxy_fallback")


@pytest.mark.parametrize("value,why", [
    ("", "empty"),
    (f"{CLIENT}, ", "empty"),
    ("client.example.com", "malformed"),
    (f"{CLIENT}:443", "malformed"),
    (f"[{'2001:db8::1'}]", "malformed"),
    ("fe80::1%eth0", "malformed"),
    ("0.0.0.0", "malformed"),
    ("224.0.0.1", "malformed"),
    ("unknown", "malformed"),
    (f"{CLIENT}\x01", "malformed"),
])
def test_unusable_required_identity_is_refused(value, why):
    with pytest.raises(ci.IdentityRefused) as exc:
        _resolve(FRONTEND, ("X-Forwarded-For", value))
    assert why in exc.value.reason


def test_a_chain_of_only_trusted_proxies_is_refused():
    with pytest.raises(ci.IdentityRefused, match="only trusted"):
        _resolve(FRONTEND, ("X-Forwarded-For", f"{FRONTEND}, {FRONTEND}"))


def test_an_oversized_unused_left_prefix_is_not_a_reason_to_refuse():
    junk = ",".join(["garbage"] * 5000)          # ~40 KiB the walk never reads
    r = _resolve(FRONTEND, ("X-Forwarded-For", f"{junk}, {CLIENT}"))
    assert (r.address, r.provenance) == (CLIENT, "xff")


def test_a_suffix_that_exhausts_its_element_budget_is_refused():
    ops = ci.load_config({"TRUSTED_PROXIES": "192.0.2.0/24"})
    hops = ", ".join(f"192.0.2.{i}" for i in range(1, ci.MAX_SUFFIX_ELEMENTS + 2))
    with pytest.raises(ci.IdentityRefused, match="budget"):
        _resolve("192.0.2.250", ("X-Forwarded-For", f"{CLIENT}, {hops}"), config=ops)
    # One element under the budget still resolves.
    hops = ", ".join(f"192.0.2.{i}" for i in range(1, ci.MAX_SUFFIX_ELEMENTS))
    assert _resolve("192.0.2.250", ("X-Forwarded-For", f"{CLIENT}, {hops}"), config=ops).address == CLIENT


def test_a_suffix_that_exhausts_its_byte_budget_is_refused():
    ops = ci.load_config({"TRUSTED_PROXIES": "2001:db8:ffff::/48"})
    hop = "2001:db8:ffff:ffff:ffff:ffff:ffff:fffe"
    hops = ", ".join([hop] * 31)  # 31 elements, ~1.2 KiB: under both budgets
    assert _resolve(hop, ("X-Forwarded-For", f"{CLIENT}, {hops}"), config=ops).address == CLIENT
    padded = f"{CLIENT}, " + ", ".join(["  " * 60 + hop] * 31)  # same count, > 4 KiB
    with pytest.raises(ci.IdentityRefused, match="budget"):
        _resolve(hop, ("X-Forwarded-For", padded), config=ops)


# ---------------------------------------------------------------------------
# Cloudflare Tunnel
# ---------------------------------------------------------------------------

def test_direct_tunnel_hop_uses_cf_connecting_ip_and_ignores_xff():
    r = _resolve(CLOUDFLARED, ("CF-Connecting-IP", CLIENT), ("X-Forwarded-For", FORGED))
    assert (r.address, r.provenance) == (CLIENT, "cf")


def test_tunnel_through_nginx_uses_cf_once_the_suffix_reaches_cloudflared():
    r = _resolve(FRONTEND, ("CF-Connecting-IP", CLIENT),
                 ("X-Forwarded-For", f"{FORGED}, {CLOUDFLARED}"))
    assert (r.address, r.provenance) == (CLIENT, "cf")


def test_cf_header_is_ignored_unless_the_suffix_reaches_cloudflared():
    r = _resolve(FRONTEND, ("CF-Connecting-IP", FORGED), ("X-Forwarded-For", CLIENT))
    assert (r.address, r.provenance) == (CLIENT, "xff")


def test_absent_cf_identity_keeps_the_original_peer_never_the_xff_prefix():
    r = _resolve(CLOUDFLARED, ("X-Forwarded-For", FORGED))
    assert (r.address, r.provenance) == (CLOUDFLARED, "proxy_fallback")
    r = _resolve(FRONTEND, ("X-Forwarded-For", f"{FORGED}, {CLOUDFLARED}"))
    assert (r.address, r.provenance) == (FRONTEND, "proxy_fallback")


@pytest.mark.parametrize("fields,why", [
    ([("CF-Connecting-IP", "")], "empty"),
    ([("CF-Connecting-IP", "not-an-ip")], "malformed"),
    ([("CF-Connecting-IP", CLIENT), ("CF-Connecting-IP", OTHER)], "duplicate"),
])
def test_unusable_cf_identity_is_refused(fields, why):
    with pytest.raises(ci.IdentityRefused) as exc:
        _resolve(CLOUDFLARED, *fields)
    assert why in exc.value.reason


def test_cf_pseudo_ipv4_is_a_valid_client():
    assert _resolve(CLOUDFLARED, ("CF-Connecting-IP", "240.0.0.9")).address == "240.0.0.9"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_bundled_trust_reserves_dot_two_and_dot_three_of_the_prefix():
    c = ci.load_config({"TRINITY_BUNDLED_PROXY_TRUST": "1", "TRINITY_PROXY_PREFIX": "172.31.40"})
    assert (str(c.frontend), str(c.cloudflared), str(c.proxy_subnet)) == (
        "172.31.40.2", "172.31.40.3", "172.31.40.0/24")
    assert not c.is_trusted(ci.parse_literal("172.31.40.1"))      # the gateway
    assert not c.is_trusted(ci.parse_literal("172.31.40.130"))    # e.g. the MCP server
    assert ci.load_config({}).proxy_subnet is None


def test_operator_proxies_are_trusted_exactly_as_listed():
    c = ci.load_config({"TRUSTED_PROXIES": "192.0.2.10, 2001:db8:aa::/64"})
    assert c.is_trusted(ci.parse_literal("192.0.2.10"))
    assert c.is_trusted(ci.parse_literal("2001:db8:aa::5"))
    assert not c.is_trusted(ci.parse_literal("192.0.2.11"))
    r = ci.resolve(c, "192.0.2.10", _h(("X-Forwarded-For", f"{FORGED}, {CLIENT}")))
    assert (r.address, r.provenance) == (CLIENT, "xff")


@pytest.mark.parametrize("env,needle", [
    ({"TRUSTED_PROXIES": "*"}, "'*'"),
    ({"TRUSTED_PROXIES": "192.0.2.10, 0.0.0.0/0"}, "'0.0.0.0/0'"),
    ({"TRUSTED_PROXIES": "::/0"}, "'::/0'"),
    ({"TRUSTED_PROXIES": "127.0.0.1"}, "loopback"),
    ({"TRUSTED_PROXIES": "::1"}, "loopback"),
    ({"TRUSTED_PROXIES": "proxy.example.com"}, "'proxy.example.com'"),
    ({"TRUSTED_PROXIES": "172.16.0.0/12"}, "172.28.0.0/16"),
    ({"TRUSTED_PROXIES": "172.29.0.9"}, "172.29.0.0/16"),
    ({"TRINITY_BUNDLED_PROXY_TRUST": "1", "TRUSTED_PROXIES": "172.30.0.1"}, "172.30.0.0/24"),
    ({"TRINITY_BUNDLED_PROXY_TRUST": "1", "TRINITY_PROXY_PREFIX": "172.30"}, "three decimal"),
    ({"TRINITY_BUNDLED_PROXY_TRUST": "1", "TRINITY_PROXY_PREFIX": "172.030.0"}, "three decimal"),
    ({"TRINITY_BUNDLED_PROXY_TRUST": "1", "TRINITY_PROXY_PREFIX": "172.28.4"}, "overlaps"),
    ({"TRINITY_BUNDLED_PROXY_TRUST": "maybe"}, "expected 1 or 0"),
])
def test_invalid_configuration_is_refused_naming_the_entry(env, needle):
    with pytest.raises(ci.ClientIPConfigError) as exc:
        ci.load_config(env)
    assert needle in str(exc.value)


def test_the_backend_refuses_to_start_on_an_invalid_list(tmp_path):
    """Startup validation: importing the app with a bad list fails loudly."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(ci.__file__).resolve().parents[1]
    env = {**os.environ, "TRUSTED_PROXIES": "10.0.0.0/8, 0.0.0.0/0",
           "TRINITY_DB_PATH": str(tmp_path / "t.db")}
    proc = subprocess.run([sys.executable, "-c", "import main"], cwd=backend, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode != 0
    assert "ClientIPConfigError" in proc.stderr and "'0.0.0.0/0'" in proc.stderr


# ---------------------------------------------------------------------------
# The middleware: scope mutation, headers, HTTP and WebSocket
# ---------------------------------------------------------------------------

def _echo_app(record):
    async def app(scope, receive, send):
        record.append(scope)
        if scope["type"] == "websocket":
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": 1000})
            return
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    return app


def _drive(scope_type, peer, headers):
    record, sent = [], []
    mw = ci.ClientAddressMiddleware(_echo_app(record), config=BUNDLED)
    scope = {"type": scope_type, "client": (peer, 41000), "headers": headers,
             "path": "/x", "method": "GET"}

    async def receive():
        return {"type": "websocket.connect"} if scope_type == "websocket" else {"type": "http.request"}

    async def send(msg):
        sent.append(msg)

    asyncio.run(mw(scope, receive, send))
    return scope, record, sent


@pytest.mark.parametrize("scope_type", ["http", "websocket"])
def test_the_scope_is_rewritten_in_place_and_headers_untouched(scope_type):
    headers = _h(("X-Forwarded-For", f"{FORGED}, {CLIENT}"), ("X-Forwarded-Proto", "https"))
    original = list(headers)
    scope, record, _ = _drive(scope_type, FRONTEND, headers)
    assert record and record[0] is scope, "the app must see the SAME scope object"
    assert scope["client"] == (CLIENT, 0)
    assert scope["headers"] == original
    assert scope[ci.SCOPE_KEY] == {"peer": (FRONTEND, 41000), "provenance": "xff"}


def test_an_untrusted_peer_keeps_its_socket_port():
    scope, _, _ = _drive("http", "::ffff:198.51.100.40", _h(("X-Forwarded-For", FORGED)))
    assert scope["client"] == (CLIENT, 41000)


def test_refused_http_identity_is_a_400_before_the_app_runs():
    scope, record, sent = _drive("http", FRONTEND, _h(("X-Forwarded-For", "nonsense")))
    assert record == []
    assert sent[0]["status"] == 400


def test_refused_websocket_identity_closes_1008_before_accept():
    scope, record, sent = _drive("websocket", CLOUDFLARED, _h(("CF-Connecting-IP", "")))
    assert record == []
    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_lifespan_and_other_scopes_pass_through_untouched():
    record = []
    mw = ci.ClientAddressMiddleware(lambda s, r, se: record.append(s) or asyncio.sleep(0),
                                    config=BUNDLED)
    scope = {"type": "lifespan"}
    asyncio.run(mw(scope, None, None))
    assert record == [{"type": "lifespan"}]


def test_registered_immediately_inside_the_host_guard():
    """Outside CORS, request-ID and security headers, so every one of them —
    and every route — sees the resolved address."""
    import main

    names = [m.cls.__name__ for m in main.app.user_middleware]
    assert names[0] == "HostHeaderGuard", names
    assert names[1] == "ClientAddressMiddleware", names


# ---------------------------------------------------------------------------
# Resource bound (code review r2): the parser never touches more than the budget
# ---------------------------------------------------------------------------

class _SpyBytes(bytes):
    """A header value that records how much of itself the parser searches,
    slices or decodes. The budget is a promise about work done, not only about
    the answer, so it is asserted on these spans."""

    def __new__(cls, value):
        obj = super().__new__(cls, value)
        obj.spans = []
        return obj

    def rfind(self, sub, start=0, end=None):
        end = len(self) if end is None else end
        self.spans.append(("search", end - start))
        return super().rfind(sub, start, end)

    def __getitem__(self, key):
        if isinstance(key, slice):
            s, e, _ = key.indices(len(self))
            self.spans.append(("slice", e - s))
        return super().__getitem__(key)

    def decode(self, *a, **k):
        self.spans.append(("decode", len(self)))
        return super().decode(*a, **k)


_MB = 1_000_000


def _largest_span(value):
    return max((n for _, n in value.spans), default=0)


def test_a_megabyte_xff_entry_is_refused_without_reading_past_the_budget():
    spy = _SpyBytes(b"9" * _MB)
    with pytest.raises(ci.IdentityRefused, match="budget"):
        ci.resolve(BUNDLED, FRONTEND, [(b"x-forwarded-for", spy)])
    assert _largest_span(spy) <= ci.MAX_SUFFIX_BYTES + 1, spy.spans


def test_a_megabyte_unread_prefix_costs_only_the_suffix():
    spy = _SpyBytes(b"9" * _MB + b", " + CLIENT.encode())
    assert ci.resolve(BUNDLED, FRONTEND, [(b"x-forwarded-for", spy)]).address == CLIENT
    assert _largest_span(spy) <= ci.MAX_SUFFIX_BYTES + 1, spy.spans


def test_a_megabyte_cf_connecting_ip_is_refused_without_decoding_it():
    spy = _SpyBytes(b"1" * _MB)
    with pytest.raises(ci.IdentityRefused, match="malformed"):
        ci.resolve(BUNDLED, CLOUDFLARED, [(b"cf-connecting-ip", spy)])
    assert _largest_span(spy) <= 64, spy.spans
