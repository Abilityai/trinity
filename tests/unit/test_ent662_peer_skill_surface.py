"""abilityai/trinity-enterprise#662 — a peer may run what an agent declares;
what an admin assigned needs a grant.

An `agent_permissions` edge from B to A used to authorize everything A could
run, library assignments included — so assigning a library skill to A (or a
commit to a skill set A holds, propagated as `assigned_by="system"`) silently
widened what every caller of A could trigger. The default is now: a peer may
invoke A's DECLARED skills; a library-assigned one needs a per-skill grant.

What these tests pin, and why each exists:

1. the decision at the real seam (`skill_gate_service.enforce`, which every
   admission seam calls) over the real per-process SQLite: a peer invoking a
   library skill — individual OR set-propagated — is refused by name; a
   declared skill, a #2914 conflict name (the agent's own copy runs), a grant,
   the agent itself, a person, a connector and the system agent all pass;
2. the `execute_task` backstop applies the rule even on an agent with NO
   gates — that early return is exactly where it would be lost;
3. fail closed: an unreadable assignment table refuses, never "unrestricted";
4. every refusal is an audit row naming caller, key, callee and skill;
5. the grant surface — admin-and-interactive on writes (read off the real
   route table), a grant only for a skill that IS library-assigned, idempotent
   both ways, and the per-edge read that doubles as the upgrade report;
6. both migration tracks, and the AgentRef registry.
"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = [pytest.mark.unit]

CALLER = "sales-662"
CALLEE = "finance-662"
OTHER = "ops-662"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def world(monkeypatch):
    """The real db facade on the per-process SQLite, a clean slate for the
    three agents, and the audit sink captured."""
    from database import db
    import services.peer_skill_policy as policy
    import services.skill_gate_service as svc

    for name in (CALLER, CALLEE, OTHER):
        db.delete_agent_skills(name)
        db.delete_agent_peer_skill_grants(name)
    audits = []

    async def _audit(**kw):
        audits.append(kw)

    monkeypatch.setattr(policy, "_audit_refusal", _audit)
    # Nothing on these agents is gated: the peer rule must hold on its own.
    monkeypatch.setattr(svc, "list_skill_gates", lambda agent: {})
    yield SimpleNamespace(db=db, policy=policy, svc=svc, audits=audits)
    for name in (CALLER, CALLEE, OTHER):
        db.delete_agent_skills(name)
        db.delete_agent_peer_skill_grants(name)


def _peer(svc, name=CALLER, key="key-662"):
    return svc.Requester(kind="agent", key=f"agent:{name}", agent_name=name, mcp_key_id=key)


async def _enforce(world, text, requester, agent=CALLEE, **kw):
    kw.setdefault("triggered_by", "agent")
    return await world.svc.enforce(agent, request_text=text, requester=requester, **kw)


# ---- 1. the decision at the seam ------------------------------------------------

class TestTheDefault:
    def test_a_peer_cannot_invoke_an_individually_assigned_library_skill(self, world):
        world.db.assign_skill(CALLEE, "pay-invoice", "owner")
        with pytest.raises(world.svc.SkillGateRefused) as info:
            _run(_enforce(world, "/pay-invoice 100 EUR to ACME", _peer(world.svc)))
        err = info.value
        assert err.status_code == 403
        assert err.code == "skill_not_granted"
        assert err.detail()["skills"] == ["pay-invoice"]
        assert CALLER in str(err) and CALLEE in str(err)

    def test_a_set_propagated_skill_is_restricted_too(self, world):
        """The ent#662 comment: a commit to a set's member list adds skills to
        every holder as assigned_by='system', individual=0. Same rule."""
        world.db.assign_skill(CALLEE, "quarter-close", "system")
        assert world.db.set_skill_individual(CALLEE, "quarter-close", False)
        with pytest.raises(world.svc.SkillGateRefused) as info:
            _run(_enforce(world, "please run /quarter-close now", _peer(world.svc)))
        assert info.value.code == "skill_not_granted"

    def test_a_declared_skill_stays_callable_over_the_edge(self, world):
        world.db.assign_skill(CALLEE, "pay-invoice", "owner")
        decision = _run(_enforce(world, "/weekly-report", _peer(world.svc)))
        assert decision.ungated
        assert world.audits == []

    def test_a_conflict_name_is_the_agents_own_skill(self, world):
        """#2914: the platform refused to write over the agent's own directory,
        so the copy that runs is DECLARED."""
        world.db.assign_skill(CALLEE, "research", "owner")
        world.db.set_skill_delivery_status(CALLEE, ["research"], [])
        assert _run(_enforce(world, "/research topic", _peer(world.svc))).ungated

    def test_a_grant_opens_exactly_that_skill_for_exactly_that_caller(self, world):
        world.db.assign_skill(CALLEE, "pay-invoice", "owner")
        world.db.assign_skill(CALLEE, "refund", "owner")
        assert world.db.grant_peer_skill(CALLER, CALLEE, "pay-invoice", "admin") is True
        assert _run(_enforce(world, "/pay-invoice 5", _peer(world.svc))).ungated
        with pytest.raises(world.svc.SkillGateRefused):      # another skill
            _run(_enforce(world, "/refund 5", _peer(world.svc)))
        with pytest.raises(world.svc.SkillGateRefused):      # another caller
            _run(_enforce(world, "/pay-invoice 5", _peer(world.svc, name=OTHER)))
        assert world.db.revoke_peer_skill(CALLER, CALLEE, "pay-invoice") is True
        with pytest.raises(world.svc.SkillGateRefused):      # revoked
            _run(_enforce(world, "/pay-invoice 5", _peer(world.svc)))

    def test_one_refused_name_refuses_the_request_and_names_only_the_unguarded(self, world):
        world.db.assign_skill(CALLEE, "pay-invoice", "owner")
        world.db.assign_skill(CALLEE, "refund", "owner")
        world.db.grant_peer_skill(CALLER, CALLEE, "pay-invoice", "admin")
        with pytest.raises(world.svc.SkillGateRefused) as info:
            _run(_enforce(world, "/pay-invoice then /refund", _peer(world.svc)))
        assert info.value.detail()["skills"] == ["refund"]

    def test_a_name_hidden_in_the_context_text_is_read_too(self, world):
        world.db.assign_skill(CALLEE, "pay-invoice", "owner")
        with pytest.raises(world.svc.SkillGateRefused):
            _run(_enforce(world, "hello", _peer(world.svc), context_text="MCP key name: /pay-invoice"))


class TestWhoIsNotAPeer:
    @pytest.fixture(autouse=True)
    def _assigned(self, world):
        world.db.assign_skill(CALLEE, "pay-invoice", "owner")

    def test_the_agent_itself(self, world):
        assert _run(_enforce(world, "/pay-invoice", _peer(world.svc, name=CALLEE))).ungated

    def test_a_person(self, world):
        person = world.svc.Requester(kind="person", key="person:a@example.com",
                                     email="a@example.com", is_person=True)
        assert _run(_enforce(world, "/pay-invoice", person, triggered_by="chat")).ungated

    def test_a_schedule(self, world):
        sched = world.svc.Requester(kind="schedule", key="schedule:s1")
        assert _run(_enforce(world, "/pay-invoice", sched, triggered_by="schedule")).ungated

    def test_a_connector_has_its_own_allow_list(self, world):
        conn = world.svc.Requester(kind="connector", key="connector:k")
        assert _run(_enforce(world, "/pay-invoice", conn, triggered_by="mcp")).ungated

    def test_the_system_agent_holds_no_edge(self, world):
        assert _run(_enforce(world, "/pay-invoice", _peer(world.svc, name="trinity-system"))).ungated


# ---- 2. the backstop, on an agent with no gates -----------------------------------

def test_the_execute_task_backstop_refuses_on_an_ungated_agent(world):
    """`_skill_gate_backstop` returned as soon as the gate map was empty — so a
    fan-out or event dispatch would skip the peer rule entirely."""
    from services.task_execution_service import TaskExecutionService

    world.db.assign_skill(CALLEE, "pay-invoice", "owner")
    fields = dict(source_user_id=1, source_user_email=None, source_agent_name=CALLER,
                  source_mcp_key_id="key-662", source_mcp_key_name=None, model=None,
                  timeout_seconds=None, source_channel=None, source_channel_chat_id=None)
    with pytest.raises(world.svc.SkillGateRefused) as info:
        _run(TaskExecutionService()._skill_gate_backstop(
            agent_name=CALLEE, message="/pay-invoice 3", request_text=None,
            triggered_by="fan_out", execution_id=None, fields=fields))
    assert info.value.code == "skill_not_granted"
    # ...and lets a declared skill through, unchanged.
    _run(TaskExecutionService()._skill_gate_backstop(
        agent_name=CALLEE, message="/weekly-report", request_text=None,
        triggered_by="fan_out", execution_id=None, fields=fields))


# ---- 3. fail closed ----------------------------------------------------------------

def test_an_unreadable_assignment_table_refuses_a_peer(world, monkeypatch):
    def _boom(agent):
        raise RuntimeError("db down")
    monkeypatch.setattr(world.db, "get_agent_skills", _boom)
    with pytest.raises(world.svc.SkillGateRefused) as info:
        _run(_enforce(world, "/anything", _peer(world.svc)))
    assert (info.value.status_code, info.value.code) == (503, "peer_surface_unavailable")


def test_an_unreadable_assignment_table_does_not_touch_a_person(world, monkeypatch):
    monkeypatch.setattr(world.db, "get_agent_skills", lambda a: (_ for _ in ()).throw(RuntimeError()))
    person = world.svc.Requester(kind="person", key="person:a@example.com", is_person=True)
    assert _run(_enforce(world, "/anything", person, triggered_by="chat")).ungated


# ---- 4. the audit row --------------------------------------------------------------

def test_every_refusal_is_an_audit_row(world):
    world.db.assign_skill(CALLEE, "pay-invoice", "owner")
    with pytest.raises(world.svc.SkillGateRefused):
        _run(_enforce(world, "/pay-invoice", _peer(world.svc), origin_execution_id="exec-662"))
    assert len(world.audits) == 1
    row = world.audits[0]
    assert row["caller"] == CALLER and row["callee"] == CALLEE
    assert row["skills"] == ["pay-invoice"]
    assert row["mcp_key_id"] == "key-662"
    assert row["reason"] == "skill_not_granted"
    assert row["execution_id"] == "exec-662"


# ---- 5. the grant surface ----------------------------------------------------------

def _admin():
    return SimpleNamespace(id=1, username="admin", email=None, role="admin", mcp_scope=None,
                           agent_name=None, connector_agent=None, mcp_key_id=None)


def _request(path="/api/agents/x/peer-skills"):
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), method="PUT",
                           scope={"path": path}, state=SimpleNamespace(request_id="r1"))


def test_the_write_routes_are_admin_and_the_read_route_exists():
    """Read off FastAPI's real dependant graph — the next refactor cannot drop
    the admin gate from a write route without this going red."""
    from routers.agent_files import router, grant_agent_peer_skill
    # The unit conftest pops `dependencies` between tests, so read the object
    # the ROUTER bound, not a fresh import (the ent#596 `_Deps` lesson).
    require_admin = grant_agent_peer_skill.__globals__["require_admin"]
    assert (require_admin.__module__, require_admin.__name__) == ("dependencies", "require_admin")

    def _deps(route):
        out, stack = set(), [route.dependant]
        while stack:
            d = stack.pop()
            if d.call:
                out.add(d.call)
            stack.extend(d.dependencies)
        return out

    routes = {(m, r.path): r for r in router.routes for m in r.methods}
    assert ("GET", "/api/agents/{agent_name}/peer-skills") in routes
    for method in ("PUT", "DELETE"):
        route = routes[(method, "/api/agents/{agent_name}/peer-skills/{target}/{skill}")]
        assert require_admin in _deps(route), method


def test_granting_needs_an_interactive_session(world):
    from fastapi import HTTPException
    from routers.agent_files import grant_agent_peer_skill
    key_admin = _admin()
    key_admin.mcp_scope = "user"
    with pytest.raises(HTTPException) as info:
        _run(grant_agent_peer_skill(CALLER, CALLEE, "pay-invoice", _request(), key_admin))
    assert info.value.status_code == 403
    assert world.db.granted_peer_skills(CALLER, CALLEE) == set()


def test_a_grant_is_only_for_a_library_assigned_skill(world, monkeypatch):
    from fastapi import HTTPException
    from routers import agent_files
    monkeypatch.setattr(agent_files.platform_audit_service, "log", _noop_log)
    monkeypatch.setattr(world.policy, "_agent_exists", lambda name: True)
    with pytest.raises(HTTPException) as info:
        _run(agent_files.grant_agent_peer_skill(CALLER, CALLEE, "weekly-report", _request(), _admin()))
    assert info.value.status_code == 422
    assert info.value.detail["code"] == "skill_not_assigned"


def test_grant_and_revoke_are_idempotent_and_audited(world, monkeypatch):
    from routers import agent_files
    logged = []

    async def _log(**kw):
        logged.append(kw)
    monkeypatch.setattr(agent_files.platform_audit_service, "log", _log)
    monkeypatch.setattr(world.policy, "_agent_exists", lambda name: True)
    world.db.assign_skill(CALLEE, "pay-invoice", "owner")

    first = _run(agent_files.grant_agent_peer_skill(CALLER, CALLEE, "pay-invoice", _request(), _admin()))
    again = _run(agent_files.grant_agent_peer_skill(CALLER, CALLEE, "pay-invoice", _request(), _admin()))
    assert (first["changed"], again["changed"]) == (True, False)
    assert "may now run" in first["consent"]
    gone = _run(agent_files.revoke_agent_peer_skill(CALLER, CALLEE, "pay-invoice", _request(), _admin()))
    assert gone["changed"] is True
    assert [e["event_action"] for e in logged] == ["peer_skill_grant", "peer_skill_revoke"]


def test_self_and_the_system_agent_are_refused_a_grant(world, monkeypatch):
    from fastapi import HTTPException
    from routers import agent_files
    monkeypatch.setattr(world.policy, "_agent_exists", lambda name: True)
    world.db.assign_skill(CALLEE, "pay-invoice", "owner")
    for caller in (CALLEE, "trinity-system"):
        with pytest.raises(HTTPException) as info:
            _run(agent_files.grant_agent_peer_skill(caller, CALLEE, "pay-invoice", _request(), _admin()))
        assert info.value.status_code == 422


def test_the_per_edge_view_is_the_upgrade_report(world, monkeypatch):
    """Every edge into an agent that holds library skills shows what the edge
    no longer reaches, until an admin grants it."""
    monkeypatch.setattr(world.db, "get_permitted_agents", lambda caller: [CALLEE, OTHER])
    monkeypatch.setattr(world.db, "can_user_access_agent", lambda user, agent: agent != OTHER)
    world.db.assign_skill(CALLEE, "pay-invoice", "owner")
    world.db.assign_skill(CALLEE, "refund", "system")
    world.db.assign_skill(OTHER, "secret-skill", "owner")
    world.db.grant_peer_skill(CALLER, CALLEE, "pay-invoice", "admin")

    view = world.policy.peer_skills_view(CALLER, viewer=_admin())
    assert [t["target"] for t in view["targets"]] == [CALLEE]        # OTHER: viewer can't open it
    skills = {s["name"]: s for s in view["targets"][0]["skills"]}
    assert skills["pay-invoice"]["granted"] is True and skills["pay-invoice"]["granted_by"] == "admin"
    assert skills["refund"]["granted"] is False
    assert view["targets"][0]["not_granted"] == 1


async def _noop_log(**kw):
    return None


# ---- 6. storage ------------------------------------------------------------------

def test_the_sqlite_migration_builds_the_table(tmp_path):
    import sqlite3
    from db.migrations import MIGRATIONS, _migrate_agent_peer_skill_grants
    conn = sqlite3.connect(tmp_path / "m.db")
    cur = conn.cursor()
    _migrate_agent_peer_skill_grants(cur, conn)
    _migrate_agent_peer_skill_grants(cur, conn)            # idempotent
    assert {r[1] for r in cur.execute("PRAGMA table_info(agent_peer_skill_grants)")} == {
        "caller_agent", "target_agent", "skill_name", "granted_by", "granted_at"}
    assert ("agent_peer_skill_grants", _migrate_agent_peer_skill_grants) in MIGRATIONS


def test_the_alembic_revision_extends_the_single_head():
    import importlib.util
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "src/backend/migrations/versions"
    spec = importlib.util.spec_from_file_location("rev", root / "0099_agent_peer_skill_grants.py")
    rev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rev)
    assert (rev.revision, rev.down_revision) == (
        "0099_agent_peer_skill_grants", "0098_proactive_consent_not_asked")


def test_a_grant_follows_both_agents_through_rename_and_delete():
    from db.agent_cleanup import AGENT_REFS, Policy
    refs = {(r.table, r.column): r.policy for r in AGENT_REFS}
    assert refs[("agent_peer_skill_grants", "caller_agent")] is Policy.CASCADE
    assert refs[("agent_peer_skill_grants", "target_agent")] is Policy.CASCADE


# ---- 7. a parent may run what it spawned (parent -> child only) --------------------

@pytest.fixture
def spawned(world, monkeypatch):
    """CALLEE was spawned by CALLER with the key CALLER holds now — the ent#629
    relation (`dependencies._spawned_by_caller`), driven through the real db reads."""
    def _info(agent):
        return {"spawned_by_agent": CALLER, "spawned_by_key_id": "key-parent"} if agent == CALLEE else {}

    def _key(agent):
        return SimpleNamespace(id="key-parent") if agent == CALLER else None
    monkeypatch.setattr(world.db, "get_agent_ephemeral_info", _info)
    monkeypatch.setattr(world.db, "get_agent_mcp_api_key", _key)
    world.db.assign_skill(CALLEE, "pay-invoice", "owner")
    world.db.assign_skill(CALLER, "pay-invoice", "owner")
    return world


def test_a_parent_may_run_a_library_skill_on_an_agent_it_spawned(spawned):
    assert _run(_enforce(spawned, "/pay-invoice", _peer(spawned.svc))).ungated


def test_a_child_calling_its_parent_still_needs_a_grant(spawned):
    with pytest.raises(spawned.svc.SkillGateRefused) as info:
        _run(_enforce(spawned, "/pay-invoice", _peer(spawned.svc, name=CALLEE), agent=CALLER))
    assert info.value.code == "skill_not_granted"


def test_a_stale_spawn_key_is_not_a_parent(spawned, monkeypatch):
    """A name-only match is forgeable via name reuse; the key id decides."""
    monkeypatch.setattr(spawned.db, "get_agent_mcp_api_key", lambda a: SimpleNamespace(id="key-new"))
    with pytest.raises(spawned.svc.SkillGateRefused):
        _run(_enforce(spawned, "/pay-invoice", _peer(spawned.svc)))


# ---- 8. a grant goes with its edge ----------------------------------------------------

@pytest.fixture
def edges(world):
    for caller in (CALLER, OTHER):
        for target in (CALLER, CALLEE, OTHER):
            world.db.remove_agent_permission(caller, target)
    yield world
    for caller in (CALLER, OTHER):
        for target in (CALLER, CALLEE, OTHER):
            world.db.remove_agent_permission(caller, target)


def test_removing_an_edge_removes_its_grants_and_only_its_grants(edges):
    db = edges.db
    db.add_agent_permission(CALLER, CALLEE, "admin")
    db.add_agent_permission(OTHER, CALLEE, "admin")
    db.grant_peer_skill(CALLER, CALLEE, "pay-invoice", "admin")
    db.grant_peer_skill(OTHER, CALLEE, "pay-invoice", "admin")
    assert db.remove_agent_permission(CALLER, CALLEE) is True
    assert db.granted_peer_skills(CALLER, CALLEE) == set()
    assert db.granted_peer_skills(OTHER, CALLEE) == {"pay-invoice"}


def test_a_full_replace_drops_grants_on_the_edges_it_drops(edges):
    db = edges.db
    db.set_agent_permissions(CALLER, [CALLEE, OTHER], "admin")
    db.grant_peer_skill(CALLER, CALLEE, "pay-invoice", "admin")
    db.grant_peer_skill(CALLER, OTHER, "refund", "admin")
    db.set_agent_permissions(CALLER, [CALLEE], "admin")              # OTHER dropped
    assert db.granted_peer_skills(CALLER, CALLEE) == {"pay-invoice"}
    assert db.granted_peer_skills(CALLER, OTHER) == set()


# ---- 9. the honest upgrade: what an edge used, now refused -----------------------------

@pytest.fixture
def history(world, monkeypatch):
    """Edges + the caller's recent executions on the target, as rows."""
    rows = []
    monkeypatch.setattr(world.db, "get_all_permission_edges",
                        lambda accessible_agents=None: [{"source": CALLER, "target": CALLEE},
                                                        {"source": OTHER, "target": CALLEE}])
    monkeypatch.setattr(world.db, "recent_peer_messages",
                        lambda caller, target, since, limit=500: [
                            r for r in rows if r["caller"] == caller and r["target"] == target])
    world.db.assign_skill(CALLEE, "pay-invoice", "owner")
    world.db.assign_skill(CALLEE, "refund", "system")
    world.db.assign_skill(CALLEE, "never-called", "owner")
    return SimpleNamespace(world=world, rows=rows)


