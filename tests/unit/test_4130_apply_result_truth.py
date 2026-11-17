"""#4130: apply and deploy results say only what was observed or done.

A result names only what a run observed or did. These tests drive the real
apply engine (`system_service.apply_manifest`, the function the route calls)
over the REAL `db` on a full schema; Docker and skill delivery are faked at
their seams (`_ent695_world.py`). One test per corrected statement:

* `adopted[]` and the adoption line: the plan on a dry run (valid or
  invalid), nothing on a refused run, and on a real run only the members
  whose SYSTEM tag landed.
* `summary.<group>.write` on a real run counts only the writes that landed;
  a refused run counts none. `report` / `unobserved` stay plan counts.
* A declared-only member is never re-read after the writes, and its
  `applied` is None: nothing was planned or attempted for it.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest
import yaml

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
    skill_assignment_service, skill_gate_map_service, skill_set_service, system_service,
)
from services.skill_service import skill_service  # noqa: E402

pytestmark = pytest.mark.unit

SYSTEM = W.SYSTEM
LIBRARY = {"research", "deploy", "notes"}
SETS = {
    "ops-core": {"name": "ops-core", "source_id": "src-1", "source_name": "default",
                 "shadowed_by": [], "status": "ok", "problems": [],
                 "members": [{"name": "research", "present": True},
                             {"name": "deploy", "present": True}],
                 "requires": {"env": []}, "schedules": []},
}


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
    w.delivery = None  # a test may set the report the delivery seam answers
    _patch_skill_service(monkeypatch, "get_skill", lambda n: {"name": n} if n in LIBRARY else None)
    _patch_skill_service(monkeypatch, "list_skills", lambda: [])
    monkeypatch.setattr(skill_set_service, "library_sets", lambda **_k: dict(SETS))

    async def deliver(agent, names):
        w.calls.append(("deliver", agent, tuple(names)))
        if w.delivery is not None:
            return w.delivery
        return {"status": "pending_start", "skills": {n: {"status": "pending_start"} for n in names}}

    async def gates(agent, actor, request=None, **_k):
        return None

    _patch_skill_service(monkeypatch, "deliver_assigned", deliver)
    monkeypatch.setattr(skill_assignment_service, "sync_gates", gates)
    W.install_executor_seams(w, monkeypatch)
    return w


def _manifest(agents: dict, *, naming=None, **top) -> str:
    doc = {"name": SYSTEM}
    if naming:
        doc["naming"] = naming
    doc["agents"] = {k: {"template": "local:plain", **v} for k, v in agents.items()}
    doc.update(top)
    return yaml.safe_dump(doc, sort_keys=False)


def _apply(world, text, *, dry_run):
    return W.apply(world, text, dry_run=dry_run)


def _member(res, name):
    return next(m for m in res.members if m.name == name)


def _adoption_lines(res):
    return [w for w in res.warnings if "adopted into" in w]


# ===========================================================================
# S3 — adopted[] and its line
# ===========================================================================

class TestAdoptedSaysWhatHappened:
    def _refusable(self, world):
        """An adoptable bare member beside a member whose skill is unknown."""
        world.seed_agent("scout-lead", tags=())
        return _manifest({"scout-lead": {}, "broken": {"skills": ["nope"]}}, naming="bare")

    def test_a_refused_run_adopts_nobody_and_says_nothing_about_adoption(self, world):
        res = _apply(world, self._refusable(world), dry_run=False)
        assert res.status == "refused"
        assert res.adopted == []
        assert _adoption_lines(res) == []
        # The member's own change still names the plan.
        assert [c.code for c in _member(res, "scout-lead").changes] == ["member_adopted"]
        assert SYSTEM not in (db.get_agent_tags("scout-lead") or [])

    def test_an_invalid_dry_run_still_shows_the_plan(self, world):
        res = _apply(world, self._refusable(world), dry_run=True)
        assert res.status == "invalid"
        assert [a.name for a in res.adopted] == ["scout-lead"]
        (line,) = _adoption_lines(res)
        assert "will be adopted" in line

    def test_a_real_run_lists_only_members_whose_system_tag_landed(self, world, monkeypatch):
        world.seed_agent("scout-a", tags=())
        world.seed_agent("scout-b", tags=())
        real_add = db.add_agent_tag

        def add(name, tag):
            if name == "scout-b":
                raise RuntimeError("db locked")
            return real_add(name, tag)
        monkeypatch.setattr(db, "add_agent_tag", add)
        res = _apply(world, _manifest({"scout-a": {}, "scout-b": {}}, naming="bare"), dry_run=False)
        assert res.status == "partial"
        assert [a.name for a in res.adopted] == ["scout-a"]
        (line,) = _adoption_lines(res)
        assert "was adopted" in line and "scout-a" in line and "scout-b" not in line
        assert [(f.name, f.step) for f in res.failed] == [("scout-b", "tag")]

    def test_a_default_tag_that_landed_is_not_an_adoption(self, world, monkeypatch):
        world.seed_agent("scout-a", tags=())
        real_add = db.add_agent_tag

        def add(name, tag):
            if tag == SYSTEM:
                raise RuntimeError("db locked")
            return real_add(name, tag)
        monkeypatch.setattr(db, "add_agent_tag", add)
        res = _apply(world, _manifest({"scout-a": {}}, naming="bare", default_tags=["fleet"]),
                     dry_run=False)
        assert "fleet" in db.get_agent_tags("scout-a")
        assert res.adopted == []
        assert _adoption_lines(res) == []

    def test_a_clean_real_run_says_it_in_the_past_tense(self, world):
        world.seed_agent("scout-a", tags=())
        res = _apply(world, _manifest({"scout-a": {}}, naming="bare"), dry_run=False)
        assert res.status == "applied"
        assert [a.name for a in res.adopted] == ["scout-a"]
        (line,) = _adoption_lines(res)
        assert "1 agent was adopted" in line and "will be" not in line


# ===========================================================================
# S4 / S11 — summary.write counts what landed on a real run
# ===========================================================================

class TestSummaryCountsLandedWrites:
    def test_a_writer_that_raised_is_not_counted(self, world, monkeypatch):
        world.seed_agent(f"{SYSTEM}-a")
        world.seed_agent(f"{SYSTEM}-b")
        real = skill_assignment_service.assign_and_deliver

        async def assign(name, names, actor, **kw):
            if name == f"{SYSTEM}-b":
                raise RuntimeError("db locked")
            return await real(name, names, actor, **kw)
        monkeypatch.setattr(skill_assignment_service, "assign_and_deliver", assign)
        text = _manifest({"a": {"skills": ["research"]}, "b": {"skills": ["research"]}})
        assert _apply(world, text, dry_run=True).summary["skills"]["write"] == 2
        res = _apply(world, text, dry_run=False)
        assert res.status == "partial"
        assert res.summary["skills"]["write"] == 1
        assert res.summary["members"]["write"] == 1

    def test_a_skill_held_back_for_its_gate_is_not_counted(self, world, monkeypatch):
        world.seed_agent(f"{SYSTEM}-lead")

        async def boom(*_a, **_k):
            raise RuntimeError("db locked")
        monkeypatch.setattr(skill_gate_map_service, "set_gate", boom)
        text = _manifest({"lead": {"skills": ["deploy"], "gates": {"deploy": {}}}})
        dry = _apply(world, text, dry_run=True)
        assert dry.summary["skills"]["write"] == 1 and dry.summary["gates"]["write"] == 1
        res = _apply(world, text, dry_run=False)
        assert any(f.step == "skills" and f.reason.startswith("gate_not_written") for f in res.failed)
        assert res.summary["skills"]["write"] == 0
        assert res.summary["gates"]["write"] == 0
        assert res.summary["members"]["write"] == 0

    def test_a_refused_run_counts_no_write_and_keeps_the_reports(self, world):
        world.seed_agent(f"{SYSTEM}-lead")
        text = _manifest({"lead": {"skills": ["research"]}, "broken": {"skills": ["nope"]},
                          "canon": {"deploy": False}})
        dry = _apply(world, text, dry_run=True)
        assert dry.summary["skills"]["write"] == 1 and dry.summary["members"]["report"] == 1
        res = _apply(world, text, dry_run=False)
        assert res.status == "refused"
        assert all(counts["write"] == 0 for counts in res.summary.values())
        assert res.summary["members"]["report"] == 1

    def test_a_run_where_everything_landed_reports_the_plan_counts(self, world):
        world.seed_agent(f"{SYSTEM}-lead")
        text = _manifest({"lead": {"skills": ["research"]}, "new": {"skills": ["notes"]}})
        dry = _apply(world, text, dry_run=True)
        res = _apply(world, text, dry_run=False)
        assert res.status == "applied"
        assert res.summary == dry.summary
        assert res.summary["skills"]["write"] == 2 and res.summary["members"]["write"] == 2


def _apply_route(world, monkeypatch, text):
    """A real `POST /api/systems/apply` handler call on a real run: the route
    runs the real apply over the real `db`; only create/start are the world's."""
    from starlette.requests import Request

    from models import SystemApplyRequest
    from routers import systems

    real = systems.apply_manifest

    async def apply_with_seams(manifest, user, request, *, dry_run):
        return await real(manifest, user, request, dry_run=dry_run,
                          create_agent_fn=world.create, start_agent_fn=world.docker.start)

    monkeypatch.setattr(systems, "apply_manifest", apply_with_seams)
    request = Request({"type": "http", "method": "POST", "path": "/api/systems/apply",
                       "headers": [], "client": ("127.0.0.1", 1), "query_string": b""})
    return asyncio.run(systems.apply_system(
        SystemApplyRequest(manifest=text, dry_run=False), request,
        current_user=world.user(W.ALICE)))


