"""#3180 — the `ops_write` scope, end to end through the real resolver and routes.

`ops_write` is the bounded WRITE sibling of the read-only `ops` scope: an admin
mints it from a signed-in session, and it may do four things — reset an agent's
circuit breaker, start or stop an agent, terminate an execution — while its
owner is still an admin. This suite drives every claim through the REAL
credential resolver (`get_current_user`) with REAL keys minted through the real
`POST /api/mcp/keys`, on the real schema (`tests/db_harness.py`), against the
real routers. Only the boundaries a unit tier cannot have are faked, and each
fake is named where it is installed: the Docker daemon (container lookup,
start, stop, recreate, the in-container HTTP calls after a start), and Redis
(the breaker keys, the self-heal lock — `fakeredis`, real semantics).

What is pinned, by section:

* pass paths for the four routes, start through the real `start_agent_internal`
  including the #1854 self-heal of a revoked baked-in key (no credential reaches
  the caller, its reach is unchanged), and the audit attribution;
* the target policy on start/stop (system agent, `is_system` flag, ghost, a
  ghost lookup that fails), and that reset/terminate keep every agent;
* dies with its owner: demoted (creator, operator) → 403 on all four, suspended
  → 401 on all four, a password change with "revoke keys" revokes it, and the
  revoke set equals the mintable set;
* the mint matrix, composed (route) and isolated (each redundant rung alone);
* the read-only `ops` key still cannot reach the four writes;
* A2A answers 403 with no dispatch and no anonymous fallback;
* the verify verdict, enumeration safety, and the crafted-`Host` other shape.

The every-route sweep, the mark census and layer masking live in
`test_3180_ops_write_route_census.py`. Principals are real `models.User`
objects, never a MagicMock, with every scope field set; pinned values differ
from what production returns by default.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import fakeredis
import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from db_harness import db_backend, run as _hrun  # noqa: E402,F401

import routers.a2a as _A2A  # noqa: E402
import routers.agents as _AGENTS  # noqa: E402
import routers.chat as _CHAT  # noqa: E402
import routers.mcp_keys as _KEYS  # noqa: E402
import routers.users as _USERS  # noqa: E402
from database import db  # noqa: E402
from db_models import McpApiKeyCreate, UserCreate  # noqa: E402
from models import User  # noqa: E402

pytestmark = pytest.mark.unit

# The dependencies module the routers ACTUALLY call (the unit conftest may
# re-import `dependencies` between tests; the routers keep the copy they bound).
_D = SimpleNamespace(**_AGENTS.get_current_user.__globals__)

OWNER = "ow3180-owner"
OWNER_EMAIL = "ow3180-owner@example.com"
PERSON = "ow3180-person"          # a non-admin human
TARGET = "ow3180-target"          # a durable agent the owner holds
GHOST = "ow3180-ghost"            # an ephemeral agent the owner holds
FLAGGED = "ow3180-flagged-system"  # an agent carrying the is_system flag
SYSTEM = "trinity-system"
NOPE = "ow3180-no-such-agent"
OLD_PASSWORD = "Ow3180-Old-Passw0rd!"
NEW_PASSWORD = "Ow3180-New-Passw0rd!"

FOUR = {
    "reset": ("POST", f"/api/agents/{TARGET}/circuit-breaker/reset"),
    "start": ("POST", f"/api/agents/{TARGET}/start"),
    "stop": ("POST", f"/api/agents/{TARGET}/stop"),
    "terminate": ("POST", f"/api/agents/{TARGET}/executions/ow3180-exec/terminate"),
}


def _dep_globals():
    """Each distinct `dependencies` globals dict a router in this app calls."""
    fns = (_AGENTS.get_current_user, _CHAT.get_current_user, _KEYS.require_interactive,
           _USERS.require_interactive, _A2A.get_user_or_anonymous)
    return list({id(f.__globals__): f.__globals__ for f in fns}.values())


@pytest.fixture
def world(db_backend, monkeypatch):
    redis = fakeredis.FakeRedis()
    for g in _dep_globals():
        monkeypatch.setitem(g, "get_breaker_redis", lambda: redis)
    from services import password_service
    for g in {id(f.__globals__): f.__globals__ for f in (
            password_service.change_own_password, password_service.revoke_user_sessions)}.values():
        monkeypatch.setitem(g, "get_breaker_redis", lambda: redis)

    db.create_user(UserCreate(username=OWNER, role="admin", email=OWNER_EMAIL))
    db.update_user_password(OWNER, _D.hash_password(OLD_PASSWORD))
    db.create_user(UserCreate(username=PERSON, role="operator", email="ow3180-person@example.com"))
    for agent in (TARGET, GHOST, FLAGGED, SYSTEM):
        db.register_agent_owner(agent, OWNER)
    _hrun("UPDATE agent_ownership SET is_ephemeral = 1 WHERE agent_name = :a", a=GHOST)
    _hrun("UPDATE agent_ownership SET is_system = 1 WHERE agent_name IN (:a, :b)", a=FLAGGED, b=SYSTEM)

    app = FastAPI()
    for router in (_AGENTS.router, _CHAT.router, _KEYS.router, _USERS.router, _A2A.a2a_server_router):
        app.include_router(router)
    client = TestClient(app)

    def jwt(username=OWNER):
        return {"Authorization": f"Bearer {_D.create_access_token({'sub': username})}"}

    def mint(scope, name=None):
        """Through the REAL mint route, with the owner's signed-in session."""
        res = client.post("/api/mcp/keys", json={"name": name or f"ow3180 {scope}", "scope": scope},
                          headers=jwt())
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["scope"] == scope
        return SimpleNamespace(id=body["id"], secret=body["api_key"],
                               headers={"Authorization": f"Bearer {body['api_key']}"})

    yield SimpleNamespace(client=client, app=app, jwt=jwt, mint=mint, redis=redis)
    app.dependency_overrides.clear()


