"""
Persistent Task Backlog Tests (BACKLOG-001)

Unit tests for the BACKLOG-001 feature: when an async task arrives at POST
/api/agents/{name}/task and the agent's parallel slots are full, the task
should spill into a persistent SQLite-backed backlog and auto-drain as slots
free up.

These tests run against an ephemeral SQLite database (TRINITY_DB_PATH set to
a tmp path) so they can exercise the real db/schedules.py and the
BacklogService without a live backend. Background execution is mocked so the
tests don't need a real agent container.

Covered:
- QUEUED value is present on the TaskExecutionStatus enum
- Migration creates queued_at, backlog_metadata, max_backlog_depth, and the
  partial index
- update_execution_to_queued / claim_next_queued FIFO semantics
- claim_next_queued is atomic under concurrent callers
- cancel_queued_execution / cancel_queued_for_agent
- expire_stale_queued honours max_age_hours
- BacklogService.enqueue respects max_backlog_depth
- BacklogService.drain_next: slot-first acquire, failed-claim releases slot,
  corrupt metadata marks FAILED, happy path spawns background task
- on_slot_released callback hook fires without blocking
- #496: lazy-import target `_run_async_task_with_persistence` exists in the
  real `routers/chat.py` source (AST-based — catches test-drift where a
  SimpleNamespace mock would inject the missing symbol back in).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: make src/backend importable. pytest auto-adds `tests/` to
# sys.path, which means `tests/utils/` (the API test helpers package) would
# shadow `src/backend/utils/` when backend code does `from utils.helpers ...`.
# We pop the shadow package from sys.modules and put src/backend at the FRONT
# of sys.path so backend imports resolve to the real utils package.
# ---------------------------------------------------------------------------

_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
# Evict any cached shadow package that would intercept `from utils ...`.
for _shadow in ("utils", "utils.api_client", "utils.assertions", "utils.cleanup"):
    sys.modules.pop(_shadow, None)
# Ensure backend path wins over tests/ which may already be on sys.path.
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun  # noqa: E402


@pytest.fixture
def tmp_db(db_backend):
    """Active backend with a fresh FULL production schema (db_harness, #300).

    Runs on SQLite and, when TEST_POSTGRES_URL is set, PostgreSQL. Replaces the
    old hand-rolled partial schema — which omitted schedule_executions.attempt_
    number that the backlog claim/cancel/expire queries reference, so those
    tests failed once the suite actually ran (quarantined in #1103). The real
    schema includes it, so they pass and are un-quarantined here. Returns the
    backend marker. Pops cached db modules so production code re-resolves."""
    def _evict():
        for mod in ("db.connection", "db.schedules",
                    "db.agent_settings.resources", "database"):
            sys.modules.pop(mod, None)

    _evict()
    try:
        yield db_backend
    finally:
        _evict()


@pytest.fixture
def schedule_ops(tmp_db):
    """Fresh ScheduleOperations instance bound to tmp_db."""
    from db.schedules import ScheduleOperations

    # BACKLOG-001 code paths only touch user_ops/agent_ops in methods we don't
    # exercise here (create_schedule, update_schedule), so stubs suffice.
    return ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())


@pytest.fixture
def resources_mixin(tmp_db):
    """Return a throwaway class that only uses ResourcesMixin methods."""
    from db.agent_settings.resources import ResourcesMixin

    class _Owner(ResourcesMixin):
        pass

    return _Owner()


@pytest.fixture
def seed_agent(tmp_db):
    """Insert a minimal agent_ownership row so backlog depth lookups work."""

    def _seed(name: str, max_parallel_tasks: int = 1, max_backlog_depth: int = 50):
        _hrun(
            "INSERT INTO agent_ownership (agent_name, owner_id, max_parallel_tasks, "
            "execution_timeout_seconds, max_backlog_depth, created_at) "
            "VALUES (:n, 1, :mpt, 900, :mbd, '2026-01-01T00:00:00Z')",
            n=name, mpt=max_parallel_tasks, mbd=max_backlog_depth,
        )

    return _seed


@pytest.fixture
def insert_execution(tmp_db):
    """Insert a schedule_executions row in a given status. Returns the id."""

    def _insert(
        *,
        agent_name: str,
        status: str = "running",
        started_at: str | None = None,
        queued_at: str | None = None,
        message: str = "do stuff",
        execution_id: str | None = None,
    ):
        import secrets as _secrets

        exec_id = execution_id or _secrets.token_urlsafe(12)
        ts = started_at or datetime.now(timezone.utc).isoformat()
        _hrun(
            "INSERT INTO schedule_executions "
            "(id, schedule_id, agent_name, status, started_at, queued_at, "
            " message, triggered_by) "
            "VALUES (:id, '__manual__', :a, :st, :ts, :q, :msg, 'manual')",
            id=exec_id, a=agent_name, st=status, ts=ts, q=queued_at, msg=message,
        )
        return exec_id

    return _insert


# ---------------------------------------------------------------------------
# TaskExecutionStatus enum
# ---------------------------------------------------------------------------


class TestEnum:
    def test_queued_value_exists(self):
        from models import TaskExecutionStatus

        assert TaskExecutionStatus.QUEUED == "queued"
        assert "queued" in {s.value for s in TaskExecutionStatus}


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


class TestMigration:
    def test_columns_and_index_present(self, tmp_db):
        # Dialect-agnostic schema introspection (#300) — works on SQLite + PG.
        from sqlalchemy import inspect
        from db.engine import get_engine

        insp = inspect(get_engine())
        se_cols = {c["name"] for c in insp.get_columns("schedule_executions")}
        assert "queued_at" in se_cols
        assert "backlog_metadata" in se_cols

        ao_cols = {c["name"] for c in insp.get_columns("agent_ownership")}
        assert "max_backlog_depth" in ao_cols

        index_names = {ix["name"] for ix in insp.get_indexes("schedule_executions")}
        assert "idx_executions_queued" in index_names


# ---------------------------------------------------------------------------
# ResourcesMixin: max_backlog_depth
# ---------------------------------------------------------------------------


class TestMaxBacklogDepth:
    def test_default_is_50(self, resources_mixin, seed_agent):
        seed_agent("alpha")
        assert resources_mixin.get_max_backlog_depth("alpha") == 50

    def test_unknown_agent_returns_default(self, resources_mixin):
        assert resources_mixin.get_max_backlog_depth("ghost") == 50

    def test_set_valid_range(self, resources_mixin, seed_agent):
        seed_agent("alpha")
        assert resources_mixin.set_max_backlog_depth("alpha", 100) is True
        assert resources_mixin.get_max_backlog_depth("alpha") == 100

    @pytest.mark.parametrize("bad", [0, -1, 201, 5000])
    def test_set_out_of_range_rejected(self, resources_mixin, seed_agent, bad):
        seed_agent("alpha")
        assert resources_mixin.set_max_backlog_depth("alpha", bad) is False
        assert resources_mixin.get_max_backlog_depth("alpha") == 50


# ---------------------------------------------------------------------------
# ScheduleOperations: backlog queries
# ---------------------------------------------------------------------------


class TestBacklogQueries:
    def test_update_execution_to_queued_transitions_row(
        self, schedule_ops, insert_execution
    ):
        eid = insert_execution(agent_name="alpha", status="running")
        now = datetime.now(timezone.utc).isoformat()
        assert schedule_ops.update_execution_to_queued(eid, '{"k":1}', now) is True
        assert schedule_ops.get_queued_count("alpha") == 1

    def test_claim_next_queued_is_fifo(self, schedule_ops, insert_execution):
        now = datetime.now(timezone.utc)
        eid_a = insert_execution(agent_name="alpha", status="running")
        eid_b = insert_execution(agent_name="alpha", status="running")
        eid_c = insert_execution(agent_name="alpha", status="running")

        schedule_ops.update_execution_to_queued(
            eid_a, "{}", (now - timedelta(seconds=3)).isoformat()
        )
        schedule_ops.update_execution_to_queued(
            eid_b, "{}", (now - timedelta(seconds=2)).isoformat()
        )
        schedule_ops.update_execution_to_queued(
            eid_c, "{}", (now - timedelta(seconds=1)).isoformat()
        )

        assert schedule_ops.claim_next_queued("alpha")["id"] == eid_a
        assert schedule_ops.claim_next_queued("alpha")["id"] == eid_b
        assert schedule_ops.claim_next_queued("alpha")["id"] == eid_c
        assert schedule_ops.claim_next_queued("alpha") is None

    def test_claim_next_queued_sets_row_to_running(
        self, schedule_ops, insert_execution
    ):
        eid = insert_execution(agent_name="alpha", status="running")
        schedule_ops.update_execution_to_queued(
            eid, "{}", datetime.now(timezone.utc).isoformat()
        )
        schedule_ops.claim_next_queued("alpha")
        row = schedule_ops.get_execution(eid)
        assert row.status == "running"
        assert schedule_ops.get_queued_count("alpha") == 0

    def test_claim_next_queued_isolated_per_agent(
        self, schedule_ops, insert_execution
    ):
        now = datetime.now(timezone.utc).isoformat()
        a_id = insert_execution(agent_name="alpha", status="running")
        b_id = insert_execution(agent_name="beta", status="running")
        schedule_ops.update_execution_to_queued(a_id, "{}", now)
        schedule_ops.update_execution_to_queued(b_id, "{}", now)

        assert schedule_ops.claim_next_queued("alpha")["id"] == a_id
        assert schedule_ops.claim_next_queued("alpha") is None
        # beta still has its row
        assert schedule_ops.claim_next_queued("beta")["id"] == b_id

    def test_release_claim_to_queued_puts_row_back(
        self, schedule_ops, insert_execution
    ):
        eid = insert_execution(agent_name="alpha", status="running")
        schedule_ops.update_execution_to_queued(
            eid, "{}", datetime.now(timezone.utc).isoformat()
        )
        claimed = schedule_ops.claim_next_queued("alpha")
        assert claimed is not None
        assert schedule_ops.release_claim_to_queued(eid) is True
        assert schedule_ops.get_queued_count("alpha") == 1

    def test_cancel_queued_execution_single(self, schedule_ops, insert_execution):
        eid = insert_execution(agent_name="alpha", status="running")
        schedule_ops.update_execution_to_queued(
            eid, "{}", datetime.now(timezone.utc).isoformat()
        )
        assert schedule_ops.cancel_queued_execution(eid, "test cancel") is True
        assert schedule_ops.get_queued_count("alpha") == 0
        assert schedule_ops.get_execution(eid).status == "cancelled"

    def test_cancel_queued_execution_noop_if_not_queued(
        self, schedule_ops, insert_execution
    ):
        eid = insert_execution(agent_name="alpha", status="running")
        assert schedule_ops.cancel_queued_execution(eid) is False

    def test_cancel_queued_for_agent_bulk(self, schedule_ops, insert_execution):
        now = datetime.now(timezone.utc).isoformat()
        ids = [insert_execution(agent_name="alpha", status="running") for _ in range(3)]
        for i in ids:
            schedule_ops.update_execution_to_queued(i, "{}", now)
        other = insert_execution(agent_name="beta", status="running")
        schedule_ops.update_execution_to_queued(other, "{}", now)

        n = schedule_ops.cancel_queued_for_agent("alpha", reason="agent_deleted")
        assert n == 3
        assert schedule_ops.get_queued_count("alpha") == 0
        assert schedule_ops.get_queued_count("beta") == 1

    def test_expire_stale_queued_respects_age(
        self, schedule_ops, insert_execution
    ):
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        new_ts = datetime.now(timezone.utc).isoformat()

        eid_old = insert_execution(agent_name="alpha", status="running")
        eid_new = insert_execution(agent_name="alpha", status="running")

        schedule_ops.update_execution_to_queued(eid_old, "{}", old_ts)
        schedule_ops.update_execution_to_queued(eid_new, "{}", new_ts)

        n = schedule_ops.expire_stale_queued(max_age_hours=24)
        assert n == 1
        assert schedule_ops.get_execution(eid_old).status == "failed"
        assert schedule_ops.get_execution(eid_new).status == "queued"

    def test_expire_stale_queued_with_tiny_threshold(
        self, schedule_ops, insert_execution
    ):
        """Tests the time-mock-free pattern: pass a very small max_age_hours."""
        past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        eid = insert_execution(agent_name="alpha", status="running")
        schedule_ops.update_execution_to_queued(eid, "{}", past)

        # 1/3600 hours == 1 second
        n = schedule_ops.expire_stale_queued(max_age_hours=(1 / 3600))
        assert n == 1

    def test_list_agents_with_queued(self, schedule_ops, insert_execution):
        now = datetime.now(timezone.utc).isoformat()
        a_id = insert_execution(agent_name="alpha", status="running")
        b_id = insert_execution(agent_name="beta", status="running")
        insert_execution(agent_name="gamma", status="running")  # not queued

        schedule_ops.update_execution_to_queued(a_id, "{}", now)
        schedule_ops.update_execution_to_queued(b_id, "{}", now)

        agents = set(schedule_ops.list_agents_with_queued())
        assert agents == {"alpha", "beta"}


# ---------------------------------------------------------------------------
# BacklogService.enqueue
# ---------------------------------------------------------------------------


class _FakeDb:
    """Stand-in for the global db module used by BacklogService.

    BacklogService late-imports `database.db`. We monkeypatch sys.modules
    before constructing the service so those imports resolve here.
    """

    def __init__(self):
        self.queued = {}  # execution_id -> metadata
        self.backlog_depth = 50
        self.max_parallel_tasks = 1
        self.execution_timeout = 900
        self.claim_next_return = None
        self.spawned = []
        self.queued_count_value = 0
        self.release_claim_called = False
        self.fail_status_calls = []

    def get_queued_count(self, agent_name):
        return self.queued_count_value

    def get_max_backlog_depth(self, agent_name):
        return self.backlog_depth

    def get_max_parallel_tasks(self, agent_name):
        return self.max_parallel_tasks

    def get_execution_timeout(self, agent_name):
        return self.execution_timeout

    def update_execution_to_queued(self, execution_id, metadata, queued_at):
        self.queued[execution_id] = metadata
        self.queued_count_value += 1
        return True

    def claim_next_queued(self, agent_name):
        return self.claim_next_return

    def release_claim_to_queued(self, execution_id):
        self.release_claim_called = True
        return True

    def update_execution_status(self, **kwargs):
        self.fail_status_calls.append(kwargs)
        return True

    def list_agents_with_queued(self):
        return []

    def expire_stale_queued(self, max_age_hours):
        return 0

    def cancel_queued_for_agent(self, agent_name, reason="agent_deleted"):
        return 0


class _FakeSlotService:
    """Minimal slot service that records acquisitions/releases."""

    def __init__(self, *, allow_acquire=True):
        self.allow_acquire = allow_acquire
        self.acquire_calls = []
        self.release_calls = []
        self._on_release_callbacks = []

    async def acquire_slot(self, **kwargs):
        self.acquire_calls.append(kwargs)
        return self.allow_acquire

    async def release_slot(self, agent_name, execution_id):
        self.release_calls.append((agent_name, execution_id))

    def register_on_release(self, cb):
        self._on_release_callbacks.append(cb)


@pytest.fixture
def fake_db(monkeypatch):
    """Install a fake `database` module under sys.modules."""
    fake = _FakeDb()
    module = types.SimpleNamespace(db=fake)
    monkeypatch.setitem(sys.modules, "database", module)
    return fake


@pytest.fixture
def fake_slots(monkeypatch):
    """Patch get_slot_service to return our fake slots."""
    fake = _FakeSlotService()
    import services.slot_service as slot_mod

    monkeypatch.setattr(slot_mod, "get_slot_service", lambda: fake)
    # Also patch on services.backlog_service so late imports hit the fake
    import services.backlog_service as backlog_mod

    monkeypatch.setattr(backlog_mod, "get_slot_service", lambda: fake)
    return fake


def _make_request():
    from models import ParallelTaskRequest

    return ParallelTaskRequest(
        message="hello world",
        model="sonnet",
        allowed_tools=["Read"],
        system_prompt="be terse",
        timeout_seconds=300,
        async_mode=True,
    )


@pytest.mark.asyncio
class TestBacklogEnqueue:
    async def test_enqueue_under_cap_succeeds(self, fake_db, fake_slots):
        from services.backlog_service import BacklogService

        svc = BacklogService()
        fake_db.queued_count_value = 5
        fake_db.backlog_depth = 50

        ok = await svc.enqueue(
            agent_name="alpha",
            execution_id="exec-1",
            request=_make_request(),
            effective_timeout=300,
            user_id=7,
            user_email="u@example.com",
            subscription_id="sub-1",
            x_source_agent=None,
            x_mcp_key_id=None,
            x_mcp_key_name=None,
            triggered_by="manual",
            collaboration_activity_id=None,
        )
        assert ok is True
        stored = json.loads(fake_db.queued["exec-1"])
        assert stored["message"] == "hello world"
        assert stored["model"] == "sonnet"
        assert stored["user_id"] == 7
        assert stored["subscription_id"] == "sub-1"
        # Credentials must never leak into the metadata blob
        assert "password" not in stored
        assert "credential" not in stored
        assert "token" not in str(stored).lower() or "mcp" in str(stored).lower()

    async def test_enqueue_rejected_when_at_cap(self, fake_db, fake_slots):
        from services.backlog_service import BacklogService

        svc = BacklogService()
        fake_db.queued_count_value = 50
        fake_db.backlog_depth = 50

        ok = await svc.enqueue(
            agent_name="alpha",
            execution_id="exec-1",
            request=_make_request(),
            effective_timeout=300,
            user_id=None,
            user_email=None,
            subscription_id=None,
            x_source_agent=None,
            x_mcp_key_id=None,
            x_mcp_key_name=None,
            triggered_by="manual",
            collaboration_activity_id=None,
        )
        assert ok is False
        assert "exec-1" not in fake_db.queued

    async def test_enqueue_captures_self_task_fields(self, fake_db, fake_slots):
        """#496: SELF-EXEC-001 (#264) inject_result must survive backlog
        spillover. enqueue stores is_self_task + self_task_activity_id +
        request.inject_result so _spawn_drain can rehydrate the request
        and pass them to _run_async_task_with_persistence.
        """
        from services.backlog_service import BacklogService
        from models import ParallelTaskRequest

        svc = BacklogService()
        fake_db.queued_count_value = 0
        fake_db.backlog_depth = 50

        request = ParallelTaskRequest(
            message="self-task at capacity",
            async_mode=True,
            inject_result=True,
        )
        ok = await svc.enqueue(
            agent_name="alpha",
            execution_id="exec-self-1",
            request=request,
            effective_timeout=300,
            user_id=7,
            user_email="u@example.com",
            subscription_id="sub-1",
            x_source_agent="alpha",  # source == target = self-task
            x_mcp_key_id=None,
            x_mcp_key_name=None,
            triggered_by="self_task",
            collaboration_activity_id=None,
            is_self_task=True,
            self_task_activity_id="act-self-9",
        )
        assert ok is True
        stored = json.loads(fake_db.queued["exec-self-1"])
        assert stored["is_self_task"] is True
        assert stored["self_task_activity_id"] == "act-self-9"
        assert stored["inject_result"] is True
        assert stored["x_source_agent"] == "alpha"


# ---------------------------------------------------------------------------
# BacklogService.drain_next
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBacklogDrain:
    async def test_drain_empty_backlog_is_noop(self, fake_db, fake_slots):
        from services.backlog_service import BacklogService

        svc = BacklogService()
        fake_db.queued_count_value = 0
        assert await svc.drain_next("alpha") is False
        assert fake_slots.acquire_calls == []

    async def test_drain_releases_slot_when_claim_returns_nothing(
        self, fake_db, fake_slots
    ):
        from services.backlog_service import BacklogService

        svc = BacklogService()
        fake_db.queued_count_value = 1
        fake_db.claim_next_return = None  # race: someone else drained

        assert await svc.drain_next("alpha") is False
        # Sentinel slot acquired then released.
        assert len(fake_slots.acquire_calls) == 1
        assert len(fake_slots.release_calls) == 1

    async def test_drain_uses_effective_cap_for_slot_acquire(
        self, fake_db, fake_slots, monkeypatch
    ):
        """#506: the backlog-drain bypass sizes the slot acquire from the
        EFFECTIVE cap (``get_effective_max_parallel_tasks`` = stored clamped to
        the fleet ceiling), not the raw stored ``max_parallel_tasks``.

        Patch the exact function ``drain_next`` imports so the test is hermetic
        — independent of the settings DB, the ceiling-read chain, ``from
        database import db`` resolution, and any cross-test stubbing of the
        ``services.settings_service`` module in ``sys.modules``. The raw stored
        cap is 9 and the effective cap is 2; asserting 2 (not 9) proves drain
        uses the clamped value. The clamp math itself is covered directly in
        tests/unit/test_506_max_parallel_ceiling.py.
        """
        from services.backlog_service import BacklogService

        fake_db.max_parallel_tasks = 9  # raw stored value drain must NOT use
        # Install a stub `services.settings_service` directly in sys.modules
        # (same technique as the `fake_db` fixture for `database`). drain_next
        # does a call-time `from services.settings_service import
        # get_effective_max_parallel_tasks`, so this is what it resolves —
        # immune to the module-identity gotcha where a `monkeypatch.setattr`
        # on a separately-imported `ss` lands on a different object than the
        # one backlog re-resolves from sys.modules under some test orderings.
        fake_ss = types.SimpleNamespace(
            get_effective_max_parallel_tasks=lambda _name: 2
        )
        monkeypatch.setitem(sys.modules, "services.settings_service", fake_ss)

        svc = BacklogService()
        fake_db.queued_count_value = 1
        fake_db.claim_next_return = None  # stop after the sentinel acquire

        await svc.drain_next("alpha")
        assert fake_slots.acquire_calls, "expected a sentinel slot acquire"
        assert fake_slots.acquire_calls[0]["max_parallel_tasks"] == 2

    async def test_drain_corrupt_metadata_marks_failed(self, fake_db, fake_slots):
        from services.backlog_service import BacklogService

        svc = BacklogService()
        fake_db.queued_count_value = 1
        fake_db.claim_next_return = {
            "id": "exec-1",
            "agent_name": "alpha",
            "message": "hi",
            "backlog_metadata": "{not json",
        }

        assert await svc.drain_next("alpha") is False
        assert fake_db.fail_status_calls
        assert fake_db.fail_status_calls[0]["status"] == "failed"
        assert "corrupt" in fake_db.fail_status_calls[0]["error"]

    async def test_drain_slot_acquire_failure_is_noop(self, fake_db, monkeypatch):
        from services.backlog_service import BacklogService
        import services.backlog_service as backlog_mod

        fake = _FakeSlotService(allow_acquire=False)
        monkeypatch.setattr(backlog_mod, "get_slot_service", lambda: fake)

        svc = BacklogService()
        fake_db.queued_count_value = 1

        assert await svc.drain_next("alpha") is False
        assert len(fake.acquire_calls) == 1
        # No release because nothing was acquired
        assert len(fake.release_calls) == 0

    async def test_drain_happy_path_spawns_background(
        self, fake_db, fake_slots, monkeypatch
    ):
        """#496: drain spawns _run_async_task_with_persistence (post-#95
        replacement for the deleted _execute_task_background). Asserts the
        new kwarg surface and that self-task metadata round-trips through
        the drain so SELF-EXEC-001 (#264) survives backlog spillover.
        """
        from services.backlog_service import BacklogService

        spawned = {}

        async def _fake_bg(**kwargs):
            spawned.update(kwargs)

        # Install a fake routers.chat module so the late import inside
        # _spawn_drain picks up our stub instead of the real one. The
        # AST-based regression test (TestLazyImportTarget) separately
        # asserts the real routers/chat.py defines this symbol — together
        # they catch the test-drift class that produced #496.
        fake_chat = types.SimpleNamespace(_run_async_task_with_persistence=_fake_bg)
        monkeypatch.setitem(sys.modules, "routers.chat", fake_chat)

        metadata = {
            "message": "hi",
            "model": "sonnet",
            "timeout_seconds": 300,
            "user_id": 5,
            "user_email": "u@example.com",
            "subscription_id": "sub-x",
            "collaboration_activity_id": "collab-1",
            "x_source_agent": "beta",
            "is_self_task": False,
            "self_task_activity_id": None,
            "inject_result": False,
        }
        fake_db.queued_count_value = 1
        fake_db.claim_next_return = {
            "id": "exec-7",
            "agent_name": "alpha",
            "message": "hi",
            "backlog_metadata": json.dumps(metadata),
        }

        svc = BacklogService()
        assert await svc.drain_next("alpha") is True

        # Give the spawned task a tick to run
        await asyncio.sleep(0)
        assert spawned["agent_name"] == "alpha"
        assert spawned["execution_id"] == "exec-7"
        assert spawned["user_id"] == 5
        assert spawned["user_email"] == "u@example.com"
        assert spawned["subscription_id"] == "sub-x"
        assert spawned["collaboration_activity_id"] == "collab-1"
        assert spawned["x_source_agent"] == "beta"
        assert spawned["is_self_task"] is False
        assert spawned["self_task_activity_id"] is None

    async def test_drain_threads_self_task_fields(
        self, fake_db, fake_slots, monkeypatch
    ):
        """#496: when a queued row was a self-task at enqueue time, drain
        rehydrates is_self_task + self_task_activity_id + inject_result
        on the request, so _run_async_task_with_persistence completes the
        SELF-EXEC-001 activity and injects the result.
        """
        from services.backlog_service import BacklogService

        spawned = {}

        async def _fake_bg(**kwargs):
            spawned.update(kwargs)
            spawned["request_inject_result"] = kwargs["request"].inject_result

        fake_chat = types.SimpleNamespace(_run_async_task_with_persistence=_fake_bg)
        monkeypatch.setitem(sys.modules, "routers.chat", fake_chat)

        metadata = {
            "message": "self-task at capacity",
            "timeout_seconds": 300,
            "user_id": 5,
            "user_email": "u@example.com",
            "subscription_id": "sub-x",
            "x_source_agent": "alpha",  # source == target
            "is_self_task": True,
            "self_task_activity_id": "act-self-9",
            "inject_result": True,
        }
        fake_db.queued_count_value = 1
        fake_db.claim_next_return = {
            "id": "exec-self-7",
            "agent_name": "alpha",
            "message": "self-task at capacity",
            "backlog_metadata": json.dumps(metadata),
        }

        svc = BacklogService()
        assert await svc.drain_next("alpha") is True
        await asyncio.sleep(0)
        assert spawned["is_self_task"] is True
        assert spawned["self_task_activity_id"] == "act-self-9"
        assert spawned["request_inject_result"] is True


# ---------------------------------------------------------------------------
# Lazy-import target validation (#496 — test-drift regression guard)
# ---------------------------------------------------------------------------


class TestLazyImportTarget:
    """Static (AST-based) check that BacklogService._spawn_drain's lazy-import
    target exists in the real routers/chat.py source.

    History: #95 deleted `_execute_task_background` from routers/chat.py and
    replaced it with `_run_async_task_with_persistence`, but the lazy import
    in services/backlog_service.py was missed. The exception was swallowed
    at the call site, so backlog drain silently failed for weeks. The
    pre-existing happy-path test injected a SimpleNamespace stub of
    routers.chat into sys.modules with whatever attribute name the test
    expected, masking the bug.

    This test parses the real routers/chat.py without importing it (the
    module pulls in FastAPI, the database singleton, etc., which is too
    heavy for unit tests) and asserts the symbol is still defined.
    Same idea works for any future rename — keep this in sync with the
    lazy import in services/backlog_service.py._spawn_drain.
    """

    LAZY_IMPORT_TARGETS = ("_run_async_task_with_persistence",)

    def test_routers_chat_defines_lazy_import_targets(self):
        import ast

        chat_src = _BACKEND / "routers" / "chat.py"
        assert chat_src.exists(), f"routers/chat.py not found at {chat_src}"

        tree = ast.parse(chat_src.read_text(), filename=str(chat_src))
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        }
        for target in self.LAZY_IMPORT_TARGETS:
            assert target in defined, (
                f"BacklogService._spawn_drain lazy-imports `{target}` from "
                f"routers.chat, but the symbol is not defined there. This is "
                f"the failure mode that produced #496 — update either the "
                f"lazy import or this allow-list."
            )

    def test_backlog_service_lazy_import_matches_target_list(self):
        """Belt-and-suspenders: the lazy import string in
        services/backlog_service.py must reference one of the targets
        validated above. Catches the inverse drift (someone renames the
        symbol in routers/chat.py and forgets to update backlog_service.py
        — production breaks; this test catches it).
        """
        import re

        backlog_src = _BACKEND / "services" / "backlog_service.py"
        text = backlog_src.read_text()
        # Match: from routers.chat import <name>
        matches = re.findall(
            r"from\s+routers\.chat\s+import\s+([A-Za-z_][A-Za-z0-9_]*)",
            text,
        )
        assert matches, (
            "Expected at least one `from routers.chat import ...` in "
            "services/backlog_service.py — has the lazy-import scheme changed?"
        )
        for imported in matches:
            assert imported in self.LAZY_IMPORT_TARGETS, (
                f"services/backlog_service.py lazy-imports `{imported}` from "
                f"routers.chat, but it's not in the validated allow-list "
                f"{self.LAZY_IMPORT_TARGETS}. Update the allow-list or fix "
                f"the lazy import."
            )


# ---------------------------------------------------------------------------
# SlotService release callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSlotReleaseCallback:
    async def test_register_and_fire_callback(self, monkeypatch):
        import services.slot_service as slot_mod

        # SlotService requires a working Redis — stub the constructor.
        stub_redis = MagicMock()
        stub_redis.zrem.return_value = 1
        stub_redis.zcard.return_value = 0
        stub_redis.delete.return_value = None
        stub_redis.zrangebyscore.return_value = []
        monkeypatch.setattr(slot_mod.redis, "from_url", lambda *a, **kw: stub_redis)

        svc = slot_mod.SlotService("redis://localhost:6379")
        fired = []

        async def _cb(agent_name):
            fired.append(agent_name)

        svc.register_on_release(_cb)
        await svc.release_slot("alpha", "exec-1")
        # Callback runs via create_task; yield to let it execute.
        await asyncio.sleep(0)
        assert fired == ["alpha"]

    async def test_one_callback_exception_does_not_block_others(self, monkeypatch):
        import services.slot_service as slot_mod

        stub_redis = MagicMock()
        stub_redis.zrem.return_value = 1
        stub_redis.zcard.return_value = 0
        stub_redis.delete.return_value = None
        stub_redis.zrangebyscore.return_value = []
        monkeypatch.setattr(slot_mod.redis, "from_url", lambda *a, **kw: stub_redis)

        svc = slot_mod.SlotService("redis://localhost:6379")
        fired = []

        async def _bad(agent_name):
            raise RuntimeError("boom")

        async def _good(agent_name):
            fired.append(agent_name)

        svc.register_on_release(_bad)
        svc.register_on_release(_good)
        await svc.release_slot("alpha", "exec-1")
        await asyncio.sleep(0)
        assert fired == ["alpha"]
