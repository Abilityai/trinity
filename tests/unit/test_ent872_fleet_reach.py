"""trinity-enterprise#872 — fleet-level routes narrow an agent key to its reach.

An agent-scoped key resolves to its OWNER carrying the owner's role
(Invariant #8). ent#629 narrowed every per-agent gate to
`dependencies.agent_may_reach`; the routes that take no single agent in the path
(lists, counts, timelines, the event stream) still answered with the owner's
reach — on an admin-owned install, every agent's task text and requester emails.

One reach source (`dependencies.principal_reach`) feeds every roster accessor,
so an agent key's fleet view is {itself} ∪ its edges ∪ the agents it spawned
with its current key. Every other principal is unchanged.

Fixture: admin owner; agent key A; sibling B (no edge); C (A holds an edge to
it); D (spawned by A's current key); F (another user's agent).

Part A: the reach source. Part B: every listed route, through the real routers.
Part C: the writes and the person-only routes. Part D: static guard — a
`role == "admin"` shortcut or a raw roster read consults the reach, and the
`db/` methods that run an access predicate on a principal's username sit behind
a narrowed gate.
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
while str(_BACKEND) in sys.path:
    sys.path.remove(str(_BACKEND))
sys.path.insert(0, str(_BACKEND))
_TESTS = str(Path(__file__).resolve().parents[1])
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

from db_harness import db_backend, seed_agent, seed_execution, seed_user  # noqa: E402,F401

import dependencies  # noqa: E402
from models import User  # noqa: E402
# Bind the real modules at collection: several unit files install stand-ins for
# these with `sys.modules.setdefault` when they are collected.
import services.agent_service.helpers  # noqa: E402,F401

OWNER, OTHER = "owner", "other"
OWNER_EMAIL, OTHER_EMAIL = "owner@example.com", "other@example.com"
A, B, C, D = "a-alpha", "a-beta", "a-gamma", "a-delta"
F = "o-foreign"
ALL = (A, B, C, D, F)
REACH = {A, C, D}
HIDDEN = (B, F)


def _agent_key(agent: str = A, role: str = "admin") -> User:
    """Agent A's key: resolves to its owner, carrying the owner's role."""
    return User(id=1, username=OWNER, email=OWNER_EMAIL, role=role,
                mcp_scope="agent", agent_name=agent, mcp_key_id="key-a")


def _person(role: str = "admin") -> User:
    return User(id=1, username=OWNER, email=OWNER_EMAIL, role=role)


def _set_email(user_id: int, email: str):
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE users SET email = :e WHERE id = :i"), {"e": email, "i": user_id})


def _seed_fleet(owner_role: str = "admin"):
    from database import db
    from sqlalchemy import text
    from db.engine import get_engine

    seed_user(1, OWNER, role=owner_role)
    seed_user(2, OTHER, role="user")
    _set_email(1, OWNER_EMAIL)
    _set_email(2, OTHER_EMAIL)
    for name in (A, B, C, D):
        seed_agent(name, owner_id=1)
    seed_agent(F, owner_id=2)
    db.add_agent_permission(A, C, created_by=OWNER)
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE agent_ownership SET spawned_by_agent = :a, "
                          "spawned_by_key_id = 'key-a' WHERE agent_name = :d"), {"a": A, "d": D})
    return db


def _seed_rows(db):
    """One row per agent in every fleet table a listed route reads."""
    from db_models import NotificationCreate
    from models import ActivityCreate, ActivityType
    from utils.helpers import utc_now_iso
    from sqlalchemy import text
    from db.engine import get_engine

    now = utc_now_iso()
    for name in ALL:
        eid = seed_execution(f"s-{name}", agent_name=name, exec_id=f"e-{name}", started_at=now)
        with get_engine().begin() as conn:
            conn.execute(text("UPDATE schedule_executions SET message = :m WHERE id = :i"),
                         {"m": f"task text for {name}", "i": eid})
        db.create_notification(name, NotificationCreate(
            notification_type="info", title=f"note from {name}"))
        db.create_activity(ActivityCreate(agent_name=name, activity_type=ActivityType.CHAT_START,
                                          details={"preview": f"said by {name}"}))
        db.create_agent_event(name, "custom.ping", {"from": name})
        db.create_report(name, 1, "status", f"report of {name}", {"k": name})
        db.create_agent_evaluation(name, evaluator="platform", quality=0.5)
        db.add_agent_tag(name, f"tag-{name}")


@pytest.fixture
def fleet(db_backend, monkeypatch):
    db = _seed_fleet()

    class _Key:
        id = "key-a"

    monkeypatch.setattr(dependencies.db, "get_agent_mcp_api_key",
                        lambda agent: _Key() if agent == A else None)
    # Docker: every agent has a running container.
    from services.agent_service import helpers
    import services.docker_service as ds
    containers = lambda: [{"name": n, "status": "running"} for n in ALL]  # noqa: E731
    monkeypatch.setattr(helpers, "list_all_agents_fast", containers)
    # A file that re-imports services.agent_service at collection leaves routers
    # bound to an older copy of `get_accessible_agents`: patch every loaded copy.
    for module in list(sys.modules.values()):
        fn = getattr(module, "get_accessible_agents", None)
        scope = getattr(fn, "__globals__", None)
        if isinstance(scope, dict) and "list_all_agents_fast" in scope:
            monkeypatch.setitem(scope, "list_all_agents_fast", containers)
    monkeypatch.setattr(ds, "list_all_agents_fast",
                        lambda: [type("S", (), {"name": n, "status": "running"})() for n in ALL])
    return db


# ---------------------------------------------------------------------------
# Part A — one reach source
# ---------------------------------------------------------------------------

def test_the_fleet_reach_of_an_agent_key(fleet):
    assert dependencies.principal_reach(_agent_key()) == REACH


def test_every_other_principal_has_no_fleet_reach(fleet):
    for who in (_person(), _person("user"),
                User(id=1, username=OWNER, role="admin", mcp_scope="user"),
                User(id=1, username=OWNER, role="admin", mcp_scope="system")):
        assert dependencies.principal_reach(who) is None
        assert dependencies.is_fleet_admin(who) is (who.role == "admin")


def test_a_mock_principal_is_not_an_agent():
    """learnings: a bare MagicMock carries a truthy `agent_name`; the predicate is
    `isinstance(str)`, the same one `agent_may_reach` uses."""
    from unittest.mock import MagicMock
    m = MagicMock()
    m.role = "admin"
    assert dependencies.principal_reach(m) is None
    assert dependencies.is_fleet_admin(m) is True


def test_the_reach_is_read_once_per_request(fleet, monkeypatch):
    key = _agent_key()
    assert dependencies.principal_reach(key) == REACH
    monkeypatch.setattr(dependencies.db, "get_permitted_agents",
                        lambda a: (_ for _ in ()).throw(AssertionError("re-read")))
    assert dependencies.principal_reach(key) == REACH
    dependencies.principal_reach(key).add(B)                 # a caller's copy
    assert dependencies.principal_reach(key) == REACH


def test_a_failed_reach_read_keeps_only_self(fleet, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(dependencies.db, "get_permitted_agents", boom)
    monkeypatch.setattr(dependencies.db, "list_agents_spawned_by_key", boom)
    assert dependencies.principal_reach(_agent_key()) == {A}


def test_a_failed_owner_roster_read_keeps_only_self(fleet, monkeypatch):
    monkeypatch.setattr(dependencies.db, "get_accessible_agent_names",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert dependencies.principal_reach(_agent_key(role="user")) == {A}


def test_a_non_admin_owner_bounds_the_reach(db_backend, monkeypatch):
    """An edge to an agent the owner cannot access is not reach."""
    db = _seed_fleet(owner_role="user")
    db.add_agent_permission(A, F, created_by=OTHER)
    monkeypatch.setattr(dependencies.db, "get_agent_mcp_api_key", lambda agent: None)
    assert dependencies.principal_reach(_agent_key(role="user")) == {A, C}


def test_the_operator_queue_reads_the_same_reach(fleet):
    import routers.operator_queue as oq
    from services import operator_queue_service as svc
    assert oq._accessible_set(_agent_key()) == REACH
    assert svc.narrow_to_agent_key(_agent_key(), None) == REACH
    assert svc.narrow_to_agent_key(_agent_key(), {A, B, D}) == {A, D}
    assert oq._accessible_set(_person()) is None
    assert svc.narrow_to_agent_key(_person(), None) is None


def test_the_accessors_narrow(fleet):
    from services.agent_service import helpers
    key = _agent_key()
    assert {a["name"] for a in helpers.get_accessible_agents(key)} == REACH
    assert set(helpers.accessible_agent_names(key)) == REACH
    assert helpers.visible_agent_names(key) == REACH
    assert helpers.narrow_to_agent(helpers.accessible_agent_names(key), B) == []
    for fn in (helpers.accessible_agent_names, helpers.visible_agent_names):
        assert fn(_person()) is None
    assert {a["name"] for a in helpers.get_accessible_agents(_person())} == set(ALL)


def test_an_agent_keys_names_never_depend_on_docker(fleet, monkeypatch):
    """Pure DB: a stopped container's history still shows for its reach."""
    from services.agent_service import helpers
    monkeypatch.setattr(helpers, "list_all_agents_fast", lambda: [])
    assert set(helpers.accessible_agent_names(_agent_key())) == REACH


