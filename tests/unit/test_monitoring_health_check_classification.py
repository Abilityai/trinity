"""Unit tests for monitoring_service.check_network_health exception
classification (#474).

The `/health` probe in monitoring_service must distinguish three categories:

  1. Real liveness signals (TimeoutException, ConnectError, httpx.{Read,Write,
     RemoteProtocol}Error) → record_failure(). These mean the agent is actually
     in a bad state — partial writes, refused connections, wedged event loop.

  2. Client-side transport drops (BrokenPipeError, ConnectionResetError) →
     NOT record_failure(). The agent's health was never observed; the socket
     died on our side, likely from upstream MCP-sync cancellation.

  3. Unknown errors → record_failure() (conservative default).
"""

import importlib
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest


_REPO = Path(__file__).resolve().parent.parent.parent
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# Load monitoring_service directly to bypass services/__init__.py side effects.
_spec = importlib.util.spec_from_file_location(
    "monitoring_service_under_test",
    str(_BACKEND / "services" / "monitoring_service.py"),
)
monitoring_service = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(monitoring_service)


class _FakeAsyncClient:
    """Minimal stand-in for httpx.AsyncClient that raises on .get()."""

    def __init__(self, raise_exc: Exception):
        self._raise_exc = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *_a, **_kw):
        raise self._raise_exc


def _patch_httpx_and_circuit(monkeypatch, raise_exc):
    """Patch httpx.AsyncClient to raise on .get() and inject a MagicMock
    circuit. Returns (fake_circuit) so the test can assert on its calls."""
    monkeypatch.setattr(
        monitoring_service.httpx,
        "AsyncClient",
        lambda *_a, **_kw: _FakeAsyncClient(raise_exc),
    )

    fake_circuit = MagicMock()

    # The lazy import inside perform_health_check pulls CircuitState from
    # services.agent_client at call time. Inject a stub by patching the
    # import target.
    fake_module = MagicMock()
    fake_module.CircuitState = MagicMock(return_value=fake_circuit)
    monkeypatch.setitem(sys.modules, "services.agent_client", fake_module)

    return fake_circuit


@pytest.mark.asyncio
async def test_health_check_broken_pipe_does_not_record_failure(monkeypatch):
    """Client-side BrokenPipeError on /health is NOT a liveness signal —
    must not record_failure on the circuit. The agent could be fine; only
    the socket on our side died."""
    fake_circuit = _patch_httpx_and_circuit(
        monkeypatch, BrokenPipeError(32, "Broken pipe")
    )

    result = await monitoring_service.check_network_health("agent-a")

    assert result.reachable is False
    assert "Connection dropped" in result.error
    assert "BrokenPipeError" in result.error
    fake_circuit.record_failure.assert_not_called()
    fake_circuit.record_success.assert_not_called()


@pytest.mark.asyncio
async def test_health_check_connection_reset_does_not_record_failure(monkeypatch):
    """ConnectionResetError parallels BrokenPipeError — client-side socket
    died, agent health unobserved."""
    fake_circuit = _patch_httpx_and_circuit(
        monkeypatch, ConnectionResetError(104, "Connection reset by peer")
    )

    result = await monitoring_service.check_network_health("agent-b")

    assert result.reachable is False
    assert "Connection dropped" in result.error
    fake_circuit.record_failure.assert_not_called()


@pytest.mark.asyncio
async def test_health_check_httpx_read_error_DOES_record_failure(monkeypatch):
    """httpx.ReadError on /health IS a liveness signal — the agent
    partially wrote a response then died (event-loop wedge, OOM mid-write,
    segfault). Phase 3 Eng finding #3: this must still record_failure or
    we open an evasion path."""
    fake_circuit = _patch_httpx_and_circuit(monkeypatch, httpx.ReadError("read"))

    result = await monitoring_service.check_network_health("agent-c")

    assert result.reachable is False
    assert "HTTP transport error on /health" in result.error
    assert "ReadError" in result.error
    fake_circuit.record_failure.assert_called_once()


@pytest.mark.asyncio
async def test_health_check_httpx_write_error_DOES_record_failure(monkeypatch):
    """Parallel coverage for httpx.WriteError."""
    fake_circuit = _patch_httpx_and_circuit(monkeypatch, httpx.WriteError("write"))

    result = await monitoring_service.check_network_health("agent-d")

    assert result.reachable is False
    fake_circuit.record_failure.assert_called_once()


@pytest.mark.asyncio
async def test_health_check_remote_protocol_error_DOES_record_failure(monkeypatch):
    """Parallel coverage for httpx.RemoteProtocolError."""
    fake_circuit = _patch_httpx_and_circuit(
        monkeypatch,
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
    )

    result = await monitoring_service.check_network_health("agent-e")

    assert result.reachable is False
    fake_circuit.record_failure.assert_called_once()


@pytest.mark.asyncio
async def test_health_check_timeout_still_records_failure(monkeypatch):
    """Regression guard: TimeoutException still records failure (the
    original semantics, unchanged by #474)."""
    fake_circuit = _patch_httpx_and_circuit(
        monkeypatch, httpx.TimeoutException("timed out")
    )

    result = await monitoring_service.check_network_health("agent-f")

    assert result.reachable is False
    assert result.error == "HTTP timeout"
    fake_circuit.record_failure.assert_called_once()


@pytest.mark.asyncio
async def test_health_check_connect_error_still_records_failure(monkeypatch):
    """Regression guard: ConnectError still records failure."""
    fake_circuit = _patch_httpx_and_circuit(
        monkeypatch, httpx.ConnectError("connection refused")
    )

    result = await monitoring_service.check_network_health("agent-g")

    assert result.reachable is False
    assert result.error == "Connection refused"
    fake_circuit.record_failure.assert_called_once()
