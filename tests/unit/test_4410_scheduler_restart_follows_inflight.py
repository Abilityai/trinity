"""A scheduler restart keeps following the runs that were in flight (#4410).

The scheduler follows each run it starts with an in-process poll task. That
task is what publishes the run's completion and applies retry (RETRY-001) and
validation (VALIDATE-001). Shutdown cancels the task and boot used to recover
pending retries and reminders only, so a run in flight across a restart never
got either.

Each restart below is two `SchedulerService` objects over one database and one
Redis: the first starts following a run and is shut down, the second boots and
must finish the job. The real poll loop, retry decision and validation decision
run; only the retry job's arming and the validation HTTP call are replaced.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_SRC = str(Path(__file__).resolve().parent.parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from scheduler import service as service_module  # noqa: E402
from scheduler.database import SchedulerDatabase  # noqa: E402
from scheduler.models import ExecutionStatus  # noqa: E402
from scheduler.service import SchedulerService, _FOLLOWED_EXECUTIONS_KEY  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

AGENT = "test-agent"
SCHEDULE = "schedule-1"


class FakeRedis:
    """The hash commands the scheduler uses, over a dict two services share."""

    def __init__(self, store: dict):
        self.store = store

    def hset(self, key, field, value):
        self.store.setdefault(key, {})[field] = value
        return 1

    def hdel(self, key, field):
        return 1 if self.store.get(key, {}).pop(field, None) is not None else 0

    def hgetall(self, key):
        return dict(self.store.get(key, {}))

    def publish(self, *_a, **_k):
        return 1

    def close(self):
        pass


@pytest.fixture
def db(tmp_path) -> SchedulerDatabase:
    path = str(tmp_path / "scheduler.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE agent_schedules (
            id TEXT PRIMARY KEY, agent_name TEXT NOT NULL, name TEXT NOT NULL,
            cron_expression TEXT NOT NULL, message TEXT NOT NULL,
            enabled INTEGER DEFAULT 1, timezone TEXT DEFAULT 'UTC',
            description TEXT, owner_id INTEGER, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, last_run_at TEXT, next_run_at TEXT,
            model TEXT, timeout_seconds INTEGER, max_retries INTEGER DEFAULT 0,
            retry_delay_seconds INTEGER DEFAULT 60,
            validation_enabled INTEGER DEFAULT 0, validation_prompt TEXT,
            validation_timeout_seconds INTEGER DEFAULT 120, deleted_at TEXT
        );
        CREATE TABLE schedule_executions (
            id TEXT PRIMARY KEY, schedule_id TEXT, agent_name TEXT NOT NULL,
            status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
            duration_ms INTEGER, message TEXT NOT NULL, response TEXT, error TEXT,
            triggered_by TEXT NOT NULL, context_used INTEGER, context_max INTEGER,
            cost REAL, tool_calls TEXT, execution_log TEXT, claude_session_id TEXT,
            source_user_id INTEGER, source_user_email TEXT, source_agent_name TEXT,
            source_mcp_key_id TEXT, source_mcp_key_name TEXT, model_used TEXT,
            skip_reason TEXT, attempt_number INTEGER DEFAULT 1,
            retry_of_execution_id TEXT, retry_scheduled_at TEXT, claim_token TEXT,
            chain_depth INTEGER, subscription_id TEXT,
            business_status TEXT, validated_at TEXT, validation_execution_id TEXT
        );
        CREATE TABLE agent_ownership (
            agent_name TEXT PRIMARY KEY, owner_id INTEGER NOT NULL,
            autonomy_enabled INTEGER DEFAULT 0, created_at TEXT, deleted_at TEXT,
            subscription_id TEXT
        );
        """
    )
    now = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, autonomy_enabled, created_at) "
        "VALUES (?, 1, 1, ?)",
        (AGENT, now),
    )
    conn.execute(
        "INSERT INTO agent_schedules (id, agent_name, name, cron_expression, message, "
        "created_at, updated_at, timeout_seconds, max_retries, validation_enabled) "
        "VALUES (?, ?, 'Morning', '0 9 * * *', 'Run the report', ?, ?, 900, 2, 1)",
        (SCHEDULE, AGENT, now, now),
    )
    conn.commit()
    conn.close()
    return SchedulerDatabase(database_path=path)