def _row(caller, message, at):
    return {"caller": caller, "target": CALLEE, "message": message, "started_at": at}


def test_the_drift_report_lists_the_pairs_an_edge_used_and_will_now_be_refused(history):
    history.rows += [
        _row(CALLER, "/pay-invoice 1", "2026-10-10T00:00:00Z"),
        _row(CALLER, "please /pay-invoice 2", "2026-10-12T00:00:00Z"),
        _row(CALLER, "/weekly-report", "2026-10-12T00:00:00Z"),       # declared: not drift
        _row(OTHER, "/refund 3", "2026-10-11T00:00:00Z"),
    ]
    report = history.world.policy.drift_report()
    pairs = {(p["caller"], p["target"], p["skill"]): p for p in report["pairs"]}
    assert set(pairs) == {(CALLER, CALLEE, "pay-invoice"), (OTHER, CALLEE, "refund")}
    assert pairs[(CALLER, CALLEE, "pay-invoice")]["uses"] == 2
    assert pairs[(CALLER, CALLEE, "pay-invoice")]["last_used_at"] == "2026-10-12T00:00:00Z"
    assert report["window_days"] == 30


def test_a_granted_pair_leaves_the_report(history):
    history.rows.append(_row(CALLER, "/pay-invoice 1", "2026-10-10T00:00:00Z"))
    history.world.db.grant_peer_skill(CALLER, CALLEE, "pay-invoice", "admin")
    assert history.world.policy.drift_report()["pairs"] == []


