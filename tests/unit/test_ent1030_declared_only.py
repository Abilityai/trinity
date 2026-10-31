"""trinity-enterprise#1030 CP5: `deploy: false` — a member named, never run.

A member may declare `deploy: false`. Its verdict is `declared_only`: apply
reports `template_in_catalog` / `template_not_in_catalog`, and
`declared_only_member_live` when an agent already holds its name — never
created, never reconciled, never deleted. Permissions or folders naming a
declared-only member are a named 400; a preset resolves over the deployed
members only and says so. Deploy skips it and lists it. Nothing platform-side
records one (T6 = A), so every export says so in its `# unobserved:` header.

Runs over the REAL `db` on a full schema; Docker is faked at its read seams
(`_ent695_world.py`).
"""
from __future__ import annotations

import asyncio
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
from services import system_service, template_service  # noqa: E402

pytestmark = pytest.mark.unit

SYSTEM = W.SYSTEM
LEAD, CANON = f"{SYSTEM}-lead", f"{SYSTEM}-canon"


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    w = W.make_world(monkeypatch, tmp_path)
    W.install_executor_seams(w, monkeypatch)
    return w


def _manifest(*, canon_template="local:plain", canon_extra=(), top_extra=()) -> str:
    lines = [f"name: {SYSTEM}", "agents:",
             "  lead:", "    template: local:plain",
             "  canon:", f"    template: {canon_template}", "    deploy: false",
             *[f"    {line}" for line in canon_extra], *top_extra]
    return "\n".join(lines) + "\n"


def _apply(world, text, user=W.ALICE, *, dry_run):
    return asyncio.run(system_service.apply_manifest(
        text, world.user(user), None, dry_run=dry_run,
        create_agent_fn=world.create, start_agent_fn=world.docker.start))


def _deploy(world, text, *, dry_run=False):
    return asyncio.run(system_service.deploy_manifest(
        text, world.user(W.ALICE), None, dry_run=dry_run, create_agent_fn=world.create))


def _member(res, name):
    return next(m for m in res.members if m.name == name)


def _codes(member):
    return [(c.field, c.action, c.code) for c in member.changes]


def _validate(text):
    return system_service.validate_manifest(system_service.parse_manifest(text))


# ===========================================================================
# Parse and validate
# ===========================================================================

class TestParse:
    def test_deploy_false_is_read_and_is_not_an_unknown_key(self):
        m = system_service.parse_manifest(_manifest())
        assert m.agents["canon"].deploy is False and m.agents["lead"].deploy is True
        assert m.unknown_agent_keys == {}

    def test_a_deploy_value_that_is_not_a_boolean_is_a_named_refusal(self):
        with pytest.raises(ValueError, match="Agent 'canon' deploy must be true or false"):
            system_service.parse_manifest(_manifest().replace("deploy: false", "deploy: 'no'"))

    def test_classification(self):
        assert system_service.APPLY_FIELD_CLASSIFICATION["SystemAgentConfig.deploy"][0] == "report-only"

    @pytest.mark.parametrize("explicit", [
        ["    lead: [canon]"],            # edge -> declared-only target
        ["    canon: [lead]"],            # declared-only source
    ])
    def test_explicit_permissions_naming_a_declared_only_member_are_a_named_400(self, explicit):
        text = _manifest(top_extra=["permissions:", "  explicit:", *explicit])
        with pytest.raises(ValueError, match="Permissions name declared-only member 'canon'"):
            _validate(text)

    def test_declared_only_to_declared_only_is_refused_too(self):
        text = (f"name: {SYSTEM}\nagents:\n  lead:\n    template: local:plain\n"
                "  a:\n    template: local:plain\n    deploy: false\n"
                "  b:\n    template: local:plain\n    deploy: false\n"
                "permissions:\n  explicit:\n    a: [b]\n")
        with pytest.raises(ValueError, match="declared-only member 'a'"):
            _validate(text)

    def test_folders_on_a_declared_only_member_are_a_named_400(self):
        text = _manifest(canon_extra=["folders: {expose: true, consume: true}"])
        with pytest.raises(ValueError, match="'canon' is declared-only .* cannot declare folders"):
            _validate(text)

    def test_a_preset_is_a_named_warning_never_a_refusal(self):
        warnings = _validate(_manifest(top_extra=["permissions:", "  preset: full-mesh"]))
        assert any("resolves over deployed members only" in w and "canon" in w for w in warnings)

    def test_the_recognised_key_text_stays_the_create_mode_text(self):
        warnings = _validate(_manifest(canon_extra=["credentials: {}"]))
        assert warnings == ["Agent 'canon': ignored unknown key(s): credentials. Recognised "
                            "per-agent keys are: folders, kind, resources, schedules, tags, "
                            "template."]


# ===========================================================================
# Apply
# ===========================================================================

