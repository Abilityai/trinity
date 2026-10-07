"""#2996 — autonomy and the agent-config toggles are person-only.

`routers/agent_config.py` is the owner configuration surface: autonomy decides
whether an agent's cron schedules fire unattended, and the other toggles set its
guardrails, resources, capabilities and model. Every write there takes
`Depends(require_person)` — a JWT session or the person's own `user`-scoped key;
an agent-scoped key (on its own agent or any other), the system key, and every
other scope get a 403 with `HUMAN_ONLY_DETAIL`.

Two harnesses, on purpose:

* The REAL `get_current_user` with key rows seeded in the real schema
  (`tests/db_harness.py`) and presented as bearers — agent key, system key, the
  owner's user key, JWTs. A dependency override would skip the `scope or "user"`
  coercion and the entry-point fences, so the principal kinds that matter most
  go through the real path.
* An override of `get_current_user` with a stated principal (every field set,
  never a `MagicMock`) for the kinds the real path fences before the route
  (ops, portal_delegate) or cannot mint (an unknown future scope, the loopback
  JWT, a `user` principal carrying an agent identity) — so the ROUTE's gate is
  what refuses them.

Every refusal asserts the stored value did not move, from a non-ambient seed;
every admission asserts it did.

Related: docs/memory/requirements/auth.md §2.8, feature-flows/autonomy-mode.md.
"""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Optional

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from db_harness import db_backend, run as _hrun  # noqa: E402,F401

import dependencies  # noqa: E402
import routers.agent_config as _CFG  # noqa: E402
from database import db  # noqa: E402
from db_models import McpApiKeyCreate, UserCreate  # noqa: E402

pytestmark = pytest.mark.unit

OWNER = "cfg2996-owner"
ADMIN = "cfg2996-admin"
AGENT = "cfg2996-agent"
SYSTEM = "cfg2996-system"
ORCH = "cfg2996-orch"              # a sibling the same owner owns
ADMIN_ORCH = "cfg2996-admin-orch"  # an agent an ADMIN owns
GHOST = "cfg2996-no-such-agent"


@dataclass
class _Principal:
    """Every field `models.User` carries, stated — never a MagicMock."""

    id: int
    username: str
    email: Optional[str]
    role: str
    agent_name: Optional[str] = None
    connector_agent: Optional[str] = None
    portal_delegate: bool = False
    mcp_scope: Optional[str] = None
    mcp_key_id: Optional[str] = None
    mcp_key_name: Optional[str] = None
    vouched_source_agent: Optional[str] = None


_STOPPED = SimpleNamespace(status="exited", attrs={"Config": {"Labels": {}}})


_READONLY_NAME = "services.agent_service.read_only"


def _stub_containers(monkeypatch):
    """Point every container lookup the routes make at a stopped container —
    on the module objects the routes will ACTUALLY call, not the ones this file
    happened to import.

    The router and the autonomy / api-key logic bind at import, so their lookup
    is patched through the globals of the functions the router holds. The
    read-only logic is imported lazily inside the route, from `sys.modules`
    at call time, and sibling unit files evict or stub that entry
    (`test_validate_runtime.py` pops it at import); so the real module is
    resolved here, pinned into `sys.modules` for this test, and patched.
    """
    stub = lambda _n: _STOPPED  # noqa: E731
    monkeypatch.setattr(_CFG, "get_agent_container", stub)
    for fn in (_CFG.set_autonomy_status_logic, _CFG.update_agent_api_key_setting_logic):
        monkeypatch.setitem(fn.__globals__, "get_agent_container", stub)
    mod = sys.modules.get(_READONLY_NAME)
    if not (isinstance(mod, ModuleType) and (getattr(mod, "__file__", "") or "").endswith("read_only.py")):
        monkeypatch.delitem(sys.modules, _READONLY_NAME, raising=False)
        mod = importlib.import_module(_READONLY_NAME)
    monkeypatch.setitem(sys.modules, _READONLY_NAME, mod)
    monkeypatch.setattr(mod, "get_agent_container", stub)


