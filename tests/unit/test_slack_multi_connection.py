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

    def test_start_aborts_when_all_clients_fail(self, monkeypatch):
        """0/N clients succeed → _running stays False, no watchdogs spawned."""
        monkeypatch.setenv("SLACK_SOCKET_CONNECTION_COUNT", "2")

        adapter = MagicMock()
        router = MagicMock()
        t = SlackSocketTransport(app_token="xapp-test", adapter=adapter, router=router)

        async def fake_start_one(index):
            return None

        t._start_one_client = fake_start_one  # type: ignore

        async def _go():
            await t.start()

        _run(_go())

        assert t._running is False
        assert t.contexts == []
        assert t.is_connected is False
        assert t.connected_count == 0

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
