"""trinity-enterprise#530 — skill sets: a named slice of the library, assigned as a unit.

What these tests pin, and why each exists:

1. the catalog parser — total (never raises), both forms, every problem a CODE;
   a set naming a skill its source does not ship stays listed as ``partial``;
2. ``plan_member_rows`` — the one pure rule every mutation ends in, and its
   FAIL-CLOSED half: an unresolvable set removes nothing (a catalog hiccup must
   never strip a fleet's skills);
3. the catalog read over a REAL git repo — ``sets`` shares the guarded
   ``catalog.yaml`` load with ``skills_root``;
4. the materialised rows over a REAL SQLite file — overlap (each assignment
   stands on its own), unassign keeps individual + other-set members, upstream
   add/remove, the bulk-PUT round trip a legacy client does;
5. the service — resolution precedence, shadowing, named refusals, the honest
   per-agent status (#342);
6. both migration tracks, the single Alembic head, the cascade registration;
7. the routes, through a real FastAPI app — static-before-catch-all, named
   404/422, the "retained via set" answer, a skills-only PUT leaves sets alone;
8. the inject paths — fleet re-inject prunes a member dropped upstream;
9. AC3 — the CLAUDE.md "via <set>" annotation.

Every test EXECUTES the path; none reads source text.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

AGENT = "acme-bot"


@pytest.fixture(autouse=True)
def _real_modules_not_stubs(monkeypatch):
    """Evict another module's import-time `sys.modules` stubs (the ent#332 harness)."""
    import importlib

    for name in ("utils.url_validation", "utils.safe_yaml", "services.skill_service",
                 "services.skill_source_clone", "services.skill_packaging"):
        mod = sys.modules.get(name)
        if mod is not None and getattr(mod, "__file__", None) is None:
            monkeypatch.delitem(sys.modules, name, raising=False)
    try:
        importlib.import_module("utils.url_validation")
    except Exception:  # noqa: BLE001
        pass
    yield


# =============================================================================
# 1. the parser
# =============================================================================

class TestParser:
    SKILLS = {"backlog", "groom", "roadmap", "project-init", "project-task"}

    def _parse(self, raw):
        from services.skill_sets import parse_catalog_sets
        return {s.name: s for s in parse_catalog_sets(raw, self.SKILLS)}

    def test_no_sets_block_yields_none(self):
        from services.skill_sets import parse_catalog_sets
        for raw in (None, [], "x", 3):
            assert parse_catalog_sets(raw, self.SKILLS) == []

    def test_short_and_long_form(self):
        got = self._parse({
            "project-management": ["project-init", "project-task"],
            "dev-backlog": {
                "skills": ["backlog", "groom", "roadmap"],
                "requires": {"env": ["GITHUB_TOKEN"]},
                "schedules": [{"name": "Weekly groom", "cron": "0 9 * * 1", "message": "/groom"}],
            },
        })
        assert got["project-management"].members == ["project-init", "project-task"]
        assert got["project-management"].status == "ok"
        dev = got["dev-backlog"]
        assert dev.members == ["backlog", "groom", "roadmap"]
        assert dev.requires_env == ["GITHUB_TOKEN"]
        assert dev.schedules == [{"name": "Weekly groom", "cron": "0 9 * * 1", "message": "/groom"}]
        assert dev.problems == []

    def test_missing_member_is_partial_and_named(self):
        s = self._parse({"x": ["backlog", "claim"]})["x"]
        assert (s.status, s.missing, s.problems) == ("partial", ["claim"], ["member_missing"])

    def test_problems_are_codes_never_author_text(self):
        got = self._parse({
            "groom": ["backlog"],                                  # collides with a skill
            "bad-member": ["backlog", "../etc", 7],
            "bad-body": "not a list",
            "bad-env": {"skills": ["backlog"], "requires": {"env": ["lower", "OK_KEY"]}},
            "bad-sched": {"skills": ["backlog"], "schedules": [{"name": "n", "cron": "* *", "message": "m"}]},
        })
        assert got["groom"].problems == ["name_collides_with_skill"]
        assert got["bad-member"].problems == ["invalid_member_name"]
        assert got["bad-member"].members == ["backlog"]
        assert got["bad-body"].problems == ["invalid_set"]
        assert got["bad-env"].requires_env == ["OK_KEY"] and got["bad-env"].problems == ["invalid_env"]
        assert got["bad-sched"].schedules == [] and got["bad-sched"].problems == ["invalid_schedule"]

    def test_unaddressable_set_names_are_dropped_and_bounds_hold(self):
        from services.skill_sets import MAX_MEMBERS, MAX_SETS
        got = self._parse({"../x": ["backlog"], "ok": ["backlog"]})
        assert list(got) == ["ok"]
        many = self._parse({f"s{i}": ["backlog"] for i in range(MAX_SETS + 5)})
        assert len(many) == MAX_SETS
        big = self._parse({"big": [f"m{i}" for i in range(MAX_MEMBERS + 3)]})["big"]
        assert len(big.members) == MAX_MEMBERS and "too_many" in big.problems

    def test_split_set_ref(self):
        from services.skill_sets import split_set_ref
        assert split_set_ref("set:dev-backlog") == "dev-backlog"
        assert split_set_ref("dev-backlog") is None
        assert split_set_ref("set:../x") is None