class TestSummaryCountsEachWriteByItsOwnKey:
    """Two writes of one kind on one member, one of which did not land: the
    count is 1, not 2, and `failed[]` names the one that did not land."""

    def test_two_sets_on_one_member_one_held_for_its_gate(self, world, monkeypatch):
        lead = f"{SYSTEM}-lead"
        world.seed_agent(lead)
        sets = dict(SETS, **{"notes-kit": {
            **SETS["ops-core"], "name": "notes-kit",
            "members": [{"name": "notes", "present": True}]}})
        monkeypatch.setattr(skill_set_service, "library_sets", lambda **_k: dict(sets))

        async def boom(*_a, **_k):
            raise RuntimeError("db locked")
        monkeypatch.setattr(skill_gate_map_service, "set_gate", boom)
        text = _manifest({"lead": {"skill_sets": ["ops-core", "notes-kit"],
                                   "gates": {"deploy": {}}}})
        assert _apply(world, text, dry_run=True).summary["skills"]["write"] == 2
        res = _apply_route(world, monkeypatch, text)
        assert res.status == "partial"
        assert list(skill_set_service.held_sets(lead)) == ["notes-kit"]  # one landed
        assert res.summary["skills"]["write"] == 1
        held = [f for f in res.failed if f.step == "skill_sets"]
        assert [(f.name, f.key) for f in held] == [(lead, "ops-core")]
        assert held[0].reason.startswith("gate_not_written")

    def test_two_gates_on_one_member_one_write_failing(self, world, monkeypatch):
        lead = f"{SYSTEM}-lead"
        world.seed_agent(lead)
        real = skill_gate_map_service.set_gate

        async def set_gate(agent_name, skill_name, **kw):
            if skill_name == "deploy":
                raise RuntimeError("db locked")
            return await real(agent_name, skill_name, **kw)
        monkeypatch.setattr(skill_gate_map_service, "set_gate", set_gate)
        text = _manifest({"lead": {"gates": {"deploy": {}, "research": {}}}})
        assert _apply(world, text, dry_run=True).summary["gates"]["write"] == 2
        res = _apply_route(world, monkeypatch, text)
        assert res.status == "partial"
        assert res.summary["gates"]["write"] == 1
        assert [(f.name, f.step, f.key) for f in res.failed] == [(lead, "gates", "deploy")]