@pytest.fixture
def world(db_backend, monkeypatch):
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: None)
    _stub_containers(monkeypatch)
    db.create_user(
        UserCreate(username=OWNER, role="user", email="cfg2996-owner@example.com")
    )
    db.create_user(
        UserCreate(username=ADMIN, role="admin", email="cfg2996-admin@example.com")
    )
    db.register_agent_owner(AGENT, OWNER)
    owner_id = db.get_user_by_username(OWNER)["id"]

    app = FastAPI()
    app.include_router(_CFG.router)
    client = TestClient(app)

    def as_(**fields):
        base = {
            "id": owner_id,
            "username": OWNER,
            "email": "cfg2996-owner@example.com",
            "role": "user",
        }
        base.update(fields)
        app.dependency_overrides[dependencies.get_current_user] = lambda: _Principal(
            **base
        )

    yield SimpleNamespace(client=client, app=app, as_=as_)
    app.dependency_overrides.clear()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _jwt(username: str) -> dict:
    return _auth(dependencies.create_access_token({"sub": username}))


def _agent_key(agent: str = AGENT) -> dict:
    return _auth(db.create_agent_mcp_api_key(agent, OWNER).api_key)


def _admin_owned_agent_key() -> dict:
    """The key that carries `role=admin` without being a person (#3236 review)."""
    db.register_agent_owner(ADMIN_ORCH, ADMIN)
    return _auth(db.create_agent_mcp_api_key(ADMIN_ORCH, ADMIN).api_key)


def _system_key() -> dict:
    """Created the way the platform creates it: an agent key under the admin,
    then its scope column set to `system`."""
    key = db.create_agent_mcp_api_key(SYSTEM, ADMIN)
    _hrun("UPDATE mcp_api_keys SET scope = 'system' WHERE id = :i", i=key.id)
    return _auth(key.api_key)


def _owner_user_key() -> dict:
    return _auth(
        db.create_mcp_api_key(OWNER, McpApiKeyCreate(name="owner key")).api_key
    )


# Principal kinds refused by the ROUTE (the real path fences ops/portal_delegate
# earlier, and the rest cannot be minted as a key row).
OVERRIDE_REFUSED = [
    pytest.param({"mcp_scope": "ops"}, id="ops-key"),
    pytest.param(
        {"mcp_scope": "portal_delegate", "portal_delegate": True},
        id="portal-delegate-key",
    ),
    pytest.param(
        {"mcp_scope": "connector", "connector_agent": AGENT}, id="connector-key"
    ),
    pytest.param({"mcp_scope": "a-scope-shipped-tomorrow"}, id="unknown-future-scope"),
    pytest.param(
        {"mcp_scope": "user", "agent_name": AGENT}, id="user-scope-carrying-an-agent"
    ),
    pytest.param({"vouched_source_agent": AGENT}, id="loopback-jwt"),
]


def _is_human_only_refusal(res) -> bool:
    detail = res.json().get("detail")
    return (
        res.status_code == 403
        and isinstance(detail, dict)
        and detail.get("code") == "person_required"
    )


# ===========================================================================
# PUT /api/agents/{name}/autonomy
# ===========================================================================

AUTONOMY = f"/api/agents/{AGENT}/autonomy"


def _autonomy() -> bool:
    return bool(db.get_autonomy_enabled(AGENT))


class TestAutonomyRealKeys:
    @pytest.fixture(autouse=True)
    def _seed_off(self, world):
        db.set_autonomy_enabled(
            AGENT, False
        )  # non-ambient start; every request asks for True
        assert _autonomy() is False

    @pytest.mark.parametrize(
        "headers",
        [
            pytest.param(lambda: _agent_key(), id="agent-key-own-agent"),
            pytest.param(lambda: _system_key(), id="system-key"),
        ],
    )
    def test_a_machine_key_is_refused_and_autonomy_stays_off(self, world, headers):
        res = world.client.put(AUTONOMY, json={"enabled": True}, headers=headers())
        assert _is_human_only_refusal(res), res.text
        assert _autonomy() is False

    def test_an_agent_key_is_refused_on_a_sibling_too(self, world):
        db.register_agent_owner("cfg2996-sibling", OWNER)
        res = world.client.put(
            AUTONOMY, json={"enabled": True}, headers=_agent_key("cfg2996-sibling")
        )
        assert _is_human_only_refusal(res), res.text
        assert _autonomy() is False

    def test_the_refusal_does_not_disclose_whether_the_agent_exists(self, world):
        res = world.client.put(
            f"/api/agents/{GHOST}/autonomy",
            json={"enabled": True},
            headers=_agent_key(),
        )
        assert _is_human_only_refusal(res), res.text

    @pytest.mark.parametrize(
        "headers",
        [
            pytest.param(lambda: _jwt(OWNER), id="owner-jwt"),
            pytest.param(lambda: _jwt(ADMIN), id="admin-jwt-not-owner"),
            pytest.param(lambda: _owner_user_key(), id="owner-user-key"),
        ],
    )
    def test_a_person_turns_autonomy_on(self, world, headers):
        res = world.client.put(AUTONOMY, json={"enabled": True}, headers=headers())
        assert res.status_code == 200, res.text
        assert _autonomy() is True


