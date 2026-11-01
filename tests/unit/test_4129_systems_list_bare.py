"""#4129: `GET /api/systems` lists systems by their tag, never by a name split.

The list used to split every accessible agent's name at its last hyphen and
call the left part a system. A `naming: bare` member (`writer`) was dropped,
`content-writer` invented a `content` system, and `acme-api-worker` landed in
`acme-api`. The deploy and apply paths record membership as the system TAG;
the list now reads it (one batched read over the caller's roster) and lists a
tag only when there is evidence it is a system: an auto-created system view
for it, a `system_apply` audit row naming it, or a carrier named `<tag>-*`.
A plain tag (`prod`, a manifest's `default_tags`) is not a system.

Every case runs the REAL route over the REAL `db` facade on a full schema
(`db_backend`). The only seam is the Docker-backed roster
(`routers.agents.get_accessible_agents`), fed rows in the real
`AgentStatus.dict()` shape; test 6 additionally makes the tag read raise,
test 9 the system-view and audit reads.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
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
from models import AgentStatus, SystemApplyRequest  # noqa: E402
from services import system_service  # noqa: E402

pytestmark = pytest.mark.unit

SYSTEM = W.SYSTEM
T0 = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    return W.make_world(monkeypatch, tmp_path)


@pytest.fixture
def ew(world, monkeypatch):
    W.install_executor_seams(world, monkeypatch)
    return world


def _row(name: str, created: datetime = T0) -> dict:
    """One roster row exactly as `get_accessible_agents` returns it."""
    return AgentStatus(name=name, status="running", port=2222, created=created,
                       resources={}, template="local:plain").dict()


def _client(world, monkeypatch, rows):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routers import agents as agents_router
    from routers import systems

    monkeypatch.setattr(agents_router, "get_accessible_agents", lambda user: list(rows))
    app = FastAPI()
    app.include_router(systems.router)
    app.dependency_overrides[systems.get_current_user] = lambda: world.user(W.ALICE)
    return TestClient(app, raise_server_exceptions=False)


def _listed(client) -> dict:
    r = client.get("/api/systems")
    assert r.status_code == 200, r.text
    return {s["name"]: sorted(a["name"] for a in s["agents"]) for s in r.json()["systems"]}


def _apply_through_the_route(world, monkeypatch, manifest_yaml: str):
    """A real `POST /api/systems/apply`: the route runs the real apply and writes
    its own `system_apply` audit row. Only create/start are the world's seams."""
    from starlette.requests import Request

    from routers import systems

    real = systems.apply_manifest

    async def apply_with_seams(manifest, user, request, *, dry_run):
        return await real(manifest, user, request, dry_run=dry_run,
                          create_agent_fn=world.create, start_agent_fn=world.docker.start)

    monkeypatch.setattr(systems, "apply_manifest", apply_with_seams)
    request = Request({"type": "http", "method": "POST", "path": "/api/systems/apply",
                       "headers": [], "client": ("127.0.0.1", 1), "query_string": b""})
    res = asyncio.run(systems.apply_system(
        SystemApplyRequest(manifest=manifest_yaml, dry_run=False), request,
        current_user=world.user(W.ALICE)))
    assert getattr(res, "status", None) == "applied", res
    return res


def _auto_view(system_name: str) -> None:
    """The view deploy/apply auto-create for a manifest's `system_view:`."""
    from models import SystemViewConfig
    assert system_service.create_system_view(
        system_name, SystemViewConfig(name=f"{system_name} view"), None,
        str(db.get_user_by_username(W.ALICE)["id"]))


BARE = (f"name: {SYSTEM}\nnaming: bare\nagents:\n"
        "  writer:\n    template: local:plain\n"
        "  content-writer:\n    template: local:plain\n")