# ===========================================================================
# S5 / S6 / S12 — a declared-only member is neither re-read nor "applied"
# ===========================================================================

def _record_post_write_reads(monkeypatch):
    """Names whose container `execute_apply_plan` reads after the writes."""
    seen: list = []
    active = {"on": False}
    real_container = system_service._member_container
    real_execute = system_service.execute_apply_plan

    def container(name):
        if active["on"]:
            seen.append(name)
        return real_container(name)

    async def execute(*a, **k):
        active["on"] = True
        try:
            return await real_execute(*a, **k)
        finally:
            active["on"] = False
    monkeypatch.setattr(system_service, "_member_container", container)
    monkeypatch.setattr(system_service, "execute_apply_plan", execute)
    return seen


class TestDeclaredOnlyIsNotTouched:
    def _text(self):
        return _manifest({"lead": {"skills": ["research"]}, "canon": {"deploy": False}})

    def test_an_absent_declared_only_member_is_not_re_read(self, world, monkeypatch):
        world.seed_agent(f"{SYSTEM}-lead")
        seen = _record_post_write_reads(monkeypatch)
        res = _apply(world, self._text(), dry_run=False)
        assert res.status == "applied"
        assert not [w for w in res.warnings if "could not be re-read" in w]
        assert seen == [f"{SYSTEM}-lead"]
        assert _member(res, f"{SYSTEM}-canon").restart_required is False

    def test_a_live_declared_only_member_is_not_re_read(self, world, monkeypatch):
        world.seed_agent(f"{SYSTEM}-lead")
        world.seed_agent(f"{SYSTEM}-canon")
        seen = _record_post_write_reads(monkeypatch)
        res = _apply(world, self._text(), dry_run=False)
        assert f"{SYSTEM}-canon" not in seen and f"{SYSTEM}-lead" in seen
        assert _member(res, f"{SYSTEM}-canon").restart_required is False

    def test_applied_is_none_for_a_declared_only_member(self, world):
        world.seed_agent(f"{SYSTEM}-lead")
        dry = _apply(world, self._text(), dry_run=True)
        assert {m.name: m.applied for m in dry.members} == {
            f"{SYSTEM}-lead": None, f"{SYSTEM}-canon": None}
        res = _apply(world, self._text(), dry_run=False)
        assert {m.name: m.applied for m in res.members} == {
            f"{SYSTEM}-lead": True, f"{SYSTEM}-canon": None}

    def test_an_all_declared_only_apply_is_applied_with_nothing_written(self, world):
        """S12 (no change): 'every attempted write succeeded, including none'."""
        text = _manifest({"canon": {"deploy": False}, "docs": {"deploy": False}})
        dry = _apply(world, text, dry_run=True)
        assert dry.status == "valid"
        res = _apply(world, text, dry_run=False)
        assert res.status == "applied"
        assert res.summary["members"] == {"write": 0, "report": 2, "unobserved": 0}
        assert not [w for w in res.warnings if "could not be re-read" in w]
        assert db.get_agent_tags(f"{SYSTEM}-canon") in (None, [])