@pytest.fixture
def redis_store() -> dict:
    return {}


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch, redis_store):
    monkeypatch.setattr(service_module.config, "poll_interval", 0.01)
    # `shutdown()` drops the client and the `redis` property reconnects on the
    # next use. The reconnect reaches the same store, as it would in production,
    # so a write made after shutdown is seen by these tests.
    monkeypatch.setattr(
        service_module.redis, "from_url", lambda *_a, **_k: FakeRedis(redis_store)
    )


def _service(db: SchedulerDatabase, store: dict) -> SchedulerService:
    service = SchedulerService(database=db, lock_manager=MagicMock())
    service._redis = FakeRedis(store)
    service._schedule_retry_job = MagicMock()
    service._call_backend_trigger_validation = AsyncMock(return_value={})
    return service


def _run(db: SchedulerDatabase, triggered_by: str = "schedule"):
    return db.create_execution(
        schedule_id=SCHEDULE, agent_name=AGENT, message="Run the report",
        triggered_by=triggered_by,
    )


async def _stop(service: SchedulerService) -> None:
    """What `shutdown()` does to the poll tasks, then let the cancels land."""
    tasks = list(service._active_poll_tasks)
    service.shutdown()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert all(t.cancelled() for t in tasks)


async def _settle(service: SchedulerService) -> None:
    tasks = list(service._active_poll_tasks)
    assert tasks, "nothing is being followed"
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    await asyncio.sleep(0)  # done callbacks


def _followed(store: dict) -> dict:
    return store.get(_FOLLOWED_EXECUTIONS_KEY, {})


# ---------------------------------------------------------------------------
# The restart
# ---------------------------------------------------------------------------

async def test_a_run_that_fails_while_the_scheduler_is_down_is_still_retried(db, redis_store):
    before = _service(db, redis_store)
    run = _run(db)
    before._spawn_poll(run.id, 900, AGENT)
    await _stop(before)

    db.update_execution_status(run.id, ExecutionStatus.FAILED, error="Agent at capacity")

    after = _service(db, redis_store)
    after._recover_followed_executions()
    await _settle(after)

    after._schedule_retry_job.assert_called_once()
    assert after._schedule_retry_job.call_args.kwargs["execution"].id == run.id
    assert db.get_execution(run.id).status == ExecutionStatus.PENDING_RETRY


async def test_a_run_that_succeeds_while_the_scheduler_is_down_is_still_validated(db, redis_store):
    before = _service(db, redis_store)
    run = _run(db)
    before._spawn_poll(run.id, 900, AGENT)
    await _stop(before)

    db.update_execution_status(run.id, ExecutionStatus.SUCCESS, response="done")

    after = _service(db, redis_store)
    after._recover_followed_executions()
    await _settle(after)

    after._call_backend_trigger_validation.assert_awaited_once()
    assert after._call_backend_trigger_validation.await_args.kwargs["execution_id"] == run.id
    after._schedule_retry_job.assert_not_called()


async def test_a_run_still_going_at_boot_is_followed_to_its_end(db, redis_store):
    before = _service(db, redis_store)
    run = _run(db)
    before._spawn_poll(run.id, 900, AGENT)
    await _stop(before)

    after = _service(db, redis_store)
    after._recover_followed_executions()
    await asyncio.sleep(0.05)  # several polls see `running`
    after._schedule_retry_job.assert_not_called()
    assert after._active_poll_tasks

    db.update_execution_status(run.id, ExecutionStatus.FAILED, error="boom")
    await _settle(after)

    after._schedule_retry_job.assert_called_once()


async def test_the_poll_budget_and_agent_survive_the_restart(db, redis_store):
    before = _service(db, redis_store)
    run = _run(db)
    before._spawn_poll(run.id, 1234, AGENT)
    await _stop(before)

    after = _service(db, redis_store)
    after._spawn_poll = MagicMock()
    after._recover_followed_executions()

    after._spawn_poll.assert_called_once_with(run.id, 1234, AGENT)


async def test_boot_runs_the_recovery(db, redis_store):
    before = _service(db, redis_store)
    run = _run(db)
    before._spawn_poll(run.id, 900, AGENT)
    await _stop(before)
    db.update_execution_status(run.id, ExecutionStatus.FAILED, error="boom")

    after = _service(db, redis_store)
    after.initialize()
    try:
        await _settle(after)
        after._schedule_retry_job.assert_called_once()
    finally:
        after.shutdown()


async def test_a_failing_recovery_does_not_stop_boot(db, redis_store):
    service = _service(db, redis_store)
    service._recover_followed_executions = MagicMock(side_effect=RuntimeError("redis down"))
    service.initialize()
    try:
        assert service._initialized is True
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

