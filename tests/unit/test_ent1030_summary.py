"""trinity-enterprise#1030 CP6: the per-group summary and `unobserved`.

`SystemApplyResponse.summary` carries one count per config group — `skills`,
`grants`, `gates`, `roles` — and one for `members`, each `{write, report,
unobserved}`; `grants` also keeps `held_back`. Each member lists the groups
the platform could not observe (`unobserved`): a seam answer, never "in sync"
and never a blocker. A READ ERROR is not unobserved — it stays the retryable
`member_reads_unavailable` blocker, and a blocked member is counted nowhere.

Also here: one manifest exercising every group in one apply under three
callers (S8), two bare manifests declaring one agent (S6: the last writer
adds, nobody removes), and the deploy preview's per-agent `configured` counts.

Runs over the REAL `db` on a full schema; Docker is faked at its read seams
(`_ent695_world.py`) plus the two the gate map touches.
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
from services import (  # noqa: E402
    assignment_provider, docker_utils, skill_assignment_service, system_service,
)
from services.skill_service import skill_service  # noqa: E402
from services.system_apply import skills as apply_skills  # noqa: E402

pytestmark = pytest.mark.unit

SYSTEM = W.SYSTEM
LEAD, CANON = f"{SYSTEM}-lead", f"{SYSTEM}-canon"
GROUPS = ("skills", "grants", "gates", "roles", "members")
LIBRARY = {"research", "deploy"}


def _patch_skill_service(monkeypatch, attr, value):
    """Patch `attr` on every live `skill_service` object, not only the one this
    file bound at import. `system_apply.skills` binds the singleton at import,
    but `skill_gate_map_service._recommended` imports it at call time from
    `sys.modules` - and a test file that loads `services.skill_service` a second
    time (`test_skill_service_user_agent`) leaves the two pointing at different
    objects, so a patch on one alone depends on which files share the process."""
    live = importlib.import_module("services.skill_service").skill_service
    for obj in {id(o): o for o in (skill_service, live)}.values():
        monkeypatch.setattr(obj, attr, value)


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    w = W.make_world(monkeypatch, tmp_path)

    async def log(**_kw):
        return None

    audit_mod = importlib.import_module("services.platform_audit_service")
    monkeypatch.setattr(audit_mod.platform_audit_service, "log", log)

    async def state(name):
        return w.docker.states().get(name, "missing")

    async def exec_(container_name, command, timeout=60, *, environment=None, user="developer"):
        return {"exit_code": 0, "output": "", "timed_out": False}

    DS = importlib.import_module("services.docker_service")
    monkeypatch.setattr(docker_utils, "agent_container_state_async", state)
    monkeypatch.setattr(DS, "execute_command_in_container", exec_)
    _patch_skill_service(monkeypatch, "get_skill", lambda n: {"name": n} if n in LIBRARY else None)
    _patch_skill_service(monkeypatch, "list_skills", lambda: [])

    async def deliver(agent, names):
        return {"status": "pending_start", "skills": {n: {"status": "pending_start"} for n in names}}

    async def gates(agent, actor, request=None, **_k):
        return None

    _patch_skill_service(monkeypatch, "deliver_assigned", deliver)
    monkeypatch.setattr(skill_assignment_service, "sync_gates", gates)
    W.install_executor_seams(w, monkeypatch)
    assignment_provider.clear_provider()
    yield w
    assignment_provider.clear_provider()


FULL = f"""name: {SYSTEM}
agents:
  lead:
    template: local:plain
    skills: [research]
    gates:
      deploy: {{approver: primary}}
    grants: [agents.manage]
    seat: lead-role
    person_seats: [{{role: fleet-owner, kind: approver}}]
  canon:
    template: local:plain
    deploy: false
