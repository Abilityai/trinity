"""trinity-enterprise#873 / #3047 — sign-in limits key on the client the proxy saw.

Per-IP sign-in buckets and their resets are keyed on the canonical client
address the trusted proxy reports, never on the proxy's own address, so each
client's bucket counts only that client's attempts (the #591 split, per
client). These tests drive the REAL application (`main.app`, every middleware included)
over ASGI with a chosen TCP peer, the way uvicorn hands a connection to it, and
read the limiter keys a Redis double actually received. The proxy addresses are
the prod/hosted ingress reservations (`TRINITY_PROXY_PREFIX=172.30.0`): `.2` is
the bundled nginx, `.3` the bundled cloudflared. Client addresses come from the
documentation ranges and differ from every default and from each other.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

pytestmark = pytest.mark.unit

FRONTEND = "172.30.0.2"     # bundled nginx (trusted forwarder)
CLOUDFLARED = "172.30.0.3"  # bundled cloudflared (trusted forwarder)
GATEWAY = "172.30.0.1"      # bridge gateway — what a published port delivers
CLIENT_A = "198.51.100.23"
CLIENT_B = "203.0.113.77"
AGENT = "172.28.0.41"       # an agent container on the agent network
DIRECT = "192.0.2.150"      # an off-box caller hitting the published :8000
FORGED = "10.9.8.7"


# ---------------------------------------------------------------------------
# Redis double — only what the auth limiter uses.
# ---------------------------------------------------------------------------

class _Pipe:
    def __init__(self, r):
        self.r, self.ops = r, []

    def incr(self, k):
        self.ops.append(("incr", k))
        return self

    def expire(self, k, s):
        self.ops.append(("expire", k, s))
        return self

    def execute(self):
        for op in self.ops:
            if op[0] == "incr":
                self.r.store[op[1]] = int(self.r.store.get(op[1], 0)) + 1
            else:
                self.r.ttls[op[1]] = time.monotonic() + op[2]
        self.ops.clear()


class FakeRedis:
    def __init__(self):
        self.store: dict = {}
        self.ttls: dict = {}

    def get(self, k):
        return None if k not in self.store else str(self.store[k])

    def ttl(self, k):
        return int(self.ttls.get(k, time.monotonic()) - time.monotonic())

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)

    def pipeline(self):
        return _Pipe(self)

    def ping(self):
        return True

    def ip_keys(self):
        return {k: v for k, v in self.store.items() if k.startswith("login_attempts_ip:")}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@pytest.fixture
def bundled(monkeypatch):
    """The prod/hosted backend environment: bundled forwarders trusted, no
    operator proxies."""
    monkeypatch.setenv("TRINITY_BUNDLED_PROXY_TRUST", "1")
    monkeypatch.setenv("TRINITY_PROXY_PREFIX", "172.30.0")
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)


def route_globals(path: str, method: str) -> dict:
    """The module namespace the app's route was BUILT from.

    Another unit test may evict and re-import a router module; patching
    `import routers.auth` would then patch a copy the app never calls. The
    endpoint's own `__globals__` is the namespace its code resolves names in.
    """
    import main

    for r in main.app.routes:
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or ()):
            return r.endpoint.__globals__
    raise AssertionError(f"no {method} {path} route")


def _request(method, path, *, peer, headers=None, **kw):
    import main

    async def go():
        transport = httpx.ASGITransport(app=main.app, client=(peer, 40123))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            return await c.request(method, path, headers=headers or {}, **kw)

    return asyncio.run(go())


def _via_nginx(client: str, prefix: str = "") -> dict:
    """What the bundled nginx sends: its observed peer appended to whatever the
    caller supplied (`$proxy_add_x_forwarded_for`), plus `X-Real-IP`."""
    return {"X-Forwarded-For": f"{prefix}, {client}" if prefix else client, "X-Real-IP": client}


GOOD_USER = "owner@example.com"
GOOD_PASSWORD = "right-password-873"


@pytest.fixture
def token_env(monkeypatch):
    """`POST /api/token` with the real limiter over a Redis double; only the
    user store, the DB stamp and the audit writer are stubbed."""
    ra = route_globals("/api/token", "POST")

    redis = FakeRedis()
    monkeypatch.setitem(ra, "_redis_client", redis)
    monkeypatch.setitem(ra, "get_redis_client", lambda: redis)
    monkeypatch.setitem(ra, "is_setup_completed", lambda: True)

    def _authenticate(username, password):
        if username == GOOD_USER and password == GOOD_PASSWORD:
            return {"id": 7, "username": GOOD_USER, "role": "user", "email": GOOD_USER}
        return None

    monkeypatch.setitem(ra, "authenticate_user", _authenticate)
    monkeypatch.setattr(ra["db"], "update_last_login", lambda *a, **k: None)

    async def _log(*a, **k):
        return "evt"

    monkeypatch.setattr(ra["platform_audit_service"], "log", _log)
    return redis


def _login(peer, headers, username, password="wrong-password"):
    return _request("POST", "/api/token", peer=peer, headers=headers,
                    data={"username": username, "password": password})


# ---------------------------------------------------------------------------
# AC1 — one source's failures do not block another source
# ---------------------------------------------------------------------------

def test_each_forwarded_client_has_its_own_bucket(bundled, token_env):
    limit = route_globals("/api/token", "POST")["LOGIN_IP_LIMIT"]

    # Distinct accounts, so the tighter per-account limit cannot be what trips.
    for i in range(limit):
        r = _login(FRONTEND, _via_nginx(CLIENT_A, prefix=FORGED), f"user{i}@example.com")
        assert r.status_code == 401, r.text

    blocked = _login(FRONTEND, _via_nginx(CLIENT_A), "another@example.com")
    assert blocked.status_code == 429, blocked.text
    assert "this network" in blocked.json()["detail"]

    ok = _login(FRONTEND, _via_nginx(CLIENT_B), GOOD_USER, GOOD_PASSWORD)
    assert ok.status_code == 200, ok.text
    assert ok.json()["access_token"]

    keys = token_env.ip_keys()
    assert keys == {f"login_attempts_ip:{CLIENT_A}": limit}, keys
    # Neither the proxy's address nor the forged left-hand entry is a key.
    assert f"login_attempts_ip:{FRONTEND}" not in token_env.store
    assert f"login_attempts_ip:{FORGED}" not in token_env.store


def test_a_successful_sign_in_still_clears_that_clients_bucket(bundled, token_env):
    """#591's reset is unchanged — it now clears the real client's bucket."""
    for i in range(4):
        assert _login(FRONTEND, _via_nginx(CLIENT_A), f"u{i}@example.com").status_code == 401
    assert token_env.get(f"login_attempts_ip:{CLIENT_A}") == "4"
    assert _login(FRONTEND, _via_nginx(CLIENT_A), GOOD_USER, GOOD_PASSWORD).status_code == 200
    assert token_env.get(f"login_attempts_ip:{CLIENT_A}") is None


