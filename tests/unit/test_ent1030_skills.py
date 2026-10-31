"""trinity-enterprise#1030 CP1: a manifest member's `skills:` and `skill_sets:`.

Additive like tags: a declared library skill or set the member does not hold
is assigned through the same sequence as the assign routes (row, gates,
delivery, broadcast); one it holds that the manifest does not name is
reported and never unassigned. A declared name the library cannot assign is
a blocker, with the route's own refusal.

Runs over the REAL `db` on a full schema; Docker is faked at its read seams
(`_ent695_world.py`). Faked here: the library (which skills and sets exist),
package delivery, and the gate sync — the last is ent#753's own subject
(`test_ent753_assignment_hooks.py`); here only its ORDER is asserted.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

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
from services import skill_assignment_service, skill_set_service, system_service  # noqa: E402
from services.skill_service import skill_service  # noqa: E402

pytestmark = pytest.mark.unit

SYSTEM = W.SYSTEM
LIBRARY = {"research", "deploy", "notes", "triage", "billing"}
SETS = {
    "ops-core": {"name": "ops-core", "source_id": "src-1", "source_name": "default",
                 "shadowed_by": [], "status": "ok", "problems": [],
                 "members": [{"name": "triage", "present": True},
                             {"name": "billing", "present": True}],
                 "requires": {"env": []}, "schedules": []},
}


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    w = W.make_world(monkeypatch, tmp_path)
    w.calls = []
    monkeypatch.setattr(skill_service, "get_skill",
                        lambda n: {"name": n} if n in LIBRARY else None)
    monkeypatch.setattr(skill_set_service, "library_sets", lambda **_k: dict(SETS))

    async def deliver(agent, names):
        w.calls.append(("deliver", agent, tuple(names)))
        return {"status": "pending_start",
                "skills": {n: {"status": "pending_start"} for n in names}}

    async def gates(agent, actor, request=None, **_k):
        w.calls.append(("gates", agent))
        return None

    monkeypatch.setattr(skill_service, "deliver_assigned", deliver)
    monkeypatch.setattr(skill_assignment_service, "sync_gates", gates)
    W.install_executor_seams(w, monkeypatch)  # apply's dry run takes the create seam too
    return w


@pytest.fixture
def ew(world):
    start = world.docker.start

    async def recorded_start(name):
        world.calls.append(("start", name))
        return await start(name)

    world.docker.start = recorded_start
    return world


def _manifest(skills=None, sets=None, *, short="lead") -> str:
    lines = [f"name: {SYSTEM}", "agents:", f"  {short}:", "    template: local:plain"]
    if skills is not None:
        lines.append(f"    skills: [{', '.join(skills)}]")
    if sets is not None:
        lines.append(f"    skill_sets: [{', '.join(sets)}]")
    return "\n".join(lines) + "\n"


LEAD = f"{SYSTEM}-lead"


def _member(res, name=LEAD):
    return next(m for m in res.members if m.name == name)


def _codes(member):
    return [(c.field, c.action, c.code) for c in member.changes]


# ===========================================================================
# Parse and validate
# ===========================================================================

class TestParse:
    def test_skills_and_sets_are_read_and_are_not_unknown_keys(self):
        m = system_service.parse_manifest(_manifest(["research", "deploy"], ["ops-core"]))
        cfg = m.agents["lead"]
        assert cfg.skills == ["research", "deploy"] and cfg.skill_sets == ["ops-core"]
        assert m.unknown_agent_keys == {}

    @pytest.mark.parametrize("value", ["research", "{a: 1}", "[1, 2]"])
    def test_a_value_that_is_not_a_list_of_names_is_a_named_refusal(self, value):
        text = f"name: {SYSTEM}\nagents:\n  lead:\n    template: local:plain\n    skills: {value}\n"
        with pytest.raises(ValueError, match="Agent 'lead' skills must be a list of names"):
            system_service.parse_manifest(text)

    def test_the_recognised_key_text_stays_the_create_mode_text(self):
        text = _manifest(["research"]).replace("skills:", "credentials: {}\n    skills:")
        warnings = system_service.validate_manifest(system_service.parse_manifest(text))
        assert warnings == ["Agent 'lead': ignored unknown key(s): credentials. Recognised "
                            "per-agent keys are: folders, kind, resources, schedules, tags, "
                            "template."]

    def test_both_fields_are_classified_reconciled(self):
        table = system_service.APPLY_FIELD_CLASSIFICATION
        assert table["SystemAgentConfig.skills"][0] == "reconciled"
        assert table["SystemAgentConfig.skill_sets"][0] == "reconciled"


# ===========================================================================
# Plan (dry run) on an existing member
# ===========================================================================

class TestPlan:
    def test_a_missing_skill_is_a_write_and_an_undeclared_one_a_report(self, world):
        world.seed_agent(LEAD)
        db.assign_skill(LEAD, "notes", W.ALICE)
        res = W.apply(world, _manifest(["research", "notes"]), dry_run=True)
        lead = _member(res)
        assert res.status == "valid" and lead.verdict == "update"
        (missing,) = [c for c in lead.changes if c.code == "skill_missing"]
        assert missing.action == "write" and missing.desired == ["research"]
        assert missing.current == ["notes"]
        assert not [c for c in lead.changes if c.code == "skill_extra"]

        res = W.apply(world, _manifest(["research"]), dry_run=True)
        (extra,) = [c for c in _member(res).changes if c.code == "skill_extra"]
        assert extra.action == "report" and extra.current == ["notes"]

    def test_a_member_holding_every_declared_skill_is_unchanged(self, world):
        world.seed_agent(LEAD)
        db.assign_skill(LEAD, "research", W.ALICE)
        res = W.apply(world, _manifest(["research"]), dry_run=True)
        assert _member(res).verdict == "unchanged"

    def test_a_set_derived_row_is_not_a_declared_skill(self, world):
        """`skills:` lists individual assignments, as export writes them; a skill
        a set brought is made individual by the assign (ent#530)."""
        world.seed_agent(LEAD)
        db.assign_skill(LEAD, "triage", W.ALICE)
        db.set_skill_individual(LEAD, "triage", False)
        res = W.apply(world, _manifest(["triage"]), dry_run=True)
        (missing,) = [c for c in _member(res).changes if c.code == "skill_missing"]
        assert missing.desired == ["triage"]

    @pytest.mark.parametrize("name,code", [("ghost", "skill_unknown"),
                                           ("\"..\"", "skill_name_invalid")])
    def test_a_name_the_library_cannot_assign_blocks(self, world, name, code):
        world.seed_agent(LEAD)
        res = W.apply(world, _manifest([name]), dry_run=True)
        assert res.status == "invalid" and _member(res).verdict == "blocked"
        assert [(b.code, b.member) for b in res.blockers] == [(code, LEAD)]

    def test_a_held_set_undeclared_is_reported_and_a_missing_one_written(self, world):
        world.seed_agent(LEAD)
        res = W.apply(world, _manifest(sets=["ops-core"]), dry_run=True)
        assert ("skill_sets", "write", "skill_set_missing") in _codes(_member(res))
        asyncio.run(asyncio.to_thread(skill_set_service.assign, LEAD, "ops-core", W.ALICE, None))
        res = W.apply(world, _manifest(sets=[]), dry_run=True)
        (extra,) = [c for c in _member(res).changes if c.code == "skill_set_extra"]
        assert extra.current == ["ops-core"]

    def test_an_unknown_set_blocks_with_the_set_routes_reason(self, world):
        world.seed_agent(LEAD)
        res = W.apply(world, _manifest(sets=["nope"]), dry_run=True)
        (b,) = res.blockers
        assert b.code == "skill_set_unknown" and "unknown_set" in b.detail

    def test_an_unreadable_library_is_a_retryable_blocker_never_unknown(self, world, monkeypatch):
        world.seed_agent(LEAD)
        def boom(_n):
            raise RuntimeError("clone unreadable")
        monkeypatch.setattr(skill_service, "get_skill", boom)
        res = W.apply(world, _manifest(["research"]), dry_run=True)
        (b,) = res.blockers
        assert b.code == "member_reads_unavailable" and b.retryable
        assert "skills library" in b.detail

    def test_a_member_without_the_fields_reads_no_skills(self, world, monkeypatch):
        world.seed_agent(LEAD)
        monkeypatch.setattr(db, "get_agent_skills",
                            lambda *_a: (_ for _ in ()).throw(AssertionError("read skills")))
        res = W.apply(world, _manifest(), dry_run=True)
        assert res.status == "valid"


# ===========================================================================
# The executor
# ===========================================================================

class TestExecute:
    def test_an_existing_member_is_assigned_gated_then_delivered(self, ew):
        ew.seed_agent(LEAD)
        res = W.apply(ew, _manifest(["research", "deploy"]), dry_run=False)
        assert res.status == "applied", res.failed
        assert sorted(db.get_agent_skill_names(LEAD)) == ["deploy", "research"]
        assert ew.calls == [("gates", LEAD), ("deliver", LEAD, ("research", "deploy"))]
        (delivery,) = [c for c in _member(res).changes if c.code == "skill_delivery"]
        assert delivery.current == "pending_start" and "next start" in delivery.note
        again = W.apply(ew, _manifest(["research", "deploy"]), dry_run=True)
        assert _member(again).verdict == "unchanged"

    def test_a_set_is_assigned_with_its_members(self, ew):
        ew.seed_agent(LEAD)
        res = W.apply(ew, _manifest(sets=["ops-core"]), dry_run=False)
        assert res.status == "applied", res.failed
        assert list(skill_set_service.held_sets(LEAD)) == ["ops-core"]
        assert sorted(db.get_agent_skill_names(LEAD)) == ["billing", "triage"]
        assert ("deliver", LEAD, ("billing", "triage")) in ew.calls or \
               ("deliver", LEAD, ("triage", "billing")) in ew.calls

    def test_a_created_member_is_assigned_before_it_starts(self, ew):
        res = W.apply(ew, _manifest(["research"]), dry_run=False)
        assert res.status == "applied", res.failed
        assert db.get_agent_skill_names(LEAD) == ["research"]
        assert ew.calls == [("gates", LEAD), ("deliver", LEAD, ("research",)), ("start", LEAD)]

    def test_a_failed_assign_is_a_named_failure_and_the_run_continues(self, ew, monkeypatch):
        ew.seed_agent(LEAD)
        def refuse(*_a, **_k):
            raise RuntimeError("disk full")
        monkeypatch.setattr(db, "assign_skill", refuse)
        res = W.apply(ew, _manifest(["research"], ["ops-core"]), dry_run=False)
        assert res.status == "partial"
        assert [(f.name, f.step) for f in res.failed] == [(LEAD, "skills")]
        assert list(skill_set_service.held_sets(LEAD)) == ["ops-core"]


# ===========================================================================
# Create mode (deploy): a best-effort post-create phase
# ===========================================================================

def _deploy(world, text, user, *, dry_run=False):
    return asyncio.run(system_service.deploy_manifest(
        text, user, None, dry_run=dry_run, create_agent_fn=world.create))


class TestDeploy:
    def test_deploy_assigns_declared_skills_and_sets_to_what_it_created(self, ew, monkeypatch):
        async def started(names):
            return {n: "started" for n in names}
        monkeypatch.setattr(system_service, "start_all_agents", started)
        res = _deploy(ew, _manifest(["research"], ["ops-core"]), ew.user(W.ALICE))
        assert res.status == "deployed", res.warnings
        assert sorted(db.get_agent_skill_names(LEAD)) == ["billing", "research", "triage"]
        assert list(skill_set_service.held_sets(LEAD)) == ["ops-core"]

    def test_deploy_skips_an_unknown_skill_with_a_warning(self, ew, monkeypatch):
        async def started(names):
            return {n: "started" for n in names}
        monkeypatch.setattr(system_service, "start_all_agents", started)
        res = _deploy(ew, _manifest(["research", "ghost"]), ew.user(W.ALICE))
        assert db.get_agent_skill_names(LEAD) == ["research"]
        assert any("ghost" in w and "skill_unknown" in w for w in res.warnings)

    def test_an_agent_key_without_skills_manage_assigns_nothing_and_says_so(self, ew, monkeypatch):
        """T8: deploy is agent-callable (agents.manage); an agent key writes
        skills only with skills.manage, the skill routes' own capability."""
        async def started(names):
            return {n: "started" for n in names}
        monkeypatch.setattr(system_service, "start_all_agents", started)
        base = ew.user(W.ALICE)
        agent_key = base.model_copy(update={"mcp_scope": "agent", "agent_name": "orchestrator"})
        res = _deploy(ew, _manifest(["research"]), agent_key)
        assert db.get_agent_skill_names(LEAD) == []
        assert any("skills.manage" in w for w in res.warnings)


# ===========================================================================
# Export
# ===========================================================================

class TestExport:
    def _export(self, names):
        import yaml
        text = system_service.export_manifest(
            SYSTEM, [{"name": n, "template": "local:plain"} for n in names])
        return text, yaml.safe_load(text)

    def test_individual_skills_and_held_sets_are_exported_and_round_trip(self, world):
        world.seed_agent(LEAD)
        db.assign_skill(LEAD, "research", W.ALICE)
        asyncio.run(asyncio.to_thread(skill_set_service.assign, LEAD, "ops-core", W.ALICE, None))
        text, doc = self._export([LEAD])
        lead = doc["agents"]["lead"]
        assert lead["skills"] == ["research"]  # set-derived rows are the set's
        assert lead["skill_sets"] == ["ops-core"]
        res = W.apply(world, text, dry_run=True)
        assert _member(res).verdict == "unchanged"

    def test_a_failed_skills_read_is_named_in_the_text(self, world, monkeypatch):
        world.seed_agent(LEAD)
        def boom(_n):
            raise RuntimeError("db locked")
        monkeypatch.setattr(db, "get_agent_skills", boom)
        text, doc = self._export([LEAD])
        assert text.splitlines()[0] == f"# unobserved: could not be read: skills of {LEAD}"
        assert "skills" not in doc["agents"]["lead"]
