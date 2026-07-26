"""Integration tests for the #1560 per-agent Redis lifecycle sweep.

The unit suite (``tests/unit/test_1560_breaker_cleared_on_lifecycle.py``) stubs
the four clearing primitives and asserts the wiring. What it *cannot* prove is
the thing acceptance criterion #6 actually asks for: that after the sweep a real
execution **reaches the agent** instead of being fast-failed by a predecessor's
verdict. That verdict lives in Redis behind atomic Lua (``allow_request``), and
fakeredis has no ``EVALSHA`` — so the only faithful test runs against a real
Redis, mirroring ``tests/integration/test_circuit_breaker.py``.

Two layers, with different prerequisites:

``TestSweepAgainstRealRedis``
    Needs Redis only. Drives the real transport-breaker Lua into ``dormant``,
    asserts ``allow_request()`` denies (this is the exact gate
    ``task_execution_service`` reads: ``transport_open = not circuit.allow_request()``),
    runs the sweep, and asserts the circuit now allows. Also covers the
    probe-lock, the dispatch breaker, and the slot ZSET + metadata keys.

``TestRecreateClearsInheritedVerdict``
    The full end-to-end AC #6 path against a live stack: plant a ``dormant``
    verdict on a real agent, confirm ``POST /task`` fast-fails, force a container
    **recreate** through ``start_agent_internal`` (resource drift), then confirm
    the same task now reaches the agent. **Opt-in** via
    ``TRINITY_LIFECYCLE_TEST_AGENT`` because it recreates a real container; it
    restores the agent's original cpu/memory labels in a ``finally``.

Deliberately does NOT use the session ``api_client`` fixture: it deletes every
agent whose name starts with ``test-`` on the target instance (#1558). The
``cleanup_after_test`` override below keeps the parent conftest from pulling it
in — same reason ``test_circuit_breaker.py`` overrides it.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _env_value(key: str) -> str | None:
    env_path = _REPO / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return None


# #1775: config.py is pointed at the resolved Redis target by
# tests/integration/conftest.py, which runs before this module is imported.
# No import-time os.environ mutation lives here any more.


def _load(name: str, rel: str):
    """Load a backend module standalone, bypassing services/__init__.py (Docker SDK)."""
    spec = importlib.util.spec_from_file_location(name, str(_BACKEND / rel))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


agent_client = _load("agent_client_1560", "services/agent_client.py")
dispatch_breaker = _load("dispatch_breaker_1560", "services/dispatch_breaker.py")

_CIRCUIT = agent_client._CIRCUIT_HASH_PREFIX
_PROBE = agent_client._CIRCUIT_PROBE_LOCK_SUFFIX


# `clear_agent_breakers` resolves its dependencies through call-time lazy imports,
# so exercising the real code against real Redis means binding these names in
# `sys.modules` — monkeypatch cannot reach an import that happens inside the
# function under test. Without the restore fixture below, replacing the `services`
# package would leak a bare stub into every later test in the session: exactly the
# cross-file pollution class of #762. This is the sanctioned escape hatch
# (precedent: tests/unit/test_telegram_webhook_backfill.py).
_STUBBED_MODULE_NAMES = [
    "agent_client_1560",
    "dispatch_breaker_1560",
    "agent_runtime_state_1560",
    "heartbeat_service_1560",
    "services",
    "services.heartbeat_service",
    "services.agent_client",
    "services.dispatch_breaker",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture(autouse=True)
def cleanup_after_test():
    """Override the parent conftest's autouse fixture.

    It requests `api_client`, whose session setup deletes every agent named
    `test-*` on the target instance (#1558). These tests need Redis, not that.
    """
    yield


# `redis_client` comes from tests/integration/conftest.py (#1775) — including
# the credential masking its unreachable-Redis message needs.


@pytest.fixture
def agent_name(redis_client):
    name = f"itest-1560-{uuid.uuid4().hex[:8]}"
    yield name
    redis_client.delete(
        f"{_CIRCUIT}{name}",
        f"{_CIRCUIT}{name}{_PROBE}",
        f"agent:dispatch:{name}",
        f"agent:dispatch:{name}:probe-lock",
        f"agent:slots:{name}",
        f"agent:heartbeat:{name}",
        f"agent:heartbeat:seen:{name}",
        f"agent:heartbeat:misses:{name}",
    )
    agent_client._reset_circuit_redis_client()


def _clear_agent_breakers(name: str) -> None:
    """Invoke the production sweep with the *real* modules bound to real Redis.

    `agent_runtime_state` resolves its dependencies through call-time lazy
    imports, so binding the standalone-loaded modules under the names it imports
    is what makes it exercise the real Lua rather than a stub.
    """
    ars = _load("agent_runtime_state_1560", "services/agent_runtime_state.py")

    heartbeat = _load("heartbeat_service_1560", "services/heartbeat_service.py")
    pkg = type(sys)("services")
    pkg.heartbeat_service = heartbeat
    sys.modules["services"] = pkg
    sys.modules["services.heartbeat_service"] = heartbeat
    sys.modules["services.agent_client"] = agent_client
    sys.modules["services.dispatch_breaker"] = dispatch_breaker
    ars.clear_agent_breakers(name)


# ── Redis-level: the sweep against the real Lua ──────────────────────────────


class TestSweepAgainstRealRedis:

    def test_dormant_circuit_denies_then_allows_after_the_sweep(self, agent_name, redis_client):
        """AC #6, at the gate `task_execution_service` actually reads."""
        agent_client.force_circuit_dormant(agent_name, reason="itest")

        # Precondition: this is the state that fast-fails an execution.
        assert redis_client.hget(f"{_CIRCUIT}{agent_name}", "state") == "dormant"
        assert agent_client.CircuitState(agent_name).allow_request() is False

        _clear_agent_breakers(agent_name)

        assert redis_client.exists(f"{_CIRCUIT}{agent_name}") == 0
        # The next execution reaches the agent instead of being fast-failed.
        assert agent_client.CircuitState(agent_name).allow_request() is True

    def test_sweep_clears_the_probe_lock_too(self, agent_name, redis_client):
        agent_client.force_circuit_dormant(agent_name, reason="itest")
        redis_client.set(f"{_CIRCUIT}{agent_name}{_PROBE}", "1", ex=30)
        assert redis_client.exists(f"{_CIRCUIT}{agent_name}{_PROBE}") == 1

        _clear_agent_breakers(agent_name)

        assert redis_client.exists(f"{_CIRCUIT}{agent_name}{_PROBE}") == 0

    def test_sweep_clears_the_dispatch_breaker(self, agent_name, redis_client):
        redis_client.hset(f"agent:dispatch:{agent_name}", mapping={"state": "open", "failures": "3"})

        _clear_agent_breakers(agent_name)

        assert redis_client.exists(f"agent:dispatch:{agent_name}") == 0

    def test_circuit_key_has_no_ttl_so_nothing_expires_it(self, agent_name, redis_client):
        """The premise of #1560: without an explicit clear the verdict is immortal."""
        agent_client.force_circuit_dormant(agent_name, reason="itest")

        assert redis_client.ttl(f"{_CIRCUIT}{agent_name}") == -1


# ── End-to-end: recreate must not inherit the verdict (AC #6 verbatim) ───────


_LIVE_AGENT = os.environ.get("TRINITY_LIFECYCLE_TEST_AGENT")


@pytest.mark.skipif(
    not _LIVE_AGENT,
    reason="set TRINITY_LIFECYCLE_TEST_AGENT=<running agent> to run the destructive recreate leg",
)
class TestRecreateClearsInheritedVerdict:
    """Drives the real `start_agent_internal` recreate path over HTTP.

    Recreates a real container, so it is opt-in and restores the agent's original
    resource labels afterwards.
    """

    @pytest.fixture(scope="class")
    def api(self):
        import httpx

        base = os.environ.get("TRINITY_API_URL", "http://localhost:8000")
        password = os.environ.get("TRINITY_ADMIN_PASSWORD") or _env_value("ADMIN_PASSWORD")
        if not password:
            pytest.skip("ADMIN_PASSWORD unavailable")
        try:
            r = httpx.post(f"{base}/api/token", data={"username": "admin", "password": password}, timeout=10)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Trinity API unreachable at {base}: {e}")
        token = r.json()["access_token"]
        return httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=60)

    def test_recreated_container_does_not_inherit_a_dormant_breaker(self, api, redis_client):
        name = _LIVE_AGENT
        key = f"{_CIRCUIT}{name}"

        original_cpu = api.get(f"/api/agents/{name}/resources").json()["current_cpu"]
        drift_cpu = "2" if original_cpu != "2" else "4"

        try:
            # A predecessor's dormant verdict, still in its deny window.
            now = time.time()
            redis_client.hset(key, mapping={
                "state": "dormant", "failures": "175", "probe_count_since_open": "173",
                "last_failure_ts": str(now), "next_probe_at": str(now + 3600),
            })

            # Precondition: a healthy agent is fast-failed, never contacted.
            denied = api.post(f"/api/agents/{name}/task", json={"message": "ping"})
            assert denied.status_code >= 400
            assert "circuit breaker open" in denied.text.lower()

            # Force the recreate through start_agent_internal (config drift).
            api.put(f"/api/agents/{name}/resources", json={"cpu": drift_cpu}).raise_for_status()
            api.post(f"/api/agents/{name}/start").raise_for_status()
            time.sleep(6)

            # The inherited verdict is gone — allow_request no longer denies.
            assert redis_client.hget(key, "state") != "dormant"
            assert agent_client.CircuitState(name).allow_request() is True

            # And the execution actually reaches the agent.
            ok = api.post(f"/api/agents/{name}/task", json={"message": "ping"})
            assert ok.status_code == 200, ok.text
        finally:
            api.put(f"/api/agents/{name}/resources", json={"cpu": original_cpu})
            api.post(f"/api/agents/{name}/start")
            time.sleep(6)
            api.put(f"/api/agents/{name}/resources", json={"cpu": None, "memory": None})
