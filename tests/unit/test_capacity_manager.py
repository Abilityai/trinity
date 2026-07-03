"""Unit tests for CapacityManager — #428 (CAPACITY-CONSOLIDATE).

Covers the unified facade that replaced ExecutionQueue + SlotService +
BacklogService at the caller surface. SlotService and BacklogService are
exercised as internal collaborators via mocking; their own unit tests
(tests/unit/test_backlog.py and the existing slot fixture in
test_watchdog_unit.py) cover their direct semantics.

Test surfaces:
- acquire(): admit / overflow_policy=reject / queue_in_memory / queue_persistent
- release(): drains in-memory + fires slot-release callback for persistent
- release_if_matches(): TOCTOU-safe release used by watchdog
- get_status(): merges slot state with in-memory queue
- force_release(), reclaim_stale(), cancel_all_overflow()
- in-memory queue depth bound (CapacityFull when at IN_MEMORY_DEPTH)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Bootstrap src/backend on sys.path (mirrors test_backlog.py).
_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
for _shadow in ("utils", "utils.api_client", "utils.assertions", "utils.cleanup"):
    sys.modules.pop(_shadow, None)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis():
    """Minimal Redis fake supporting only what CapacityManager touches:
    LPUSH, RPOP, LLEN, LRANGE, DELETE, EXISTS, ZSCORE."""
    class _FakeRedis:
        def __init__(self):
            self.lists: dict[str, list[str]] = {}
            self.zsets: dict[str, dict[str, float]] = {}

        def lpush(self, key, *values):
            self.lists.setdefault(key, [])
            for v in values:
                self.lists[key].insert(0, v)
            return len(self.lists[key])

        def rpop(self, key):
            if not self.lists.get(key):
                return None
            return self.lists[key].pop()

        def llen(self, key):
            return len(self.lists.get(key, []))

        def lrange(self, key, start, end):
            items = self.lists.get(key, [])
            if end == -1:
                end = len(items)
            else:
                end += 1
            return items[start:end]

        def delete(self, key):
            self.lists.pop(key, None)
            self.zsets.pop(key, None)
            return 1

        def exists(self, key):
            return int(key in self.lists or key in self.zsets)

        def zscore(self, key, member):
            return self.zsets.get(key, {}).get(member)

        def zadd(self, key, mapping):
            self.zsets.setdefault(key, {}).update(mapping)
            return len(mapping)

    return _FakeRedis()


@pytest.fixture
def slot_service():
    """SlotService surface CapacityManager calls into."""
    s = AsyncMock()
    s.slots_prefix = "agent:slots:"
    s.acquire_slot = AsyncMock(return_value=True)
    s.release_slot = AsyncMock()
    s.cleanup_stale_slots = AsyncMock(return_value={})
    s.force_clear_slots = AsyncMock(return_value=0)
    # SlotService.register_on_release accepts a callback — record it so we
    # can assert CapacityManager wired itself up.
    s._registered_callbacks = []
    s.register_on_release = lambda cb: s._registered_callbacks.append(cb)
    # Default slot_state for status tests (overridden per test).
    state = MagicMock()
    state.active_slots = 0
    state.slots = []
    s.get_slot_state = AsyncMock(return_value=state)
    s.get_all_slot_states = AsyncMock(return_value={})
    return s


@pytest.fixture
def backlog_service():
    b = AsyncMock()
    b.enqueue = AsyncMock(return_value=True)
    b.drain_next = AsyncMock(return_value=False)
    b.cancel_all_backlog = AsyncMock(return_value=0)
    return b


@pytest.fixture
def capacity(monkeypatch, fake_redis, slot_service, backlog_service):
    """A fresh CapacityManager wired to mocked collaborators."""
    from services import capacity_manager as cm_module

    # Bypass real Redis init — point the constructor at our fake.
    monkeypatch.setattr(
        cm_module.redis, "from_url", lambda *_a, **_kw: fake_redis
    )
    cm = cm_module.CapacityManager(
        redis_url="redis://test",
        slot_service=slot_service,
        backlog_service=backlog_service,
    )
    return cm


# ---------------------------------------------------------------------------
# acquire()
# ---------------------------------------------------------------------------


class TestAcquireAdmitted:
    """Slot is free → admitted regardless of overflow policy."""

    def test_admitted_with_reject_policy(self, capacity, slot_service):
        result = asyncio.run(capacity.acquire(
            agent_name="alice",
            execution_id="exec-1",
            max_concurrent=1,
            overflow_policy="reject",
        ))
        assert result.state == "admitted"
        assert result.execution_id == "exec-1"
        slot_service.acquire_slot.assert_awaited_once()

    def test_admitted_with_in_memory_policy(self, capacity):
        result = asyncio.run(capacity.acquire(
            agent_name="alice",
            execution_id="exec-1",
            max_concurrent=1,
            overflow_policy="queue_in_memory",
        ))
        assert result.state == "admitted"
        assert result.queue_position is None

    def test_admitted_with_persistent_policy(self, capacity, backlog_service):
        from services.capacity_manager import PersistentTaskPayload
        result = asyncio.run(capacity.acquire(
            agent_name="alice",
            execution_id="exec-1",
            max_concurrent=3,
            overflow_policy="queue_persistent",
            overflow_payload=PersistentTaskPayload(
                request=MagicMock(),
                effective_timeout=900,
                user_id=1, user_email="a@b", subscription_id=None,
                x_source_agent=None, x_mcp_key_id=None, x_mcp_key_name=None,
                triggered_by="user", collaboration_activity_id=None,
            ),
        ))
        assert result.state == "admitted"
        # No backlog write when admitted.
        backlog_service.enqueue.assert_not_awaited()


class TestAcquireCeilingClamp:
    """#506: CapacityManager.acquire clamps max_concurrent to the fleet ceiling.

    Monkeypatches the ceiling getter so the test stays a true unit test (no
    settings DB). The clamp is the single runtime-enforcement point for every
    facade caller (chat ×3, task_execution_service, …).
    """

    def _patch_ceiling(self, monkeypatch, value):
        from services import settings_service as ss
        monkeypatch.setattr(ss, "get_max_parallel_tasks_ceiling", lambda: value)

    def test_acquire_clamps_above_ceiling(self, capacity, slot_service, monkeypatch):
        """max_concurrent=5 with ceiling=2 → slot acquired with max_parallel_tasks=2."""
        self._patch_ceiling(monkeypatch, 2)
        result = asyncio.run(capacity.acquire(
            agent_name="alice",
            execution_id="exec-1",
            max_concurrent=5,
            overflow_policy="reject",
        ))
        assert result.state == "admitted"
        _, kwargs = slot_service.acquire_slot.await_args
        assert kwargs["max_parallel_tasks"] == 2

    def test_acquire_leaves_below_ceiling_untouched(self, capacity, slot_service, monkeypatch):
        """max_concurrent=1 with ceiling=10 → unchanged (min(1, 10) == 1)."""
        self._patch_ceiling(monkeypatch, 10)
        asyncio.run(capacity.acquire(
            agent_name="alice",
            execution_id="exec-1",
            max_concurrent=1,
            overflow_policy="reject",
        ))
        _, kwargs = slot_service.acquire_slot.await_args
        assert kwargs["max_parallel_tasks"] == 1

    def test_get_slot_state_clamps(self, capacity, slot_service, monkeypatch):
        """get_slot_state passes the clamped cap to the underlying slot service."""
        self._patch_ceiling(monkeypatch, 3)
        asyncio.run(capacity.get_slot_state("alice", 9))
        args, _ = slot_service.get_slot_state.await_args
        assert args == ("alice", 3)

    def test_get_all_states_clamps_each(self, capacity, slot_service, monkeypatch):
        """get_all_states clamps every agent's cap before the bulk read."""
        self._patch_ceiling(monkeypatch, 2)
        asyncio.run(capacity.get_all_states({"alice": 8, "bob": 1}))
        args, _ = slot_service.get_all_slot_states.await_args
        assert args[0] == {"alice": 2, "bob": 1}