"""


def _apply(world, text, user, *, dry_run):
    return asyncio.run(system_service.apply_manifest(
        text, user, None, dry_run=dry_run,
        create_agent_fn=world.create, start_agent_fn=world.docker.start))


def _member(res, name=LEAD):
    return next(m for m in res.members if m.name == name)


def _counts(res):
    return {g: {k: res.summary[g][k] for k in ("write", "report", "unobserved")} for g in GROUPS}


# ===========================================================================
# Shape
# ===========================================================================

class TestShape:
    def test_every_group_has_write_report_unobserved(self, world):
        world.seed_agent(LEAD)
        res = _apply(world, f"name: {SYSTEM}\nagents:\n  lead:\n    template: local:plain\n",
                     world.user(W.ALICE), dry_run=True)
        assert set(res.summary) == set(GROUPS)
        assert _counts(res) == {g: {"write": 0, "report": 0, "unobserved": 0} for g in GROUPS}
        assert res.summary["grants"]["held_back"] == []
        assert _member(res).unobserved == []


# ===========================================================================
# The five groups in one apply, three callers (S8)
# ===========================================================================

class TestMatrix:
    def test_an_admin_in_a_session_writes_every_writable_group(self, world):
        world.seed_agent(LEAD, owner=W.ADMIN)
        res = _apply(world, FULL, world.user(W.ADMIN), dry_run=True)
        assert res.blockers == []
        assert _counts(res) == {
            "skills": {"write": 1, "report": 0, "unobserved": 0},
            "grants": {"write": 1, "report": 0, "unobserved": 0},
            "gates": {"write": 1, "report": 0, "unobserved": 0},
            "roles": {"write": 0, "report": 0, "unobserved": 2},
            "members": {"write": 1, "report": 1, "unobserved": 1},
        }
        assert _member(res).unobserved == ["roles"]
        assert _member(res, CANON).verdict == "declared_only"
        assert _member(res, CANON).unobserved == []

    def test_a_non_admin_creator_holds_the_grant_back_and_writes_the_rest(self, world):
        world.seed_agent(LEAD)
        res = _apply(world, FULL, world.user(W.ALICE), dry_run=True)
        counts = _counts(res)
        assert counts["grants"] == {"write": 0, "report": 1, "unobserved": 0}
        assert res.summary["grants"]["held_back"] == [{"name": LEAD, "grants": ["agents.manage"]}]
        assert counts["skills"]["write"] == counts["gates"]["write"] == 1

    def test_an_admins_own_mcp_key_holds_the_grant_back(self, world):
        world.seed_agent(LEAD, owner=W.ADMIN)
        key = world.user(W.ADMIN).model_copy(update={"mcp_scope": "user"})
        res = _apply(world, FULL, key, dry_run=True)
        assert _counts(res)["grants"] == {"write": 0, "report": 1, "unobserved": 0}

    def test_the_real_run_reports_the_same_counts(self, world):
        world.seed_agent(LEAD, owner=W.ADMIN)
        dry = _apply(world, FULL, world.user(W.ADMIN), dry_run=True)
        real = _apply(world, FULL, world.user(W.ADMIN), dry_run=False)
        assert real.status == "applied", real.failed
        assert _counts(real) == _counts(dry)
        again = _apply(world, FULL, world.user(W.ADMIN), dry_run=True)
        assert {g: again.summary[g]["write"] for g in GROUPS} == {
            "skills": 0, "grants": 0, "gates": 0, "roles": 0, "members": 0}


# ===========================================================================
# Unobserved is a seam answer; a read error is the retryable blocker
# ===========================================================================

class TestUnobservedIsNotAReadError:
    def test_a_person_seat_read_error_is_a_blocker_not_unobserved(self, world):
        class Boom:
            def assignment_for(self, *a):
                return None

            def person_seat_status(self, *a):
                raise RuntimeError("down")
        assignment_provider.register_provider(Boom())
        world.seed_agent(LEAD)
        res = _apply(world, f"name: {SYSTEM}\nagents:\n  lead:\n    template: local:plain\n"
                     "    person_seats: [{role: fleet-owner, kind: approver}]\n",
                     world.user(W.ALICE), dry_run=True)
        assert [(b.code, b.retryable) for b in res.blockers] == [("member_reads_unavailable", True)]
        assert _member(res).verdict == "blocked" and _member(res).unobserved == []
        assert res.summary["roles"]["unobserved"] == 0
        assert res.summary["members"]["unobserved"] == 0

    def test_a_skills_read_error_is_a_blocker_not_unobserved(self, world, monkeypatch):
        def boom(_name):
            raise RuntimeError("db locked")
        monkeypatch.setattr(apply_skills, "read_member", boom)
        world.seed_agent(LEAD)
        res = _apply(world, f"name: {SYSTEM}\nagents:\n  lead:\n    template: local:plain\n"
                     "    skills: [research]\n", world.user(W.ALICE), dry_run=True)
        assert [b.code for b in res.blockers] == ["member_reads_unavailable"]
        assert res.summary["skills"] == {"write": 0, "report": 0, "unobserved": 0}


    def test_a_blocked_member_is_counted_in_no_group(self, world):
        """A plan-time blocker (an invalid gate) leaves the member's other
        planned writes in its changes; nothing about it runs, so none count."""
        world.seed_agent(LEAD)
        res = _apply(world, f"name: {SYSTEM}\nagents:\n  lead:\n    template: local:plain\n"
                     "    skills: [research]\n    gates:\n      deploy: {approver: boss}\n",
                     world.user(W.ALICE), dry_run=True)
        assert _member(res).verdict == "blocked"
        assert any(c.action == "write" for c in _member(res).changes)
        assert _counts(res) == {g: {"write": 0, "report": 0, "unobserved": 0} for g in GROUPS}


# ===========================================================================
# Two bare manifests on one agent (S6): the last writer adds, nobody removes
# ===========================================================================

def _bare(system, gate):
    return (f"name: {system}\nnaming: bare\nagents:\n  shared-lead:\n    template: local:plain\n"
            f"    gates:\n      {gate}: {{approver: primary}}\n")


class TestTwoBareManifests:
    def test_the_second_adds_its_gate_and_reports_the_first(self, world):
        world.seed_agent("shared-lead", tags=())
        first = _apply(world, _bare("sysa", "deploy"), world.user(W.ALICE), dry_run=False)
        assert first.status == "applied", first.failed
        dry = _apply(world, _bare("sysb", "research"), world.user(W.ALICE), dry_run=True)
        codes = [(c.code, c.action) for c in _member(dry, "shared-lead").changes
                 if c.field == "gates"]
        assert ("gate_missing", "write") in codes and ("gate_undeclared", "report") in codes
        second = _apply(world, _bare("sysb", "research"), world.user(W.ALICE), dry_run=False)
        assert second.status == "applied", second.failed
        rows = {r["skill_name"]: r["origin"] for r in db.list_agent_skill_gates("shared-lead")}
        assert rows == {"deploy": "set", "research": "set"}


# ===========================================================================
# The deploy preview's per-agent counts (the one preview line)
# ===========================================================================

class TestDeployPreview:
    def test_counts_are_present_only_for_a_member_that_declares_a_group(self, world):
        res = asyncio.run(system_service.deploy_manifest(
            FULL.replace("  canon:\n    template: local:plain\n    deploy: false\n",
                         "  plain:\n    template: local:plain\n"),
            world.user(W.ALICE), None, dry_run=True, create_agent_fn=world.create))
        by_name = {a["short_name"]: a for a in res.agents_to_create}
        assert by_name["lead"]["configured"] == {
            "skills": 1, "gates": 1, "grants": 1, "seat": 1, "person_seats": 1}
        assert "configured" not in by_name["plain"]