def test_the_per_edge_view_says_when_a_library_skill_was_last_used(history, monkeypatch):
    monkeypatch.setattr(history.world.db, "get_permitted_agents", lambda caller: [CALLEE])
    monkeypatch.setattr(history.world.db, "can_user_access_agent", lambda u, a: True)
    history.rows.append(_row(CALLER, "/refund 9", "2026-10-13T00:00:00Z"))
    view = history.world.policy.peer_skills_view(CALLER, viewer=_admin())
    skills = {s["name"]: s for s in view["targets"][0]["skills"]}
    assert skills["refund"]["last_used_at"] == "2026-10-13T00:00:00Z"
    assert skills["never-called"]["last_used_at"] is None


def test_the_drift_route_is_admin_only():
    from routers.skills import router, list_peer_skill_drift
    require_admin = list_peer_skill_drift.__globals__["require_admin"]
    route = next(r for r in router.routes if r.path == "/api/skills/peer-grants/drift")
    calls, stack = set(), [route.dependant]
    while stack:
        d = stack.pop()
        if d.call:
            calls.add(d.call)
        stack.extend(d.dependencies)
    assert require_admin in calls


def test_recent_peer_messages_reads_the_execution_ledger(world):
    """The db read the report rests on, over the real table."""
    import uuid
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions as t
    ids = [str(uuid.uuid4()) for _ in range(3)]
    rows = [
        dict(id=ids[0], agent_name=CALLEE, source_agent_name=CALLER, message="/pay-invoice", started_at="2026-10-14T00:00:00Z"),
        dict(id=ids[1], agent_name=CALLEE, source_agent_name=OTHER, message="/pay-invoice", started_at="2026-10-14T00:00:00Z"),
        dict(id=ids[2], agent_name=CALLEE, source_agent_name=CALLER, message="/old", started_at="2026-01-01T00:00:00Z"),
    ]
    from sqlalchemy import delete
    with get_engine().begin() as conn:
        for r in rows:
            conn.execute(insert(t).values(schedule_id="peer-662", status="success", triggered_by="agent", **r))
    try:
        got = world.db.recent_peer_messages(CALLER, CALLEE, "2026-10-01T00:00:00Z")
        assert [g["message"] for g in got] == ["/pay-invoice"]
        assert got[0]["started_at"] == "2026-10-14T00:00:00Z"
    finally:
        with get_engine().begin() as conn:
            conn.execute(delete(t).where(t.c.id.in_(ids)))


