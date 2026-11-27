"""#4813 — the scheduler publishes nothing on Redis.

The standalone scheduler published `schedule_execution_started`,
`schedule_execution_completed` and `schedule_execution_skipped` on a Redis
pub/sub channel that never had a subscriber, so the events reached neither
`/ws` nor `/ws/events`. The publisher, its nine call sites and the
`PUBLISH_EVENTS` setting are removed.

What a listener uses instead: a scheduled run is dispatched through
`POST /api/internal/execute-task`, and the backend broadcasts its dispatch
activity as `agent_activity` (`activity_type` "chat_start",
`details.triggered_by` "schedule") when it starts and again when it ends. A
skipped run has no live signal: it is a `skipped` row, read through the
executions API.

If a live signal for skipped runs is ever wanted, it belongs on the backend's
event bus (Invariant #10), reached through an internal route as every other
scheduler notice is. It does not belong on a second channel from the scheduler.

Layers:
  * behaviour: every path that used to publish runs on a real
    `SchedulerService` whose Redis client records its calls;
  * a scan of `src/scheduler/` for a send, so a path added later is covered;
  * the setting: no config field, no compose variable.
"""
from __future__ import annotations

import ast
import asyncio
import dataclasses
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO = Path(__file__).resolve().parents[2]

# `src.scheduler` imports as a package, so the repo root must be importable.
# Appended, never inserted at 0, so it cannot shadow the conftest-managed
# `src/backend` entries (mirrors test_1969).
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))

# `src/scheduler/config.py` reads these at import time (#589).
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

pytestmark = pytest.mark.unit

AGENT = "brief-agent"
# A Redis call that sends something to a reader: pub/sub, or the event stream
# the backend owns. The scheduler may keep state in Redis; it may not send.
_SENDS = {"publish", "xadd"}
_CHANNEL = "scheduler:events"


def _schedule(**over):
    fields = dict(
        id="s1", name="Morning brief", agent_name=AGENT, enabled=True, message="brief",
        cron_expression="0 7 * * *", timezone="UTC", model=None, timeout_seconds=600,
        allowed_tools=None, deliver_to_workspace_email=None,
    )
    fields.update(over)
    return SimpleNamespace(**fields)


def _service(schedule=None):
    """A real service on mocked collaborators. Every value an event used to
    carry is a plain string, so the old publisher would have serialised it."""
    import src.scheduler.service as ssvc

    svc = object.__new__(ssvc.SchedulerService)
    svc._redis = MagicMock(name="redis")  # what the `redis` property hands out
    svc._active_poll_tasks = set()
    svc.db = MagicMock()
    svc.db.get_schedule.return_value = schedule or _schedule()
    svc.db.create_skipped_execution.return_value = SimpleNamespace(id="skipped-1")
    svc.db.get_execution.return_value = SimpleNamespace(
        id="exec-1", schedule_id="s1", status="success", triggered_by="schedule",
        source_user_id=None, source_user_email=None, source_agent_name=None,
        source_mcp_key_id=None, source_mcp_key_name=None, chain_depth=None,
    )
    svc.db.get_autonomy_enabled.return_value = True
    svc.db.sync_freeze_reason.return_value = None
    svc._report_retry_dropped = AsyncMock()
    svc._maybe_trigger_validation = AsyncMock()
    svc._maybe_schedule_retry = AsyncMock()
    return svc


def _sent(svc):
    return [call for call in svc._redis.method_calls if call[0] in _SENDS]


def _assert_nothing_sent(svc):
    assert _sent(svc) == [], f"the scheduler sent on Redis: {_sent(svc)}"


def _run(coro):
    async def settle():
        result = await coro
        await asyncio.sleep(0)  # let a spawned task run
        return result

    return asyncio.run(settle())


def test_the_recording_client_is_the_one_the_service_uses():
    """Stops the tests below passing for the wrong reason: a send through the
    service's own `redis` property is seen."""
    svc = _service()
    svc.redis.publish("any", "payload")
    assert [call[0] for call in _sent(svc)] == ["publish"]


