"""ent#467 — `/ws` may not broadcast one tenant's agents to another.

`/ws` was `SCOPE_ALL` and unfiltered: every `agent_activity`, `agent_created`,
`operator_queue_*` and `agent_shared` event reached EVERY authenticated
client. Any `role=user` with a single shared agent could mint a `/ws` ticket
(`POST /api/ws/ticket` is plain `get_current_user`) and read the whole
instance's agent names, execution ids and activity types — and, from the two
sharing events, another user's email address. `/ws/events` had scoped by
`accessible_agents` since #306; `/ws` never did.

What is pinned here:

  * the visibility rule itself — a `/ws` client sees an event only when EVERY
    agent the payload names is one it may access, admins short-circuit before
    the roster is consulted, and an event naming no agent stays fleet-visible;
  * that BOTH delivery paths filter. Live fan-out is the obvious one; the
    reconnect replay (`_catchup`) re-reads history straight out of Redis, so a
    filter wired only into `_fanout` would hand the entire unfiltered backlog
    to any client that reconnects with `last-event-id`;
  * `SCOPE_SCOPED` (`/ws/events`) semantics are UNCHANGED — a security fix to
    one surface must not quietly move what the other one delivers;
  * identity resolution fails CLOSED — an unknown, suspended, or email-less
    subject closes the socket instead of registering an unscoped client;
  * and the **discovery guard**: every `/ws` broadcast payload in the tree is
    either agent-keyed (the extractor finds its agent) or explicitly listed as
    fleet-level with a reason. The extractor reads a fixed key vocabulary, so
    a new event shape that stores its agent under a novel key would be
    fleet-visible again — silently. The guard, not the extractor, is what
    stops that, which is why it is written over every call site rather than
    over the ones this issue named.
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

from services import event_bus as event_bus_mod
from services.event_bus import (
    SCOPE_ALL,
    SCOPE_SCOPED,
    StreamDispatcher,
    agent_names_in_payload,
)
from services.ws_identity_service import resolve_ws_identity

_ClientSlot = event_bus_mod._ClientSlot
_event_is_visible = event_bus_mod._event_is_visible

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"

pytestmark = pytest.mark.unit


def _slot(scope=SCOPE_ALL, *, is_admin=False, agents=(), email="user@example.com"):
    async def _send(_payload):  # pragma: no cover — never awaited in these tests
        return None

    return _ClientSlot(
        ws=object(),
        scope=scope,
        send_func=_send,
        is_admin=is_admin,
        accessible_agents=set(agents),
        email=email,
    )


# ---------------------------------------------------------------------------
# Which agents does a payload name?
# ---------------------------------------------------------------------------

# Verbatim shapes of the live `/ws` broadcasts, one per distinct payload
# layout. They disagree about where the agent name lives, which is the whole
# reason identity is derived in one place instead of at each call site.
LIVE_PAYLOADS = {
    "agent_activity": ({"type": "agent_activity", "agent_name": "alpha", "details": {}}, {"alpha"}),
    "agent_created": ({"event": "agent_created", "data": {"name": "alpha", "port": 2222}}, {"alpha"}),
    "agent_deleted": ({"event": "agent_deleted", "data": {"name": "alpha"}}, {"alpha"}),
    "agent_started": (
        {"event": "agent_started", "type": "agent_started", "name": "alpha",
         "data": {"name": "alpha", "credentials_injection": "ok"}},
        {"alpha"},
    ),
    "agent_renamed": (
        {"event": "agent_renamed", "type": "agent_renamed", "name": "beta",
         "data": {"old_name": "alpha", "new_name": "beta"}},
        {"alpha", "beta"},
    ),
    "agent_shared": (
        {"event": "agent_shared", "data": {"name": "alpha", "shared_with": "other@example.com"}},
        {"alpha"},
    ),
    "operator_queue_new": (
        {"type": "operator_queue_new", "data": {"id": "q1", "agent_name": "alpha"}},
        {"alpha"},
    ),
    "agent_collaboration": (
        {"type": "agent_collaboration", "source_agent": "alpha", "target_agent": "beta"},
        {"alpha", "beta"},
    ),
    "agent_tags_changed": ({"type": "agent_tags_changed", "agent_name": "alpha"}, {"alpha"}),
    "chat_response_ready": (
        {"type": "chat_response_ready", "execution_id": "e1", "agent_name": "alpha"},
        {"alpha"},
    ),
    "notifications_cleared_all": (
        {"type": "notifications_cleared", "data": {"count": 3, "agent_name": None}},
        set(),
    ),
    "resync_required": ({"type": "resync_required", "reason": "trimmed"}, set()),
}


@pytest.mark.parametrize("label", sorted(LIVE_PAYLOADS))
def test_agent_names_are_derived_from_the_live_payload_shapes(label):
    payload, expected = LIVE_PAYLOADS[label]
    assert agent_names_in_payload(payload) == expected


def test_details_is_read_narrowly():
    """`details` is free-form: an activity's `details["name"]` can hold a TOOL
    name. Reading it as an agent would hide the event from the agent's own
    owner — over-filtering is how this change breaks a working UI, so only the
    two collaboration keys are read there."""
    payload = {"type": "agent_activity", "agent_name": "alpha",
               "details": {"name": "Bash", "tool_name": "Bash"}}
    assert agent_names_in_payload(payload) == {"alpha"}
    collab = {"type": "agent_activity", "agent_name": "alpha",
              "details": {"source_agent": "alpha", "target_agent": "beta"}}
    assert agent_names_in_payload(collab) == {"alpha", "beta"}


def test_non_dict_and_blank_values_are_not_agents():
    assert agent_names_in_payload(None) == frozenset()
    assert agent_names_in_payload("nope") == frozenset()
    assert agent_names_in_payload({"agent_name": "", "data": {"name": "   "}}) == frozenset()
    assert agent_names_in_payload({"agent_name": 17, "data": None}) == frozenset()


# ---------------------------------------------------------------------------
# The visibility rule
# ---------------------------------------------------------------------------

def test_ws_client_does_not_see_an_agent_it_cannot_access():
    """The reported disclosure, stated as a test."""
    slot = _slot(agents={"mine"})
    foreign = frozenset({"someone-elses-agent"})
    assert _event_is_visible(slot, SCOPE_ALL, None, foreign) is False


def test_ws_client_still_sees_its_own_agents():
    slot = _slot(agents={"mine"})
    assert _event_is_visible(slot, SCOPE_ALL, None, frozenset({"mine"})) is True


def test_an_event_naming_no_agent_stays_fleet_visible():
    """Fail-open by design — `resync_required` and a fleet-wide
    `notifications_cleared` name nobody. The discovery guard below is what
    keeps this from becoming a silent hole."""
    slot = _slot(agents=set())
    assert _event_is_visible(slot, SCOPE_ALL, None, frozenset()) is True
    assert _event_is_visible(slot, SCOPE_ALL, None, None) is True


def test_a_multi_agent_event_needs_every_agent_accessible():
    """A collaboration between an agent you own and one you cannot see is
    withheld: the value being protected is the other agent's existence."""
    slot = _slot(agents={"mine"})
    assert _event_is_visible(slot, SCOPE_ALL, None, frozenset({"mine", "theirs"})) is False