class TestAutonomyEveryOtherPrincipal:
    @pytest.fixture(autouse=True)
    def _seed_off(self, world):
        db.set_autonomy_enabled(AGENT, False)

    @pytest.mark.parametrize("principal", OVERRIDE_REFUSED)
    def test_refused_by_the_route_gate(self, world, principal):
        world.as_(**principal)
        res = world.client.put(AUTONOMY, json={"enabled": True})
        assert _is_human_only_refusal(res), res.text
        assert _autonomy() is False

    def test_a_principal_without_mcp_scope_fails_closed(self, world):
        world.app.dependency_overrides[dependencies.get_current_user] = (
            lambda: SimpleNamespace(
                id=1, username=OWNER, email="cfg2996-owner@example.com", role="user"
            )
        )
        res = world.client.put(AUTONOMY, json={"enabled": True})
        assert res.status_code == 403, res.text
        assert _autonomy() is False

    def test_the_read_stays_open_to_an_agent_key(self, world):
        """GET /autonomy is a read at access level — unchanged."""
        res = world.client.get(AUTONOMY, headers=_agent_key())
        assert res.status_code == 200, res.text


# ===========================================================================
# The other eight agent-config writes — same file, same class, same rule
# ===========================================================================


@dataclass(frozen=True)
class _ConfigWrite:
    """One owner-config write: how to seed a non-ambient stored value, the body
    that changes it to a different non-ambient value, and how to read it back."""

    path: str
    seed: object
    body: dict
    read: object
    expected_after: object


_MODEL = "claude-sonnet-4-6"

CONFIG_WRITES = {
    "api-key-setting": _ConfigWrite(
        "api-key-setting",
        seed=lambda: db.set_use_platform_api_key(AGENT, False),
        body={"use_platform_api_key": True},
        read=lambda: db.get_use_platform_api_key(AGENT),
        expected_after=True,
    ),
    "read-only": _ConfigWrite(
        "read-only",
        seed=lambda: db.set_read_only_mode(AGENT, False, {"blocked_patterns": ["seed/*"], "allowed_patterns": []}),
        body={"enabled": True, "config": {"blocked_patterns": ["*.md"], "allowed_patterns": []}},
        read=lambda: db.get_read_only_mode(AGENT)["enabled"],
        expected_after=True,
    ),
    "resources": _ConfigWrite(
        "resources",
        seed=lambda: db.set_resource_limits(AGENT, memory="2g", cpu="1"),
        body={"memory": "8g", "cpu": "4"},
        read=lambda: db.get_resource_limits(AGENT),
        expected_after={"memory": "8g", "cpu": "4"},
    ),
    "capabilities": _ConfigWrite(
        "capabilities",
        seed=lambda: db.set_full_capabilities(AGENT, False),
        body={"full_capabilities": True},
        read=lambda: db.get_full_capabilities(AGENT),
        expected_after=True,
    ),
    "capacity": _ConfigWrite(
        "capacity",
        seed=lambda: db.set_max_parallel_tasks(AGENT, 2),
        body={"max_parallel_tasks": 5},
        read=lambda: db.get_max_parallel_tasks(AGENT),
        expected_after=5,
    ),
    "timeout": _ConfigWrite(
        "timeout",
        seed=lambda: db.set_execution_timeout(AGENT, 900),
        body={"execution_timeout_seconds": 1500},
        read=lambda: db.get_execution_timeout(AGENT),
        expected_after=1500,
    ),
    "public-channel-model": _ConfigWrite(
        "public-channel-model",
        seed=lambda: db.set_public_channel_model(AGENT, "claude-haiku-4-5-20251001"),
        body={"model": _MODEL},
        read=lambda: db.get_public_channel_model(AGENT),
        expected_after=_MODEL,
    ),
    "guardrails": _ConfigWrite(
        "guardrails",
        seed=lambda: db.set_guardrails_config(AGENT, {"max_turns_chat": 7}),
        body={"max_turns_chat": 42},
        read=lambda: db.get_guardrails_config(AGENT),
        expected_after={"max_turns_chat": 42},
    ),
}

