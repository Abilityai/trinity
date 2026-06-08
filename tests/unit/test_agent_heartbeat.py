"""
Unit tests for the agent-side heartbeat loop (RELIABILITY-004 / #307).

Mirrors tests/unit/test_agent_server_auto_sync.py — force-loads the real
agent_server package from docker/base-image so the relative imports resolve.

Covers:
  * _parse_vmrss / _read_memory_mb — /proc/self/status VmRSS parsing + failure
  * _build_payload — payload shape (memory_mb, active_executions, uptime_s)
  * _post_heartbeat_once — a non-2xx response is debug-logged, never raised
  * run_heartbeat_loop — swallows a POST error and keeps looping (sleeps first)
  * schedule_heartbeat — no-ops unless TRINITY_BACKEND_URL AND TRINITY_MCP_API_KEY
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.unit

_BASE_IMAGE = Path(__file__).resolve().parent.parent.parent / "docker" / "base-image"
_BASE_IMAGE_STR = str(_BASE_IMAGE)
if _BASE_IMAGE_STR not in sys.path:
    sys.path.insert(0, _BASE_IMAGE_STR)

# Import-time `agent_server` namespace shim — monkeypatch can't reach this
# (it runs before any fixture). Declared via the _STUBBED_MODULE_NAMES +
# _restore_sys_modules escape hatch so tests/lint_sys_modules.py permits the
# bare sys.modules writes below and the shim doesn't leak across test files.
# Precedent: tests/unit/test_telegram_webhook_backfill.py.
_STUBBED_MODULE_NAMES = [
    "agent_server",
    "agent_server.heartbeat",
    "agent_server.state",
    "agent_server.services",
    "agent_server.services.process_registry",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot the agent_server entries we mutate; restore after each test."""
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


# Evict any cached `agent_server` (shadow or real), then register the real
# base-image package as a *namespace* package (no __init__.py exec). This
# resolves `from .state import agent_state` via __path__ without booting the
# FastAPI app (which needs python-multipart etc.). Same shim conftest uses.
for _mod in list(sys.modules):
    if _mod == "agent_server" or _mod.startswith("agent_server."):
        sys.modules.pop(_mod, None)

_stub = types.ModuleType("agent_server")
_stub.__path__ = [str(_BASE_IMAGE / "agent_server")]  # type: ignore[attr-defined]
_stub.__package__ = "agent_server"
sys.modules["agent_server"] = _stub

from agent_server import heartbeat  # noqa: E402


# ---------------------------------------------------------------------------
# VmRSS parsing
# ---------------------------------------------------------------------------
_SAMPLE_STATUS = """\
Name:\tpython3
Umask:\t0022
State:\tS (sleeping)
VmPeak:\t  723416 kB
VmSize:\t  723416 kB
VmRSS:\t   53124 kB
VmData:\t  123456 kB
Threads:\t12
"""


def test_parse_vmrss_reads_kb_as_mb():
    mb = heartbeat._parse_vmrss(_SAMPLE_STATUS)
    assert mb == pytest.approx(53124 / 1024, rel=1e-6)


def test_parse_vmrss_missing_field_returns_none():
    assert heartbeat._parse_vmrss("Name:\tpython3\nThreads:\t1\n") is None


def test_parse_vmrss_garbage_returns_none():
    assert heartbeat._parse_vmrss("VmRSS:\tnot-a-number kB") is None


def test_read_memory_mb_handles_unreadable(monkeypatch):
    def _boom(*_a, **_k):
        raise OSError("nope")
    monkeypatch.setattr(heartbeat.Path, "read_text", _boom, raising=False)
    # Should not raise; returns None on failure.
    assert heartbeat._read_memory_mb() is None


# ---------------------------------------------------------------------------
# _build_payload
# ---------------------------------------------------------------------------
def test_build_payload_shape(monkeypatch):
    monkeypatch.setattr(heartbeat, "_read_memory_mb", lambda: 42.0)
    monkeypatch.setattr(heartbeat, "_count_active_executions", lambda: 3)

    payload = heartbeat._build_payload()

    assert set(payload) == {"memory_mb", "active_executions", "uptime_s"}
    assert payload["memory_mb"] == 42.0
    assert payload["active_executions"] == 3
    assert isinstance(payload["uptime_s"], float)
    assert payload["uptime_s"] >= 0.0


