"""Pull pilots own their dispatch — #1766 (pilot-scoped slice of #1081 Phase 5).

Before this change the ``PULL_MODE_PILOT_AGENTS`` flag was purely **additive**:
the agent's container started pulling, but the backend kept admitting-and-pushing
whenever a slot was free (``CapacityManager.acquire`` had no pilot branch), and
the backend's own ``backlog_service.drain_next`` kept claiming queued rows
through the SAME ``db.claim_next_queued`` the agent's worker uses. The result was
push and pull coexisting **inside one agent**:

  * a row only ever reached the queue on capacity overflow, so the pull path
    carried almost no traffic on a healthy instance;
  * when a row did queue, backend-drain and agent-worker raced for it — no
    double-run (the claim is one atomic UPDATE), but the winner was whoever
    polled first, and the backend was structurally favoured;
  * two independent capacity counters (Redis ZSET vs the container's worker
    pool, both sized ``max_parallel_tasks``) meant a pilot could run up to **2x**
    its configured concurrency, invisible to canary S-02 (``ZCARD`` only) and to
    S-01 (which excludes leased rows by design).

The three properties proven here make the pilot flag a true **either/or**:

  1. **Producer** — a pilot's autonomous work is never admitted; it goes straight
     to the durable queue, and no slot is ZADDed.
  2. **Consumer** — the backend never drains a pilot's queue, so the agent's
     worker pool is the sole claimant. One guard covers every drain path
     (release callback, 60s orphan sweep, ``drain_on_release``).
  3. **Carve-out** — interactive triggers are excluded and still take the
     synchronous push path (``TARGET_ARCHITECTURE.md`` Open Question 7's scope
     cut), so human chat is not parked behind N batch tasks and per-session
     ``--resume`` serialization is untouched.

Plus the inertness property the whole dark-ship rests on: with an empty
allowlist (the default) every path is byte-for-byte unchanged.

Pure unit test — mocked ``SlotService`` / ``BacklogService`` collaborators,
mirroring ``tests/unit/test_capacity_manager.py``. No Redis, no DB, no agent.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Bootstrap src/backend on sys.path.
#
# Deliberately WITHOUT the `sys.modules.pop("utils", ...)` preamble that
# test_capacity_manager.py carries: tests/unit/conftest.py already installs
# src/backend/utils as the canonical `utils` package via an importlib file
# loader, so evicting it leaves `utils` unbound and pytest's prepend import mode
# rebinds it to the tests/ helper package (the failure mode spelled out in
# test_1081_physical_meter.py). Every backend module here is imported lazily
# inside a test or fixture, so the path insert is all this file needs — and it
# keeps the file clean under tests/lint_sys_modules.py.
_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/unit/test_capacity_manager.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis():
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
            end = len(items) if end == -1 else end + 1
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

        def set(self, *_a, **_kw):
            return True

    return _FakeRedis()


@pytest.fixture
def slot_service():
    s = AsyncMock()
    s.slots_prefix = "agent:slots:"
    s.acquire_slot = AsyncMock(return_value=True)
    s.release_slot = AsyncMock()
    s._registered_callbacks = []
    s.register_on_release = lambda cb: s._registered_callbacks.append(cb)
    return s


@pytest.fixture
def backlog_service():
    b = AsyncMock()
    b.enqueue = AsyncMock(return_value=True)
    b.drain_next = AsyncMock(return_value=False)
    return b


@pytest.fixture
def capacity(monkeypatch, fake_redis, slot_service, backlog_service):
    from services import capacity_manager as cm_module

    monkeypatch.setattr(cm_module.redis, "from_url", lambda *_a, **_kw: fake_redis)
    return cm_module.CapacityManager(
        redis_url="redis://test",
        slot_service=slot_service,
        backlog_service=backlog_service,
    )


def _payload(triggered_by: str):
    from services.capacity_manager import PersistentTaskPayload

    return PersistentTaskPayload(
        request=MagicMock(),
        effective_timeout=900,
        user_id=1,
        user_email="u@x",
        subscription_id=None,
        x_source_agent=None,
        triggered_by=triggered_by,
        collaboration_activity_id=None,
    )


@pytest.fixture
def pilot(monkeypatch):
    """Put `alice` in the pilot allowlist. `bob` stays a normal push agent."""
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "alice")


# ---------------------------------------------------------------------------
# 1. pull_owns_dispatch — the predicate
# ---------------------------------------------------------------------------


class TestPullOwnsDispatch:
    def test_non_pilot_never_owns_dispatch(self, pilot):
        from services.agent_service.pull_mode import pull_owns_dispatch

        assert pull_owns_dispatch("bob", "schedule") is False

    @pytest.mark.parametrize(
        "trigger",
        ["agent", "event", "schedule", "webhook", "reminder", "loop", "fan_out",
         "a2a", "operator_response"],
    )
    def test_pilot_owns_the_autonomous_triggers_dispatch_can_deliver(self, pilot, trigger):
        """Narrowed by #2048, re-widened by #2391 and #2523.

        This case originally parametrized all seven of ``_AUTONOMOUS_TRIGGERS``
        and asserted True for each — encoding reach the system did not have; it
        passed only because it called the predicate directly, outside the context
        that constrains it. #2048 cut it to what ``POST /task`` can emit. #2391
        then gave ``task_execution_service`` a pilot-gated ``queue_persistent``
        policy, so the scheduler's async-polled triggers genuinely reach the
        queue and belong here; #2523 added ``loop`` by making its driver
        terminal-driven, and #2524 added ``fan_out`` (aggregate as a query) plus
        ``a2a`` and ``operator_response`` (the sync edge adapter). The set is
        now every autonomous trigger. See ``test_2048_pull_pilot_reach.py``.
        """
        from services.agent_service.pull_mode import pull_owns_dispatch

        assert pull_owns_dispatch("alice", trigger) is True

    def test_pilot_does_not_own_a_trigger_the_reach_set_omits(self, pilot, monkeypatch):
        """The #2048 correction as a positive assertion.

        #2524 emptied the stranded set — every autonomous trigger reaches the
        queue now — so this drives the narrowing itself against a synthetic
        omission. That is the behaviour worth keeping: the next trigger declared
        autonomous must be classified, not inherit reach.
        """
        import services.pull_pilot as pp
        from services.agent_service.pull_mode import pull_owns_dispatch

        monkeypatch.setattr(
            pp, "PULL_REACHABLE_TRIGGERS", pp.PULL_REACHABLE_TRIGGERS - {"schedule"}
        )
        assert pull_owns_dispatch("alice", "schedule") is False
        assert pull_owns_dispatch("alice", "agent") is True

    @pytest.mark.parametrize("trigger", ["manual", "user", "chat", "voip", "voice", None])
    def test_pilot_does_not_own_interactive_triggers(self, pilot, trigger):
        """Open Question 7 scope cut: human turns keep the synchronous path."""
        from services.agent_service.pull_mode import pull_owns_dispatch

        assert pull_owns_dispatch("alice", trigger) is False

    def test_empty_allowlist_is_inert(self, monkeypatch):
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "")
        from services.agent_service.pull_mode import pull_owns_dispatch

        assert pull_owns_dispatch("alice", "schedule") is False

    def test_unresolvable_trigger_set_falls_back_to_push(self, pilot, monkeypatch):
        """Fail-safe direction: if the trigger set can't be resolved we must
        behave exactly as today (push), never silently claim dispatch."""
        import services.agent_service.pull_mode as pm

        monkeypatch.setitem(sys.modules, "services.task_execution_service", None)
        assert pm.pull_owns_dispatch("alice", "schedule") is False


# ---------------------------------------------------------------------------
# 2. Producer — a pilot's autonomous work is queue-only
# ---------------------------------------------------------------------------


class TestProducerGate:
    def test_pilot_autonomous_work_bypasses_admission(
        self, capacity, slot_service, backlog_service, pilot
    ):
        """The core fix: a free slot no longer means a push for a pilot.

        Driven with ``agent`` rather than the ``schedule`` this originally used
        (#2048). A ``queue_persistent`` acquire carrying ``schedule`` is a
        combination production cannot produce — cron dispatches through the
        ``"reject"`` producer — so the old pairing asserted the fix over a shape
        that does not exist while leaving the shape that does (agent-to-agent
        ``chat_with_agent``, the only traffic a pilot actually pulls) uncovered.
        """
        slot_service.acquire_slot = AsyncMock(return_value=True)  # slot IS free
        result = asyncio.run(
            capacity.acquire(
                agent_name="alice",
                execution_id="exec-1",
                max_concurrent=3,
                overflow_policy="queue_persistent",
                overflow_payload=_payload("agent"),
            )
        )
        assert result.state == "queued_persistent"
        # No ZADD: capacity for this agent is its worker pool, not the ZSET.
        slot_service.acquire_slot.assert_not_awaited()
        backlog_service.enqueue.assert_awaited_once()

    def test_pilot_interactive_work_still_pushes(
        self, capacity, slot_service, backlog_service, pilot
    ):
        """The carve-out, on the same agent: a human turn is admitted."""
        result = asyncio.run(
            capacity.acquire(
                agent_name="alice",
                execution_id="exec-2",
                max_concurrent=3,
                overflow_policy="queue_persistent",
                overflow_payload=_payload("manual"),
            )
        )
        assert result.state == "admitted"
        slot_service.acquire_slot.assert_awaited_once()
        backlog_service.enqueue.assert_not_awaited()

    def test_non_pilot_autonomous_work_unchanged(
        self, capacity, slot_service, backlog_service, pilot
    ):
        """`bob` is not in the allowlist, so `schedule` admits normally even
        though #2391 made that trigger reachable for a pilot."""
        result = asyncio.run(
            capacity.acquire(
                agent_name="bob",
                execution_id="exec-3",
                max_concurrent=3,
                overflow_policy="queue_persistent",
                overflow_payload=_payload("schedule"),
            )
        )
        assert result.state == "admitted"
        slot_service.acquire_slot.assert_awaited_once()

    def test_in_memory_policy_never_force_queued(
        self, capacity, slot_service, pilot
    ):
        """`/chat` uses queue_in_memory; the gate must not touch it even for a
        pilot, or the interactive path changes shape."""
        result = asyncio.run(
            capacity.acquire(
                agent_name="alice",
                execution_id="exec-4",
                max_concurrent=3,
                overflow_policy="queue_in_memory",
            )
        )
        assert result.state == "admitted"
        slot_service.acquire_slot.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. Consumer — the backend never drains a pilot's queue
# ---------------------------------------------------------------------------


class TestConsumerGate:
    def test_drain_skipped_for_pilot(self, monkeypatch, pilot):
        """Guard sits ahead of the queued COUNT, so the DB is never touched."""
        from services.backlog_service import BacklogService

        svc = BacklogService()
        called = {"count": 0}

        import database

        monkeypatch.setattr(
            database.db,
            "get_queued_count",
            lambda *_a, **_kw: called.__setitem__("count", called["count"] + 1) or 5,
        )
        assert asyncio.run(svc.drain_next("alice")) is False
        assert called["count"] == 0, "pilot drain must short-circuit before the COUNT"

    def test_drain_proceeds_for_non_pilot(self, monkeypatch, pilot):
        from services.backlog_service import BacklogService

        svc = BacklogService()
        import database

        monkeypatch.setattr(database.db, "get_queued_count", lambda *_a, **_kw: 0)
        # Reaches the COUNT (0 ⇒ False) rather than short-circuiting on identity.
        assert asyncio.run(svc.drain_next("bob")) is False

    def test_drain_unchanged_when_allowlist_empty(self, monkeypatch):
        from services.backlog_service import BacklogService

        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "")
        svc = BacklogService()
        import database

        seen = {}
        monkeypatch.setattr(
            database.db,
            "get_queued_count",
            lambda name, *_a, **_kw: seen.setdefault("name", name) and 0 or 0,
        )
        assert asyncio.run(svc.drain_next("alice")) is False
        assert seen.get("name") == "alice", "default path must still reach the COUNT"
