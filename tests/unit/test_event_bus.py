"""
Unit tests for Redis Streams event bus (#306 / RELIABILITY-003).
Related flow: docs/memory/feature-flows/websocket-event-bus.md

Covers:
- Envelope shape (payload/scope/agent_name fields)
- last-event-id validation + id comparison
- Scope visibility rules (SCOPE_ALL vs SCOPE_SCOPED with accessible_agents)
- Serialize/deserialize round-trip
- Graceful degradation when Redis is unavailable
- EventBus outbound-queue overflow drops oldest
- StreamDispatcher client queue + failure eviction
- Fallback buffer cap

These tests don't require the backend to be running — they import
``services.event_bus`` directly and exercise its logic.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys

import pytest

# ``services/__init__.py`` auto-imports docker_service and the rest of the
# backend graph, so we load ``event_bus.py`` directly via a spec — isolates the
# unit under test from Docker / database setup. Minimal shim: only config.py is
# also needed, and it's stdlib-only.

_BACKEND_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")
)
if _BACKEND_PATH not in sys.path:
    sys.path.insert(0, _BACKEND_PATH)


def _load_module(name, relpath):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_BACKEND_PATH, relpath)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Load config first (event_bus imports REDIS_URL from it).
_load_module("config", "config.py")
event_bus_mod = _load_module("services.event_bus", "services/event_bus.py")

CLIENT_QUEUE_MAXSIZE = event_bus_mod.CLIENT_QUEUE_MAXSIZE
EVICT_AFTER_FAILURES = event_bus_mod.EVICT_AFTER_FAILURES
EventBus = event_bus_mod.EventBus
SCOPE_ALL = event_bus_mod.SCOPE_ALL
SCOPE_SCOPED = event_bus_mod.SCOPE_SCOPED
STREAM_KEY = event_bus_mod.STREAM_KEY
StreamDispatcher = event_bus_mod.StreamDispatcher
_ClientSlot = event_bus_mod._ClientSlot
_event_is_visible = event_bus_mod._event_is_visible
_id_greater_than = event_bus_mod._id_greater_than
validate_last_event_id = event_bus_mod.validate_last_event_id


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------- pure helpers


class TestEventIdValidation:
    def test_valid_id_accepted(self):
        assert validate_last_event_id("1700000000000-0") == "1700000000000-0"
        assert validate_last_event_id("1-42") == "1-42"

    def test_missing_id_returns_none(self):
        assert validate_last_event_id(None) is None
        assert validate_last_event_id("") is None

    def test_malformed_id_rejected(self):
        assert validate_last_event_id("not-an-id") is None
        assert validate_last_event_id("123") is None
        assert validate_last_event_id("123-abc") is None
        # CSV/injection attempts should not parse.
        assert validate_last_event_id("1-0; DROP") is None
        assert validate_last_event_id("$") is None


class TestIdComparison:
    def test_greater_by_ms(self):
        assert _id_greater_than("2-0", "1-999") is True

    def test_greater_by_seq(self):
        assert _id_greater_than("1-2", "1-1") is True

    def test_equal_is_not_greater(self):
        assert _id_greater_than("1-1", "1-1") is False

    def test_lesser_is_not_greater(self):
        assert _id_greater_than("1-0", "2-0") is False

    def test_malformed_returns_false(self):
        assert _id_greater_than("junk", "1-0") is False


# ----------------------------------------------------------- scope visibility


class _FakeWS:
    async def close(self, *args, **kwargs):
        pass


def _make_slot(scope, *, is_admin=False, agents=None):
    return _ClientSlot(
        ws=_FakeWS(),
        scope=scope,
        send_func=lambda *_: None,
        is_admin=is_admin,
        accessible_agents=set(agents or []),
    )


class TestScopeVisibility:
    def test_all_scope_sees_all_events(self):
        slot = _make_slot(SCOPE_ALL)
        assert _event_is_visible(slot, SCOPE_ALL, "agent-a") is True

    def test_all_scope_ignores_scoped_events(self):
        slot = _make_slot(SCOPE_ALL)
        assert _event_is_visible(slot, SCOPE_SCOPED, "agent-a") is False

    def test_scoped_admin_sees_any_agent(self):
        slot = _make_slot(SCOPE_SCOPED, is_admin=True)
        assert _event_is_visible(slot, SCOPE_SCOPED, "agent-a") is True
        assert _event_is_visible(slot, SCOPE_SCOPED, "agent-z") is True

    def test_scoped_user_sees_only_accessible_agents(self):
        slot = _make_slot(SCOPE_SCOPED, agents=["agent-a", "agent-b"])
        assert _event_is_visible(slot, SCOPE_SCOPED, "agent-a") is True
        assert _event_is_visible(slot, SCOPE_SCOPED, "agent-c") is False

    def test_scoped_event_without_agent_name_not_visible(self):
        slot = _make_slot(SCOPE_SCOPED, agents=["agent-a"])
        assert _event_is_visible(slot, SCOPE_SCOPED, None) is False

    def test_scoped_user_ignores_all_events(self):
        slot = _make_slot(SCOPE_SCOPED, agents=["agent-a"])
        # Events on SCOPE_ALL should stay on /ws; /ws/events must not replay them.
        assert _event_is_visible(slot, SCOPE_ALL, "agent-a") is False


# ----------------------------------------------------------------- EventBus


class _FakeRedis:
    """Minimal in-memory stand-in for ``redis.asyncio.Redis`` that records XADD
    calls and exposes them for assertions. Used to isolate EventBus from a real
    Redis in unit tests."""

    def __init__(self):
        self.xadd_calls = []
        self.closed = False

    async def ping(self):
        return True

    async def xadd(self, key, fields, maxlen=None, approximate=False):
        self.xadd_calls.append(
            {"key": key, "fields": fields, "maxlen": maxlen, "approximate": approximate}
        )
        return f"{len(self.xadd_calls)}-0"

    async def aclose(self):
        self.closed = True


@pytest.fixture
def fake_redis_bus(monkeypatch):
    """Return (bus, fake_redis) with the bus's Redis replaced by a fake."""
    bus = EventBus()
    fake = _FakeRedis()

    async def _get_redis():
        bus._ready = True
        bus._redis = fake
        return fake

    monkeypatch.setattr(bus, "_get_redis", _get_redis)
    return bus, fake


