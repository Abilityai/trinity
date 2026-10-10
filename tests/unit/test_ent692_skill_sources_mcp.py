"""Skill-library source management for admin keys over MCP (trinity-enterprise#692).

The MCP tools are thin proxies; what they need from the backend is:

  * an **idempotent register** — `POST /api/skills/sources/apply`, keyed on the
    repository URL, so a declared source can be re-applied without a 409 the
    caller must branch on, and never as a duplicate row (AC 3);
  * the **same embedded-credential refusal** as the create route (AC 4);
  * an optional `priority` on the create route (the tool's documented shape);
  * the **agent-key fence inherited from the gate**, not bolted per endpoint
    (AC 2) — proven by neutralising the per-route `reject_agent_principal` and
    showing every route still refuses an agent key through `require_admin`;
  * the **last fleet re-inject summary, counts only**, on the open library
    status, so a sync's consequence is readable by whoever ran it (AC 5,
    folded gap) — without the per-agent `failures` map.

Every route test drives the REAL skills router through a TestClient. Overrides
are keyed off the symbols `routers.skills` closed over (see the ent#334 test for
why `dependencies.*` would silently miss). The `db` the router and the service
use is a facade over a throwaway SQLite `SkillSourcesOperations`, never the
`database.db` singleton (the ent#237 test explains the import-order trap).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

REPO = "https://github.com/acme/skills"


@pytest.fixture(autouse=True)
def _real_modules_not_stubs(monkeypatch):
    """Evict import-time `sys.modules` stubs other files install (#1898).

    Same fixture as test_ent237_skill_sources: a stub has no `__file__`.
    `monkeypatch.delitem` so the eviction is undone at teardown.
    """
    import importlib

    for name in ("utils.url_validation", "services.skill_service", "routers.skills"):
        mod = sys.modules.get(name)
        if mod is not None and getattr(mod, "__file__", None) is None:
            monkeypatch.delitem(sys.modules, name, raising=False)
    try:
        importlib.import_module("utils.url_validation")
    except Exception:  # noqa: BLE001
        pass
    yield


@pytest.fixture
def sources_ops(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    from db.schema import init_schema

    conn = sqlite3.connect(db_path)
    init_schema(conn.cursor(), conn)
    conn.commit()
    conn.close()

    import db.engine as engine_mod

    if hasattr(engine_mod.get_engine, "cache_clear"):
        engine_mod.get_engine.cache_clear()

    from db.skill_sources import SkillSourcesOperations

    return SkillSourcesOperations()


class _Facade:
    """The slice of the `db` facade the source routes and `apply_skill_source` use."""

    def __init__(self, ops):
        self._ops = ops
        self.settings = {}

    def list_skill_sources(self, enabled_only=False):
        return self._ops.list_sources(enabled_only)

    def get_skill_source(self, source_id):
        return self._ops.get_source(source_id)

    def create_skill_source(self, **kwargs):
        return self._ops.create_source(**kwargs)

    def update_skill_source(self, source_id, **fields):
        return self._ops.update_source(source_id, **fields)

    def delete_skill_source(self, source_id):
        return self._ops.delete_source(source_id)

    def get_setting_value(self, key, default=None):
        return self.settings.get(key, default)


def _user(role="admin", agent_name=None, connector_agent=None, mcp_scope=None):
    from models import User

    return User(
        id=1, username="u", role=role, agent_name=agent_name,
        connector_agent=connector_agent, mcp_scope=mcp_scope,
    )


@pytest.fixture
def facade(sources_ops):
    return _Facade(sources_ops)


def _client(monkeypatch, facade, *, user, real_admin_gate=False):
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import skills as skills_router
        import services.skill_service as ss
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")

    monkeypatch.setattr(skills_router, "db", facade)
    monkeypatch.setattr(ss, "db", facade)

    async def _no_audit(*_a, **_k):
        return None

    monkeypatch.setattr(skills_router, "_audit_source", _no_audit)

    app = FastAPI()
    app.include_router(skills_router.router)
    app.dependency_overrides[skills_router.get_current_user] = lambda: user
    if not real_admin_gate:
        app.dependency_overrides[skills_router.require_admin] = lambda: user
    return TestClient(app)


# =============================================================================
# AC 3 — idempotent register on url
# =============================================================================

class TestApplyIsIdempotentOnUrl:
    def test_absent_url_is_created_with_the_given_priority(self, monkeypatch, facade, sources_ops):
        c = _client(monkeypatch, facade, user=_user())
        resp = c.post("/api/skills/sources/apply", json={"url": REPO, "ref": "v1", "ref_type": "tag", "priority": 50})

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["action"] == "created"
        assert body["source"]["url"] == REPO
        assert body["source"]["ref"] == "v1"
        assert body["source"]["ref_type"] == "tag"
        assert body["source"]["priority"] == 50
        # name defaults to owner/repo
        assert body["source"]["name"] == "acme/skills"
        assert len(sources_ops.list_sources()) == 1

    def test_reapplying_updates_ref_and_priority_in_place(self, monkeypatch, facade, sources_ops):
        c = _client(monkeypatch, facade, user=_user())
        first = c.post("/api/skills/sources/apply", json={"url": REPO, "ref": "v1", "ref_type": "tag"}).json()

        resp = c.post("/api/skills/sources/apply", json={"url": REPO, "ref": "v2", "priority": 7})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["action"] == "updated"
        assert body["source"]["id"] == first["source"]["id"]
        assert body["source"]["ref"] == "v2"
        assert body["source"]["priority"] == 7
        # ref_type was not named → untouched, not reset to a default
        assert body["source"]["ref_type"] == "tag"
        assert len(sources_ops.list_sources()) == 1, "never a duplicate row"

    def test_identical_reapply_is_unchanged_and_writes_nothing(self, monkeypatch, facade, sources_ops):
        c = _client(monkeypatch, facade, user=_user())
        c.post("/api/skills/sources/apply", json={"url": REPO, "ref": "main"})
        before = sources_ops.list_sources()[0]

        resp = c.post("/api/skills/sources/apply", json={"url": REPO, "ref": "main"})

        assert resp.status_code == 200
        assert resp.json()["action"] == "unchanged"
        after = sources_ops.list_sources()[0]
        assert after.updated_at == before.updated_at

    def test_scheme_less_stored_url_matches_its_normalized_spelling(self, monkeypatch, facade, sources_ops):
        """The bundled default is seeded from a bare literal (#2763)."""
        sources_ops.create_source(name="community", url="github.com/acme/skills", ref="v1",
                                  ref_type="tag", is_default=True)
        c = _client(monkeypatch, facade, user=_user())

        resp = c.post("/api/skills/sources/apply", json={"url": REPO, "ref": "v2"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["action"] == "updated"
        assert len(sources_ops.list_sources()) == 1

    def test_two_rows_for_one_url_is_ambiguous_never_a_guess(self, monkeypatch, facade, sources_ops):
        a = sources_ops.create_source(name="a", url=REPO, ref="main")
        b = sources_ops.create_source(name="b", url=REPO, ref="dev")
        c = _client(monkeypatch, facade, user=_user())

        resp = c.post("/api/skills/sources/apply", json={"url": REPO, "ref": "v3"})

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "ambiguous_source"
        assert sorted(detail["source_ids"]) == sorted([a.id, b.id])
        assert {s.ref for s in sources_ops.list_sources()} == {"main", "dev"}

    def test_embedded_credentials_refused_with_the_create_routes_text(self, monkeypatch, facade, sources_ops):
        """AC 4 — same named error as REST create, not a generic failure."""
        c = _client(monkeypatch, facade, user=_user())
        tokenized = "https://tok_placeholder@github.com/acme/skills"

        via_apply = c.post("/api/skills/sources/apply", json={"url": tokenized})
        via_create = c.post("/api/skills/sources", json={"name": "x", "url": tokenized})

        assert via_apply.status_code == 400
        assert via_apply.json() == via_create.json()
        assert "must not embed a token" in via_apply.json()["detail"]
        assert sources_ops.list_sources() == []

    def test_off_allowlist_host_is_refused(self, monkeypatch, facade, sources_ops):
        c = _client(monkeypatch, facade, user=_user())
        resp = c.post("/api/skills/sources/apply", json={"url": "https://example.com/acme/skills"})
        assert resp.status_code == 400
        assert sources_ops.list_sources() == []


class TestCreateAcceptsPriority:
    def test_priority_is_honoured(self, monkeypatch, facade):
        c = _client(monkeypatch, facade, user=_user())
        resp = c.post("/api/skills/sources", json={"name": "x", "url": REPO, "priority": 42})
        assert resp.status_code == 201, resp.text
        assert resp.json()["priority"] == 42

    def test_omitted_priority_keeps_the_custom_default(self, monkeypatch, facade):
        from db.skill_sources import CUSTOM_SOURCE_PRIORITY

        c = _client(monkeypatch, facade, user=_user())
        resp = c.post("/api/skills/sources", json={"name": "x", "url": REPO})
        assert resp.status_code == 201
        assert resp.json()["priority"] == CUSTOM_SOURCE_PRIORITY

    def test_duplicate_create_still_409s(self, monkeypatch, facade):
        """The Settings panel's contract is unchanged — only /apply is idempotent."""
        c = _client(monkeypatch, facade, user=_user())
        c.post("/api/skills/sources", json={"name": "x", "url": REPO})
        assert c.post("/api/skills/sources", json={"name": "x", "url": REPO}).status_code == 409


# =============================================================================
# AC 2 — the agent fence is the GATE's, not a per-endpoint bolt-on
# =============================================================================

SOURCE_ROUTES = [
    ("GET", "/api/skills/sources", None),
    ("POST", "/api/skills/sources", {"name": "x", "url": REPO}),
    ("POST", "/api/skills/sources/apply", {"url": REPO}),
    ("PUT", "/api/skills/sources/src_x", {"ref": "v2"}),
    ("DELETE", "/api/skills/sources/src_x", None),
    ("POST", "/api/skills/sources/src_x/sync", None),
    ("POST", "/api/skills/library/sync", None),
]


class TestAgentKeysRefusedByTheGate:
    @pytest.mark.parametrize("method, path, body", SOURCE_ROUTES)
    def test_agent_key_refused_even_without_the_per_route_check(
        self, monkeypatch, facade, method, path, body
    ):
        from routers import skills as skills_router

        # Neutralise the belt-and-braces per-route call: if the refusal still
        # happens, it came from `require_admin` itself (#1890).
        monkeypatch.setattr(skills_router, "reject_agent_principal", lambda _u: None)
        agent = _user(role="admin", agent_name="some-agent", mcp_scope="agent")
        c = _client(monkeypatch, facade, user=agent, real_admin_gate=True)

        resp = c.request(method, path, json=body)

        assert resp.status_code == 403, f"{method} {path}: {resp.status_code} {resp.text}"
        assert resp.json()["detail"] == (
            "This operation is human-only; agent-scoped keys cannot perform it"
        )

    @pytest.mark.parametrize("method, path, body", SOURCE_ROUTES)
    def test_connector_key_refused(self, monkeypatch, facade, method, path, body):
        conn = _user(role="admin", connector_agent="bound-agent", mcp_scope="connector")
        c = _client(monkeypatch, facade, user=conn, real_admin_gate=True)
        assert c.request(method, path, json=body).status_code == 403

    def test_admin_user_scoped_key_passes_the_gate(self, monkeypatch, facade):
        """The principal this issue is for: an admin owner's user-scoped key."""
        c = _client(monkeypatch, facade, user=_user(mcp_scope="user"), real_admin_gate=True)
        resp = c.post("/api/skills/sources/apply", json={"url": REPO})
        assert resp.status_code == 201, resp.text

    def test_non_admin_user_key_refused(self, monkeypatch, facade):
        c = _client(monkeypatch, facade, user=_user(role="user", mcp_scope="user"), real_admin_gate=True)
        resp = c.post("/api/skills/sources/apply", json={"url": REPO})
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Admin access required"


# =============================================================================
# AC 5 (folded gap) — last fleet re-inject summary, counts only, for every reader
# =============================================================================

REPORT = {
    "started_at": "2026-10-01T00:00:00Z",
    "finished_at": "2026-10-01T00:01:00Z",
    "trigger": "manual_sync",
    "commit_sha": "abc1234",
    "agents_total": 3,
    "agents_injected": 1,
    "agents_skipped": 1,
    "agents_failed": 1,
    "failures": {"secret-agent": "boom at /data/skills-library/src_x"},
}


class TestStatusCarriesReinjectSummary:
    def _with_report(self, monkeypatch, facade):
        import json
        from services.skills_sync_service import FLEET_LAST_RUN_KEY

        facade.settings[FLEET_LAST_RUN_KEY] = json.dumps(REPORT)

    @pytest.mark.parametrize("user", [
        _user(role="user", mcp_scope="user"),
        _user(role="admin", agent_name="some-agent", mcp_scope="agent"),
    ], ids=["non-admin", "agent-key"])
    def test_counts_only_on_the_open_status(self, monkeypatch, facade, user):
        self._with_report(monkeypatch, facade)
        c = _client(monkeypatch, facade, user=user)

        resp = c.get("/api/skills/library/status")

        assert resp.status_code == 200, resp.text
        summary = resp.json()["last_fleet_reinject"]
        for key in ("started_at", "finished_at", "trigger", "commit_sha",
                    "agents_total", "agents_injected", "agents_skipped", "agents_failed"):
            assert summary[key] == REPORT[key], key
        assert "failures" not in summary
        assert "secret-agent" not in resp.text

    def test_admin_sources_route_keeps_the_failures(self, monkeypatch, facade):
        self._with_report(monkeypatch, facade)
        c = _client(monkeypatch, facade, user=_user())
        body = c.get("/api/skills/sources").json()
        assert body["last_fleet_reinject"]["failures"] == REPORT["failures"]

    def test_no_report_yet_is_null(self, monkeypatch, facade):
        c = _client(monkeypatch, facade, user=_user(role="user"))
        resp = c.get("/api/skills/library/status")
        assert resp.status_code == 200
        assert resp.json()["last_fleet_reinject"] is None

    def test_malformed_report_never_500s(self, monkeypatch, facade):
        from services.skills_sync_service import FLEET_LAST_RUN_KEY

        facade.settings[FLEET_LAST_RUN_KEY] = "{not json"
        c = _client(monkeypatch, facade, user=_user(role="user"))
        resp = c.get("/api/skills/library/status")
        assert resp.status_code == 200
        assert resp.json()["last_fleet_reinject"] is None


# =============================================================================
# Invariant #13 — the router names its covering MCP module
# =============================================================================

def test_router_mcp_header_points_at_the_source_tools():
    first = (_BACKEND / "routers" / "skills.py").read_text().splitlines()[0]
    assert first.startswith("# mcp:")
    assert "skill_sources.ts" in first
    for tool in ("list_skill_sources", "register_skill_source", "update_skill_source",
                 "delete_skill_source", "sync_skill_source", "sync_skill_library"):
        assert tool in first, tool


# =============================================================================
# Review fixes — a concurrent delete, and a type-malformed re-inject report
# =============================================================================

class TestApplyRacesAConcurrentDelete:
    def test_row_deleted_between_match_and_update_is_recreated_not_a_500(
        self, monkeypatch, facade, sources_ops
    ):
        """The matched row vanishes (another admin's DELETE) before the update
        lands: `update_source` answers None. That must not surface as a 500 off
        `source.id`, nor as `updated` with a null source — the declared source is
        simply absent now, so the apply creates it."""
        existing = sources_ops.create_source(name="a", url=REPO, ref="main")
        real_update = facade.update_skill_source
        calls = {"n": 0}

        def _racing_update(source_id, **fields):
            calls["n"] += 1
            if calls["n"] == 1:
                sources_ops.delete_source(source_id)  # the concurrent DELETE
            return real_update(source_id, **fields)

        monkeypatch.setattr(facade, "update_skill_source", _racing_update)
        c = _client(monkeypatch, facade, user=_user())

        resp = c.post("/api/skills/sources/apply", json={"url": REPO, "ref": "v2"})

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["action"] == "created"
        assert body["source"]["ref"] == "v2"
        rows = sources_ops.list_sources()
        assert [r.ref for r in rows] == ["v2"]
        assert rows[0].id != existing.id


class TestStatusSurvivesATypeMalformedReport:
    @pytest.mark.parametrize("blob", [
        {"agents_total": None},
        {"agents_total": "three"},
        {"agents_failed": ["a", "b"]},
        {"trigger": {"nested": 1}},
        {"started_at": 12345},
    ], ids=["null-count", "str-count", "list-count", "dict-trigger", "int-timestamp"])
    def test_open_status_never_500s(self, monkeypatch, facade, blob):
        """`_read_last_fleet_reinject` promises status never 500s over a
        malformed blob — but a parseable dict with a wrong-typed field passed
        the isinstance check and failed the `FleetReinjectSummary` response
        model, 500-ing the route every agent's Skills tab reads."""
        import json
        from services.skills_sync_service import FLEET_LAST_RUN_KEY

        facade.settings[FLEET_LAST_RUN_KEY] = json.dumps({**REPORT, **blob})
        c = _client(monkeypatch, facade, user=_user(role="user", mcp_scope="user"))

        resp = c.get("/api/skills/library/status")

        assert resp.status_code == 200, resp.text
        assert resp.json()["last_fleet_reinject"] is None
