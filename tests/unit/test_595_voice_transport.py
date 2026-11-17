"""trinity-enterprise#595 — the voice socket's first-frame admission.

The voice socket authenticates with a single-use, short-lived credential; no
session token appears in any URL. The client opens `/ws/voice/{id}` with no
query string and sends `{"type":"auth","ticket":...}` as its first frame. The
server accepts the transport, reads that one frame (5 s, 1 KiB), consumes the
ticket under `voice:{id}`, re-reads the account it names, and only then looks
the session up, applies the #600 owner gate, acknowledges with `auth_ok` and
starts the provider.

Three harnesses, each for what only it can show:

  * the REGISTERED route through Starlette's real WebSocket (`TestClient`), so
    query parameters, binary frames and the ASGI message shapes are the
    framework's own;
  * the handler driven with a recording socket, so the ORDER of consume →
    account → lookup → owner gate → `auth_ok` → provider is observable;
  * two independently loaded ticket-service instances over one fakeredis — two
    workers — so a grant minted on one is consumed on the other, and exactly
    one of two concurrent attachments wins.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

SID = "vs_595"
OTHER_SID = "vs_595_other"
NOT_VERIFIED = "The voice connection expired or could not be verified. Start a new call."
RELOAD = "Voice was updated. Reload this page, then start a new call."
UNAVAILABLE = "Voice is unavailable right now. Try again shortly."

ROWS = {
    1: {"id": 1, "username": "alice", "email": "alice@example.com", "role": "user", "suspended_at": None},
    2: {"id": 2, "username": "omar", "email": "omar@example.com", "role": "operator", "suspended_at": None},
}


class _Session(SimpleNamespace):
    pass


@pytest.fixture
def bridge(monkeypatch):
    """The real `routers.voice` handler over a recording provider and store."""
    import fakeredis
    voice = importlib.import_module("routers.voice")
    # The service namespaces the handler actually calls into (a re-imported
    # copy is never the one patched). Resolved defensively so the two contract
    # tests below can also run — and fail by assertion — on a tree without them.
    auth_fn = getattr(voice, "authenticate_voice_socket", None)
    auth_g = auth_fn.__globals__ if auth_fn else {}
    ticket_g = (auth_g["consume_ticket"].__globals__ if "consume_ticket" in auth_g
                else importlib.import_module("services.ws_ticket_service").__dict__)
    store = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setitem(ticket_g, "get_redis_client", lambda: store)

    events = []
    rows = {k: dict(v) for k, v in ROWS.items()}

    def _by_id(i):
        events.append("account")
        # SQLite compares an INTEGER column with integer affinity: `True` and
        # "2" would find rows 1 and 2. The fake answers the same way, so only
        # the ticket's own type check can refuse them.
        try:
            return rows.get(int(i))
        except (TypeError, ValueError):
            return None

    import database
    monkeypatch.setattr(database.db, "get_user_by_id", _by_id)

    if "consume_ticket" in auth_g:
        real_consume = auth_g["consume_ticket"]

        def _consume(ticket, **kw):
            events.append("consume")
            return real_consume(ticket, **kw)

        monkeypatch.setitem(auth_g, "consume_ticket", _consume)

    sessions = {}

    async def _get_session(sid):
        events.append("lookup")
        return sessions.get(sid)

    async def _stream(*a, **k):
        events.append("provider")

    async def _end(sid):
        return None

    fake = SimpleNamespace(
        get_session=_get_session, connect_and_stream=_stream, end_session=_end,
        remove_session=AsyncMock(), send_audio=AsyncMock(),
    )
    monkeypatch.setattr(voice, "voice_service", fake)
    real_gate = voice.assert_owns_or_admin

    def _gate(user, owner_id, **kw):
        events.append("owner")
        return real_gate(user, owner_id, **kw)

    monkeypatch.setattr(voice, "assert_owns_or_admin", _gate)

    def mint(sub="alice", sid=SID, *, scope=None, origin="workspace", user_id=1, claims=None):
        extra = {"user_id": user_id, "origin": origin} if claims is None else claims
        return ticket_g["mint_ticket"](sub, scope=scope or f"voice:{sid}", ttl_seconds=30, claims=extra)

    def add(sid=SID, owner=1):
        sessions[sid] = _Session(session_id=sid, agent_name="scribe", user_id=owner,
                                 portal_session_id=None, max_duration=60)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(voice.router)
    return SimpleNamespace(voice=voice, auth_g=auth_g, ticket_g=ticket_g, store=store, events=events,
                           rows=rows, mint=mint, add=add, client=TestClient(app), monkeypatch=monkeypatch)


def _auth(ticket):
    return json.dumps({"type": "auth", "ticket": ticket})


def _talk(bridge, path=f"/ws/voice/{SID}", send=None, then_end=True):
    """Connect through the registered route; return every frame received and
    the close code."""
    from starlette.websockets import WebSocketDisconnect
    frames, code = [], None
    with bridge.client.websocket_connect(path) as ws:
        if send is not None:
            send(ws)
        try:
            while True:
                frame = ws.receive_json()
                frames.append(frame)
                if frame.get("type") == "auth_ok" and then_end:
                    ws.send_text(json.dumps({"type": "end"}))
        except WebSocketDisconnect as d:
            code = d.code
    return frames, code


def _refused_before_lookup(bridge, frames, code, message=NOT_VERIFIED):
    assert code == 4001
    assert frames == [{"type": "status", "state": "ended", "reason": "error", "message": message}]
    assert "lookup" not in bridge.events and "provider" not in bridge.events


# ---------------------------------------------------------------------------
# The contract: the URL carries no credential
# ---------------------------------------------------------------------------

def test_the_socket_route_takes_no_credential_parameter(bridge):
    import inspect
    params = inspect.signature(bridge.voice.voice_websocket).parameters
    assert list(params) == ["websocket", "voice_session_id"], (
        f"the voice socket still reads a credential from its URL: {list(params)}")


def test_a_valid_session_jwt_in_the_url_no_longer_opens_the_socket(bridge):
    """The hard cut (D1): the owner's own, valid session token in `?token=` is
    refused with reload words, and the session is never looked up."""
    import config
    import database
    from jose import jwt
    bridge.add(owner=1)
    bridge.monkeypatch.setattr(database.db, "get_user_by_username", lambda u: ROWS[1] if u == "alice" else None)
    bridge.monkeypatch.setattr(bridge.voice, "db", database.db)
    token = jwt.encode({"sub": "alice"}, config.SECRET_KEY, algorithm=config.ALGORITHM)
    # The client also sends `end`, so a handler that DID admit the URL token
    # finishes its bridge instead of waiting for audio.
    frames, code = _talk(bridge, path=f"/ws/voice/{SID}?token={token}",
                         send=lambda ws: ws.send_text(json.dumps({"type": "end"})))
    assert {"type": "auth_ok"} not in frames
    assert "provider" not in bridge.events
    _refused_before_lookup(bridge, frames, code, message=RELOAD)


# ---------------------------------------------------------------------------
# The registered route, through Starlette's real WebSocket
# ---------------------------------------------------------------------------

def test_a_valid_first_frame_is_acknowledged_then_streams(bridge):
    bridge.add()
    frames, _ = _talk(bridge, send=lambda ws: ws.send_text(_auth(bridge.mint())))
    assert frames[0] == {"type": "auth_ok"}
    assert bridge.events == ["consume", "account", "lookup", "owner", "provider"]


@pytest.mark.parametrize("name,send", [
    ("malformed json", lambda ws, t: ws.send_text("{not json")),
    ("not an object", lambda ws, t: ws.send_text(json.dumps(["auth", t]))),
    ("wrong type", lambda ws, t: ws.send_text(json.dumps({"type": "audio", "ticket": t}))),
    ("no ticket", lambda ws, t: ws.send_text(json.dumps({"type": "auth"}))),
    ("ticket not a string", lambda ws, t: ws.send_text(json.dumps({"type": "auth", "ticket": 7}))),
    ("ticket wrong shape", lambda ws, t: ws.send_text(json.dumps({"type": "auth", "ticket": t + "x"}))),
    ("binary frame", lambda ws, t: ws.send_bytes(_auth(t).encode())),
    ("oversized", lambda ws, t: ws.send_text(json.dumps({"type": "auth", "ticket": t, "pad": "x" * 1100}))),
    ("disconnect", lambda ws, t: ws.close()),
])
def test_a_bad_first_frame_is_refused_with_words_before_lookup(bridge, name, send):
    bridge.add()
    ticket = bridge.mint()
    if name == "disconnect":
        _frames, _code = _talk(bridge, send=lambda ws: send(ws, ticket))
        assert "lookup" not in bridge.events and "provider" not in bridge.events
        return
    frames, code = _talk(bridge, send=lambda ws: send(ws, ticket))
    _refused_before_lookup(bridge, frames, code)
    assert "consume" not in bridge.events        # a malformed frame never reaches the store


@pytest.mark.parametrize("param", ["token", "ticket"])
def test_a_credential_in_the_url_is_refused_with_reload_words(bridge, param):
    bridge.add()
    frames, code = _talk(bridge, path=f"/ws/voice/{SID}?{param}=legacy-marker",
                         send=lambda ws: ws.send_text(_auth(bridge.mint())))
    _refused_before_lookup(bridge, frames, code, message=RELOAD)
    assert "consume" not in bridge.events


@pytest.mark.parametrize("case", ["wrong session", "wrong surface", "expired", "reused"])
def test_a_ticket_that_does_not_open_this_socket_is_refused_before_lookup(bridge, case):
    bridge.add()
    bridge.add(OTHER_SID)
    if case == "wrong session":
        ticket = bridge.mint(sid=OTHER_SID)
    elif case == "wrong surface":
        ticket = bridge.mint(scope="user")
    elif case == "expired":
        ticket = bridge.mint()
        bridge.store.delete(f"ws_ticket:{ticket}")
    else:
        ticket = bridge.mint()
        _talk(bridge, send=lambda ws: ws.send_text(_auth(ticket)))
        bridge.events.clear()
    frames, code = _talk(bridge, send=lambda ws: ws.send_text(_auth(ticket)))
    _refused_before_lookup(bridge, frames, code)
    if case == "wrong session":
        # Spent on the wrong socket: it cannot be retried where it belongs.
        frames, code = _talk(bridge, path=f"/ws/voice/{OTHER_SID}",
                             send=lambda ws: ws.send_text(_auth(ticket)))
        assert code == 4001


def test_an_unreadable_ticket_store_says_so(bridge, monkeypatch):
    bridge.add()
    ticket = bridge.mint()

    class _Down:
        def getdel(self, key):
            raise ConnectionError("redis://u:SENTINEL@host")

    monkeypatch.setitem(bridge.ticket_g, "get_redis_client", lambda: _Down())
    frames, code = _talk(bridge, send=lambda ws: ws.send_text(_auth(ticket)))
    _refused_before_lookup(bridge, frames, code, message=UNAVAILABLE)


def test_the_auth_frame_is_bounded_in_time(bridge, monkeypatch):
    """A client that opens the socket and sends nothing is refused, with words,
    once the bound passes — the worker is not pinned."""
    bridge.add()
    monkeypatch.setitem(bridge.auth_g, "AUTH_FRAME_TIMEOUT_SECONDS", 0.05)
    frames, code = _talk(bridge, send=None)
    _refused_before_lookup(bridge, frames, code)


# ---------------------------------------------------------------------------
# The account as it is at attach time
# ---------------------------------------------------------------------------

def _admit(bridge, ticket):
    frames, code = _talk(bridge, send=lambda ws: ws.send_text(_auth(ticket)))
    return frames, code


@pytest.mark.parametrize("case", [
    "deleted", "suspended", "renamed", "id mismatch", "bad origin",
    "operator demoted", "id is bool", "id is zero", "id is a string", "no id",
])
def test_an_account_that_no_longer_qualifies_is_refused_before_lookup(bridge, case):
    bridge.add(owner=2)
    sub, origin, user_id, claims = "omar", "operator", 2, None
    if case == "deleted":
        del bridge.rows[2]
    elif case == "suspended":
        bridge.rows[2]["suspended_at"] = "2026-10-21T00:00:00Z"
    elif case == "renamed":
        bridge.rows[2]["username"] = "omar2"
    elif case == "id mismatch":
        bridge.rows[2]["id"] = 3
    elif case == "bad origin":
        origin = "mcp"
    elif case == "operator demoted":
        bridge.rows[2]["role"] = "user"
    elif case == "id is bool":
        # `True == 1`: alice's row would match on id AND username.
        sub, claims = "alice", {"user_id": True, "origin": "workspace"}
    elif case == "id is zero":
        claims = {"user_id": 0, "origin": "operator"}
    elif case == "id is a string":
        claims = {"user_id": "2", "origin": "operator"}
    else:
        claims = {"origin": "operator"}
    frames, code = _admit(bridge, bridge.mint(sub, origin=origin, user_id=user_id, claims=claims))
    _refused_before_lookup(bridge, frames, code)


def test_a_workspace_origin_grant_keeps_workspace_only_eligibility(bridge):
    bridge.add(owner=1)
    frames, _ = _admit(bridge, bridge.mint("alice", origin="workspace", user_id=1))
    assert frames[0] == {"type": "auth_ok"}


def test_the_role_used_is_the_rows_not_a_value_in_the_ticket(bridge):
    """A ticket cannot carry a role: an admin claim inside it does not make a
    non-owner an admin at the owner gate."""
    bridge.add(owner=2)
    ticket = bridge.mint("alice", claims={"user_id": 1, "origin": "workspace", "role": "admin"})
    frames, code = _admit(bridge, ticket)
    assert code == 4003 and {"type": "auth_ok"} not in frames
    assert "provider" not in bridge.events


def test_an_unknown_session_is_4004_only_after_authentication(bridge):
    frames, code = _admit(bridge, bridge.mint())
    assert code == 4004
    assert bridge.events == ["consume", "account", "lookup"]
    assert frames[0]["type"] == "status" and frames[0]["message"]


def test_a_repeated_auth_frame_ends_the_call_without_reauthenticating(bridge):
    bridge.add()
    first, second = bridge.mint(), bridge.mint()

    def send(ws):
        ws.send_text(_auth(first))
        assert ws.receive_json() == {"type": "auth_ok"}
        ws.send_text(_auth(second))

    frames, _ = _talk(bridge, send=send, then_end=False)
    assert bridge.events.count("consume") == 1
    assert any(f.get("type") == "saved" for f in frames)
    # The second ticket was never consumed: it is still in the store.
    assert bridge.store.exists(f"ws_ticket:{second}")


# ---------------------------------------------------------------------------
# Order, with a recording socket
# ---------------------------------------------------------------------------

class _RecordingSocket:
    def __init__(self, events, frames):
        self.events = events
        self.query_params = {}
        self._frames = list(frames)

    async def accept(self):
        self.events.append("accept")

    async def receive(self):
        self.events.append("frame")
        if not self._frames:
            return {"type": "websocket.disconnect", "code": 1000}
        return {"type": "websocket.receive", "text": self._frames.pop(0)}

    async def receive_text(self):
        await asyncio.sleep(0)
        from fastapi import WebSocketDisconnect
        if not self._frames:
            raise WebSocketDisconnect()
        return self._frames.pop(0)

    async def send_json(self, payload):
        if payload.get("type") == "auth_ok":
            self.events.append("auth_ok")

    async def close(self, code=1000, reason=""):
        self.events.append(f"close:{code}")


def test_admission_runs_frame_consume_account_lookup_owner_ack_provider_in_order(bridge):
    bridge.add()
    ws = _RecordingSocket(bridge.events, [_auth(bridge.mint()), json.dumps({"type": "end"})])
    asyncio.run(bridge.voice.voice_websocket(ws, SID))
    order = [e for e in bridge.events if e in
             {"accept", "frame", "consume", "account", "lookup", "owner", "auth_ok", "provider"}]
    assert order == ["accept", "frame", "consume", "account", "lookup", "owner", "auth_ok", "provider"]


# ---------------------------------------------------------------------------
# Two workers, one store
# ---------------------------------------------------------------------------

def _load(path, name, extra=None):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for k, v in (extra or {}).items():
        setattr(mod, k, v)
    return mod


@pytest.fixture
def two_workers(monkeypatch):
    """Two independently loaded ticket services AND voice-auth services, as two
    uvicorn workers would hold them, sharing only Redis."""
    import fakeredis
    store = fakeredis.FakeRedis(decode_responses=True)
    workers = []
    for n in ("a", "b"):
        tickets = _load(_BACKEND / "services" / "ws_ticket_service.py", f"_ent595_tickets_{n}")
        tickets.get_redis_client = lambda: store
        auth = _load(_BACKEND / "services" / "voice_auth_service.py", f"_ent595_auth_{n}")
        auth.mint_ticket = tickets.mint_ticket
        auth.consume_ticket = tickets.consume_ticket
        auth.TicketStoreUnavailable = tickets.TicketStoreUnavailable
        workers.append(SimpleNamespace(tickets=tickets, auth=auth))
    import database
    monkeypatch.setattr(database.db, "get_user_by_id", lambda i: dict(ROWS[i]) if i in ROWS else None)
    return SimpleNamespace(a=workers[0], b=workers[1], store=store)


def test_a_grant_issued_on_one_worker_is_consumed_on_another(two_workers):
    from models import User
    alice = User(id=1, username="alice", email="alice@example.com", role="user")
    cred = asyncio.run(two_workers.a.auth.issue_voice_ticket(
        alice, SID, origin="workspace", remove_session=AsyncMock()))
    ws = _RecordingSocket([], [_auth(cred["websocket_ticket"])])
    user = asyncio.run(two_workers.b.auth.authenticate_voice_socket(ws, SID))
    assert (user.id, user.username, user.role) == (1, "alice", "user")


def test_exactly_one_of_two_concurrent_attachments_is_admitted(two_workers):
    ticket = two_workers.a.tickets.mint_ticket(
        "alice", scope=f"voice:{SID}", claims={"user_id": 1, "origin": "workspace"})

    async def _attach(worker):
        ws = _RecordingSocket([], [_auth(ticket)])
        try:
            return await worker.auth.authenticate_voice_socket(ws, SID)
        except worker.auth.VoiceAuthRefused:
            return None

    async def _both():
        return await asyncio.gather(_attach(two_workers.a), _attach(two_workers.b))

    results = asyncio.run(_both())
    assert sum(r is not None for r in results) == 1