def test_admins_are_never_filtered_and_never_need_a_roster():
    """An admin's roster would be a connect-time snapshot; filtering on it
    would blind the operator to every agent created after the page loaded."""
    slot = _slot(is_admin=True, agents=set())
    assert _event_is_visible(slot, SCOPE_ALL, None, frozenset({"anything"})) is True


def test_scoped_surface_semantics_are_unchanged():
    """`/ws/events` (#306) keeps keying on the single envelope `agent_name`
    `publish()` infers — this fix must not move what that surface delivers."""
    scoped = _slot(SCOPE_SCOPED, agents={"a"})
    assert _event_is_visible(scoped, SCOPE_SCOPED, "a", None) is True
    assert _event_is_visible(scoped, SCOPE_SCOPED, "b", None) is False
    assert _event_is_visible(scoped, SCOPE_SCOPED, None, None) is False
    assert _event_is_visible(scoped, SCOPE_ALL, "a", frozenset({"a"})) is False
    admin_scoped = _slot(SCOPE_SCOPED, is_admin=True)
    assert _event_is_visible(admin_scoped, SCOPE_SCOPED, "anything", None) is True


# ---------------------------------------------------------------------------
# Both delivery paths, not just the obvious one
# ---------------------------------------------------------------------------

def _fields(payload: dict, scope: str = SCOPE_ALL) -> dict:
    return {"payload": json.dumps(payload), "scope": scope, "agent_name": ""}


@pytest.mark.asyncio
async def test_live_fanout_withholds_a_foreign_agents_activity():
    dispatcher = StreamDispatcher()
    mine, theirs = _slot(agents={"mine"}), _slot(agents={"theirs"}, email="b@example.com")
    dispatcher._clients = {"a": mine, "b": theirs}

    await dispatcher._fanout("1-1", _fields(
        {"type": "agent_activity", "agent_name": "mine", "details": {"execution_id": "e1"}}
    ))

    assert mine.queue.qsize() == 1
    assert theirs.queue.qsize() == 0