# =============================================================================
# 2. the pure rule
# =============================================================================

class TestPlanMemberRows:
    def _plan(self, rows, assigned, resolved):
        from services.skill_sets import plan_member_rows
        return plan_member_rows(rows, assigned, resolved)

    def test_adds_missing_members_and_removes_orphaned_set_rows(self):
        add, remove = self._plan({"a": True, "old": False}, ["s"], {"s": ["a", "b"]})
        assert (add, remove) == ({"b"}, {"old"})

    def test_individual_rows_are_never_removed(self):
        _, remove = self._plan({"a": True}, [], {})
        assert remove == set()

    def test_a_member_two_sets_share_stays_while_either_holds_it(self):
        _, remove = self._plan({"m": False}, ["s2"], {"s2": ["m"]})
        assert remove == set()

    def test_fails_closed_while_any_set_is_unresolved(self):
        add, remove = self._plan({"a": False, "b": False}, ["s", "gone"], {"s": ["a", "c"], "gone": None})
        assert add == {"c"}           # a resolvable set still ADDS
        assert remove == set()        # but nothing is removed


# =============================================================================
# 3. the catalog read over a real git repo
# =============================================================================

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _mkrepo(root: Path, name: str, skills, catalog: str | None) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    for skill in skills:
        d = repo / "skills" / skill
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {skill}\ndescription: {skill}\n---\nbody\n")
    if catalog is not None:
        (repo / "catalog.yaml").write_text(catalog)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "seed")
    return repo


def _clone_of(repo: Path, tmp_path: Path, source_id="src_aaaaaaaa"):
    from services.skill_source_clone import SkillSourceClone
    clone = SkillSourceClone(source_id, str(repo), "main", "branch", tmp_path / "clones")
    assert clone.sync(str(repo)).get("success")
    return clone


class TestCatalogRead:
    def test_sets_share_the_guarded_catalog_with_skills_root(self, tmp_path):
        repo = _mkrepo(tmp_path, "lib", ["backlog", "groom"], (
            "schema_version: 1\nskills_root: skills\n"
            "sets:\n  dev-backlog: [backlog, groom, claim]\n"
        ))
        clone = _clone_of(repo, tmp_path)
        assert clone.skills_rel_root() == "skills"
        (s,) = clone.declared_sets()
        assert (s.name, s.members, s.missing) == ("dev-backlog", ["backlog", "groom", "claim"], ["claim"])

    def test_a_catalog_without_sets_and_no_catalog_yield_none(self, tmp_path):
        with_catalog = _clone_of(_mkrepo(tmp_path, "a", ["x"], "schema_version: 1\n"), tmp_path, "src_aaaaaaa1")
        without = _clone_of(_mkrepo(tmp_path, "b", ["x"], None), tmp_path, "src_bbbbbbb1")
        assert with_catalog.declared_sets() == [] and without.declared_sets() == []

    def test_an_unusable_catalog_yields_none_never_raises(self, tmp_path):
        clone = _clone_of(_mkrepo(tmp_path, "c", ["x"], "schema_version: 2\nsets: {s: [x]}\n"), tmp_path)
        assert clone.declared_sets() == []


# =============================================================================
# 4. the rows, over a real SQLite file
# =============================================================================

@pytest.fixture
def real_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    import db.tables as tables
    import db.skills as skills_mod
    import db.skill_sets as sets_mod
    engine = create_engine(f"sqlite:///{tmp_path / 'sets.db'}")
    tables.agent_skills.create(engine)
    tables.agent_skill_sets.create(engine)
    monkeypatch.setattr(skills_mod, "get_engine", lambda: engine)
    monkeypatch.setattr(sets_mod, "get_engine", lambda: engine)
    monkeypatch.setattr(sets_mod, "is_sqlite", lambda: True)
    return SimpleNamespace(skills=skills_mod.SkillsOperations(), sets=sets_mod.SkillSetsOperations())


def _rows(real_db):
    return {s.skill_name: s.individual for s in real_db.skills.get_agent_skills(AGENT)}


