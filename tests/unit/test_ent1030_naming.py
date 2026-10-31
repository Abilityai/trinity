"""trinity-enterprise#1030: `naming:` — a manifest may name its members bare.

`naming: prefixed` (the default) is today: member `x` of system `acme` is the
agent `acme-x`. `naming: bare` makes the manifest key the agent's full name, so
a fleet whose agents were never prefixed can be described — and reconciled —
by the one format.

Every case runs over the REAL `db` on a full schema (`db_backend`); Docker is
faked at its read seams only (`_ent695_world.py`). The prefixed path is pinned
byte-for-byte by `test_ent695_create_mode_golden.py`, which this issue leaves
unedited.
"""
from __future__ import annotations

import ast
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
from services import system_service  # noqa: E402

pytestmark = pytest.mark.unit

SYSTEM = W.SYSTEM


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    return W.make_world(monkeypatch, tmp_path)


@pytest.fixture
def ew(world, monkeypatch):
    W.install_executor_seams(world, monkeypatch)
    return world


def _manifest(*shorts: str, naming: str | None = None, extra: str = "") -> str:
    head = f"name: {SYSTEM}\n" + (f"naming: {naming}\n" if naming is not None else "")
    agents = "\n".join(f"  {s}:\n    template: local:plain" for s in shorts)
    return f"{head}agents:\n{agents}\n{extra}"


def _blocker(snapshot, code):
    found = [b for b in snapshot.blockers if b.code == code]
    assert found, f"no {code} blocker in {[(b.code, b.member) for b in snapshot.blockers]}"
    return found


def _parse(yaml_text: str):
    manifest = system_service.parse_manifest(yaml_text)
    warnings = system_service.validate_manifest(manifest)
    return manifest, warnings


# ===========================================================================
# CP0a — the key and the one name resolver
# ===========================================================================

class TestTheNamingKey:

    def test_absent_is_prefixed_which_is_today(self):
        manifest, warnings = _parse(_manifest("a"))
        assert manifest.naming == "prefixed"
        assert warnings == []

    def test_bare_is_read_and_is_not_an_unknown_key(self):
        manifest, warnings = _parse(_manifest("scout-lead", naming="bare"))
        assert manifest.naming == "bare"
        assert "naming" not in manifest.unknown_keys
        assert warnings == []

    @pytest.mark.parametrize("value", ["flat", "Bare", "'yes'", "1", "null"])
    def test_any_other_value_is_a_named_refusal(self, value):
        manifest = system_service.parse_manifest(_manifest("a", naming=value))
        with pytest.raises(ValueError) as e:
            system_service.validate_manifest(manifest)
        assert "naming must be 'prefixed' or 'bare'" in str(e.value)

    def test_the_route_answers_the_refusal_as_a_named_400(self, world):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as e:
            asyncio.run(system_service.deploy_manifest(
                _manifest("a", naming="flat"), world.user(W.ALICE), dry_run=True))
        assert e.value.status_code == 400
        assert "naming must be 'prefixed' or 'bare'" in str(e.value.detail)

    def test_a_member_may_be_named_like_its_system_under_bare(self):
        manifest, _ = _parse(_manifest(SYSTEM, "scout-lead", naming="bare"))
        assert system_service.declared_names(manifest) == {SYSTEM: SYSTEM, "scout-lead": "scout-lead"}