# ===========================================================================
# S1 / S2 — deploy: nothing to deploy is a named 400; declared-only is a field
# ===========================================================================

def _deploy(world, text, *, dry_run):
    return asyncio.run(system_service.deploy_manifest(
        text, world.user(W.ALICE), None, dry_run=dry_run, create_agent_fn=world.create))


class TestDeployNamesDeclaredOnly:
    @pytest.mark.parametrize("dry_run", [True, False])
    def test_every_member_declared_only_is_a_named_400(self, world, dry_run):
        from fastapi import HTTPException
        text = _manifest({"canon": {"deploy": False}, "docs": {"deploy": False}})
        with pytest.raises(HTTPException) as e:
            _deploy(world, text, dry_run=dry_run)
        assert e.value.status_code == 400
        assert e.value.detail.startswith("nothing_to_deploy:")
        assert world.create_calls == []

    def test_the_declared_only_names_are_a_field_on_both_branches(self, world):
        text = _manifest({"lead": {}, "canon": {"deploy": False}})
        dry = _deploy(world, text, dry_run=True)
        assert dry.status == "valid" and dry.agents_declared_only == [f"{SYSTEM}-canon"]
        res = _deploy(world, text, dry_run=False)
        assert res.status == "deployed" and res.agents_declared_only == [f"{SYSTEM}-canon"]
        assert any("declared-only" in w for w in res.warnings)  # the line stays

    def test_a_manifest_without_declared_only_members_has_an_empty_list(self, world):
        assert _deploy(world, _manifest({"lead": {}}), dry_run=True).agents_declared_only == []

    def test_the_failed_branch_carries_the_declared_only_names(self, world, monkeypatch):
        world.fail_create.add(f"{SYSTEM}-lead")
        r = _deploy_route(world, monkeypatch, _manifest({"lead": {}, "canon": {"deploy": False}}))
        assert r.status_code == 500 and r.json()["status"] == "failed"
        assert r.json()["agents_declared_only"] == [f"{SYSTEM}-canon"]

    @pytest.mark.parametrize("dry_run", [True, False])
    def test_the_route_answers_nothing_to_deploy_as_a_400(self, world, monkeypatch, dry_run):
        text = _manifest({"canon": {"deploy": False}, "docs": {"deploy": False}})
        r = _deploy_route(world, monkeypatch, text, dry_run=dry_run)
        assert r.status_code == 400
        assert r.json()["detail"].startswith("nothing_to_deploy:")
        assert world.create_calls == []


