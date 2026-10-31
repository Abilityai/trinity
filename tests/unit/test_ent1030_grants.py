"""trinity-enterprise#1030 CP2: a manifest member's `grants:` — admin capabilities.

The GRANT half of grant-vs-use. A declared capability the member does not hold
is written ONLY when the caller passes the grant route's own gate — an admin
in a signed-in session (`dependencies.assert_interactive_admin`, the one
spelling of `assert_admin` + `reject_non_interactive_principal`). Every other
caller gets a report (`grant_requires_interactive_admin`), a top-level
`summary.grants.held_back` and one warning line, never a blocker: the rest of
the plan still applies. A held capability the manifest does not name is
reported (`grant_extra`) and never revoked. Deploy, which an agent key may
call through `agents.manage`, is fenced the same way — the back door this
checkpoint closes.

Runs over the REAL `db` on a full schema; Docker is faked at its read seams
(`_ent695_world.py`). The audit sink is recorded, never written.
"""
from __future__ import annotations

import ast
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
from fastapi import HTTPException  # noqa: E402
from database import db  # noqa: E402
from services import capability_grant_service, system_service  # noqa: E402

pytestmark = pytest.mark.unit

SYSTEM = W.SYSTEM
LEAD = f"{SYSTEM}-lead"


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    w = W.make_world(monkeypatch, tmp_path)
    w.calls = []
    w.audit = []

    async def log(**kw):
        w.audit.append(kw)

    audit_mod = importlib.import_module("services.platform_audit_service")
    monkeypatch.setattr(audit_mod.platform_audit_service, "log", log)

    # The no-revoke pin's live half: every grant write this checkpoint makes
    # passes `granted=True`. A revoke here is a defect, not a branch.
    real_set_grant = capability_grant_service.set_grant

    def spy(agent_name, capability, granted, actor):
        assert granted is True, f"set_grant({agent_name}, {capability}, {granted!r}): apply never revokes"
        w.calls.append(("grant", agent_name, capability))
        return real_set_grant(agent_name, capability, granted, actor)

    monkeypatch.setattr(capability_grant_service, "set_grant", spy)
    W.install_executor_seams(w, monkeypatch)
    start = w.docker.start

    async def recorded_start(name):
        w.calls.append(("start", name))
        return await start(name)

    w.docker.start = recorded_start
    return w


def _manifest(grants=None, *, short="lead", tags=None) -> str:
    lines = [f"name: {SYSTEM}", "agents:", f"  {short}:", "    template: local:plain"]
    if grants is not None:
        lines.append(f"    grants: [{', '.join(grants)}]")
    if tags is not None:
        lines.append(f"    tags: [{', '.join(tags)}]")
    return "\n".join(lines) + "\n"


def _apply(world, text, user, *, dry_run):
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


def _held(name):
    return sorted(g["capability"] for g in db.list_agent_capabilities(name))


def _agent_key(world, username=W.ALICE):
    return world.user(username).model_copy(update={"mcp_scope": "agent", "agent_name": "orchestrator"})


def _user_key(world, username=W.ADMIN):
    """An admin's own MCP key: passes `assert_admin`, fails the interactive gate."""
    return world.user(username).model_copy(update={"mcp_scope": "user"})


# ===========================================================================
# Parse and validate
# ===========================================================================

class TestParse:
    def test_grants_are_read_and_are_not_an_unknown_key(self):
        m = system_service.parse_manifest(_manifest(["agents.manage", "skills.manage"]))
        assert m.agents["lead"].grants == ["agents.manage", "skills.manage"]
        assert m.unknown_agent_keys == {}

    def test_a_value_that_is_not_a_list_of_names_is_a_named_refusal(self):
        text = f"name: {SYSTEM}\nagents:\n  lead:\n    template: local:plain\n    grants: agents.manage\n"
        with pytest.raises(ValueError, match="Agent 'lead' grants must be a list of names"):
            system_service.parse_manifest(text)

    def test_the_recognised_key_text_stays_the_create_mode_text(self):
        text = _manifest(["agents.manage"]).replace("grants:", "credentials: {}\n    grants:")
        warnings = system_service.validate_manifest(system_service.parse_manifest(text))
        assert warnings == ["Agent 'lead': ignored unknown key(s): credentials. Recognised "
                            "per-agent keys are: folders, kind, resources, schedules, tags, "
                            "template."]

    def test_grants_are_classified_reconciled(self):
        assert system_service.APPLY_FIELD_CLASSIFICATION["SystemAgentConfig.grants"][0] == "reconciled"


