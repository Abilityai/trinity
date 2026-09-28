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

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
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
import services.agent_service.api_key as _APIKEY  # noqa: E402
import services.agent_service.autonomy as _AUTONOMY  # noqa: E402
import services.agent_service.read_only as _READONLY  # noqa: E402
from database import db  # noqa: E402
from db_models import McpApiKeyCreate, UserCreate  # noqa: E402

pytestmark = pytest.mark.unit

OWNER = "cfg2996-owner"
ADMIN = "cfg2996-admin"
AGENT = "cfg2996-agent"
SYSTEM = "cfg2996-system"
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


@pytest.fixture
def world(db_backend, monkeypatch):
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: None)
    for mod in (_CFG, _AUTONOMY, _APIKEY, _READONLY):
        monkeypatch.setattr(mod, "get_agent_container", lambda _n: _STOPPED)
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