def test_count_active_executions_failure_returns_zero(monkeypatch):
    # If the process registry import/raises, we report 0 rather than crash.
    def _boom():
        raise RuntimeError("registry down")
    monkeypatch.setattr(heartbeat, "_list_running", _boom, raising=False)
    assert heartbeat._count_active_executions() == 0


# ---------------------------------------------------------------------------
# _post_heartbeat_once — surface non-2xx, never raise on it
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient that returns a canned response."""

    _status_code = 200

    def __init__(self, *_a, **_k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, _url, json=None, headers=None):
        return _FakeResp(type(self)._status_code)


def test_post_heartbeat_logs_non_2xx(monkeypatch, caplog):
    """A 403 (e.g. mis-provisioned key) is debug-logged and does NOT raise.

    The loop now owns the client and passes it in, so the test supplies its
    own fake client instance rather than monkeypatching httpx.AsyncClient.
    """
    import logging

    class _Client403(_FakeAsyncClient):
        _status_code = 403

    monkeypatch.setattr(heartbeat, "_build_payload", lambda: {"memory_mb": 1})

    with caplog.at_level(logging.DEBUG, logger=heartbeat.logger.name):
        # Must not raise on a non-2xx status.
        asyncio.run(
            heartbeat._post_heartbeat_once(
                _Client403(), "http://backend:8000", "key", "agent-a"
            )
        )

    assert any("backend returned 403" in r.getMessage() for r in caplog.records)


def test_post_heartbeat_no_log_on_2xx(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(heartbeat, "_build_payload", lambda: {"memory_mb": 1})

    with caplog.at_level(logging.DEBUG, logger=heartbeat.logger.name):
        asyncio.run(
            heartbeat._post_heartbeat_once(
                _FakeAsyncClient(), "http://backend:8000", "key", "agent-a"
            )
        )

    assert not any("backend returned" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# run_heartbeat_loop — swallows errors, sleeps first
# ---------------------------------------------------------------------------
def test_loop_swallows_post_error_and_keeps_looping(monkeypatch):
    monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend:8000")
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "trinity_mcp_test")

    post_calls = {"n": 0}

    async def _failing_post(*_a, **_k):
        post_calls["n"] += 1
        raise httpx.ConnectError("backend unreachable")

    monkeypatch.setattr(heartbeat, "_post_heartbeat_once", _failing_post)

    # Break the infinite loop on the 2nd sleep so we observe one full iteration.
    sleep_calls = {"n": 0}

    async def _fake_sleep(_seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(heartbeat.asyncio, "sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(heartbeat.run_heartbeat_loop(interval=5))

    # Sleeps-first (sleep #1), then one post attempt (error swallowed), then
    # sleep #2 raises CancelledError to exit. The httpx error never propagated.
    assert post_calls["n"] == 1
    assert sleep_calls["n"] == 2


# ---------------------------------------------------------------------------
# schedule_heartbeat — env gating
# ---------------------------------------------------------------------------
class _FakeApp:
    def __init__(self):
        self.startup = []
        self.shutdown = []

    def on_event(self, name):
        def deco(fn):
            (self.startup if name == "startup" else self.shutdown).append(fn)
            return fn
        return deco


def test_schedule_noop_without_backend_url(monkeypatch):
    monkeypatch.delenv("TRINITY_BACKEND_URL", raising=False)
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "trinity_mcp_test")
    app = _FakeApp()
    heartbeat.schedule_heartbeat(app)
    assert app.startup == []  # no handler registered


def test_schedule_noop_without_mcp_key(monkeypatch):
    monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend:8000")
    monkeypatch.delenv("TRINITY_MCP_API_KEY", raising=False)
    app = _FakeApp()
    heartbeat.schedule_heartbeat(app)
    assert app.startup == []


def test_schedule_registers_when_both_present(monkeypatch):
    monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend:8000")
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "trinity_mcp_test")
    app = _FakeApp()
    heartbeat.schedule_heartbeat(app)
    assert len(app.startup) == 1
    assert len(app.shutdown) == 1