class TestNoPathSends:
    def test_a_run_that_starts(self):
        svc = _service()
        svc._dispatch_and_record_outcome = AsyncMock()
        execution = SimpleNamespace(id="exec-1")

        _run(svc._execute_schedule_with_lock("s1", triggered_by="manual", execution=execution))

        svc._dispatch_and_record_outcome.assert_awaited_once()
        _assert_nothing_sent(svc)

    @pytest.mark.parametrize(
        "result",
        [{"status": "success"}, {"status": "failed", "error": "boom"}],
        ids=["success", "failed"],
    )
    def test_a_run_that_ends_inline(self, result):
        svc = _service()
        svc._call_backend_execute_task = AsyncMock(return_value=result)

        _run(svc._dispatch_and_record_outcome(
            _schedule(), SimpleNamespace(id="exec-1"), "brief", "schedule",
        ))

        svc._call_backend_execute_task.assert_awaited_once()
        _assert_nothing_sent(svc)

    def test_a_dispatch_that_raises(self):
        svc = _service()
        svc._call_backend_execute_task = AsyncMock(side_effect=Exception("dispatch timed out"))

        _run(svc._dispatch_and_record_outcome(
            _schedule(), SimpleNamespace(id="exec-1"), "brief", "schedule",
        ))

        svc.db.get_execution.assert_called_with("exec-1")
        _assert_nothing_sent(svc)

    @pytest.mark.parametrize(
        "result, follow_up",
        [
            ({"status": "success"}, "_maybe_trigger_validation"),
            ({"status": "failed", "error": "boom"}, "_maybe_schedule_retry"),
        ],
        ids=["success", "failed"],
    )
    def test_a_run_the_background_poll_finishes(self, result, follow_up):
        svc = _service()
        svc._poll_execution_completion = AsyncMock(return_value=result)

        _run(svc._poll_and_finalize("exec-1", 600, AGENT))

        getattr(svc, follow_up).assert_awaited_once()
        _assert_nothing_sent(svc)

    def test_a_tick_skipped_because_the_last_run_is_still_going(self):
        svc = _service()

        async def go():
            svc._record_skipped_agent_schedule("s1")

        _run(go())

        assert svc.db.create_skipped_execution.call_args.kwargs["skip_reason"]
        _assert_nothing_sent(svc)

    def test_a_tick_the_agents_pre_check_declines(self):
        svc = _service()
        svc._run_pre_check = AsyncMock(return_value={"fire": False, "reason": "nothing new"})

        should_fire, _ = _run(svc._apply_pre_check_gate(_schedule(), "schedule"))

        assert should_fire is False
        assert svc.db.create_skipped_execution.call_args.kwargs["skip_reason"] == (
            "pre-check: nothing new"
        )
        svc.db.update_schedule_run_times.assert_called_once()
        _assert_nothing_sent(svc)

    def test_a_brief_the_readiness_gate_holds(self):
        svc = _service()
        svc._run_readiness_check = AsyncMock(
            return_value={"fire": False, "reason": "Held: runs once its owner marks it ready"}
        )
        schedule = _schedule(deliver_to_workspace_email="client@example.com")

        assert _run(svc._apply_readiness_gate(schedule, "schedule")) is False

        assert svc.db.create_skipped_execution.call_args.kwargs["skip_reason"] == (
            "Held: runs once its owner marks it ready"
        )
        _assert_nothing_sent(svc)

    def test_a_retry_a_sync_freeze_stops(self):
        svc = _service()
        svc.db.sync_freeze_reason.return_value = "push denied"

        _run(svc._execute_retry(
            original_execution_id="orig", failed_execution_id="failed", schedule_id="s1",
            agent_name=AGENT, message="brief", timeout_seconds=None, model=None,
            allowed_tools=None, next_attempt_number=2,
        ))

        assert svc.db.create_skipped_execution.call_args.kwargs["skip_reason"] == (
            "Git sync frozen: push denied"
        )
        svc._report_retry_dropped.assert_awaited_once_with("failed")
        _assert_nothing_sent(svc)


def test_the_service_has_no_publisher():
    import src.scheduler.service as ssvc

    assert not hasattr(ssvc.SchedulerService, "_publish_event")


def test_no_scheduler_module_sends_on_redis_or_names_the_channel():
    """Covers a path the behaviour tests do not drive, and one added later.

    The reader of this text is the scheduler image: every module here ships in
    it. String constants include docstrings, so the channel is not named there
    either.
    """
    modules = sorted((_REPO / "src" / "scheduler").rglob("*.py"))
    assert modules, "no scheduler modules found — the scan would pass on nothing"
    offenders = []
    for path in modules:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _SENDS
            ):
                offenders.append(f"{path.name}:{node.lineno} calls .{node.func.attr}()")
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _CHANNEL in node.value
            ):
                offenders.append(f"{path.name}:{node.lineno} names {_CHANNEL}")
    assert not offenders, (
        "the scheduler sends events again, and nothing reads them (#4813). A "
        "scheduled run's start and end are already live as the backend's "
        "agent_activity; a new live signal goes through an internal backend "
        f"route and the event bus, not from here: {offenders}"
    )


def test_the_setting_is_gone():
    from src.scheduler.config import SchedulerConfig

    assert "publish_events" not in {f.name for f in dataclasses.fields(SchedulerConfig)}


def test_no_compose_file_sets_the_variable():
    files = sorted(_REPO.glob("docker-compose*.yml")) + sorted(
        (_REPO / "docker").rglob("docker-compose*.yml")
    )
    assert len(files) >= 4, f"expected the compose files, found {files}"
    hits = [
        f"{path.relative_to(_REPO)}:{number}"
        for path in files
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "PUBLISH_EVENTS" in line
    ]
    assert not hits, f"PUBLISH_EVENTS is read by nothing (#4813): {hits}"
