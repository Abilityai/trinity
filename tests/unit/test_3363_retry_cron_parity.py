"""#3363 — a scheduler retry runs with the same gates and delivery as the cron fire.

RETRY-001 re-dispatches a failed scheduled run as a new `retry` row. Before:

* `_execute_retry` never forwarded `deliver_to_workspace_email`, so when a
  Workspace-delivered schedule's first attempt failed, a successful retry's
  answer was never delivered to the client;
* it checked only `schedule.enabled`, so a pending retry still fired after the
  operator turned the agent's autonomy off or a git-sync freeze engaged;
* the client Workspace ledger showed a retry as kind `other` (attempt 1 shows
  `schedule`), and the first-autonomous-success milestone ignored a run that
  only succeeded on retry.

The gates apply to the retry of a cron fire, exactly as they apply to the cron
fire itself; a retry of an operator's manual run keeps firing, as the manual
run did. An unreadable original is gated (it is not known to be an operator's).
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
# `src.scheduler` imports as a package (relative imports), so the repo root
# must be importable too — appended, so `database` still resolves to the backend.
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))

pytestmark = pytest.mark.unit

AGENT = "brief-agent"
SEAT = "client@example.com"


def _service(*, original_trigger="schedule", autonomy=True, freeze=None, deliver=SEAT,
             original_raises=False):
    import src.scheduler.service as ssvc

    svc = object.__new__(ssvc.SchedulerService)
    svc.db = MagicMock()
    svc.db.get_schedule.return_value = SimpleNamespace(
        id="s1", name="Morning brief", agent_name=AGENT, enabled=True, message="brief",
        deliver_to_workspace_email=deliver, cron_expression="0 7 * * *", timezone="UTC",
    )
    if original_raises:
        svc.db.get_execution.side_effect = RuntimeError("db down")
    else:
        svc.db.get_execution.return_value = SimpleNamespace(
            triggered_by=original_trigger, source_user_id=None, source_user_email=None,
            source_agent_name=None, source_mcp_key_id=None, source_mcp_key_name=None,
            chain_depth=None,
        )
    svc.db.get_autonomy_enabled.return_value = autonomy
    svc.db.sync_freeze_reason.return_value = freeze
    svc.db.create_execution.return_value = SimpleNamespace(id="retry-1")
    svc.db.create_skipped_execution.return_value = SimpleNamespace(id="skipped-1")
    svc._call_backend_execute_task = AsyncMock(return_value={"status": "dispatched"})
    svc._publish_event = AsyncMock()
    return svc


def _retry(svc):
    asyncio.run(svc._execute_retry(
        original_execution_id="orig", failed_execution_id="failed", schedule_id="s1",
        agent_name=AGENT, message="brief", timeout_seconds=None, model=None,
        allowed_tools=None, next_attempt_number=2,
    ))


class TestDelivery:
    def test_a_retry_delivers_to_the_seat_the_schedule_names(self):
        svc = _service()
        _retry(svc)
        kwargs = svc._call_backend_execute_task.call_args.kwargs
        assert kwargs["deliver_to_workspace_email"] == SEAT
        assert kwargs["execution_id"] == "retry-1"

    def test_a_schedule_without_a_seat_sends_none(self):
        svc = _service(deliver=None)
        _retry(svc)
        assert svc._call_backend_execute_task.call_args.kwargs["deliver_to_workspace_email"] is None


class TestAutonomyGate:
    def test_a_retry_of_a_cron_fire_does_not_run_once_autonomy_is_off(self):
        svc = _service(autonomy=False)
        _retry(svc)
        svc.db.create_execution.assert_not_called()
        svc.db.create_skipped_execution.assert_not_called()  # no row, as on cron
        svc._call_backend_execute_task.assert_not_called()

    def test_a_retry_of_a_manual_run_still_runs(self):
        svc = _service(original_trigger="manual", autonomy=False)
        _retry(svc)
        svc._call_backend_execute_task.assert_called_once()

    def test_an_unreadable_original_is_gated(self):
        svc = _service(original_raises=True, autonomy=False)
        _retry(svc)
        svc._call_backend_execute_task.assert_not_called()


class TestSyncFreezeGate:
    def test_a_retry_of_a_cron_fire_is_skipped_with_the_reason_while_frozen(self):
        svc = _service(freeze="git sync failing")
        _retry(svc)
        svc.db.create_execution.assert_not_called()
        svc._call_backend_execute_task.assert_not_called()
        kwargs = svc.db.create_skipped_execution.call_args.kwargs
        assert kwargs["triggered_by"] == "retry"
        assert "git sync failing" in kwargs["skip_reason"]

    def test_a_retry_of_a_manual_run_still_runs_while_frozen(self):
        svc = _service(original_trigger="manual", freeze="git sync failing")
        _retry(svc)
        svc._call_backend_execute_task.assert_called_once()
        svc.db.create_skipped_execution.assert_not_called()

    def test_a_gated_retry_never_moves_the_cron_projection(self):
        # A retry does not consume a cron window; only the fire does.
        for svc in (_service(autonomy=False), _service(freeze="frozen")):
            _retry(svc)
            svc.db.update_schedule_run_times.assert_not_called()


class TestDisplaySites:
    def test_the_workspace_ledger_shows_a_retry_as_a_schedule(self):
        from client_portal.work.service import work_kind

        assert work_kind({"triggered_by": "retry"}) == work_kind({"triggered_by": "schedule"})

    def test_a_run_that_only_succeeded_on_retry_is_an_autonomous_success(self, tmp_path, monkeypatch):
        db_file = tmp_path / "trinity-3363.db"
        monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
        import db.connection as conn_mod
        monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
        from db.schema import init_schema
        raw = sqlite3.connect(db_file)
        init_schema(raw.cursor(), raw)
        raw.commit()
        raw.close()
        from database import db

        from db.write_params import ExecutionResult

        # The only success on this fresh install is a retry's.
        assert db.first_autonomous_success_at() is None
        row = db.create_task_execution(
            agent_name=f"agent-{uuid.uuid4().hex[:8]}", message="m", triggered_by="retry")
        db.update_execution_status(row.id, "success", ExecutionResult(response="ok"))
        assert db.first_autonomous_success_at() is not None