class TestRows:
    def test_assigning_a_set_materialises_its_members_as_set_derived(self, real_db):
        real_db.skills.assign_skill(AGENT, "backlog", "alice")
        created, add, remove = real_db.sets.assign_set(
            AGENT, "dev", "src", "alice", None, {"dev": ["backlog", "groom"]})
        assert (created, add, remove) == (True, ["groom"], [])
        assert _rows(real_db) == {"backlog": True, "groom": False}
        again = real_db.sets.assign_set(AGENT, "dev", "src", "alice", None, {"dev": ["backlog", "groom"]})
        assert again == (False, [], [])                                  # idempotent

    def test_unassign_keeps_individual_and_other_set_members(self, real_db):
        real_db.skills.assign_skill(AGENT, "backlog", "alice")
        real_db.sets.assign_set(AGENT, "dev", "src", "alice", None, {"dev": ["backlog", "groom", "claim"]})
        real_db.sets.assign_set(AGENT, "pm", "src", "alice", None,
                                {"dev": ["backlog", "groom", "claim"], "pm": ["claim", "plan"]})
        existed, _, removed = real_db.sets.unassign_set(AGENT, "dev", {"pm": ["claim", "plan"]}, "alice")
        assert existed and removed == ["groom"]
        assert _rows(real_db) == {"backlog": True, "claim": False, "plan": False}
        _, _, removed = real_db.sets.unassign_set(AGENT, "pm", {}, "alice")
        assert removed == ["claim", "plan"]
        assert _rows(real_db) == {"backlog": True}

    def test_upstream_add_and_remove_are_reconciled(self, real_db):
        real_db.sets.assign_set(AGENT, "dev", "src", "alice", None, {"dev": ["a", "b"]})
        add, remove = real_db.sets.reconcile(AGENT, {"dev": ["a", "c"]})
        assert (add, remove) == (["c"], ["b"])
        assert _rows(real_db) == {"a": False, "c": False}

    def test_an_unresolvable_set_prunes_nothing(self, real_db):
        """C1: a disabled source / unreadable catalog must never strip a fleet."""
        real_db.sets.assign_set(AGENT, "dev", "src", "alice", None, {"dev": ["a", "b"]})
        real_db.sets.assign_set(AGENT, "pm", "src", "alice", None, {"dev": ["a", "b"], "pm": ["c"]})
        add, remove = real_db.sets.reconcile(AGENT, {"dev": None, "pm": []})
        assert (add, remove) == ([], [])
        assert _rows(real_db) == {"a": False, "b": False, "c": False}

    def test_the_bulk_put_round_trip_neither_drops_nor_promotes_set_members(self, real_db):
        """P1/P2: a legacy client reads every name and writes them back."""
        real_db.skills.assign_skill(AGENT, "solo", "alice")
        real_db.sets.assign_set(AGENT, "dev", "src", "alice", None, {"dev": ["a", "b"]})
        names_ab = lambda held: {"a", "b"}
        real_db.skills.set_agent_skills(AGENT, ["solo", "a", "b"], "alice", set_resolver=names_ab)
        assert _rows(real_db) == {"solo": True, "a": False, "b": False}
        # A skills-only list that omits the members does not drop them either.
        real_db.skills.set_agent_skills(AGENT, ["solo"], "alice", set_resolver=names_ab)
        assert _rows(real_db) == {"solo": True, "a": False, "b": False}
        # And without set_named (no sets) the pre-ent#530 replace semantics hold.
        real_db.skills.set_agent_skills(AGENT, ["solo"], "alice")
        assert _rows(real_db) == {"solo": True}

    def test_replace_sets_and_set_individual(self, real_db):
        real_db.sets.assign_set(AGENT, "dev", "src", "alice", None, {"dev": ["a"]})
        add, remove = real_db.sets.replace_sets(AGENT, ["pm"], {"pm": "src"}, "alice", None, {"pm": ["b"]})
        assert (add, remove) == (["b"], ["a"])
        assert real_db.sets.agent_set_names(AGENT) == ["pm"]
        assert real_db.sets.set_individual(AGENT, "b", True)
        _, _, removed = real_db.sets.unassign_set(AGENT, "pm", {}, "alice")
        assert removed == [] and _rows(real_db) == {"b": True}           # promoted rows stay

    def test_the_record_names_who_assigned_it(self, real_db):
        real_db.sets.assign_set(AGENT, "dev", "src", "owner", "trinity-pm", {"dev": ["a"]})
        (row,) = real_db.sets.list_agent_sets(AGENT)
        assert (row["assigned_by"], row["assigned_by_agent"], row["source_id"]) == ("owner", "trinity-pm", "src")


# =============================================================================
# 5. the service
# =============================================================================

def _setdef(name, members, missing=(), env=(), schedules=()):
    from services.skill_sets import SetDef
    return SetDef(name, list(members), list(missing), list(env), list(schedules),
                  ["member_missing"] if missing else [])


class _FakeClone:
    def __init__(self, source_id, sets, shas):
        self.source_id, self._sets, self._shas = source_id, sets, shas

    def declared_sets(self):
        return self._sets

    def tree_shas(self):
        return self._shas


@pytest.fixture
def fake_library(monkeypatch):
    """Two sources, custom first (precedence): both declare `dev`; `shared`
    resolves from custom although the default source's set names it."""
    import services.skill_set_service as svc_mod
    custom = _FakeClone("custom", [_setdef("dev", ["backlog"])], {"backlog": "c-sha", "shared": "c-shared"})
    default = _FakeClone("default", [
        _setdef("dev", ["backlog", "groom"]),
        _setdef("pm", ["plan", "shared"], env=["GITHUB_TOKEN"],
                schedules=[{"name": "n", "cron": "0 9 * * 1", "message": "/plan"}]),
        _setdef("broken", ["plan", "ghost"], missing=["ghost"]),
    ], {"backlog": "d-sha", "groom": "g-sha", "plan": "p-sha", "shared": "d-shared"})
    resolution = {
        "backlog": {"source_id": "custom", "clone": custom},
        "shared": {"source_id": "custom", "clone": custom},
        "groom": {"source_id": "default", "clone": default},
        "plan": {"source_id": "default", "clone": default},
    }
    fake = SimpleNamespace(
        _clones=lambda enabled_only=True: [custom, default],
        _library_fingerprint=lambda clones: None,             # never cached in tests
        _source_names=lambda: {"custom": "Custom", "default": "Default"},
        _resolution=lambda: resolution,
        list_skills=lambda: [{"name": "plan", "requires": {"env": ["JIRA_KEY"]}},
                             {"name": "groom", "requires": {"env": ["NOT_IN_PM"]}}],
        _probe_dependencies=AsyncMock(return_value={"env": {"GITHUB_TOKEN": False, "JIRA_KEY": True}}),
    )
    monkeypatch.setattr(svc_mod, "_skill_service", lambda: fake)
    return fake


