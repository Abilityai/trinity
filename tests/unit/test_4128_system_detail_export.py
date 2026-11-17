"""#4128 — the system detail route and the manifest export lose permissions and folders.

Pinned by running the REAL route (`routers.systems.get_system`) and the REAL
exporter (`system_service.export_manifest`) over the real `DatabaseManager`
(`db_harness.db_backend`), seeded through the real writers. No facade method is
stubbed: the bug hid because two tests stubbed a method the facade does not
have, and `monkeypatch.setattr(..., raising=False)` created it.

* the detail route called `db.get_agent_permissions` / `db.get_agent_folder_config`,
  neither of which exists, inside ONE `try` — so `permissions`, `folders` and
  `schedules` were all absent for every member;
* the export's folder read had the same missing name, and subscripted the
  pydantic row as a dict besides;
* the export's permission block probed `agents[0]`: a system whose first
  member has no outgoing edge exported no edges at all.

Related flow: docs/memory/feature-flows/system-manifest.md
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from pathlib import Path

import pytest
import yaml

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402,F401

pytest.importorskip("docker", reason="backend venv required (system_service chain)")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _ent695_world as W  # noqa: E402
from database import db  # noqa: E402
from db_models import ScheduleCreate  # noqa: E402
from services import system_service  # noqa: E402

pytestmark = pytest.mark.unit

SYSTEM = W.SYSTEM
A, B, C = f"{SYSTEM}-a", f"{SYSTEM}-b", f"{SYSTEM}-c"


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    w = W.make_world(monkeypatch, tmp_path)
    for name in (A, B, C):
        w.seed_agent(name)
    return w


def _row(name):
    return {"name": name, "status": "running", "template": "local:plain"}


def _detail(monkeypatch, user, roster):
    """The real route, with only the Docker-backed roster read replaced."""
    routers_systems = importlib.import_module("routers.systems")
    routers_agents = importlib.import_module("routers.agents")
    monkeypatch.setattr(routers_agents, "get_accessible_agents", lambda _u: [_row(n) for n in roster])
    out = asyncio.run(routers_systems.get_system(SYSTEM, current_user=user))
    return {a["name"]: a for a in out["agents"]}


def _export(names, viewer):
    text = system_service.export_manifest(SYSTEM, [_row(n) for n in names], viewer=viewer)
    return text, yaml.safe_load(text)


# ---------------------------------------------------------------------------
# GET /api/systems/{name}
# ---------------------------------------------------------------------------

class TestDetailRoute:
    def test_permissions_folders_and_schedules_are_present(self, world, monkeypatch, caplog):
        db.add_agent_permission(A, B, W.ALICE)
        db.upsert_shared_folder_config(B, expose_enabled=True, consume_enabled=False)
        caplog.set_level(logging.WARNING)
        agents = _detail(monkeypatch, world.user(W.ALICE), [A, B, C])
        assert agents[A]["permissions"] == [B]
        assert agents[B]["permissions"] == []
        assert agents[B]["folders"] == {"expose": True, "consume": False}
        assert "folders" not in agents[A]  # no row: no key
        assert all("schedules" in a for a in agents.values())
        assert "Failed to get" not in caplog.text

    def test_edge_targets_are_what_the_direct_permissions_route_shows(self, world, monkeypatch):
        # `outsider` belongs to another person: the caller cannot reach it.
        db.register_agent_owner("outsider", W.BOB)
        # `other` is the caller's own agent outside the system: reachable.
        db.register_agent_owner("other", W.ALICE)
        for target in (B, "outsider", "other"):
            db.add_agent_permission(A, target, W.ALICE)
        roster = [A, B, C, "other"]
        agents = _detail(monkeypatch, world.user(W.ALICE), roster)
        assert agents[A]["permissions"] == sorted([B, "other"])

        # Parity with the standard, not a second hand-written rule.
        from services.agent_service import permissions as perms_logic
        monkeypatch.setattr(perms_logic, "get_accessible_agents", lambda _u: [_row(n) for n in roster])
        monkeypatch.setattr(perms_logic, "get_agent_container", lambda _n: object())
        direct = asyncio.run(perms_logic.get_agent_permissions_logic(A, world.user(W.ALICE)))
        assert sorted(p["name"] for p in direct["permitted_agents"]) == agents[A]["permissions"]

    def test_schedules_are_live_and_never_carry_the_webhook_token(self, world, monkeypatch):
        sched = db.create_schedule(B, W.ALICE, ScheduleCreate(
            name="daily", cron_expression="0 9 * * *", message="/daily"))
        db.generate_webhook_token(sched.id)
        agents = _detail(monkeypatch, world.user(W.ALICE), [A, B])
        assert [s["name"] for s in agents[B]["schedules"]] == ["daily"]
        blob = repr(agents)
        assert "webhook_token" not in blob and "webhook_secret_encrypted" not in blob

    def test_a_failed_permission_read_keeps_folders_and_schedules(self, world, monkeypatch):
        db.upsert_shared_folder_config(A, expose_enabled=True, consume_enabled=True)

        def boom(_n):
            raise RuntimeError("db locked")
        monkeypatch.setattr(db, "get_permitted_agents", boom)
        agents = _detail(monkeypatch, world.user(W.ALICE), [A, B])
        assert "permissions" not in agents[A]
        assert agents[A]["folders"] == {"expose": True, "consume": True}
        assert "schedules" in agents[A]


# ---------------------------------------------------------------------------
# GET /api/systems/{name}/manifest -> export_manifest
# ---------------------------------------------------------------------------

class TestExportFolders:
    def test_folders_are_exported(self, world):
        db.upsert_shared_folder_config(B, expose_enabled=True, consume_enabled=False)
        db.upsert_shared_folder_config(C, expose_enabled=False, consume_enabled=False)
        text, doc = _export([A, B, C], world.user(W.ALICE))
        assert doc["agents"]["b"]["folders"] == {"expose": True, "consume": False}
        assert "folders" not in doc["agents"]["c"]  # both off: no key
        assert "folders" not in doc["agents"]["a"]  # no row: no key
        assert "folders of" not in text

    def test_a_failed_folder_read_is_named(self, world, monkeypatch):
        def boom(_n):
            raise RuntimeError("db locked")
        monkeypatch.setattr(db, "get_shared_folder_config", boom)
        text, doc = _export([A, B], world.user(W.ALICE))
        assert f"folders of {A}" in text.splitlines()[0]
        assert "folders" not in doc["agents"]["a"]


class TestExportPermissions:
    def test_a_first_member_without_edges_still_exports_the_others(self, world):
        db.add_agent_permission(B, C, W.ALICE)
        _text, doc = _export([A, B, C], world.user(W.ALICE))
        assert doc["permissions"] == {"explicit": {"b": ["c"]}}

    def test_the_block_does_not_depend_on_roster_order(self, world):
        db.add_agent_permission(B, C, W.ALICE)
        _t1, forward = _export([A, B, C], world.user(W.ALICE))
        _t2, backward = _export([C, B, A], world.user(W.ALICE))
        assert forward["permissions"] == backward["permissions"]

    def test_a_one_member_system_is_not_a_full_mesh(self, world):
        _text, doc = _export([A], world.user(W.ALICE))
        assert "permissions" not in doc

    def test_two_members_both_edged_is_a_full_mesh(self, world):
        db.add_agent_permission(A, B, W.ALICE)
        db.add_agent_permission(B, A, W.ALICE)
        _text, doc = _export([A, B], world.user(W.ALICE))
        assert doc["permissions"] == {"preset": "full-mesh"}

    def test_an_edge_leaving_the_system_does_not_break_the_mesh(self, world):
        db.register_agent_owner("other", W.ALICE)
        for s, t in ((A, B), (B, A), (A, "other")):
            db.add_agent_permission(s, t, W.ALICE)
        text, doc = _export([A, B], world.user(W.ALICE))
        assert doc["permissions"] == {"preset": "full-mesh"}
        assert "other" not in text

    def test_a_tag_only_member_is_outside_the_mesh_universe(self, world):
        # Prefixed system, one member without the prefix: it has no manifest
        # key, the header names it, and the mesh is stated over keyed members.
        world.seed_agent("helper")
        db.add_agent_permission(A, B, W.ALICE)
        db.add_agent_permission(B, A, W.ALICE)
        text, doc = _export([A, B, "helper"], world.user(W.ALICE))
        assert doc["permissions"] == {"preset": "full-mesh"}
        assert "members omitted" in text and "helper" in text

    def test_the_export_round_trips_to_the_stored_edges(self, world):
        db.add_agent_permission(B, C, W.ALICE)
        db.add_agent_permission(C, A, W.ALICE)
        db.add_agent_permission(C, B, W.ALICE)
        text, _doc = _export([A, B, C], world.user(W.ALICE))
        manifest = system_service.parse_manifest(text)
        desired = system_service._desired_edges(
            system_service.declared_names(manifest), manifest.permissions)
        stored = {n: sorted(db.get_permitted_agents(n)) for n in (A, B, C)}
        assert {s: t for s, t in desired.items() if t} == {s: t for s, t in stored.items() if t}
