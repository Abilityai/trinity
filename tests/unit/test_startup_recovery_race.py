"""
Tests for the startup execution-recovery race (#748).

The seam: a scheduler INSERT + `POST /internal/execute-task` can land
during backend startup, before `recover_orphaned_executions` finishes.
Without coordination, recovery marks the row FAILED milliseconds before
the in-flight task handler ZADDs a capacity slot for it — leaving a
ghost slot in Redis until 1200s TTL.

Two defences shipped, both tested here:

1. `_within_startup_grace` — recovery skips rows whose `started_at` is
   younger than `STARTUP_RECOVERY_GRACE_SECONDS`. Mirrors the regular-cycle
   `WATCHDOG_MIN_AGE_SECONDS` pattern.
2. Warming-up 503 gate — `/internal/execute-task` returns 503 until
   `mark_startup_recovery_complete()` flips the flag. Scheduler retries
   on transient errors.
"""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Module preload — mirror tests/unit/test_cleanup_unreachable_orphan.py
# ---------------------------------------------------------------------------

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


@pytest.fixture(autouse=True)
def _stub_docker_and_database(monkeypatch):
    """Stub the docker package + services.docker_service before cleanup_service
    is imported. The unit-test venv doesn't ship `docker`, and
    services/__init__.py eagerly imports docker_service.
    """
    if "docker" not in sys.modules:
        monkeypatch.setitem(sys.modules, "docker", MagicMock(name="docker_stub"))
    if "services.docker_service" not in sys.modules:
        _ds_stub = types.ModuleType("services.docker_service")
        _ds_stub.get_docker_client = MagicMock(return_value=MagicMock())
        _ds_stub.get_agent_container = lambda *a, **kw: None
        _ds_stub.get_agent_status_from_container = lambda *a, **kw: "stopped"
        _ds_stub.list_all_agents = lambda *a, **kw: []
        _ds_stub.get_agent_by_name = lambda *a, **kw: None
        _ds_stub.get_next_available_port = lambda *a, **kw: 2222

        async def _exec(*a, **kw):
            return {"exit_code": 0, "output": ""}

        _ds_stub.execute_command_in_container = _exec
        monkeypatch.setitem(sys.modules, "services.docker_service", _ds_stub)
    if "database" not in sys.modules:
        monkeypatch.setitem(sys.modules, "database", MagicMock(name="database_stub"))


def _iso_now_minus(seconds: int) -> str:
    ts = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=seconds)
    return ts.isoformat().replace("+00:00", "Z")


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Grace-window helper
# ---------------------------------------------------------------------------


class TestWithinStartupGrace:
    """`_within_startup_grace` — symmetric with WATCHDOG_MIN_AGE_SECONDS."""

    def test_fresh_row_is_within_grace(self):
        from services.cleanup_service import (
            STARTUP_RECOVERY_GRACE_SECONDS,
            _within_startup_grace,
        )

        # Anything younger than the grace window is "still arriving".
        execution = {"id": "exec-1", "started_at": _iso_now_minus(1)}
        assert _within_startup_grace(execution) is True

        # And right at the boundary minus 1s is still inside.
        execution = {
            "id": "exec-2",
            "started_at": _iso_now_minus(STARTUP_RECOVERY_GRACE_SECONDS - 1),
        }
        assert _within_startup_grace(execution) is True

    def test_old_row_is_outside_grace(self):
        from services.cleanup_service import (
            STARTUP_RECOVERY_GRACE_SECONDS,
            _within_startup_grace,
        )

        execution = {
            "id": "exec-old",
            "started_at": _iso_now_minus(STARTUP_RECOVERY_GRACE_SECONDS + 5),
        }
        assert _within_startup_grace(execution) is False

    def test_missing_started_at_falls_through(self):
        """No timestamp → conservatively allow recovery (don't pretend it's fresh)."""
        from services.cleanup_service import _within_startup_grace

        assert _within_startup_grace({"id": "no-ts"}) is False
        assert _within_startup_grace({"id": "null-ts", "started_at": None}) is False

    def test_unparseable_started_at_falls_through(self):
        from services.cleanup_service import _within_startup_grace

        assert _within_startup_grace({"id": "bad", "started_at": "not-a-date"}) is False


# ---------------------------------------------------------------------------
# 2. recover_orphaned_executions — fresh rows are skipped, old rows recovered
# ---------------------------------------------------------------------------