# ===========================================================================
# The shared gate
# ===========================================================================

class TestSharedGate:
    def test_only_a_signed_in_admin_passes(self, world):
        from dependencies import assert_interactive_admin, is_interactive_admin
        assert_interactive_admin(world.user(W.ADMIN))
        assert is_interactive_admin(world.user(W.ADMIN)) is True
        for who in (_agent_key(world, W.ADMIN), _user_key(world), world.user(W.ALICE)):
            with pytest.raises(HTTPException) as exc:
                assert_interactive_admin(who)
            assert exc.value.status_code == 403
            assert is_interactive_admin(who) is False

    def test_the_gate_is_spelled_once_and_both_grant_routes_call_it(self):
        """A second copy of the `assert_admin` + `reject_non_interactive_principal`
        pair is a finding: the routes, the snapshot and deploy all consult the
        one helper. Pinned on the source because the consumer is the gate's
        single spelling, which no call can observe."""
        deps = ast.parse((_BACKEND / "dependencies.py").read_text())
        gate = next(n for n in ast.walk(deps)
                    if isinstance(n, ast.FunctionDef) and n.name == "assert_interactive_admin")
        called = {n.func.id for n in ast.walk(gate) if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Name)}
        assert {"assert_admin", "reject_non_interactive_principal"} <= called
        for router, handler in (("agent_config", "set_agent_capability_grant"),
                                ("skills", "set_skill_manager")):
            tree = ast.parse((_BACKEND / "routers" / f"{router}.py").read_text())
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.AsyncFunctionDef) and n.name == handler)
            names = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name)}
            assert "assert_interactive_admin" in names, handler
            assert "reject_non_interactive_principal" not in names, handler

    def test_no_write_in_this_checkpoint_revokes(self):
        """`set_grant(..., False, ...)` has no caller in the apply engine or
        its grants group — the manifest adds, never removes (rule 1)."""
        for path in (_BACKEND / "services" / "system_apply" / "grants.py",
                     _BACKEND / "services" / "system_service.py"):
            tree = ast.parse(path.read_text())
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and getattr(n.func, "attr", getattr(n.func, "id", None)) == "set_grant"]
            assert calls or path.name == "system_service.py"
            for call in calls:
                granted = (call.args[2] if len(call.args) > 2
                           else next(k.value for k in call.keywords if k.arg == "granted"))
                assert isinstance(granted, ast.Constant) and granted.value is True, ast.dump(call)


# ===========================================================================
# Plan (dry run)
# ===========================================================================