class TestOneNameResolver:

    def test_prefixed_names_carry_the_system(self):
        manifest, _ = _parse(_manifest("a", "b"))
        assert system_service.member_name(manifest, "a") == "acme-a"
        assert system_service.declared_names(manifest) == {"a": "acme-a", "b": "acme-b"}

    def test_bare_names_are_the_keys(self):
        manifest, _ = _parse(_manifest("scout-lead", "catalog-entry", naming="bare"))
        assert system_service.member_name(manifest, "scout-lead") == "scout-lead"
        assert system_service.declared_names(manifest) == {"scout-lead": "scout-lead", "catalog-entry": "catalog-entry"}

    def test_no_member_name_is_spelled_outside_the_resolver(self):
        """Three f-strings of one rule is how #2373 became a bug in three
        places: a `{x}-{y}` join in `system_service.py` lives in the resolver
        and nowhere else."""
        src = (_BACKEND / "services" / "system_service.py").read_text()
        tree = ast.parse(src)
        owners = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    owners.setdefault(id(node), fn.name)
        joins = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            v = node.values
            if any(
                isinstance(v[i], ast.FormattedValue)
                and isinstance(v[i + 1], ast.Constant) and v[i + 1].value == "-"
                and isinstance(v[i + 2], ast.FormattedValue)
                for i in range(len(v) - 2)
            ):
                joins.append((owners.get(id(node)), node.lineno))
        assert joins and {fn for fn, _ in joins} == {"_member_name"}, joins


class TestDeployUnderBare:
    """P8 (ruled yes): deploy creates bare names too."""

    def test_the_dry_run_previews_bare_names(self, world):
        res = asyncio.run(system_service.deploy_manifest(
            _manifest("scout-lead", "catalog-entry", naming="bare"), world.user(W.ALICE), dry_run=True))
        assert [a["name"] for a in res.agents_to_create] == ["scout-lead", "catalog-entry"]
        assert res.failed == []

    def test_a_taken_bare_name_still_suffixes_in_create_mode(self, world):
        world.seed_agent("scout-lead", tags=())
        res = asyncio.run(system_service.deploy_manifest(
            _manifest("scout-lead", naming="bare"), world.user(W.ALICE), dry_run=True))
        assert [a["name"] for a in res.agents_to_create] == ["scout-lead_2"]
        assert any("'scout-lead' already exists" in w for w in res.warnings)

    def test_a_real_deploy_creates_and_tags_the_bare_name(self, ew, monkeypatch):
        async def _no_start(names):
            return {n: "started" for n in names}
        monkeypatch.setattr(system_service, "start_all_agents", _no_start)
        res = asyncio.run(system_service.deploy_manifest(
            _manifest("scout-lead", naming="bare"), ew.user(W.ALICE), create_agent_fn=ew.create))
        assert res.agents_created == ["scout-lead"]
        assert [c.name for c in ew.create_calls] == ["scout-lead"]
        # The system tag is the membership record a bare name cannot carry.
        assert SYSTEM in db.get_agent_tags("scout-lead")

    def test_prefixed_deploy_is_unchanged(self, world):
        res = asyncio.run(system_service.deploy_manifest(
            _manifest("scout-lead"), world.user(W.ALICE), dry_run=True))
        assert [a["name"] for a in res.agents_to_create] == ["acme-scout-lead"]


class TestApplyUnderBare:

    def test_an_absent_bare_member_is_created_under_its_bare_name(self, ew):
        res = W.apply(ew, _manifest("scout-lead", naming="bare"), dry_run=True)
        assert [(m.name, m.verdict) for m in res.members] == [("scout-lead", "create")]

    def test_a_tagged_bare_member_is_reconciled_not_refused(self, world):
        world.seed_agent("scout-lead")  # tagged `acme` by its deploy
        snap = world.snapshot(_manifest("scout-lead", naming="bare"))
        assert snap.blockers == []
        assert snap.members["scout-lead"].name == "scout-lead"
        assert snap.members["scout-lead"].status == "managed"
        assert snap.membership.evidence == {"scout-lead": "tag"}


# ===========================================================================
# CP0b — bare adoption of a pre-existing untagged agent (T1 = A)
# ===========================================================================

