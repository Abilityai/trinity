"""trinity-enterprise#1030 CP3: a manifest member's `gates:` — the approval map.

A member may declare `gates: {<skill>: {approver, deadline_hours}}`. A
declared gate the member does not carry as an explicit row is written
(`gate_missing`); one stored differently is rewritten (`gate_differs`); a
stored gate the manifest does not name is reported (`gate_undeclared`) and
NEVER cleared. Every write goes through `skill_gate_map_service.set_gate`
with `trigger="apply"`, so gate admission (ent#753, ent#848, ent#881) is the
route's own; the dry run validates through the same public
`validate_gate_changes` the write uses, so it reports exactly the refusal the
write would give. Gates are written BEFORE skills (ent#753: gated before
delivered). Deploy, which an agent key may call through `agents.manage`,
writes gates only with `skills.manage` (T8) and lists what it held back.

Runs over the REAL `db` on a full schema; Docker is faked at its read seams
(`_ent695_world.py`) plus the two the gate map touches: the single-agent
container state and the marker exec. The audit sink is recorded.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
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
from db_models import UserCreate  # noqa: E402
from services import (  # noqa: E402
    assignment_provider, docker_utils, skill_assignment_service, skill_gate_map_service,
    system_service,
)
from services.skill_gate_map_service import SkillGateMapRefused  # noqa: E402
from services.skill_service import skill_service  # noqa: E402

pytestmark = pytest.mark.unit

SYSTEM = W.SYSTEM
LEAD = f"{SYSTEM}-lead"
NOMAIL = "nomail1030"
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


class _Provider:
    """An assignments provider: one canon role on every roster, `approver`
    answered by `emails` (ent#881's shape)."""

    def __init__(self, emails=()):
        self.emails = list(emails)

    def assignment_for(self, agent_name, triggered_by):
        return None

    def people_for(self, agent_name, role):
        return {"emails": list(self.emails)} if role == "approver" else None

    def people_for_role_id(self, agent_name, role_id):
        return {"emails": list(self.emails)} if role_id == "fleet-owner" else None

    def approver_roles_for(self, agent_name):
        return {"roles": [{"id": "fleet-owner", "title": "Fleet owner"}]}


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    w = W.make_world(monkeypatch, tmp_path)
    db.create_user(UserCreate(username=NOMAIL, role="creator"))
    row = db.get_user_by_username(NOMAIL)
    from models import User
    w.users[NOMAIL] = User(id=row["id"], username=NOMAIL, role=row["role"], email=None)
    w.calls = []
    w.audit = []
    w.marker = []

    async def log(**kw):
        w.audit.append(kw)

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

    # The write seam, observed: every gate write goes through set_gate.
    real_set_gate = skill_gate_map_service.set_gate

    async def spy_set(agent_name, skill_name, *, changes, ctx):
        w.calls.append(("gate_set", agent_name, skill_name, ctx.trigger, ctx.via))
        return await real_set_gate(agent_name, skill_name, changes=changes, ctx=ctx)

    monkeypatch.setattr(skill_gate_map_service, "set_gate", spy_set)

    # The no-clear pin's live half: nothing in this checkpoint clears a gate.
    # A test that needs a tombstone writes it through the real pair, kept here.
    w.real_clear_gate = skill_gate_map_service.clear_gate
    w.real_clear_row = db.clear_skill_gate

    async def never_clear(*_a, **_k):
        raise AssertionError("apply never clears a gate")

    def never_clear_row(*_a, **_k):
        raise AssertionError("apply never clears a gate row")

    monkeypatch.setattr(skill_gate_map_service, "clear_gate", never_clear)
    monkeypatch.setattr(db, "clear_skill_gate", never_clear_row)
    monkeypatch.setattr(db, "drop_unassigned_skill_gates", never_clear_row)

    # Skills (CP1), for the gates-before-skills order.
    _patch_skill_service(monkeypatch, "get_skill",
                         lambda n: {"name": n} if n in LIBRARY else None)
    # The library recommends gating `deploy`: a clear on it leaves a tombstone.
    _patch_skill_service(monkeypatch, "list_skills",
                         lambda: [{"name": "deploy", "approval": "recommended"}])

    async def deliver(agent, names):
        w.calls.append(("deliver", agent, tuple(names)))
        return {"status": "pending_start", "skills": {n: {"status": "pending_start"} for n in names}}

    async def gates(agent, actor, request=None, **_k):
        w.calls.append(("reconcile", agent))
        return None

    _patch_skill_service(monkeypatch, "deliver_assigned", deliver)
    monkeypatch.setattr(skill_assignment_service, "sync_gates", gates)

    W.install_executor_seams(w, monkeypatch)
    start = w.docker.start

    async def recorded_start(name):
        w.calls.append(("start", name))
        return await start(name)

    w.docker.start = recorded_start
    assignment_provider.clear_provider()
    yield w
    assignment_provider.clear_provider()


def _manifest(gates=None, *, short="lead", skills=None, tags=None) -> str:
    lines = [f"name: {SYSTEM}", "agents:", f"  {short}:", "    template: local:plain"]
    if gates is not None:
        if gates == {}:
            lines.append("    gates: {}")
        else:
            lines.append("    gates:")
            for skill, spec in gates.items():
                if spec:
                    lines.append(f"      {skill}:")
                    for k, v in spec.items():
                        lines.append(f"        {k}: {v}")
                else:
                    lines.append(f"      {skill}: {{}}")
    if skills is not None:
        lines.append(f"    skills: [{', '.join(skills)}]")
    if tags is not None:
        lines.append(f"    tags: [{', '.join(tags)}]")
    return "\n".join(lines) + "\n"


def _apply(world, text, user=None, *, dry_run):
    user = user or world.user(W.ALICE)
    return asyncio.run(system_service.apply_manifest(
        text, user, None, dry_run=dry_run,
        create_agent_fn=world.create, start_agent_fn=world.docker.start))


def _deploy(world, text, user, *, dry_run=False):
    return asyncio.run(system_service.deploy_manifest(
        text, user, None, dry_run=dry_run, create_agent_fn=world.create))


def _member(res, name=LEAD):
    return next(m for m in res.members if m.name == name)


def _codes(member):
    return [(c.field, c.action, c.code) for c in member.changes]


def _rows(name):
    return {r["skill_name"]: (r["approver"], r["deadline_hours"], r["origin"])
            for r in db.list_agent_skill_gates(name)}


def _set(name, skill, **changes):
    """A stored explicit gate, through the real writer (no Docker: the world fakes it)."""
    return asyncio.run(skill_gate_map_service.set_gate(
        name, skill, changes=changes,
        ctx=skill_gate_map_service.GateContext(actor=None, via="system", trigger="direct")))


def _tombstone(world, monkeypatch, skill):
    """A `cleared` tombstone on an assigned skill, written through the real
    clear pair the fixture otherwise pins never-called; the pin is restored."""
    monkeypatch.setattr(db, "clear_skill_gate", world.real_clear_row)
    _set(LEAD, skill)
    asyncio.run(world.real_clear_gate(
        LEAD, skill, ctx=skill_gate_map_service.GateContext(actor=None)))
    monkeypatch.setattr(db, "clear_skill_gate",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("clear")))


