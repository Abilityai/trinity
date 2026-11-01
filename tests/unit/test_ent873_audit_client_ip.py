"""trinity-enterprise#873 AC3 — the audit log records the real client address.

Every request-derived audit row records `request.client.host`, which behind the
bundled proxy is the client the proxy saw, never the proxy. These tests write
real rows through real routes into this process's temporary SQLite database
(`TRINITY_DB_PATH`, pinned by the unit conftest) and read them back by request
ID, so what is asserted is what an operator would export.

Proxy addresses are the prod/hosted ingress reservations (`172.30.0.2` nginx,
`172.30.0.3` cloudflared); client addresses come from the documentation ranges.
"""
from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

pytestmark = pytest.mark.unit

FRONTEND = "172.30.0.2"
CLOUDFLARED = "172.30.0.3"
CLIENT_A = "198.51.100.31"
CLIENT_B = "203.0.113.58"
DIRECT = "192.0.2.211"
FORGED = "10.66.77.88"


@pytest.fixture
def bundled(monkeypatch):
    monkeypatch.setenv("TRINITY_BUNDLED_PROXY_TRUST", "1")
    monkeypatch.setenv("TRINITY_PROXY_PREFIX", "172.30.0")
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)


def _request(method, path, *, peer, headers, **kw):
    import main

    async def go():
        transport = httpx.ASGITransport(app=main.app, client=(peer, 51515))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            return await c.request(method, path, headers=headers, **kw)

    return asyncio.run(go())


def route_globals(path: str, method: str) -> dict:
    """The namespace the app's route was built from (see the sign-in tests)."""
    import main

    for r in main.app.routes:
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or ()):
            return r.endpoint.__globals__
    raise AssertionError(f"no {method} {path} route")


def _rows(request_id):
    from database import db

    return db.get_audit_entries(request_id=request_id, limit=10)


@pytest.fixture
def token_route(monkeypatch):
    """Real audit writer; only the limiter's Redis and the user store stubbed."""
    ra = route_globals("/api/token", "POST")

    monkeypatch.setitem(ra, "get_redis_client", lambda: None)  # limiter fails open
    monkeypatch.setitem(ra, "is_setup_completed", lambda: True)
    monkeypatch.setitem(
        ra, "authenticate_user",
        lambda u, p: {"id": 3, "username": u, "role": "user", "email": u}
        if p == "right-873" else None,
    )
    monkeypatch.setattr(ra["db"], "update_last_login", lambda *a, **k: None)


def _sign_in(peer, headers, password):
    rid = f"ent873-{uuid.uuid4()}"
    r = _request("POST", "/api/token", peer=peer, headers={**headers, "X-Request-ID": rid},
                 data={"username": "auditee@example.com", "password": password})
    return r, _rows(rid)


def test_failed_sign_in_through_nginx_records_the_client(bundled, token_route):
    r, rows = _sign_in(FRONTEND, {"X-Forwarded-For": f"{FORGED}, {CLIENT_A}", "X-Real-IP": CLIENT_A},
                       "wrong")
    assert r.status_code == 401
    assert [(x["event_action"], x["actor_ip"]) for x in rows] == [("login_failed", CLIENT_A)]


def test_successful_sign_in_through_the_tunnel_records_the_client(bundled, token_route):
    r, rows = _sign_in(CLOUDFLARED, {"CF-Connecting-IP": CLIENT_B, "X-Forwarded-For": CLIENT_B},
                       "right-873")
    assert r.status_code == 200, r.text
    assert [(x["event_action"], x["actor_ip"]) for x in rows] == [("login_success", CLIENT_B)]
    assert rows[0]["target_id"] == "auditee@example.com"


def test_a_direct_caller_is_recorded_as_itself_whatever_it_claims(bundled, token_route):
    r, rows = _sign_in(DIRECT, {"X-Forwarded-For": FORGED, "X-Real-IP": FORGED,
                                "CF-Connecting-IP": FORGED}, "wrong")
    assert r.status_code == 401
    assert [x["actor_ip"] for x in rows] == [DIRECT]


def test_a_proxy_that_forwards_no_identity_is_recorded_as_the_proxy(bundled, token_route):
    """Absent identity is never guessed: the row names the hop that sent it."""
    r, rows = _sign_in(FRONTEND, {}, "wrong")
    assert r.status_code == 401
    assert [x["actor_ip"] for x in rows] == [FRONTEND]


def test_an_authorization_refusal_records_the_client(bundled, monkeypatch):
    """A non-auth row: an agent key without `agents.manage` asking to create a
    durable agent is refused AND audited (`dependencies.enforce_agent_capability`)."""
    import main
    from models import User

    # The object the app's routes were built with. A bare `from dependencies
    # import ...` can name a re-imported copy when another unit test has
    # evicted and reloaded the module, and an override keyed on that copy is
    # silently never consulted.
    get_current_user = main.get_current_user

    agent_principal = User(id=99, username="owner", email="owner@example.com", role="admin",
                           agent_name="worker-873", mcp_scope="agent")
    main.app.dependency_overrides[get_current_user] = lambda: agent_principal
    try:
        rid = f"ent873-{uuid.uuid4()}"
        r = _request("POST", "/api/agents", peer=FRONTEND,
                     headers={"X-Forwarded-For": CLIENT_A, "X-Request-ID": rid},
                     json={"name": "durable-873"})
    finally:
        main.app.dependency_overrides.pop(get_current_user, None)
    assert r.status_code == 403, r.text
    rows = _rows(rid)
    assert [(x["event_action"], x["actor_ip"], x["actor_id"]) for x in rows] == [
        ("capability_refused", CLIENT_A, "worker-873")]


def test_hash_chained_rows_carry_the_client_and_still_verify(bundled, token_route):
    platform_audit_service = route_globals("/api/token", "POST")["platform_audit_service"]

    was = platform_audit_service.hash_chain_enabled
    platform_audit_service.enable_hash_chain(True)
    try:
        _, first = _sign_in(FRONTEND, {"X-Forwarded-For": CLIENT_A}, "wrong")
        _, second = _sign_in(FRONTEND, {"X-Forwarded-For": CLIENT_B}, "wrong")
    finally:
        platform_audit_service.enable_hash_chain(was)
    assert [first[0]["actor_ip"], second[0]["actor_ip"]] == [CLIENT_A, CLIENT_B]
    result = asyncio.run(platform_audit_service.verify_chain(first[0]["id"], second[0]["id"]))
    assert result["valid"] is True, result


def test_the_a2a_card_limiter_keys_on_the_client(bundled, monkeypatch):
    """A2A's unauthenticated per-IP limit reads the same canonical address."""
    from fastapi import HTTPException

    rate_limiter = route_globals("/a2a/{agent_name}/.well-known/agent-card.json", "GET")["rate_limiter"]
    seen = []

    def _enforce(key, *a, **k):
        seen.append(key)
        raise HTTPException(status_code=429, detail="recorded")

    monkeypatch.setattr(rate_limiter, "enforce", _enforce)
    r = _request("GET", "/a2a/some-agent/.well-known/agent-card.json", peer=FRONTEND,
                 headers={"X-Forwarded-For": f"{FORGED}, {CLIENT_B}"})
    assert r.status_code == 429
    assert seen == [f"a2a_card_ip:{CLIENT_B}"]
