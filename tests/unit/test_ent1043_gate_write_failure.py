"""trinity-enterprise#1043: a gate write that fails on a skill the member already holds.

A manifest member may declare `gates: {<skill>: {...}}` on a skill it already
holds. Gates are written before skills (ent#753), and a NEW assignment whose
gate did not land is held back (#4125) — but a skill the member already holds
produces no skill op, so nothing holds it: when its gate write fails after the
plan validated (a service refusal, a database fault, a lost lease), the skill
stays assigned and keeps running with no approval gate, or under the previous
gate, and before this fix the result only said "the gate failed".

The additive contract stands (apply never unassigns): the fix is REPORTING.
When a gate write for a held skill fails, the `failed[]` reason carries the
consequence, the member gets a `report` change — `skill_runs_ungated` (no
gate in force) or `previous_gate_stands` (a stored gate still applies) — one
top-level warning line per ungated skill follows the held-back grant and seat
lines, and the member's `applied` is false. "Held" is decided at plan time
from the snapshot read under the lock (`ApplyMemberState.skill_names`).

Runs over the REAL `db` on a full schema through the real apply route
(`routers.systems.apply_system`); Docker is faked at its read seams
(`_ent695_world.py`) plus the two the gate map touches (the single-agent
container state and the marker exec). The admission read asserted here is
`skill_gate_service.read_gates`, the one every entry point enforces on.
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
from services.skill_service import skill_service  # noqa: E402

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
UNGATED = "skill_runs_ungated"
PREVIOUS = "previous_gate_stands"


def _patch_skill_service(monkeypatch, attr, value):
    """Patch every live `skill_service` object (a second import of the module
    in the same process leaves two singletons)."""
    live = importlib.import_module("services.skill_service").skill_service
    for obj in {id(o): o for o in (skill_service, live)}.values():
        monkeypatch.setattr(obj, attr, value)


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    w = W.make_world(monkeypatch, tmp_path)
    w.calls = []
    w.marker = []

    async def log(**kw):
        pass

    audit_mod = importlib.import_module("services.platform_audit_service")
    monkeypatch.setattr(audit_mod.platform_audit_service, "log", log)

    # The gate map's two Docker reads: the single-agent state and the marker exec.
    async def state(name):
        return w.docker.states().get(name, "missing")

    async def exec_(container_name, command, timeout=60, *, environment=None, user="developer"):
        w.marker.append(container_name)
        return {"exit_code": 0, "output": "", "timed_out": False}

    DS = importlib.import_module("services.docker_service")
    monkeypatch.setattr(docker_utils, "agent_container_state_async", state)
    monkeypatch.setattr(DS, "execute_command_in_container", exec_)

    _patch_skill_service(monkeypatch, "get_skill",
                         lambda n: {"name": n} if n in LIBRARY else None)
    _patch_skill_service(monkeypatch, "list_skills", lambda: [])
    monkeypatch.setattr(skill_set_service, "library_sets", lambda **_k: dict(SETS))

    async def deliver(agent, names):
        w.calls.append(("deliver", agent, tuple(names)))
        return {"status": "pending_start", "skills": {n: {"status": "pending_start"} for n in names}}

    async def gates(agent, actor, request=None, **_k):
        return None

    _patch_skill_service(monkeypatch, "deliver_assigned", deliver)
    monkeypatch.setattr(skill_assignment_service, "sync_gates", gates)
    W.install_executor_seams(w, monkeypatch)
    assignment_provider.clear_provider()
    yield w
    assignment_provider.clear_provider()


def _manifest(gates, *, skills=None, skill_sets=None, tags=("x",)) -> str:
    lines = [f"name: {SYSTEM}", "agents:", "  lead:", "    template: local:plain", "    gates:"]
    for skill, spec in gates.items():
        if spec:
            lines.append(f"      {skill}:")
            lines.extend(f"        {k}: {v}" for k, v in spec.items())
        else:
            lines.append(f"      {skill}: {{}}")
    if skills is not None:
        lines.append(f"    skills: [{', '.join(skills)}]")
    if skill_sets is not None:
        lines.append(f"    skill_sets: [{', '.join(skill_sets)}]")
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


def _member(res, name=LEAD):
    return next(m for m in res.members if m.name == name)


def _reports(member, code):
    return [c for c in member.changes if c.field == "gates" and c.action == "report"
            and c.code == code]


def _gate_failures(res):
    return [f for f in res.failed if f.name == LEAD and f.step == "gates"]


def _ungated_lines(res, skill="research"):
    return [w for w in res.warnings if f"'{LEAD}'" in w and f"'{skill}'" in w
            and "no approval gate" in w]


def _set_gate(name, skill, **changes):
    """A stored explicit gate, through the real writer (the world fakes Docker)."""
    return asyncio.run(skill_gate_map_service.set_gate(
        name, skill, changes=changes,
        ctx=skill_gate_map_service.GateContext(actor=None, via="system", trigger="direct")))


# --- the three ways a validated gate write fails ----------------------------

def _refuse_after_validation(monkeypatch):
    """F1: the service refuses after the plan validated (the agent was renamed
    or deleted between plan and write)."""
    async def refusing(agent_name, skill_name, *, changes, ctx):
        raise SkillGateMapRefused(404, "agent_not_found", "Agent not found")
    monkeypatch.setattr(skill_gate_map_service, "set_gate", refusing)
    return "agent_not_found"


def _db_fault(monkeypatch):
    """F2: the facade's own gate writer raises, so the real `set_gate`, its
    marker lock and its ordering run up to the fault."""
    def boom(*_a, **_k):
        raise RuntimeError("disk I/O error")
    monkeypatch.setattr(db, "write_skill_gate", boom)
    return "disk I/O error"


def _lose_lease_after_tag(monkeypatch):
    """F3: the apply lease is lost once the tag write (the phase before gates)
    has landed, so the gate write is reported `lock_lost`."""
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
    pytest.param(_lose_lease_after_tag, id="lost_lease"),
]


def _assert_still_assigned_and_ungated(skill="research"):
    """The state the result must describe: the skill is still assigned, and the
    admission read (what every entry point enforces on) shows no gate for it."""
    assert skill in db.get_agent_skill_names(LEAD)
    assert skill not in skill_gate_service.read_gates(LEAD)


def _assert_result_says_ungated(res, code, skill="research"):
    member = _member(res)
    assert res.status == "partial"
    assert member.applied is False
    failures = _gate_failures(res)
    assert len(failures) == 1, res.failed
    reason = failures[0].reason
    assert reason.startswith(code), reason
    assert f"'{skill}' stays assigned and runs with NO approval gate" in reason, reason
    reports = _reports(member, UNGATED)
    assert len(reports) == 1, [c.code for c in member.changes]
    assert reports[0].desired == [skill] and reports[0].current is None
    assert reports[0].note and "re-apply" in reports[0].note
    assert _reports(member, PREVIOUS) == []
    lines = _ungated_lines(res, skill)
    assert len(lines) == 1, res.warnings
    assert code in lines[0]
    assert res.summary["gates"]["write"] == 0


class TestHeldSkillRunsUngated:
    """CP1: each failure path, through the real route, on a skill the member
    already holds and that carries no gate yet."""

    @pytest.mark.parametrize("fail", FAILURES)
    def test_a_failed_gate_write_on_a_held_skill_is_named_as_ungated(
            self, world, monkeypatch, fail):
        world.seed_agent(LEAD)
        db.assign_skill(LEAD, "research", W.ALICE)
        code = fail(monkeypatch)
        res = _apply(world, monkeypatch, _manifest({"research": {}}))
        _assert_still_assigned_and_ungated()
        _assert_result_says_ungated(res, code)
        # The write that preceded the gate landed: the run went on (P-C).
        assert "x" in db.get_agent_tags(LEAD)

    def test_the_consequence_is_appended_to_the_services_own_reason(self, world, monkeypatch):
        """The `<code>: <message>` prefix stays (consumers split on it); the
        consequence is one clause after it, never a second `failed[]` row."""
        world.seed_agent(LEAD)
        db.assign_skill(LEAD, "research", W.ALICE)
        _refuse_after_validation(monkeypatch)
        res = _apply(world, monkeypatch, _manifest({"research": {}}))
        [failure] = _gate_failures(res)
        assert failure.reason.split(";")[0] == "agent_not_found: Agent not found"
        assert len([f for f in res.failed if f.name == LEAD]) == 1

