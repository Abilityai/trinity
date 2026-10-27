"""trinity-enterprise#595 — both voice starts issue the socket's credential.

The voice socket authenticates with a single-use, short-lived credential; no
session token appears in any URL. The credential is issued by the same
authorized request that creates the session, so this file drives the two
REGISTERED start routes through FastAPI with real platform credentials (a real
JWT; an MCP key resolved by the real resolver) and the real dependency chain —
`require_interactive` on the OSS start, `get_portal_principal` +
`resolve_platform_user_unfloored` + `require_interactive` on the Workspace start.
Only the storage, provider and roster leaves are replaced.

  * Both starts return a fresh opaque ticket, its 30 s expiry, the unchanged
    relative `websocket_url`, and `Cache-Control: no-store`; consuming the
    ticket from the shared store yields the session scope, the caller's
    username, id and the origin of the start.
  * An interactive operator (OSS) and a Workspace-only account (Workspace)
    succeed; every MCP key scope is refused at both — before the agent lookup.
  * Expired, revoked, MFA-challenge, portal-scope and suspended credentials
    keep their existing refusals (regression guards, not new behaviour).
  * A ticket store that cannot be written → 503 with words, the new session is
    removed, no provider starts, no lease is armed. A session store failure
    → the same words, never the exception text.

Modules are reached through the objects the routes actually call (unit
conftest evicts `dependencies` between tests; learnings 2026-09-23).
"""
from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

AGENT = "scribe"
THREAD = "thread-595"
UNAVAILABLE = "Voice is unavailable right now. Try again shortly."

USERS = {
    "olga": {"id": 11, "username": "olga", "email": "olga@example.com", "role": "operator", "suspended_at": None},
    "wendy": {"id": 12, "username": "wendy", "email": "wendy@example.com", "role": "user", "suspended_at": None},
    "admin": {"id": 1, "username": "admin", "email": "admin@example.com", "role": "admin", "suspended_at": None},
}


def _jwt(sub, **extra):
    import config
    from jose import jwt
    claims = {"sub": sub, "exp": int(time.time()) + 300, **extra}
    return jwt.encode(claims, config.SECRET_KEY, algorithm=config.ALGORITHM)


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


class _Store:
    """fakeredis with a switchable write failure."""

    def __init__(self):
        import fakeredis
        self.r = fakeredis.FakeRedis(decode_responses=True)
        self.fail_writes = False

    def setex(self, *a, **k):
        if self.fail_writes:
            raise ConnectionError("redis://u:SENTINEL@host write failed")
        return self.r.setex(*a, **k)

    def getdel(self, key):
        return self.r.getdel(key)

    def exists(self, key):
        return self.r.exists(key)


def _distinct(dicts):
    seen, out = set(), []
    for d in dicts:
        if id(d) not in seen:
            seen.add(id(d))
            out.append(d)
    return out