class TestPlan:
    def test_a_missing_grant_is_a_write_for_a_signed_in_admin(self, world):
        world.seed_agent(LEAD)
        res = _apply(world, _manifest(["agents.manage"]), world.user(W.ADMIN), dry_run=True)
        lead = _member(res)
        assert res.status == "valid" and lead.verdict == "update"
        (missing,) = [c for c in lead.changes if c.code == "grant_missing"]
        assert (missing.field, missing.action) == ("grants", "write")
        assert missing.current == [] and missing.desired == ["agents.manage"]
        assert res.summary["grants"]["held_back"] == [] and res.summary["grants"]["write"] == 1
        assert not [w for w in res.warnings if "grant" in w]

    def test_a_held_undeclared_grant_is_reported_and_never_a_write(self, world):
        world.seed_agent(LEAD)
        db.grant_agent_capability(LEAD, "schedules.manage", "admin")
        res = _apply(world, _manifest([]), world.user(W.ADMIN), dry_run=True)
        lead = _member(res)
        assert lead.verdict == "unchanged"
        assert _codes(lead) == [("grants", "report", "grant_extra")]
        assert lead.changes[0].current == ["schedules.manage"]
        assert "never revokes" in lead.changes[0].note

    def test_an_unknown_capability_blocks(self, world):
        world.seed_agent(LEAD)
        res = _apply(world, _manifest(["fleet.rule"]), world.user(W.ADMIN), dry_run=True)
        assert res.status == "invalid" and _member(res).verdict == "blocked"
        assert [(b.code, b.member) for b in res.blockers] == [("grant_unknown", LEAD)]
        assert "agents.manage" in res.blockers[0].remedy

    def test_a_member_without_the_key_reads_no_grants(self, world, monkeypatch):
        world.seed_agent(LEAD)
        monkeypatch.setattr(db, "list_agent_capabilities",
                            lambda *_a: (_ for _ in ()).throw(AssertionError("read grants")))
        assert _apply(world, _manifest(), world.user(W.ADMIN), dry_run=True).status == "valid"

    @pytest.mark.parametrize("who,verdict", [
        # An agent key never reaches an existing sibling (ent#629), so its case
        # runs on the member apply CREATES: the one it could otherwise grant to.
        ("agent_key", "create"), ("admin_user_key", "update"), ("non_admin_person", "update")])
    def test_every_other_caller_is_held_back_and_the_rest_still_applies(self, world, who, verdict):
        if verdict == "update":
            world.seed_agent(LEAD)
        user = {"agent_key": lambda: _agent_key(world, W.ADMIN),
                "admin_user_key": lambda: _user_key(world),
                "non_admin_person": lambda: world.user(W.ALICE)}[who]()
        res = _apply(world, _manifest(["agents.manage", "skills.manage"], tags=["x"]),
                     user, dry_run=True)
        lead = _member(res)
        assert res.status == "valid" and not res.blockers
        (held,) = [c for c in lead.changes if c.code == "grant_requires_interactive_admin"]
        assert held.action == "report" and held.desired == ["agents.manage", "skills.manage"]
        assert ("tags", "write", "tag_missing") in _codes(lead)   # the rest of the plan
        assert lead.verdict == verdict
        assert res.summary["grants"]["held_back"] == [
            {"name": LEAD, "grants": ["agents.manage", "skills.manage"]}]
        grant_warnings = [w for w in res.warnings if "grant_requires_interactive_admin" in w]
        assert len(grant_warnings) == 1 and LEAD in grant_warnings[0]

    def test_held_back_alone_leaves_the_member_unchanged(self, world):
        world.seed_agent(LEAD)
        res = _apply(world, _manifest(["agents.manage"]), world.user(W.ALICE), dry_run=True)
        assert _member(res).verdict == "unchanged"

    def test_the_snapshot_consults_the_shared_gate_not_its_own_copy(self, world, monkeypatch):
        import dependencies
        monkeypatch.setattr(dependencies, "is_interactive_admin", lambda _u: False)
        world.seed_agent(LEAD)
        res = _apply(world, _manifest(["agents.manage"]), world.user(W.ADMIN), dry_run=True)
        assert _codes(_member(res)) == [("grants", "report", "grant_requires_interactive_admin")]


# ===========================================================================
# The executor
# ===========================================================================

