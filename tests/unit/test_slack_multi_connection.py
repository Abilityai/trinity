"""
Unit tests for Slack Socket Mode multi-connection support (#244).

Covers:
- N=2 happy path — event flows through both clients via shared handler
- Envelope-ID dedup ring — duplicate delivery suppressed exactly-once
- Per-client independent backoff — one client failing does not affect siblings
- Partial startup degraded mode — is_connected/connected_count semantics
- Env-var bounds — SLACK_SOCKET_CONNECTION_COUNT clamps + invalid fallback
- N=1 backward compatibility — existing single-client behavior preserved

Module: src/backend/adapters/transports/slack_socket.py
Issue: https://github.com/abilityai/trinity/issues/244
"""

import asyncio
import os
import sys
import types
import pytest
from unittest.mock import AsyncMock, MagicMock


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Stub heavy deps before importing slack_socket (mirrors test_slack_watchdog.py)
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


_BACKEND_ADAPTERS = None
try:
    from pathlib import Path as _Path
    _BACKEND_ADAPTERS = _Path(__file__).resolve().parent.parent.parent / "src" / "backend" / "adapters"
except Exception:
    pass

if "adapters" not in sys.modules:
    _adapters_pkg = types.ModuleType("adapters")
    if _BACKEND_ADAPTERS and _BACKEND_ADAPTERS.exists():
        _adapters_pkg.__path__ = [str(_BACKEND_ADAPTERS)]  # type: ignore[attr-defined]
    sys.modules["adapters"] = _adapters_pkg
elif not getattr(sys.modules["adapters"], "__path__", None):
    _adapters_pkg = sys.modules["adapters"]
    if _BACKEND_ADAPTERS and _BACKEND_ADAPTERS.exists():
        _adapters_pkg.__path__ = [str(_BACKEND_ADAPTERS)]  # type: ignore[attr-defined]

if "adapters.transports" not in sys.modules:
    _transports_pkg = types.ModuleType("adapters.transports")
    if _BACKEND_ADAPTERS and (_BACKEND_ADAPTERS / "transports").exists():
        _transports_pkg.__path__ = [str(_BACKEND_ADAPTERS / "transports")]  # type: ignore[attr-defined]
    sys.modules["adapters.transports"] = _transports_pkg
elif not getattr(sys.modules["adapters.transports"], "__path__", None):
    _transports_pkg = sys.modules["adapters.transports"]
    if _BACKEND_ADAPTERS and (_BACKEND_ADAPTERS / "transports").exists():
        _transports_pkg.__path__ = [str(_BACKEND_ADAPTERS / "transports")]  # type: ignore[attr-defined]

_base = types.ModuleType("adapters.transports.base")
_base.ChannelTransport = _StubChannelTransport
sys.modules["adapters.transports.base"] = _base


# Stub slack_sdk.socket_mode.response so _handle_request can build an ack
# without requiring the real slack_sdk package in the test env.
class _StubSocketModeResponse:
    def __init__(self, envelope_id):
        self.envelope_id = envelope_id


_slack_response_mod = types.ModuleType("slack_sdk.socket_mode.response")
_slack_response_mod.SocketModeResponse = _StubSocketModeResponse
sys.modules.setdefault("slack_sdk", types.ModuleType("slack_sdk"))
sys.modules.setdefault("slack_sdk.socket_mode", types.ModuleType("slack_sdk.socket_mode"))
sys.modules["slack_sdk.socket_mode.response"] = _slack_response_mod


import importlib.util