# ---------------------------------------------------------------------------
# Part B — every listed route, through the real routers
# ---------------------------------------------------------------------------

def _client(user_factory, *modules, attr="router"):
    import importlib
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    found = set()

    def walk(dependant):
        for sub in dependant.dependencies:
            if getattr(sub.call, "__name__", "") == "get_current_user":
                found.add(sub.call)
            walk(sub)

    for name in modules:
        router = getattr(importlib.import_module(name), attr)
        app.include_router(router)
        for route in router.routes:
            if getattr(route, "dependant", None) is not None:
                walk(route.dependant)
    for call in found:
        # Zero-arg wrapper: FastAPI would read the factory's own parameters
        # (`agent`, `role`) as query parameters. A FRESH principal per request.
        app.dependency_overrides[call] = lambda: user_factory()
    return TestClient(app, raise_server_exceptions=True)


_READS = [
    ("routers.executions", "/api/executions?hours=0"),
    ("routers.executions", f"/api/executions?hours=0&agent={B}"),
    ("routers.executions", "/api/executions/timeline?group_by=agent&hours=168"),
    ("routers.notifications", "/api/notifications"),
    ("routers.activities", "/api/activities/timeline"),
    ("routers.event_subscriptions", "/api/events"),
    ("routers.reports", "/api/reports?hours=0"),
    ("routers.reports", f"/api/reports?hours=0&agent={B}"),
    ("routers.evaluations", "/api/evaluations"),
    ("routers.evaluations", f"/api/evaluations?agent={B}"),
    ("routers.systems", "/api/systems/a"),
    ("routers.tags", "/api/tags"),
    ("routers.agents", "/api/agents/execution-stats"),
    ("routers.agents", "/api/agents/sync-health"),
    ("routers.agents", "/api/agents/permissions-edges"),
]


@pytest.mark.parametrize("module,url", _READS)
def test_an_agent_key_reads_none_of_a_siblings_rows(fleet, module, url):
    _seed_rows(fleet)
    fleet.add_agent_permission(B, F, created_by=OWNER)    # an edge outside A's reach
    r = _client(_agent_key, module).get(url)
    assert r.status_code == 200, r.text
    for hidden in HIDDEN:
        assert hidden not in r.text, f"{url} leaked {hidden}: {r.text[:400]}"


@pytest.mark.parametrize("module,url", [(m, u) for m, u in _READS if B not in u])
def test_an_admin_persons_view_is_unchanged(fleet, module, url):
    _seed_rows(fleet)
    fleet.add_agent_permission(B, C, created_by=OWNER)
    r = _client(_person, module).get(url)
    assert r.status_code == 200, r.text
    if module not in ("routers.agents",) or "edges" in url:
        assert B in r.text, f"{url}: admin lost {B}"


def test_an_empty_roster_reads_no_edges(fleet):
    """An empty accessible set filters to nothing; only None means every edge."""
    seed_user(3, "nobody", role="user")
    assert fleet.get_all_permission_edges(set()) == []
    assert {"source": A, "target": C} in fleet.get_all_permission_edges(None)
    nobody = lambda: User(id=3, username="nobody", email="nobody@example.com", role="user")  # noqa: E731
    r = _client(nobody, "routers.agents").get("/api/agents/permissions-edges")
    assert r.status_code == 200 and r.json() == {"edges": []}