def test_the_tunnel_hop_keys_on_the_cloudflare_client_both_routing_shapes(bundled, token_env):
    # Direct-to-backend tunnel rule: cloudflared is the TCP peer.
    _login(CLOUDFLARED, {"CF-Connecting-IP": CLIENT_A, "X-Forwarded-For": CLIENT_A},
           "t1@example.com")
    # Catch-all rule through nginx: nginx appends cloudflared's address.
    _login(FRONTEND, {"CF-Connecting-IP": CLIENT_B, "X-Forwarded-For": f"{CLIENT_B}, {CLOUDFLARED}"},
           "t2@example.com")
    assert token_env.ip_keys() == {
        f"login_attempts_ip:{CLIENT_A}": 1,
        f"login_attempts_ip:{CLIENT_B}": 1,
    }


def test_ipv6_clients_keep_exact_address_keys_in_canonical_form(bundled, token_env):
    _login(FRONTEND, _via_nginx("2001:DB8:0:0:0:0:0:1"), "v6a@example.com")
    _login(FRONTEND, _via_nginx("2001:db8::2"), "v6b@example.com")
    _login(FRONTEND, _via_nginx("::ffff:198.51.100.99"), "v4m@example.com")
    assert token_env.ip_keys() == {
        "login_attempts_ip:2001:db8::1": 1,
        "login_attempts_ip:2001:db8::2": 1,
        "login_attempts_ip:198.51.100.99": 1,
    }


# ---------------------------------------------------------------------------
# AC2 — a caller that is not a trusted proxy cannot pick its address
# ---------------------------------------------------------------------------

_SPOOF = {"X-Forwarded-For": FORGED, "X-Real-IP": FORGED, "CF-Connecting-IP": FORGED}