class TestService:
    def test_precedence_shadowing_and_member_provenance(self, fake_library):
        from services import skill_set_service as svc
        lib = svc.library_sets()
        assert lib["dev"]["source_id"] == "custom"
        assert lib["dev"]["shadowed_by"] == [{"source_id": "default", "source_name": "Default"}]
        pm = {m["name"]: m for m in lib["pm"]["members"]}
        assert pm["plan"] == {"name": "plan", "present": True, "version": "p-sha", "shadowed_source": None}
        assert pm["shared"]["shadowed_source"] == "custom" and pm["shared"]["version"] == "c-shared"
        assert lib["broken"]["status"] == "partial" and lib["broken"]["problems"] == ["member_missing"]
        assert lib["pm"]["requires"] == {"env": ["GITHUB_TOKEN"]}

    def test_named_refusals_never_a_partial_assign(self, fake_library):
        from services import skill_set_service as svc
        with pytest.raises(svc.SetError) as e:
            svc.require_set("nope")
        assert (e.value.status_code, e.value.detail["code"]) == (404, "unknown_set")
        with pytest.raises(svc.SetError) as e:
            svc.require_set("broken")
        assert (e.value.status_code, e.value.detail["code"], e.value.detail["missing"]) == (
            422, "set_member_missing", ["ghost"])

    def test_resolved_members_fails_closed_on_an_unknown_set(self, fake_library):
        from services import skill_set_service as svc
        assert svc.resolved_members(["dev", "gone"]) == {"dev": ["backlog"], "gone": None}

    def test_strip_set_prefix(self):
        from services.skill_set_service import strip_set_prefix
        assert strip_set_prefix(["a", "set:dev", "b"]) == (["a", "b"], ["dev"])

    def test_the_honest_status_of_an_assigned_set(self, fake_library, real_db, monkeypatch):
        """#342: partial is never silently 'on'; prerequisites include members'
        own env; probed only while running, `unknown` otherwise."""
        from database import db
        from services import skill_set_service as svc
        monkeypatch.setattr(db, "_skills_ops", real_db.skills, raising=False)
        monkeypatch.setattr(db, "_skill_sets_ops", real_db.sets, raising=False)
        real_db.sets.assign_set(AGENT, "pm", "default", "alice", None, {"pm": ["plan", "shared"]})
        real_db.sets.assign_set(AGENT, "gone", "default", "alice", None, {"pm": ["plan", "shared"], "gone": None})
        real_db.skills.unassign_skill(AGENT, "shared")                   # half-assigned

        monkeypatch.setattr(svc, "_is_running", AsyncMock(return_value=False))
        status = {s["name"]: s for s in asyncio.run(svc.agent_set_status(AGENT, probe=True))}
        assert status["gone"]["status"] == "unresolved"
        pm = status["pm"]
        assert pm["status"] == "partial"
        assert {m["name"]: m["state"] for m in pm["members"]} == {"plan": "assigned", "shared": "not_assigned"}
        assert pm["prerequisites"] == {"state": "unknown", "missing_env": []}
        assert pm["suggested_schedules"] == [{"name": "n", "cron": "0 9 * * 1", "message": "/plan"}]

        monkeypatch.setattr(svc, "_is_running", AsyncMock(return_value=True))
        pm = {s["name"]: s for s in asyncio.run(svc.agent_set_status(AGENT, probe=True))}["pm"]
        assert pm["prerequisites"] == {"state": "missing", "missing_env": ["GITHUB_TOKEN"]}
        env_asked = fake_library._probe_dependencies.await_args.args[2]
        assert set(env_asked) == {"GITHUB_TOKEN", "JIRA_KEY"}   # the set's own ∪ its members', no others


# =============================================================================
# 6. both tracks, the head, the cascade
# =============================================================================

def test_the_sqlite_migration_builds_the_table_and_the_column(tmp_path):
    import sqlite3
    from db.migrations import MIGRATIONS, _migrate_agent_skill_sets
    conn = sqlite3.connect(tmp_path / "m.db")
    cur = conn.cursor()
    cur.execute("CREATE TABLE agent_skills (id INTEGER PRIMARY KEY, agent_name TEXT, skill_name TEXT)")
    cur.execute("INSERT INTO agent_skills (agent_name, skill_name) VALUES ('a', 'legacy')")
    _migrate_agent_skill_sets(cur, conn)
    _migrate_agent_skill_sets(cur, conn)                                  # idempotent
    assert cur.execute("SELECT individual FROM agent_skills").fetchone() == (1,)   # additive-safe
    assert {r[1] for r in cur.execute("PRAGMA table_info(agent_skill_sets)")} == {
        "agent_name", "set_name", "source_id", "assigned_by", "assigned_by_agent", "assigned_at"}
    assert MIGRATIONS[-1] == ("agent_skill_sets", _migrate_agent_skill_sets)


