"""#2845 — `retry` must be classified in every trigger set, like the run it retries.

RETRY-001 re-dispatches a failed scheduled run as a NEW row with
`triggered_by="retry"`. That value was in none of the hand-kept trigger sets, so:

* on a pull pilot, the first attempt was claimed from the durable queue and the
  retry was PUSHED — one logical task running its second attempt on the path
  with no lease and no reaper recovery;
* an unresolved slash command inside a retry raised no operator alert, although
  nobody reads a retry's reply any more than a cron run's;
* `?triggered_by=retry` on the executions list degraded to "no filter" and
  returned every execution; the analytics chart put retries under `Other`.

The last test is the class guard: every `triggered_by=` literal the scheduler
emits must be classified in all four sets, so the next scheduler-produced
trigger cannot ship unclassified the way `retry` did.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

pytestmark = pytest.mark.unit

PILOT = "pilot-a"
NON_PILOT = "bob"


def _await(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# 1. Membership + pull ownership
# ---------------------------------------------------------------------------


def test_retry_is_autonomous_and_pull_reachable():
    from services.pull_pilot import PULL_REACHABLE_TRIGGERS
    from services.task_execution_service import _AUTONOMOUS_TRIGGERS

    assert "retry" in _AUTONOMOUS_TRIGGERS
    assert "retry" in PULL_REACHABLE_TRIGGERS


def test_a_pilot_owns_its_retries_like_its_scheduled_runs(monkeypatch):
    """The straddle the issue measured on eu2: `schedule` PULL, `retry` PUSH."""
    from services.pull_pilot import pull_owns_dispatch

    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", PILOT)
    assert pull_owns_dispatch(PILOT, "schedule") is True
    assert pull_owns_dispatch(PILOT, "retry") is True
    assert pull_owns_dispatch(NON_PILOT, "retry") is False


# ---------------------------------------------------------------------------
# 2. Unresolved command in a retry alerts like the scheduled run would
# ---------------------------------------------------------------------------


def _run_unresolved(triggered_by: str):
    """Drive the real `execute_task` on a NON-pilot (the push path — the only
    path that detects an unresolved command) with a `/foo` message the runtime
    answers with "Unknown command: /foo". Returns the alert mock."""
    import config
    from services.task_execution_service import TaskExecutionService

    mock_db = MagicMock()
    mock_db.get_max_parallel_tasks.return_value = 3
    mock_db.get_execution_timeout.return_value = 300
    mock_db.get_execution.return_value = MagicMock(status="cancelled")
    mock_db.update_execution_status.return_value = True

    mock_capacity = MagicMock()
    mock_capacity.acquire = AsyncMock(return_value=MagicMock(state="admitted"))
    mock_capacity.release = AsyncMock()
    mock_circuit = MagicMock()
    mock_circuit.allow_request.return_value = True
    mock_activity = MagicMock(
        track_activity=AsyncMock(return_value="act-1"),
        complete_activity=AsyncMock(),
    )
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "response": "Unknown command: /foo",
        "session_id": "s1",
        "metadata": {"cost_usd": 0.0, "context_window": 200000},
        "execution_log": [],
    }
    alert = MagicMock(return_value=None)

    with (
        patch.object(config, "DISPATCH_ASYNC", False),
        patch("services.task_execution_service.db", mock_db),
        patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
        patch("services.task_execution_service.activity_service", mock_activity),
        patch("services.task_execution_service.CircuitState", return_value=mock_circuit),
        patch("services.task_execution_service.agent_post_with_retry", AsyncMock(return_value=resp)),
        patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
        patch("services.task_execution_service._record_dispatch_terminal", AsyncMock()),
        patch("services.task_execution_service._alert_skill_not_found", alert),
        patch("services.task_execution_service._spawn_bg", MagicMock()),
    ):
        _await(
            TaskExecutionService().execute_task(
                agent_name=NON_PILOT,
                message="/foo",
                triggered_by=triggered_by,
                execution_id=f"exec-{triggered_by}",
                timeout_seconds=300,
            )
        )
    return alert


@pytest.mark.parametrize("trigger", ["schedule", "retry"])
def test_unresolved_command_alerts_on_the_first_attempt_and_on_its_retry(trigger):
    alert = _run_unresolved(trigger)
    alert.assert_called_once()
    assert alert.call_args.args[1] == "/foo"
    assert alert.call_args.args[3] == trigger


def test_an_interactive_trigger_still_does_not_alert():
    """The human is reading the "Unknown command" reply as it comes back."""
    _run_unresolved("manual").assert_not_called()


# ---------------------------------------------------------------------------
# 3. Class guard — every trigger the scheduler emits is classified everywhere
# ---------------------------------------------------------------------------


def _scheduler_emitted_triggers() -> set:
    """`triggered_by="<literal>"` keyword arguments across `src/scheduler`.

    Read from the AST, so a comment or docstring naming a trigger cannot satisfy
    it. `manual`/`webhook` reach the scheduler through a variable in `main.py`
    and are classified by their own producers, not here.
    """
    found = set()
    for path in (_REPO / "src" / "scheduler").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "triggered_by"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                found.add(node.value.value)
    return found


def test_every_scheduler_emitted_trigger_is_classified_in_all_four_sets():
    from db.schedules.analytics import _TRIGGER_BUCKETS
    from routers.executions import _VALID_TRIGGERS
    from services.pull_pilot import PULL_REACHABLE_TRIGGERS
    from services.task_execution_service import _AUTONOMOUS_TRIGGERS

    emitted = _scheduler_emitted_triggers()
    # Never pass on an empty scan — a moved file must fail loudly, not vacuously.
    assert "retry" in emitted and "schedule" in emitted, emitted

    for name, allowed in (
        ("_AUTONOMOUS_TRIGGERS", _AUTONOMOUS_TRIGGERS),
        ("PULL_REACHABLE_TRIGGERS", PULL_REACHABLE_TRIGGERS),
        ("_VALID_TRIGGERS", _VALID_TRIGGERS),
        ("_TRIGGER_BUCKETS", set(_TRIGGER_BUCKETS)),
    ):
        missing = emitted - set(allowed)
        assert not missing, f"scheduler emits {sorted(missing)} but {name} omits it"


def test_a_retry_is_bucketed_with_the_scheduled_run_it_retries():
    from db.schedules.analytics import _bucket_for_trigger

    assert _bucket_for_trigger("retry") == _bucket_for_trigger("schedule") == "Scheduled"