@pytest.mark.parametrize("peer", [DIRECT, AGENT, GATEWAY], ids=["direct", "agent", "gateway"])
def test_an_untrusted_peer_is_keyed_on_its_own_address(bundled, token_env, peer):
    _login(peer, _SPOOF, "spoof@example.com")
    assert token_env.ip_keys() == {f"login_attempts_ip:{peer}": 1}


def test_without_bundled_trust_even_the_proxy_address_is_just_a_peer(monkeypatch, token_env):
    """Dev and a bare `docker run` grant no forwarding trust at all."""
    monkeypatch.delenv("TRINITY_BUNDLED_PROXY_TRUST", raising=False)
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    _login(FRONTEND, _via_nginx(CLIENT_A), "dev@example.com")
    assert token_env.ip_keys() == {f"login_attempts_ip:{FRONTEND}": 1}


# ---------------------------------------------------------------------------
# Every other per-IP consumer sees the same canonical address
# ---------------------------------------------------------------------------

def _recorder(seen):
    from fastapi import HTTPException

    def _record(ip, *a, **k):
        seen.append(ip)
        raise HTTPException(status_code=429, detail="recorded")

    return _record


@pytest.fixture
def consumers(monkeypatch, tmp_path):
    """Each consumer's per-IP limiter replaced by a recorder that stops the
    request there. Settings reads and the setup user lookup are stubbed so the
    request reaches its limiter; the address itself is never stubbed."""
    from fastapi import HTTPException
    from services import rate_limiter

    ra = route_globals("/api/auth/email/verify", "POST")
    rp = route_globals("/api/public/link/{token}", "GET")
    rs = route_globals("/api/setup/admin-password", "POST")
    seen: list = []
    rec = _recorder(seen)
    monkeypatch.setitem(ra, "is_setup_completed", lambda: True)
    monkeypatch.setattr(ra["db"], "get_setting_value", lambda key, default=None: "true")
    monkeypatch.setitem(ra, "check_login_rate_limit", rec)
    monkeypatch.setitem(rp, "check_public_link_rate_limit", rec)
    monkeypatch.setitem(rs, "check_login_rate_limit", rec)

    def _enforce(key, *a, **k):
        # `portal_signin_req_ip:<ip>` — the address is everything after the
        # first colon (an IPv6 address carries its own colons).
        seen.append(key.split(":", 1)[1])
        raise HTTPException(status_code=429, detail="recorded")

    monkeypatch.setattr(rate_limiter, "enforce", _enforce)

    class _NoAdmin:
        def get_user_by_username(self, *_):
            return None

        def get_setting_value(self, key, default=None):
            return default

    monkeypatch.setitem(rs, "db", _NoAdmin())
    monkeypatch.setenv("ADMIN_PASSWORD_SOURCE", "instance-id")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    claim = tmp_path / "setup-claim"
    claim.write_text("i-0123456789abcdef0\n")
    monkeypatch.setitem(rs, "_SETUP_CLAIM_PATH", claim)
    return seen


_CONSUMERS = [
    ("POST", "/api/auth/email/verify", {"json": {"email": "v@example.com", "code": "123456"}}),
    ("POST", "/api/access/request", {"json": {"email": "r@example.com"}}),
    ("POST", "/api/enterprise/client-portal/auth/request", {"json": {"email": "p@example.com"}}),
    ("POST", "/api/enterprise/client-portal/auth/verify", {"json": {"email": "p@example.com", "code": "123456"}}),
    ("GET", "/api/public/link/not-a-real-token", {}),
    ("POST", "/api/setup/admin-password", {"json": {
        "password": "Str0ng!Passw0rd#873", "confirm_password": "Str0ng!Passw0rd#873",
        "email": "admin@example.com", "claim_code": "i-0123456789abcdef0"}}),
]


@pytest.mark.parametrize("method,path,kw", _CONSUMERS, ids=[c[1] for c in _CONSUMERS])
def test_each_consumer_receives_the_forwarded_client(bundled, consumers, method, path, kw):
    r = _request(method, path, peer=FRONTEND, headers=_via_nginx(CLIENT_A, prefix=FORGED), **kw)
    assert r.status_code == 429, r.text
    assert consumers == [CLIENT_A], consumers


@pytest.mark.parametrize("method,path,kw", _CONSUMERS, ids=[c[1] for c in _CONSUMERS])
def test_each_consumer_ignores_identity_headers_from_an_agent(bundled, consumers, method, path, kw):
    r = _request(method, path, peer=AGENT, headers=_SPOOF, **kw)
    assert r.status_code == 429, r.text
    assert consumers == [AGENT], consumers
