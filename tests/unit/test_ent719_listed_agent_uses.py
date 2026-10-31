"""trinity-enterprise#719 — the channel, VoIP, agent-delete and event-subscription
routes an agent key may use, as the route census lists them.

These eleven routes sat in the frozen human-only baseline with no ruling. Each
is a USE of something a person granted: a channel binding and its proactive
consent, a VoIP binding, the `agents.manage` capability, the edge to an event
source. So the census lists them in `AGENT_CALLABLE_ANY`, each with its answer
to "why may an agent other than the one in the path reach this". No runtime
behaviour changes. These tests pin that answer through the real routers, so a
later change that widens a route's reach fails here.

Every principal is a real credential through real authentication, with no
dependency override: an agent's own MCP key and a person's `user`-scoped key
(both minted by the real key store), a signed-in session JWT, and a real signed
event-loopback JWT. The fleet has an admin owner, the worst case, since every
owner/access predicate answers yes for that role:

* A, the calling agent;
* B, an agent A holds an `agent_permissions` edge to (use reach, never manage);
* C, a sibling A has no edge to;
* D, a durable agent A spawned with the key it holds now (use and manage reach);
* P, a durable agent A spawned under a key it no longer holds (outside both: a
  spawn record counts only with the caller's current key);
* G, an ephemeral ("ghost") agent A spawned;
* F, another user's agent.

Channel and VoIP data are stubbed at each router's own accessors, which no gate
reads. Every gate predicate reads the real production schema: `db_harness.bootstrap_schema` builds it once
per module and each test starts on a fresh copy (SQLite).
"""

from __future__ import annotations

import importlib
import re
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _route_census as rc  # noqa: E402

_TESTS = str(Path(__file__).resolve().parents[1])
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)
from db_harness import bootstrap_schema, run, scalar, seed_agent, seed_user  # noqa: E402

# Bind the real routers at collection (the test_ent872 pattern). A sibling file
# that monkeypatches a gate on `dependencies` during one of its tests would
# otherwise hand its stand-in to a router module first imported inside that
# test, for the rest of the session.
import routers.agents  # noqa: E402,F401
import routers.event_subscriptions  # noqa: E402,F401
import routers.slack  # noqa: E402,F401
import routers.telegram  # noqa: E402,F401
import routers.voip  # noqa: E402,F401

pytestmark = pytest.mark.unit

OWNER, OWNER_EMAIL = "owner", "owner@example.com"
OTHER, OTHER_EMAIL = "other", "other@example.com"
A, B, C, D, G, F = "a-alpha", "a-beta", "a-gamma", "a-delta", "a-ghost", "o-foreign"
P = "a-delta-prior"  # spawned by A under a key A no longer holds

USE_REACH = {A, B, D}  # itself, its edge target, the agent it spawned
MANAGE_REACH = {A, D}  # itself, the agent it spawned; an edge is never "manage"

ROUTER_MODULES = (
    ("routers.telegram", "auth_router"),
    ("routers.slack", "auth_router"),
    ("routers.voip", "auth_router"),
    ("routers.event_subscriptions", "router"),
    ("routers.agents", "router"),
)
LOOPBACK_FENCE_DETAIL = "Event-loopback tokens may only dispatch a subscriber task"

S1A = (
    "routers/telegram.py::list_telegram_groups",
    "routers/telegram.py::send_telegram_group_message",
    "routers/slack.py::list_agent_slack_channels",
    "routers/slack.py::get_agent_slack_channel",
    "routers/slack.py::send_agent_slack_channel_message",
    "routers/voip.py::place_voip_call",
    "routers/voip.py::get_voip_binding",
    "routers/agents.py::delete_agent_endpoint",
)
S1B = (
    "routers/event_subscriptions.py::create_event_subscription",
    "routers/event_subscriptions.py::update_event_subscription",
    "routers/event_subscriptions.py::delete_event_subscription",
)


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


_GATES = ("assert_agent_access", "assert_agent_owner", "enforce_agent_spawn_scope", "capability_fence")


def _assert_real_gates(modules) -> None:
    """Fail loudly, naming the leak, if a router module holds a stand-in for one
    of its gates (a sibling test's monkeypatch, bound when it was imported)."""
    for mod in modules:
        for name in _GATES:
            fn = getattr(mod, name, None)
            if fn is None:
                continue
            assert (getattr(fn, "__module__", None), getattr(fn, "__name__", None)) == (
                "dependencies",
                name,
            ), f"{mod.__name__}.{name} is {fn!r}, not the real gate"