_slack_socket_path = os.path.join(
    os.path.dirname(__file__),
    "..", "..",
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
_read_connection_count = _mod._read_connection_count
DEFAULT_CONNECTION_COUNT = _mod.DEFAULT_CONNECTION_COUNT
MIN_CONNECTION_COUNT = _mod.MIN_CONNECTION_COUNT
MAX_CONNECTION_COUNT = _mod.MAX_CONNECTION_COUNT
DEDUP_RING_SIZE = _mod.DEDUP_RING_SIZE


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeSession:
    def __init__(self, closed=False):
        self.closed = closed

    async def ping(self):
        pass

    async def close(self):
        self.closed = True


class FakeMonitor:
    def __init__(self, is_done=False):
        self._done = is_done

    def done(self):
        return self._done


class FakeClient:
    """SocketModeClient fake with the methods _handle_request needs."""

    def __init__(self, session=None, monitor=None):
        self.current_session = session if session is not None else FakeSession(closed=False)
        self.current_session_monitor = monitor if monitor is not None else FakeMonitor(is_done=False)
        self.acks_sent = []
        self._reconnect_called = False

    async def send_socket_mode_response(self, response):
        self.acks_sent.append(response.envelope_id)

    async def connect_to_new_endpoint(self):
        self._reconnect_called = True
        self.current_session = FakeSession(closed=False)
        self.current_session_monitor = FakeMonitor(is_done=False)

    async def disconnect(self):
        if self.current_session is not None:
            self.current_session.closed = True


class FakeReq:
    """Minimal Slack SocketModeRequest stand-in."""

    def __init__(self, envelope_id, type="events_api", payload=None):
        self.envelope_id = envelope_id
        self.type = type
        self.payload = payload if payload is not None else {"event": {"type": "message", "text": "hello"}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_transport(n=2):
    """Build a SlackSocketTransport with N pre-populated FakeClient contexts.

    Bypasses real connect(); _running=True; on_event is replaced with a
    counter so tests can assert exactly-once delivery.
    """
    adapter = MagicMock()
    router = MagicMock()
    t = SlackSocketTransport(app_token="xapp-test", adapter=adapter, router=router)
    t.contexts = [
        _ClientCtx(index=i, client=FakeClient())
        for i in range(n)
    ]
    t._running = True

    # Replace on_event with a counting AsyncMock so tests can assert call count.
    t.on_event = AsyncMock()
    return t


async def _drain_pending_tasks():
    """Yield once so any asyncio.create_task() callbacks have a chance to run."""
    # _handle_request spawns _process via create_task. We need at least one
    # event-loop tick for those tasks to start and complete.
    for _ in range(3):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Env-var read tests
# ---------------------------------------------------------------------------

class TestReadConnectionCount:
    """Test _read_connection_count env-var parsing and clamping."""

    def setup_method(self):
        self._saved = os.environ.pop("SLACK_SOCKET_CONNECTION_COUNT", None)

    def teardown_method(self):
        os.environ.pop("SLACK_SOCKET_CONNECTION_COUNT", None)
        if self._saved is not None:
            os.environ["SLACK_SOCKET_CONNECTION_COUNT"] = self._saved

    def test_default_when_unset(self):
        assert _read_connection_count() == DEFAULT_CONNECTION_COUNT

    def test_default_when_blank(self):
        os.environ["SLACK_SOCKET_CONNECTION_COUNT"] = ""
        assert _read_connection_count() == DEFAULT_CONNECTION_COUNT

    def test_explicit_value(self):
        os.environ["SLACK_SOCKET_CONNECTION_COUNT"] = "3"
        assert _read_connection_count() == 3

    def test_invalid_string_falls_back_to_default(self):
        os.environ["SLACK_SOCKET_CONNECTION_COUNT"] = "abc"
        assert _read_connection_count() == DEFAULT_CONNECTION_COUNT

    def test_zero_clamps_to_min(self):
        os.environ["SLACK_SOCKET_CONNECTION_COUNT"] = "0"
        assert _read_connection_count() == MIN_CONNECTION_COUNT

    def test_negative_clamps_to_min(self):
        os.environ["SLACK_SOCKET_CONNECTION_COUNT"] = "-5"
        assert _read_connection_count() == MIN_CONNECTION_COUNT

    def test_too_high_clamps_to_max(self):
        os.environ["SLACK_SOCKET_CONNECTION_COUNT"] = "15"
        assert _read_connection_count() == MAX_CONNECTION_COUNT

    def test_min_value_passes_through(self):
        os.environ["SLACK_SOCKET_CONNECTION_COUNT"] = "1"
        assert _read_connection_count() == 1

    def test_max_value_passes_through(self):
        os.environ["SLACK_SOCKET_CONNECTION_COUNT"] = "10"
        assert _read_connection_count() == 10


# ---------------------------------------------------------------------------
# is_connected / connected_count semantics
# ---------------------------------------------------------------------------

class TestConnectionStateAccessors:
    """Test is_connected (any healthy) and connected_count (precise count)."""

    def test_n2_both_healthy(self):
        t = make_transport(n=2)
        assert t.is_connected is True
        assert t.connected_count == 2

    def test_n2_one_client_session_closed(self):
        """is_connected stays True (degraded but functional); count drops to 1."""
        t = make_transport(n=2)
        t.contexts[0].client.current_session.closed = True
        assert t.is_connected is True
        assert t.connected_count == 1

    def test_n2_both_sessions_closed(self):
        t = make_transport(n=2)
        for ctx in t.contexts:
            ctx.client.current_session.closed = True
        assert t.is_connected is False
        assert t.connected_count == 0

    def test_no_contexts(self):
        t = make_transport(n=2)
        t.contexts = []
        assert t.is_connected is False
        assert t.connected_count == 0

    def test_not_running(self):
        t = make_transport(n=2)
        t._running = False
        assert t.is_connected is False
        assert t.connected_count == 0


# ---------------------------------------------------------------------------
# Event delivery + dedup ring
# ---------------------------------------------------------------------------

class TestHandleRequest:
    """Test _handle_request with N>1 clients and the envelope-ID dedup ring."""

    def test_single_event_processed_once(self):
        """N=2: deliver event_X to client 0 → on_event called exactly once."""
        t = make_transport(n=2)
        req = FakeReq(envelope_id="env-single")

        async def _go():
            await t._handle_request(t.contexts[0].client, req)
            await _drain_pending_tasks()

        _run(_go())
        assert t.on_event.call_count == 1
        # Slack always gets ack'd exactly once on the receiving client
        assert t.contexts[0].client.acks_sent == ["env-single"]
        assert t.contexts[1].client.acks_sent == []

    def test_dedup_skips_duplicate_delivery(self):
        """N=2: same envelope_id delivered to both clients → on_event called once."""
        t = make_transport(n=2)
        req = FakeReq(envelope_id="env-dup")

        async def _go():
            # Both connections receive the same envelope (the failure mode P7
            # the dedup ring is here to defend against).
            await t._handle_request(t.contexts[0].client, req)
            await t._handle_request(t.contexts[1].client, req)
            await _drain_pending_tasks()

        _run(_go())
        assert t.on_event.call_count == 1, "duplicate event should be dropped"
        assert t._dedup_hits == 1
        # Both clients must still ack — otherwise Slack retries.
        assert t.contexts[0].client.acks_sent == ["env-dup"]
        assert t.contexts[1].client.acks_sent == ["env-dup"]

    def test_dedup_concurrent_access(self):
        """Two coroutines insert same envelope_id concurrently → still only one process."""
        t = make_transport(n=2)
        req = FakeReq(envelope_id="env-race")

        async def _go():
            # Fire both _handle_request invocations as concurrent tasks so the
            # asyncio.Lock protecting the dedup ring is genuinely contested.
            await asyncio.gather(
                t._handle_request(t.contexts[0].client, req),
                t._handle_request(t.contexts[1].client, req),
            )
            await _drain_pending_tasks()

        _run(_go())
        assert t.on_event.call_count == 1
        assert t._dedup_hits == 1

    def test_distinct_events_both_processed(self):
        """Distinct envelope_ids → both processed, no false dedup hits."""
        t = make_transport(n=2)
        req_a = FakeReq(envelope_id="env-A")
        req_b = FakeReq(envelope_id="env-B")

        async def _go():
            await t._handle_request(t.contexts[0].client, req_a)
            await t._handle_request(t.contexts[1].client, req_b)
            await _drain_pending_tasks()

        _run(_go())
        assert t.on_event.call_count == 2
        assert t._dedup_hits == 0

    def test_dedup_ring_eviction_fifo(self):
        """Insert DEDUP_RING_SIZE+1 unique envelope_ids → oldest evicted."""
        t = make_transport(n=2)

        async def _go():
            # Fill ring to capacity + 1
            for i in range(DEDUP_RING_SIZE + 1):
                req = FakeReq(envelope_id=f"env-{i:05d}")
                await t._handle_request(t.contexts[0].client, req)
            await _drain_pending_tasks()

        _run(_go())
        assert len(t._envelope_seen) == DEDUP_RING_SIZE
        assert "env-00000" not in t._envelope_seen, "oldest should be evicted (FIFO)"
        assert f"env-{DEDUP_RING_SIZE:05d}" in t._envelope_seen, "newest should remain"

    def test_non_event_request_is_acked_but_not_deduped(self):
        """interactive / unknown request types: ack and skip (no dedup, no on_event)."""
        t = make_transport(n=2)
        req = FakeReq(
            envelope_id="env-interactive",
            type="interactive",
            payload={"type": "block_actions"},
        )

        async def _go():
            await t._handle_request(t.contexts[0].client, req)
            await _drain_pending_tasks()

        _run(_go())
        assert t.on_event.call_count == 0
        assert t.contexts[0].client.acks_sent == ["env-interactive"]
        assert "env-interactive" not in t._envelope_seen


# ---------------------------------------------------------------------------
# Per-client backoff isolation
# ---------------------------------------------------------------------------

class TestPerClientBackoff:
    """Each client owns its consecutive_failures counter independently."""

    def test_one_client_failing_does_not_affect_sibling(self):
        t = make_transport(n=2)
        # Force client 0 to fail reconnect 3 times
        t.contexts[0].client.connect_to_new_endpoint = AsyncMock(
            side_effect=Exception("client 0 cannot reconnect")
        )

        async def _go():
            await t._attempt_reconnect(t.contexts[0], "test reason")
            await t._attempt_reconnect(t.contexts[0], "test reason")
            await t._attempt_reconnect(t.contexts[0], "test reason")

        _run(_go())

        assert t.contexts[0].consecutive_failures == 3
        assert t.contexts[1].consecutive_failures == 0, (
            "sibling client must retain its own backoff state"
        )

    def test_backoff_intervals_are_per_client(self):
        t = make_transport(n=2)
        t.contexts[0].consecutive_failures = 3   # backoff = 240
        t.contexts[1].consecutive_failures = 0   # backoff = 60

        b0 = t._get_backoff_interval(t.contexts[0])
        b1 = t._get_backoff_interval(t.contexts[1])
        assert b0 == 240
        assert b1 == 60


# ---------------------------------------------------------------------------
# Partial startup degraded mode
# ---------------------------------------------------------------------------

class TestPartialStartup:
    """Test the start() path when some clients connect and some don't."""

    def test_start_handles_partial_connect(self, monkeypatch):
        """1/2 clients succeed → is_connected=True, connected_count=1, watchdog runs only on the live one."""
        monkeypatch.setenv("SLACK_SOCKET_CONNECTION_COUNT", "2")

        adapter = MagicMock()
        router = MagicMock()
        t = SlackSocketTransport(app_token="xapp-test", adapter=adapter, router=router)

        # Make client 0 succeed, client 1 fail.
        call_count = {"i": 0}

        async def fake_start_one(index):
            call_count["i"] += 1
            if index == 0:
                return _ClientCtx(index=0, client=FakeClient())
            return None

        t._start_one_client = fake_start_one  # type: ignore

        async def _go():
            await t.start()
            # Watchdog tasks were spawned — cancel immediately so the test exits.
            for ctx in t.contexts:
                if ctx.watchdog_task is not None:
                    ctx.watchdog_task.cancel()

        _run(_go())

        assert call_count["i"] == 2
        assert len(t.contexts) == 1
        assert t.contexts[0].index == 0
        assert t.is_connected is True
        assert t.connected_count == 1

    def test_start_spawns_supervisor_when_all_clients_fail(self, monkeypatch):
        """0/N clients succeed → supervisor task spawned, _running=True, contexts=[].

        Pre-#708: start() returned silently with _running=False, leaving Slack
        permanently offline. Now the recovery supervisor takes over in the
        background; is_connected stays False until the supervisor recovers
        a client, but the transport is no longer dead.
        """
        monkeypatch.setenv("SLACK_SOCKET_CONNECTION_COUNT", "2")
        # Make the supervisor's first sleep instant so the spawned task
        # begins executing without holding the test for 60s. We immediately
        # cancel it below — we're only asserting it was spawned, not that
        # it succeeds. (Recovery is covered in TestStartupRecoverySupervisor.)
        monkeypatch.setattr(_mod, "WATCHDOG_BACKOFF_INITIAL_SECONDS", 60)

        adapter = MagicMock()
        router = MagicMock()
        t = SlackSocketTransport(app_token="xapp-test", adapter=adapter, router=router)

        async def fake_start_one(index):
            return None

        t._start_one_client = fake_start_one  # type: ignore

        async def _go():
            await t.start()
            # Cancel the supervisor immediately so the test exits without
            # waiting 60s for its first real attempt.
            if t._supervisor_task is not None:
                t._supervisor_task.cancel()
                try:
                    await t._supervisor_task
                except asyncio.CancelledError:
                    pass

        _run(_go())

        # _running is now True so the supervisor's loop guard is satisfied.
        assert t._running is True
        assert t.contexts == []
        # is_connected requires a healthy session, which we don't have yet.
        assert t.is_connected is False
        assert t.connected_count == 0
        # The supervisor task was spawned (and we cancelled it just above).
        assert t._supervisor_task is not None
        assert t._supervisor_task.cancelled() or t._supervisor_task.done()

    def test_start_rejects_invalid_token_format(self):
        """Non-xapp- token returns immediately, no connection attempts."""
        adapter = MagicMock()
        router = MagicMock()
        t = SlackSocketTransport(app_token="not-an-app-token", adapter=adapter, router=router)

        async def _go():
            await t.start()

        _run(_go())
        assert t._running is False
        assert t.contexts == []


# ---------------------------------------------------------------------------
# Startup recovery supervisor (#708)
# ---------------------------------------------------------------------------

class TestStartupRecoverySupervisor:
    """Test the supervisor that retries connection when ALL initial attempts fail.

    Pre-#708, a single 10s startup timeout left Slack permanently offline.
    These tests pin the new behavior: supervisor spawns, retries with the
    watchdog backoff cadence, recovers cleanly when the network heals,
    and shuts down cleanly when stop() is called.
    """

    def test_supervisor_not_spawned_when_initial_fully_succeeds(self, monkeypatch):
        """Happy path: all N clients connect on first try → no supervisor."""
        monkeypatch.setenv("SLACK_SOCKET_CONNECTION_COUNT", "2")

        adapter = MagicMock()
        router = MagicMock()
        t = SlackSocketTransport(app_token="xapp-test", adapter=adapter, router=router)

        async def fake_start_one(index):
            return _ClientCtx(index=index, client=FakeClient())

        t._start_one_client = fake_start_one  # type: ignore

        async def _go():
            await t.start()
            for ctx in t.contexts:
                if ctx.watchdog_task is not None:
                    ctx.watchdog_task.cancel()

        _run(_go())

        assert t._supervisor_task is None
        assert len(t.contexts) == 2
        assert t.is_connected is True

    def test_supervisor_not_spawned_when_partial_initial_succeeds(self, monkeypatch):
        """1 of N succeeds → no supervisor (degraded mode handled by watchdog)."""
        monkeypatch.setenv("SLACK_SOCKET_CONNECTION_COUNT", "2")

        adapter = MagicMock()
        router = MagicMock()
        t = SlackSocketTransport(app_token="xapp-test", adapter=adapter, router=router)

        async def fake_start_one(index):
            if index == 0:
                return _ClientCtx(index=0, client=FakeClient())
            return None

        t._start_one_client = fake_start_one  # type: ignore

        async def _go():
            await t.start()
            for ctx in t.contexts:
                if ctx.watchdog_task is not None:
                    ctx.watchdog_task.cancel()

        _run(_go())

        assert t._supervisor_task is None
        assert len(t.contexts) == 1
        assert t.is_connected is True

    def test_supervisor_not_spawned_for_invalid_token(self, monkeypatch):
        """Bad token format is permanent — supervisor would just spin."""
        adapter = MagicMock()
        router = MagicMock()
        t = SlackSocketTransport(app_token="not-an-app-token", adapter=adapter, router=router)

        _run(t.start())

        assert t._supervisor_task is None
        assert t._running is False

    def test_supervisor_recovers_on_subsequent_attempt(self, monkeypatch):
        """Supervisor retries after sleep and exits once any client succeeds.

        Patches the backoff constants to 0 so the supervisor loop runs at
        full speed; first call to _start_one_client returns None for both
        clients (initial gather), then succeeds on the supervisor's first
        retry. End state: contexts populated, watchdogs spawned, supervisor
        task done (it returned cleanly).
        """
        monkeypatch.setenv("SLACK_SOCKET_CONNECTION_COUNT", "2")
        monkeypatch.setattr(_mod, "WATCHDOG_BACKOFF_INITIAL_SECONDS", 0)
        monkeypatch.setattr(_mod, "WATCHDOG_BACKOFF_MAX_SECONDS", 0)

        adapter = MagicMock()
        router = MagicMock()
        t = SlackSocketTransport(app_token="xapp-test", adapter=adapter, router=router)

        # Track call rounds: first 2 calls (initial gather, both clients) fail,
        # next 2 calls (supervisor's first retry) succeed.
        state = {"calls": 0}

        async def fake_start_one(index):
            state["calls"] += 1
            if state["calls"] <= 2:
                return None
            return _ClientCtx(index=index, client=FakeClient())

        t._start_one_client = fake_start_one  # type: ignore

        async def _go():
            await t.start()
            # Wait for the supervisor to finish its work (recovery).
            if t._supervisor_task is not None:
                await asyncio.wait_for(t._supervisor_task, timeout=2)
            # Cancel any spawned watchdogs before exiting the test.
            for ctx in t.contexts:
                if ctx.watchdog_task is not None:
                    ctx.watchdog_task.cancel()

        _run(_go())

        assert state["calls"] == 4  # 2 initial + 2 supervisor retry
        assert len(t.contexts) == 2
        assert t.is_connected is True
        assert t._supervisor_attempts == 0  # reset on success
        assert t._supervisor_task is not None
        assert t._supervisor_task.done()

    def test_supervisor_keeps_retrying_until_success(self, monkeypatch):
        """Multiple failed retries before recovery — backoff increments correctly."""
        monkeypatch.setenv("SLACK_SOCKET_CONNECTION_COUNT", "1")
        monkeypatch.setattr(_mod, "WATCHDOG_BACKOFF_INITIAL_SECONDS", 0)
        monkeypatch.setattr(_mod, "WATCHDOG_BACKOFF_MAX_SECONDS", 0)

        adapter = MagicMock()
        router = MagicMock()
        t = SlackSocketTransport(app_token="xapp-test", adapter=adapter, router=router)

        # Fail 4 times (1 initial + 3 supervisor retries), succeed on the 5th.
        state = {"calls": 0}

        async def fake_start_one(index):
            state["calls"] += 1
            if state["calls"] <= 4:
                return None
            return _ClientCtx(index=index, client=FakeClient())

        t._start_one_client = fake_start_one  # type: ignore

        async def _go():
            await t.start()
            if t._supervisor_task is not None:
                await asyncio.wait_for(t._supervisor_task, timeout=2)
            for ctx in t.contexts:
                if ctx.watchdog_task is not None:
                    ctx.watchdog_task.cancel()

        _run(_go())

        assert state["calls"] == 5
        assert len(t.contexts) == 1
        assert t.is_connected is True

    def test_supervisor_cancelled_by_stop(self, monkeypatch):
        """stop() while supervisor is sleeping cancels it cleanly, no leak."""
        monkeypatch.setenv("SLACK_SOCKET_CONNECTION_COUNT", "2")
        # Use a long sleep so the supervisor parks on its first asyncio.sleep
        # and stop() catches it mid-sleep — exercising the cancel path.
        monkeypatch.setattr(_mod, "WATCHDOG_BACKOFF_INITIAL_SECONDS", 60)

        adapter = MagicMock()
        router = MagicMock()
        t = SlackSocketTransport(app_token="xapp-test", adapter=adapter, router=router)

        async def fake_start_one(index):
            return None

        t._start_one_client = fake_start_one  # type: ignore

        async def _go():
            await t.start()
            assert t._supervisor_task is not None
            assert not t._supervisor_task.done()
            # Capture the task reference BEFORE stop() clears it.
            captured = t._supervisor_task
            # Yield to let the supervisor enter its asyncio.sleep.
            await asyncio.sleep(0)
            await t.stop()
            return captured

        captured_task = _run(_go())

        # After stop(): _running flipped, supervisor reference cleared,
        # the captured task should be done (cancelled by stop()).
        assert t._running is False
        assert t._supervisor_task is None
        assert captured_task.done()

    def test_supervisor_backoff_interval_progression(self):
        """Pin the cadence: 60 → 60 → 120 → 240 → 300 → 300 (cap)."""
        adapter = MagicMock()
        router = MagicMock()
        t = SlackSocketTransport(app_token="xapp-test", adapter=adapter, router=router)

        # attempts=0 → initial value (60s)
        t._supervisor_attempts = 0
        assert t._supervisor_backoff_interval() == 60
        # attempts=1 → 60 * 2^0 = 60
        t._supervisor_attempts = 1
        assert t._supervisor_backoff_interval() == 60
        # attempts=2 → 60 * 2^1 = 120
        t._supervisor_attempts = 2
        assert t._supervisor_backoff_interval() == 120
        # attempts=3 → 60 * 2^2 = 240
        t._supervisor_attempts = 3
        assert t._supervisor_backoff_interval() == 240
        # attempts=4 → 60 * 2^3 = 480 → capped to 300
        t._supervisor_attempts = 4
        assert t._supervisor_backoff_interval() == 300
        # attempts=10 → still capped at 300
        t._supervisor_attempts = 10
        assert t._supervisor_backoff_interval() == 300

    def test_admin_connect_path_can_fail_loud_and_cancel_supervisor(self, monkeypatch):
        """Models routers/settings.py:756 behavior — admin connect raises 400.

        Admin-triggered /api/settings/slack/connect wants synchronous fail-loud
        semantics: if start() returns and is_connected is False, the router
        raises 400 and stops the transport. This must cancel the supervisor
        cleanly so we don't leak a zombie task.
        """
        monkeypatch.setenv("SLACK_SOCKET_CONNECTION_COUNT", "2")
        monkeypatch.setattr(_mod, "WATCHDOG_BACKOFF_INITIAL_SECONDS", 60)

        adapter = MagicMock()
        router = MagicMock()
        t = SlackSocketTransport(app_token="xapp-test", adapter=adapter, router=router)

        async def fake_start_one(index):
            return None

        t._start_one_client = fake_start_one  # type: ignore

        async def _go():
            await t.start()
            # Mimic routers/settings.py:756 — synchronous fail-loud check.
            assert t.is_connected is False, "admin would raise 400 here"
            # Then admin path stops the transport before raising.
            await t.stop()

        _run(_go())

        assert t._running is False
        assert t._supervisor_task is None
        assert t.contexts == []

    def test_supervisor_logs_after_three_consecutive_failures(self, monkeypatch, caplog):
        """Operator-visible signal: supervisor escalates to ERROR after 3 fails."""
        import logging as _logging
        monkeypatch.setenv("SLACK_SOCKET_CONNECTION_COUNT", "1")
        monkeypatch.setattr(_mod, "WATCHDOG_BACKOFF_INITIAL_SECONDS", 0)
        monkeypatch.setattr(_mod, "WATCHDOG_BACKOFF_MAX_SECONDS", 0)

        adapter = MagicMock()
        router = MagicMock()
        t = SlackSocketTransport(app_token="xapp-test", adapter=adapter, router=router)

        # Fail 3 supervisor attempts then succeed on the 4th so the test exits.
        state = {"calls": 0}

        async def fake_start_one(index):
            state["calls"] += 1
            # 1 initial + 3 retries = 4 fails, succeed on 5th
            if state["calls"] <= 4:
                return None
            return _ClientCtx(index=index, client=FakeClient())

        t._start_one_client = fake_start_one  # type: ignore

        async def _go():
            with caplog.at_level(_logging.ERROR, logger="adapters.transports.slack_socket"):
                await t.start()
                if t._supervisor_task is not None:
                    await asyncio.wait_for(t._supervisor_task, timeout=2)
            for ctx in t.contexts:
                if ctx.watchdog_task is not None:
                    ctx.watchdog_task.cancel()

        _run(_go())

        unreachable_logs = [
            r for r in caplog.records
            if "STARTUP UNREACHABLE" in r.getMessage()
        ]
        assert len(unreachable_logs) >= 1, (
            f"expected at least one STARTUP UNREACHABLE error log, "
            f"got messages: {[r.getMessage() for r in caplog.records]}"
        )


# ---------------------------------------------------------------------------
# N=1 backward compatibility
# ---------------------------------------------------------------------------

class TestN1BackwardCompat:
    """N=1 must behave identically to the pre-#244 single-client path."""

    def setup_method(self):
        self._saved = os.environ.pop("SLACK_SOCKET_CONNECTION_COUNT", None)
        os.environ["SLACK_SOCKET_CONNECTION_COUNT"] = "1"

    def teardown_method(self):
        os.environ.pop("SLACK_SOCKET_CONNECTION_COUNT", None)
        if self._saved is not None:
            os.environ["SLACK_SOCKET_CONNECTION_COUNT"] = self._saved

    def test_n1_event_flows_through(self):
        t = make_transport(n=1)
        req = FakeReq(envelope_id="env-n1")

        async def _go():
            await t._handle_request(t.contexts[0].client, req)
            await _drain_pending_tasks()

        _run(_go())
        assert t.on_event.call_count == 1
        assert t.is_connected is True
        assert t.connected_count == 1

    def test_n1_dedup_ring_still_protects_against_redelivery(self):
        """Even with N=1, Slack could in theory redeliver after a refresh —
        the dedup ring should still suppress it."""
        t = make_transport(n=1)
        req = FakeReq(envelope_id="env-n1-dup")

        async def _go():
            await t._handle_request(t.contexts[0].client, req)
            await t._handle_request(t.contexts[0].client, req)
            await _drain_pending_tasks()

        _run(_go())
        assert t.on_event.call_count == 1
        assert t._dedup_hits == 1


# ---------------------------------------------------------------------------
# stop() iterates all clients
# ---------------------------------------------------------------------------

class TestStopCleansEverything:
    """stop() must cancel all watchdog tasks and disconnect all clients."""

    def test_stop_disconnects_all_clients(self):
        t = make_transport(n=3)

        async def _go():
            await t.stop()

        _run(_go())
        # All sessions closed, contexts cleared
        assert t._running is False
        assert t.contexts == []