class TestBareAdoption:
    """A declared, live, manageable agent that carries no system tag is
    ADOPTED under `naming: bare`: one additive tag write, planned in the dry
    run and listed in `adopted[]`. `naming: bare` plus the declared name plus a
    person's credential is the explicit statement; the prefix inference that
    makes `member_claimed_by_another_system` true does not exist for a bare
    name."""

    def _three(self, world):
        world.seed_agent("scout-lead", tags=())
        world.seed_agent("catalog-entry", tags=("ops",))
        world.seed_agent("scout", tags=())

    def test_the_snapshot_marks_them_untagged_never_claimed(self, world):
        self._three(world)
        snap = world.snapshot(_manifest("scout-lead", "catalog-entry", "scout", naming="bare"))
        assert snap.blockers == []
        assert {s: m.untagged for s, m in snap.members.items()} == {
            "scout-lead": True, "catalog-entry": True, "scout": True}
        assert {m.status for m in snap.members.values()} == {"managed"}

    def test_the_dry_run_plans_one_adoption_write_each_and_no_create(self, ew):
        self._three(ew)
        res = W.apply(ew, _manifest("scout-lead", "catalog-entry", "scout", naming="bare"), dry_run=True)
        assert res.status == "valid"
        assert [(m.name, m.verdict) for m in res.members] == [
            ("scout-lead", "update"), ("catalog-entry", "update"), ("scout", "update")]
        catalog_entry = next(m for m in res.members if m.name == "catalog-entry")
        (adopt,) = [c for c in catalog_entry.changes if c.code == "member_adopted"]
        assert adopt.field == "members" and adopt.action == "write"
        assert adopt.desired == [SYSTEM]
        # E1: the agent's existing tags are SHOWN — the platform cannot know
        # whether `ops` is another system's name.
        assert adopt.current == ["ops"]
        assert "two systems" in adopt.note
        # The system tag is the adoption, not also a `tag_missing`.
        assert not [c for c in catalog_entry.changes if c.code == "tag_missing"]
        assert [(a.name, a.tags) for a in res.adopted] == [
            ("scout-lead", []), ("catalog-entry", ["ops"]), ("scout", [])]

    def test_the_warning_names_restart_and_teardown_in_plain_words(self, ew):
        self._three(ew)
        res = W.apply(ew, _manifest("scout-lead", "catalog-entry", "scout", naming="bare"), dry_run=True)
        (line,) = [w for w in res.warnings if "adopted" in w]
        assert "3 agents" in line and f"'{SYSTEM}'" in line
        assert "restart" in line and "teardown" in line
        assert "scout-lead" in line and "catalog-entry" in line and "scout" in line

    def test_the_real_run_tags_them_and_the_second_run_is_unchanged(self, ew):
        self._three(ew)
        yaml_text = _manifest("scout-lead", "catalog-entry", "scout", naming="bare")
        res = W.apply(ew, yaml_text, dry_run=False)
        assert res.status == "applied", res
        assert ew.create_calls == []
        for name in ("scout-lead", "catalog-entry", "scout"):
            assert SYSTEM in db.get_agent_tags(name)
        assert sorted(db.get_agent_tags("catalog-entry")) == sorted(["ops", SYSTEM])  # additive
        again = W.apply(ew, yaml_text, dry_run=True)
        assert {m.verdict for m in again.members} == {"unchanged"}
        assert again.adopted == []
        assert not [w for w in again.warnings if "adopted" in w]
        snap = ew.snapshot(yaml_text)
        assert snap.membership.evidence == {"scout-lead": "tag", "catalog-entry": "tag", "scout": "tag"}

    def test_an_extra_declared_tag_is_still_a_tag_missing(self, ew):
        ew.seed_agent("scout-lead", tags=())
        res = W.apply(ew, _manifest("scout-lead", naming="bare", extra="default_tags: [fleet]\n"),
                      dry_run=True)
        (m,) = res.members
        assert [c.code for c in m.changes] == ["member_adopted", "tag_missing"]
        assert next(c for c in m.changes if c.code == "tag_missing").desired == ["fleet"]

    def test_prefixed_mode_keeps_todays_refusal(self, world):
        """The evidence gate for prefix-only members is untouched."""
        world.seed_agent("acme-a", tags=())
        snap = world.snapshot(_manifest("a"))
        (b,) = _blocker(snap, "membership_evidence_prefix_only")
        assert b.member == "acme-a"
        assert snap.members["a"].untagged is False

    def test_prefixed_mode_still_refuses_a_name_claimed_elsewhere(self, world):
        world.seed_agent("acme-extra-w", tags=("acme-extra",))
        snap = world.snapshot(_manifest("extra-w"))
        assert [(b.code, b.member) for b in snap.blockers] == [
            ("member_claimed_by_another_system", "acme-extra-w")]

    def test_an_unmanageable_bare_name_stays_the_uniform_refusal(self, world):
        world.seed_agent("scout-lead", owner=W.BOB, tags=())
        snap = world.snapshot(_manifest("scout-lead", naming="bare"))
        assert [b.code for b in snap.blockers] == ["member_name_unavailable"]
        assert snap.members["scout-lead"].untagged is False

    def test_deploy_of_a_bare_manifest_over_an_existing_name_still_suffixes(self, world):
        world.seed_agent("scout-lead", tags=())
        res = asyncio.run(system_service.deploy_manifest(
            _manifest("scout-lead", naming="bare"), world.user(W.ALICE), dry_run=True))
        assert [a["name"] for a in res.agents_to_create] == ["scout-lead_2"]