WRITE_IDS = sorted(CONFIG_WRITES)
# trinity-enterprise#164: these five are `agents.manage`-grantable — an agent
# holding the grant may change them; one without it gets the NAMED refusal.
GRANTABLE = {"read-only", "resources", "timeout", "public-channel-model", "guardrails"}
# #3236 review: grantable on a sibling, person-only on the holder itself.
SELF_PERSON_ONLY = {"read-only", "guardrails"}


def _is_capability_refusal(res) -> bool:
    detail = res.json().get("detail")
    return (
        res.status_code == 403
        and isinstance(detail, dict)
        and detail.get("code") == "agent_management_not_permitted"
    )


def _seeded(name: str):
    w = CONFIG_WRITES[name]
    w.seed()
    before = w.read()
    assert before != w.expected_after, f"{name}: the seed must differ from the target"
    return w, before


def _put(world, w: _ConfigWrite, headers=None):
    return world.client.put(f"/api/agents/{AGENT}/{w.path}", json=w.body, headers=headers or {})


class TestConfigWritesRealKeys:
    @pytest.mark.parametrize("name", WRITE_IDS)
    @pytest.mark.parametrize("headers", [
        pytest.param(lambda: _agent_key(), id="agent-key-own-agent"),
        pytest.param(lambda: _system_key(), id="system-key"),
    ])
    def test_a_machine_key_is_refused_and_the_value_does_not_move(self, world, name, headers, request):
        w, before = _seeded(name)
        res = _put(world, w, headers())
        if name in GRANTABLE - SELF_PERSON_ONLY and "agent-key" in request.node.callspec.id:
            assert _is_capability_refusal(res), res.text      # ent#164: named, askable
        else:
            assert _is_human_only_refusal(res), res.text
        assert w.read() == before

    @pytest.mark.parametrize("name", sorted(GRANTABLE))
    def test_an_agent_holding_agents_manage_changes_the_stored_value(self, world, name):
        """ent#164 through the REAL `get_current_user` and the real grant table:
        a holder reconfigures a sibling its owner owns."""
        w, before = _seeded(name)
        db.register_agent_owner(ORCH, OWNER)
        db.grant_agent_capability(ORCH, "agents.manage", "admin")
        try:
            res = _put(world, w, _agent_key(ORCH))
            assert 200 <= res.status_code < 300, res.text
            assert w.read() == w.expected_after
        finally:
            db.revoke_agent_capability(ORCH, "agents.manage")

    @pytest.mark.parametrize("name", sorted(SELF_PERSON_ONLY))
    def test_a_holder_cannot_lift_its_own_read_only_or_guardrails(self, world, name):
        """#3236 review: the agent a setting constrains must not be the one that
        lifts it — person-only when target == caller, grant or not."""
        w, before = _seeded(name)
        db.grant_agent_capability(AGENT, "agents.manage", "admin")
        try:
            res = _put(world, w, _agent_key())
            assert _is_human_only_refusal(res), res.text
            assert w.read() == before
        finally:
            db.revoke_agent_capability(AGENT, "agents.manage")

    @pytest.mark.parametrize("name", sorted(GRANTABLE - SELF_PERSON_ONLY))
    def test_a_holder_may_still_change_its_own_other_settings(self, world, name):
        w, before = _seeded(name)
        db.grant_agent_capability(AGENT, "agents.manage", "admin")
        try:
            res = _put(world, w, _agent_key())
            assert 200 <= res.status_code < 300, res.text
            assert w.read() == w.expected_after
        finally:
            db.revoke_agent_capability(AGENT, "agents.manage")

    @pytest.mark.parametrize("name", sorted(GRANTABLE))
    def test_an_admin_owned_holder_cannot_reach_another_users_agent(self, world, name):
        """#3236 review: these handlers authorise through `can_user_share_agent`,
        which admits any admin — and an admin-owned agent's key carries `admin`.
        One grant reached every agent on the instance; now only its owner's."""
        w, before = _seeded(name)
        db.register_agent_owner(ADMIN_ORCH, ADMIN)
        db.grant_agent_capability(ADMIN_ORCH, "agents.manage", "admin")
        try:
            res = world.client.put(f"/api/agents/{AGENT}/{w.path}", json=w.body,
                                   headers=_auth(db.create_agent_mcp_api_key(ADMIN_ORCH, ADMIN).api_key))
            assert res.status_code == 404, res.text
            assert w.read() == before
        finally:
            db.revoke_agent_capability(ADMIN_ORCH, "agents.manage")

    @pytest.mark.parametrize("name", sorted(set(WRITE_IDS) - GRANTABLE))
    def test_the_grant_does_not_open_the_person_only_writes(self, world, name):
        w, before = _seeded(name)
        db.grant_agent_capability(AGENT, "agents.manage", "admin")
        try:
            res = _put(world, w, _agent_key())
            assert _is_human_only_refusal(res), res.text
            assert w.read() == before
        finally:
            db.revoke_agent_capability(AGENT, "agents.manage")

    @pytest.mark.parametrize("name", WRITE_IDS)
    @pytest.mark.parametrize("headers", [
        pytest.param(lambda: _jwt(OWNER), id="owner-jwt"),
        pytest.param(lambda: _owner_user_key(), id="owner-user-key"),
    ])
    def test_a_person_changes_the_stored_value(self, world, name, headers):
        w, before = _seeded(name)
        res = _put(world, w, headers())
        assert 200 <= res.status_code < 300, res.text
        assert w.read() == w.expected_after