def _agent_key(world, username=W.ALICE, agent="orchestrator"):
    return world.user(username).model_copy(update={"mcp_scope": "agent", "agent_name": agent})


# ===========================================================================
# Parse and validate
# ===========================================================================

class TestParse:
    def test_gates_are_read_and_are_not_an_unknown_key(self):
        m = system_service.parse_manifest(_manifest(
            {"deploy": {"approver": "primary", "deadline_hours": 24}, "apply-view": {}}))
        assert m.agents["lead"].gates == {
            "deploy": {"approver": "primary", "deadline_hours": 24}, "apply-view": {}}
        assert m.unknown_agent_keys == {}

    @pytest.mark.parametrize("value", ["deploy", "[deploy]", "{deploy: primary}"])
    def test_a_value_that_is_not_a_map_of_gates_is_a_named_refusal(self, value):
        text = f"name: {SYSTEM}\nagents:\n  lead:\n    template: local:plain\n    gates: {value}\n"
        with pytest.raises(ValueError, match="Agent 'lead' gates must be a map"):
            system_service.parse_manifest(text)

    def test_an_unknown_gate_field_is_a_named_refusal(self):
        with pytest.raises(ValueError, match="gate 'deploy': unknown key\\(s\\): approvers"):
            system_service.parse_manifest(_manifest({"deploy": {"approvers": "primary"}}))

    def test_the_recognised_key_text_stays_the_create_mode_text(self):
        text = _manifest({"deploy": {}}).replace("gates:", "credentials: {}\n    gates:")
        warnings = system_service.validate_manifest(system_service.parse_manifest(text))
        assert warnings == ["Agent 'lead': ignored unknown key(s): credentials. Recognised "
                            "per-agent keys are: folders, kind, resources, schedules, tags, "
                            "template."]

    def test_gates_are_classified_reconciled(self):
        assert system_service.APPLY_FIELD_CLASSIFICATION["SystemAgentConfig.gates"][0] == "reconciled"