class TestAcquireOverflow:
    """Slot full → behavior depends on overflow_policy."""

    def test_reject_policy_raises_capacity_full(self, capacity, slot_service):
        from services.capacity_manager import CapacityFull
        slot_service.acquire_slot.return_value = False
        with pytest.raises(CapacityFull) as exc:
            asyncio.run(capacity.acquire(
                agent_name="alice",
                execution_id="exec-1",
                max_concurrent=1,
                overflow_policy="reject",
            ))
        assert exc.value.reason == "rejected"
        assert exc.value.agent_name == "alice"

    def test_queue_in_memory_returns_position(self, capacity, slot_service):
        slot_service.acquire_slot.return_value = False
        result = asyncio.run(capacity.acquire(
            agent_name="alice",
            execution_id="exec-1",
            max_concurrent=1,
            overflow_policy="queue_in_memory",
            message="hi",
        ))
        assert result.state == "queued_in_memory"
        assert result.queue_position == 1
        # Second overflow lands at position 2.
        result2 = asyncio.run(capacity.acquire(
            agent_name="alice",
            execution_id="exec-2",
            max_concurrent=1,
            overflow_policy="queue_in_memory",
            message="hi",
        ))
        assert result2.queue_position == 2

    def test_queue_in_memory_full_raises(self, capacity, slot_service):
        from services.capacity_manager import CapacityFull, IN_MEMORY_DEPTH
        slot_service.acquire_slot.return_value = False
        # Fill the queue to its bound.
        for i in range(IN_MEMORY_DEPTH):
            asyncio.run(capacity.acquire(
                agent_name="alice", execution_id=f"exec-{i}",
                max_concurrent=1, overflow_policy="queue_in_memory",
            ))
        # The next acquire must raise.
        with pytest.raises(CapacityFull) as exc:
            asyncio.run(capacity.acquire(
                agent_name="alice", execution_id="exec-overflow",
                max_concurrent=1, overflow_policy="queue_in_memory",
            ))
        assert exc.value.reason == "in_memory_full"
        assert exc.value.depth == IN_MEMORY_DEPTH

    def test_queue_persistent_writes_to_backlog(
        self, capacity, slot_service, backlog_service
    ):
        from services.capacity_manager import PersistentTaskPayload
        slot_service.acquire_slot.return_value = False
        payload = PersistentTaskPayload(
            request=MagicMock(),
            effective_timeout=900,
            user_id=42, user_email="u@x", subscription_id="sub-1",
            x_source_agent=None, x_mcp_key_id=None, x_mcp_key_name=None,
            triggered_by="user", collaboration_activity_id=None,
        )
        result = asyncio.run(capacity.acquire(
            agent_name="alice", execution_id="exec-1",
            max_concurrent=3, overflow_policy="queue_persistent",
            overflow_payload=payload,
        ))
        assert result.state == "queued_persistent"
        backlog_service.enqueue.assert_awaited_once()
        # Args were forwarded.
        kwargs = backlog_service.enqueue.await_args.kwargs
        assert kwargs["agent_name"] == "alice"
        assert kwargs["execution_id"] == "exec-1"
        assert kwargs["user_id"] == 42

    def test_queue_persistent_full_raises(
        self, capacity, slot_service, backlog_service
    ):
        from services.capacity_manager import CapacityFull, PersistentTaskPayload
        slot_service.acquire_slot.return_value = False
        backlog_service.enqueue.return_value = False  # backlog at depth cap
        payload = PersistentTaskPayload(
            request=MagicMock(), effective_timeout=900,
            user_id=1, user_email=None, subscription_id=None,
            x_source_agent=None, x_mcp_key_id=None, x_mcp_key_name=None,
            triggered_by="user", collaboration_activity_id=None,
        )
        with pytest.raises(CapacityFull) as exc:
            asyncio.run(capacity.acquire(
                agent_name="alice", execution_id="exec-1",
                max_concurrent=3, overflow_policy="queue_persistent",
                overflow_payload=payload,
            ))
        assert exc.value.reason == "persistent_full"

    def test_queue_persistent_requires_payload(self, capacity, slot_service):
        slot_service.acquire_slot.return_value = False
        with pytest.raises(ValueError, match="requires overflow_payload"):
            asyncio.run(capacity.acquire(
                agent_name="alice", execution_id="exec-1",
                max_concurrent=3, overflow_policy="queue_persistent",
            ))