def test_the_alembic_revision_extends_the_single_head():
    import importlib.util
    root = _BACKEND / "migrations" / "versions"
    spec = importlib.util.spec_from_file_location("rev530", root / "0075_agent_skill_sets.py")
    rev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rev)
    assert (rev.revision, rev.down_revision) == ("0075_agent_skill_sets", "0074_role_readiness_rollout_seed")
    script = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "check_alembic_heads.py"
    proc = subprocess.run([sys.executable, str(script), str(root)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_alembic_upgrade_runs_against_a_real_database(tmp_path):
    """Executed through Alembic's op layer (SQLite dialect): table + column, idempotent guard."""
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text
    spec = importlib.util.spec_from_file_location(
        "rev530b", _BACKEND / "migrations" / "versions" / "0075_agent_skill_sets.py")
    rev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rev)
    engine = create_engine(f"sqlite:///{tmp_path / 'a.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE agent_skills (id INTEGER PRIMARY KEY, agent_name TEXT, skill_name TEXT)"))
        conn.execute(text("INSERT INTO agent_skills (agent_name, skill_name) VALUES ('a', 'legacy')"))
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            rev.upgrade()
            rev.upgrade()
        assert conn.execute(text("SELECT individual FROM agent_skills")).scalar() == 1
        assert "agent_skill_sets" in inspect(conn).get_table_names()


def test_sets_follow_the_agent_through_rename_and_delete():
    from db.agent_cleanup import AGENT_REFS, Policy
    refs = {(r.table, r.column): r.policy for r in AGENT_REFS}
    assert refs[("agent_skill_sets", "agent_name")] is Policy.CASCADE


# =============================================================================
# 7. the routes, through a real FastAPI app
# =============================================================================