def test_a_non_admin_persons_tags_are_their_agents(fleet):
    """Tag names and counts cover the person's owned and shared agents only."""
    _seed_rows(fleet)
    fleet.add_agent_tag(B, "shared-tag")
    fleet.add_agent_tag(F, "shared-tag")
    user = _client(lambda: _person("user"), "routers.tags").get("/api/tags").json()["tags"]
    got = {t["tag"]: t["count"] for t in user}
    assert set(got) == {f"tag-{n}" for n in (A, B, C, D)} | {"shared-tag"}
    assert got["shared-tag"] == 1                        # F's copy is another user's
    admin = _client(_person, "routers.tags").get("/api/tags").json()["tags"]
    assert {t["tag"]: t["count"] for t in admin}["shared-tag"] == 2
    assert f"tag-{F}" in {t["tag"] for t in admin}


def test_a_non_admin_owners_key_never_sees_an_edge_outside_the_owner(fleet):
    """Route level: an edge to another user's agent is not reach for a key whose
    owner cannot access that agent."""
    fleet.update_user_role(OWNER, "user")
    fleet.add_agent_permission(A, F, created_by=OTHER)
    _seed_rows(fleet)
    key = lambda: _agent_key(role="user")  # noqa: E731
    for module, url in (("routers.executions", "/api/executions?hours=0"),
                        ("routers.notifications", "/api/notifications"),
                        ("routers.event_subscriptions", "/api/events"),
                        ("routers.tags", "/api/tags")):
        r = _client(key, module).get(url)
        assert r.status_code == 200, r.text
        assert F not in r.text and B not in r.text, url
        assert C in r.text, url


_USER_KEY = lambda: User(id=1, username=OWNER, email=OWNER_EMAIL, role="admin",  # noqa: E731
                         mcp_scope="user")
_SYSTEM_KEY = lambda: User(id=1, username=OWNER, email=OWNER_EMAIL, role="admin",  # noqa: E731
                           mcp_scope="system")


@pytest.mark.parametrize("who", [_USER_KEY, _SYSTEM_KEY], ids=["user-key", "system-key"])
@pytest.mark.parametrize("module,url", [
    ("routers.executions", "/api/executions?hours=0"),
    ("routers.notifications", "/api/notifications"),
    ("routers.event_subscriptions", "/api/events"),
    ("routers.tags", "/api/tags"),
])
def test_user_and_system_keys_keep_the_owners_view(fleet, who, module, url):
    _seed_rows(fleet)
    r = _client(who, module).get(url)
    assert r.status_code == 200, r.text
    for name in ALL:
        assert name in r.text, f"{url}: {who().mcp_scope} key lost {name}"


def test_a_reach_of_only_self_is_never_unfiltered(fleet):
    """Reach = {E}, no rows for E: every list is empty, never the fleet."""
    E = "a-epsilon"
    seed_agent(E, owner_id=1)
    _seed_rows(fleet)
    key = lambda: _agent_key(agent=E)  # noqa: E731
    assert dependencies.principal_reach(key()) == {E}
    assert _client(key, "routers.notifications").get(
        "/api/notifications/count").json()["count"] == 0
    events = _client(key, "routers.event_subscriptions").get("/api/events")
    assert events.status_code == 200 and not any(n in events.text for n in ALL)
    assert _client(key, "routers.tags").get("/api/tags").json()["tags"] == []
    execs = _client(key, "routers.executions").get("/api/executions?hours=0")
    assert execs.status_code == 200 and not any(n in execs.text for n in ALL)


def test_a_siblings_counts_read_zero(fleet):
    _seed_rows(fleet)
    for url in (f"/api/executions/stats?hours=0&agent={B}", f"/api/reports/stats?hours=0&agent={B}"):
        mod = "routers.executions" if "executions" in url else "routers.reports"
        key = _client(_agent_key, mod).get(url).json()
        admin = _client(_person, mod).get(url).json()
        assert admin != key, url
    count = _client(_agent_key, "routers.notifications").get("/api/notifications/count").json()
    assert count["count"] == len(REACH)
    assert _client(_person, "routers.notifications").get(
        "/api/notifications/count").json()["count"] == len(ALL)


@pytest.mark.parametrize("url", [
    f"/api/notifications?agent_name={B}",
    f"/api/events?source_agent={B}",
])
def test_naming_a_sibling_is_refused(fleet, url):
    _seed_rows(fleet)
    mod = "routers.notifications" if "notifications" in url else "routers.event_subscriptions"
    assert _client(_agent_key, mod).get(url).status_code in (403, 404)
    assert _client(_person, mod).get(url).status_code == 200


def _fleet_status(who, monkeypatch):
    import routers.monitoring as rm
    import services.heartbeat_service as hb
    import services.agent_client as ac
    monkeypatch.setattr(rm, "get_monitoring_service",
                        lambda: type("M", (), {"is_running": False})())
    monkeypatch.setattr(hb, "heartbeat_status_bulk", lambda names: {})   # no Redis
    monkeypatch.setattr(ac, "get_all_circuit_states", lambda: {})
    r = _client(who, "routers.monitoring").get("/api/monitoring/status")
    assert r.status_code == 200, r.text
    return {a["name"] for a in r.json()["agents"]}


def test_the_fleet_health_sweep_keeps_the_owners_set(fleet, monkeypatch):
    """trinity-enterprise#706 D12, deliberate: an agent key's fleet health sweep
    sees its owner's accessible agents (health state only)."""
    assert _fleet_status(_agent_key, monkeypatch) == set(ALL)
    assert _fleet_status(_person, monkeypatch) == set(ALL)
    fleet.update_user_role(OWNER, "user")
    assert _fleet_status(lambda: _agent_key(role="user"), monkeypatch) == {A, B, C, D}


def test_the_fleet_sync_audit_keeps_the_owners_set(fleet, monkeypatch):
    """trinity-enterprise#706 D12, deliberate: same for the git sync audit."""
    seen = []

    async def build(agent_names=None):
        seen.append(agent_names)
        return {"agents": [], "summary": {}}

    import routers.fleet as rf
    monkeypatch.setattr(rf, "build_fleet_sync_audit", build)
    for who in (_agent_key, _person):
        assert _client(who, "routers.fleet").get("/api/fleet/sync-audit").status_code == 200
    assert seen == [None, None]                          # admin-owned key: every agent
    _client(lambda: _agent_key(role="user"), "routers.fleet").get("/api/fleet/sync-audit")
    assert set(seen[-1]) == {A, B, C, D}                 # non-admin owner's set, not the reach