@pytest.mark.asyncio
async def test_reconnect_replay_applies_the_same_filter(monkeypatch):
    """A `/ws` reconnect passes `last-event-id` and the dispatcher re-reads
    history from Redis. A filter wired only into `_fanout` would hand the
    whole unfiltered backlog to the client that asked for it."""
    dispatcher = StreamDispatcher()
    slot = _slot(agents={"mine"})

    class _FakeRedis:
        async def xrange(self, _key, min=None, max=None, count=None):  # noqa: A002
            if min == "-":
                # Oldest surviving id — below the client's cursor, so the
                # dispatcher's trim-race check passes and we exercise replay
                # itself rather than the resync path.
                return [("0-1", _fields({"type": "agent_activity", "agent_name": "mine"}))]
            return [
                ("1-1", _fields({"type": "agent_activity", "agent_name": "mine"})),
                ("1-2", _fields({"type": "agent_activity", "agent_name": "theirs"})),
            ]

    async def _fake_get_redis():
        return _FakeRedis()

    monkeypatch.setattr(dispatcher, "_get_redis", _fake_get_redis)
    await dispatcher._catchup("c", slot, "1-0", "1-9")

    delivered = [slot.queue.get_nowait()[1] for _ in range(slot.queue.qsize())]
    assert [d.get("agent_name") for d in delivered] == ["mine"]


def test_both_delivery_paths_pass_the_derived_set():
    """A path that forgets the argument silently reverts to fleet-visible —
    the pre-fix behaviour — so the wiring is pinned structurally rather than
    left to whoever edits the dispatcher next."""
    tree = ast.parse((_BACKEND / "services" / "event_bus.py").read_text())
    for fn_name in ("_fanout", "_catchup"):
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name
        )
        calls = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "_event_is_visible"
        ]
        assert calls, f"{fn_name} no longer consults _event_is_visible"
        for call in calls:
            assert len(call.args) >= 4, (
                f"{fn_name} calls _event_is_visible without the derived agent set — "
                "the SCOPE_ALL filter is inert on that path"
            )


# ---------------------------------------------------------------------------
# A share granted mid-connection has to reach a live socket
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_access_changing_event_refreshes_a_live_roster():
    dispatcher = StreamDispatcher()
    slot = _slot(agents=set())
    dispatcher._clients = {"a": slot}
    dispatcher.set_accessible_resolver(lambda email: ["newly-shared"])

    await dispatcher._fanout("1-1", _fields(
        {"event": "agent_shared", "data": {"name": "newly-shared", "shared_with": "user@example.com"}}
    ))
    await asyncio.gather(*list(dispatcher._refresh_tasks))

    assert slot.accessible_agents == {"newly-shared"}


@pytest.mark.asyncio
async def test_the_refresh_is_deduped_per_user_not_per_socket():
    """Several browser tabs are several slots with one roster. Per-slot
    refreshing would fire one DB read per tab into a ~32-thread pool on every
    agent-create."""
    calls = []
    dispatcher = StreamDispatcher()
    tabs = [_slot(agents=set(), email="same@example.com") for _ in range(3)]
    other = _slot(agents=set(), email="other@example.com")
    dispatcher._clients = {str(i): s for i, s in enumerate(tabs + [other])}

    def _resolver(email):
        calls.append(email)
        return ["shared-with-both"]

    dispatcher.set_accessible_resolver(_resolver)
    await dispatcher._fanout("1-1", _fields({"event": "agent_created", "data": {"name": "x"}}))
    await asyncio.gather(*list(dispatcher._refresh_tasks))

    assert sorted(calls) == ["other@example.com", "same@example.com"]
    for slot in tabs + [other]:
        assert slot.accessible_agents == {"shared-with-both"}


@pytest.mark.asyncio
async def test_an_ordinary_event_does_not_hit_the_database():
    calls = []
    dispatcher = StreamDispatcher()
    dispatcher._clients = {"a": _slot(agents={"mine"})}
    dispatcher.set_accessible_resolver(lambda email: calls.append(email) or [])

    await dispatcher._fanout("1-1", _fields({"type": "agent_activity", "agent_name": "mine"}))
    await asyncio.sleep(0)

    assert calls == [], "roster refresh must be event-triggered, never per-event polling"


