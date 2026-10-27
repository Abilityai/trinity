"""#3180 — the two unaudited operational writes now leave an audit row.

`POST /api/agents/{agent_name}/circuit-breaker/reset` is the most-run write of
the real ops consumer, and `POST /api/agents/{name}/executions/{id}/terminate`
ends a running turn. Before this, neither wrote to `audit_log`, so a person's
reset, a `user`-scoped key's reset and a terminate were all invisible after the
fact. Now each successful call writes ONE row, attributed to the principal; the
key fields (`mcp_key_id` / `mcp_key_name` / `mcp_scope`) are derived from that
principal by `platform_audit_service.log` (#2389), so a key's write names the key.

Everything runs through the REAL credential resolver (`get_current_user`, with a
JWT or a key row minted in the real schema) and the REAL routes, on a real
SQLite store (`tests/db_harness.py`), and the rows are read back from the real
`audit_log` table. The only fakes are the boundaries a unit run has no access
to: Redis (a fakeredis instance every breaker, token and in-flight lookup
shares), the agent container lookup, the HTTP call into the container, and the
capacity manager's slot release.

Pinned values differ from what production returns by default: the breaker
master switch and the per-agent toggle are ON (both default off), both
breakers are OPEN (default closed), the principals' usernames, emails, roles
and key names are distinctive, and the execution rows carry a fixed date.

The "other shape" is the feature flag off: `DISPATCH_BREAKER_ENABLED` false and
the per-agent breaker disabled, as on the consumer instance where the reset was
run against a tripped transport breaker. The reset still writes its row.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import fakeredis
import httpx
import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from db_harness import db_backend, seed_execution  # noqa: E402,F401

import config  # noqa: E402
import dependencies  # noqa: E402
import redis_breaker_util  # noqa: E402
import routers.agents as agents_router  # noqa: E402
import routers.chat as chat_router  # noqa: E402
from database import db  # noqa: E402
from db_models import McpApiKeyCreate, UserCreate  # noqa: E402

pytestmark = pytest.mark.unit

ADMIN = "s2-3180-admin"
ADMIN_EMAIL = "s2-3180-admin@example.test"
OWNER = "s2-3180-operator"
OWNER_EMAIL = "s2-3180-operator@example.test"
OUTSIDER = "s2-3180-outsider"
AGENT = "s2-3180-agent"  # owned by OWNER (an operator)
ADMIN_AGENT = "s2-3180-admin-agent"  # owned by ADMIN
ADMIN_KEY_NAME = "s2-3180 ops automation"
OWNER_KEY_NAME = "s2-3180 owner cli"

RESET = "circuit_breaker_reset"
TERMINATE = "execution_terminate"
# A fixed date, never compared to "now".
STARTED_AT = "2026-03-14T09:26:53.000000Z"


def _reset_path(agent: str = AGENT) -> str:
    return f"/api/agents/{agent}/circuit-breaker/reset"


def _terminate_path(execution_id: str, agent: str = AGENT) -> str:
    return f"/api/agents/{agent}/executions/{execution_id}/terminate"


# --------------------------------------------------------------------------- #
# The container side of the proxy arm: the HTTP call the backend makes into
# `agent-{name}:8000`. Recorded, so a test can assert it was (or was not) made.
# --------------------------------------------------------------------------- #


class _AgentReply:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _AgentBoundary:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.status_code = 200
        self.payload: dict = {"status": "terminated", "returncode": -2}
        self.exc: Exception | None = None

    def client(self, name: str, timeout=None):
        boundary = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, *args, **kwargs):
                boundary.calls.append((name, url))
                if boundary.exc is not None:
                    raise boundary.exc
                return _AgentReply(boundary.status_code, boundary.payload)

        return _Client()


class _Capacity:
    """The slot release the proxy and parked arms make. Recorded, not modelled."""

    def __init__(self) -> None:
        self.released: list[tuple[str, str]] = []

    async def release_if_matches(self, name: str, execution_id: str) -> bool:
        self.released.append((name, execution_id))
        return True


# --------------------------------------------------------------------------- #
# The world
# --------------------------------------------------------------------------- #


def _install_fake_redis(monkeypatch) -> fakeredis.FakeRedis:
    """One fakeredis behind every Redis lookup the two routes make: token
    revocation in the resolver, both breakers, and the in-flight marker read.

    Patched on the module objects the code under test resolves at call time
    (the reset handler imports its breaker modules inside the function)."""
    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_breaker_util, "_redis_client", fake)
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: fake)
    breaker = importlib.import_module("services.dispatch_breaker")
    monkeypatch.setattr(breaker, "get_breaker_redis", lambda: fake)
    circuit = importlib.import_module("services.agent_client.circuit")
    monkeypatch.setattr(circuit, "_get_circuit_redis", lambda: fake)
    limiter = chat_router.chat_execution_service.agent_call_limiter
    monkeypatch.setattr(limiter, "_client_factory", lambda: fake)
    monkeypatch.setattr(limiter, "_REDIS_UNAVAILABLE_UNTIL", 0.0)
    return fake


def _open_both_breakers(fake: fakeredis.FakeRedis, agent: str) -> None:
    fake.hset(f"agent:circuit:{agent}", mapping={"state": "open", "failures": "7"})
    fake.hset(f"agent:dispatch:{agent}", mapping={"state": "open", "failures": "5"})


def _breaker_states(fake: fakeredis.FakeRedis, agent: str) -> tuple:
    return (
        fake.hget(f"agent:circuit:{agent}", "state"),
        fake.hget(f"agent:dispatch:{agent}", "state"),
    )


@pytest.fixture
def world(db_backend, monkeypatch):
    fake = _install_fake_redis(monkeypatch)
    # Pinned ON: both default off in production.
    monkeypatch.setattr(config, "DISPATCH_BREAKER_ENABLED", True)

    db.create_user(UserCreate(username=ADMIN, role="admin", email=ADMIN_EMAIL))
    db.create_user(UserCreate(username=OWNER, role="operator", email=OWNER_EMAIL))
    db.create_user(
        UserCreate(
            username=OUTSIDER, role="operator", email="s2-3180-outsider@example.test"
        )
    )
    db.register_agent_owner(AGENT, OWNER)
    db.register_agent_owner(ADMIN_AGENT, ADMIN)
    db.set_circuit_breaker_enabled(AGENT, True)
    db.set_circuit_breaker_enabled(ADMIN_AGENT, True)
    _open_both_breakers(fake, AGENT)
    _open_both_breakers(fake, ADMIN_AGENT)

    ce = chat_router.chat_execution_service
    agent = _AgentBoundary()
    capacity = _Capacity()
    container = {"value": SimpleNamespace(status="running")}
    monkeypatch.setattr(ce, "agent_httpx_client", agent.client)
    monkeypatch.setattr(ce, "get_agent_container", lambda _name: container["value"])
    monkeypatch.setattr(ce, "get_capacity_manager", lambda: capacity)
    # The real activity service writes its rows to the real store; nothing here
    # is listening on a socket.
    monkeypatch.setattr(ce.activity_service, "websocket_manager", None)
    monkeypatch.setattr(ce.activity_service, "filtered_websocket_manager", None)

    app = FastAPI()
    app.include_router(agents_router.router)
    app.include_router(chat_router.router)
    client = TestClient(app, raise_server_exceptions=False)

    yield SimpleNamespace(
        client=client, redis=fake, agent=agent, capacity=capacity, container=container
    )


# --------------------------------------------------------------------------- #
# Credentials: presented as bearers, resolved by the real resolver
# --------------------------------------------------------------------------- #


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _session(username: str) -> dict:
    return _bearer(dependencies.create_access_token({"sub": username}))


def _user_key(username: str, name: str) -> tuple[dict, str]:
    key = db.create_mcp_api_key(username, McpApiKeyCreate(name=name, scope="user"))
    return _bearer(key.api_key), key.id


def _ops_key() -> dict:
    return _bearer(
        db.create_mcp_api_key(
            ADMIN, McpApiKeyCreate(name="s2-3180 read-only", scope="ops")
        ).api_key
    )


def _agent_key(agent: str, owner: str) -> dict:
    return _bearer(db.create_agent_mcp_api_key(agent, owner).api_key)


def _user_id(username: str) -> str:
    return str(db.get_user_by_username(username)["id"])


# --------------------------------------------------------------------------- #
# Reading the real audit_log back
# --------------------------------------------------------------------------- #


def _rows(action: str) -> list[dict]:
    from db.engine import get_engine

    with get_engine().connect() as conn:
        rows = (
            conn.execute(
                text("SELECT * FROM audit_log WHERE event_action = :a ORDER BY id"),
                {"a": action},
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def _assert_person_row(row: dict, username: str, email: str) -> None:
    assert row["actor_type"] == "user"
    assert row["actor_id"] == _user_id(username)
    assert row["actor_email"] == email
    assert row["mcp_key_id"] is None
    assert row["mcp_key_name"] is None
    assert row["mcp_scope"] is None


def _assert_key_row(
    row: dict, username: str, email: str, key_id: str, key_name: str
) -> None:
    assert row["actor_type"] == "user"
    assert row["actor_id"] == _user_id(username)
    assert row["actor_email"] == email
    assert row["mcp_key_id"] == key_id
    assert row["mcp_key_name"] == key_name
    assert row["mcp_scope"] == "user"


# =========================================================================== #
# Circuit-breaker reset
# =========================================================================== #


class TestCircuitBreakerReset:
    def test_a_session_reset_writes_one_row_naming_the_person(self, world):
        res = world.client.post(_reset_path(), headers=_session(ADMIN))

        assert res.status_code == 200, res.text
        assert _breaker_states(world.redis, AGENT) == (None, None)
        rows = _rows(RESET)
        assert len(rows) == 1
        row = rows[0]
        _assert_person_row(row, ADMIN, ADMIN_EMAIL)
        assert row["event_type"] == "configuration"
        assert row["source"] == "api"
        assert (row["target_type"], row["target_id"]) == ("agent", AGENT)
        assert json.loads(row["details"]) == {
            "prior_transport_state": "open",
            "prior_dispatch_state": "open",
        }

    def test_a_user_key_reset_writes_one_row_naming_the_key(self, world):
        headers, key_id = _user_key(ADMIN, ADMIN_KEY_NAME)

        res = world.client.post(_reset_path(), headers=headers)

        assert res.status_code == 200, res.text
        rows = _rows(RESET)
        assert len(rows) == 1
        _assert_key_row(rows[0], ADMIN, ADMIN_EMAIL, key_id, ADMIN_KEY_NAME)
        assert (rows[0]["target_type"], rows[0]["target_id"]) == ("agent", AGENT)

    @pytest.mark.parametrize(
        "credential, agent, status",
        [
            pytest.param(
                lambda: _session(OWNER), AGENT, 403, id="operator-owner-session"
            ),
            pytest.param(
                lambda: _user_key(OWNER, OWNER_KEY_NAME)[0],
                AGENT,
                403,
                id="operator-owner-user-key",
            ),
            pytest.param(_ops_key, AGENT, 403, id="admin-owned-ops-key"),
            pytest.param(
                lambda: _agent_key(ADMIN_AGENT, ADMIN),
                ADMIN_AGENT,
                403,
                id="admin-owned-agent-key-on-itself",
            ),
            pytest.param(dict, AGENT, 401, id="no-credential"),
        ],
    )
    def test_a_refused_reset_writes_no_row(self, world, credential, agent, status):
        res = world.client.post(_reset_path(agent), headers=credential())

        assert res.status_code == status, res.text
        assert _breaker_states(world.redis, agent) == ("open", "open")
        assert _rows(RESET) == []

    def test_a_failed_reset_writes_no_row(self, world, monkeypatch):
        breaker = importlib.import_module("services.dispatch_breaker")

        def _boom(agent_name):
            raise RuntimeError("dispatch reset failed")

        monkeypatch.setattr(breaker, "reset_dispatch", _boom)

        res = world.client.post(_reset_path(), headers=_session(ADMIN))

        assert res.status_code == 500
        assert world.redis.hget(f"agent:dispatch:{AGENT}", "state") == "open"
        assert _rows(RESET) == []

    def test_with_the_breaker_feature_off_the_reset_still_writes_its_row(
        self, world, monkeypatch
    ):
        """Other shape: the master switch off and the per-agent breaker
        disabled. The transport breaker trips regardless of both, and resetting
        it is exactly the operator action to attribute."""
        monkeypatch.setattr(config, "DISPATCH_BREAKER_ENABLED", False)
        db.set_circuit_breaker_enabled(AGENT, False)
        world.redis.delete(f"agent:dispatch:{AGENT}")
        assert config.DISPATCH_BREAKER_ENABLED is False
        assert db.get_circuit_breaker_enabled(AGENT) is False
        assert _breaker_states(world.redis, AGENT) == ("open", None)

        res = world.client.post(_reset_path(), headers=_session(ADMIN))

        assert res.status_code == 200, res.text
        assert res.json()["prior_transport_state"] == "open"
        assert _breaker_states(world.redis, AGENT) == (None, None)
        rows = _rows(RESET)
        assert len(rows) == 1
        _assert_person_row(rows[0], ADMIN, ADMIN_EMAIL)
        assert json.loads(rows[0]["details"]) == {
            "prior_transport_state": "open",
            "prior_dispatch_state": "closed",
        }


# =========================================================================== #
# Execution terminate
# =========================================================================== #


def _seed(execution_id: str, status: str, agent: str = AGENT) -> str:
    return seed_execution(
        "__manual__",
        agent,
        exec_id=execution_id,
        status=status,
        started_at=STARTED_AT,
        duration_ms=None,
        cost=None,
        triggered_by="manual",
    )


def _status_of(execution_id: str) -> str:
    return db.get_execution(execution_id).status


def _owner_session() -> tuple[dict, None]:
    return _session(OWNER), None


def _owner_user_key() -> tuple[dict, str]:
    return _user_key(OWNER, OWNER_KEY_NAME)


CALLERS = [
    pytest.param(_owner_session, id="owner-session"),
    pytest.param(_owner_user_key, id="owner-user-key"),
]


def _assert_terminate_row(row: dict, key_id, execution_id: str, outcome: str) -> None:
    if key_id is None:
        _assert_person_row(row, OWNER, OWNER_EMAIL)
    else:
        _assert_key_row(row, OWNER, OWNER_EMAIL, key_id, OWNER_KEY_NAME)
    assert row["event_type"] == "execution"
    assert row["source"] == "api"
    assert (row["target_type"], row["target_id"]) == ("agent", AGENT)
    assert json.loads(row["details"]) == {
        "execution_id": execution_id,
        "task_execution_id": execution_id,
        "status": outcome,
    }


class TestExecutionTerminate:
    @pytest.mark.parametrize("caller", CALLERS)
    def test_a_queued_cancel_writes_one_row(self, world, caller):
        eid = _seed("s2-3180-queued", "queued")
        headers, key_id = caller()

        res = world.client.post(_terminate_path(eid), headers=headers)

        assert res.status_code == 200, res.text
        assert res.json()["status"] == "cancelled_while_queued"
        assert _status_of(eid) == "cancelled"
        assert world.agent.calls == []  # the queued arm never asks the agent
        rows = _rows(TERMINATE)
        assert len(rows) == 1
        _assert_terminate_row(rows[0], key_id, eid, "cancelled_while_queued")

    @pytest.mark.parametrize("caller", CALLERS)
    def test_a_container_terminate_writes_one_row(self, world, caller):
        eid = _seed("s2-3180-running", "running")
        headers, key_id = caller()

        res = world.client.post(_terminate_path(eid), headers=headers)

        assert res.status_code == 200, res.text
        assert res.json()["status"] == "terminated"
        assert world.agent.calls == [
            (AGENT, f"http://agent-{AGENT}:8000/api/executions/{eid}/terminate")
        ]
        assert world.capacity.released == [(AGENT, eid)]
        assert _status_of(eid) == "cancelled"
        rows = _rows(TERMINATE)
        assert len(rows) == 1
        _assert_terminate_row(rows[0], key_id, eid, "terminated")

    def test_a_parked_cancel_writes_one_row(self, world):
        """The third arm the service can take (#2433): the turn is parked in
        the backend agent-call queue of another worker. Never reaches the agent."""
        eid = _seed("s2-3180-parked", "running")
        limiter = chat_router.chat_execution_service.agent_call_limiter
        world.redis.set(
            limiter._marker_key(eid), json.dumps({"agent": AGENT, "phase": "parked"})
        )

        res = world.client.post(_terminate_path(eid), headers=_session(OWNER))

        assert res.status_code == 200, res.text
        assert res.json()["status"] == "cancelled_while_parked"
        assert world.agent.calls == []
        assert _status_of(eid) == "cancelled"
        rows = _rows(TERMINATE)
        assert len(rows) == 1
        _assert_terminate_row(rows[0], None, eid, "cancelled_while_parked")

    @pytest.mark.parametrize(
        "credential, status",
        [
            pytest.param(lambda: _session(OUTSIDER), 404, id="outsider-session"),
            pytest.param(_ops_key, 403, id="admin-owned-ops-key"),
            pytest.param(
                lambda: _agent_key(ADMIN_AGENT, ADMIN),
                404,
                id="admin-owned-sibling-agent-key",
            ),
            pytest.param(dict, 401, id="no-credential"),
        ],
    )
    @pytest.mark.parametrize("seeded", ["queued", "running"])
    def test_a_refused_terminate_writes_no_row(self, world, credential, status, seeded):
        eid = _seed(f"s2-3180-refused-{seeded}", seeded)

        res = world.client.post(_terminate_path(eid), headers=credential())

        assert res.status_code == status, res.text
        assert _status_of(eid) == seeded
        assert world.agent.calls == []
        assert _rows(TERMINATE) == []

    @pytest.mark.parametrize(
        "agent_side, status",
        [
            pytest.param(
                {"status_code": 500, "payload": {"detail": "kill failed"}},
                500,
                id="agent-500",
            ),
            pytest.param(
                {"status_code": 404, "payload": {"detail": "Execution not found"}},
                404,
                id="agent-404",
            ),
            pytest.param(
                {"exc": httpx.ConnectError("refused")}, 502, id="agent-unreachable"
            ),
            pytest.param({"exc": httpx.ReadTimeout("slow")}, 504, id="agent-timeout"),
            pytest.param(
                {"container": SimpleNamespace(status="exited")},
                503,
                id="container-stopped",
            ),
            pytest.param({"container": None}, 404, id="container-missing"),
        ],
    )
    def test_a_failed_terminate_writes_no_row(self, world, agent_side, status):
        eid = _seed("s2-3180-failed", "running")
        if "container" in agent_side:
            world.container["value"] = agent_side["container"]
        world.agent.status_code = agent_side.get("status_code", 200)
        world.agent.payload = agent_side.get("payload", {})
        world.agent.exc = agent_side.get("exc")

        res = world.client.post(_terminate_path(eid), headers=_session(OWNER))

        assert res.status_code == status, res.text
        assert _status_of(eid) == "running"
        assert _rows(TERMINATE) == []

    def test_terminating_an_already_finished_turn_writes_no_row(self, world):
        """The agent answers 200 `already_finished`: nothing was ended, the
        turn's own terminal stands (#679), so there is no write to attribute."""
        eid = _seed("s2-3180-finished", "running")
        world.agent.payload = {"status": "already_finished", "returncode": 0}

        res = world.client.post(_terminate_path(eid), headers=_session(OWNER))

        assert res.status_code == 200, res.text
        assert res.json()["status"] == "already_finished"
        assert len(world.agent.calls) == 1
        assert _status_of(eid) == "running"
        assert _rows(TERMINATE) == []