def _q(sql, **binds):
    """Read rows on the active engine (`db_harness.run` is write-only)."""
    from db.engine import get_engine
    from sqlalchemy import text
    with get_engine().connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), binds).mappings()]


def _key_row(key_id):
    rows = _q("SELECT scope, agent_name, is_active, user_id FROM mcp_api_keys WHERE id = :i", i=key_id)
    return rows[0] if rows else None


def _detail_code(res):
    detail = res.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


# --------------------------------------------------------------------------- #
# Docker / Redis boundaries for the pass paths
# --------------------------------------------------------------------------- #

class _Container:
    """A container as Docker reports it: an id, a status, its env."""

    def __init__(self, env, status="exited"):
        self.id = "c0ffee3180"
        self.status = status
        self.attrs = {"Config": {"Env": [f"{k}={v}" for k, v in env.items()]}, "HostConfig": {}}


def _patch(monkeypatch, fn, name, value):
    monkeypatch.setitem(fn.__globals__, name, value)


def _fake_breakers(monkeypatch):
    """The breaker state lives in Redis; record what the route resets."""
    reset = []
    agent_client = importlib.import_module("services.agent_client")
    dispatch = importlib.import_module("services.dispatch_breaker")
    monkeypatch.setattr(agent_client, "CircuitState",
                        lambda name: SimpleNamespace(state="open"), raising=True)
    monkeypatch.setattr(agent_client, "reset_circuit", lambda name: reset.append(("transport", name)))
    monkeypatch.setattr(dispatch, "DispatchBreaker",
                        lambda name: SimpleNamespace(to_dict=lambda: {"state": "open"}), raising=True)
    monkeypatch.setattr(dispatch, "reset_dispatch", lambda name: reset.append(("dispatch", name)))
    return reset