@pytest.mark.asyncio
async def test_a_failing_resolver_leaves_the_previous_roster_in_place():
    """The stale roster is the older, smaller one — degrading to it is the
    safe direction; emptying it would break a working UI."""
    dispatcher = StreamDispatcher()
    slot = _slot(agents={"mine"})
    dispatcher._clients = {"a": slot}

    def _boom(_email):
        raise RuntimeError("db down")

    dispatcher.set_accessible_resolver(_boom)
    await dispatcher._fanout("1-1", _fields({"event": "agent_created", "data": {"name": "x"}}))
    await asyncio.gather(*list(dispatcher._refresh_tasks))

    assert slot.accessible_agents == {"mine"}
    assert slot.refreshing is False


# ---------------------------------------------------------------------------
# Identity resolution fails closed
# ---------------------------------------------------------------------------

class _FakeDb:
    def __init__(self, user=None, agents=None, raises=False):
        self._user, self._agents, self._raises = user, agents or [], raises

    def get_user_by_username(self, _username):
        if self._raises:
            raise RuntimeError("db down")
        return self._user

    def get_accessible_agent_names(self, _email, _is_admin):
        return self._agents


@pytest.fixture
def fake_db(monkeypatch):
    import database

    def _install(**kwargs):
        monkeypatch.setattr(database, "db", _FakeDb(**kwargs))

    return _install


@pytest.mark.parametrize("kwargs", [
    {"user": None},                                                    # unknown subject
    {"user": {"role": "user", "email": "u@e.com", "suspended_at": "2026-01-01T00:00:00Z"}},
    {"raises": True},                                                  # DB unreachable
])
def test_unresolvable_identities_close_the_socket(fake_db, kwargs):
    fake_db(**kwargs)
    assert resolve_ws_identity("someone") is None


def test_a_non_admin_without_an_email_is_resolved_with_an_empty_roster(fake_db):
    """Not a refusal: `agent_ownership` joins `users.email` and `agent_sharing`
    is keyed on it, so this user can access no agent. The empty set is the
    exact answer, and the frontend never retries a 4001 — refusing would leave
    a legacy row permanently dark instead of merely agent-less."""
    fake_db(user={"role": "user", "email": ""}, agents=["should-not-be-read"])
    assert resolve_ws_identity("someone") == {
        "email": "", "is_admin": False, "accessible_agents": [],
    }


def test_a_non_admin_gets_only_its_accessible_agents(fake_db):
    fake_db(user={"role": "user", "email": "u@e.com"}, agents=["mine"])
    identity = resolve_ws_identity("someone")
    assert identity == {"email": "u@e.com", "is_admin": False, "accessible_agents": ["mine"]}


def test_an_admin_is_flagged_and_carries_no_roster(fake_db):
    fake_db(user={"role": "admin", "email": "a@e.com"}, agents=["everything"])
    identity = resolve_ws_identity("admin")
    assert identity["is_admin"] is True
    assert identity["accessible_agents"] == []


def test_the_ws_endpoint_refuses_an_unresolvable_subject():
    """The endpoint must CLOSE, not accept-then-register-empty: a socket that
    connects and silently receives nothing is the failure nobody reports."""
    source = (_BACKEND / "main.py").read_text()
    start = source.index('@app.websocket("/ws")')
    body = source[start:source.index('@app.websocket("/ws/events")')]
    assert "resolve_ws_identity" in body
    assert "if identity is None:" in body
    close_at = body.index("if identity is None:")
    connect_at = body.index("await manager.connect(")
    assert close_at < connect_at, "identity is resolved after the socket is registered"


def test_connect_defaults_are_fail_closed():
    """A caller that forgets the identity arguments registers a client that
    sees only agent-less events, never the whole fleet."""
    source = (_BACKEND / "main.py").read_text()
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "connect"
    )
    assert [a.arg for a in fn.args.kwonlyargs] == ["email", "is_admin", "accessible_agents"]
    assert [getattr(d, "value", "sentinel") for d in fn.args.kw_defaults] == ["", False, None]


# ---------------------------------------------------------------------------
# Discovery guard — every `/ws` payload is classified
# ---------------------------------------------------------------------------

# Payloads the extractor cannot key, each with the reason it is fleet-visible.
# An entry here is a decision, not an exemption to be copied: adding one means
# asserting that the event names no agent a stranger should not learn about.
FLEET_LEVEL_ALLOWLIST = {
    # A bulk "clear resolved"/"dismiss all" reports a count for the acting
    # operator's own accessible set; it names no agent.
    ("routers/operator_queue.py", "operator_queue_cleared"),
    # ent#170 room triggers are ids only (`room_id` + `seq`). `identity` on
    # `room_participant_state` IS an agent name, but the column is polymorphic
    # (agent name / user id / client email), so keying on it would drop the
    # event for the room's own human participants. Scoping room events by room
    # membership is tracked separately — see the debt inbox entry for ent#467.
    ("shared_sessions/service.py", "<dynamic>"),
}


