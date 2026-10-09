"""trinity-enterprise#837 — a Workspace-only account's `/ws` socket carries only the
event kinds the Workspace reads.

ent#467 scoped `/ws` by agent. #837 made the platform role `user` Workspace-only and
refuses it on every operator REST route, but `/ws` (opened through the marked
`POST /api/ws/ticket`) still streamed every event kind for the agents a member can
see: operator-queue items, notifications, reports, room triggers, the sharing events
that carry another person's email, and the agent-lifecycle events. The SPA ignored
them for a `user`; the wire still carried them.

Pinned here, each through the code that applies it:

  * identity — a Workspace-only role (`user`, or one outside the ladder) resolves with
    the Workspace's kind allowlist; operator, creator and admin resolve with none;
  * the rule — a slot with an allowlist receives only those kinds (`type`, else the
    lifecycle `event` key); a payload with no kind is withheld; the agent scope still
    applies on top of it;
  * both delivery paths — live fan-out and the reconnect replay;
  * the fields — a member's slot gets each event cut down to the fields the
    Workspace reads (the kind, the agent, the activity state, the replay cursor):
    an `agent_activity` carries prompt and reply previews, cost and session ids;
  * a slot without an allowlist is unchanged;
  * the wiring — `/ws` hands the identity's allowlist to `connect`, `connect` hands it
    to the dispatcher, and `connect`'s default is the closed one.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from services import event_bus as event_bus_mod
from services import ws_identity_service
from services.event_bus import SCOPE_ALL, StreamDispatcher
from services.ws_identity_service import resolve_ws_identity

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"

# What the Workspace reads off `/ws` (src/frontend/src/utils/websocket.js → the portal
# stores): activity and loop runs re-read the rail and the Work feed, a skills change
# re-hydrates the briefing, and `resync_required` resets the replay cursor. A literal on
# purpose: widening it is a decision, made here and in the service together.
WORKSPACE_KINDS = frozenset({
    "agent_activity", "loop_run_completed", "loop_completed",
    "agent_skills_changed", "resync_required",
})

# The fields those consumers read: the kind (`type`, or `event` for the lifecycle
# kinds), the agent the event is about, and `activity_state` (the rail re-reads on
# a terminal activity). `_eid`, the replay cursor, is the dispatcher's own.
WORKSPACE_FIELDS = frozenset({"type", "event", "agent_name", "activity_state"})


def _slot(*, agents=("shared",), allowed_types=None, allowed_fields=None, is_admin=False):
    async def _send(_payload):  # pragma: no cover — never awaited here
        return None

    slot = event_bus_mod._ClientSlot(
        ws=object(), scope=SCOPE_ALL, send_func=_send, is_admin=is_admin,
        accessible_agents=set(agents), email="member@example.com",
    )
    slot.allowed_types = allowed_types
    slot.allowed_fields = allowed_fields
    return slot


def _fields(payload: dict) -> dict:
    return {"payload": json.dumps(payload), "scope": SCOPE_ALL, "agent_name": ""}


def _drain(slot) -> list:
    return [slot.queue.get_nowait()[1] for _ in range(slot.queue.qsize())]


def _kind(payload: dict):
    return payload.get("type") or payload.get("event")


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

class _FakeDb:
    def __init__(self, user):
        self._user = user

    def get_user_by_username(self, _username):
        return self._user

    def get_accessible_agent_names(self, _email, _is_admin):
        return ["shared"]


@pytest.fixture
def resolve_as(monkeypatch):
    import database

    def _resolve(role, email="member@example.com"):
        monkeypatch.setattr(database, "db", _FakeDb({"role": role, "email": email}))
        return resolve_ws_identity("someone")

    return _resolve


@pytest.mark.parametrize("role", ["user", "guest", None])
def test_a_workspace_only_role_resolves_with_the_workspace_kinds(resolve_as, role):
    identity = resolve_as(role)
    assert identity is not None
    assert identity.get("allowed_types") == WORKSPACE_KINDS
    assert identity.get("allowed_fields") == WORKSPACE_FIELDS
    assert identity["accessible_agents"] == ["shared"]  # the agent scope is unchanged


def test_a_member_without_an_email_keeps_the_kind_filter(resolve_as):
    assert resolve_as("user", email="") == {
        "email": "", "is_admin": False, "accessible_agents": [],
        "allowed_types": WORKSPACE_KINDS, "allowed_fields": WORKSPACE_FIELDS,
    }


@pytest.mark.parametrize("role", ["operator", "creator", "admin"])
def test_the_operator_rungs_resolve_with_no_kind_filter(resolve_as, role):
    identity = resolve_as(role)
    assert "allowed_types" in identity and "allowed_fields" in identity
    assert identity["allowed_types"] is None
    assert identity["allowed_fields"] is None


def test_the_service_names_the_same_set():
    assert getattr(ws_identity_service, "WORKSPACE_WS_EVENT_TYPES", None) == WORKSPACE_KINDS
    assert getattr(ws_identity_service, "WORKSPACE_WS_EVENT_FIELDS", None) == WORKSPACE_FIELDS


# ---------------------------------------------------------------------------
# The rule, through both delivery paths
# ---------------------------------------------------------------------------

_MEMBER_RECEIVES = [
    {"type": "agent_activity", "agent_name": "shared", "details": {"execution_id": "e1"}},
    {"type": "loop_run_completed", "agent_name": "shared", "loop_id": "l1"},
    {"type": "loop_completed", "agent_name": "shared", "loop_id": "l1"},
    {"type": "agent_skills_changed", "agent_name": "shared"},
    {"type": "resync_required", "reason": "server"},
]

_MEMBER_NEVER_RECEIVES = [
    {"type": "operator_queue_new", "data": {"agent_name": "shared", "id": "q1"}},
    {"type": "agent_notification", "agent_name": "shared", "notification_id": "n1", "title": "t"},
    {"type": "agent_report", "agent_name": "shared", "report_id": "r1"},
    # Agent-less, so fleet-visible to every non-admin before #837.
    {"type": "notifications_cleared", "count": 3},
    {"type": "room_message", "room_id": "room-1", "seq": 4},
    # Lifecycle events are keyed by `event`; the sharing one carries another email.
    {"event": "agent_shared", "data": {"name": "shared", "shared_with": "someone@example.com"}},
    {"event": "agent_started", "data": {"name": "shared"}},
    {"type": "", "event": "agent_stopped", "data": {"name": "shared"}},
    # No usable kind at all: withheld, not guessed.
    {"type": 5, "agent_name": "shared"},
    {"agent_name": "shared"},
]


@pytest.mark.asyncio
async def test_live_fanout_delivers_a_member_only_the_workspace_kinds(monkeypatch):
    dispatcher = StreamDispatcher()
    monkeypatch.setattr(dispatcher, "_maybe_invalidate_rosters", lambda _payload: None)
    member, operator = _slot(allowed_types=WORKSPACE_KINDS), _slot()
    dispatcher._clients = {"member": member, "operator": operator}

    payloads = _MEMBER_NEVER_RECEIVES + _MEMBER_RECEIVES
    for i, payload in enumerate(payloads, start=1):
        await dispatcher._fanout(f"1-{i}", _fields(payload))

    assert [_kind(p) for p in _drain(member)] == [p["type"] for p in _MEMBER_RECEIVES]
    # A slot without an allowlist is unchanged: every one of them still arrives.
    assert len(_drain(operator)) == len(payloads)


@pytest.mark.asyncio
async def test_a_lifecycle_event_is_named_by_its_event_key(monkeypatch):
    """The rule names a payload the way the SPA dispatches on it — `type`, else the
    lifecycle `event` key — so allowlisting a lifecycle kind delivers it instead of
    dropping it as kind-less."""
    dispatcher = StreamDispatcher()
    monkeypatch.setattr(dispatcher, "_maybe_invalidate_rosters", lambda _payload: None)
    slot = _slot(allowed_types=frozenset({"agent_started"}))
    dispatcher._clients = {"slot": slot}

    await dispatcher._fanout("1-1", _fields({"event": "agent_started", "data": {"name": "shared"}}))
    await dispatcher._fanout("1-2", _fields({"event": "agent_stopped", "data": {"name": "shared"}}))

    assert [_kind(p) for p in _drain(slot)] == ["agent_started"]


@pytest.mark.asyncio
async def test_the_kind_filter_narrows_the_agent_scope_and_never_widens_it(monkeypatch):
    dispatcher = StreamDispatcher()
    monkeypatch.setattr(dispatcher, "_maybe_invalidate_rosters", lambda _payload: None)
    member = _slot(allowed_types=WORKSPACE_KINDS)
    dispatcher._clients = {"member": member}

    await dispatcher._fanout("1-1", _fields({"type": "agent_activity", "agent_name": "foreign"}))

    assert member.queue.qsize() == 0


_RICH_ACTIVITY = {
    "type": "agent_activity", "agent_name": "shared", "activity_type": "chat_start",
    "activity_state": "completed", "action": "Processing: the owner's prompt",
    "details": {"message_preview": "the owner's prompt", "response_preview": "the reply",
                "cost_usd": 0.12, "session_id": "s-1"},
}


@pytest.mark.asyncio
async def test_live_fanout_cuts_a_members_payload_to_the_fields_the_workspace_reads(monkeypatch):
    dispatcher = StreamDispatcher()
    monkeypatch.setattr(dispatcher, "_maybe_invalidate_rosters", lambda _payload: None)
    member = _slot(allowed_types=WORKSPACE_KINDS, allowed_fields=WORKSPACE_FIELDS)
    operator = _slot()
    dispatcher._clients = {"member": member, "operator": operator}

    await dispatcher._fanout("1-1", _fields(_RICH_ACTIVITY))

    (received,) = _drain(member)
    assert received == {"type": "agent_activity", "agent_name": "shared",
                        "activity_state": "completed", "_eid": "1-1"}
    (full,) = _drain(operator)
    assert full["details"]["message_preview"] == "the owner's prompt"  # unchanged


@pytest.mark.asyncio
async def test_reconnect_replay_cuts_the_payload_the_same_way(monkeypatch):
    dispatcher = StreamDispatcher()
    member = _slot(allowed_types=WORKSPACE_KINDS, allowed_fields=WORKSPACE_FIELDS)

    class _FakeRedis:
        async def xrange(self, _key, min=None, max=None, count=None):  # noqa: A002
            if min == "-":
                return [("0-1", _fields({"type": "agent_activity", "agent_name": "shared"}))]
            return [("1-1", _fields(_RICH_ACTIVITY))]

    async def _fake_get_redis():
        return _FakeRedis()

    monkeypatch.setattr(dispatcher, "_get_redis", _fake_get_redis)
    await dispatcher._catchup("c", member, "1-0", "1-9")

    (received,) = _drain(member)
    assert set(received) == {"type", "agent_name", "activity_state", "_eid"}


@pytest.mark.asyncio
async def test_reconnect_replay_applies_the_same_kind_filter(monkeypatch):
    """`/ws` reconnects re-read history out of Redis; a filter wired only into the
    live fan-out would hand a member the whole backlog of operator events."""
    dispatcher = StreamDispatcher()
    member = _slot(allowed_types=WORKSPACE_KINDS)
    shared = {"name": "shared", "shared_with": "someone@example.com"}

    class _FakeRedis:
        async def xrange(self, _key, min=None, max=None, count=None):  # noqa: A002
            if min == "-":
                return [("0-1", _fields({"type": "agent_activity", "agent_name": "shared"}))]
            return [
                ("1-1", _fields({"type": "agent_activity", "agent_name": "shared"})),
                ("1-2", _fields({"type": "operator_queue_new", "data": {"agent_name": "shared"}})),
                ("1-3", _fields({"event": "agent_shared", "data": shared})),
            ]

    async def _fake_get_redis():
        return _FakeRedis()

    monkeypatch.setattr(dispatcher, "_get_redis", _fake_get_redis)
    await dispatcher._catchup("c", member, "1-0", "1-9")

    assert [_kind(p) for p in _drain(member)] == ["agent_activity"]


@pytest.mark.asyncio
async def test_register_gives_the_slot_its_allowlist_and_defaults_to_none():
    dispatcher = StreamDispatcher()

    async def _send(_payload):  # pragma: no cover
        return None

    member_id = await dispatcher.register(
        object(), scope=SCOPE_ALL, send_func=_send, accessible_agents=["shared"],
        email="member@example.com", allowed_types=["agent_activity"],
        allowed_fields=["type", "agent_name"],
    )
    operator_id = await dispatcher.register(
        object(), scope=SCOPE_ALL, send_func=_send, accessible_agents=["shared"],
        email="operator@example.com",
    )
    try:
        assert dispatcher._clients[member_id].allowed_types == frozenset({"agent_activity"})
        assert dispatcher._clients[member_id].allowed_fields == frozenset({"type", "agent_name"})
        assert dispatcher._clients[operator_id].allowed_types is None
        assert dispatcher._clients[operator_id].allowed_fields is None
    finally:
        dispatcher.unregister(member_id)
        dispatcher.unregister(operator_id)


# ---------------------------------------------------------------------------
# The wiring in main.py (importing main needs pristine sys.modules — #1483 —
# so the two hand-offs are pinned structurally; the paths they feed are
# executed above)
# ---------------------------------------------------------------------------

def _main_tree():
    return ast.parse((_BACKEND / "main.py").read_text())


def _function(tree, name, *, owner=None):
    scope = tree
    if owner:
        scope = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == owner)
    return next(
        n for n in ast.walk(scope)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


def _calls_to(fn, attr):
    return [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr
    ]


def _keyword(call, name):
    return next((k.value for k in call.keywords if k.arg == name), None)


def test_the_ws_endpoint_hands_the_identity_allowlist_to_connect():
    (call,) = _calls_to(_function(_main_tree(), "websocket_endpoint"), "connect")
    value = _keyword(call, "allowed_types")
    assert value is not None, "/ws registers a member without the kind allowlist"
    assert ast.unparse(value) == "identity['allowed_types']"
    fields = _keyword(call, "allowed_fields")
    assert fields is not None, "/ws registers a member without the field projection"
    assert ast.unparse(fields) == "identity['allowed_fields']"


def test_connect_hands_the_allowlist_to_the_dispatcher_and_defaults_closed():
    connect = _function(_main_tree(), "connect", owner="ConnectionManager")
    (call,) = _calls_to(connect, "register")
    value = _keyword(call, "allowed_types")
    assert value is not None and ast.unparse(value) == "allowed_types"
    fields = _keyword(call, "allowed_fields")
    assert fields is not None and ast.unparse(fields) == "allowed_fields"
    defaults = dict(zip((a.arg for a in connect.args.kwonlyargs),
                        (ast.unparse(d) for d in connect.args.kw_defaults)))
    # A caller that forgets either argument registers a socket that receives
    # nothing usable — noticed at once — rather than everything, silently.
    assert defaults.get("allowed_types") == "frozenset()"
    assert defaults.get("allowed_fields") == "frozenset()"