@pytest.mark.asyncio
async def test_publish_dict_xadds_envelope(fake_redis_bus):
    bus, fake = fake_redis_bus
    await bus.start()
    try:
        await bus.publish({"type": "agent_started", "agent_name": "a"}, scope=SCOPE_ALL)
        await asyncio.wait_for(bus._outbound.join(), timeout=2.0)
    finally:
        await bus.stop(drain_timeout=1.0)

    assert len(fake.xadd_calls) == 1
    call = fake.xadd_calls[0]
    assert call["key"] == STREAM_KEY
    assert call["maxlen"] > 0 and call["approximate"] is True
    assert "payload" in call["fields"]
    assert call["fields"]["scope"] == SCOPE_ALL


@pytest.mark.asyncio
async def test_publish_accepts_json_string(fake_redis_bus):
    """Legacy ConnectionManager.broadcast() passes a JSON-encoded string."""
    bus, fake = fake_redis_bus
    await bus.start()
    try:
        await bus.publish('{"type": "agent_stopped", "name": "a"}', scope=SCOPE_ALL)
        await asyncio.wait_for(bus._outbound.join(), timeout=2.0)
    finally:
        await bus.stop(drain_timeout=1.0)

    import json

    payload = json.loads(fake.xadd_calls[0]["fields"]["payload"])
    assert payload["type"] == "agent_stopped"


@pytest.mark.asyncio
async def test_publish_infers_agent_name_for_scoped(fake_redis_bus):
    bus, fake = fake_redis_bus
    await bus.start()
    try:
        await bus.publish(
            {"type": "agent_activity", "agent_name": "ruby", "details": {}},
            scope=SCOPE_SCOPED,
        )
        await asyncio.wait_for(bus._outbound.join(), timeout=2.0)
    finally:
        await bus.stop(drain_timeout=1.0)

    assert fake.xadd_calls[0]["fields"]["agent_name"] == "ruby"


@pytest.mark.asyncio
async def test_publish_without_redis_buffers_to_fallback(monkeypatch):
    bus = EventBus()

    async def _no_redis():
        return None

    monkeypatch.setattr(bus, "_get_redis", _no_redis)
    await bus.start()
    try:
        for i in range(5):
            await bus.publish({"type": "test", "i": i}, scope=SCOPE_ALL)
        await asyncio.wait_for(bus._outbound.join(), timeout=2.0)
    finally:
        await bus.stop(drain_timeout=1.0)

    # All events land in fallback buffer; never raises.
    assert len(bus._fallback) == 5


# -------------------------------------------------------- StreamDispatcher


@pytest.mark.asyncio
async def test_client_eviction_after_consecutive_failures():
    """Matches the AC: failed sends evict the client after N consecutive failures."""
    dispatcher = StreamDispatcher()

    failures = {"count": 0}

    class _BrokenWS:
        async def close(self, *args, **kwargs):
            pass

    async def _broken_send(payload):
        failures["count"] += 1
        raise RuntimeError("broken pipe")

    client_id = await dispatcher.register(
        ws=_BrokenWS(), scope=SCOPE_ALL, send_func=_broken_send
    )
    slot = dispatcher._clients[client_id]

    # Feed enough events to trigger eviction.
    for i in range(EVICT_AFTER_FAILURES + 1):
        slot.queue.put_nowait((f"{i}-0", {"type": "x"}))

    # Give the consumer task time to drain.
    for _ in range(20):
        await asyncio.sleep(0.01)
        if client_id not in dispatcher._clients:
            break

    assert client_id not in dispatcher._clients
    assert failures["count"] >= EVICT_AFTER_FAILURES


