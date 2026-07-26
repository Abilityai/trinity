"""Integration test for #1557 — pausing autonomy must not block inbound chat.

The unit suite (``tests/unit/test_1557_autonomy_breaker_decoupled.py``) proves
the source no longer writes the breaker and that the fast-fail message is
honest. What it cannot prove is the behaviour the bug is about: that after
disabling autonomy on a *real* healthy agent, its transport circuit is **not**
parked ``dormant`` and an inbound ``POST /task`` still reaches the agent instead
of fast-failing "circuit breaker open — agent is unhealthy". That verdict lives
in Redis behind atomic Lua, and toggling autonomy runs through the real API
against a running container — so this leg is **opt-in** on a live agent, exactly
like ``tests/integration/test_1560_breaker_lifecycle.py``'s recreate leg.

Set ``TRINITY_AUTONOMY_TEST_AGENT`` (or reuse ``TRINITY_LIFECYCLE_TEST_AGENT``)
to a running agent's name. The test restores the agent's autonomy to ON in a
``finally``.

Deliberately does NOT use the session ``api_client`` fixture: it deletes every
agent whose name starts with ``test-`` on the target instance (#1558). The
``cleanup_after_test`` override keeps the parent conftest from pulling it in.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
import redis as _redis

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


# Point config.py at the local stack BEFORE importing agent_client (it fails fast
# when REDIS_URL lacks credentials). Honor a pre-set REDIS_URL for sibling stacks.
if "REDIS_URL" not in os.environ:
    _PASSWORD = _env_value("REDIS_BACKEND_PASSWORD")
    if not _PASSWORD:
        pytest.skip(
            "REDIS_BACKEND_PASSWORD not in .env — cannot derive Redis credentials",
            allow_module_level=True,
        )
    os.environ["REDIS_URL"] = f"redis://backend:{_PASSWORD}@localhost:6379"
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")


def _load(name: str, rel: str):
    """Load a backend module standalone, bypassing services/__init__.py (Docker SDK).

    Deliberately NOT registered in ``sys.modules``: this test only reads
    ``agent_client``'s constants + ``CircuitState`` and never drives the lazy-import
    sweep, so registering the name would only risk cross-file pollution (#762) and
    trip the ``lint_sys_modules`` gate. (Mirrors the #1560 *unit* loader, not the
    integration one, which registers because it exercises ``clear_agent_breakers``.)
    """
    spec = importlib.util.spec_from_file_location(name, str(_BACKEND / rel))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent_client = _load("agent_client_1557", "services/agent_client.py")
_CIRCUIT = agent_client._CIRCUIT_HASH_PREFIX


@pytest.fixture(autouse=True)
def cleanup_after_test():
    """Override the parent conftest's autouse fixture (it deletes `test-*` agents, #1558)."""
    yield


@pytest.fixture(scope="module")
def redis_client():
    client = _redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        client.ping()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Redis unreachable at {os.environ['REDIS_URL']}: {e}")
    return client


_LIVE_AGENT = os.environ.get("TRINITY_AUTONOMY_TEST_AGENT") or os.environ.get(
    "TRINITY_LIFECYCLE_TEST_AGENT"
)


@pytest.mark.skipif(
    not _LIVE_AGENT,
    reason="set TRINITY_AUTONOMY_TEST_AGENT=<running agent> to run the live autonomy leg",
)
class TestPausingAutonomyKeepsInboundWorking:
    """Drives the real autonomy toggle over HTTP against a running agent."""

    @pytest.fixture(scope="class")
    def api(self):
        import httpx

        base = os.environ.get("TRINITY_API_URL", "http://localhost:8000")
        password = os.environ.get("TRINITY_ADMIN_PASSWORD") or _env_value("ADMIN_PASSWORD")
        if not password:
            pytest.skip("ADMIN_PASSWORD unavailable")
        try:
            r = httpx.post(
                f"{base}/api/token",
                data={"username": "admin", "password": password},
                timeout=10,
            )
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Trinity API unreachable at {base}: {e}")
        token = r.json()["access_token"]
        return httpx.Client(
            base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=120
        )

    def test_disabling_autonomy_leaves_the_circuit_closed_and_inbound_working(
        self, api, redis_client
    ):
        name = _LIVE_AGENT
        key = f"{_CIRCUIT}{name}"
        try:
            # Pause proactive work. Pre-#1557 this forced the transport breaker dormant.
            api.put(f"/api/agents/{name}/autonomy", json={"enabled": False}).raise_for_status()

            assert api.get(f"/api/agents/{name}/autonomy").json()["autonomy_enabled"] is False

            # The breaker must NOT be parked dormant by the pause (the #1557 defect).
            assert redis_client.hget(key, "state") != "dormant", (
                "disabling autonomy forced the transport circuit dormant (#1557)"
            )
            assert agent_client.CircuitState(name).allow_request() is True

            # And an inbound message still reaches the agent — no fast-fail.
            resp = api.post(f"/api/agents/{name}/task", json={"message": "ping"})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert "circuit breaker open" not in (body.get("error") or "").lower()
        finally:
            api.put(f"/api/agents/{name}/autonomy", json={"enabled": True})