def test_the_onboarding_and_permission_views_are_narrowed(fleet, monkeypatch):
    from services import onboarding_service
    from services.agent_service import permissions
    assert {a["name"] for a in onboarding_service._visible_agents(_agent_key())} == REACH
    assert {a["name"] for a in onboarding_service._visible_agents(_person())} == set(ALL)
    monkeypatch.setattr(permissions, "agent_container_states",
                        lambda: {n: "running" for n in ALL})
    view = permissions._strict_permissions(A, _agent_key())
    assert {a["name"] for a in view["available_agents"]} == REACH - {A}
    view = permissions._strict_permissions(A, _person())
    assert {a["name"] for a in view["available_agents"]} == set(ALL) - {A}


def test_the_context_stats_cache_is_written_by_a_full_fleet_caller_only(fleet, monkeypatch):
    from services.agent_service import helpers, stats
    monkeypatch.setattr(helpers, "list_all_agents_fast",
                        lambda: [{"name": n, "status": "stopped"} for n in ALL])
    monkeypatch.setattr(stats, "get_accessible_agents", helpers.get_accessible_agents)
    monkeypatch.setitem(stats._context_stats_cache, "data", None)
    got = asyncio.run(stats.get_agents_context_stats_logic(_agent_key()))
    assert {s["name"] for s in got["agents"]} == REACH
    assert stats._context_stats_cache["data"] is None
    got = asyncio.run(stats.get_agents_context_stats_logic(_person()))
    assert {s["name"] for s in got["agents"]} == set(ALL)
    assert {s["name"] for s in stats._context_stats_cache["data"]} == set(ALL)
    # A narrowed caller served from the cache still gets only its reach.
    got = asyncio.run(stats.get_agents_context_stats_logic(_agent_key()))
    assert {s["name"] for s in got["agents"]} == REACH


def test_the_slots_and_container_stats_are_scoped(fleet, monkeypatch):
    import routers.telemetry as rt
    import services.capacity_manager as cm

    monkeypatch.setattr(dependencies.db, "get_all_agents_parallel_capacity",
                        lambda: {n: 2 for n in ALL})

    class _Cap:
        async def get_all_states(self, caps):
            return {n: {"max": c, "active": 0} for n, c in caps.items()}

    monkeypatch.setattr(cm, "get_capacity_manager", lambda: _Cap())
    import config
    monkeypatch.setattr(config, "DISPATCH_BREAKER_ENABLED", False)
    for who, want in ((_agent_key, REACH), (_person, set(ALL))):
        r = _client(who, "routers.agents").get("/api/agents/slots")
        assert r.status_code == 200, r.text
        assert set(r.json()["agents"]) == want

    # D is running but its stats fetch failed: no container row, still counted.
    public = {"running_count": 5, "total_cpu_percent": 4.0, "total_memory_mb": 40.0,
              "containers": [{"name": n, "cpu": 1.0, "memory_mb": 10.0} for n in ALL if n != D],
              "timestamp": "2026-10-19T00:00:00Z"}
    payload = {**public, "_running_names": list(ALL)}
    monkeypatch.setattr(rt, "docker_client", object())
    monkeypatch.setitem(rt._stats_cache, "data", payload)
    import time
    monkeypatch.setitem(rt._stats_cache, "timestamp", time.monotonic())
    admin = _client(_person, "routers.telemetry").get("/api/telemetry/containers").json()
    assert admin == {**public, "cached": True, "stale": False,
                     "cache_age_seconds": admin["cache_age_seconds"], "no_data": False}
    key = _client(_agent_key, "routers.telemetry").get("/api/telemetry/containers").json()
    assert {c["name"] for c in key["containers"]} == REACH - {D}
    assert key["running_count"] == len(REACH)          # running agents, like the unscoped count
    assert key["total_memory_mb"] == 20.0
    assert "_running_names" not in key
    user = _client(lambda: _person("user"), "routers.telemetry").get(
        "/api/telemetry/containers").json()
    assert {c["name"] for c in user["containers"]} == {A, B, C}
    assert user["running_count"] == 4


def test_the_event_stream_roster_is_the_reach(fleet):
    from services.ws_identity_service import event_stream_identity
    owner = fleet.get_user_by_username(OWNER)
    agent_key = {"scope": "agent", "agent_name": A, "user_email": OWNER_EMAIL, "user_id": OWNER}
    assert event_stream_identity(agent_key, owner) == (False, sorted(REACH))
    user_key = {"scope": "user", "agent_name": None, "user_email": OWNER_EMAIL, "user_id": OWNER}
    is_admin, _ = event_stream_identity(user_key, owner)
    assert is_admin is True


def test_the_event_stream_delivers_none_of_a_siblings_events(fleet):
    """A non-admin scoped slot with the reach roster: B's event never arrives."""
    from services.event_bus import SCOPE_SCOPED, _ClientSlot, _event_is_visible
    from services.ws_identity_service import event_stream_identity
    is_admin, roster = event_stream_identity(
        {"scope": "agent", "agent_name": A, "user_email": OWNER_EMAIL},
        fleet.get_user_by_username(OWNER))
    slot = _ClientSlot(ws=None, scope=SCOPE_SCOPED, send_func=None,
                       is_admin=is_admin, accessible_agents=set(roster))
    assert _event_is_visible(slot, SCOPE_SCOPED, C)
    assert not _event_is_visible(slot, SCOPE_SCOPED, B)


