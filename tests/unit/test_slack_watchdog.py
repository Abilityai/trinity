"""
Unit tests for Slack Socket Mode watchdog (#278).

Tests the watchdog health checks and backoff logic without requiring
a real Slack connection, SDK, or backend dependencies.

Module: src/backend/adapters/transports/slack_socket.py
Issue: https://github.com/abilityai/trinity/issues/278

Updated for #244: per-client watchdog (contexts[0] is the single-client
backward-compat shape these tests exercise).
"""

import asyncio
import sys
import types
import pytest
from unittest.mock import AsyncMock, MagicMock


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Stub out heavy dependencies so we can import slack_socket in isolation
# ---------------------------------------------------------------------------


class _StubChannelTransport:
    def __init__(self, adapter, router):
        self.adapter = adapter
        self.router = router
        self._running = False

    @property
    def is_connected(self):
        return self._running

    async def start(self):
        pass

    async def stop(self):
        pass

    async def on_event(self, raw_event):
        pass


# Insert stubs before importing the module under test.
#
# We only need to stub `adapters.transports.base.ChannelTransport` —
# that is the ONLY symbol slack_socket.py imports from the adapters package.
# Unconditionally replacing `sys.modules["adapters"]` with a plain module
# breaks other tests in the combined suite that later do
# `from adapters.whatsapp_adapter import ...` (the package lookup then sees a
# non-package and raises "adapters is not a package").
#
# Fix: install a stub *only* for `adapters.transports.base`. Leave the top-
# level `adapters` and `adapters.transports` namespace packages alone (they
# may already point to the real backend source via sys.path). If they are
# not yet in sys.modules, install minimal namespace-package shims (with
# __path__ set) so that Python can traverse the package tree correctly.

_BACKEND_ADAPTERS = None
try:
    from pathlib import Path as _Path
    _BACKEND_ADAPTERS = _Path(__file__).resolve().parent.parent.parent / "src" / "backend" / "adapters"
except Exception:
    pass

# Ensure `adapters` is a proper package in sys.modules (not a plain module).
if "adapters" not in sys.modules:
    _adapters_pkg = types.ModuleType("adapters")
    if _BACKEND_ADAPTERS and _BACKEND_ADAPTERS.exists():
        _adapters_pkg.__path__ = [str(_BACKEND_ADAPTERS)]  # type: ignore[attr-defined]
    sys.modules["adapters"] = _adapters_pkg
elif not getattr(sys.modules["adapters"], "__path__", None):
    # Already a plain module (not a package) — upgrade it.
    _adapters_pkg = sys.modules["adapters"]
    if _BACKEND_ADAPTERS and _BACKEND_ADAPTERS.exists():
        _adapters_pkg.__path__ = [str(_BACKEND_ADAPTERS)]  # type: ignore[attr-defined]

# Ensure `adapters.transports` is a proper package.
if "adapters.transports" not in sys.modules:
    _transports_pkg = types.ModuleType("adapters.transports")
    if _BACKEND_ADAPTERS and (_BACKEND_ADAPTERS / "transports").exists():
        _transports_pkg.__path__ = [str(_BACKEND_ADAPTERS / "transports")]  # type: ignore[attr-defined]
    sys.modules["adapters.transports"] = _transports_pkg
elif not getattr(sys.modules["adapters.transports"], "__path__", None):
    _transports_pkg = sys.modules["adapters.transports"]
    if _BACKEND_ADAPTERS and (_BACKEND_ADAPTERS / "transports").exists():
        _transports_pkg.__path__ = [str(_BACKEND_ADAPTERS / "transports")]  # type: ignore[attr-defined]

# Stub only `adapters.transports.base` so SlackSocketTransport can be
# imported without instantiating the abstract ChannelTransport base class.
_base = types.ModuleType("adapters.transports.base")
_base.ChannelTransport = _StubChannelTransport
sys.modules["adapters.transports.base"] = _base

# Import via importlib to load the .py file directly
import importlib.util
import os

# Use __file__-relative path to work regardless of cwd (#341)
_slack_socket_path = os.path.join(
    os.path.dirname(__file__),
    "..", "..",  # from tests/unit to project root
    "src", "backend", "adapters", "transports", "slack_socket.py"
)