@pytest.fixture
def app_client(real_db, fake_library, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import skills
    from services import skill_set_service as svc

    monkeypatch.setattr(skills.db, "_skills_ops", real_db.skills, raising=False)
    monkeypatch.setattr(skills.db, "_skill_sets_ops", real_db.sets, raising=False)
    monkeypatch.setattr(skills.db, "get_agent_skill_names",
                        lambda a: [s.skill_name for s in real_db.skills.get_agent_skills(a)])
    monkeypatch.setattr(skills.skill_service, "deliver_assigned", AsyncMock(return_value={"status": "injected"}))
    monkeypatch.setattr(skills.skill_service, "remove_skills", AsyncMock(return_value={"success": True}))
    monkeypatch.setattr(skills.skill_service, "get_skill", lambda n: {"name": n})
    monkeypatch.setattr(skills, "broadcast_skills_changed", AsyncMock())
    monkeypatch.setattr(svc, "_is_running", AsyncMock(return_value=False))

    app = FastAPI()
    app.include_router(skills.router)
    human = SimpleNamespace(id=1, username="alice", email="a@example.com", role="admin",
                            mcp_scope=None, agent_name=None, connector_agent=None, mcp_key_id=None)
    app.dependency_overrides[skills.get_current_user] = lambda: human
    app.dependency_overrides[skills.get_skill_managed_agent_by_name] = lambda agent_name: agent_name
    app.dependency_overrides[skills.get_authorized_agent_by_name] = lambda agent_name: agent_name
    return TestClient(app)


class TestRoutes:
    def test_library_sets_is_not_read_as_a_skill_name(self, app_client):
        """Invariant #4: registered before `/skills/library/{skill_name}`."""
        r = app_client.get("/api/skills/library/sets")
        assert r.status_code == 200
        assert {s["name"] for s in r.json()} == {"dev", "pm", "broken"}

    def test_assign_unknown_and_partial_sets_are_named_refusals(self, app_client, real_db):
        r = app_client.post(f"/api/agents/{AGENT}/skill-sets/nope")
        assert (r.status_code, r.json()["detail"]["code"]) == (404, "unknown_set")
        r = app_client.post(f"/api/agents/{AGENT}/skill-sets/broken")
        assert (r.status_code, r.json()["detail"]["code"]) == (422, "set_member_missing")
        assert _rows(real_db) == {}                                      # nothing half-assigned

    def test_assign_then_list_then_unassign(self, app_client, real_db):
        r = app_client.post(f"/api/agents/{AGENT}/skill-sets/pm")
        body = r.json()
        assert r.status_code == 200 and body["members_added"] == ["plan", "shared"]
        assert body["suggested_schedules"] == [{"name": "n", "cron": "0 9 * * 1", "message": "/plan"}]
        assert body["status"]["status"] == "ok"
        rows = {s["skill_name"]: s for s in app_client.get(f"/api/agents/{AGENT}/skills").json()}
        assert rows["plan"]["individual"] is False and rows["plan"]["via_sets"] == ["pm"]
        assert [s["name"] for s in app_client.get(f"/api/agents/{AGENT}/skill-sets").json()] == ["pm"]
        r = app_client.delete(f"/api/agents/{AGENT}/skill-sets/pm")
        assert r.json()["members_removed"] == ["plan", "shared"]
        assert _rows(real_db) == {}

    def test_unassigning_a_set_named_skill_individually_says_retained(self, app_client, real_db):
        app_client.post(f"/api/agents/{AGENT}/skills/plan")              # individual first
        app_client.post(f"/api/agents/{AGENT}/skill-sets/pm")
        assert _rows(real_db)["plan"] is True
        r = app_client.delete(f"/api/agents/{AGENT}/skills/plan")
        assert r.json()["removed"] is False and r.json()["retained_via_sets"] == ["pm"]
        assert _rows(real_db)["plan"] is False                          # now the set's
        app_client.delete(f"/api/agents/{AGENT}/skill-sets/pm")
        assert "plan" not in _rows(real_db)

    def test_a_skills_only_put_leaves_sets_alone_set_prefix_adds_and_sets_replaces(self, app_client, real_db):
        app_client.post(f"/api/agents/{AGENT}/skill-sets/pm")
        r = app_client.put(f"/api/agents/{AGENT}/skills", json={"skills": ["solo"]})
        assert r.status_code == 200 and r.json()["sets"] == ["pm"]
        assert _rows(real_db) == {"solo": True, "plan": False, "shared": False}
        # `set:` entries only ADD (cso L2) — the held `pm` is untouched.
        r = app_client.put(f"/api/agents/{AGENT}/skills", json={"skills": ["solo", "set:dev"]})
        assert r.json()["sets"] == ["dev", "pm"]
        assert _rows(real_db) == {"solo": True, "backlog": False, "plan": False, "shared": False}
        # The explicit `sets` field is the only full replace.
        r = app_client.put(f"/api/agents/{AGENT}/skills", json={"skills": ["solo"], "sets": ["dev"]})
        assert r.json()["sets"] == ["dev"]
        assert _rows(real_db) == {"solo": True, "backlog": False}

    def test_a_set_prefix_add_is_all_or_nothing(self, app_client, real_db):
        r = app_client.put(f"/api/agents/{AGENT}/skills", json={"skills": ["set:pm", "set:broken"]})
        assert r.status_code == 422
        assert real_db.sets.agent_set_names(AGENT) == [] and _rows(real_db) == {}

    def test_the_status_read_probes_only_when_asked(self, app_client, monkeypatch):
        """cso L1: a plain read is never an in-container exec."""
        from services import skill_set_service as svc
        monkeypatch.setattr(svc, "_is_running", AsyncMock(return_value=True))
        probe = AsyncMock(return_value={"GITHUB_TOKEN": True, "JIRA_KEY": True})
        monkeypatch.setattr(svc, "_probe_env", probe)
        app_client.post(f"/api/agents/{AGENT}/skill-sets/pm")           # the assign response probes
        probe.reset_mock()
        st = app_client.get(f"/api/agents/{AGENT}/skill-sets").json()[0]
        probe.assert_not_awaited()
        assert st["prerequisites"]["state"] == "unknown"
        st = app_client.get(f"/api/agents/{AGENT}/skill-sets?probe=true").json()[0]
        probe.assert_awaited_once()
        assert st["prerequisites"]["state"] == "ok"

    def test_put_with_an_unknown_set_changes_nothing(self, app_client, real_db):
        app_client.post(f"/api/agents/{AGENT}/skill-sets/pm")
        r = app_client.put(f"/api/agents/{AGENT}/skills", json={"skills": [], "sets": ["nope"]})
        assert r.status_code == 404
        assert _rows(real_db) == {"plan": False, "shared": False}


# =============================================================================
# 8. fleet re-inject prunes what the set reconcile dropped
# =============================================================================

def test_fleet_reinject_prunes_a_member_removed_upstream(monkeypatch):
    import services.skills_sync_service as sync_mod
    from services import skill_set_service as svc
    monkeypatch.setattr(svc, "reconcile_agent", lambda agent, lib=None: ([], ["dropped"]))
    monkeypatch.setattr(sync_mod.db, "get_agent_skill_names", lambda a: ["kept"])
    inject = AsyncMock(return_value={"success": True})
    prune = AsyncMock(return_value={})
    monkeypatch.setattr(sync_mod.skill_service, "inject_skills", inject)
    monkeypatch.setattr(sync_mod.skill_service, "reconcile_agent_skills", prune)
    import services.skill_service as ss
    monkeypatch.setattr(ss, "broadcast_skills_changed", AsyncMock())
    asyncio.run(sync_mod.SkillsLibrarySyncService._reinject_agent(AGENT, {"dev": {}}))
    inject.assert_awaited_once()
    prune.assert_awaited_once_with(AGENT, ["kept"])

    prune.reset_mock()
    monkeypatch.setattr(svc, "reconcile_agent", lambda agent, lib=None: ([], []))
    asyncio.run(sync_mod.SkillsLibrarySyncService._reinject_agent(AGENT, {"dev": {}}))
    prune.assert_not_awaited()                                            # nothing dropped, no extra prune


# =============================================================================
# 9. AC3 — why the skill is present, in the agent's own CLAUDE.md
# =============================================================================

def test_claude_md_lines_say_via_which_set():
    from services.skill_service import SkillService
    written = {}

    class _Client:
        async def read_file(self, path):
            return {"success": True, "content": "# Agent\n"}

        async def write_file(self, path, content):
            written["content"] = content
            return {"success": True}

    asyncio.run(SkillService()._update_claude_md_skills_section(
        _Client(), ["groom", "solo"], None, {"groom": ["dev-backlog"]}))
    assert "- `/groom` - Use with /groom command (via dev-backlog)" in written["content"]
    assert "- `/solo` - Use with /solo command\n" in written["content"]


# =============================================================================
# 10. review regressions — every doubt fails closed, nothing drifts silently
# =============================================================================

class TestReviewRegressions:
    def test_a_malformed_set_is_invalid_never_an_empty_ok_set(self):
        """Finding 1: `skill:` for `skills:` used to parse as an ok set with no
        members, which then pruned every member fleet-wide."""
        from services.skill_sets import parse_catalog_sets
        for raw in ({"dev": {"skill": ["a"]}}, {"dev": "a"}, {"dev": []}, {"dev": ["a", "../x"]}):
            (s,) = parse_catalog_sets(raw, {"a"})
            assert s.status == "invalid", raw

    def test_a_non_ok_or_moved_set_resolves_to_none(self, fake_library):
        """Findings 1-3: invalid, partial (an empty skills root reads as all
        members missing) and a name now owned by another source all fail closed."""
        from services import skill_set_service as svc
        from services.skill_sets import SetDef
        lib = svc.library_sets()
        lib["bad"] = {**lib["pm"], "name": "bad", "status": "invalid"}
        got = svc.resolved_members({"pm": "default", "broken": "default", "bad": "default",
                                    "dev": "default", "gone": None}, lib)
        assert got == {"pm": ["plan", "shared"], "broken": None, "bad": None,
                       "dev": None,          # held from default; custom owns the name now
                       "gone": None}
        assert svc.resolved_members({"dev": "custom"}, lib) == {"dev": ["backlog"]}
        assert svc.resolved_members({}, lib) == {}

    def test_an_invalid_set_cannot_be_assigned(self, fake_library, monkeypatch):
        from services import skill_set_service as svc
        from services.skill_sets import SetDef
        fake_library._clones()[1]._sets.append(SetDef("bad", ["plan", "x"], problems=["invalid_member_name"]))
        with pytest.raises(svc.SetError) as e:
            svc.require_set("bad")
        assert (e.value.status_code, e.value.detail["code"]) == (422, "set_invalid")

    def test_a_moved_set_keeps_its_rows_reports_drift_and_reassign_repoints(self, fake_library, real_db, monkeypatch):
        """Finding 3: disabling a custom source must not swap in another source's family."""
        from database import db
        from services import skill_set_service as svc
        monkeypatch.setattr(db, "_skills_ops", real_db.skills, raising=False)
        monkeypatch.setattr(db, "_skill_sets_ops", real_db.sets, raising=False)
        monkeypatch.setattr(svc, "_is_running", AsyncMock(return_value=False))
        real_db.sets.assign_set(AGENT, "dev", "default", "alice", None, {"dev": ["backlog", "groom"]})
        assert svc.reconcile_agent(AGENT) == ([], [])                    # custom owns `dev` now → nothing moves
        assert _rows(real_db) == {"backlog": False, "groom": False}
        (st,) = asyncio.run(svc.agent_set_status(AGENT))
        assert (st["status"], st["reason"], st["drift"]) == ("unresolved", "source_changed", True)
        svc.assign(AGENT, "dev", "alice", None)                          # the operator re-points it
        assert real_db.sets.list_agent_sets(AGENT)[0]["source_id"] == "custom"
        assert _rows(real_db) == {"backlog": False}

    def test_unticking_an_individual_set_member_demotes_it_never_deletes_it(self, real_db):
        """Finding 4: the PUT must follow the single-unassign rule."""
        real_db.skills.assign_skill(AGENT, "x", "alice")                 # individual
        real_db.sets.assign_set(AGENT, "s", "src", "alice", None, {"s": ["x", "y"]})
        written = {}
        real_db.skills.set_agent_skills(AGENT, [], "alice", set_resolver=lambda held: {"x", "y"},
                                        result=written)
        assert _rows(real_db) == {"x": False, "y": False}
        assert sorted(written["names"]) == ["x", "y"]
        # With the set unresolved, set-derived rows are kept and an unlisted
        # individual row goes (it cannot be known to be set-named).
        real_db.skills.assign_skill(AGENT, "solo", "alice")
        real_db.skills.set_agent_skills(AGENT, [], "alice", set_resolver=lambda held: None)
        assert _rows(real_db) == {"x": False, "y": False}

    def test_the_replace_resolves_sets_inside_its_own_transaction(self, real_db):
        """Finding 5: the held sets are read in the replace's transaction, so a
        set assigned after the caller looked is still honoured."""
        from services.skill_set_service import set_resolver
        real_db.sets.assign_set(AGENT, "late", "src", "alice", None, {"late": ["m"]})
        seen = []
        real_db.skills.set_agent_skills(
            AGENT, ["solo"], "alice", set_resolver=lambda held: seen.append(dict(held)) or {"m"})
        assert seen == [{"late": "src"}]
        assert _rows(real_db) == {"solo": True, "m": False}
        # A stale (empty) library snapshot resolves nothing → fails closed, keeps `m`.
        real_db.skills.set_agent_skills(AGENT, [], "alice", set_resolver=set_resolver({}))
        assert _rows(real_db) == {"m": False}

    def test_the_sqlite_lock_is_taken_before_the_reads(self, tmp_path, monkeypatch):
        """Finding 9: a second writer is blocked from the moment the lock is taken."""
        import sqlite3
        from sqlalchemy import create_engine
        import db.tables as tables
        import db.skill_sets as sets_mod
        path = tmp_path / "lock.db"
        engine = create_engine(f"sqlite:///{path}")
        tables.agent_skill_sets.create(engine)
        monkeypatch.setattr(sets_mod, "is_sqlite", lambda: True)
        with engine.begin() as conn:
            sets_mod.lock_agent_rows(conn, AGENT)
            other = sqlite3.connect(path, timeout=0.1)
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("INSERT INTO agent_skill_sets (agent_name, set_name, assigned_by, assigned_at) "
                              "VALUES ('b', 's', 'x', 'y')")
            other.close()


class TestReviewRouteRegressions:
    def test_unassigning_a_set_derived_skill_while_its_set_is_unresolved_is_refused(self, app_client, real_db):
        """Finding 7: the delete would be silently undone by the next reconcile."""
        real_db.sets.assign_set(AGENT, "gone", "default", "alice", None, {"gone": ["orphan"]})
        r = app_client.delete(f"/api/agents/{AGENT}/skills/orphan")
        assert (r.status_code, r.json()["detail"]["code"]) == (409, "skill_set_unresolved")
        assert _rows(real_db) == {"orphan": False}

    def test_a_set_unassign_behind_an_unresolved_set_says_deferred(self, app_client, real_db):
        """Finding 8: never a silent success that removed nothing."""
        app_client.post(f"/api/agents/{AGENT}/skill-sets/pm")
        real_db.sets.assign_set(AGENT, "gone", "default", "alice", None,
                                {"pm": ["plan", "shared"], "gone": None})
        body = app_client.delete(f"/api/agents/{AGENT}/skill-sets/pm").json()
        assert body["removal_deferred"] is True and body["members_removed"] == []
        assert _rows(real_db) == {"plan": False, "shared": False}

    def test_unticking_a_set_member_in_the_put_keeps_it_on_the_agent(self, app_client, real_db):
        app_client.post(f"/api/agents/{AGENT}/skills/plan")
        app_client.post(f"/api/agents/{AGENT}/skill-sets/pm")
        body = app_client.put(f"/api/agents/{AGENT}/skills", json={"skills": []}).json()
        assert body["removal"] is None                                   # nothing left the agent
        assert _rows(real_db) == {"plan": False, "shared": False}


# =============================================================================
# 11. ordering on the start and manual-inject paths (review finding 15)
# =============================================================================

def test_agent_start_reconciles_sets_before_reading_the_names(monkeypatch):
    from services.agent_service import lifecycle
    from services import skill_set_service as svc
    calls = []
    monkeypatch.setattr(svc, "reconcile_agent", lambda a, lib=None: calls.append("sets") or ([], []))
    from database import db   # lifecycle imports it lazily, inside the function
    monkeypatch.setattr(db, "get_agent_skill_names", lambda a: calls.append("names") or ["x"])
    monkeypatch.setattr(lifecycle.skill_service, "inject_skills",
                        AsyncMock(side_effect=lambda *a, **k: calls.append("inject") or {"success": True}))
    monkeypatch.setattr(lifecycle.skill_service, "reconcile_agent_skills",
                        AsyncMock(side_effect=lambda *a, **k: calls.append("prune") or {}))
    asyncio.run(lifecycle.inject_assigned_skills(AGENT))
    assert calls[:3] == ["sets", "names", "inject"] and "prune" in calls


def test_manual_inject_reconciles_sets_then_injects_then_prunes(app_client, monkeypatch):
    from routers import skills
    from services import skill_set_service as svc
    calls = []
    monkeypatch.setattr(svc, "reconcile_agent", lambda a, lib=None: calls.append("sets") or ([], []))
    monkeypatch.setattr(skills.skill_service, "inject_skills",
                        AsyncMock(side_effect=lambda *a, **k: calls.append("inject") or {"success": True}))
    monkeypatch.setattr(skills.skill_service, "reconcile_agent_skills",
                        AsyncMock(side_effect=lambda *a, **k: calls.append("prune") or {"removed": []}))
    r = app_client.post(f"/api/agents/{AGENT}/skills/inject")
    assert r.status_code == 200 and calls == ["sets", "inject", "prune"]
    assert r.json()["reconcile"] == {"removed": []}