def _deploy_route(world, monkeypatch, text, *, dry_run=False):
    """POST /api/systems/deploy through the real router; only the agent-create
    seam is the world's (the route passes none, so the service would reach Docker)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routers import systems

    real = systems.deploy_manifest

    async def deploy(*a, **k):
        return await real(*a, create_agent_fn=world.create, **k)
    monkeypatch.setattr(systems, "deploy_manifest", deploy)
    app = FastAPI()
    app.include_router(systems.router)
    app.dependency_overrides[systems.get_current_user] = lambda: world.user(W.ALICE)
    client = TestClient(app, raise_server_exceptions=False)
    return client.post("/api/systems/deploy", json={"manifest": text, "dry_run": dry_run})


# ===========================================================================
# S7 / S8 / S9 — skill delivery says what was (not) delivered
# ===========================================================================

MARKER = "marker_not_written: the package landed but its marker could not be written"
PARTIAL = {"status": "partial", "reason": "injection_error",
           "skills": {"research": {"status": "injected"},
                      "deploy": {"status": "failed", "error": MARKER}}}


class TestSkillDeliveryIsNamed:
    def test_apply_names_a_skill_whose_marker_was_not_written(self, world):
        world.seed_agent(f"{SYSTEM}-lead")
        world.delivery = PARTIAL
        res = _apply(world, _manifest({"lead": {"skills": ["research", "deploy"]}}), dry_run=False)
        member = _member(res, f"{SYSTEM}-lead")
        (change,) = [c for c in member.changes if c.code == "skill_delivery"]
        assert change.current == "partial"
        assert "deploy: marker_not_written" in change.note
        assert "research" not in change.note.split("(")[1].split(")")[0]
        assert member.applied is True  # the row is the write; delivery is best-effort

    def test_a_set_whose_skills_were_already_held_says_unchanged(self, world):
        lead = f"{SYSTEM}-lead"
        world.seed_agent(lead)
        db.assign_skill(lead, "research", W.ALICE)
        db.assign_skill(lead, "deploy", W.ALICE)
        res = _apply(world, _manifest({"lead": {"skill_sets": ["ops-core"]}}), dry_run=False)
        assert res.status == "applied", res.failed
        assert list(skill_set_service.held_sets(lead)) == ["ops-core"]
        (change,) = [c for c in _member(res, lead).changes if c.code == "skill_delivery"]
        assert change.field == "skill_sets"
        assert change.current == "unchanged" and change.desired == []
        assert "already held" in change.note

    def test_deploy_warns_when_a_skill_was_not_delivered(self, world):
        world.delivery = PARTIAL
        res = _deploy(world, _manifest({"lead": {"skills": ["research", "deploy"]}}), dry_run=False)
        assert res.status == "deployed"
        (line,) = [w for w in res.warnings if "not delivered" in w]
        assert line.startswith(f"Skills assigned to '{SYSTEM}-lead' but not delivered:")
        assert "deploy: marker_not_written" in line

    def test_deploy_says_nothing_for_the_normal_pending_start(self, world):
        res = _deploy(world, _manifest({"lead": {"skills": ["research"]}}), dry_run=False)
        assert res.status == "deployed"
        assert not [w for w in res.warnings if "not delivered" in w]


# ===========================================================================
# S10 — a gate whose marker was not written is said on both verbs
# ===========================================================================

class TestGateMarkerIsNamed:
    @pytest.fixture
    def marker_refused(self, monkeypatch):
        async def set_gate(agent_name, skill_name, *, changes, ctx):
            return {"gate": {"approver": None}, "warnings": ["marker_not_written"]}
        monkeypatch.setattr(skill_gate_map_service, "set_gate", set_gate)

    def test_apply_reports_it_on_the_member(self, world, marker_refused):
        world.seed_agent(f"{SYSTEM}-lead")
        res = _apply(world, _manifest({"lead": {"gates": {"deploy": {}}}}), dry_run=False)
        member = _member(res, f"{SYSTEM}-lead")
        (change,) = [c for c in member.changes if c.code == "gate_marker_not_written"]
        assert change.action == "report" and change.desired == ["deploy"]
        assert "fails open" in change.note
        assert member.applied is True
        assert res.summary["gates"]["write"] == 1

    def test_deploy_warns(self, world, marker_refused):
        res = _deploy(world, _manifest({"lead": {"gates": {"deploy": {}}}}), dry_run=False)
        (line,) = [w for w in res.warnings if "marker_not_written" in w]
        assert "'deploy'" in line and f"'{SYSTEM}-lead'" in line