class TestSystemsListByTag:

    def test_1_a_bare_system_is_listed_under_its_own_name(self, ew, monkeypatch):
        _apply_through_the_route(ew, monkeypatch, BARE)
        rows = db.get_audit_entries(target_type="system", target_id=SYSTEM)
        assert [r["event_action"] for r in rows] == ["system_apply"]  # the evidence
        listed = _listed(_client(ew, monkeypatch, [_row("writer"), _row("content-writer")]))
        assert listed == {SYSTEM: ["content-writer", "writer"]}
        assert "content" not in listed

    def test_2_a_prefix_only_lookalike_is_not_a_system(self, world, monkeypatch):
        for n in ("content-writer", "content-editor"):
            world.seed_agent(n, tags=())
        client = _client(world, monkeypatch, [_row("content-writer"), _row("content-editor")])
        assert _listed(client) == {}

    def test_3_a_prefixed_system_lists_once_even_with_a_hyphenated_short_name(self, world, monkeypatch):
        names = ("acme-api", "acme-web", "acme-api-worker")
        for n in names:
            world.seed_agent(n)
        listed = _listed(_client(world, monkeypatch, [_row(n) for n in names]))
        assert listed == {SYSTEM: sorted(names)}

    def test_4_members_are_the_carriers_never_a_prefix_match(self, ew, monkeypatch):
        _apply_through_the_route(ew, monkeypatch, BARE)
        ew.seed_agent("notes", tags=("content",))
        _auto_view("content")  # `content` IS a system here; its member is `notes` only
        rows = [_row("writer"), _row("content-writer"), _row("notes")]
        listed = _listed(_client(ew, monkeypatch, rows))
        assert listed == {SYSTEM: ["content-writer", "writer"], "content": ["notes"]}

    def test_5_only_the_callers_roster_and_detail_agrees(self, world, monkeypatch):
        world.seed_agent("writer")
        world.seed_agent("hidden")
        world.seed_agent("secret-only", tags=("secret",))
        _auto_view(SYSTEM)
        _auto_view("secret")
        client = _client(world, monkeypatch, [_row("writer")])
        listed = _listed(client)
        assert listed == {SYSTEM: ["writer"]}  # `hidden` and `secret` never seen
        for name, members in listed.items():
            r = client.get(f"/api/systems/{name}")
            assert r.status_code == 200, r.text
            assert set(members) <= {a["name"] for a in r.json()["agents"]}

    def test_6_unreadable_tags_are_a_named_503_never_a_guess(self, world, monkeypatch):
        world.seed_agent("acme-api")

        def boom(_names):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(db, "get_tags_for_agents", boom)
        r = _client(world, monkeypatch, [_row("acme-api")]).get("/api/systems")
        assert r.status_code == 503, r.text
        assert "tags" in r.json()["detail"].lower()

    def test_7_a_plain_tag_without_evidence_is_not_a_system(self, ew, monkeypatch):
        W.apply(ew, f"name: {SYSTEM}\ndefault_tags: [fleet]\nagents:\n"
                    "  api:\n    template: local:plain\n"
                    "  web:\n    template: local:plain\n", dry_run=False)
        assert "fleet" in db.get_agent_tags("acme-api")
        ew.seed_agent("lone", tags=("prod",))
        # A view a person made over `prod` is not the auto-created system view.
        from db_models import SystemViewCreate
        db.create_system_view(str(db.get_user_by_username(W.ALICE)["id"]),
                              SystemViewCreate(name="Prod", filter_tags=["prod"]))
        rows = [_row("acme-api"), _row("acme-web"), _row("lone")]
        assert _listed(_client(ew, monkeypatch, rows)) == {SYSTEM: ["acme-api", "acme-web"]}

    def test_8_created_at_is_the_earliest_member_and_newest_system_first(self, world, monkeypatch):
        for n, tag in (("old-a", "old"), ("old-b", "old"), ("new-a", "new"),
                       ("tie-a", "tie"), ("same-a", "same")):
            world.seed_agent(n, tags=(tag,))
        late = T0.replace(month=11)
        rows = [_row("old-a", T0.replace(day=5)), _row("old-b", T0), _row("new-a", late),
                _row("tie-a", T0.replace(day=2)), _row("same-a", T0.replace(day=2))]
        r = _client(world, monkeypatch, rows).get("/api/systems")
        assert r.status_code == 200, r.text
        got = [(s["name"], s["created_at"]) for s in r.json()["systems"]]
        assert got == [
            ("new", "2026-11-01T09:00:00.000000Z"),
            ("same", "2026-10-02T09:00:00.000000Z"),
            ("tie", "2026-10-02T09:00:00.000000Z"),
            ("old", "2026-10-01T09:00:00.000000Z"),
        ]

    @pytest.mark.parametrize("read", ["list_all_system_views", "get_audit_entries"])
    def test_9_unreadable_evidence_is_a_named_503_never_a_guess(self, world, monkeypatch, read):
        world.seed_agent("writer")  # carries the system tag, but no `<tag>-*` carrier and no view

        def boom(*_a, **_k):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(db, read, boom)
        r = _client(world, monkeypatch, [_row("writer")]).get("/api/systems")
        assert r.status_code == 503, r.text
        detail = r.json()["detail"].lower()
        assert "system views" in detail and "audit" in detail