def _fake_docker_for_start(monkeypatch, container):
    """Fake ONLY what talks to Docker or into the container on the start path.

    Kept real: the router, the resolver, `enforce_agent_spawn_scope`,
    `start_agent_internal`'s decision logic, `check_agent_mcp_key_matches` (a DB
    lookup of the container's key), `heal_agent_mcp_key_env` (mint, reconcile,
    prune, audit — DB), the read-only sync decision and the audit log."""
    lc = _AGENTS.start_agent_internal
    seen = {"recreate": [], "started": []}

    async def _reload(c):
        return None

    async def _start(c):
        seen["started"].append(c)
        c.status = "running"

    async def _recreate(agent_name, old, owner, **kw):
        seen["recreate"].append(kw)

    async def _true_async(*a, **k):
        return True

    async def _ok(*a, **k):
        return {"status": "success"}

    _patch(monkeypatch, lc, "get_agent_container", lambda name: container)
    _patch(monkeypatch, lc, "container_reload", _reload)
    _patch(monkeypatch, lc, "container_start", _start)
    _patch(monkeypatch, lc, "recreate_container_with_updated_config", _recreate)
    _patch(monkeypatch, lc, "clear_agent_breakers", lambda name: None)  # Redis
    # Comparisons of the container's Docker config (mounts, limits, image, env
    # derived from settings) against the DB: the container is a fake, so they
    # are pinned to "matches". The MCP-key predicate is NOT on this list.
    for name in ("check_public_folder_mount_matches", "check_api_key_env_matches",
                 "check_github_pat_env_matches", "check_resource_limits_match",
                 "check_full_capabilities_match", "check_guardrails_env_matches",
                 "check_agent_auth_token_env_matches", "check_pull_mode_env_matches",
                 "check_agent_name_env_matches"):
        _patch(monkeypatch, lc, name, lambda *a, **k: True)
    for name in ("check_shared_folder_mounts_match", "check_base_image_matches"):
        _patch(monkeypatch, lc, name, _true_async)
    # HTTP into the container after it is up.
    for name in ("wait_for_agent_ready", "inject_assigned_credentials", "inject_assigned_skills",
                 "inject_read_only_hooks", "remove_read_only_hooks"):
        _patch(monkeypatch, lc, name, _ok)
    # Fire-and-forget execs into the container.
    from services import git_service, metric_registry, skill_gate_service
    monkeypatch.setattr(git_service, "spawn_gitignore_merge_after_clone", lambda *a, **k: None)
    monkeypatch.setattr(git_service, "spawn_git_remote_token_scrub", lambda *a, **k: None)
    monkeypatch.setattr(metric_registry, "spawn_refresh_from_running_agent", lambda *a, **k: None)
    monkeypatch.setattr(skill_gate_service, "spawn_gate_marker_sync", lambda *a, **k: None)
    # The self-heal's cross-worker lock is Redis: real semantics, in memory.
    svc = importlib.import_module("services.agent_mcp_key_service")
    lock = fakeredis.FakeRedis()
    monkeypatch.setattr(svc, "_regen_lock_client", lambda: lock)
    return seen


def _fake_docker_for_stop(monkeypatch):
    stopped = []

    async def _stop(c):
        stopped.append(c)

    container = _Container({}, status="running")
    _patch(monkeypatch, _AGENTS.stop_agent_endpoint, "get_agent_container", lambda name: container)
    _patch(monkeypatch, _AGENTS.stop_agent_endpoint, "container_stop", _stop)
    return stopped


def _seed_queued_execution():
    ex = db.create_task_execution(TARGET, "ow3180 queued task", triggered_by="manual",
                                  execution_id="ow3180-exec")
    _hrun("UPDATE schedule_executions SET status = 'queued' WHERE id = :i", i=ex.id)
    return ex.id


def _audit(action):
    return _q("SELECT mcp_key_id, mcp_scope, actor_email, target_id FROM audit_log "
              "WHERE event_action = :a ORDER BY id", a=action)


# =========================================================================== #
# Pass paths — the four routes, real key, real resolver, real route
# =========================================================================== #

