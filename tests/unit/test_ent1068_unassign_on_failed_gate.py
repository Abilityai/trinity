"""trinity-enterprise#1068: a held skill whose declared gate cannot be written is unassigned.

ent#1043 made the failure honest: a gate write that fails on a skill the
member already holds left the skill running with no approval gate, and the
result said so. The owner's ruling is stricter — no gate, no skill. With the
per-install switch `apply_unassign_on_failed_gate` on (the default) apply
unassigns the skill through the unassign route's own sequence
(`skill_assignment_service.unassign_and_remove`: row, package, broadcast, gate
reconcile) and says so: the gate failure's reason names the unassign, the
member gets a `skill_unassigned_gate_failed` write change, one warning line.

Report-only stays where an unassign is wrong or impossible: the switch is off,
a stored gate still applies (`previous_gate_stands`), a set brings the skill
(`skill_held_via_set`), the unassign itself is refused (a lost lease), or the
gate row turns out to be stored after all.

Runs over the REAL `db` on a full schema through the real apply route, on the
ent#1043 harness (`_ent695_world.py`). Package removal is faked at
`skill_service.remove_skills`; the gate reconcile after the unassign is the
real `sync_gates`, with the ent#1030 never-clear pin installed (apply clears no
gate, and there is none to clear).
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402,F401

pytest.importorskip("docker", reason="backend venv required (system_service chain)")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _ent695_world as W  # noqa: E402
from database import db  # noqa: E402
from models import SystemApplyRequest  # noqa: E402
from services import (  # noqa: E402
    assignment_provider, docker_utils, skill_assignment_service, skill_gate_map_service,
    skill_gate_service, skill_set_service, system_service,
)
from services.skill_gate_map_service import SkillGateMapRefused  # noqa: E402
from services.skill_service import SkillInjectionBusy, skill_service  # noqa: E402

pytestmark = pytest.mark.unit

SYSTEM = W.SYSTEM
LEAD = f"{SYSTEM}-lead"
LIBRARY = {"research", "deploy"}
SETS = {
    "ops-core": {"name": "ops-core", "source_id": "src-1", "source_name": "default",
                 "shadowed_by": [], "status": "ok", "problems": [],
                 "members": [{"name": "research", "present": True},
                             {"name": "deploy", "present": True}],
                 "requires": {"env": []}, "schedules": []},
}
SWITCH = "apply_unassign_on_failed_gate"
UNASSIGNED = "skill_unassigned_gate_failed"
UNGATED = "skill_runs_ungated"
PREVIOUS = "previous_gate_stands"
VIA_SET = "skill_held_via_set"


def _patch_skill_service(monkeypatch, attr, value):
    live = importlib.import_module("services.skill_service").skill_service
    for obj in {id(o): o for o in (skill_service, live)}.values():
        monkeypatch.setattr(obj, attr, value)


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    w = W.make_world(monkeypatch, tmp_path)
    w.removed = []
    w.removal_error = None

    async def log(**kw):
        pass

    audit_mod = importlib.import_module("services.platform_audit_service")
    monkeypatch.setattr(audit_mod.platform_audit_service, "log", log)

    async def state(name):
        return w.docker.states().get(name, "missing")

    async def exec_(container_name, command, timeout=60, *, environment=None, user="developer"):
        return {"exit_code": 0, "output": "", "timed_out": False}

    DS = importlib.import_module("services.docker_service")
    monkeypatch.setattr(docker_utils, "agent_container_state_async", state)
    monkeypatch.setattr(DS, "execute_command_in_container", exec_)

    _patch_skill_service(monkeypatch, "get_skill",
                         lambda n: {"name": n} if n in LIBRARY else None)
    _patch_skill_service(monkeypatch, "list_skills", lambda: [])
    monkeypatch.setattr(skill_set_service, "library_sets", lambda **_k: dict(SETS))

    async def deliver(agent, names):
        return {"status": "pending_start", "skills": {n: {"status": "pending_start"} for n in names}}

    async def remove(agent, names):
        if w.removal_error is not None:
            raise w.removal_error
        w.removed.append((agent, tuple(names)))
        return {"success": True, "skills_removed": len(names), "skills_failed": 0, "results": {}}

    _patch_skill_service(monkeypatch, "deliver_assigned", deliver)
    _patch_skill_service(monkeypatch, "remove_skills", remove)

    # ent#1030's never-clear pin: the unassign's gate reconcile clears nothing.
    async def never_clear(*_a, **_k):
        raise AssertionError("apply never clears a gate")

    def never_clear_row(*_a, **_k):
        raise AssertionError("apply never clears a gate row")

    monkeypatch.setattr(skill_gate_map_service, "clear_gate", never_clear)
    monkeypatch.setattr(db, "clear_skill_gate", never_clear_row)
    monkeypatch.setattr(db, "drop_unassigned_skill_gates", never_clear_row)

    W.install_executor_seams(w, monkeypatch)
    assignment_provider.clear_provider()
    yield w
    assignment_provider.clear_provider()


def _switch(on: bool):
    db.set_setting(SWITCH, "true" if on else "false")


def _manifest(gates, *, skills=None, tags=("x",)) -> str:
    lines = [f"name: {SYSTEM}", "agents:", "  lead:", "    template: local:plain", "    gates:"]
    for skill, spec in gates.items():
        if spec:
            lines.append(f"      {skill}:")
            lines.extend(f"        {k}: {v}" for k, v in spec.items())
        else:
            lines.append(f"      {skill}: {{}}")
    if skills is not None:
        lines.append(f"    skills: [{', '.join(skills)}]")
    if tags:
        lines.append(f"    tags: [{', '.join(tags)}]")
    return "\n".join(lines) + "\n"


def _apply(world, monkeypatch, text, *, dry_run=False):
    """A real `POST /api/systems/apply` over the real database facade."""
    from starlette.requests import Request

    from routers import systems

    real = system_service.apply_manifest

    async def apply_with_seams(manifest, u, request, *, dry_run):
        return await real(manifest, u, request, dry_run=dry_run,
                          create_agent_fn=world.create, start_agent_fn=world.docker.start)

    monkeypatch.setattr(systems, "apply_manifest", apply_with_seams)
    request = Request({"type": "http", "method": "POST", "path": "/api/systems/apply",
                       "headers": [], "client": ("127.0.0.1", 1), "query_string": b""})
    return asyncio.run(systems.apply_system(
        SystemApplyRequest(manifest=text, dry_run=dry_run), request,
        current_user=world.user(W.ALICE)))


def _member(res):
    return next(m for m in res.members if m.name == LEAD)


def _changes(member, code, field=None):
    return [c for c in member.changes if c.code == code and (field is None or c.field == field)]


def _failures(res, step):
    return [f for f in res.failed if f.name == LEAD and f.step == step]


def _lines(res, skill, words):
    return [w for w in res.warnings if f"'{LEAD}'" in w and f"'{skill}'" in w and words in w]


def _set_gate(name, skill, **changes):
    return asyncio.run(skill_gate_map_service.set_gate(
        name, skill, changes=changes,
        ctx=skill_gate_map_service.GateContext(actor=None, via="system", trigger="direct")))


def _refuse_after_validation(monkeypatch):
    async def refusing(agent_name, skill_name, *, changes, ctx):
        raise SkillGateMapRefused(404, "agent_not_found", "Agent not found")
    monkeypatch.setattr(skill_gate_map_service, "set_gate", refusing)
    return "agent_not_found"


def _db_fault(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("disk I/O error")
    monkeypatch.setattr(db, "write_skill_gate", boom)
    return "disk I/O error"


def _lose_lease_after_tag(monkeypatch):
    state = {"lost": False}
    real_add_tag = db.add_agent_tag

    def add_tag(name, tag):
        out = real_add_tag(name, tag)
        state["lost"] = True
        return out

    monkeypatch.setattr(db, "add_agent_tag", add_tag)
    monkeypatch.setattr(system_service.ApplyLeaseKeeper, "ok",
                        property(lambda self: not state["lost"]))
    return "lock_lost"


FAILURES = [
    pytest.param(_refuse_after_validation, id="service_refusal_after_validation"),
    pytest.param(_db_fault, id="db_fault"),
]


def _held(world, *skills):
    world.seed_agent(LEAD)
    for s in skills:
        db.assign_skill(LEAD, s, W.ALICE)


class TestSwitchOnUnassigns:
    """The default: a held skill with no gate in force, whose gate write
    failed, is unassigned and its package removed — and the result says so."""

    @pytest.mark.parametrize("fail", FAILURES)
    def test_the_held_skill_is_unassigned_and_named(self, world, monkeypatch, fail):
        _held(world, "research")
        code = fail(monkeypatch)
        res = _apply(world, monkeypatch, _manifest({"research": {}}))

        # The state: no row, no package, nothing for admission to read.
        assert "research" not in db.get_agent_skill_names(LEAD)
        assert world.removed == [(LEAD, ("research",))]
        assert "research" not in skill_gate_service.read_gates(LEAD)

        member = _member(res)
        assert res.status == "partial" and member.applied is False
        [failure] = _failures(res, "gates")
        assert failure.key == "research"
        assert failure.reason.startswith(code)
        assert f"'research' was unassigned from '{LEAD}' because its gate was not written" \
            in failure.reason
        assert _failures(res, "skills") == []
        [change] = _changes(member, UNASSIGNED)
        assert (change.field, change.action, change.desired) == ("skills", "write", ["research"])
        assert "re-declare" in change.note and "re-apply" in change.note
        assert _changes(member, UNGATED) == []
        assert len(_lines(res, "research", "was unassigned")) == 1
        assert _lines(res, "research", "no approval gate") == []
        # Decision 7: the landed unassign is a skills write; no gate landed.
        assert res.summary["skills"]["write"] == 1
        assert res.summary["gates"]["write"] == 0
        assert "x" in db.get_agent_tags(LEAD)

    def test_the_switch_is_on_by_default(self, world, monkeypatch):
        """No row written: the default decides."""
        _held(world, "research")
        _refuse_after_validation(monkeypatch)
        _apply(world, monkeypatch, _manifest({"research": {}}))
        assert "research" not in db.get_agent_skill_names(LEAD)

    def test_several_failing_gates_each_unassign_their_own_skill(self, world, monkeypatch):
        _held(world, "research", "deploy")
        _refuse_after_validation(monkeypatch)
        res = _apply(world, monkeypatch, _manifest({"research": {}, "deploy": {}}))
        assert db.get_agent_skill_names(LEAD) == []
        assert sorted(world.removed) == [(LEAD, ("deploy",)), (LEAD, ("research",))]
        member = _member(res)
        assert sorted(c.desired[0] for c in _changes(member, UNASSIGNED)) == ["deploy", "research"]
        assert len(_failures(res, "gates")) == 2
        assert all("was unassigned" in f.reason for f in _failures(res, "gates"))
        assert res.summary["skills"]["write"] == 2

    def test_only_the_skill_whose_gate_failed_goes(self, world, monkeypatch):
        """One gate lands, one fails: the member keeps the gated skill."""
        _held(world, "research", "deploy")
        real = skill_gate_map_service.set_gate

        async def fail_research(agent_name, skill_name, *, changes, ctx):
            if skill_name == "research":
                raise SkillGateMapRefused(404, "agent_not_found", "Agent not found")
            return await real(agent_name, skill_name, changes=changes, ctx=ctx)

        monkeypatch.setattr(skill_gate_map_service, "set_gate", fail_research)
        res = _apply(world, monkeypatch, _manifest({"research": {}, "deploy": {}}))
        assert db.get_agent_skill_names(LEAD) == ["deploy"]
        assert "deploy" in skill_gate_service.read_gates(LEAD)
        assert [c.desired for c in _changes(_member(res), UNASSIGNED)] == [["research"]]


class TestSwitchOffReports:
    """The switch off: ent#1043's report, nothing unassigned."""

    @pytest.mark.parametrize("fail", FAILURES)
    def test_the_held_skill_stays_and_runs_ungated(self, world, monkeypatch, fail):
        _held(world, "research")
        _switch(False)
        code = fail(monkeypatch)
        res = _apply(world, monkeypatch, _manifest({"research": {}}))
        assert "research" in db.get_agent_skill_names(LEAD)
        assert world.removed == []
        member = _member(res)
        [failure] = _failures(res, "gates")
        assert failure.reason.startswith(code)
        assert "stays assigned and runs with NO approval gate" in failure.reason
        assert len(_changes(member, UNGATED)) == 1
        assert _changes(member, UNASSIGNED) == []
        assert len(_lines(res, "research", "no approval gate")) == 1
        assert res.summary["skills"]["write"] == 0