@pytest.fixture
def world(monkeypatch):
    voice = importlib.import_module("routers.voice")
    cpr = importlib.import_module("client_portal.router")
    portal_auth = importlib.import_module("client_portal.portal_auth")
    import client_portal.voice as workspace_voice
    import database

    store = _Store()
    revoked = set()

    # Every `voice_auth_service` / `ws_ticket_service` / `dependencies` copy a
    # route can reach — the OSS router's import-time copy and whatever the
    # Workspace route imports lazily now. Resolved defensively, so on a tree
    # without the ticket path these tests fail by ASSERTION, not in the fixture.
    auth_mods = []
    if getattr(voice, "issue_voice_ticket", None):
        auth_mods.append(voice.issue_voice_ticket.__globals__)
    try:
        auth_mods.append(importlib.import_module("services.voice_auth_service").__dict__)
    except ImportError:
        pass
    ticket_mods = _distinct([g["mint_ticket"].__globals__ for g in _distinct(auth_mods)]
                            or [importlib.import_module("services.ws_ticket_service").__dict__])
    for g in ticket_mods:
        monkeypatch.setitem(g, "get_redis_client", lambda: store)
    dep_mods = _distinct([
        voice.get_authorized_agent.__globals__,
        cpr.resolve_platform_user_unfloored.__globals__,
        portal_auth.resolve_platform_user_unfloored.__globals__,
    ])

    class _Breaker:
        def exists(self, key):
            return 1 if key.rsplit(":", 1)[-1] in revoked else 0

    dbs = _distinct([g["db"] for g in dep_mods] + [voice.db, database.db])
    keys = {}
    for g in dep_mods:
        monkeypatch.setitem(g, "get_breaker_redis", lambda: _Breaker())
    for db in dbs:
        monkeypatch.setattr(db, "get_user_by_username", lambda u, *a, **k: USERS.get(u), raising=False)
        monkeypatch.setattr(db, "get_user_by_id", lambda i, *a, **k: next(
            (r for r in USERS.values() if r["id"] == i), None), raising=False)
        monkeypatch.setattr(db, "get_user_by_email", lambda e, *a, **k: next(
            (r for r in USERS.values() if r["email"] == e), None), raising=False)
        monkeypatch.setattr(db, "validate_mcp_api_key", lambda t, *a, **k: keys.get(t), raising=False)
        monkeypatch.setattr(db, "get_agent_ephemeral_info", lambda *a, **k: {"is_ephemeral": False}, raising=False)
        monkeypatch.setattr(db, "get_agent_owner", lambda n, *a, **k: {"owner_username": "admin"} if n == AGENT else None, raising=False)
        monkeypatch.setattr(db, "can_user_access_agent", lambda u, n, *a, **k: n == AGENT, raising=False)
        monkeypatch.setattr(db, "get_or_create_chat_session", lambda **k: SimpleNamespace(id="cs-595"), raising=False)

    # OSS start leaves.
    sessions = {"n": 0}

    async def _create_session(**kwargs):
        sessions["n"] += 1
        sessions["last"] = kwargs
        return SimpleNamespace(session_id=f"vs_oss_{sessions['n']}")

    fake_voice = SimpleNamespace(
        is_available=lambda: True,
        create_session=AsyncMock(side_effect=_create_session),
        remove_session=AsyncMock(),
        connect_and_stream=AsyncMock(),
    )
    monkeypatch.setattr(voice, "VOICE_ENABLED", True)
    monkeypatch.setattr(voice, "voice_service", fake_voice)
    monkeypatch.setattr(voice, "_get_voice_system_prompt", AsyncMock(return_value="You are Scribe."))
    monkeypatch.setattr(voice, "_build_context_summary", lambda cid: "")
    monkeypatch.setattr(voice, "_get_voice_name", lambda n: "Kore")

    # Workspace start leaves.
    monkeypatch.setattr(cpr.service, "agent_on_roster", lambda name, email, include_owned=False: name == AGENT)
    import services.rate_limiter as rl
    monkeypatch.setattr(rl, "enforce", lambda *a, **k: None)
    monkeypatch.setattr(importlib.import_module("client_portal.db"), "is_client_blocked", lambda e: False)

    async def _start_workspace_voice(**kwargs):
        sessions["n"] += 1
        sessions["last"] = kwargs
        sid = f"vs_ws_{sessions['n']}"
        return {"voice_session_id": sid, "websocket_url": f"/ws/voice/{sid}",
                "portal_session_id": kwargs["portal_session_id"], "max_duration_seconds": 1800}

    start_ws = AsyncMock(side_effect=_start_workspace_voice)
    monkeypatch.setattr(workspace_voice, "start_workspace_voice", start_ws)
    leases = []
    monkeypatch.setattr(workspace_voice, "mark_voice_call_active", lambda *a, **k: leases.append(a))
    import services.gemini_voice as gv
    monkeypatch.setattr(gv, "voice_service", fake_voice)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(voice.router)
    app.include_router(cpr.router)
    client = TestClient(app, raise_server_exceptions=False)

    def consume(ticket, sid):
        return ticket_mods[0]["consume_ticket"](ticket, scope=f"voice:{sid}")

    def add_key(token, scope, owner="admin", agent=None):
        keys[token] = {"scope": scope, "agent_name": agent, "key_id": f"k-{scope}", "key_name": scope,
                       "user_id": owner, "user_email": USERS[owner]["email"]}
        return token

    return SimpleNamespace(client=client, store=store, voice=fake_voice, sessions=sessions,
                           start_ws=start_ws, leases=leases, consume=consume, add_key=add_key,
                           revoked=revoked)


def _oss(world, token, agent=AGENT):
    return world.client.post(f"/api/agents/{agent}/voice/start", json={}, headers=_bearer(token))


def _ws(world, token, agent=AGENT):
    return world.client.post(f"/api/enterprise/client-portal/agents/{agent}/voice/start",
                             json={"portal_session_id": THREAD}, headers=_bearer(token))


# ---------------------------------------------------------------------------
# Issuance
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("door,user,origin", [("oss", "olga", "operator"), ("workspace", "wendy", "workspace")])
def test_both_starts_issue_a_fresh_session_bound_ticket(world, door, user, origin):
    start = _oss if door == "oss" else _ws
    r1 = start(world, _jwt(user))
    r2 = start(world, _jwt(user))
    assert r1.status_code == 200, r1.text
    body = r1.json()
    assert body.get("websocket_ticket"), "the start response carries no socket credential"
    assert body.get("websocket_ticket_expires_in") == 30
    assert r1.headers.get("cache-control") == "no-store"
    sid = body["voice_session_id"]
    # The URL stays relative and credential-free.
    assert body["websocket_url"] == f"/ws/voice/{sid}"
    assert body["websocket_ticket"] != r2.json()["websocket_ticket"]
    assert body["websocket_ticket"] not in r1.headers.get("location", "")

    payload = world.consume(body["websocket_ticket"], sid)
    assert payload == {"sub": user, "scope": f"voice:{sid}", "user_id": USERS[user]["id"], "origin": origin}
    # Bound to ITS session: the other start's session cannot use it.
    assert world.consume(r2.json()["websocket_ticket"], sid) is None


