"""#1969 — a cron tick suppressed by the distributed lock must leave a record.

Suppression itself is correct: two concurrent runs of one schedule is the thing
the lock exists to prevent. What was missing is the *evidence*. The lock-denial
branch of `_execute_schedule()` logged at INFO and returned bare, so no
`schedule_executions` row was written and the suppressed tick was
indistinguishable from a tick that never fired — in the execution history, the
UI, and monitoring alike. APScheduler still reported the job successful.

Trinity already audits the *other* suppression path. Two exist and they do not
overlap:

  * APScheduler refuses the job (`max_instances=1`) → `EVENT_JOB_MAX_INSTANCES`
    → `_on_job_max_instances` → a `status='skipped'` row. The job never starts,
    so `_execute_schedule` is never entered and no lock is attempted.
  * The Redis lock denies the run → this branch. Reaching it means APScheduler
    already let the job start, so no max-instances event fires.

Exactly one of the two per tick — which is why this can be a missing call
rather than a design change, and why it cannot reintroduce the duplicate
`skipped` + `success` pairing of #91.

The gap bit hardest on the common case: a MANUAL trigger bypasses APScheduler
entirely (`_trigger_handler` dispatches via `asyncio.create_task`), so its
instance counter stays at zero, the cron job starts normally, and the collision
lands one layer down — in precisely the unaudited branch.

`src/scheduler` is a standalone package that cannot import the backend, so the
service is driven directly with an injected fake DB and lock manager: the
assertion is about which calls the branch makes, and a real Redis/SQLite pair
would only add ways for the test to be flaky about it.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

# The `src.scheduler` namespace import resolves only with the repo root on
# sys.path — true for a repo-root `pytest` run but NOT in CI, whose rootdir is
# `tests/`. Appended (never inserted at 0) so the repo root cannot shadow the
# conftest-managed `src/backend` entries. Mirrors test_1808.
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))

# `src/scheduler/config.py` reads these at import time (#589 made the Redis
# credentials mandatory), so they must exist before the package is imported.
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")


def _service_module():
    import src.scheduler.service as scheduler_service

    return scheduler_service


class _FakeLock:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


class _FakeLockManager:
    """Hands out a lock, or denies it, and remembers what was asked."""

    def __init__(self, *, grant: bool):
        self._grant = grant
        self.acquire_calls: list[str] = []
        self.lock = _FakeLock() if grant else None

    def try_acquire_schedule_lock(self, schedule_id: str):
        self.acquire_calls.append(schedule_id)
        return self.lock


class _Recorder:
    """Captures `_record_skipped_agent_schedule` calls."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, schedule_id, skip_reason=None, event_reason=None):
        self.calls.append(
            {
                "schedule_id": schedule_id,
                "skip_reason": skip_reason,
                "event_reason": event_reason,
            }
        )


def _service(*, grant_lock: bool):
    """A SchedulerService with the two collaborators stubbed.

    `SchedulerService.__init__` takes both by injection, so nothing here
    reaches Redis or SQLite.
    """
    service = _service_module().SchedulerService(
        database=object(),
        lock_manager=_FakeLockManager(grant=grant_lock),
        redis_url="redis://test:test@redis:6379",
    )
    return service


# ---------------------------------------------------------------------------
# The defect: a denied tick recorded nothing.
# ---------------------------------------------------------------------------


def test_lock_denied_records_a_skipped_execution():
    """The bug, directly: the branch returned without writing anything."""
    service = _service(grant_lock=False)
    recorder = _Recorder()
    service._record_skipped_agent_schedule = recorder

    asyncio.run(service._execute_schedule("sch-1"))

    assert len(recorder.calls) == 1, (
        "a lock-denied tick left no audit record — it is indistinguishable "
        "from a tick that never fired (#1969)"
    )
    assert recorder.calls[0]["schedule_id"] == "sch-1"