class TestReportOnlyCases:
    def test_a_lost_lease_refuses_the_unassign_and_the_ungated_report_stands(
            self, world, monkeypatch):
        _held(world, "research")
        _lose_lease_after_tag(monkeypatch)
        res = _apply(world, monkeypatch, _manifest({"research": {}}))
        assert "research" in db.get_agent_skill_names(LEAD)
        assert world.removed == []
        [gate_failure] = _failures(res, "gates")
        [skill_failure] = _failures(res, "skills")
        assert gate_failure.reason.startswith("lock_lost")
        assert "runs with NO approval gate" in gate_failure.reason
        assert (skill_failure.key, skill_failure.reason[:9]) == ("research", "lock_lost")
        member = _member(res)
        assert len(_changes(member, UNGATED)) == 1 and _changes(member, UNASSIGNED) == []
        assert len(_lines(res, "research", "no approval gate")) == 1

    def test_a_deferred_removal_says_the_package_can_still_load(self, world, monkeypatch):
        _held(world, "research")
        world.removal_error = SkillInjectionBusy("busy")
        _refuse_after_validation(monkeypatch)
        res = _apply(world, monkeypatch, _manifest({"research": {}}))
        assert "research" not in db.get_agent_skill_names(LEAD)
        [failure] = _failures(res, "gates")
        assert "was unassigned" in failure.reason
        assert "package removal is pending (injection_in_progress)" in failure.reason
        member = _member(res)
        assert len(_changes(member, UNASSIGNED)) == 1
        assert len(_changes(member, UNGATED)) == 1
        [line] = _lines(res, "research", "no approval gate")
        assert "was unassigned" in line and "next start" in line

    def test_a_gate_row_stored_despite_the_raise_unassigns_nothing(self, world, monkeypatch):
        _held(world, "research")
        real = skill_gate_map_service.set_gate

        async def lands_then_raises(agent_name, skill_name, *, changes, ctx):
            await real(agent_name, skill_name, changes=changes, ctx=ctx)
            raise RuntimeError("connection reset after commit")

        monkeypatch.setattr(skill_gate_map_service, "set_gate", lands_then_raises)
        res = _apply(world, monkeypatch, _manifest({"research": {}}))
        assert "research" in db.get_agent_skill_names(LEAD)
        assert "research" in skill_gate_service.read_gates(LEAD)
        assert world.removed == []
        [failure] = _failures(res, "gates")
        assert "gate row is stored all the same" in failure.reason
        member = _member(res)
        assert _changes(member, UNASSIGNED) == [] and _changes(member, UNGATED) == []
        assert _lines(res, "research", "no approval gate") == []

    @pytest.mark.parametrize("fail", FAILURES)
    def test_previous_gate_stands_stays_report_only(self, world, monkeypatch, fail):
        """Decision 1: a stored gate still applies, so nothing is unassigned
        and no gate is cleared."""
        _held(world, "research")
        _set_gate(LEAD, "research", approver="primary", deadline_hours=4)
        fail(monkeypatch)
        res = _apply(world, monkeypatch, _manifest({"research": {"deadline_hours": 12}}))
        assert "research" in db.get_agent_skill_names(LEAD)
        assert world.removed == []
        gate = skill_gate_service.read_gates(LEAD)["research"]
        assert (gate.approver, gate.deadline_hours) == ("primary", 4)
        member = _member(res)
        assert len(_changes(member, PREVIOUS)) == 1
        assert _changes(member, UNASSIGNED) == [] and _changes(member, UNGATED) == []
        assert "still runs under its previous gate" in _failures(res, "gates")[0].reason

    def test_a_set_held_skill_is_reported_not_unassigned(self, world, monkeypatch):
        """Decision 2: an unassign would take the set's other skills."""
        world.seed_agent(LEAD)
        asyncio.run(skill_assignment_service.assign_set_and_deliver(
            LEAD, "ops-core", world.user(W.ALICE), acting_agent=None))
        _refuse_after_validation(monkeypatch)
        res = _apply(world, monkeypatch, _manifest({"research": {}}))
        assert sorted(db.get_agent_skill_names(LEAD)) == ["deploy", "research"]
        assert world.removed == []
        member = _member(res)
        [via] = _changes(member, VIA_SET)
        assert via.desired == ["research"] and "unassign the set" in via.note
        assert len(_changes(member, UNGATED)) == 1
        assert _changes(member, UNASSIGNED) == []

    def test_a_landed_gate_unassigns_nothing(self, world, monkeypatch):
        _held(world, "research")
        res = _apply(world, monkeypatch, _manifest({"research": {}}))
        assert res.status == "applied", res.failed
        assert "research" in db.get_agent_skill_names(LEAD)
        assert world.removed == []
        assert _changes(_member(res), UNASSIGNED) == []