def test_the_event_stream_endpoint_scopes_an_agent_key(fleet, monkeypatch):
    """Drives main.py's /ws/events with an admin-owned agent key: the connected
    roster, the slot it registers, and `refresh` are all the reach."""
    import main
    from fastapi.testclient import TestClient
    from database import db
    from services.event_bus import SCOPE_SCOPED, _ClientSlot, _event_is_visible

    monkeypatch.setattr(db, "validate_mcp_api_key", lambda token: {
        "scope": "agent", "agent_name": A, "user_email": OWNER_EMAIL,
        "user_id": OWNER, "id": "key-a"})
    seen = {}

    async def connect(ws, email, is_admin, roster, last_event_id=None, roster_resolver=None):
        seen.update(is_admin=is_admin, roster=roster, resolver=roster_resolver)

    monkeypatch.setattr(main.filtered_manager, "connect", connect)
    monkeypatch.setattr(main.filtered_manager, "disconnect", lambda ws: None)
    monkeypatch.setattr(main.filtered_manager, "update_accessible_agents",
                        lambda ws, roster: seen.update(refreshed=roster))
    with TestClient(main.app).websocket_connect("/ws/events?token=trinity_mcp_x") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "connected" and set(hello["accessible_agents"]) == REACH
        ws.send_text("refresh")
        again = ws.receive_json()
        assert again["type"] == "refreshed" and set(again["accessible_agents"]) == REACH
    assert seen["is_admin"] is False and set(seen["roster"]) == REACH
    assert set(seen["refreshed"]) == REACH
    assert set(seen["resolver"]()) == REACH           # the slot's re-read is the reach too

    slot = _ClientSlot(ws=None, scope=SCOPE_SCOPED, send_func=None,
                       is_admin=seen["is_admin"], accessible_agents=set(seen["roster"]))
    assert _event_is_visible(slot, SCOPE_SCOPED, C)
    assert not _event_is_visible(slot, SCOPE_SCOPED, B)


def test_an_agent_key_slot_refreshes_its_reach_on_an_access_change():
    """A spawned child appears without a reconnect; a failed re-read keeps the
    older, smaller roster; a non-access event reads nothing."""
    from services.event_bus import SCOPE_SCOPED, StreamDispatcher, _ClientSlot

    calls = []

    def read():
        calls.append(1)
        return [A, C, "a-child"]

    async def go():
        d = StreamDispatcher()
        slot = _ClientSlot(ws=None, scope=SCOPE_SCOPED, send_func=None,
                           accessible_agents={A, C}, roster_resolver=read)
        d._clients["x"] = slot
        d._maybe_invalidate_rosters({"type": "agent_activity", "agent_name": A})
        assert not d._refresh_tasks and not calls
        d._maybe_invalidate_rosters({"type": "agent_created", "name": "a-child"})
        await asyncio.gather(*list(d._refresh_tasks))
        assert slot.accessible_agents == {A, C, "a-child"} and not slot.refreshing

        def boom():
            raise RuntimeError("db down")
        slot.roster_resolver = boom
        d._maybe_invalidate_rosters({"type": "agent_deleted", "name": C})
        await asyncio.gather(*list(d._refresh_tasks))
        assert slot.accessible_agents == {A, C, "a-child"} and not slot.refreshing

    asyncio.run(go())


def test_the_chat_resume_check_is_not_skipped_by_the_owners_admin_role():
    """routers/chat.py: an admin PERSON may resume any session; an agent key
    carrying its owner's admin role is checked for ownership like everyone else."""
    from unittest.mock import MagicMock, patch
    import routers.chat as chat_mod
    import services.dispatch_admission_service as das
    from fastapi import HTTPException
    from models import ParallelTaskRequest

    class _Passed(Exception):
        pass

    def call(agent_name):
        user = MagicMock()
        user.id, user.role, user.agent_name, user.vouched_source_agent = 1, "admin", agent_name, None
        running = MagicMock()
        running.status = "running"
        with patch.object(chat_mod, "get_agent_container", return_value=running), \
             patch.object(chat_mod.db, "resume_session_belongs_to_user", return_value=False), \
             patch.object(chat_mod.db, "get_execution_timeout", return_value=3600), \
             patch.object(das.idempotency_service, "begin", side_effect=_Passed):
            return asyncio.run(chat_mod.execute_parallel_task(
                request=ParallelTaskRequest(message="hi", resume_session_id="sess-1", async_mode=True),
                name=A, current_user=user, x_source_agent=agent_name,
                x_via_mcp=None, idempotency_key=None))

    with pytest.raises(_Passed):
        call(None)                      # an admin person
    with pytest.raises(HTTPException) as e:
        call(A)                         # an agent key with its owner's admin role
    assert e.value.status_code == 404


# ---------------------------------------------------------------------------
# Part C — writes, person-only routes, credentials
# ---------------------------------------------------------------------------

def test_dismiss_all_never_clears_a_siblings_notifications(fleet):
    _seed_rows(fleet)
    client = _client(_agent_key, "routers.notifications")
    assert client.post("/api/notifications/dismiss-all", json={"agent_name": B}).status_code == 403
    r = client.post("/api/notifications/dismiss-all", json={})
    assert r.status_code == 200 and r.json()["dismissed"] == len(REACH)
    left = {n.agent_name for n in fleet.list_notifications(status="pending", limit=50)}
    assert left == set(HIDDEN)


def test_a_subscription_names_only_the_agents_in_reach(fleet, monkeypatch):
    from datetime import datetime
    from db_models import SubscriptionWithAgents
    import routers.subscriptions as rs
    now = datetime(2026, 1, 1)
    row = SubscriptionWithAgents(id="s1", name="max", owner_id=1, created_at=now,
                                 updated_at=now, agent_count=4, agents=[A, B, C, D])
    seen = []
    monkeypatch.setattr(rs.db, "list_subscriptions_with_agents",
                        lambda owner_id=None: seen.append(owner_id) or [row])
    key = _client(_agent_key, "routers.subscriptions").get("/api/subscriptions").json()
    assert key[0]["agents"] == [A, C, D] and key[0]["agent_count"] == 3
    person = _client(_person, "routers.subscriptions").get("/api/subscriptions").json()
    assert person[0]["agents"] == [A, B, C, D]
    assert seen == [1, None]                 # the key is owner-scoped; the admin person sees all


def test_the_proactive_consent_write_is_person_only(fleet):
    fleet.share_agent(B, OWNER, "friend@example.com")
    r = _client(_agent_key, "routers.messages").put(
        f"/api/agents/{B}/shares/proactive",
        json={"email": "friend@example.com", "allow_proactive": True})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "person_required"
    assert fleet.get_proactive_enabled_shares(B) == []
    r = _client(_person, "routers.messages").put(
        f"/api/agents/{B}/shares/proactive",
        json={"email": "friend@example.com", "allow_proactive": True})
    assert r.status_code == 200, r.text