def test_the_workspace_start_keeps_its_service_result_and_user_binding(world):
    r = _ws(world, _jwt("wendy"))
    assert r.status_code == 200, r.text
    kwargs = world.start_ws.await_args.kwargs
    assert kwargs["user_id"] == 12 and kwargs["portal_session_id"] == THREAD
    assert set(r.json()) == {"voice_session_id", "websocket_url", "portal_session_id",
                             "max_duration_seconds", "websocket_ticket", "websocket_ticket_expires_in"}


# ---------------------------------------------------------------------------
# Eligible principals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scope", ["user", "agent", "system"])
def test_no_mcp_key_obtains_a_voice_credential_on_the_oss_start(world, scope):
    token = world.add_key(f"trinity_mcp_{scope}", scope, agent=AGENT if scope == "agent" else None)
    r = _oss(world, token)
    assert r.status_code == 403, r.text
    assert "websocket_ticket" not in r.text
    world.voice.create_session.assert_not_awaited()


def test_a_key_is_refused_before_the_agent_lookup(world):
    """The refusal depends only on the principal (§2.8): an unknown agent
    answers the same 403, never a 404 that would tell keys which agents exist."""
    token = world.add_key("trinity_mcp_user", "user")
    assert _oss(world, token, agent="no-such-agent").status_code == 403


@pytest.mark.parametrize("scope", ["user", "system"])
def test_no_mcp_key_obtains_a_voice_credential_on_the_workspace_start(world, scope):
    token = world.add_key(f"trinity_mcp_{scope}", scope)
    r = _ws(world, token)
    assert r.status_code == 403, r.text
    assert "websocket_ticket" not in r.text
    world.start_ws.assert_not_awaited()


def test_an_agent_key_keeps_the_workspace_refusal(world):
    token = world.add_key("trinity_mcp_agent", "agent", agent=AGENT)
    assert _ws(world, token).status_code == 403
    world.start_ws.assert_not_awaited()


def test_a_workspace_only_account_still_starts_in_the_workspace_but_not_on_the_operator_route(world):
    assert _ws(world, _jwt("wendy")).status_code == 200
    r = _oss(world, _jwt("wendy"))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "workspace_only"


def test_unchanged_refusals_stay_refusals(world):
    import dependencies
    expired = _jwt("olga", exp=int(time.time()) - 5)
    world.revoked.add("jti-gone")
    revoked = _jwt("olga", jti="jti-gone")
    mfa = _jwt("olga", scope=dependencies.MFA_CHALLENGE_SCOPE)
    portal = _jwt("x", scope=dependencies.PORTAL_SESSION_SCOPE, email="client@example.com")
    for token in (expired, revoked, mfa, portal):
        assert _oss(world, token).status_code == 401
    for token in (expired, revoked, mfa):
        assert _ws(world, token).status_code == 401
    # A portal-token client is the uniform Workspace 404.
    assert _ws(world, portal).status_code == 404
    USERS["olga"]["suspended_at"] = "2026-10-21T00:00:00Z"
    try:
        assert _oss(world, _jwt("olga")).status_code == 401
        assert _ws(world, _jwt("olga")).status_code == 401
    finally:
        USERS["olga"]["suspended_at"] = None
    world.voice.create_session.assert_not_awaited()
    world.start_ws.assert_not_awaited()


# ---------------------------------------------------------------------------
# Store failures — words, cleanup, no effects
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("door,user", [("oss", "olga"), ("workspace", "wendy")])
def test_a_ticket_store_write_failure_is_a_503_that_removes_the_new_session(world, door, user):
    world.store.fail_writes = True
    start = _oss if door == "oss" else _ws
    r = start(world, _jwt(user))
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == UNAVAILABLE
    assert "SENTINEL" not in r.text
    created = f"vs_{'oss' if door == 'oss' else 'ws'}_1"
    world.voice.remove_session.assert_awaited_once_with(created)
    world.voice.connect_and_stream.assert_not_called()
    assert world.leases == []


@pytest.mark.parametrize("door,user", [("oss", "olga"), ("workspace", "wendy")])
def test_a_session_store_failure_is_the_same_503_with_words(world, door, user):
    err = RuntimeError("Failed to persist voice session metadata to Redis: redis://u:SENTINEL@host")
    world.voice.create_session.side_effect = err
    world.start_ws.side_effect = err
    start = _oss if door == "oss" else _ws
    r = start(world, _jwt(user))
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == UNAVAILABLE
    assert "SENTINEL" not in r.text
    world.voice.remove_session.assert_not_awaited()