class TestExecute:
    def test_a_missing_grant_is_written_and_audited_with_the_routes_verbs(self, world):
        world.seed_agent(LEAD)
        res = _apply(world, _manifest(["agents.manage", "skills.manage"]),
                     world.user(W.ADMIN), dry_run=False)
        assert res.status == "applied", res.failed
        assert _held(LEAD) == ["agents.manage", "skills.manage"]
        from services.platform_audit_service import AuditEventType
        verbs = {a["details"]["capability"]: a["event_action"] for a in world.audit}
        assert verbs == {"agents.manage": "capability_grant",
                         "skills.manage": "skill_manager_grant"}  # #3236: one verb per door
        for row in world.audit:
            assert row["event_type"] == AuditEventType.AUTHORIZATION
            assert row["target_type"] == "agent" and row["target_id"] == LEAD
            assert row["actor_user"].username == W.ADMIN
            assert row["details"]["granted"] is True
        again = _apply(world, _manifest(["agents.manage", "skills.manage"]),
                       world.user(W.ADMIN), dry_run=False)
        assert _member(again).verdict == "unchanged" and len(world.audit) == 2

    def test_a_held_undeclared_grant_survives_a_real_run(self, world):
        world.seed_agent(LEAD)
        db.grant_agent_capability(LEAD, "schedules.manage", "admin")
        res = _apply(world, _manifest([], tags=["x"]), world.user(W.ADMIN), dry_run=False)
        assert res.status == "applied", res.failed
        assert _held(LEAD) == ["schedules.manage"]
        assert ("grants", "report", "grant_extra") in _codes(_member(res))
        assert not [a for a in world.audit if "revoke" in a["event_action"]]
        assert not [c for c in world.calls if c[0] == "grant"]

    def test_a_held_back_caller_writes_no_grant_on_a_real_run(self, world):
        world.seed_agent(LEAD)
        res = _apply(world, _manifest(["agents.manage"], tags=["x"]), world.user(W.ALICE), dry_run=False)
        assert res.status == "applied", res.failed
        assert _held(LEAD) == [] and "x" in db.get_agent_tags(LEAD)
        assert world.audit == []

    def test_a_created_member_is_granted_before_it_starts(self, world):
        res = _apply(world, _manifest(["agents.manage"]), world.user(W.ADMIN), dry_run=False)
        assert res.status == "applied", res.failed
        assert _held(LEAD) == ["agents.manage"]
        assert world.calls == [("grant", LEAD, "agents.manage"), ("start", LEAD)]

    def test_a_system_agent_and_a_ghost_are_refused_before_any_grant(self, world):
        """Apply never reconfigures either (the member gates), so neither of
        the service's refusals for them is ever reached: no grant is attempted."""
        system_agent, ghost = f"{SYSTEM}-sys", f"{SYSTEM}-ghost"
        db.register_agent_owner(system_agent, W.ADMIN, is_system=True)
        db.set_agent_tags(system_agent, [SYSTEM])
        world.docker.add(system_agent, template="local:plain")
        db.register_agent_owner(ghost, W.ADMIN, is_ephemeral=True)
        db.set_agent_tags(ghost, [SYSTEM])
        world.docker.add(ghost, template="local:plain")
        text = "\n".join([
            f"name: {SYSTEM}", "agents:",
            "  sys:", "    template: local:plain", "    grants: [agents.manage]",
            "  ghost:", "    template: local:plain", "    grants: [agents.manage]",
        ]) + "\n"
        res = _apply(world, text, world.user(W.ADMIN), dry_run=False)
        assert res.status == "refused"
        assert sorted(b.code for b in res.blockers) == ["member_is_ephemeral", "member_is_system_agent"]
        assert not [c for c in world.calls if c[0] == "grant"]
        assert _held(system_agent) == [] and _held(ghost) == []

    def test_the_services_refusals_land_in_failed_and_the_rest_lands(self, world, monkeypatch):
        """`calibrating_agent` is real (a calibrating companion, ent#663); the
        other two codes are faked at the service seam, since the member gates
        above refuse their targets first — the executor's mapping of ANY
        `CapabilityGrantRefused` into `failed[]` is what this pins."""
        from services.capability_grant_service import CapabilityGrantRefused
        one, two, companion = f"{SYSTEM}-one", f"{SYSTEM}-two", f"{SYSTEM}-comp"
        for name in (one, two, companion):
            world.seed_agent(name, W.ADMIN)
        db.set_agent_role_readiness(companion, "calibrating", W.ADMIN)
        spy = capability_grant_service.set_grant
        faked = {one: "system_agent_not_grantable", two: "ephemeral_agent_not_grantable"}

        def refusing(agent_name, capability, granted, actor):
            if agent_name in faked:
                raise CapabilityGrantRefused(faked[agent_name], "refused by the service", 422)
            return spy(agent_name, capability, granted, actor)

        monkeypatch.setattr(capability_grant_service, "set_grant", refusing)
        text = "\n".join([
            f"name: {SYSTEM}", "agents:",
            "  one:", "    template: local:plain", "    grants: [agents.manage]", "    tags: [x]",
            "  two:", "    template: local:plain", "    grants: [agents.manage]",
            "  comp:", "    template: local:plain",
            "    grants: [instructions.manage, schedules.manage]",
        ]) + "\n"
        res = _apply(world, text, world.user(W.ADMIN), dry_run=False)
        assert res.status == "partial", [(b.code, b.member) for b in res.blockers]
        failed = sorted((f.name, f.step, f.reason.split(":")[0]) for f in res.failed)
        assert failed == [(companion, "grants", "calibrating_agent"),
                          (one, "grants", "system_agent_not_grantable"),
                          (two, "grants", "ephemeral_agent_not_grantable")]
        assert _held(companion) == ["schedules.manage"]     # the other grant landed
        assert "x" in db.get_agent_tags(one)                 # the rest of the plan landed
        assert _member(res, one).applied is False and _member(res, companion).applied is False
        assert [a["details"]["capability"] for a in world.audit] == ["schedules.manage"]