class TestPassPaths:
    def test_reset(self, world, monkeypatch):
        key = world.mint("ops_write")
        reset = _fake_breakers(monkeypatch)
        res = world.client.post(f"/api/agents/{TARGET}/circuit-breaker/reset", headers=key.headers)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["agent_name"] == TARGET and body["prior_dispatch_state"] == "open"
        assert sorted(reset) == [("dispatch", TARGET), ("transport", TARGET)]

    def test_stop_is_attributed_to_the_key(self, world, monkeypatch):
        key = world.mint("ops_write")
        stopped = _fake_docker_for_stop(monkeypatch)
        res = world.client.post(f"/api/agents/{TARGET}/stop", headers=key.headers)
        assert res.status_code == 200, res.text
        assert len(stopped) == 1
        rows = _audit("stop")
        assert rows and rows[-1]["mcp_key_id"] == key.id and rows[-1]["mcp_scope"] == "ops_write"
        assert rows[-1]["target_id"] == TARGET and rows[-1]["actor_email"] == OWNER_EMAIL

    def test_terminate_cancels_a_queued_execution(self, world):
        key = world.mint("ops_write")
        eid = _seed_queued_execution()
        res = world.client.post(f"/api/agents/{TARGET}/executions/{eid}/terminate", headers=key.headers)
        assert res.status_code == 200, res.text
        assert res.json() == {"status": "cancelled_while_queued", "execution_id": eid}
        assert db.get_execution(eid).status == "cancelled"

    def test_start_self_heals_a_revoked_baked_key_and_hands_the_caller_nothing(self, world, monkeypatch):
        """The #1854 heal is authorized as a USE (plan D3): it mints the TARGET's
        own key into the target's container and prunes its superseded keys. No
        credential reaches the caller, and the caller's own reach is unchanged."""
        key = world.mint("ops_write")
        baked = db.create_agent_mcp_api_key(TARGET, OWNER)
        _hrun("UPDATE mcp_api_keys SET is_active = 0 WHERE id = :i", i=baked.id)  # revoked for containment
        # A second, still-active agent key the container does not hold — what
        # the heal captures as superseded and prunes.
        stale = db.create_agent_mcp_api_key(TARGET, OWNER)
        container = _Container({"TRINITY_MCP_API_KEY": baked.api_key,
                                "TRINITY_MCP_URL": "http://mcp-server:8080/mcp"})
        seen = _fake_docker_for_start(monkeypatch, container)

        res = world.client.post(f"/api/agents/{TARGET}/start", headers=key.headers)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["recreated"] is True and body["recreate_reason"] == "config_drift"

        # The heal ran for real: one recreate, carrying a freshly minted key whose
        # hash is an ACTIVE agent-scoped row bound to TARGET.
        assert len(seen["recreate"]) == 1
        overrides = seen["recreate"][0]["env_overrides"]
        fresh = overrides["TRINITY_MCP_API_KEY"]
        digest = hashlib.sha256(fresh.encode()).hexdigest()
        row = _q("SELECT scope, agent_name, is_active FROM mcp_api_keys WHERE key_hash = :h", h=digest)
        assert row == [{"scope": "agent", "agent_name": TARGET, "is_active": 1}]
        assert _key_row(stale.id) is None, "the superseded active key is pruned"
        assert _key_row(baked.id)["is_active"] == 0, "the revoked key stays revoked"
        active = _q("SELECT key_hash FROM mcp_api_keys WHERE agent_name = :a AND scope = 'agent' "
                    "AND is_active = 1", a=TARGET)
        assert active == [{"key_hash": digest}], "the target holds exactly its new key"

        # ...and nothing of it reached the caller.
        assert "trinity_mcp_" not in res.text and fresh not in res.text
        assert _key_row(key.id) == {"scope": "ops_write", "agent_name": None, "is_active": 1,
                                    "user_id": db.get_user_by_username(OWNER)["id"]}
        still = world.client.get("/api/mcp/keys", headers=key.headers)
        assert still.status_code == 403 and _detail_code(still) == "ops_write_route_not_listed"

        rows = _audit("start")
        assert rows and rows[-1]["mcp_key_id"] == key.id and rows[-1]["mcp_scope"] == "ops_write"


# =========================================================================== #
# Target policy — start / stop only
# =========================================================================== #