# ===========================================================================
# CP0c — export round-trips naming
# ===========================================================================

def _agents(*names):
    return [{"name": n, "template": "local:plain"} for n in names]


def _export(world, names, **kw):
    import yaml
    text = system_service.export_manifest(SYSTEM, _agents(*names), **kw)
    return text, yaml.safe_load(text)


class TestExportNaming:

    def test_all_prefixed_infers_prefixed_and_is_todays_text(self, world):
        for n in ("acme-a", "acme-b"):
            world.seed_agent(n)
        text, doc = _export(world, ["acme-a", "acme-b"])
        assert "naming" not in doc
        assert list(doc["agents"]) == ["a", "b"]
        assert not text.startswith("#")

    def test_none_prefixed_infers_bare_and_round_trips(self, world):
        for n in ("scout-lead", "catalog-entry"):
            world.seed_agent(n)
        db.add_agent_permission("scout-lead", "catalog-entry", W.ALICE)
        text, doc = _export(world, ["scout-lead", "catalog-entry"])
        assert doc["naming"] == "bare"
        assert list(doc["agents"]) == ["scout-lead", "catalog-entry"]
        assert doc["permissions"] == {"explicit": {"scout-lead": ["catalog-entry"]}}
        manifest, _ = _parse(text)
        assert system_service.declared_names(manifest) == {"scout-lead": "scout-lead", "catalog-entry": "catalog-entry"}
        # The round trip: applying the export to the live system it came from
        # changes nothing.
        snap = world.snapshot(text)
        assert snap.blockers == []

    def test_mixed_keeps_todays_omission_and_says_so_in_a_comment(self, world, caplog):
        for n in ("acme-a", "helper", "bot"):
            world.seed_agent(n)
        with caplog.at_level("WARNING"):
            text, doc = _export(world, ["acme-a", "helper", "bot"])
        assert "naming" not in doc and list(doc["agents"]) == ["a"]
        first = text.splitlines()[0]
        assert first == "# unobserved: members omitted (no 'acme-' prefix): bot, helper"
        assert any("OMITTED" in r.getMessage() for r in caplog.records)

    def test_the_override_names_the_mode(self, world):
        for n in ("acme-a", "helper"):
            world.seed_agent(n)
        _, doc = _export(world, ["acme-a", "helper"], naming="bare")
        assert doc["naming"] == "bare"
        assert list(doc["agents"]) == ["acme-a", "helper"]
        _, doc = _export(world, ["scout-lead"], naming="prefixed")
        assert "naming" not in doc and doc["agents"] == {}

    def test_an_unknown_override_is_refused(self, world):
        with pytest.raises(ValueError, match="naming must be 'prefixed' or 'bare'"):
            system_service.export_manifest(SYSTEM, _agents("scout-lead"), naming="flat")