class TestConfigWritesEveryOtherPrincipal:
    @pytest.mark.parametrize("name", WRITE_IDS)
    @pytest.mark.parametrize("principal", OVERRIDE_REFUSED)
    def test_refused_by_the_route_gate(self, world, name, principal):
        w, before = _seeded(name)
        world.as_(**principal)
        res = _put(world, w)
        assert _is_human_only_refusal(res), res.text
        assert w.read() == before


def test_every_write_in_the_file_is_person_gated():
    """The eight writes above plus autonomy are every PUT in agent_config.py —
    a ninth would need a row here (and the route census names it too). The
    ent#164 grant route is admin-and-interactive, pinned by its own suite."""
    puts = sorted(
        r.path.rsplit("/", 1)[-1]
        for r in _CFG.router.routes
        if "PUT" in getattr(r, "methods", set())
        and not r.path.endswith("/capability-grants/{capability}")
    )
    assert puts == sorted(WRITE_IDS + ["autonomy"])


# ===========================================================================
# trinity-enterprise#164 — the grant route: interactive admin only
# ===========================================================================


class TestCapabilityGrantRoute:
    URL = f"/api/agents/{AGENT}/capability-grants"

    def _held(self, world, headers):
        res = world.client.get(self.URL, headers=headers)
        assert res.status_code == 200, res.text
        return {g["capability"] for g in res.json()["grants"] if g["granted"]}

    def test_an_admin_session_grants_and_revokes(self, world, monkeypatch):
        audit = []

        async def log(**kw):
            audit.append(kw)
        monkeypatch.setattr(_CFG.platform_audit_service, "log", log)
        res = world.client.put(f"{self.URL}/agents.manage", json={"granted": True}, headers=_jwt(ADMIN))
        assert res.status_code == 200 and res.json()["changed"] is True, res.text
        assert db.agent_has_capability(AGENT, "agents.manage")
        assert self._held(world, _jwt(OWNER)) == {"agents.manage"}
        again = world.client.put(f"{self.URL}/agents.manage", json={"granted": True}, headers=_jwt(ADMIN))
        assert again.json()["changed"] is False                     # idempotent, one audit row
        world.client.put(f"{self.URL}/agents.manage", json={"granted": False}, headers=_jwt(ADMIN))
        assert not db.agent_has_capability(AGENT, "agents.manage")
        assert [a["event_action"] for a in audit] == ["capability_grant", "capability_revoke"]

    @pytest.mark.parametrize("headers", [
        pytest.param(lambda: _jwt(OWNER), id="owner-non-admin"),
        pytest.param(lambda: _agent_key(), id="agent-key"),
        pytest.param(lambda: _system_key(), id="system-key"),
        pytest.param(lambda: _admin_owned_agent_key(), id="admin-owned-agent-key"),
        pytest.param(lambda: _auth(db.create_mcp_api_key(
            ADMIN, McpApiKeyCreate(name="admin script")).api_key), id="admin-user-key"),
    ])
    def test_nobody_else_can_grant(self, world, headers):
        res = world.client.put(f"{self.URL}/agents.manage", json={"granted": True}, headers=headers())
        assert res.status_code == 403, res.text
        assert not db.agent_has_capability(AGENT, "agents.manage")

    def test_an_unknown_capability_is_a_named_422(self, world):
        res = world.client.put(f"{self.URL}/autonomy.manage", json={"granted": True}, headers=_jwt(ADMIN))
        assert res.status_code == 422, res.text