# ---- 10. what the refusal tells the caller (trinity#3949) --------------------------
# The matcher reads a `/name` anywhere, so a peer that only MENTIONS a library
# skill is refused too. The text is what the calling model acts on ("On
# `retryable: false`, do what it says") — so it says a mention trips it, how to
# send a mention, and never sends the caller to widen a standing permission.

_ONE = (
    f"Not run, nothing was delivered: this message names the skill pay-invoice, "
    f"which {CALLER} may not run on {CALLEE} (it was assigned from the skills library "
    f"and needs a grant). If you only meant to mention it, write its name without the "
    f"leading slash and send again. Do not carry out its steps another way. Asking for it "
    f"by name without the slash is the same request and is not permitted."
)
_TWO = (
    f"Not run, nothing was delivered: this message names the skills pay-invoice, refund, "
    f"which {CALLER} may not run on {CALLEE} (they were assigned from the skills library "
    f"and need a grant). If you only meant to mention them, write their names without the "
    f"leading slashes and send again. Do not carry out their steps another way. Asking for "
    f"them by name without the slashes is the same request and is not permitted."
)


@pytest.fixture
def refused(world):
    """The refusal a peer gets for a message that only mentions library skills."""
    world.db.assign_skill(CALLEE, "pay-invoice", "owner")
    world.db.assign_skill(CALLEE, "refund", "owner")

    def _refuse(text):
        with pytest.raises(world.svc.SkillGateRefused) as info:
            _run(_enforce(world, text, _peer(world.svc)))
        return info.value
    return _refuse