# ---------------------------------------------------------------------------
# release(), release_if_matches()
# ---------------------------------------------------------------------------


class TestRelease:

    def test_release_calls_slot_service_and_pops_in_memory(
        self, capacity, slot_service
    ):
        # Pre-populate the in-memory queue (admit then overflow).
        slot_service.acquire_slot.return_value = False
        asyncio.run(capacity.acquire(
            agent_name="alice", execution_id="exec-q",
            max_concurrent=1, overflow_policy="queue_in_memory",
        ))
        # Release should fire slot release AND pop one from in-mem queue.
        asyncio.run(capacity.release("alice", "exec-1"))
        slot_service.release_slot.assert_awaited_with("alice", "exec-1")
        assert capacity._mem_list("alice") == []

    def test_release_idempotent_with_no_queued(self, capacity, slot_service):
        # No-op queue, just verify it doesn't raise.
        asyncio.run(capacity.release("alice", "exec-1"))
        slot_service.release_slot.assert_awaited_with("alice", "exec-1")


class TestReleaseIfMatches:
    """TOCTOU-safe release used by the watchdog (#378)."""

    def test_returns_false_when_not_holding_slot(
        self, capacity, fake_redis, slot_service
    ):
        # ZSET is empty → no match.
        result = asyncio.run(capacity.release_if_matches("alice", "exec-1"))
        assert result is False
        slot_service.release_slot.assert_not_awaited()

    def test_returns_true_when_holding_slot(
        self, capacity, fake_redis, slot_service
    ):
        # Seed the ZSET so zscore returns a non-None value.
        fake_redis.zadd("agent:slots:alice", {"exec-1": 1.0})
        result = asyncio.run(capacity.release_if_matches("alice", "exec-1"))
        assert result is True
        slot_service.release_slot.assert_awaited_with("alice", "exec-1")