async def test_a_poll_that_ends_on_its_own_forgets_the_run(db, redis_store):
    service = _service(db, redis_store)
    run = _run(db)
    service._spawn_poll(run.id, 900, AGENT)
    assert json.loads(_followed(redis_store)[run.id]) == {
        "agent_name": AGENT, "timeout_seconds": 900,
    }

    db.update_execution_status(run.id, ExecutionStatus.SUCCESS, response="done")
    await _settle(service)

    assert _followed(redis_store) == {}


async def test_a_poll_cancelled_by_shutdown_keeps_the_run(db, redis_store):
    service = _service(db, redis_store)
    run = _run(db)
    service._spawn_poll(run.id, 900, AGENT)
    await _stop(service)

    assert list(_followed(redis_store)) == [run.id]


async def test_a_redis_fault_does_not_stop_the_run_being_followed(db, redis_store):
    service = _service(db, redis_store)
    service._redis.hset = MagicMock(side_effect=ConnectionError("redis down"))
    service._redis.hdel = MagicMock(side_effect=ConnectionError("redis down"))
    run = _run(db)

    service._spawn_poll(run.id, 900, AGENT)
    db.update_execution_status(run.id, ExecutionStatus.FAILED, error="boom")
    await _settle(service)

    service._schedule_retry_job.assert_called_once()


# ---------------------------------------------------------------------------
# What recovery leaves alone
# ---------------------------------------------------------------------------

async def test_a_run_whose_retry_is_already_decided_is_not_handled_twice(db, redis_store):
    """`pending_retry` is owned by `_recover_pending_retries`."""
    before = _service(db, redis_store)
    run = _run(db)
    before._spawn_poll(run.id, 900, AGENT)
    await _stop(before)
    db.update_execution_status(run.id, ExecutionStatus.FAILED, error="boom")
    db.schedule_retry(run.id, datetime.utcnow())

    after = _service(db, redis_store)
    after._recover_followed_executions()

    assert not after._active_poll_tasks
    assert _followed(redis_store) == {}


async def test_a_record_for_a_row_that_is_gone_is_dropped(db, redis_store):
    redis_store[_FOLLOWED_EXECUTIONS_KEY] = {"purged-row": "{}"}

    service = _service(db, redis_store)
    service._recover_followed_executions()

    assert not service._active_poll_tasks
    assert _followed(redis_store) == {}


async def test_an_unreadable_record_still_follows_the_run(db, redis_store):
    run = _run(db)
    redis_store[_FOLLOWED_EXECUTIONS_KEY] = {run.id: "not json"}

    service = _service(db, redis_store)
    service._spawn_poll = MagicMock()
    service._recover_followed_executions()

    service._spawn_poll.assert_called_once_with(run.id, None, AGENT)


# ---------------------------------------------------------------------------
# The database fallback: a lost record, or the first boot after this change
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("triggered_by", ["schedule", "retry"])
async def test_an_in_flight_run_with_no_record_is_followed_from_the_database(
    db, redis_store, triggered_by,
):
    run = _run(db, triggered_by)

    service = _service(db, redis_store)
    service._spawn_poll = MagicMock()
    service._recover_followed_executions()

    # The poll budget comes from the schedule, as it did at the fire.
    service._spawn_poll.assert_called_once_with(run.id, 900, AGENT)


async def test_a_queued_run_counts_as_in_flight(db, redis_store):
    run = _run(db)
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE schedule_executions SET status = ? WHERE id = ?",
            (ExecutionStatus.QUEUED, run.id),
        )
        conn.commit()

    assert [r.id for r in db.get_inflight_scheduled_executions()] == [run.id]


@pytest.mark.parametrize("triggered_by", ["manual", "webhook", "validation", "reminder"])
async def test_the_fallback_takes_only_runs_a_cron_fire_or_a_retry_created(
    db, redis_store, triggered_by,
):
    """Nothing says the scheduler was following these, and a validation run
    followed here would gain a retry it never had."""
    _run(db, triggered_by)

    service = _service(db, redis_store)
    service._recover_followed_executions()

    assert not service._active_poll_tasks


async def test_the_fallback_skips_runs_that_already_ended(db, redis_store):
    run = _run(db)
    db.update_execution_status(run.id, ExecutionStatus.FAILED, error="boom")

    assert db.get_inflight_scheduled_executions() == []