class TestDryRunSaysTheConsequence:
    """CP5: the dry run's gate change on a held skill names what a failed
    write would lead to; it predicts no failure and changes nothing."""

    @pytest.mark.parametrize("on", [True, False], ids=["switch_on", "switch_off"])
    def test_the_note_matches_the_switch(self, world, monkeypatch, on):
        _held(world, "research")
        _switch(on)
        _refuse_after_validation(monkeypatch)
        res = _apply(world, monkeypatch, _manifest({"research": {}}), dry_run=True)
        assert res.dry_run is True and res.failed == []
        [change] = [c for c in _member(res).changes if c.field == "gates"]
        assert change.code == "gate_missing"
        if on:
            assert f"'research' is unassigned ({SWITCH})" in change.note
        else:
            assert "stays assigned and runs with no approval gate" in change.note
            assert f"{SWITCH} is off" in change.note
        assert _changes(_member(res), UNASSIGNED) == []
        assert "research" in db.get_agent_skill_names(LEAD)
        assert world.removed == []

    def test_a_skill_not_yet_held_carries_no_consequence_note(self, world, monkeypatch):
        world.seed_agent(LEAD)
        res = _apply(world, monkeypatch, _manifest({"research": {}}, skills=["research"]),
                     dry_run=True)
        [change] = [c for c in _member(res).changes if c.field == "gates"]
        assert change.note is None or "unassigned" not in change.note