_MENTIONS = {
    "one": ("Thanks, the tree was left dirty by /pay-invoice", _ONE),
    "two": ("Both /pay-invoice and /refund left the tree dirty", _TWO),
}


@pytest.mark.parametrize("text,expected", _MENTIONS.values(), ids=list(_MENTIONS))
def test_the_refusal_says_a_mention_trips_it_and_how_to_send_one(refused, text, expected):
    assert str(refused(text)) == expected


@pytest.mark.parametrize("text", [t for t, _ in _MENTIONS.values()], ids=list(_MENTIONS))
def test_the_refusal_does_not_send_the_caller_to_widen_a_permission(refused, text):
    message = str(refused(text))
    assert "Permissions" not in message
    assert "admin" not in message
    assert "grant it" not in message


@pytest.mark.parametrize("text", [t for t, _ in _MENTIONS.values()], ids=list(_MENTIONS))
def test_no_name_in_the_refusal_carries_a_slash(refused, text):
    """The text lands in rows and replies; a slashed name there reads as a
    request to run the skill."""
    assert "/" not in str(refused(text))


def test_the_refusal_still_names_the_skills_the_caller_and_the_agent(refused):
    err = refused("Both /pay-invoice and /refund left the tree dirty")
    detail = err.detail()
    assert (err.status_code, err.code, detail["code"]) == (403, "skill_not_granted", "skill_not_granted")
    assert detail["skills"] == ["pay-invoice", "refund"]
    assert (detail["caller"], detail["agent"]) == (CALLER, CALLEE)
    assert detail["message"] == _TWO


def test_the_backstop_refuses_with_the_same_text(world):
    """Fan-out and the other `execute_task` producers close their row SKIPPED
    with this text, so it is the caller-facing text there too."""
    from services.task_execution_service import TaskExecutionService

    world.db.assign_skill(CALLEE, "pay-invoice", "owner")
    fields = dict(source_user_id=1, source_user_email=None, source_agent_name=CALLER,
                  source_mcp_key_id="key-662", source_mcp_key_name=None, model=None,
                  timeout_seconds=None, source_channel=None, source_channel_chat_id=None)
    with pytest.raises(world.svc.SkillGateRefused) as info:
        _run(TaskExecutionService()._skill_gate_backstop(
            agent_name=CALLEE, message="Thanks, the tree was left dirty by /pay-invoice",
            request_text=None, triggered_by="fan_out", execution_id=None, fields=fields))
    assert str(info.value) == _ONE