def test_recorded_reason_names_the_lock_not_max_instances():
    """The two suppression paths must be tellable apart in the record.

    Reusing the default wording would file a lock collision as a
    max_instances refusal, which sends anyone debugging it to the wrong
    mechanism — the parameters exist (#1808) precisely so callers say which
    one they are.
    """
    service = _service(grant_lock=False)
    recorder = _Recorder()
    service._record_skipped_agent_schedule = recorder

    asyncio.run(service._execute_schedule("sch-1"))

    skip_reason = recorder.calls[0]["skip_reason"]
    assert skip_reason, "the skip reason must be explicit, not the default"
    assert "lock" in skip_reason.lower()
    assert "max_instances" not in skip_reason
    assert recorder.calls[0]["event_reason"]


def test_denied_tick_does_not_run_the_schedule():
    """Auditing the suppression must not undo it. The row is a record that the
    run did NOT happen; actually running it would be the concurrent execution
    the lock exists to prevent."""
    service = _service(grant_lock=False)
    service._record_skipped_agent_schedule = _Recorder()

    ran = []

    async def _should_not_run(*args, **kwargs):
        ran.append(args)

    service._execute_schedule_with_lock = _should_not_run

    asyncio.run(service._execute_schedule("sch-1"))

    assert ran == [], "a lock-denied tick executed the schedule anyway"


# ---------------------------------------------------------------------------
# The half that must NOT change.
# ---------------------------------------------------------------------------


def test_granted_lock_records_nothing_and_runs():
    """The happy path must stay silent. A `skipped` row on every successful
    tick would be worse than the missing row this fixes: it inverts the
    meaning of the status for every consumer of the history."""
    service = _service(grant_lock=True)
    recorder = _Recorder()
    service._record_skipped_agent_schedule = recorder

    ran = []

    async def _run(schedule_id, *args, **kwargs):
        ran.append(schedule_id)

    service._execute_schedule_with_lock = _run

    asyncio.run(service._execute_schedule("sch-1"))

    assert ran == ["sch-1"]
    assert recorder.calls == [], "a successful tick was recorded as skipped"


def test_lock_is_released_after_a_successful_run():
    """Guard the pre-existing `finally` — the new branch sits directly above
    it, and a lock leaked here would wedge the schedule until the TTL."""
    service = _service(grant_lock=True)
    service._record_skipped_agent_schedule = _Recorder()

    async def _run(*args, **kwargs):
        return None

    service._execute_schedule_with_lock = _run

    asyncio.run(service._execute_schedule("sch-1"))

    assert service.lock_manager.lock.released is True


def test_lock_is_released_when_the_run_raises():
    """Same guard on the failure path."""
    service = _service(grant_lock=True)
    service._record_skipped_agent_schedule = _Recorder()

    async def _boom(*args, **kwargs):
        raise RuntimeError("agent exploded")

    service._execute_schedule_with_lock = _boom

    with pytest.raises(RuntimeError):
        asyncio.run(service._execute_schedule("sch-1"))

    assert service.lock_manager.lock.released is True


def test_no_lock_is_released_when_none_was_acquired():
    """A denial hands back no lock object; the branch must return before the
    `finally`, not call `.release()` on None."""
    service = _service(grant_lock=False)
    service._record_skipped_agent_schedule = _Recorder()

    asyncio.run(service._execute_schedule("sch-1"))  # must not raise

    assert service.lock_manager.lock is None


# ---------------------------------------------------------------------------
# #91 regression shape: exactly one row per suppressed tick.
# ---------------------------------------------------------------------------


def test_one_denied_tick_records_exactly_one_row():
    """Not two. #91 was a duplicate skipped+success pairing for a single
    trigger; the fix must not re-create that shape."""
    service = _service(grant_lock=False)
    recorder = _Recorder()
    service._record_skipped_agent_schedule = recorder

    asyncio.run(service._execute_schedule("sch-1"))

    assert len(recorder.calls) == 1