_spec = importlib.util.spec_from_file_location(
    "adapters.transports.slack_socket",
    _slack_socket_path,
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["adapters.transports.slack_socket"] = _mod
_spec.loader.exec_module(_mod)

SlackSocketTransport = _mod.SlackSocketTransport
_ClientCtx = _mod._ClientCtx
WATCHDOG_INTERVAL_SECONDS = _mod.WATCHDOG_INTERVAL_SECONDS
WATCHDOG_BACKOFF_INITIAL_SECONDS = _mod.WATCHDOG_BACKOFF_INITIAL_SECONDS
WATCHDOG_BACKOFF_MAX_SECONDS = _mod.WATCHDOG_BACKOFF_MAX_SECONDS
WATCHDOG_PING_TIMEOUT_SECONDS = _mod.WATCHDOG_PING_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeSession:
    """Fake aiohttp WebSocket session."""

    def __init__(self, closed=False):
        self.closed = closed
        self._ping_should_fail = False
        self._ping_should_timeout = False

    async def ping(self):
        if self._ping_should_timeout:
            await asyncio.sleep(100)
        if self._ping_should_fail:
            raise ConnectionResetError("connection reset")

    async def close(self):
        self.closed = True


class FakeMonitor:
    """Fake asyncio Future for the SDK monitor task."""

    def __init__(self, is_done=False):
        self._done = is_done

    def done(self):
        return self._done

    def cancel(self):
        self._done = True


class FakeClient:
    """Fake SocketModeClient with just the attributes the watchdog checks."""

    def __init__(self, session=None, monitor=None):
        self.current_session = session
        self.current_session_monitor = monitor
        self._reconnect_called = False

    async def connect_to_new_endpoint(self):
        self._reconnect_called = True
        self.current_session = FakeSession(closed=False)
        self.current_session_monitor = FakeMonitor(is_done=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def transport():
    """Create a SlackSocketTransport with a single fake client at contexts[0]."""
    adapter = MagicMock()
    router = MagicMock()
    t = SlackSocketTransport(app_token="xapp-test", adapter=adapter, router=router)
    fake_client = FakeClient(
        session=FakeSession(closed=False),
        monitor=FakeMonitor(is_done=False),
    )
    ctx = _ClientCtx(index=0, client=fake_client)
    t.contexts = [ctx]
    t._running = True
    return t


@pytest.fixture
def ctx(transport):
    """Convenience accessor for the single ClientCtx in the fixture."""
    return transport.contexts[0]


# ---------------------------------------------------------------------------
# Health check tests
# ---------------------------------------------------------------------------

class TestHealthCheck:
    """Test _check_health detects the three structural failure modes."""

    def test_healthy(self, transport, ctx):
        assert transport._check_health(ctx) is None

    def test_session_none(self, transport, ctx):
        ctx.client.current_session = None
        assert "dead/closed" in transport._check_health(ctx)

    def test_session_closed(self, transport, ctx):
        ctx.client.current_session.closed = True
        assert "dead/closed" in transport._check_health(ctx)

    def test_monitor_none(self, transport, ctx):
        ctx.client.current_session_monitor = None
        assert "monitor is None" in transport._check_health(ctx)

    def test_monitor_exited(self, transport, ctx):
        ctx.client.current_session_monitor = FakeMonitor(is_done=True)
        assert "monitor task exited" in transport._check_health(ctx)


# ---------------------------------------------------------------------------
# Ping check tests
# ---------------------------------------------------------------------------

class TestPingCheck:
    """Test _ping_check detects connection-level failures."""

    def test_ping_success(self, transport, ctx):
        result = _run(transport._ping_check(ctx))
        assert result is None

    def test_ping_failure(self, transport, ctx):
        ctx.client.current_session._ping_should_fail = True
        result = _run(transport._ping_check(ctx))
        assert "ping failed" in result

    def test_ping_timeout(self, transport, ctx):
        original = _mod.WATCHDOG_PING_TIMEOUT_SECONDS
        _mod.WATCHDOG_PING_TIMEOUT_SECONDS = 0.1
        try:
            ctx.client.current_session._ping_should_timeout = True
            result = _run(transport._ping_check(ctx))
            assert "ping timeout" in result
        finally:
            _mod.WATCHDOG_PING_TIMEOUT_SECONDS = original

    def test_ping_session_gone(self, transport, ctx):
        ctx.client.current_session = None
        result = _run(transport._ping_check(ctx))
        assert "gone" in result


# ---------------------------------------------------------------------------
# is_connected tests (now reflects "any client healthy" semantics)
# ---------------------------------------------------------------------------

class TestIsConnected:
    """Test is_connected reflects actual socket state."""

    def test_connected(self, transport):
        assert transport.is_connected is True

    def test_not_running(self, transport):
        transport._running = False
        assert transport.is_connected is False

    def test_no_clients(self, transport):
        transport.contexts = []
        assert transport.is_connected is False

    def test_session_closed(self, transport, ctx):
        ctx.client.current_session.closed = True
        assert transport.is_connected is False

    def test_session_none(self, transport, ctx):
        ctx.client.current_session = None
        assert transport.is_connected is False


# ---------------------------------------------------------------------------
# Backoff tests
# ---------------------------------------------------------------------------

class TestBackoff:
    """Test exponential backoff with cap (per-client)."""

    def test_no_failures(self, transport, ctx):
        ctx.consecutive_failures = 0
        assert transport._get_backoff_interval(ctx) == WATCHDOG_INTERVAL_SECONDS

    def test_first_failure(self, transport, ctx):
        ctx.consecutive_failures = 1
        assert transport._get_backoff_interval(ctx) == WATCHDOG_BACKOFF_INITIAL_SECONDS

    def test_second_failure(self, transport, ctx):
        ctx.consecutive_failures = 2
        assert transport._get_backoff_interval(ctx) == WATCHDOG_BACKOFF_INITIAL_SECONDS * 2

    def test_third_failure(self, transport, ctx):
        ctx.consecutive_failures = 3
        assert transport._get_backoff_interval(ctx) == WATCHDOG_BACKOFF_INITIAL_SECONDS * 4

    def test_capped(self, transport, ctx):
        ctx.consecutive_failures = 4
        assert transport._get_backoff_interval(ctx) == WATCHDOG_BACKOFF_MAX_SECONDS

    def test_stays_capped(self, transport, ctx):
        ctx.consecutive_failures = 10
        assert transport._get_backoff_interval(ctx) == WATCHDOG_BACKOFF_MAX_SECONDS


# ---------------------------------------------------------------------------
# Reconnect tests
# ---------------------------------------------------------------------------

class TestReconnect:
    """Test _attempt_reconnect behavior."""

    def test_reconnect_success_resets_failures(self, transport, ctx):
        ctx.consecutive_failures = 3
        _run(transport._attempt_reconnect(ctx, "test reason"))
        assert ctx.consecutive_failures == 0
        assert ctx.client._reconnect_called is True

    def test_reconnect_failure_increments_count(self, transport, ctx):
        ctx.client.connect_to_new_endpoint = AsyncMock(
            side_effect=Exception("network error")
        )
        ctx.consecutive_failures = 2
        _run(transport._attempt_reconnect(ctx, "test reason"))
        assert ctx.consecutive_failures == 3


# ---------------------------------------------------------------------------
# Watchdog loop integration tests
# ---------------------------------------------------------------------------

class TestWatchdogLoop:
    """Test the _watchdog loop orchestrates checks correctly."""

    def _run_one_cycle(self, transport, ctx):
        """Run the watchdog for one cycle then stop it."""
        async def _one_cycle():
            cycle_count = 0
            _original_sleep = asyncio.sleep

            async def _fast_sleep(seconds):
                nonlocal cycle_count
                cycle_count += 1
                if cycle_count > 1:
                    transport._running = False
                await _original_sleep(0)

            _mod.asyncio.sleep = _fast_sleep
            try:
                await transport._watchdog(ctx)
            finally:
                _mod.asyncio.sleep = _original_sleep

        _run(_one_cycle())

    def test_healthy_session_no_reconnect(self, transport, ctx):
        """Healthy session + monitor → no reconnect triggered."""
        ctx.client._reconnect_called = False
        self._run_one_cycle(transport, ctx)
        assert ctx.client._reconnect_called is False
        assert ctx.consecutive_failures == 0

    def test_dead_session_triggers_reconnect(self, transport, ctx):
        """Dead session → reconnect without even trying ping."""
        ctx.client.current_session.closed = True
        self._run_one_cycle(transport, ctx)
        assert ctx.client._reconnect_called is True

    def test_dead_monitor_triggers_reconnect(self, transport, ctx):
        """Monitor exited → reconnect."""
        ctx.client.current_session_monitor = FakeMonitor(is_done=True)
        self._run_one_cycle(transport, ctx)
        assert ctx.client._reconnect_called is True

    def test_ping_failure_triggers_reconnect(self, transport, ctx):
        """Session looks OK but ping fails → reconnect."""
        ctx.client.current_session._ping_should_fail = True
        self._run_one_cycle(transport, ctx)
        assert ctx.client._reconnect_called is True

    def test_healthy_resets_consecutive_failures(self, transport, ctx):
        """After recovery, consecutive failures reset to 0."""
        ctx.consecutive_failures = 5
        self._run_one_cycle(transport, ctx)
        assert ctx.consecutive_failures == 0

    def test_survives_unexpected_exception(self, transport, ctx):
        """Watchdog must not die on unexpected errors — keeps backend alive."""
        async def _error_cycle():
            cycle_count = 0
            _original_sleep = asyncio.sleep

            async def _fast_sleep(seconds):
                nonlocal cycle_count
                cycle_count += 1
                if cycle_count > 2:
                    transport._running = False
                await _original_sleep(0)

            _mod.asyncio.sleep = _fast_sleep

            # Make _check_health throw something unexpected on first call
            call_count = 0
            original_check = transport._check_health

            def _exploding_check(ctx_arg):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise RuntimeError("unexpected SDK explosion")
                return original_check(ctx_arg)

            transport._check_health = _exploding_check

            try:
                await transport._watchdog(ctx)
            finally:
                _mod.asyncio.sleep = _original_sleep

            # Watchdog survived the explosion and ran a second cycle
            assert call_count >= 2

        _run(_error_cycle())
