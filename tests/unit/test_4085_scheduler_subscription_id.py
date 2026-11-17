"""#4085 — scheduler-created executions record the agent's subscription.

SUB-004 snapshots the agent's assigned subscription onto
`schedule_executions.subscription_id` at INSERT, and the usage windows
(`get_subscription_usage` → `window_5h` / `window_7d`) aggregate by that
column. The backend's inserts did it; the scheduler's own `create_execution`
(cron ticks, retries, one-shot reminders) and `create_skipped_execution` never
adopted the column. The #3470 walk re-points a row only when a DIFFERENT
credential served it, so a cron turn served by the agent's own subscription
stayed NULL, and a cron-driven subscription read as almost idle.

Run against the real backend schema (`db_backend`: SQLite, and PostgreSQL in
the schema-parity tier, where the scheduler follows `DATABASE_URL` too).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db_harness import db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit

AGENT = "cron-agent"
SUB = "sub-4085"


def _sql(statement, params=None):
    from sqlalchemy import text
    from db.engine import get_engine

    with get_engine().begin() as conn:
        result = conn.execute(text(statement), params or {})
        return result.mappings().all() if result.returns_rows else None


def _own(agent, subscription_id, *, deleted=False):
    _sql(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at, subscription_id, deleted_at) "
        "VALUES (:a, 1, '2026-10-01T00:00:00Z', :s, :d)",
        {"a": agent, "s": subscription_id, "d": "2026-10-02T00:00:00Z" if deleted else None},
    )


def _scheduler_db():
    from src.scheduler.database import SchedulerDatabase

    return SchedulerDatabase(os.environ.get("TRINITY_DB_PATH", ""))


def _subscription_of(execution_id):
    [row] = _sql("SELECT subscription_id FROM schedule_executions WHERE id = :id",
                 {"id": execution_id})
    return row["subscription_id"]


@pytest.mark.parametrize("triggered_by", ["schedule", "reminder", "retry"])
def test_a_scheduler_execution_records_the_assigned_subscription(db_backend, triggered_by):  # noqa: F811
    _own(AGENT, SUB)

    execution = _scheduler_db().create_execution(
        schedule_id="s1", agent_name=AGENT, message="nightly", triggered_by=triggered_by)

    assert _subscription_of(execution.id) == SUB


def test_the_run_counts_toward_the_subscription_s_usage_window(db_backend):  # noqa: F811
    """The issue's symptom: a cron run missing from `window_5h` / `window_7d`."""
    from db.subscriptions import SubscriptionOperations

    _own(AGENT, SUB)
    execution = _scheduler_db().create_execution(
        schedule_id="s1", agent_name=AGENT, message="nightly", triggered_by="schedule")
    _sql("UPDATE schedule_executions SET status = 'success', cost = 0.5, context_used = 1200 "
         "WHERE id = :id", {"id": execution.id})

    usage = SubscriptionOperations.__new__(SubscriptionOperations).get_subscription_usage(SUB)

    for window in (usage.window_5h, usage.window_7d):
        assert (window.message_count, window.cost_usd, window.input_tokens) == (1, 0.5, 1200)


def test_a_skipped_execution_records_it_too(db_backend):  # noqa: F811
    _own(AGENT, SUB)

    execution = _scheduler_db().create_skipped_execution(
        schedule_id="s1", agent_name=AGENT, message="nightly", skip_reason="still running")

    assert _subscription_of(execution.id) == SUB


@pytest.mark.parametrize("owned", ["unassigned", "soft-deleted", "no-ownership-row"])
def test_no_live_assignment_records_null(db_backend, owned):  # noqa: F811
    """The backend's own predicate: a live (not soft-deleted) ownership row."""
    if owned == "unassigned":
        _own(AGENT, None)
    elif owned == "soft-deleted":
        _own(AGENT, SUB, deleted=True)

    execution = _scheduler_db().create_execution(
        schedule_id="s1", agent_name=AGENT, message="nightly")

    assert _subscription_of(execution.id) is None