def test_each_denied_tick_records_once_independently():
    """Three denials → three rows, one per suppressed tick. The record is
    per-occurrence, matching the cardinality the max_instances path already
    produces."""
    service = _service(grant_lock=False)
    recorder = _Recorder()
    service._record_skipped_agent_schedule = recorder

    for _ in range(3):
        asyncio.run(service._execute_schedule("sch-1"))

    assert len(recorder.calls) == 3


def test_max_instances_path_does_not_go_through_execute_schedule():
    """The two paths must stay mutually exclusive.

    This is what makes "call the helper from both" safe. If APScheduler's
    refusal ever routed through `_execute_schedule`, a single tick would be
    recorded twice — once by the event listener and once by the lock branch.
    Pinned at the source, since the two entry points are wired to APScheduler
    rather than to each other.
    """
    source = (_REPO / "src" / "scheduler" / "service.py").read_text(encoding="utf-8")
    start = source.index("def _on_job_max_instances(")
    handler = source[start : source.index("def _record_skipped_agent_schedule(")]
    assert "_execute_schedule(" not in handler, (
        "the max_instances listener now reaches _execute_schedule — one tick "
        "would be audited twice (#91 shape, #1969)"
    )


def test_denied_tick_lands_a_real_skipped_row(tmp_path):
    """End-to-end against a real SQLite DB, not the `_Recorder` stub.

    Every assertion above proves the branch *calls* the helper. This one
    proves a row an operator can actually see turns up — the thing the issue
    reports missing. A wiring test alone would still pass if the write path
    were broken downstream.
    """
    import sqlite3

    import src.scheduler.database as scheduler_database

    db_path = tmp_path / "t.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE agent_schedules (
            id TEXT PRIMARY KEY, agent_name TEXT, name TEXT, cron_expression TEXT,
            message TEXT, enabled INTEGER, timezone TEXT, description TEXT,
            owner_id INTEGER, created_at TEXT, updated_at TEXT,
            last_run_at TEXT, next_run_at TEXT, deleted_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE schedule_executions (
            id TEXT PRIMARY KEY, schedule_id TEXT, agent_name TEXT, status TEXT,
            started_at TEXT, completed_at TEXT, duration_ms INTEGER, message TEXT,
            response TEXT, error TEXT, triggered_by TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO agent_schedules VALUES ('sch-1','a1','nightly','0 3 * * *',"
        "'do the thing',1,'UTC',NULL,1,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',"
        "NULL,NULL,NULL)"
    )
    conn.commit()
    conn.close()

    service = _service_module().SchedulerService(
        database=scheduler_database.SchedulerDatabase(str(db_path)),
        lock_manager=_FakeLockManager(grant=False),
        redis_url="redis://test:test@redis:6379",
    )

    asyncio.run(service._execute_schedule("sch-1"))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM schedule_executions")]
    conn.close()

    assert len(rows) == 1, "the suppressed tick produced no visible row"
    row = rows[0]
    assert row["status"] == "skipped"
    assert row["agent_name"] == "a1"
    assert row["triggered_by"] == "schedule"
    # The reason rides in `error`, which is where the max_instances path puts
    # it too — one shape for both suppression causes.
    assert "lock" in (row["error"] or "").lower()


def test_audit_helper_is_still_failure_isolated():
    """The new caller runs on the cron path, so a raise from the audit write
    would take down tick handling itself. The helper swallows its own errors —
    pin that, because the guarantee is now load-bearing for a second caller.
    """
    source = (_REPO / "src" / "scheduler" / "service.py").read_text(encoding="utf-8")
    start = source.index("def _record_skipped_agent_schedule(")
    helper = source[start : source.index("def _record_skipped_process_schedule(")]
    assert "try:" in helper and "except Exception" in helper, (
        "_record_skipped_agent_schedule no longer isolates its failures, and "
        "it is now called from the cron execution path (#1969)"
    )