# Where a listed route's dependency callables may come from: the gates in
# `dependencies`, and FastAPI's own bearer scheme under `get_current_user`.
_DEPENDENCY_MODULES = frozenset({"dependencies", "fastapi.security.oauth2"})


def _census_key(route) -> str:
    endpoint = route.endpoint
    return f"{endpoint.__module__.replace('.', '/')}.py::{endpoint.__name__}"


def _assert_real_route_dependencies(routers) -> None:
    """Fail loudly, naming the leak, if a listed route resolves a dependency
    that is not defined in `dependencies`.

    `_assert_real_gates` sees only the gates a router imports by name. The
    Telegram and VoIP routes, the subscription create and the agent delete take
    their gate as a path dependency instead (`AuthorizedAgentByName`,
    `OwnedAgentByName`, `OwnedAgent`, a `capability_fence`), which lives in the
    route's signature, so a router first imported while a sibling test had
    patched one of those keeps the stand-in there."""
    listed = set(S1A + S1B)
    seen = set()

    def walk(route, dependant):
        for sub in dependant.dependencies:
            call = sub.call
            module = getattr(call, "__module__", None) or type(call).__module__
            assert (
                module in _DEPENDENCY_MODULES
            ), f"{_census_key(route)} resolves {call!r} from {module}, not a dependencies gate"
            walk(route, sub)

    for router in routers:
        for route in router.routes:
            if getattr(route, "dependant", None) is None:
                continue
            key = _census_key(route)
            if key in listed:
                seen.add(key)
                walk(route, route.dependant)
    assert seen == listed, f"listed routes not mounted: {sorted(listed - seen)}"


def _routers():
    return [getattr(importlib.import_module(m), attr) for m, attr in ROUTER_MODULES]


def _auth_namespaces(routers) -> list:
    """The namespaces of every `get_current_user` the mounted routes resolve.

    The unit conftest pops `dependencies` between tests, so a fresh import is
    not the module the routes call, and in a full-suite run two routers may
    have been imported against two different copies of it. Patching through
    these namespaces patches what each request really runs (learnings:
    identity-keyed registries). Every copy must read the same database
    object, or the gates would not be reading the fixture's schema."""
    found = []

    def walk(dependant):
        for sub in dependant.dependencies:
            if getattr(sub.call, "__name__", "") == "get_current_user":
                found.append(sub.call)
            walk(sub)

    for router in routers:
        for route in router.routes:
            if getattr(route, "dependant", None) is not None:
                walk(route.dependant)
    namespaces = list({id(f.__globals__): f.__globals__ for f in found}.values())
    assert namespaces, "no mounted route authenticates through get_current_user"
    assert all(
        ns["db"] is namespaces[0]["db"] for ns in namespaces
    ), "the mounted routes' dependencies copies read different database objects"
    return namespaces


def _set(sql: str, **binds) -> None:
    run(sql, **binds)


@pytest.fixture(scope="module")
def _schema_template(tmp_path_factory):
    """The production schema, built ONCE per module with `db_harness`'s own
    builder, so a test starts from a byte copy instead of re-running every DDL
    statement (about ten seconds a test on a loaded laptop)."""
    from db.engine import dispose_engines

    template = tmp_path_factory.mktemp("ent719-schema") / "trinity.db"
    patch = pytest.MonkeyPatch()
    try:
        patch.setenv("TRINITY_DB_PATH", str(template))
        patch.delenv("DATABASE_URL", raising=False)
        dispose_engines()
        bootstrap_schema()
    finally:
        dispose_engines()
        patch.undo()
    return template


