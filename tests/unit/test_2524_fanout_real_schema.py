"""#2524 — the fan-out batch SQL against the real schema, not the `_DB` double.

`test_2524_fanout_async_join.py` drives the service against an in-memory row
store; this file proves the three pieces of SQL that store stands in for exist
and behave on the production schema (SQLite always; PostgreSQL when
TEST_POSTGRES_URL is set):

  1. `fan_out_task_id` round-trips through `create_task_execution` and comes
     back from `get_fan_out_executions`, which is scoped by agent;
  2. `count_fan_out_open` counts exactly `{queued, running, pending_retry}`;
  3. why a subtask row is created at slot grant and never up front: a
     just-created row is precisely the shape the #106 no-session sweep FAILs
     once it is 60s old.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend, run as _hrun, scalar as _scalar  # noqa: E402,F401

pytestmark = pytest.mark.unit

_EVICT = ("db.connection", "db.schedules", "database")


@pytest.fixture
def ops(db_backend, monkeypatch):
    # Evict cached db modules so production code re-resolves against the
    # harness engine; monkeypatch restores them afterwards.
    for name in _EVICT:
        monkeypatch.delitem(sys.modules, name, raising=False)
    from unittest.mock import MagicMock

    from db.schedules import ScheduleOperations

    return ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())


def _set_status(eid: str, status: str) -> None:
    _hrun("UPDATE schedule_executions SET status=:s WHERE id=:i", s=status, i=eid)


def test_fan_out_task_id_round_trips_and_the_read_is_agent_scoped(ops):
    a = ops.create_task_execution(
        agent_name="scout", message="m1", triggered_by="fan_out",
        fan_out_id="fo_real", fan_out_task_id="alpha", subscription_id="sub_1",
    )
    ops.create_task_execution(
        agent_name="other", message="m2", triggered_by="fan_out",
        fan_out_id="fo_real", fan_out_task_id="beta",
    )

    rows = ops.get_fan_out_executions("scout", "fo_real")
    assert [(r["id"], r["fan_out_task_id"]) for r in rows] == [(a.id, "alpha")]
    assert _scalar(
        "SELECT subscription_id FROM schedule_executions WHERE id=:i", i=a.id
    ) == "sub_1"


def test_count_fan_out_open_counts_only_non_terminal_rows(ops):
    ids = [
        ops.create_task_execution(
            agent_name="scout", message=f"m{i}", triggered_by="fan_out",
            fan_out_id="fo_count", fan_out_task_id=f"t{i}",
        ).id
        for i in range(6)
    ]
    for eid, status in zip(ids, (
        "queued", "running", "pending_retry", "success", "failed", "cancelled",
    )):
        _set_status(eid, status)

    assert ops.count_fan_out_open("fo_count") == 3
    assert ops.count_fan_out_open("fo_nothing") == 0


def test_a_row_left_waiting_for_a_slot_would_be_swept(ops):
    """The #2524 review's reproduction, kept as the reason the service creates
    each row inside the semaphore. `create_task_execution` writes RUNNING with
    no session; nothing distinguishes a row waiting behind the fan-out semaphore
    from a silent launch failure, so after 60s the sweep FAILs it."""
    waiting = ops.create_task_execution(
        agent_name="scout", message="tail", triggered_by="fan_out",
        fan_out_id="fo_tail", fan_out_task_id="t9",
    )
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    _hrun("UPDATE schedule_executions SET started_at=:t WHERE id=:i", t=old, i=waiting.id)

    assert ops.mark_no_session_executions_failed(60) == 1
    assert _scalar(
        "SELECT status FROM schedule_executions WHERE id=:i", i=waiting.id
    ) == "failed"