# ===========================================================================
# Create mode (deploy): the back door this checkpoint closes
# ===========================================================================

class TestDeploy:
    @pytest.fixture(autouse=True)
    def _started(self, monkeypatch):
        async def started(names):
            return {n: "started" for n in names}
        monkeypatch.setattr(system_service, "start_all_agents", started)

    def test_a_signed_in_admin_deploys_grants_and_they_are_audited(self, world):
        res = _deploy(world, _manifest(["agents.manage"]), world.user(W.ADMIN))
        assert res.status == "deployed", res.warnings
        assert _held(LEAD) == ["agents.manage"]
        assert [a["event_action"] for a in world.audit] == ["capability_grant"]

    @pytest.mark.parametrize("who", ["agent_key", "admin_user_key", "non_admin_person"])
    def test_any_other_deployer_writes_no_grant_and_is_told(self, world, who):
        """`deploy_system` is `capability_fence("agents.manage")`, which an agent
        key satisfies: a manifest it deploys must never write a grant."""
        user = {"agent_key": lambda: _agent_key(world, W.ADMIN),
                "admin_user_key": lambda: _user_key(world),
                "non_admin_person": lambda: world.user(W.ALICE)}[who]()
        res = _deploy(world, _manifest(["agents.manage"]), user)
        assert res.status == "deployed", res.failed
        assert _held(LEAD) == [] and world.audit == []
        held = [w for w in res.warnings if "grants_require_interactive_admin" in w]
        assert len(held) == 1 and "agents.manage" in held[0] and LEAD in held[0]

    def test_the_dry_run_already_says_what_will_be_held_back(self, world):
        res = _deploy(world, _manifest(["agents.manage"]), _agent_key(world, W.ADMIN), dry_run=True)
        assert res.status == "valid"
        assert any("grants_require_interactive_admin" in w for w in res.warnings)
        assert _deploy(world, _manifest(["agents.manage"]), world.user(W.ADMIN),
                       dry_run=True).warnings == []

    def test_a_refused_grant_is_a_warning_and_the_deploy_stands(self, world, monkeypatch):
        monkeypatch.setattr(db, "get_agent_ephemeral_info", lambda _n: {"is_ephemeral": True})
        res = _deploy(world, _manifest(["agents.manage"]), world.user(W.ADMIN))
        assert res.status == "deployed" and _held(LEAD) == []
        assert any("ephemeral_agent_not_grantable" in w for w in res.warnings)


# ===========================================================================
# Export
# ===========================================================================

class TestExport:
    def _export(self, names):
        import yaml
        text = system_service.export_manifest(
            SYSTEM, [{"name": n, "template": "local:plain"} for n in names])
        return text, yaml.safe_load(text)

    def test_held_grants_are_exported_and_round_trip(self, world):
        world.seed_agent(LEAD)
        db.grant_agent_capability(LEAD, "skills.manage", "admin")
        text, doc = self._export([LEAD])
        assert doc["agents"]["lead"]["grants"] == ["skills.manage"]
        res = _apply(world, text, world.user(W.ADMIN), dry_run=True)
        assert _member(res).verdict == "unchanged"

    def test_a_failed_grants_read_is_named_in_the_text(self, world, monkeypatch):
        world.seed_agent(LEAD)
        def boom(_n):
            raise RuntimeError("db locked")
        monkeypatch.setattr(db, "list_agent_capabilities", boom)
        text, doc = self._export([LEAD])
        assert text.splitlines()[0] == f"# unobserved: could not be read: grants of {LEAD}"
        assert "grants" not in doc["agents"]["lead"]