def test_the_ws_ticket_is_person_only(fleet, monkeypatch):
    import routers.ws_tickets as wt
    monkeypatch.setattr(wt, "mint_ticket", lambda subject, scope: "t-1")
    assert _client(_agent_key, "routers.ws_tickets").post("/api/ws/ticket").status_code == 403
    user_key = lambda: User(id=1, username=OWNER, role="admin", mcp_scope="user")  # noqa: E731
    for who in (_person, user_key):
        r = _client(who, "routers.ws_tickets").post("/api/ws/ticket")
        assert r.status_code == 200 and r.json()["ticket"] == "t-1"


def test_an_agent_key_reads_only_its_own_mcp_key(fleet, monkeypatch):
    import routers.mcp_keys as mk
    from datetime import datetime

    def _key(key_id):
        return {"id": key_id, "name": key_id, "description": None, "key_prefix": "trinity_mcp_x",
                "created_at": datetime(2026, 1, 1), "last_used_at": None, "usage_count": 0,
                "is_active": True, "user_id": 1, "username": OWNER, "user_email": OWNER_EMAIL,
                "agent_name": None, "scope": "agent"}

    monkeypatch.setattr(mk.db, "get_mcp_api_key", lambda key_id, username: _key(key_id))
    client = _client(_agent_key, "routers.mcp_keys")
    assert client.get("/api/mcp/keys/key-a").status_code == 200
    assert client.get("/api/mcp/keys/key-b").status_code == 404
    assert _client(_person, "routers.mcp_keys").get("/api/mcp/keys/key-b").status_code == 200


def test_system_views_are_person_only(fleet):
    from db_models import SystemViewCreate
    view = fleet.create_system_view("1", SystemViewCreate(name="mine", filter_tags=["x"]))
    client = _client(_agent_key, "routers.system_views")
    for method, url, body in (("get", "/api/system-views", None),
                              ("post", "/api/system-views", {"name": "n", "filter_tags": ["x"]}),
                              ("get", f"/api/system-views/{view.id}", None),
                              ("put", f"/api/system-views/{view.id}", {"name": "m"}),
                              ("delete", f"/api/system-views/{view.id}", None)):
        r = client.request(method, url, json=body)
        assert r.status_code == 403, (method, url, r.text)
        assert r.json()["detail"]["code"] == "person_required"
    assert _client(_person, "routers.system_views").get(
        f"/api/system-views/{view.id}").status_code == 200


def test_a_system_views_count_is_the_callers_agents(fleet):
    from db_models import SystemViewCreate
    for name in (A, B, F):
        fleet.add_agent_tag(name, "team")
    view = fleet.create_system_view("1", SystemViewCreate(
        name="team", filter_tags=["team"], is_shared=True))
    user = _client(lambda: _person("user"), "routers.system_views")
    assert user.get(f"/api/system-views/{view.id}").json()["agent_count"] == 2
    assert [v["agent_count"] for v in user.get("/api/system-views").json()["views"]] == [2]
    admin = _client(_person, "routers.system_views")
    assert admin.get(f"/api/system-views/{view.id}").json()["agent_count"] == 3


# ---------------------------------------------------------------------------
# Part D — static guard
# ---------------------------------------------------------------------------
# A `role == "admin"` comparison (any spelling: `.role`, `["role"]`,
# `.get("role")`, `getattr(x, "role")`, a bare `role`), or a raw roster read
# (`db.get_accessible_agent_names` / `db.get_all_agent_metadata`), selects an
# unfiltered fleet for an admin. An agent key carries its owner's role, so the
# innermost function making it must also consult the reach, a person/admin gate
# that refuses agent keys, or be listed below with the reason its principal is
# never an agent key. The three helper accessors narrow inside, so calling one
# excuses a raw roster read (`test_the_helper_accessors_consult_the_reach` keeps
# that true). It never excuses a role compare: the admin branch beside it can
# still return the raw fleet.

#: Passes a role-compare site: the reach itself, or a gate that refuses an agent key.
_PASS = {
    "principal_reach", "is_fleet_admin", "agent_may_reach", "agent_principal_name",
    "_sees_every_user",
    "require_person", "assert_person", "require_interactive", "is_person_principal",
    "reject_agent_principal", "human_only", "require_admin", "assert_admin",
}
#: Also passes a roster-read site: an accessor that narrows inside. A role
#: compare beside an accessor call can still hand the admin branch the raw fleet.
_ACCESSOR_PASS = {
    "get_accessible_agents", "accessible_agent_names", "visible_agent_names",
    "_visible_agent_names",
}
_ROSTER_READS = {"get_accessible_agent_names", "get_all_agent_metadata",
                 "list_subscriptions_with_agents", "list_all_agents_fast"}
_ROSTER_READ_PREFIXES = ("get_all_agents_",)   # db readers returning a row per agent


def _is_roster_read(node) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = getattr(node.func, "attr", None) or getattr(node.func, "id", None) or ""
    return name in _ROSTER_READS or name.startswith(_ROSTER_READ_PREFIXES)
_HELPER_ACCESSORS = {"get_accessible_agents", "accessible_agent_names", "visible_agent_names"}