@pytest.fixture
def fresh_db(_schema_template, tmp_path, monkeypatch):
    """A fresh database per test: a copy of this module's template."""
    from db.engine import dispose_engines

    path = tmp_path / "trinity.db"
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(_schema_template) + suffix)
        if source.exists():
            shutil.copyfile(source, str(path) + suffix)
    monkeypatch.setenv("TRINITY_DB_PATH", str(path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    dispose_engines()
    try:
        yield path
    finally:
        dispose_engines()


@pytest.fixture
def fleet(fresh_db, monkeypatch):
    """The fleet above, with real keys, on a fresh schema."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _assert_real_gates([importlib.import_module(m) for m, _attr in ROUTER_MODULES])
    routers = _routers()
    _assert_real_route_dependencies(routers)
    namespaces = _auth_namespaces(routers)
    deps = namespaces[0]
    db = deps["db"]

    seed_user(1, OWNER, role="admin")
    seed_user(2, OTHER, role="user")
    _set("UPDATE users SET email = :e WHERE id = 1", e=OWNER_EMAIL)
    _set("UPDATE users SET email = :e WHERE id = 2", e=OTHER_EMAIL)
    for name in (A, B, C, D, G, P):
        seed_agent(name, owner_id=1)
    seed_agent(F, owner_id=2)
    db.add_agent_permission(A, B, created_by=OWNER)

    agent_key = db.create_agent_mcp_api_key(A, OWNER)
    _set(
        "UPDATE agent_ownership SET spawned_by_agent = :a, spawned_by_key_id = :k "
        "WHERE agent_name IN (:d, :g)",
        a=A,
        k=agent_key.id,
        d=D,
        g=G,
    )
    _set("UPDATE agent_ownership SET is_ephemeral = 1 WHERE agent_name = :g", g=G)
    _set(
        "UPDATE agent_ownership SET spawned_by_agent = :a, spawned_by_key_id = :k "
        "WHERE agent_name = :p",
        a=A,
        k="key-retired-before-this-one",
        p=P,
    )

    from db_models import McpApiKeyCreate

    user_key = db.create_mcp_api_key(OWNER, McpApiKeyCreate(name="owner-scripts"))
    assert user_key.scope == "user"

    # A signed-in session: the JWT branch also asks Redis whether the token was
    # revoked; the unit tier has no Redis, so answer "not revoked" there.
    for ns in namespaces:
        monkeypatch.setitem(ns, "is_token_revoked", lambda *_a, **_k: False)
        monkeypatch.setitem(ns, "session_predates_user_revoke", lambda *_a, **_k: False)
    session = deps["create_access_token"]({"sub": OWNER})

    app = FastAPI()
    for router in routers:
        app.include_router(router)
    client = TestClient(app, raise_server_exceptions=True)

    def as_(token):
        return {"Authorization": f"Bearer {token}"}

    return type(
        "Fleet",
        (),
        {
            "db": db,
            "deps": deps,
            "client": client,
            "agent": as_(agent_key.api_key),
            "user_key": as_(user_key.api_key),
            "session": as_(session),
            "as_": staticmethod(as_),
        },
    )()


def _url(key: str, agent: str, **params) -> tuple:
    """The census entry's own METHOD and path, with its parameters filled in.

    Driving the stored path (not a copy of it) ties every request below to the
    entry the census pins against the live app."""
    method, path = rc.AGENT_CALLABLE_ANY[key][0].split(" ", 1)
    values = {"name": agent, "agent_name": agent, **params}
    return method, re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]), path)


def _call(fleet, key, agent, headers, body=None, **params):
    method, url = _url(key, agent, **params)
    return fleet.client.request(method, url, json=body, headers=headers)


# --------------------------------------------------------------------------- #
# The census lists them
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def classified():
    baseline = rc.load_baseline()
    classes, failures = rc.classify(rc.walk(), baseline)
    return baseline, classes, failures



@pytest.mark.parametrize("key", S1A + S1B)
def test_each_use_is_listed_agent_callable_any_and_not_in_the_baseline(classified, key):
    baseline, classes, failures = classified
    assert classes[key] == "agent_callable_any"
    assert key not in baseline
    assert not [m for _t, m in failures if key in m]
    _mp, reason, sibling = rc.AGENT_CALLABLE_ANY[key]
    assert reason.strip() and sibling.strip()


# --------------------------------------------------------------------------- #
# S1a — channel and VoIP uses: each sibling answer, through the real router
# --------------------------------------------------------------------------- #


class _Sent:
    """What the stubbed transports were asked to deliver."""

    def __init__(self):
        self.items = []


@pytest.fixture
def channels(fleet, monkeypatch):
    """A Telegram group, a Slack channel and a VoIP binding on every agent, and
    transports that record instead of sending."""
    telegram = importlib.import_module("routers.telegram")
    slack = importlib.import_module("routers.slack")
    voip = importlib.import_module("routers.voip")
    sent = _Sent()

    # Telegram
    monkeypatch.setattr(
        telegram.db, "get_telegram_binding", lambda a: {"bot_id": f"bot-{a}"}
    )
    monkeypatch.setattr(
        telegram.db,
        "get_telegram_groups_for_agent",
        lambda a: [
            {"id": 1, "chat_id": "-100", "chat_title": f"ops-{a}", "is_active": True}
        ],
    )
    monkeypatch.setattr(telegram.db, "get_telegram_bot_token", lambda a: "tok")
    monkeypatch.setattr(telegram, "get_proactive_rate_limit", lambda _k: 0)

    class _Resp:
        status_code = 200

        def json(self):
            return {"result": {"message_id": len(sent.items)}}

    class _TelegramClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            sent.items.append(("telegram", json["text"]))
            return _Resp()

    monkeypatch.setattr(telegram.httpx, "AsyncClient", _TelegramClient)

    # Slack
    monkeypatch.setattr(
        slack.db,
        "get_all_slack_workspaces",
        lambda: [{"team_id": "T1", "team_name": "Acme"}],
    )
    monkeypatch.setattr(
        slack.db,
        "get_slack_channels_for_agent",
        lambda a: [
            {
                "slack_channel_id": "C1",
                "slack_channel_name": f"chan-{a}",
                "team_id": "T1",
                "is_dm_default": False,
                "allow_proactive": True,
            }
        ],
    )
    monkeypatch.setattr(
        slack.db,
        "get_slack_channel_for_agent",
        lambda t, a: {
            "slack_channel_id": "C1",
            "slack_channel_name": f"chan-{a}",
            "is_dm_default": False,
            "allow_proactive": True,
            "created_at": "2026-01-01T00:00:00Z",
        },
    )
    monkeypatch.setattr(slack.db, "get_slack_agents_for_workspace", lambda t: [A])
    monkeypatch.setattr(slack.db, "get_slack_workspace_bot_token", lambda t: "placeholder-bot-token")
    monkeypatch.setattr(slack, "get_proactive_rate_limit", lambda _k: 0)

    async def _slack_post(**kw):
        sent.items.append(("slack", kw["text"]))
        return True, None, "1700000000.000100"

    monkeypatch.setattr(slack.slack_service, "send_message_detailed", _slack_post)

    # VoIP
    monkeypatch.setattr(voip.voip_service, "is_available", lambda: True)
    monkeypatch.setattr(
        voip.db,
        "get_voip_binding",
        lambda a: {
            "account_sid": "AC" + "0" * 32,
            "from_number": "+15555550100",
            "daily_call_cap": 5,
            "display_name": a,
            "enabled": True,
            "auth_token_encrypted": "never-returned",
        },
    )
    monkeypatch.setattr(
        voip.settings_service,
        "get_public_chat_url_setting",
        lambda: "https://trinity.example.com",
    )

    async def _place_call(**kw):
        sent.items.append(("voip", kw["agent_name"]))
        return {
            "call_id": f"call-{kw['agent_name']}",
            "to_number": kw["to_number"],
            "status": "ringing",
        }

    async def _audit(**_kw):
        return None

    monkeypatch.setattr(voip.voip_service, "place_outbound_call", _place_call)
    monkeypatch.setattr(voip.platform_audit_service, "log", _audit)
    return sent


# key, body, extra path params, reach, the refusal outside it, a check on a pass
_READ_TG = (
    "routers/telegram.py::list_telegram_groups",
    None,
    {},
    "use",
    (404, "Agent not found"),
    lambda r, t: r.json()[0]["chat_title"] == f"ops-{t}",
)
_SEND_TG = (
    "routers/telegram.py::send_telegram_group_message",
    {"message": "status: green"},
    {"chat_id": "-100"},
    "manage",
    (404, "Agent not found"),
    lambda r, t: r.json()["ok"] is True,
)
_LIST_SL = (
    "routers/slack.py::list_agent_slack_channels",
    None,
    {},
    "use",
    (403, "Access denied"),
    lambda r, t: r.json()["channels"][0]["channel_name"] == f"chan-{t}",
)
_GET_SL = (
    "routers/slack.py::get_agent_slack_channel",
    None,
    {},
    "use",
    (403, "Access denied"),
    lambda r, t: r.json()["bound"] is True,
)
_SEND_SL = (
    "routers/slack.py::send_agent_slack_channel_message",
    {"message": "status: green"},
    {"channel_id": "C1"},
    "manage",
    (403, "Only owners can send channel messages"),
    lambda r, t: r.json()["sent"] is True,
)
_CALL = (
    "routers/voip.py::place_voip_call",
    {"to_number": "+15555550123"},
    {},
    "use",
    (404, "Agent not found"),
    lambda r, t: r.json()["call_id"] == f"call-{t}",
)
_VOIP = (
    "routers/voip.py::get_voip_binding",
    None,
    {},
    "manage",
    (404, "Agent not found"),
    lambda r, t: r.json()["configured"] is True
    and "auth_token_encrypted" not in r.json(),
)
CHANNEL_USES = [_READ_TG, _SEND_TG, _LIST_SL, _GET_SL, _SEND_SL, _CALL, _VOIP]
_WRITES = {_SEND_TG[0], _SEND_SL[0], _CALL[0]}


def _ids(rows):
    return [row[0].split("::")[1] for row in rows]


@pytest.mark.parametrize("target", [A, B, C, D, P, F])
@pytest.mark.parametrize("row", CHANNEL_USES, ids=_ids(CHANNEL_USES))
def test_an_agent_key_reaches_each_channel_use_only_inside_its_stated_reach(
    fleet, channels, row, target
):
    """The own key passes; a sibling inside the route's reach passes; a sibling
    outside it, or another user's agent, is refused and nothing is sent."""
    key, body, params, reach, (status, detail), passed = row
    inside = USE_REACH if reach == "use" else MANAGE_REACH
    response = _call(fleet, key, target, fleet.agent, body, **params)
    if target in inside:
        assert response.status_code == 200, response.text
        assert passed(response, target), response.text
        if key in _WRITES:
            assert len(channels.items) == 1
    else:
        assert (response.status_code, response.json()) == (status, {"detail": detail})
        assert channels.items == []


@pytest.mark.parametrize("person", ["session", "user_key"])
@pytest.mark.parametrize("row", CHANNEL_USES, ids=_ids(CHANNEL_USES))
def test_a_person_still_uses_every_channel_route(fleet, channels, row, person):
    """The owner, signed in or on their own `user` key, reaches an agent no
    agent key reaches (C), so the listing narrowed nothing for people."""
    key, body, params, _reach, _refusal, passed = row
    response = _call(fleet, key, C, getattr(fleet, person), body, **params)
    assert response.status_code == 200, response.text
    assert passed(response, C), response.text


# The census answers a driven row may carry, by its reach column. Comparing the
# answer with the constant itself, not with a word in it, means an answer
# reworded or swapped for another reach's changes this table with it.
_ANSWERS_FOR_REACH = {
    "use": (rc._SIB_READ, rc._SIB_USE),
    "manage": (rc._SIB_MANAGE,),
}


def test_the_reach_columns_match_the_census_answers():
    """Each row above states the reach its census answer names: a use row
    carries a use-reach answer and a manage row the manage-reach answer."""
    for key, _body, _params, reach, _refusal, _passed in CHANNEL_USES:
        assert rc.AGENT_CALLABLE_ANY[key][2] in _ANSWERS_FOR_REACH[reach], key
    for answer in _ANSWERS_FOR_REACH["use"]:
        assert "edge target" in answer and "spawned" in answer, answer
        assert "never through an edge" not in answer, answer
    for answer in _ANSWERS_FOR_REACH["manage"]:
        assert "spawned with its current key" in answer, answer
        assert "never through an edge" in answer, answer


# --------------------------------------------------------------------------- #
# S1a — agent delete has its own matrix
# --------------------------------------------------------------------------- #


@pytest.fixture
def deletes(fleet, monkeypatch):
    """No container (the #1747 path), the per-agent Redis cleanup and the
    ghost discard recorded instead of run."""
    agents = importlib.import_module("routers.agents")
    discarded = []

    async def _noop(*_a, **_k):
        return None

    async def _discard(name, reason):
        discarded.append(name)
        return True

    class _Capacity:
        cancel_all_overflow = staticmethod(_noop)

    monkeypatch.setattr(agents, "get_agent_container", lambda _name: None)
    monkeypatch.setattr(agents, "manager", None)
    monkeypatch.setattr(agents.platform_audit_service, "log", _noop)
    audit_mod = importlib.import_module("services.platform_audit_service")
    monkeypatch.setattr(audit_mod.platform_audit_service, "log", _noop)
    monkeypatch.setattr(
        importlib.import_module("services.capacity_manager"),
        "get_capacity_manager",
        lambda: _Capacity(),
    )
    monkeypatch.setattr(
        importlib.import_module("services.agent_runtime_state"),
        "clear_agent_runtime_state",
        _noop,
    )
    monkeypatch.setattr(
        importlib.import_module("services.skill_gate_service"),
        "cancel_pending_for_agent",
        _noop,
    )
    monkeypatch.setattr(
        importlib.import_module("services.agent_service.ephemeral"),
        "discard_ephemeral_agent",
        _discard,
    )
    return discarded


_DELETE = "routers/agents.py::delete_agent_endpoint"
_SPAWN_SCOPE = "Agent-scoped keys may only manage agents they spawned"


def _grant_agents_manage(fleet):
    fleet.db.grant_agent_capability(A, "agents.manage", granted_by=OWNER)


def _live(fleet, name) -> bool:
    return fleet.db.is_agent_live(name)


def test_an_agent_key_never_deletes_itself(fleet, deletes):
    """Without the grant the capability fence refuses it; with the grant the
    spawn scope does. An agent did not spawn itself."""
    response = _call(fleet, _DELETE, A, fleet.agent)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "agent_management_not_permitted"

    _grant_agents_manage(fleet)
    response = _call(fleet, _DELETE, A, fleet.agent)
    assert (response.status_code, response.json()) == (403, {"detail": _SPAWN_SCOPE})
    assert _live(fleet, A)


def test_a_spawned_durable_agent_needs_agents_manage(fleet, deletes):
    response = _call(fleet, _DELETE, D, fleet.agent)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "agent_management_not_permitted"
    assert _live(fleet, D)

    _grant_agents_manage(fleet)
    response = _call(fleet, _DELETE, D, fleet.agent)
    assert response.status_code == 200, response.text
    assert response.json() == {"message": f"Agent {D} deleted"}
    assert not _live(fleet, D)


def test_a_spawned_ghost_is_discarded_without_a_grant(fleet, deletes):
    response = _call(fleet, _DELETE, G, fleet.agent)
    assert response.status_code == 200, response.text
    assert deletes == [G]


@pytest.mark.parametrize("target", [B, C], ids=["edge-only", "unrelated"])
def test_an_agent_it_did_not_spawn_is_refused_even_with_the_grant(
    fleet, deletes, target
):
    """An edge is "may call", never "may delete"; holding `agents.manage` does
    not widen the spawn scope."""
    response = _call(fleet, _DELETE, target, fleet.agent)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "agent_management_not_permitted"

    _grant_agents_manage(fleet)
    response = _call(fleet, _DELETE, target, fleet.agent)
    assert (response.status_code, response.json()) == (403, {"detail": _SPAWN_SCOPE})
    assert _live(fleet, target)


def test_a_ghost_it_did_not_spawn_is_refused(fleet, deletes):
    """The ghost exemption skips the grant, never the spawn scope."""
    _set(
        "UPDATE agent_ownership SET spawned_by_agent = NULL, spawned_by_key_id = NULL "
        "WHERE agent_name = :g",
        g=G,
    )
    response = _call(fleet, _DELETE, G, fleet.agent)
    assert (response.status_code, response.json()) == (403, {"detail": _SPAWN_SCOPE})
    assert deletes == []


def test_an_agent_spawned_under_a_key_it_no_longer_holds_is_refused(fleet, deletes):
    """A spawn record counts only with the key the caller holds now: P names A as
    its parent, but under a key A no longer holds, so even the grant does not
    reach it."""
    _grant_agents_manage(fleet)
    response = _call(fleet, _DELETE, P, fleet.agent)
    assert (response.status_code, response.json()) == (403, {"detail": _SPAWN_SCOPE})
    assert _live(fleet, P)


def test_a_holder_never_deletes_another_users_agent(fleet, deletes):
    _grant_agents_manage(fleet)
    response = _call(fleet, _DELETE, F, fleet.agent)
    assert (response.status_code, response.json()) == (
        404,
        {"detail": "Agent not found"},
    )
    assert _live(fleet, F)


@pytest.mark.parametrize("person", ["session", "user_key"])
def test_a_person_deletes_an_agent(fleet, deletes, person):
    response = _call(fleet, _DELETE, C, getattr(fleet, person))
    assert response.status_code == 200, response.text
    assert not _live(fleet, C)


# --------------------------------------------------------------------------- #
# S1b — event-subscription writes
# --------------------------------------------------------------------------- #

_CREATE = "routers/event_subscriptions.py::create_event_subscription"
_UPDATE = "routers/event_subscriptions.py::update_event_subscription"
_REMOVE = "routers/event_subscriptions.py::delete_event_subscription"


def _subscription_body(source, event_type="report.ready"):
    return {
        "source_agent": source,
        "event_type": event_type,
        "target_message": "A report is ready: {{payload.id}}",
    }


@pytest.mark.parametrize("target", [A, B, C, D, P, F])
def test_an_agent_key_subscribes_only_an_agent_inside_its_manage_reach(fleet, target):
    """The subscriber is the agent itself or one it spawned; an edge target is
    not one it may subscribe on behalf of."""
    response = _call(fleet, _CREATE, target, fleet.agent, _subscription_body(target))
    if target in MANAGE_REACH:
        assert response.status_code == 201, response.text
        assert response.json()["subscriber_agent"] == target
    else:
        assert (response.status_code, response.json()) == (
            404,
            {"detail": "Agent not found"},
        )
        assert scalar("SELECT COUNT(*) FROM agent_event_subscriptions") == 0


def test_a_cross_agent_subscription_needs_the_edge_a_person_granted(fleet):
    """The source edge is the person's grant: A may listen to B (it holds the
    edge), never to C (it does not)."""
    response = _call(fleet, _CREATE, A, fleet.agent, _subscription_body(B))
    assert response.status_code == 201, response.text

    response = _call(
        fleet, _CREATE, A, fleet.agent, _subscription_body(C, "report.other")
    )
    assert response.status_code == 403
    assert scalar("SELECT COUNT(*) FROM agent_event_subscriptions") == 1


def test_a_spawned_subscriber_listens_only_through_its_own_edge(fleet):
    """For a subscriber A spawned, the edge that counts is the subscriber's: A's
    own edge to B does not let D listen to B, a person's grant of D -> B does,
    and C, which no agent holds an edge to, stays out of reach."""
    response = _call(fleet, _CREATE, D, fleet.agent, _subscription_body(B))
    assert response.status_code == 403, response.text
    assert response.json()["detail"].startswith(f"Agent '{D}' does not have permission")
    assert scalar("SELECT COUNT(*) FROM agent_event_subscriptions") == 0

    fleet.db.add_agent_permission(D, B, created_by=OWNER)
    response = _call(fleet, _CREATE, D, fleet.agent, _subscription_body(B))
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["subscriber_agent"], body["source_agent"]) == (D, B)

    response = _call(
        fleet, _CREATE, D, fleet.agent, _subscription_body(C, "report.other")
    )
    assert response.status_code == 403, response.text
    assert scalar("SELECT COUNT(*) FROM agent_event_subscriptions") == 1


def test_the_subscription_answers_state_the_subscribers_reach():
    """The census answers for the three subscription writes are the manage
    reach, and the create answer names whose edge to a source counts: the
    subscriber's, never the caller's (driven by the test above)."""
    for key in (_UPDATE, _REMOVE):
        assert rc.AGENT_CALLABLE_ANY[key][2] == rc._SIB_SUBSCRIPTION, key
    assert "never through an edge" in rc._SIB_SUBSCRIPTION
    create = rc.AGENT_CALLABLE_ANY[_CREATE][2]
    assert "never through an edge" in create
    assert "a source that agent holds an edge to" in create
    assert "the caller's own edges do not count" in create


@pytest.mark.parametrize("person", ["session", "user_key"])
def test_a_person_subscribes_any_agent_they_own(fleet, person):
    response = _call(fleet, _CREATE, C, getattr(fleet, person), _subscription_body(C))
    assert response.status_code == 201, response.text


@pytest.fixture
def subscriptions(fleet):
    """One self-subscription per agent, created by the owner."""
    from db_models import EventSubscriptionCreate

    ids = {}
    for name in (A, B, C, D, P, F):
        sub = fleet.db.create_event_subscription(
            subscriber_agent=name,
            data=EventSubscriptionCreate(**_subscription_body(name)),
            created_by=OWNER,
        )
        ids[name] = sub.id
    return ids


def _sub_url(key, sub_id):
    method, path = rc.AGENT_CALLABLE_ANY[key][0].split(" ", 1)
    return method, path.replace("{subscription_id}", sub_id)


@pytest.mark.parametrize("target", [A, B, C, D, P, F])
def test_an_agent_key_edits_only_a_subscription_inside_its_manage_reach(
    fleet, subscriptions, target
):
    method, url = _sub_url(_UPDATE, subscriptions[target])
    response = fleet.client.request(
        method, url, json={"enabled": False}, headers=fleet.agent
    )
    stored = fleet.db.get_event_subscription(subscriptions[target])
    if target in MANAGE_REACH:
        assert response.status_code == 200, response.text
        assert stored.enabled is False
    else:
        assert (response.status_code, response.json()) == (
            403,
            {"detail": "Only the owner can modify event subscriptions"},
        )
        assert stored.enabled is True


@pytest.mark.parametrize("target", [A, B, C, D, P, F])
def test_an_agent_key_deletes_only_a_subscription_inside_its_manage_reach(
    fleet, subscriptions, target
):
    method, url = _sub_url(_REMOVE, subscriptions[target])
    response = fleet.client.request(method, url, headers=fleet.agent)
    stored = fleet.db.get_event_subscription(subscriptions[target])
    if target in MANAGE_REACH:
        assert response.status_code == 200, response.text
        assert stored is None
    else:
        assert (response.status_code, response.json()) == (
            403,
            {"detail": "Only the owner can delete event subscriptions"},
        )
        assert stored is not None


@pytest.mark.parametrize("person", ["session", "user_key"])
def test_a_person_edits_and_deletes_any_subscription_they_own(
    fleet, subscriptions, person
):
    headers = getattr(fleet, person)
    method, url = _sub_url(_UPDATE, subscriptions[C])
    assert (
        fleet.client.request(
            method, url, json={"enabled": False}, headers=headers
        ).status_code
        == 200
    )
    method, url = _sub_url(_REMOVE, subscriptions[C])
    assert fleet.client.request(method, url, headers=headers).status_code == 200
    assert fleet.db.get_event_subscription(subscriptions[C]) is None


# --------------------------------------------------------------------------- #
# The event loopback reaches none of them
# --------------------------------------------------------------------------- #


def _loopback_token(fleet) -> str:
    """A real, signed loopback JWT with no source agent, as the backend mints."""
    from jose import jwt

    deps = fleet.deps
    return jwt.encode(
        {
            "sub": OWNER,
            "scope": deps["EVENT_LOOPBACK_SCOPE"],
            "exp": int(time.time()) + 300,
        },
        deps["SECRET_KEY"],
        algorithm=deps["ALGORITHM"],
    )


@pytest.mark.parametrize("key", S1A + S1B)
def test_the_event_loopback_is_refused_by_the_entry_fence(
    fleet, channels, deletes, subscriptions, key
):
    """Listing a route as an agent use admits no new principal: a real loopback
    token is stopped by authentication itself on every one of them."""
    headers = fleet.as_(_loopback_token(fleet))
    path = rc.AGENT_CALLABLE_ANY[key][0].split(" ", 1)[1]
    if "{subscription_id}" in path:
        method, url = _sub_url(key, subscriptions[A])
        response = fleet.client.request(
            method, url, json={"enabled": False}, headers=headers
        )
    else:
        row = next((r for r in CHANNEL_USES if r[0] == key), None)
        body = row[1] if row else (_subscription_body(A) if key == _CREATE else None)
        params = row[2] if row else {}
        response = _call(fleet, key, A, headers, body, **params)
    assert (response.status_code, response.json()) == (
        403,
        {"detail": LOOPBACK_FENCE_DETAIL},
    )
    assert channels.items == [] and deletes == []
    assert _live(fleet, A)
    assert fleet.db.get_event_subscription(subscriptions[A]).enabled is True


# --------------------------------------------------------------------------- #
# Other shape: empty data — an agent with nothing bound or subscribed
# --------------------------------------------------------------------------- #


def test_an_agent_with_nothing_bound_gets_empty_answers_on_its_own_key(
    fleet, monkeypatch
):
    """A fresh agent: no Telegram group, no Slack workspace, no VoIP binding, no
    subscription, every table really empty. Its own key gets an empty answer or
    `configured: false` on each read, and a named not-bound answer on each send,
    never a 500 and never the gate's refusal."""
    voip = importlib.import_module("routers.voip")
    monkeypatch.setattr(
        voip.voip_service, "is_available", lambda: True
    )  # the feature on

    def get(key):
        return _call(fleet, key, A, fleet.agent)

    assert get(_READ_TG[0]).json() == []
    assert get(_LIST_SL[0]).json() == {"channels": [], "count": 0}
    assert get(_GET_SL[0]).json() == {"bound": False}
    assert get(_VOIP[0]).json() == {
        "agent_name": A,
        "configured": False,
        "account_sid": None,
        "from_number": None,
        "daily_call_cap": None,
        "display_name": None,
        "enabled": None,
    }

    response = _call(fleet, _SEND_TG[0], A, fleet.agent, _SEND_TG[1], **_SEND_TG[2])
    assert (response.status_code, response.json()) == (
        404,
        {"detail": "No Telegram binding found for this agent"},
    )
    response = _call(fleet, _SEND_SL[0], A, fleet.agent, _SEND_SL[1], **_SEND_SL[2])
    assert (response.status_code, response.json()) == (
        404,
        {"detail": "Channel not found or not bound to this agent"},
    )

    listed = fleet.client.get(
        f"/api/agents/{A}/event-subscriptions", headers=fleet.agent
    )
    assert listed.json() == {"count": 0, "subscriptions": []}