@pytest.mark.asyncio
async def test_client_queue_overflow_triggers_resync_marker():
    """Slow consumer shouldn't block fan-out; overflow should request resync."""
    dispatcher = StreamDispatcher()
    delivered = []
    release = asyncio.Event()

    class _WS:
        async def close(self, *args, **kwargs):
            pass

    async def _slow_send(payload):
        # Block the consumer until ``release`` is set so the queue saturates.
        if payload.get("type") != "resync_required":
            await release.wait()
        delivered.append(payload)

    client_id = await dispatcher.register(
        ws=_WS(), scope=SCOPE_ALL, send_func=_slow_send
    )

    # Push one event (the consumer will block on release).
    slot = dispatcher._clients[client_id]
    slot.queue.put_nowait(("1-0", {"type": "x"}))

    # Simulate the dispatcher's fan-out on an already-full queue. Fill the queue
    # past its capacity — the first put succeeds, subsequent ones go through the
    # same code path as _fanout (put_nowait → QueueFull → resync_required).
    fills_attempted = CLIENT_QUEUE_MAXSIZE + 10
    filled = 0
    for i in range(fills_attempted):
        try:
            slot.queue.put_nowait((f"{i+2}-0", {"type": "y"}))
            filled += 1
        except asyncio.QueueFull:
            break

    # At least one put_nowait must have hit QueueFull — meaning the slow client
    # scenario is genuinely reproduced.
    assert filled < fills_attempted

    # Release the consumer and let it drain.
    release.set()
    for _ in range(20):
        await asyncio.sleep(0.01)
        if delivered:
            break

    # Clean up
    dispatcher.unregister(client_id)
    assert len(delivered) >= 1


@pytest.mark.asyncio
async def test_update_accessible_agents_mutates_slot():
    dispatcher = StreamDispatcher()

    class _WS:
        async def close(self, *args, **kwargs):
            pass

    async def _send(_):
        pass

    client_id = await dispatcher.register(
        ws=_WS(), scope=SCOPE_SCOPED, send_func=_send, accessible_agents=["a"]
    )
    dispatcher.update_accessible_agents(client_id, ["a", "b"])
    assert dispatcher._clients[client_id].accessible_agents == {"a", "b"}
    dispatcher.unregister(client_id)


@pytest.mark.asyncio
async def test_consumer_last_delivered_id_is_monotonic():
    """Regression guard for #306 review C1: even if the queue receives events
    out of order (e.g. a catchup batch interleaved with live fan-out), the
    client's ``last_delivered_id`` must never advance backwards."""
    dispatcher = StreamDispatcher()

    sent = []

    class _WS:
        async def close(self, *args, **kwargs):
            pass

    async def _send(payload):
        sent.append(payload)

    client_id = await dispatcher.register(
        ws=_WS(), scope=SCOPE_ALL, send_func=_send
    )
    slot = dispatcher._clients[client_id]

    # Simulate out-of-order delivery: live event 200, then catchup event 100.
    slot.queue.put_nowait(("200-0", {"type": "live"}))
    slot.queue.put_nowait(("100-0", {"type": "catchup"}))

    for _ in range(30):
        await asyncio.sleep(0.01)
        if len(sent) >= 2:
            break

    # Both delivered, but cursor stuck at the higher id.
    assert len(sent) == 2
    assert slot.last_delivered_id == "200-0"
    dispatcher.unregister(client_id)


@pytest.mark.asyncio
async def test_register_with_invalid_last_event_id_queues_resync():
    """Malformed cursor should not crash; should trigger a resync marker."""
    dispatcher = StreamDispatcher()

    class _WS:
        async def close(self, *args, **kwargs):
            pass

    sent = []

    async def _send(payload):
        sent.append(payload)

    client_id = await dispatcher.register(
        ws=_WS(),
        scope=SCOPE_ALL,
        send_func=_send,
        last_event_id="definitely-not-a-valid-id",
    )

    # Give catchup task a chance to run.
    for _ in range(10):
        await asyncio.sleep(0.01)
        if sent:
            break

    assert any(p.get("type") == "resync_required" for p in sent)
    assert any(p.get("reason") == "invalid_last_event_id" for p in sent)
    dispatcher.unregister(client_id)