# ===========================================================================
# One validator: the dry run refuses exactly as the write would
# ===========================================================================

class TestValidatorSeam:
    @pytest.mark.parametrize("spec,code,provider", [
        ({"approver": "boss"}, "invalid_approver", None),
        ({"approver": "approver"}, "approver_unavailable", None),
        ({"approver": "role:other"}, "unknown_role", _Provider()),
        ({"deadline_hours": 0}, "invalid_deadline", None),
        ({"deadline_hours": 2.5}, "invalid_deadline", None),
    ])
    def test_the_dry_run_reports_the_refusal_the_write_gives(self, world, spec, code, provider):
        if provider is not None:
            assignment_provider.register_provider(provider)
        world.seed_agent(LEAD)
        with pytest.raises(SkillGateMapRefused) as exc:
            _set(LEAD, "deploy", **spec)
        assert exc.value.code == code
        res = _apply(world, _manifest({"deploy": spec}), dry_run=True)
        assert res.status == "invalid" and _member(res).verdict == "blocked"
        assert [(b.code, b.member) for b in res.blockers] == [(code, LEAD)]
        assert exc.value.message in res.blockers[0].detail
        assert _rows(LEAD) == {}

    def test_a_bad_skill_name_is_refused_the_same_way(self, world):
        world.seed_agent(LEAD)
        res = _apply(world, _manifest({"../etc": {}}), dry_run=True)
        assert [b.code for b in res.blockers] == ["invalid_skill_name"]

    def test_the_plan_consults_the_public_validator_not_a_copy(self, world, monkeypatch):
        """Whatever `validate_gate_changes` refuses, the plan reports — there
        is no second validator that could drift from the write's."""
        def refuse(agent_name, skill_name, changes):
            raise SkillGateMapRefused(422, "custom_refusal", "the validator's own words")
        monkeypatch.setattr(skill_gate_map_service, "validate_gate_changes", refuse)
        world.seed_agent(LEAD)
        res = _apply(world, _manifest({"deploy": {}}), dry_run=True)
        assert [(b.code, b.detail) for b in res.blockers] == [
            ("custom_refusal", "the validator's own words")]

    def test_the_write_and_the_plan_share_one_public_validator(self):
        """`set_gate` validates through `validate_gate_changes`; the gates
        group calls the same name and defines no validator of its own."""
        gms = ast.parse((_BACKEND / "services" / "skill_gate_map_service.py").read_text())
        assert not any(isinstance(n, ast.FunctionDef) and n.name == "_validate_changes"
                       for n in ast.walk(gms))
        set_gate = next(n for n in ast.walk(gms)
                        if isinstance(n, ast.AsyncFunctionDef) and n.name == "set_gate")
        assert "validate_gate_changes" in {
            n.func.id for n in ast.walk(set_gate)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        group = ast.parse((_BACKEND / "services" / "system_apply" / "gates.py").read_text())
        assert "validate_gate_changes" in {
            n.func.attr for n in ast.walk(group)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not [n.name for n in ast.walk(group)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and "validate" in n.name]
        src = (_BACKEND / "services" / "system_apply" / "gates.py").read_text()
        for name in ("APPROVER_KINDS", "MIN_DEADLINE_HOURS", "MAX_DEADLINE_HOURS", "SKILL_NAME_RE"):
            assert name not in src, f"gates.py re-implements the validator's {name}"


# ===========================================================================
# Plan (dry run)
# ===========================================================================

class TestPlan:
    def test_a_missing_gate_is_a_write(self, world):
        world.seed_agent(LEAD)
        res = _apply(world, _manifest({"deploy": {"approver": "primary", "deadline_hours": 24}}),
                     dry_run=True)
        lead = _member(res)
        assert res.status == "valid" and lead.verdict == "update"
        assert _codes(lead) == [("gates", "write", "gate_missing")]
        change = lead.changes[0]
        assert change.current is None
        assert change.desired == {"skill": "deploy", "approver": "primary", "deadline_hours": 24}
        assert change.note is None   # the owner has an email: primary reaches someone
        assert res.summary["gates"] == {"write": 1, "report": 0, "unobserved": 0}
        assert _rows(LEAD) == {}

    def test_a_stored_gate_that_differs_is_rewritten(self, world):
        world.seed_agent(LEAD)
        _set(LEAD, "deploy", approver="primary", deadline_hours=24)
        res = _apply(world, _manifest({"deploy": {"deadline_hours": 48}}), dry_run=True)
        (change,) = _member(res).changes
        assert (change.field, change.action, change.code) == ("gates", "write", "gate_differs")
        assert change.current == {"approver": "primary", "deadline_hours": 24, "origin": "set"}
        assert change.desired == {"skill": "deploy", "approver": "primary", "deadline_hours": 48}

    def test_a_stored_gate_that_matches_is_in_sync_and_an_omitted_field_keeps_its_value(self, world):
        world.seed_agent(LEAD)
        _set(LEAD, "deploy", approver="primary", deadline_hours=24)
        res = _apply(world, _manifest({"Deploy": {"approver": "primary"}}), dry_run=True)
        assert _member(res).verdict == "unchanged" and _codes(_member(res)) == []
        assert res.summary["gates"] == {"write": 0, "report": 0, "unobserved": 0}

    def test_a_stored_gate_the_manifest_does_not_name_is_reported_never_cleared(self, world):
        world.seed_agent(LEAD)
        _set(LEAD, "deploy")
        res = _apply(world, _manifest({}), dry_run=True)
        lead = _member(res)
        assert lead.verdict == "unchanged"
        assert _codes(lead) == [("gates", "report", "gate_undeclared")]
        assert lead.changes[0].current == [{"skill": "deploy", "approver": "primary",
                                            "deadline_hours": None}]
        assert "never clears" in lead.changes[0].note
        assert res.summary["gates"] == {"write": 0, "report": 1, "unobserved": 0}

    def test_a_declared_gate_on_a_cleared_tombstone_is_a_write(self, world, monkeypatch):
        world.seed_agent(LEAD)
        db.assign_skill(LEAD, "deploy", W.ALICE)
        _tombstone(world, monkeypatch, "deploy")
        assert _rows(LEAD)["deploy"][2] == "cleared"
        res = _apply(world, _manifest({"deploy": {}}), dry_run=True)
        assert _codes(_member(res)) == [("gates", "write", "gate_missing")]
        assert _member(res).changes[0].current is None

    def test_a_library_default_is_made_explicit_as_the_write_would(self, world):
        world.seed_agent(LEAD)
        db.write_skill_gate(LEAD, "deploy", changes={"approver": "primary"},
                            origin="library_default", set_by="library", set_by_agent=None)
        res = _apply(world, _manifest({"deploy": {"approver": "primary"}}), dry_run=True)
        (change,) = _member(res).changes
        assert change.code == "gate_differs" and change.current["origin"] == "library_default"

    def test_a_declared_gate_whose_approver_reaches_nobody_is_noted_never_silent(self, world):
        world.seed_agent(LEAD, NOMAIL)
        res = _apply(world, _manifest({"deploy": {}}), world.user(W.ADMIN), dry_run=True)
        (change,) = _member(res).changes
        assert change.code == "gate_missing" and change.action == "write"
        assert change.note.startswith("approver_unassigned:")

    def test_an_approver_whose_people_cannot_open_the_agent_is_noted(self, world):
        assignment_provider.register_provider(_Provider(["outsider@example.com"]))
        world.seed_agent(LEAD)
        res = _apply(world, _manifest({"deploy": {"approver": "approver"}}), dry_run=True)
        (change,) = _member(res).changes
        assert change.code == "gate_missing"
        assert change.note.startswith("approver_not_shared:")

    def test_a_canon_role_the_roster_names_is_accepted_and_stored_lower_cased(self, world):
        assignment_provider.register_provider(_Provider([f"{W.ALICE}@example.com"]))
        world.seed_agent(LEAD)
        res = _apply(world, _manifest({"deploy": {"approver": "role:Fleet-Owner"}}), dry_run=True)
        (change,) = _member(res).changes
        assert change.code == "gate_missing" and change.desired["approver"] == "role:fleet-owner"
        assert change.note is None

    def test_a_member_without_the_key_reads_no_gates(self, world, monkeypatch):
        world.seed_agent(LEAD)
        monkeypatch.setattr(db, "list_agent_skill_gates",
                            lambda *_a: (_ for _ in ()).throw(AssertionError("read gates")))
        assert _apply(world, _manifest(), dry_run=True).status == "valid"

    def test_an_empty_map_asks_only_for_the_report(self, world):
        world.seed_agent(LEAD)
        _set(LEAD, "deploy")
        res = _apply(world, _manifest({}), dry_run=True)
        assert _codes(_member(res)) == [("gates", "report", "gate_undeclared")]

    def test_a_created_member_plans_its_gates(self, world):
        res = _apply(world, _manifest({"deploy": {"deadline_hours": 12}}), dry_run=True)
        lead = _member(res)
        assert lead.verdict == "create"
        assert ("gates", "write", "gate_missing") in _codes(lead)


# ===========================================================================
# The executor
# ===========================================================================

class TestExecute:
    def test_a_gate_is_written_through_set_gate_with_the_apply_trigger(self, world):
        world.seed_agent(LEAD)
        res = _apply(world, _manifest({"deploy": {"approver": "primary", "deadline_hours": 24}}),
                     dry_run=False)
        assert res.status == "applied", res.failed
        assert _rows(LEAD) == {"deploy": ("primary", 24, "set")}
        assert world.calls == [("gate_set", LEAD, "deploy", "apply", "ui")]
        (row,) = world.audit
        assert row["event_action"] == "skill_gate_set"
        assert row["details"]["trigger"] == "apply" and row["details"]["via"] == "ui"
        assert row["target_id"] == LEAD and row["actor_user"].username == W.ALICE
        again = _apply(world, _manifest({"deploy": {"approver": "primary", "deadline_hours": 24}}),
                       dry_run=False)
        assert _member(again).verdict == "unchanged" and len(world.audit) == 1

    def test_the_marker_is_written_before_the_first_gate_on_a_running_member(self, world):
        world.seed_agent(LEAD)
        _apply(world, _manifest({"deploy": {}}), dry_run=False)
        assert world.marker and world.marker[0] == f"agent-{LEAD}"

    def test_a_differing_gate_is_rewritten_and_an_omitted_field_is_kept(self, world):
        world.seed_agent(LEAD)
        _set(LEAD, "deploy", approver="primary", deadline_hours=24)
        res = _apply(world, _manifest({"deploy": {"deadline_hours": 48}}), dry_run=False)
        assert res.status == "applied", res.failed
        assert _rows(LEAD) == {"deploy": ("primary", 48, "set")}

    def test_an_undeclared_gate_survives_a_real_run(self, world):
        world.seed_agent(LEAD)
        _set(LEAD, "deploy")
        res = _apply(world, _manifest({"apply-view": {}}, tags=["x"]), dry_run=False)
        assert res.status == "applied", res.failed
        assert set(_rows(LEAD)) == {"deploy", "apply-view"}
        assert ("gates", "report", "gate_undeclared") in _codes(_member(res))
        assert [c for c in world.calls if c[0] == "gate_set" and c[3] == "apply"] == [
            ("gate_set", LEAD, "apply-view", "apply", "ui")]

    def test_a_tombstone_is_replaced_by_the_declared_gate(self, world, monkeypatch):
        world.seed_agent(LEAD)
        db.assign_skill(LEAD, "deploy", W.ALICE)
        _tombstone(world, monkeypatch, "deploy")
        res = _apply(world, _manifest({"deploy": {"deadline_hours": 6}}), dry_run=False)
        assert res.status == "applied", res.failed
        assert _rows(LEAD) == {"deploy": ("primary", 6, "set")}

    def test_gates_are_written_before_skills_on_an_existing_member(self, world):
        world.seed_agent(LEAD)
        res = _apply(world, _manifest({"research": {}}, skills=["research"]), dry_run=False)
        assert res.status == "applied", res.failed
        assert world.calls == [("gate_set", LEAD, "research", "apply", "ui"),
                               ("reconcile", LEAD), ("deliver", LEAD, ("research",))]

    def test_gates_are_written_before_skills_on_a_created_member_and_before_it_starts(self, world):
        res = _apply(world, _manifest({"research": {"deadline_hours": 2}}, skills=["research"]),
                     dry_run=False)
        assert res.status == "applied", res.failed
        assert world.calls == [("gate_set", LEAD, "research", "apply", "ui"),
                               ("reconcile", LEAD), ("deliver", LEAD, ("research",)),
                               ("start", LEAD)]
        assert _rows(LEAD) == {"research": ("primary", 2, "set")}

    def test_the_services_refusal_lands_in_failed_and_the_rest_lands(self, world, monkeypatch):
        world.seed_agent(LEAD)

        async def refusing(agent_name, skill_name, *, changes, ctx):
            raise SkillGateMapRefused(409, "ephemeral_agent", "Skill gates can't be set on a ghost.")

        monkeypatch.setattr(skill_gate_map_service, "set_gate", refusing)
        res = _apply(world, _manifest({"deploy": {}}, tags=["x"]), dry_run=False)
        assert res.status == "partial"
        assert [(f.name, f.step, f.reason.split(":")[0]) for f in res.failed] == [
            (LEAD, "gates", "ephemeral_agent")]
        assert "x" in db.get_agent_tags(LEAD)
        assert _member(res).applied is False

    def test_a_real_write_whose_approver_reaches_nobody_says_so(self, world):
        world.seed_agent(LEAD, NOMAIL)
        res = _apply(world, _manifest({"deploy": {}}), world.user(W.ADMIN), dry_run=False)
        assert res.status == "applied", res.failed
        notes = [c for c in _member(res).changes if c.code == "approver_unassigned"]
        assert len(notes) == 1 and notes[0].action == "report" and notes[0].desired == ["deploy"]

    def test_no_code_path_in_this_checkpoint_clears_a_gate(self):
        """The manifest adds and rewrites, never removes (rule 1): no clear,
        drop or tombstone writer has a caller in the gates group or the engine."""
        clearing = {"clear_gate", "clear_skill_gate", "drop_explicit_gates_on_unassign",
                    "drop_unassigned_skill_gates", "reconcile_library_gates",
                    "reconcile_library_skill_gates"}
        for path in (_BACKEND / "services" / "system_apply" / "gates.py",
                     _BACKEND / "services" / "system_service.py"):
            tree = ast.parse(path.read_text())
            names = {getattr(n.func, "attr", getattr(n.func, "id", None))
                     for n in ast.walk(tree) if isinstance(n, ast.Call)}
            assert not (names & clearing), f"{path.name} reaches {names & clearing}"
        group = ast.parse((_BACKEND / "services" / "system_apply" / "gates.py").read_text())
        set_calls = [n for n in ast.walk(group) if isinstance(n, ast.Call)
                     and getattr(n.func, "attr", None) == "set_gate"]
        assert set_calls, "the gates group writes through set_gate"
        for call in set_calls:
            ctx = next(k.value for k in call.keywords if k.arg == "ctx")
            assert isinstance(ctx, ast.Call), ast.dump(call)


# ===========================================================================
# Create mode (deploy): gates before skills, written only with skills.manage
# ===========================================================================

class TestDeploy:
    @pytest.fixture(autouse=True)
    def _started(self, monkeypatch):
        async def started(names):
            return {n: "started" for n in names}
        monkeypatch.setattr(system_service, "start_all_agents", started)

    def test_a_person_deploys_gates_before_skills(self, world):
        res = _deploy(world, _manifest({"research": {"deadline_hours": 3}}, skills=["research"]),
                      world.user(W.ALICE))
        assert res.status == "deployed", res.warnings
        assert _rows(LEAD) == {"research": ("primary", 3, "set")}
        assert world.calls[:3] == [("gate_set", LEAD, "research", "deploy", "ui"),
                                   ("reconcile", LEAD), ("deliver", LEAD, ("research",))]
        assert not [w for w in res.warnings if "gate" in w.lower()]

    def test_an_agent_key_without_skills_manage_writes_no_gate_and_is_told(self, world):
        res = _deploy(world, _manifest({"deploy": {}, "research": {"deadline_hours": 3}}),
                      _agent_key(world))
        assert res.status == "deployed", res.failed
        assert _rows(LEAD) == {} and world.audit == []
        held = [w for w in res.warnings if "Gates not written" in w]
        assert len(held) == 1 and "skills.manage" in held[0]
        assert LEAD in held[0] and "deploy" in held[0] and "research" in held[0]

    def test_an_agent_key_holding_skills_manage_writes_gates(self, world):
        db.register_agent_owner("orchestrator", W.ALICE)
        db.grant_agent_capability("orchestrator", "skills.manage", W.ADMIN)
        res = _deploy(world, _manifest({"deploy": {}}), _agent_key(world))
        assert res.status == "deployed", res.warnings
        assert _rows(LEAD) == {"deploy": ("primary", None, "set")}
        assert world.calls[0] == ("gate_set", LEAD, "deploy", "deploy", "orchestrator")

    def test_a_key_the_capability_refuses_is_held_back_the_same_way_never_a_blocker(self, world):
        connector = world.user(W.ALICE).model_copy(update={"mcp_scope": "connector"})
        res = _deploy(world, _manifest({"deploy": {}}), connector)
        assert res.status == "deployed", res.failed
        assert _rows(LEAD) == {}
        assert any("Gates not written" in w and "skills.manage" in w for w in res.warnings)

    def test_the_dry_run_already_says_what_will_be_held_back(self, world):
        res = _deploy(world, _manifest({"deploy": {}}), _agent_key(world), dry_run=True)
        assert res.status == "valid"
        assert any("Gates not written" in w for w in res.warnings)
        assert _deploy(world, _manifest({"deploy": {}}), world.user(W.ALICE),
                       dry_run=True).warnings == []

    def test_a_refused_gate_is_a_warning_and_the_deploy_stands(self, world):
        res = _deploy(world, _manifest({"deploy": {"approver": "boss"}, "research": {}}),
                      world.user(W.ALICE))
        assert res.status == "deployed"
        assert _rows(LEAD) == {"research": ("primary", None, "set")}
        assert any("invalid_approver" in w and "deploy" in w for w in res.warnings)


# ===========================================================================
# Export
# ===========================================================================

class TestExport:
    def _export(self, names):
        import yaml
        text = system_service.export_manifest(
            SYSTEM, [{"name": n, "template": "local:plain"} for n in names], viewer=W.owner_viewer())
        return text, yaml.safe_load(text)

    def test_explicit_gates_are_exported_and_round_trip(self, world):
        world.seed_agent(LEAD)
        _set(LEAD, "deploy", approver="primary", deadline_hours=24)
        _set(LEAD, "apply-view")
        text, doc = self._export([LEAD])
        assert doc["agents"]["lead"]["gates"] == {
            "apply-view": {"approver": "primary"},
            "deploy": {"approver": "primary", "deadline_hours": 24}}
        res = _apply(world, text, dry_run=True)
        assert _member(res).verdict == "unchanged" and _codes(_member(res)) == []

    def test_defaults_and_tombstones_are_not_target_state(self, world):
        world.seed_agent(LEAD)
        db.write_skill_gate(LEAD, "deploy", changes={}, origin="library_default",
                            set_by="library", set_by_agent=None)
        db.write_skill_gate(LEAD, "notes", changes={}, origin="cleared",
                            set_by=W.ALICE, set_by_agent=None)
        _text, doc = self._export([LEAD])
        assert "gates" not in doc["agents"]["lead"]

    def test_a_failed_gates_read_is_named_in_the_text(self, world, monkeypatch):
        world.seed_agent(LEAD)

        def boom(_n):
            raise RuntimeError("db locked")

        W.install_seat_reader(monkeypatch)
        monkeypatch.setattr(db, "list_agent_skill_gates", boom)
        text, doc = self._export([LEAD])
        assert text.splitlines()[0] == f"# unobserved: could not be read: gates of {LEAD}"
        assert "gates" not in doc["agents"]["lead"]


# ===========================================================================
# Gated before delivered: a gate that did not land holds its skill back (ent#753)
# ===========================================================================

def _gated_manifest(gates, skills=None, skill_sets=None):
    doc = {"name": SYSTEM, "agents": {"lead": {"template": "local:plain", "gates": gates}}}
    if skills is not None:
        doc["agents"]["lead"]["skills"] = skills
    if skill_sets is not None:
        doc["agents"]["lead"]["skill_sets"] = skill_sets
    import yaml
    return yaml.safe_dump(doc)


def _delivered(world):
    return [c for c in world.calls if c[0] == "deliver"]


class TestDeployHoldsAnUngatedSkill:
    def test_a_refused_gate_holds_its_skill_and_only_its_skill(self, world):
        res = _deploy(world, _gated_manifest({"deploy": {"approver": "role:cfo"}},
                                             skills=["deploy", "research"]), world.user(W.ALICE))
        assert res.status == "deployed"
        assigned = {r.skill_name for r in db.get_agent_skills(LEAD)}
        assert assigned == {"research"}
        assert all("deploy" not in c[2] for c in _delivered(world))
        assert any("Skill 'deploy' not assigned" in w and "gate" in w for w in res.warnings)

    def test_a_refused_gate_holds_its_skill_under_another_spelling(self, world):
        """The gate key is normalised and the skill name is not."""
        res = _deploy(world, _gated_manifest({"DePloy": {"approver": "role:cfo"}},
                                             skills=["deploy"]), world.user(W.ALICE))
        assert res.status == "deployed"
        assert not list(db.get_agent_skills(LEAD))

    def test_a_gate_that_lands_still_delivers(self, world):
        res = _deploy(world, _gated_manifest({"deploy": {}}, skills=["deploy"]), world.user(W.ALICE))
        assert res.status == "deployed"
        assert {r.skill_name for r in db.get_agent_skills(LEAD)} == {"deploy"}

    def test_a_gate_phase_that_blows_up_holds_every_declared_gated_skill(self, world, monkeypatch):
        async def boom(*_a, **_k):
            raise RuntimeError("db locked")
        monkeypatch.setattr(skill_gate_map_service, "set_gate", boom)
        res = _deploy(world, _gated_manifest({"deploy": {}}, skills=["deploy", "research"]),
                      world.user(W.ALICE))
        assert {r.skill_name for r in db.get_agent_skills(LEAD)} == {"research"}
        assert any("Skill 'deploy' not assigned" in w for w in res.warnings)

    def test_a_set_carrying_the_ungated_skill_is_held_whole(self, world, monkeypatch):
        from services import skill_set_service
        monkeypatch.setattr(skill_set_service, "library_sets", lambda **_k: {
            "ops": {"name": "ops", "source_id": None,
                    "members": [{"name": "deploy"}, {"name": "research"}]}})
        monkeypatch.setattr(skill_set_service, "require_set", lambda n: {"name": n})
        res = _deploy(world, _gated_manifest({"deploy": {"approver": "role:cfo"}}, skill_sets=["ops"]),
                      world.user(W.ALICE))
        assert res.status == "deployed"
        assert not list(db.get_agent_skills(LEAD))
        assert any("Skill set 'ops' not assigned" in w and "deploy" in w for w in res.warnings)

    def test_the_dry_run_names_the_gate_the_deploy_would_refuse(self, world):
        res = _deploy(world, _gated_manifest({"deploy": {"approver": "role:cfo"}}, skills=["deploy"]),
                      world.user(W.ALICE), dry_run=True)
        assert any("deploy" in w and "approver_unavailable" in w and "would not" in w
                   for w in res.warnings), res.warnings

    def test_a_clean_dry_run_stays_silent(self, world):
        res = _deploy(world, _gated_manifest({"deploy": {}}, skills=["deploy"]),
                      world.user(W.ALICE), dry_run=True)
        assert res.warnings == []


class TestApplyHoldsAnUngatedSkill:
    def _refuse_gates(self, monkeypatch):
        async def refused(agent_name, skill_name, *, changes, ctx):
            raise SkillGateMapRefused("approver_unavailable", "no provider")
        monkeypatch.setattr(skill_gate_map_service, "set_gate", refused)

    def test_a_refused_write_holds_its_skill_and_only_its_skill(self, world, monkeypatch):
        world.seed_agent(LEAD)
        self._refuse_gates(monkeypatch)
        res = _apply(world, _gated_manifest({"deploy": {}}, skills=["deploy", "research"]),
                     dry_run=False)
        assert res.status == "partial"
        assert {r.skill_name for r in db.get_agent_skills(LEAD)} == {"research"}
        steps = {(f.step, f.reason.split(":")[0]) for f in res.failed}
        assert ("skills", "gate_not_written") in steps and any(s == "gates" for s, _ in steps)
        assert all("deploy" not in c[2] for c in _delivered(world))

    def test_a_gate_write_that_raises_holds_its_skill(self, world, monkeypatch):
        world.seed_agent(LEAD)
        async def boom(*_a, **_k):
            raise RuntimeError("db locked")
        monkeypatch.setattr(skill_gate_map_service, "set_gate", boom)
        res = _apply(world, _gated_manifest({"deploy": {}}, skills=["deploy"]), dry_run=False)
        assert not list(db.get_agent_skills(LEAD))
        assert any(f.step == "skills" and f.reason.startswith("gate_not_written") for f in res.failed)

    def test_a_set_carrying_the_ungated_skill_is_held(self, world, monkeypatch):
        from services import skill_set_service
        world.seed_agent(LEAD)
        monkeypatch.setattr(skill_set_service, "library_sets", lambda **_k: {
            "ops": {"name": "ops", "source_id": None,
                    "members": [{"name": "deploy"}, {"name": "research"}]}})
        monkeypatch.setattr(skill_set_service, "require_set", lambda n: {"name": n})
        self._refuse_gates(monkeypatch)
        res = _apply(world, _gated_manifest({"deploy": {}}, skill_sets=["ops"]), dry_run=False)
        assert not list(db.get_agent_skills(LEAD))
        assert any(f.step == "skill_sets" and f.reason.startswith("gate_not_written")
                   for f in res.failed)

    def test_a_gate_that_lands_still_delivers(self, world):
        world.seed_agent(LEAD)
        res = _apply(world, _gated_manifest({"deploy": {}}, skills=["deploy"]), dry_run=False)
        assert res.status == "applied", res.failed
        assert {r.skill_name for r in db.get_agent_skills(LEAD)} == {"deploy"}