class TestRecoverOrphanedExecutionsGraceSkip:
    """The startup recovery path must respect the grace window."""

    def _stub_capacity(self):
        cap = MagicMock(name="capacity")
        cap.release = AsyncMock(return_value=None)
        return cap

    @patch("services.cleanup_service.get_capacity_manager")
    @patch("services.cleanup_service.db")
    def test_fresh_row_with_container_down_is_skipped(
        self, mock_db, mock_cap_mgr
    ):
        """Container is down (the orphan-everything branch — the stub
        docker_service returns None for `get_agent_container`). A row
        younger than the grace window must NOT be force-failed — it
        might be a concurrent /internal/execute-task on its way to ZADD
        a slot."""
        from services.cleanup_service import recover_orphaned_executions

        fresh = {
            "id": "exec-fresh",
            "agent_name": "agent-x",
            "started_at": _iso_now_minus(2),
            "schedule_id": None,
        }
        mock_db.get_running_executions.return_value = [fresh]
        mock_cap_mgr.return_value = self._stub_capacity()

        result = _run(recover_orphaned_executions())

        assert result["recovered"] == 0
        assert result["skipped_grace"] == 1
        # Crucially: no mark_failed write hit the DB.
        mock_db.mark_execution_failed_by_watchdog.assert_not_called()

    @patch("services.cleanup_service.get_capacity_manager")
    @patch("services.cleanup_service.db")
    def test_old_row_with_container_down_is_recovered(
        self, mock_db, mock_cap_mgr
    ):
        """An old row past the grace window must be recovered as before."""
        from services.cleanup_service import (
            STARTUP_RECOVERY_GRACE_SECONDS,
            recover_orphaned_executions,
        )

        old = {
            "id": "exec-old",
            "agent_name": "agent-x",
            "started_at": _iso_now_minus(STARTUP_RECOVERY_GRACE_SECONDS + 60),
            "schedule_id": None,
        }
        mock_db.get_running_executions.return_value = [old]
        mock_db.mark_execution_failed_by_watchdog.return_value = True
        mock_cap_mgr.return_value = self._stub_capacity()

        result = _run(recover_orphaned_executions())

        assert result["recovered"] == 1
        assert result["skipped_grace"] == 0
        mock_db.mark_execution_failed_by_watchdog.assert_called_once()
        call_kwargs = mock_db.mark_execution_failed_by_watchdog.call_args.kwargs
        assert call_kwargs["execution_id"] == "exec-old"
        assert "orphaned" in call_kwargs["error_message"].lower()

    @patch("services.cleanup_service.get_capacity_manager")
    @patch("services.cleanup_service.db")
    def test_mixed_batch_only_old_rows_recovered(
        self, mock_db, mock_cap_mgr
    ):
        """Two rows for the same agent — fresh skipped, old recovered."""
        from services.cleanup_service import (
            STARTUP_RECOVERY_GRACE_SECONDS,
            recover_orphaned_executions,
        )

        rows = [
            {
                "id": "exec-fresh",
                "agent_name": "agent-x",
                "started_at": _iso_now_minus(3),
                "schedule_id": None,
            },
            {
                "id": "exec-old",
                "agent_name": "agent-x",
                "started_at": _iso_now_minus(STARTUP_RECOVERY_GRACE_SECONDS + 30),
                "schedule_id": None,
            },
        ]
        mock_db.get_running_executions.return_value = rows
        mock_db.mark_execution_failed_by_watchdog.return_value = True
        mock_cap_mgr.return_value = self._stub_capacity()

        result = _run(recover_orphaned_executions())

        assert result["recovered"] == 1
        assert result["skipped_grace"] == 1
        # Only the old row got the failure writer.
        assert mock_db.mark_execution_failed_by_watchdog.call_count == 1
        args = mock_db.mark_execution_failed_by_watchdog.call_args.kwargs
        assert args["execution_id"] == "exec-old"


# ---------------------------------------------------------------------------
# 3. Warming-up gate (the global recovery-complete flag)
# ---------------------------------------------------------------------------


class TestStartupRecoveryFlag:
    """`is_startup_recovery_complete` / `mark_startup_recovery_complete` semantics."""

    def setup_method(self):
        # Reset before each test so order independence holds.
        from services.cleanup_service import reset_startup_recovery_flag_for_tests
        reset_startup_recovery_flag_for_tests()

    def teardown_method(self):
        # Leave the flag flipped so other tests in the suite that import
        # cleanup_service don't trip the gate by accident.
        from services.cleanup_service import mark_startup_recovery_complete
        mark_startup_recovery_complete()

    def test_starts_closed(self):
        from services.cleanup_service import is_startup_recovery_complete
        assert is_startup_recovery_complete() is False

    def test_mark_flips_open(self):
        from services.cleanup_service import (
            is_startup_recovery_complete,
            mark_startup_recovery_complete,
        )
        assert is_startup_recovery_complete() is False
        mark_startup_recovery_complete()
        assert is_startup_recovery_complete() is True

    def test_mark_is_idempotent(self):
        from services.cleanup_service import (
            is_startup_recovery_complete,
            mark_startup_recovery_complete,
        )
        mark_startup_recovery_complete()
        mark_startup_recovery_complete()
        assert is_startup_recovery_complete() is True


# ---------------------------------------------------------------------------
# 4. /internal/execute-task — gate is wired into the route
# ---------------------------------------------------------------------------
#
# A live route test would pull in the full backend dependency chain
# (auth, agent_service, docker, etc.) and isn't worth stubbing for a
# four-line gate. Instead we verify the gate wiring statically: the
# router must import the readiness predicate AND consult it before the
# audit/execute path runs. The flag-level tests above prove the
# predicate's behaviour; this test prevents a regression where someone
# deletes the gate inside the route.


class TestExecuteTaskGateWiring:
    """Static-source verification that routers/internal.py uses the gate."""

    def _source(self) -> str:
        path = Path(_BACKEND) / "routers" / "internal.py"
        return path.read_text()

    def test_imports_is_startup_recovery_complete(self):
        src = self._source()
        assert "is_startup_recovery_complete" in src, (
            "routers/internal.py must consult the startup-recovery gate"
        )

    def test_gate_raises_503_before_execute(self):
        """The gate must be inside the execute-task handler, raising 503."""
        src = self._source()
        # Pin both the predicate call AND the 503 status code appearing on the
        # same handler. We don't enforce ordering at AST level, but a
        # human-meaningful proximity check catches accidental deletes.
        assert "is_startup_recovery_complete()" in src
        assert "status_code=503" in src
        # The handler function name is stable; locate it and verify both
        # markers appear inside its body (before the closing of the function).
        handler_idx = src.index("def execute_task_internal")
        # Heuristic: the gate must appear in the first 1500 chars of the body
        # so it precedes the audit + task-service call.
        gate_window = src[handler_idx : handler_idx + 1500]
        assert "is_startup_recovery_complete()" in gate_window, (
            "gate predicate must be at the top of execute_task_internal"
        )
        assert "503" in gate_window, (
            "gate must raise 503 inside execute_task_internal"
        )