class TestTargetPolicy:
    @pytest.mark.parametrize("agent", [SYSTEM, FLAGGED, GHOST], ids=["system-agent", "is-system-flag", "ghost"])
    @pytest.mark.parametrize("verb", ["start", "stop"])
    def test_start_and_stop_refuse_protected_targets(self, world, monkeypatch, agent, verb):
        key = world.mint("ops_write")
        _fake_docker_for_stop(monkeypatch)
        _fake_docker_for_start(monkeypatch, _Container({}))
        res = world.client.post(f"/api/agents/{agent}/{verb}", headers=key.headers)
        assert res.status_code == 403 and _detail_code(res) == "ops_write_protected_target", res.text

    def test_system_stop_then_start_is_refused_at_stop(self, world, monkeypatch):
        """The #1816 human-only system-agent restart cannot be spelled as two calls."""
        key = world.mint("ops_write")
        stopped = _fake_docker_for_stop(monkeypatch)
        res = world.client.post(f"/api/agents/{SYSTEM}/stop", headers=key.headers)
        assert res.status_code == 403 and stopped == []

    @pytest.mark.parametrize("answer", ["raise", None, {}], ids=["lookup-raises", "no-row", "no-flag"])
    def test_an_unprovable_ghost_lookup_refuses(self, world, monkeypatch, answer):
        key = world.mint("ops_write")
        stopped = _fake_docker_for_stop(monkeypatch)

        def lookup(name):
            if answer == "raise":
                raise RuntimeError("db down")
            return answer

        monkeypatch.setattr(db, "get_agent_ephemeral_info", lookup)
        res = world.client.post(f"/api/agents/{TARGET}/stop", headers=key.headers)
        assert res.status_code == 403 and _detail_code(res) == "ops_write_protected_target", res.text
        assert stopped == []

    def test_the_policy_is_scope_specific(self, world, monkeypatch):
        """A person's session still stops a ghost — the rule binds this scope only."""
        stopped = _fake_docker_for_stop(monkeypatch)
        res = world.client.post(f"/api/agents/{GHOST}/stop", headers=world.jwt())
        assert res.status_code == 200, res.text
        assert len(stopped) == 1

    def test_reset_and_terminate_keep_every_agent_the_owner_reaches(self, world, monkeypatch):
        key = world.mint("ops_write")
        _fake_breakers(monkeypatch)
        res = world.client.post(f"/api/agents/{GHOST}/circuit-breaker/reset", headers=key.headers)
        assert res.status_code == 200, res.text
        ex = db.create_task_execution(SYSTEM, "q", triggered_by="manual", execution_id="ow3180-sys-exec")
        _hrun("UPDATE schedule_executions SET status = 'queued' WHERE id = :i", i=ex.id)
        res = world.client.post(f"/api/agents/{SYSTEM}/executions/{ex.id}/terminate", headers=key.headers)
        assert res.status_code == 200 and res.json()["status"] == "cancelled_while_queued", res.text


# =========================================================================== #
# Dies with its owner
# =========================================================================== #

def _call_four(world, key):
    out = {}
    for name, (method, path) in FOUR.items():
        out[name] = world.client.request(method, path, headers=key.headers)
    return out