#: (file, function) → why its principal is never an agent key, or why the admin
#: branch selects no fleet. Shrink-only: `test_every_allowlist_entry_still_applies`.
_ALLOW = {
    ("routers/voice.py", "voice_websocket"):
        "the voice socket is gated to its own session's user id; the compare is that owner check",
    ("routers/system_agent.py", "system_agent_terminal"):
        "the system-agent terminal authenticates a person (JWT) and checks the user row's role",
    ("services/settings_service.py", "get_agent_quota_for_role"):
        "maps a role to an agent-creation quota; selects no roster",
    ("services/ask_service.py", "may_end"):
        "an ask's ending is person-only at every route (reject_non_person_principal)",
    ("services/person_mention_service.py", "_room_access"):
        "decides about the PERSON row being mentioned (looked up by email), never a principal's fleet",
    ("services/ws_identity_service.py", "resolve_ws_identity"):
        "a /ws ticket subject is a person (the ticket route is require_person)",
    ("routers/fleet.py", "fleet_sync_audit"):
        "trinity-enterprise#706 D12: agent fleet sweeps (corbin) see the owner's accessible agents; returns git/health state, no task text or emails",
    ("routers/monitoring.py", "get_fleet_status"):
        "trinity-enterprise#706 D12: agent fleet sweeps (corbin) see the owner's accessible agents; returns git/health state, no task text or emails",
    ("main.py", "_init_docker_and_system_agent"):
        "startup: no principal",
    ("services/agent_service/helpers.py", "get_agents_by_prefix"):
        "global name-namespace lookup for deploy version numbering; returns no rows to the caller",
    ("services/fleet_ops_service.py", "emergency_stop_impl"):
        "routers/ops.py gates it with assert_admin, which refuses agent keys",
    ("services/fleet_ops_service.py", "get_fleet_health_impl"):
        "routers/ops.py gates it with assert_admin (ops scope opted in), which refuses agent keys",
    ("services/fleet_ops_service.py", "stop_fleet_impl"):
        "routers/ops.py gates it with assert_admin, which refuses agent keys",
    ("services/git_service/token_scrub.py", "sweep_fleet_git_remote_tokens"):
        "background sweep: no principal",
    ("services/github_pat_propagation_service.py", "propagate_github_pat"):
        "called from PUT /api/settings/api-keys/github after assert_admin, which refuses agent keys",
    ("services/heartbeat_service.py", "_list_agent_names"):
        "background heartbeat loop: no principal",
    ("services/monitoring_service.py", "_run_check_cycle"):
        "background health-check loop: no principal",
    ("services/session_cleanup_service.py", "_run_cycle_inner"):
        "background cleanup loop: no principal",
    ("services/skills_sync_service.py", "_eligible_agents"):
        "background sweep, or the skills-library route behind require_admin + reject_agent_principal",
    ("services/turn_tier_service.py", "_user_tier"):
        "the role compared is the turn starter's user row (a person), never the caller's; selects no roster",
    ("services/ws_identity_service.py", "event_stream_identity"):
        "an agent key returns agent_reach_names as a non-admin slot before the role compare",
    ("services/ws_identity_service.py", "accessible_agents_for"):
        "a person's email from a person-only ticket",
    ("services/agent_service/terminal.py", "handle_terminal_session"):
        "the terminal websocket authenticates a person, never an agent key",
    ("client_portal/portal_auth.py", "_is_admin_principal"):
        "agent keys are refused before this line (reject_agent_principal); portal principals are people",
}


def _scan_files():
    files = [_BACKEND / "main.py"]
    for sub in ("routers", "services", "client_portal"):
        files += sorted((_BACKEND / sub).rglob("*.py"))
    return [f for f in files if not f.relative_to(_BACKEND).as_posix().startswith("enterprise/")]


def _role_expr(node) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "role":
        return True
    if isinstance(node, ast.Name) and node.id in ("role", "user_role"):
        return True
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
            and node.slice.value == "role":
        return True
    if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "get" and node.args \
            and isinstance(node.args[0], ast.Constant) and node.args[0].value == "role":
        return True
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "getattr" \
            and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
            and node.args[1].value == "role":
        return True
    return False


def _is_admin_compare(node) -> bool:
    if not isinstance(node, ast.Compare):
        return False
    sides = [node.left, *node.comparators]
    for op, right in zip(node.ops, node.comparators):
        left = sides[sides.index(right) - 1]
        pair = (left, right)
        if isinstance(op, (ast.Eq, ast.NotEq)):
            if any(isinstance(x, ast.Constant) and x.value == "admin" for x in pair) \
                    and any(_role_expr(x) for x in pair):
                return True
        if isinstance(op, (ast.In, ast.NotIn)) and _role_expr(left) \
                and isinstance(right, (ast.Tuple, ast.List, ast.Set)) \
                and any(isinstance(e, ast.Constant) and e.value == "admin" for e in right.elts):
            return True
    return False


def _names(fn) -> set:
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def _functions(tree):
    """Each function with the nodes it owns (nested functions judged on their own)."""
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        inner = {id(n) for sub in ast.walk(fn) if sub is not fn
                 and isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                 for n in ast.walk(sub)}
        yield fn, [n for n in ast.walk(fn) if id(n) not in inner]


def _allowed(rel: str, fn_name: str, allow: dict) -> bool:
    for (path, name) in allow:
        if (rel == path or (path.endswith("/") and rel.startswith(path))) and name in ("*", fn_name):
            return True
    return False


def _fleet_sites():
    """Every (rel, function, kind) that selects a fleet by role or reads a raw roster."""
    sites = []
    for path in _scan_files():
        rel = path.relative_to(_BACKEND).as_posix()
        for fn, own in _functions(ast.parse(path.read_text())):
            for n in own:
                if _is_admin_compare(n):
                    sites.append((rel, fn, "role-compare"))
                elif _is_roster_read(n):
                    sites.append((rel, fn, "roster-read"))
    return sites


def _fleet_offenders(allow=_ALLOW):
    out = []
    for rel, fn, kind in _fleet_sites():
        ok = _PASS | _ACCESSOR_PASS if kind == "roster-read" else _PASS
        if _names(fn) & ok or _allowed(rel, fn.name, allow):
            continue
        out.append(f"{rel}::{fn.name} ({kind})")
    return sorted(set(out))


def test_the_guard_sees_the_fleet_sites(capsys):
    """Not vacuous: print what discovery found, and require the known shapes."""
    sites = _fleet_sites()
    with capsys.disabled():
        print(f"\n[ent#872] fleet sites discovered: {len(sites)} "
              f"({sum(k == 'role-compare' for *_, k in sites)} role compares, "
              f"{sum(k == 'roster-read' for *_, k in sites)} roster reads)")
    keys = {(rel, fn.name) for rel, fn, _ in sites}
    assert len(sites) >= 30
    assert ("routers/event_subscriptions.py", "list_all_events") in keys    # .role ==
    assert ("services/onboarding_service.py", "_visible_agents") in keys   # .get("role") ==
    assert ("services/agent_service/helpers.py", "get_accessible_agents") in keys  # ["role"] ==
    assert ("main.py", "websocket_events_endpoint") in keys or \
        ("services/ws_identity_service.py", "event_stream_identity") in keys