class TestExportPermissions:
    """The exported edges are the stored ones. `export_manifest` read them
    through a facade method that does not exist, so every export dropped
    `permissions:` behind a log line."""

    def _edges(self, doc):
        return {k: sorted(v) for k, v in (doc.get("permissions") or {}).get("explicit", {}).items()}

    def test_explicit_edges_equal_the_stored_ones(self, world):
        names = ["acme-a", "acme-b", "acme-c"]
        for n in names:
            world.seed_agent(n)
        for src, dst in (("acme-a", "acme-b"), ("acme-a", "acme-c"), ("acme-c", "acme-a")):
            db.add_agent_permission(src, dst, W.ALICE)
        db.add_agent_permission("acme-b", "outsider", W.ALICE)  # not a member: not exported
        text, doc = _export(world, names)
        stored = {n[len("acme-"):]: sorted(t[len("acme-"):] for t in db.get_permitted_agents(n)
                                           if t in names)
                  for n in names if [t for t in db.get_permitted_agents(n) if t in names]}
        assert self._edges(doc) == stored == {"a": ["b", "c"], "c": ["a"]}
        assert not text.startswith("#")

    def test_a_full_mesh_exports_the_preset(self, world):
        names = ["acme-a", "acme-b"]
        for n in names:
            world.seed_agent(n)
        db.add_agent_permission("acme-a", "acme-b", W.ALICE)
        db.add_agent_permission("acme-b", "acme-a", W.ALICE)
        _, doc = _export(world, names)
        assert doc["permissions"] == {"preset": "full-mesh"}

    def test_a_failed_read_is_named_in_the_text_never_a_silent_empty_block(self, world, monkeypatch, caplog):
        for n in ("acme-a", "acme-b"):
            world.seed_agent(n)
        db.add_agent_permission("acme-a", "acme-b", W.ALICE)
        def boom(_n):
            raise RuntimeError("db locked")
        monkeypatch.setattr(db, "get_permitted_agents", boom)
        with caplog.at_level("WARNING"):
            text, doc = _export(world, ["acme-a", "acme-b"])
        assert "permissions" not in doc
        assert text.splitlines()[0] == "# unobserved: could not be read: permissions"
        assert any("Failed to export permissions" in r.getMessage() for r in caplog.records)


class TestExportRoute:
    """`GET /api/systems/{system}/manifest?naming=` through the real router."""

    def _client(self, world, monkeypatch, names):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from routers import agents as agents_router
        from routers import systems

        monkeypatch.setattr(agents_router, "get_accessible_agents",
                            lambda user: _agents(*names))
        app = FastAPI()
        app.include_router(systems.router)
        app.dependency_overrides[systems.get_current_user] = lambda: world.user(W.ALICE)
        return TestClient(app, raise_server_exceptions=False)

    def test_the_query_overrides_and_absent_infers(self, world, monkeypatch):
        for n in ("acme-a", "helper"):
            world.seed_agent(n)
        client = self._client(world, monkeypatch, ["acme-a", "helper"])
        r = client.get(f"/api/systems/{SYSTEM}/manifest", params={"naming": "bare"})
        assert r.status_code == 200 and "naming: bare" in r.text
        assert "acme-a:" in r.text and "helper:" in r.text
        r = client.get(f"/api/systems/{SYSTEM}/manifest")
        assert r.status_code == 200 and r.text.startswith("# unobserved:")

    def test_a_bad_query_is_a_named_400(self, world, monkeypatch):
        world.seed_agent("acme-a")
        client = self._client(world, monkeypatch, ["acme-a"])
        r = client.get(f"/api/systems/{SYSTEM}/manifest", params={"naming": "flat"})
        assert r.status_code == 400
        assert "naming must be 'prefixed' or 'bare'" in r.json()["detail"]


# ===========================================================================
# E5 — the executor's phase order is a pinned tuple, not dict-insertion luck
# ===========================================================================

def test_apply_op_phase_order():
    assert system_service._APPLY_OP_PHASES == (
        "tag", "edge", "folders", "resources", "created_resources",
        "schedule_create", "schedule_update", "created_schedules",
        "skill_assign", "skill_set_assign",  # CP1
        "grant",  # CP2
    )