class TestDiesWithItsOwner:
    @pytest.mark.parametrize("role", ["creator", "operator"])
    def test_a_demoted_owners_key_is_refused_on_all_four(self, world, monkeypatch, role):
        key = world.mint("ops_write")
        _fake_breakers(monkeypatch)
        stopped = _fake_docker_for_stop(monkeypatch)
        _seed_queued_execution()
        db.update_user_role(OWNER, role)
        for name, res in _call_four(world, key).items():
            assert res.status_code == 403 and _detail_code(res) == "ops_write_owner_not_admin", (name, res.text)
        assert stopped == [] and db.get_execution("ow3180-exec").status == "queued"

    def test_a_suspended_owners_key_is_refused_on_all_four(self, world):
        key = world.mint("ops_write")
        _hrun("UPDATE users SET suspended_at = '2026-10-01T00:00:00Z' WHERE username = :u", u=OWNER)
        for name, res in _call_four(world, key).items():
            assert res.status_code == 401, (name, res.text)

    def test_a_password_change_with_revoke_revokes_it(self, world):
        key = world.mint("ops_write")
        res = world.client.put("/api/users/me/password", headers=world.jwt(), json={
            "current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD, "revoke_api_keys": True})
        assert res.status_code == 200, res.text
        assert res.json()["api_keys_revoked"] == 1 and res.json()["api_keys_active"] == 0
        assert _key_row(key.id)["is_active"] == 0
        assert world.client.post(*FOUR["reset"][1:], headers=key.headers).status_code == 401

    def test_the_revoke_set_is_the_mintable_set(self):
        from db.mcp_keys import McpKeyOperations
        from services.password_service import PERSON_KEY_SCOPES
        assert PERSON_KEY_SCOPES == set(McpKeyOperations._USER_CREATABLE_SCOPES)
        assert "ops_write" in PERSON_KEY_SCOPES


# =========================================================================== #
# Mint matrix
# =========================================================================== #

class TestMint:
    """Composed: through the real route, every principal kind. Isolated: each
    redundant rung shown to refuse on its own with the rung that shadows it
    neutralized — `require_interactive` already runs
    `reject_non_interactive_principal`, and `assert_admin` already runs
    `reject_agent_principal`, so deleting one alone changes no composed result."""

    def _count(self):
        return _q("SELECT COUNT(*) AS n FROM mcp_api_keys WHERE scope = 'ops_write'")[0]["n"]

    def test_an_admin_session_mints_it_agentless_and_active(self, world):
        key = world.mint("ops_write")
        row = _key_row(key.id)
        assert row["scope"] == "ops_write" and row["agent_name"] is None and row["is_active"] == 1

    @pytest.mark.parametrize("who", ["non-admin-jwt", "user-key", "ops-key", "ops-write-key", "agent-key"])
    def test_every_other_principal_is_refused(self, world, who):
        if who == "non-admin-jwt":
            headers = world.jwt(PERSON)
        elif who == "user-key":
            headers = world.mint("user").headers
        elif who == "ops-key":
            headers = world.mint("ops").headers
        elif who == "ops-write-key":
            headers = world.mint("ops_write").headers
        else:
            headers = {"Authorization": f"Bearer {db.create_agent_mcp_api_key(TARGET, OWNER).api_key}"}
        before = self._count()
        res = world.client.post("/api/mcp/keys", json={"name": "x", "scope": "ops_write"}, headers=headers)
        assert res.status_code == 403, (who, res.text)
        assert self._count() == before

    def _as(self, world, **fields):
        base = dict(id=db.get_user_by_username(OWNER)["id"], username=OWNER, email=OWNER_EMAIL, role="admin",
                    agent_name=None, connector_agent=None, portal_delegate=False, mcp_scope=None,
                    mcp_key_id=None, mcp_key_name=None)
        base.update(fields)
        world.app.dependency_overrides[_KEYS.require_interactive] = lambda: User(**base)

    def test_isolated_the_in_handler_interactive_rung_refuses_a_key(self, world):
        self._as(world, mcp_scope="user", mcp_key_id="k-user-3180", mcp_key_name="user key")
        res = world.client.post("/api/mcp/keys", json={"name": "x", "scope": "ops_write"})
        assert res.status_code == 403 and "interactive session" in res.json()["detail"], res.text

    def test_isolated_the_agent_rung_refuses_an_agent(self, world, monkeypatch):
        self._as(world, mcp_scope="agent", agent_name=TARGET, mcp_key_id="k-agent-3180")
        _patch(monkeypatch, _KEYS.create_mcp_api_key_endpoint, "reject_non_interactive_principal", lambda u: None)
        res = world.client.post("/api/mcp/keys", json={"name": "x", "scope": "ops_write"})
        assert res.status_code == 403 and "human-only" in res.json()["detail"], res.text

    def test_isolated_the_admin_gate_refuses_an_agent_alone(self, world, monkeypatch):
        self._as(world, mcp_scope="agent", agent_name=TARGET, mcp_key_id="k-agent-3180")
        _patch(monkeypatch, _KEYS.create_mcp_api_key_endpoint, "reject_non_interactive_principal", lambda u: None)
        _patch(monkeypatch, _KEYS.create_mcp_api_key_endpoint, "reject_agent_principal", lambda u: None)
        res = world.client.post("/api/mcp/keys", json={"name": "x", "scope": "ops_write"})
        assert res.status_code == 403, res.text

    def test_isolated_the_admin_gate_refuses_a_non_admin_session(self, world):
        self._as(world, role="creator")
        res = world.client.post("/api/mcp/keys", json={"name": "x", "scope": "ops_write"})
        assert res.status_code == 403 and res.json()["detail"] == "Admin access required", res.text

    def test_the_db_layer_refuses_what_it_cannot_mint(self):
        from db.mcp_keys import McpKeyOperations
        assert "ops_write" in McpKeyOperations._USER_CREATABLE_SCOPES

    def test_the_db_layer_writes_it_agentless(self, world):
        created = db.create_mcp_api_key(OWNER, McpApiKeyCreate(name="db", description=None, scope="ops_write"))
        assert created is not None and _key_row(created.id)["agent_name"] is None
        assert db.create_mcp_api_key(OWNER, McpApiKeyCreate(name="db", description=None,
                                                            scope="ops_write_2027")) is None


# =========================================================================== #
# The negatives the slice must pin
# =========================================================================== #

class TestNegatives:
    def test_the_read_only_ops_key_cannot_reach_the_four(self, world, monkeypatch):
        key = world.mint("ops")
        _fake_breakers(monkeypatch)
        stopped = _fake_docker_for_stop(monkeypatch)
        _seed_queued_execution()
        for name, res in _call_four(world, key).items():
            assert res.status_code == 403 and res.json()["detail"].startswith("Ops keys"), (name, res.text)
        assert stopped == [] and db.get_execution("ow3180-exec").status == "queued"

    def test_an_unlisted_agent_route_refuses_identically_for_a_real_and_a_missing_agent(self, world):
        key = world.mint("ops_write")
        real = world.client.get(f"/api/agents/{TARGET}/logs", headers=key.headers)
        missing = world.client.get(f"/api/agents/{NOPE}/logs", headers=key.headers)
        assert real.status_code == missing.status_code == 403
        assert real.json() == missing.json()

    def test_a2a_answers_403_with_no_dispatch_and_no_anonymous_fallback(self, world, monkeypatch):
        calls = {"anonymous": 0, "dispatch": 0}

        async def anonymous(agent_name, request):
            calls["anonymous"] += 1
            from fastapi.responses import JSONResponse
            return JSONResponse({"anonymous": True}, status_code=402)

        async def dispatch(*a, **k):
            calls["dispatch"] += 1
            return {"dispatched": True}

        monkeypatch.setitem(_A2A.a2a_jsonrpc.__globals__, "_anonymous_jsonrpc", anonymous)
        monkeypatch.setitem(_A2A.a2a_jsonrpc.__globals__, "_dispatch_jsonrpc", dispatch)
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}}

        key = world.mint("ops_write")
        res = world.client.post(f"/a2a/{TARGET}", json=rpc, headers=key.headers)
        assert res.status_code == 403 and _detail_code(res) == "ops_write_route_not_listed", res.text
        assert calls == {"anonymous": 0, "dispatch": 0}

        # Controls, so the zeros above are about the key: an unrecognised key
        # and no key at all both take the anonymous path.
        assert world.client.post(f"/a2a/{TARGET}", json=rpc,
                                 headers={"Authorization": "Bearer trinity_mcp_unknown3180"}).status_code == 402
        assert world.client.post(f"/a2a/{TARGET}", json=rpc).status_code == 402
        assert calls == {"anonymous": 2, "dispatch": 0}