class TestTheUnassignRouteIsUnchanged:
    """CP1: the route delegates to the same service writer."""

    def test_the_route_unassigns_and_removes(self, world, monkeypatch):
        from starlette.requests import Request

        from routers import skills as skills_router

        _held(world, "research")
        request = Request({"type": "http", "method": "DELETE",
                           "path": f"/api/agents/{LEAD}/skills/research",
                           "headers": [], "client": ("127.0.0.1", 1), "query_string": b""})
        body = asyncio.run(skills_router.unassign_skill(
            "research", request, agent_name=LEAD, current_user=world.user(W.ALICE)))
        assert body["removed"] is True and body["removal"]["status"] == "completed"
        assert "research" not in db.get_agent_skill_names(LEAD)
        assert world.removed == [(LEAD, ("research",))]

    def test_the_route_still_maps_an_unresolved_set_to_409(self, world, monkeypatch):
        from fastapi import HTTPException
        from starlette.requests import Request

        from routers import skills as skills_router

        world.seed_agent(LEAD)
        db.assign_skill(LEAD, "research", W.ALICE)
        db.set_skill_individual(LEAD, "research", False)
        monkeypatch.setattr(skill_set_service, "any_unresolved", lambda _a: True)
        request = Request({"type": "http", "method": "DELETE", "path": "/x",
                           "headers": [], "client": ("127.0.0.1", 1), "query_string": b""})
        with pytest.raises(HTTPException) as exc:
            asyncio.run(skills_router.unassign_skill(
                "research", request, agent_name=LEAD, current_user=world.user(W.ALICE)))
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "skill_set_unresolved"
        assert "research" in db.get_agent_skill_names(LEAD)