class TestApply:
    def test_an_absent_declared_only_member_is_reported_and_never_created(self, world):
        res = _apply(world, _manifest(), dry_run=True)
        canon = _member(res, CANON)
        assert canon.verdict == "declared_only"
        assert _codes(canon) == [("template", "report", "template_in_catalog")]
        assert res.blockers == []
        real = _apply(world, _manifest(), dry_run=False)
        assert [c.name for c in world.create_calls] == [LEAD]
        assert _member(real, CANON).verdict == "declared_only"
        assert db.get_agent_owner(CANON) is None

    def test_a_template_outside_the_catalog_is_reported(self, world, monkeypatch):
        monkeypatch.setattr(template_service, "is_catalog_github_template", lambda repo: False)
        res = _apply(world, _manifest(canon_template="github:acme/canon"), dry_run=True)
        (c,) = _member(res, CANON).changes
        assert (c.code, c.desired) == ("template_not_in_catalog", "github:acme/canon")
        assert "an admin adds it" in c.note
        monkeypatch.setattr(template_service, "is_catalog_github_template",
                            lambda repo: repo == "acme/canon")
        res = _apply(world, _manifest(canon_template="github:acme/canon"), dry_run=True)
        assert _codes(_member(res, CANON)) == [("template", "report", "template_in_catalog")]

    def test_an_unknown_local_template_is_not_in_the_catalog(self, world):
        res = _apply(world, _manifest(canon_template="local:nope"), dry_run=True)
        assert _codes(_member(res, CANON)) == [("template", "report", "template_not_in_catalog")]

    def test_a_catalog_read_failure_is_the_retryable_blocker(self, world, monkeypatch):
        def boom(repo):
            raise RuntimeError("registry down")
        monkeypatch.setattr(template_service, "is_catalog_github_template", boom)
        res = _apply(world, _manifest(canon_template="github:acme/canon"), dry_run=True)
        assert [(b.code, b.member, b.retryable) for b in res.blockers] == [
            ("member_reads_unavailable", CANON, True)]
        assert _member(res, CANON).verdict == "blocked"

    def test_a_live_declared_only_member_is_reported_and_never_touched(self, world):
        world.seed_agent(LEAD)
        world.seed_agent(CANON, tags=(SYSTEM, "keep"))
        db.add_agent_permission(CANON, LEAD, W.ALICE)
        before = (sorted(db.get_agent_tags(CANON)), sorted(db.get_permitted_agents(CANON)))
        text = _manifest(top_extra=["permissions:", "  preset: full-mesh"])
        res = _apply(world, text, dry_run=False)
        canon = _member(res, CANON)
        assert canon.verdict == "declared_only"
        assert ("members", "report", "declared_only_member_live") in _codes(canon)
        assert res.drift == [] and res.failed == []
        assert world.docker.get(CANON) is not None and db.get_agent_owner(CANON)
        assert (sorted(db.get_agent_tags(CANON)), sorted(db.get_permitted_agents(CANON))) == before

    def test_a_preset_resolves_over_deployed_members_only(self, world):
        world.seed_agent(LEAD)
        text = (f"name: {SYSTEM}\nagents:\n  lead:\n    template: local:plain\n"
                "  helper:\n    template: local:plain\n"
                "  canon:\n    template: local:plain\n    deploy: false\n"
                "permissions:\n  preset: full-mesh\n")
        res = _apply(world, text, dry_run=True)
        edges = [c.desired for m in res.members for c in m.changes if c.field == "permissions"]
        assert edges and all(CANON not in str(e) for e in edges)

    def test_a_live_member_the_caller_cannot_manage_is_not_a_blocker(self, world):
        world.seed_agent(CANON, owner=W.BOB)
        res = _apply(world, _manifest(), dry_run=True)
        assert res.blockers == []
        assert _member(res, CANON).verdict == "declared_only"


# ===========================================================================
# Deploy
# ===========================================================================

class TestDeploy:
    @pytest.fixture(autouse=True)
    def _started(self, monkeypatch):
        async def started(names):
            return {n: "started" for n in names}
        monkeypatch.setattr(system_service, "start_all_agents", started)

    def test_deploy_skips_and_lists_it(self, world):
        res = _deploy(world, _manifest())
        assert res.status == "deployed", res.warnings
        assert res.agents_created == [LEAD]
        assert [c.name for c in world.create_calls] == [LEAD]
        listed = [w for w in res.warnings if "declared-only (deploy: false)" in w]
        assert len(listed) == 1 and CANON in listed[0]

    def test_the_dry_run_previews_without_it_and_lists_it(self, world):
        text = _manifest(top_extra=["permissions:", "  preset: full-mesh"])
        res = _deploy(world, text, dry_run=True)
        assert [a["name"] for a in res.agents_to_create] == [LEAD]
        assert CANON not in str(res.permission_edges)
        assert any("declared-only (deploy: false)" in w and CANON in w for w in res.warnings)


# ===========================================================================
# Export (T6 = A)
# ===========================================================================

class TestExport:
    def test_every_export_states_the_gap(self, world):
        world.seed_agent(LEAD)
        text = system_service.export_manifest(SYSTEM, [{"name": LEAD, "template": "local:plain"}])
        assert system_service.DECLARED_ONLY_EXPORT_HEADER in text.splitlines()
        assert system_service.DECLARED_ONLY_EXPORT_HEADER.startswith("# unobserved:")
        assert "deploy" not in text.split(system_service.DECLARED_ONLY_EXPORT_HEADER)[1]