def _dict_keys(node: ast.Dict):
    return [
        (k.value, v)
        for k, v in zip(node.keys, node.values)
        if isinstance(k, ast.Constant) and isinstance(k.value, str)
    ]


def _resolve_payload(node, assigns, depth=0):
    """Follow `broadcast(json.dumps(event))` back to the dict literal."""
    if depth > 4 or node is None:
        return None
    if isinstance(node, ast.Dict):
        return node
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "dumps":
        return _resolve_payload(node.args[0] if node.args else None, assigns, depth + 1)
    if isinstance(node, ast.Name):
        return _resolve_payload(assigns.get(node.id), assigns, depth + 1)
    return None


def _broadcast_payloads():
    """Every `/ws` broadcast payload in the OSS tree, as (file, event, keys, line).

    Discovers by callee NAME (`*.broadcast(...)` and local `_broadcast({...})`
    wrappers) rather than from a list of known sites, so a new publisher is
    found rather than assumed absent.

    Name resolution is scoped: a `broadcast(event_json)` is followed back to
    its dict literal using only the assignments of the function it sits in
    (module-level assignments for a module-level call). A whole-file `assigns`
    map would resolve `event` in one function from an `event = {...}` in
    another and classify the wrong payload — the file has fourteen locals
    called `event`. Results are keyed by (file, line) so the two passes cannot
    double-count.
    """
    found = {}
    for path in sorted(_BACKEND.rglob("*.py")):
        rel = path.relative_to(_BACKEND).as_posix()
        if rel.startswith("enterprise/"):
            continue  # private submodule owns its own twin (the #1677 convention)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        functions = [
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        scopes = [(tree, tree.body)] + [(fn, ast.walk(fn)) for fn in functions]
        for scope, assign_source in scopes:
            assigns = {
                n.targets[0].id: n.value
                for n in assign_source
                if isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
            }
            for node in ast.walk(scope):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                name = (
                    node.func.attr if isinstance(node.func, ast.Attribute)
                    else node.func.id if isinstance(node.func, ast.Name) else ""
                )
                if "broadcast" not in name or "filtered" in name:
                    continue
                payload = _resolve_payload(node.args[0], assigns)
                if payload is None:
                    continue
                items = _dict_keys(payload)
                event = next(
                    (v.value for k, v in items
                     if k in ("event", "type") and isinstance(v, ast.Constant)),
                    "<dynamic>",
                )
                skeleton = {}
                for key, value in items:
                    if isinstance(value, ast.Dict):
                        skeleton[key] = {kk: "x" for kk, _ in _dict_keys(value)}
                    else:
                        skeleton[key] = "x"
                # A function-scope hit supersedes the module-scope pass, which
                # only sees module-level assignments and may not resolve at all.
                found[(rel, node.lineno)] = (rel, event, skeleton, node.lineno)
    return [found[key] for key in sorted(found)]


def test_the_guard_actually_finds_the_broadcast_sites():
    """A discovery guard that discovers nothing is a green test that proves
    nothing — pin the floor and the known-loud events."""
    payloads = _broadcast_payloads()
    assert len({(rel, line) for rel, _, _, line in payloads}) == len(payloads), (
        "the same call site was counted twice — the module and function passes "
        "are no longer deduped, which inflates the floor below into nonsense"
    )
    # 36 sites today. A floor, not a snapshot: deleting a broadcast is normal,
    # discovery silently resolving nothing is not.
    assert len(payloads) >= 30, f"only found {len(payloads)} /ws payloads — discovery broke"
    events = {event for _, event, _, _ in payloads}
    for expected in ("agent_activity", "agent_created", "operator_queue_new", "agent_shared"):
        assert expected in events, f"{expected} is no longer discovered"


def test_every_ws_payload_is_agent_keyed_or_explicitly_fleet_level():
    unclassified = []
    for rel, event, skeleton, lineno in _broadcast_payloads():
        if agent_names_in_payload(skeleton):
            continue
        if (rel, event) in FLEET_LEVEL_ALLOWLIST:
            continue
        unclassified.append(f"{rel}:{lineno} ({event})")
    assert unclassified == [], (
        "these /ws broadcasts name no agent the scope filter can read, so they "
        "reach every authenticated client (ent#467): "
        + ", ".join(unclassified)
        + ". Either key the payload with one of the vocabulary keys in "
        "services/event_bus.py, or add it to FLEET_LEVEL_ALLOWLIST with the "
        "reason it is safe fleet-wide."
    )