# =========================================================================== #
# Verify verdict
# =========================================================================== #

def test_an_ops_write_key_in_an_agents_config_reports_its_own_verdict(world):
    from services import agent_mcp_key_service as svc
    key = world.mint("ops_write")
    row = _q("SELECT id, scope, agent_name, is_active, key_prefix FROM mcp_api_keys WHERE id = :i", i=key.id)
    verdict = svc._entry_verdict(row[0], TARGET)
    assert verdict == "foreign_ops_write_key"
    entry = SimpleNamespace(verdict=verdict, key_prefix=key.secret[:20], key_agent_name=None)
    message = svc._verdict_message(entry)
    assert "ops-write key" in message and "personal" not in message


# =========================================================================== #
# Other shape — another origin or hostname (#3102)
# =========================================================================== #

def test_a_crafted_host_cannot_make_the_fence_see_a_listed_route(world):
    """A `Host` carrying a path makes Starlette's `request.url.path` name a
    listed route while the router dispatched an unlisted one. The fence reads
    `scope["path"]` and `scope["route"]`, so it still refuses — through the real
    routers, end to end. (`test_3102_host_header_fences.py` pins the same for
    the fence alone; production also rejects such a Host at `HostHeaderGuard`.)"""
    key = world.mint("ops_write")
    seen = []
    app = FastAPI()

    @app.middleware("http")
    async def record(request, call_next):
        seen.append((request.url.path, request.scope["path"]))
        return await call_next(request)

    app.include_router(_CHAT.router)
    app.include_router(_AGENTS.router)
    client = TestClient(app)
    listed = f"/api/agents/{TARGET}/start"
    routed = f"/api/agents/{TARGET}/chat"
    res = client.post(routed, json={"message": "hi"}, headers={**key.headers, "host": f"backend{listed}?"})
    # Precondition: the desync reached the app — the URL names the listed route.
    assert seen == [(listed, routed)], seen
    assert res.status_code == 403 and _detail_code(res) == "ops_write_route_not_listed", res.text
    honest = client.post(routed, json={"message": "hi"}, headers=key.headers)
    assert honest.json() == res.json()