def test_every_fleet_selection_consults_the_reach():
    offenders = _fleet_offenders()
    assert not offenders, (
        "These pick an unfiltered fleet for the admin role (or read a raw roster) "
        "without consulting the agent key's reach. An agent key carries its owner's "
        "role (trinity-enterprise#872): use dependencies.is_fleet_admin / "
        "principal_reach, a narrowing accessor, or a person gate — or add the "
        f"function to _ALLOW with the reason: {offenders}")


def test_the_guard_bites():
    """Mutation: a function shaped like the pre-fix event list is named."""
    src = ("def list_all(current_user):\n"
           "    if current_user.role == 'admin':\n"
           "        return db.list_everything()\n"
           "    return db.get_accessible_agent_names(current_user.email)\n")
    tree = ast.parse(src)
    fn, own = next(_functions(tree))
    assert any(_is_admin_compare(n) for n in own)
    assert not (_names(fn) & _PASS)


def test_an_accessor_call_does_not_excuse_a_role_compare():
    """The pre-fix fleet status: `role != "admin"` picks the raw fleet for an
    agent key even though the other branch calls the narrowing accessor."""
    src = ("def get_fleet_status(current_user):\n"
           "    names = list_all()\n"
           "    if current_user.role != 'admin':\n"
           "        names = [a['name'] for a in get_accessible_agents(current_user)]\n"
           "    return names\n")
    fn, own = next(_functions(ast.parse(src)))
    assert any(_is_admin_compare(n) for n in own)
    assert _names(fn) & _ACCESSOR_PASS and not (_names(fn) & _PASS)


def test_every_allowlist_entry_still_applies():
    """A stale entry would quietly cover the next function of that name."""
    sites = _fleet_sites()
    for (path, name), _ in _ALLOW.items():
        hits = [(rel, fn) for rel, fn, _ in sites
                if (rel == path or (path.endswith("/") and rel.startswith(path)))
                and name in ("*", fn.name)]
        assert hits, f"{path}::{name} has no fleet site left — drop it from _ALLOW"
        assert any(not (_names(fn) & _PASS) for _, fn in hits), (
            f"{path}::{name} now consults the reach — drop it from _ALLOW")


def test_the_helper_accessors_consult_the_reach():
    tree = ast.parse((_BACKEND / "services/agent_service/helpers.py").read_text())
    fns = {fn.name: fn for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)}
    for name in _HELPER_ACCESSORS:
        assert "principal_reach" in _names(fns[name]), (
            f"helpers.{name} must narrow an agent key through principal_reach")


# --- the db/ extension -----------------------------------------------------
# #3346's guard sees inline `can_user_access_agent` / `can_user_share_agent`
# calls in routers/services. A `db` method that runs the predicate itself on a
# username it is handed is invisible to it, so the method set is DERIVED from
# db/ and each call site must sit behind a narrowed gate.

_PREDICATES = {"can_user_access_agent", "can_user_share_agent"}
_NARROWED_GATES = {
    "agent_may_reach", "_may_reach_for_path", "assert_agent_access", "assert_agent_owner",
    "OwnedAgent", "OwnedAgentByName", "AuthorizedAgent", "AuthorizedAgentByName",
    "get_owned_agent", "get_owned_agent_by_name", "get_authorized_agent",
    "get_authorized_agent_by_name", "require_person", "assert_person", "require_interactive",
}
_DB_EXEMPT = {
    ("services/system_service.py", "create_schedules"):
        "takes the deploying username; the deploy route is capability-fenced (agents.manage)",
    ("services/agent_service/crud.py", "reconcile_declared_schedules"):
        "takes the owner's username to seed template schedules at create/recreate",
    ("services/system_service.py", "_run_op"):
        "apply executor: creates a schedule as the member's OWNER only after read_apply_snapshot "
        "classified the member manageable (can_user_share_agent + agent_may_reach(manage=True)); "
        "the apply route is require_person (trinity-enterprise#695)",
    ("services/system_service.py", "_apply_created_schedules"):
        "apply executor: schedules of a member this same run created, as its creator; the apply "
        "route is require_person, so no agent key reaches it (trinity-enterprise#695)",
}


def _db_predicate_methods() -> set:
    methods = set()
    for path in (_BACKEND / "db").rglob("*.py"):
        for fn in ast.walk(ast.parse(path.read_text())):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name not in _PREDICATES \
                    and any(isinstance(n, ast.Call) and getattr(n.func, "attr", None) in _PREDICATES
                            for n in ast.walk(fn)):
                methods.add(fn.name)
    return methods


def _db_call_sites(methods):
    files = [_BACKEND / "dependencies.py", _BACKEND / "main.py"]
    for sub in ("routers", "services", "client_portal", "adapters"):
        files += sorted((_BACKEND / sub).rglob("*.py"))
    sites = []
    for path in files:
        rel = path.relative_to(_BACKEND).as_posix()
        if rel.startswith("enterprise/"):
            continue
        for fn, own in _functions(ast.parse(path.read_text())):
            for n in own:
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                        and n.func.attr in methods and getattr(n.func.value, "id", None) == "db":
                    sites.append((rel, fn))
    return sites


def test_the_db_guard_derives_its_methods(capsys):
    methods = _db_predicate_methods()
    sites = _db_call_sites(methods)
    with capsys.disabled():
        print(f"\n[ent#872] db methods running an access predicate: {sorted(methods)}; "
              f"call sites: {len(sites)}")
    assert "set_allow_proactive" in methods
    assert len(sites) >= 4


def test_every_db_predicate_call_sits_behind_a_narrowed_gate():
    offenders = sorted({f"{rel}::{fn.name}" for rel, fn in _db_call_sites(_db_predicate_methods())
                        if not (_names(fn) & _NARROWED_GATES) and (rel, fn.name) not in _DB_EXEMPT})
    assert not offenders, (
        "These call a db method that runs can_user_access_agent / can_user_share_agent "
        "on the caller's username, so an agent key would pass with its owner's role "
        "(trinity-enterprise#872). Put the route behind a narrowed gate "
        f"(an Owned/Authorized agent dependency, agent_may_reach, require_person): {offenders}")


def test_every_db_exemption_still_applies():
    sites = {(rel, fn.name) for rel, fn in _db_call_sites(_db_predicate_methods())}
    for key in _DB_EXEMPT:
        assert key in sites, f"{key} no longer calls such a db method — drop it"