# ---------------------------------------------------------------------------
# Internal slot-release callback wiring
# ---------------------------------------------------------------------------


class TestSlotReleaseCallback:
    """CapacityManager registers itself as a SlotService release callback so
    that BacklogService.drain_next fires automatically when capacity frees."""

    def test_constructor_registers_callback(self, capacity, slot_service):
        # The fixture-created CapacityManager must have wired exactly one cb.
        assert len(slot_service._registered_callbacks) == 1

    def test_callback_invokes_backlog_drain(self, capacity, backlog_service, slot_service):
        cb = slot_service._registered_callbacks[0]
        asyncio.run(cb("alice"))
        backlog_service.drain_next.assert_awaited_once_with("alice")


# ---------------------------------------------------------------------------
# get_status()
# ---------------------------------------------------------------------------


class TestGetStatus:
    """Status endpoint reports running + in-memory queue."""

    def test_idle_status(self, capacity, slot_service):
        status = asyncio.run(capacity.get_status("alice", max_concurrent=1))
        assert status.is_busy is False
        assert status.queue_length == 0
        assert status.current_execution is None

    def test_busy_status_reports_queued_in_memory(
        self, capacity, slot_service
    ):
        slot_service.acquire_slot.return_value = False
        asyncio.run(capacity.acquire(
            agent_name="alice", execution_id="exec-q",
            max_concurrent=1, overflow_policy="queue_in_memory",
            message="hello",
        ))
        # Slot service reports 1 active.
        slot_state = MagicMock()
        slot_state.active_slots = 1
        slot_state.slots = [MagicMock(execution_id="exec-running",
                                      message_preview="busy", started_at="x")]
        slot_service.get_slot_state.return_value = slot_state

        status = asyncio.run(capacity.get_status("alice", max_concurrent=1))
        assert status.is_busy is True
        assert status.queue_length == 1


# ---------------------------------------------------------------------------
# force_release(), reclaim_stale(), cancel_all_overflow()
# ---------------------------------------------------------------------------


class TestForceRelease:

    def test_force_release_clears_slots_and_queue(
        self, capacity, slot_service, fake_redis
    ):
        slot_service.force_clear_slots.return_value = 2
        # Pre-seed an in-memory queue entry.
        fake_redis.lpush("agent:queue:alice", '{"x":1}')
        result = asyncio.run(capacity.force_release("alice"))
        assert result.was_running is True
        assert result.slots_cleared == 2

    def test_force_release_idempotent(self, capacity, slot_service):
        slot_service.force_clear_slots.return_value = 0
        result = asyncio.run(capacity.force_release("alice"))
        assert result.was_running is False
        assert result.slots_cleared == 0


class TestReclaimStale:

    def test_forwards_agent_timeouts(self, capacity, slot_service):
        timeouts = {"alice": 600, "bob": 1800}
        slot_service.cleanup_stale_slots.return_value = {"alice": ["exec-1"]}
        result = asyncio.run(capacity.reclaim_stale(timeouts))
        slot_service.cleanup_stale_slots.assert_awaited_once_with(
            agent_timeouts=timeouts
        )
        assert result == {"alice": ["exec-1"]}


class TestCancelAllOverflow:

    def test_clears_in_memory_and_calls_backlog(
        self, capacity, backlog_service, fake_redis
    ):
        fake_redis.lpush("agent:queue:alice", '{"x":1}')
        backlog_service.cancel_all_backlog.return_value = 4
        cancelled = asyncio.run(
            capacity.cancel_all_overflow("alice", reason="agent_deleted")
        )
        # In-memory cleared.
        assert fake_redis.llen("agent:queue:alice") == 0
        # Persistent count returned.
        assert cancelled == 4
        backlog_service.cancel_all_backlog.assert_awaited_once_with(
            "alice", reason="agent_deleted"
        )
