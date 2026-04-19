"""
Watchdog Unit Tests (test_watchdog_unit.py)

Unit tests for Issue #129: Active watchdog reconciliation logic.
Tests DB methods, reconciliation decision matrix, recovery helper,
and error isolation — all with mocked agent HTTP responses.
"""

import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

# Add backend to path for direct imports in unit tests
_backend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "backend")
)
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)

# Pre-mock modules that conflict with test environment:
# - database: tries to write to /data (doesn't exist outside Docker)
# - utils.helpers: shadowed by tests/utils/ package
# - models: depends on utils.helpers
from unittest.mock import MagicMock as _MagicMock

# tests/utils shadows src/backend/utils — provide real helper implementations
# needed by cleanup_service for timestamp math
import types as _types
_helpers_mod = _types.ModuleType("utils.helpers")

def _utc_now():
    return datetime.utcnow()

def _utc_now_iso():
    return datetime.utcnow().isoformat() + "Z"

def _parse_iso_timestamp(s):
    s = s.rstrip("Z")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.utcnow()

_helpers_mod.utc_now = _utc_now
_helpers_mod.utc_now_iso = _utc_now_iso
_helpers_mod.parse_iso_timestamp = _parse_iso_timestamp
_helpers_mod.to_utc_iso = _MagicMock(return_value="2025-01-01T00:00:00Z")
sys.modules["utils.helpers"] = _helpers_mod

# Issue #286: Mock credential_sanitizer for cleanup_service import
_sanitizer_mod = _types.ModuleType("utils.credential_sanitizer")
_sanitizer_mod.sanitize_text = lambda x: x  # Pass-through for tests
sys.modules["utils.credential_sanitizer"] = _sanitizer_mod

sys.modules.setdefault("database", _MagicMock())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now_iso():
    return datetime.utcnow().isoformat() + "Z"


def _past_iso(minutes: int) -> str:
    """Return an ISO timestamp N minutes in the past."""
    return (datetime.utcnow() - timedelta(minutes=minutes)).isoformat() + "Z"


# ---------------------------------------------------------------------------
# CleanupReport tests
# ---------------------------------------------------------------------------

class TestCleanupReport:
    """Tests for expanded CleanupReport dataclass."""

    pytestmark = pytest.mark.unit

    def test_report_includes_watchdog_fields(self):
        """CleanupReport has orphaned_executions and auto_terminated fields."""
        from services.cleanup_service import CleanupReport

        report = CleanupReport()
        assert report.orphaned_executions == 0
        assert report.auto_terminated == 0

    def test_report_total_includes_watchdog_fields(self):
        """Total correctly sums all fields including watchdog additions."""
        import sys
        import os
        backend_path = os.path.join(os.path.dirname(__file__), "..", "src", "backend")
        if backend_path not in sys.path:
            sys.path.insert(0, os.path.abspath(backend_path))

        from services.cleanup_service import CleanupReport

        report = CleanupReport(
            orphaned_executions=2,
            auto_terminated=1,
            stale_executions=3,
            no_session_executions=1,
            orphaned_skipped=0,
            stale_activities=1,
            stale_slots=0,
        )
        assert report.total == 8

    def test_report_to_dict_includes_watchdog_fields(self):
        """to_dict() includes watchdog fields."""
        import sys
        import os
        backend_path = os.path.join(os.path.dirname(__file__), "..", "src", "backend")
        if backend_path not in sys.path:
            sys.path.insert(0, os.path.abspath(backend_path))

        from services.cleanup_service import CleanupReport

        report = CleanupReport(orphaned_executions=1, auto_terminated=2)
        d = report.to_dict()
        assert d["orphaned_executions"] == 1
        assert d["auto_terminated"] == 2
        assert "total" in d


# ---------------------------------------------------------------------------
# DB method tests (using in-memory SQLite)
# ---------------------------------------------------------------------------

class TestGetRunningExecutionsWithAgentInfo:
    """Tests for get_running_executions_with_agent_info() DB method."""

    pytestmark = pytest.mark.unit

    def _setup_db(self):
        """Create in-memory SQLite with required tables and return connection.

        Includes agent_ownership table to match the production 3-way COALESCE
        query: COALESCE(s.timeout_seconds, ao.execution_timeout_seconds, 900).
        """
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE agent_schedules (
                id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                name TEXT NOT NULL,
                cron_expression TEXT NOT NULL,
                message TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                timezone TEXT DEFAULT 'UTC',
                description TEXT,
                owner_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                timeout_seconds INTEGER DEFAULT 900
            )
        """)
        conn.execute("""
            CREATE TABLE schedule_executions (
                id TEXT PRIMARY KEY,
                schedule_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                duration_ms INTEGER,
                message TEXT NOT NULL,
                response TEXT,
                error TEXT,
                triggered_by TEXT NOT NULL DEFAULT 'schedule',
                claude_session_id TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE agent_ownership (
                agent_name TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                execution_timeout_seconds INTEGER DEFAULT 900
            )
        """)
        conn.commit()
        return conn

    def test_returns_running_executions_with_timeout(self):
        """Returns running executions joined with schedule timeout."""
        conn = self._setup_db()
        conn.execute(
            "INSERT INTO agent_schedules (id, agent_name, name, cron_expression, message, owner_id, created_at, updated_at, timeout_seconds) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sched-1", "agent-a", "Test Schedule", "0 * * * *", "do something", 1, _utc_now_iso(), _utc_now_iso(), 600),
        )
        conn.execute(
            "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, started_at, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("exec-1", "sched-1", "agent-a", "running", _past_iso(10), "test message"),
        )
        conn.commit()

        cursor = conn.cursor()
        cursor.execute("""
            SELECT e.id, e.schedule_id, e.agent_name, e.started_at, e.message,
                   COALESCE(s.timeout_seconds, ao.execution_timeout_seconds, 900) as timeout_seconds
            FROM schedule_executions e
            LEFT JOIN agent_schedules s ON e.schedule_id = s.id
            LEFT JOIN agent_ownership ao ON e.agent_name = ao.agent_name
            WHERE e.status = 'running'
        """)
        rows = [dict(r) for r in cursor.fetchall()]

        assert len(rows) == 1
        assert rows[0]["id"] == "exec-1"
        assert rows[0]["agent_name"] == "agent-a"
        assert rows[0]["timeout_seconds"] == 600

    def test_manual_execution_coalesces_to_default(self):
        """Manual executions (no schedule) get COALESCE default of 900s."""
        conn = self._setup_db()
        conn.execute(
            "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, started_at, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("exec-2", "__manual__", "agent-b", "running", _past_iso(5), "manual task"),
        )
        conn.commit()

        cursor = conn.cursor()
        cursor.execute("""
            SELECT e.id, COALESCE(s.timeout_seconds, ao.execution_timeout_seconds, 900) as timeout_seconds
            FROM schedule_executions e
            LEFT JOIN agent_schedules s ON e.schedule_id = s.id
            LEFT JOIN agent_ownership ao ON e.agent_name = ao.agent_name
            WHERE e.status = 'running'
        """)
        rows = [dict(r) for r in cursor.fetchall()]

        assert len(rows) == 1
        assert rows[0]["timeout_seconds"] == 900

    def test_agent_timeout_fallback(self):
        """When schedule has no timeout but agent_ownership does, use agent timeout."""
        conn = self._setup_db()
        # Schedule with NULL timeout
        conn.execute(
            "INSERT INTO agent_schedules (id, agent_name, name, cron_expression, message, owner_id, created_at, updated_at, timeout_seconds) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sched-1", "agent-a", "Test Schedule", "0 * * * *", "do something", 1, _utc_now_iso(), _utc_now_iso(), None),
        )
        # Agent ownership with custom timeout
        conn.execute(
            "INSERT INTO agent_ownership (agent_name, owner_id, execution_timeout_seconds) "
            "VALUES (?, ?, ?)",
            ("agent-a", 1, 1800),
        )
        conn.execute(
            "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, started_at, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("exec-1", "sched-1", "agent-a", "running", _past_iso(10), "test"),
        )
        conn.commit()

        cursor = conn.cursor()
        cursor.execute("""
            SELECT e.id, COALESCE(s.timeout_seconds, ao.execution_timeout_seconds, 900) as timeout_seconds
            FROM schedule_executions e
            LEFT JOIN agent_schedules s ON e.schedule_id = s.id
            LEFT JOIN agent_ownership ao ON e.agent_name = ao.agent_name
            WHERE e.status = 'running'
        """)
        rows = [dict(r) for r in cursor.fetchall()]

        assert len(rows) == 1
        assert rows[0]["timeout_seconds"] == 1800

    def test_empty_result_when_no_running(self):
        """Returns empty list when no running executions."""
        conn = self._setup_db()
        conn.execute(
            "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, started_at, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("exec-3", "__manual__", "agent-c", "success", _past_iso(60), "done"),
        )
        conn.commit()

        cursor = conn.cursor()
        cursor.execute("""
            SELECT e.id FROM schedule_executions e WHERE e.status = 'running'
        """)
        rows = cursor.fetchall()
        assert len(rows) == 0


class TestMarkExecutionFailedByWatchdog:
    """Tests for mark_execution_failed_by_watchdog() DB method."""

    pytestmark = pytest.mark.unit

    def _setup_db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE schedule_executions (
                id TEXT PRIMARY KEY,
                schedule_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                duration_ms INTEGER,
                message TEXT NOT NULL,
                error TEXT
            )
        """)
        conn.commit()
        return conn

    def test_marks_running_as_failed(self):
        """Updates status from running to failed with error message."""
        conn = self._setup_db()
        started = _past_iso(20)
        conn.execute(
            "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, started_at, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("exec-1", "sched-1", "agent-a", "running", started, "test"),
        )
        conn.commit()

        # Simulate the conditional update
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE schedule_executions
            SET status = 'failed', error = ?
            WHERE id = ? AND status = 'running'
        """, ("Recovered by watchdog", "exec-1"))
        conn.commit()

        assert cursor.rowcount == 1

        # Verify the update
        cursor.execute("SELECT status, error FROM schedule_executions WHERE id = ?", ("exec-1",))
        row = dict(cursor.fetchone())
        assert row["status"] == "failed"
        assert row["error"] == "Recovered by watchdog"

    def test_race_guard_returns_zero_if_already_completed(self):
        """WHERE status='running' guard prevents overwriting completed execution."""
        conn = self._setup_db()
        conn.execute(
            "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, started_at, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("exec-2", "sched-1", "agent-a", "success", _past_iso(20), "test"),
        )
        conn.commit()

        cursor = conn.cursor()
        cursor.execute("""
            UPDATE schedule_executions
            SET status = 'failed', error = ?
            WHERE id = ? AND status = 'running'
        """, ("Recovered by watchdog", "exec-2"))
        conn.commit()

        assert cursor.rowcount == 0  # No rows updated — already completed


# ---------------------------------------------------------------------------
# Reconciliation logic tests
# ---------------------------------------------------------------------------

class TestReconcileOrphanedExecutions:
    """Tests for _reconcile_orphaned_executions() logic."""

    pytestmark = pytest.mark.unit

    def _make_service(self):
        """Create a CleanupService with mocked dependencies."""
        from services.cleanup_service import CleanupService
        return CleanupService()

    def _mock_httpx_client(self):
        """Create a mock httpx.AsyncClient context manager."""
        mock_client = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        return mock_cm, mock_client

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_slot_service")
    @patch("services.cleanup_service.get_execution_queue")
    def test_agent_unreachable_skips(self, mock_queue_fn, mock_slot_fn, mock_db, mock_httpx):
        """When agent is unreachable, skip its executions entirely."""
        mock_cm, mock_client = self._mock_httpx_client()
        mock_httpx.return_value = mock_cm

        mock_db.get_running_executions_with_agent_info.return_value = [
            {"id": "exec-1", "agent_name": "agent-down", "started_at": _past_iso(60), "timeout_seconds": 900, "schedule_id": "s1"},
        ]

        service = self._make_service()

        # Mock _get_agent_running_ids to return None (unreachable)
        service._get_agent_running_ids = AsyncMock(return_value=None)

        orphaned, terminated, confirmed_running = asyncio.run(
            service._reconcile_orphaned_executions()
        )

        assert orphaned == 0
        assert terminated == 0
        assert confirmed_running == set()
        mock_db.mark_execution_failed_by_watchdog.assert_not_called()

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_slot_service")
    @patch("services.cleanup_service.get_execution_queue")
    def test_orphan_not_found_on_agent(self, mock_queue_fn, mock_slot_fn, mock_db, mock_httpx):
        """Execution not found on agent -> orphan recovery."""
        mock_cm, _ = self._mock_httpx_client()
        mock_httpx.return_value = mock_cm

        mock_db.get_running_executions_with_agent_info.return_value = [
            {"id": "exec-1", "agent_name": "agent-a", "started_at": _past_iso(10), "timeout_seconds": 900, "schedule_id": "s1"},
        ]
        mock_db.mark_execution_failed_by_watchdog.return_value = True

        mock_slot = AsyncMock()
        mock_slot_fn.return_value = mock_slot
        mock_q = AsyncMock()
        mock_queue_fn.return_value = mock_q

        service = self._make_service()
        service._get_agent_running_ids = AsyncMock(return_value=set())
        service._broadcast_watchdog_event = AsyncMock()

        orphaned, terminated, confirmed_running = asyncio.run(
            service._reconcile_orphaned_executions()
        )

        assert orphaned == 1
        assert terminated == 0
        assert confirmed_running == set()
        mock_db.mark_execution_failed_by_watchdog.assert_called_once()
        mock_slot.release_slot.assert_called_once_with("agent-a", "exec-1")
        # Atomic conditional release — no TOCTOU race
        mock_q.force_release_if_matches.assert_called_once_with("agent-a", "exec-1")

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_slot_service")
    @patch("services.cleanup_service.get_execution_queue")
    def test_running_under_timeout_no_action(self, mock_queue_fn, mock_slot_fn, mock_db, mock_httpx):
        """Execution running on agent under timeout -> no action."""
        mock_cm, _ = self._mock_httpx_client()
        mock_httpx.return_value = mock_cm

        mock_db.get_running_executions_with_agent_info.return_value = [
            {"id": "exec-1", "agent_name": "agent-a", "started_at": _past_iso(5), "timeout_seconds": 900, "schedule_id": "s1"},
        ]

        service = self._make_service()
        service._get_agent_running_ids = AsyncMock(return_value={"exec-1"})

        orphaned, terminated, confirmed_running = asyncio.run(
            service._reconcile_orphaned_executions()
        )

        assert orphaned == 0
        assert terminated == 0
        # #226: Execution confirmed as still running within timeout
        assert confirmed_running == {"exec-1"}
        mock_db.mark_execution_failed_by_watchdog.assert_not_called()

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_slot_service")
    @patch("services.cleanup_service.get_execution_queue")
    def test_running_over_timeout_auto_terminates(self, mock_queue_fn, mock_slot_fn, mock_db, mock_httpx):
        """Execution running on agent over timeout -> auto-terminate."""
        mock_cm, _ = self._mock_httpx_client()
        mock_httpx.return_value = mock_cm

        mock_db.get_running_executions_with_agent_info.return_value = [
            {"id": "exec-1", "agent_name": "agent-a", "started_at": _past_iso(20), "timeout_seconds": 600, "schedule_id": "s1"},
        ]
        mock_db.mark_execution_failed_by_watchdog.return_value = True

        mock_slot = AsyncMock()
        mock_slot_fn.return_value = mock_slot
        mock_q = AsyncMock()
        mock_queue_fn.return_value = mock_q

        service = self._make_service()
        service._get_agent_running_ids = AsyncMock(return_value={"exec-1"})
        service._terminate_on_agent = AsyncMock(return_value=True)
        service._broadcast_watchdog_event = AsyncMock()

        orphaned, terminated, confirmed_running = asyncio.run(
            service._reconcile_orphaned_executions()
        )

        assert orphaned == 0
        assert terminated == 1
        assert confirmed_running == set()  # Over timeout, so not confirmed
        service._terminate_on_agent.assert_called_once_with(ANY, "agent-a", "exec-1")
        mock_db.mark_execution_failed_by_watchdog.assert_called_once()

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_slot_service")
    @patch("services.cleanup_service.get_execution_queue")
    def test_terminate_fails_skips_recovery(self, mock_queue_fn, mock_slot_fn, mock_db, mock_httpx):
        """If terminate returns False, DB/resource cleanup is skipped."""
        mock_cm, _ = self._mock_httpx_client()
        mock_httpx.return_value = mock_cm

        mock_db.get_running_executions_with_agent_info.return_value = [
            {"id": "exec-1", "agent_name": "agent-a", "started_at": _past_iso(20), "timeout_seconds": 600, "schedule_id": "s1"},
        ]

        mock_slot = AsyncMock()
        mock_slot_fn.return_value = mock_slot
        mock_q = AsyncMock()
        mock_queue_fn.return_value = mock_q

        service = self._make_service()
        service._get_agent_running_ids = AsyncMock(return_value={"exec-1"})
        service._terminate_on_agent = AsyncMock(return_value=False)
        service._broadcast_watchdog_event = AsyncMock()

        orphaned, terminated, confirmed_running = asyncio.run(
            service._reconcile_orphaned_executions()
        )

        # Terminate failed — should NOT mark as failed or release resources
        assert terminated == 0
        assert confirmed_running == set()
        mock_db.mark_execution_failed_by_watchdog.assert_not_called()
        mock_slot.release_slot.assert_not_called()

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_slot_service")
    @patch("services.cleanup_service.get_execution_queue")
    def test_race_condition_db_update_noop(self, mock_queue_fn, mock_slot_fn, mock_db, mock_httpx):
        """When DB update returns False (race), skip slot/queue release."""
        mock_cm, _ = self._mock_httpx_client()
        mock_httpx.return_value = mock_cm

        mock_db.get_running_executions_with_agent_info.return_value = [
            {"id": "exec-1", "agent_name": "agent-a", "started_at": _past_iso(10), "timeout_seconds": 900, "schedule_id": "s1"},
        ]
        mock_db.mark_execution_failed_by_watchdog.return_value = False

        mock_slot = AsyncMock()
        mock_slot_fn.return_value = mock_slot
        mock_q = AsyncMock()
        mock_queue_fn.return_value = mock_q

        service = self._make_service()
        service._get_agent_running_ids = AsyncMock(return_value=set())
        service._broadcast_watchdog_event = AsyncMock()

        orphaned, terminated, confirmed_running = asyncio.run(
            service._reconcile_orphaned_executions()
        )

        assert orphaned == 0
        assert confirmed_running == set()
        mock_slot.release_slot.assert_not_called()
        mock_q.force_release_if_matches.assert_not_called()

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_slot_service")
    @patch("services.cleanup_service.get_execution_queue")
    def test_per_execution_error_isolation(self, mock_queue_fn, mock_slot_fn, mock_db, mock_httpx):
        """One execution's failure doesn't block recovery of others."""
        mock_cm, _ = self._mock_httpx_client()
        mock_httpx.return_value = mock_cm

        mock_db.get_running_executions_with_agent_info.return_value = [
            {"id": "exec-BAD", "agent_name": "agent-a", "started_at": _past_iso(10), "timeout_seconds": 900, "schedule_id": "s1"},
            {"id": "exec-GOOD", "agent_name": "agent-a", "started_at": _past_iso(10), "timeout_seconds": 900, "schedule_id": "s1"},
        ]

        mock_db.mark_execution_failed_by_watchdog.side_effect = [
            Exception("DB error on first"),
            True,
        ]

        mock_slot = AsyncMock()
        mock_slot_fn.return_value = mock_slot
        mock_q = AsyncMock()
        mock_queue_fn.return_value = mock_q

        service = self._make_service()
        service._get_agent_running_ids = AsyncMock(return_value=set())
        service._broadcast_watchdog_event = AsyncMock()

        orphaned, terminated, confirmed_running = asyncio.run(
            service._reconcile_orphaned_executions()
        )

        assert orphaned == 1
        assert confirmed_running == set()
        assert mock_db.mark_execution_failed_by_watchdog.call_count == 2


# ---------------------------------------------------------------------------
# WebSocket broadcast tests
# ---------------------------------------------------------------------------

class TestBroadcastWatchdogEvent:
    """Tests for _broadcast_watchdog_event()."""

    pytestmark = pytest.mark.unit

    def test_noop_when_ws_manager_none(self):
        """No error when WebSocket manager is not set."""
        from services.cleanup_service import CleanupService
        import services.cleanup_service as cs_module

        original = cs_module._ws_manager
        cs_module._ws_manager = None
        try:
            service = CleanupService()
            # Should not raise
            asyncio.run(
                service._broadcast_watchdog_event("orphan_recovered", "agent-a", "exec-1", "test reason")
            )
        finally:
            cs_module._ws_manager = original

    def test_broadcasts_correct_event_format(self):
        """WebSocket event has correct JSON structure."""
        from services.cleanup_service import CleanupService
        import services.cleanup_service as cs_module

        mock_manager = MagicMock()
        mock_manager.broadcast = AsyncMock()

        original = cs_module._ws_manager
        cs_module._ws_manager = mock_manager
        try:
            service = CleanupService()
            asyncio.run(
                service._broadcast_watchdog_event("auto_terminated", "agent-x", "exec-42", "timed out")
            )

            mock_manager.broadcast.assert_called_once()
            event_json = mock_manager.broadcast.call_args[0][0]
            event = json.loads(event_json)

            assert event["type"] == "watchdog_recovery"
            assert event["agent_name"] == "agent-x"
            assert event["execution_id"] == "exec-42"
            assert event["action"] == "auto_terminated"
            assert event["reason"] == "timed out"
            assert "timestamp" in event
        finally:
            cs_module._ws_manager = original


# ---------------------------------------------------------------------------
# Error Context Preservation tests (Issue #286)
# ---------------------------------------------------------------------------

class TestGetExecutionError:
    """Tests for _get_execution_error() — Issue #286."""

    pytestmark = pytest.mark.unit

    def _make_service(self):
        from services.cleanup_service import CleanupService
        return CleanupService()

    def test_returns_error_from_agent(self):
        """Returns formatted error when agent responds with error info."""
        service = self._make_service()
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "error_type": "auth_failure",
            "error_message": "Invalid API key"
        }
        mock_client.get = AsyncMock(return_value=mock_response)

        result = asyncio.run(
            service._get_execution_error(mock_client, "agent-a", "exec-1")
        )

        assert result == "[auth_failure] Invalid API key"
        mock_client.get.assert_called_once()
        assert "/api/executions/exec-1/last-error" in mock_client.get.call_args[0][0]

    def test_returns_none_when_no_error(self):
        """Returns None when agent has no error to report."""
        service = self._make_service()
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "error_type": None,
            "error_message": None
        }
        mock_client.get = AsyncMock(return_value=mock_response)

        result = asyncio.run(
            service._get_execution_error(mock_client, "agent-a", "exec-1")
        )

        assert result is None

    def test_returns_none_on_connect_error(self):
        """Returns None when agent is unreachable."""
        import httpx
        service = self._make_service()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

        result = asyncio.run(
            service._get_execution_error(mock_client, "agent-down", "exec-1")
        )

        assert result is None

    def test_returns_none_on_timeout(self):
        """Returns None when agent request times out."""
        import httpx
        service = self._make_service()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("Request timed out"))

        result = asyncio.run(
            service._get_execution_error(mock_client, "agent-slow", "exec-1")
        )

        assert result is None

    def test_handles_error_type_only(self):
        """Handles case where error_type is set but error_message is None."""
        service = self._make_service()
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "error_type": "rate_limit",
            "error_message": None
        }
        mock_client.get = AsyncMock(return_value=mock_response)

        result = asyncio.run(
            service._get_execution_error(mock_client, "agent-a", "exec-1")
        )

        assert result == "[rate_limit]"


class TestRecoverExecutionWithErrorContext:
    """Tests for _recover_execution() with error context — Issue #286."""

    pytestmark = pytest.mark.unit

    def _make_service(self):
        from services.cleanup_service import CleanupService
        return CleanupService()

    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_slot_service")
    @patch("services.cleanup_service.get_execution_queue")
    def test_combines_original_error_with_cleanup_reason(self, mock_queue_fn, mock_slot_fn, mock_db):
        """Combines original error context with cleanup reason."""
        mock_db.mark_execution_failed_by_watchdog.return_value = True
        mock_slot = AsyncMock()
        mock_slot_fn.return_value = mock_slot
        mock_q = AsyncMock()
        mock_queue_fn.return_value = mock_q

        service = self._make_service()
        service._get_execution_error = AsyncMock(return_value="[auth_failure] Token expired")
        service._broadcast_watchdog_event = AsyncMock()

        mock_client = AsyncMock()

        result = asyncio.run(
            service._recover_execution(
                "exec-1", "agent-a", "recovered by watchdog", "orphan_recovered", mock_client
            )
        )

        assert result is True
        # Verify combined error message was passed to DB
        mock_db.mark_execution_failed_by_watchdog.assert_called_once()
        error_arg = mock_db.mark_execution_failed_by_watchdog.call_args[0][1]
        assert "[auth_failure] Token expired" in error_arg
        assert "recovered by watchdog" in error_arg

    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_slot_service")
    @patch("services.cleanup_service.get_execution_queue")
    def test_uses_cleanup_reason_when_no_original_error(self, mock_queue_fn, mock_slot_fn, mock_db):
        """Uses cleanup reason alone when no original error is available."""
        mock_db.mark_execution_failed_by_watchdog.return_value = True
        mock_slot = AsyncMock()
        mock_slot_fn.return_value = mock_slot
        mock_q = AsyncMock()
        mock_queue_fn.return_value = mock_q

        service = self._make_service()
        service._get_execution_error = AsyncMock(return_value=None)
        service._broadcast_watchdog_event = AsyncMock()

        mock_client = AsyncMock()

        result = asyncio.run(
            service._recover_execution(
                "exec-1", "agent-a", "recovered by watchdog", "orphan_recovered", mock_client
            )
        )

        assert result is True
        error_arg = mock_db.mark_execution_failed_by_watchdog.call_args[0][1]
        assert error_arg == "recovered by watchdog"

    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_slot_service")
    @patch("services.cleanup_service.get_execution_queue")
    def test_truncates_long_error_messages(self, mock_queue_fn, mock_slot_fn, mock_db):
        """Truncates combined error message to prevent DB bloat."""
        mock_db.mark_execution_failed_by_watchdog.return_value = True
        mock_slot = AsyncMock()
        mock_slot_fn.return_value = mock_slot
        mock_q = AsyncMock()
        mock_queue_fn.return_value = mock_q

        service = self._make_service()
        # Create a very long error message
        long_error = "x" * 3000
        service._get_execution_error = AsyncMock(return_value=long_error)
        service._broadcast_watchdog_event = AsyncMock()

        mock_client = AsyncMock()

        asyncio.run(
            service._recover_execution(
                "exec-1", "agent-a", "cleanup reason", "orphan_recovered", mock_client
            )
        )

        error_arg = mock_db.mark_execution_failed_by_watchdog.call_args[0][1]
        # Should be truncated to MAX_ERROR_MESSAGE_LENGTH (2000) + "..."
        assert len(error_arg) <= 2003
        assert error_arg.endswith("...")

    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_slot_service")
    @patch("services.cleanup_service.get_execution_queue")
    def test_works_without_client(self, mock_queue_fn, mock_slot_fn, mock_db):
        """Works correctly when no client is provided (fallback path)."""
        mock_db.mark_execution_failed_by_watchdog.return_value = True
        mock_slot = AsyncMock()
        mock_slot_fn.return_value = mock_slot
        mock_q = AsyncMock()
        mock_queue_fn.return_value = mock_q

        service = self._make_service()
        service._get_execution_error = AsyncMock()
        service._broadcast_watchdog_event = AsyncMock()

        result = asyncio.run(
            service._recover_execution(
                "exec-1", "agent-a", "cleanup reason only", "orphan_recovered", None
            )
        )

        assert result is True
        # _get_execution_error should NOT be called when client is None
        service._get_execution_error.assert_not_called()
        error_arg = mock_db.mark_execution_failed_by_watchdog.call_args[0][1]
        assert error_arg == "cleanup reason only"


# ---------------------------------------------------------------------------
# Phase 3 slot reclaim re-verification tests (Issue #378)
# ---------------------------------------------------------------------------

# Eager import so @patch("services.cleanup_service.*") decorators can resolve
# the module before test setup runs.
import services.cleanup_service  # noqa: E402,F401


class TestProcessStaleSlotReclaims:
    """Tests for _process_stale_slot_reclaims() — Phase 3 slot cleanup with #378 race fix.

    The bug: cleanup service's Phase 3 sometimes marked executions FAILED with
    "Stale execution — slot TTL expired" even though the task was still running
    (or had just completed). The fix adds a just-in-time re-verify call to the
    agent right before failing. On agent unreachable, we skip this cycle — the
    120-min Phase 1 stale cleanup is the backstop.
    """

    pytestmark = pytest.mark.unit

    PHANTOM_ERROR_PREFIX = "Stale execution — slot TTL expired"

    def _make_service(self):
        from services.cleanup_service import CleanupService
        return CleanupService()

    def _make_report(self):
        from services.cleanup_service import CleanupReport
        return CleanupReport()

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    def test_empty_reclaimed_is_noop(self, mock_db, mock_httpx):
        """No reclaimed slots → method returns early without any calls."""
        service = self._make_service()
        service._get_agent_running_ids = AsyncMock()
        service._terminate_on_agent = AsyncMock()

        report = self._make_report()
        asyncio.run(service._process_stale_slot_reclaims({}, set(), report))

        service._get_agent_running_ids.assert_not_called()
        service._terminate_on_agent.assert_not_called()
        mock_db.fail_stale_slot_execution.assert_not_called()
        # httpx.AsyncClient should not even be constructed
        mock_httpx.assert_not_called()
        assert report.stale_slot_executions == 0

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    def test_skips_when_in_confirmed_running_ids(self, mock_db, mock_httpx):
        """Phase 0 confirmed this exec as running → Phase 3 skips without
        even calling fail_stale_slot_execution. Regression guard for #226."""
        mock_httpx.return_value.__aenter__ = AsyncMock(
            return_value=AsyncMock()
        )
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        service = self._make_service()
        service._get_agent_running_ids = AsyncMock(return_value=set())
        service._terminate_on_agent = AsyncMock()

        reclaimed = {"agent-a": ["exec-1"]}
        confirmed = {"exec-1"}
        report = self._make_report()

        asyncio.run(service._process_stale_slot_reclaims(reclaimed, confirmed, report))

        mock_db.fail_stale_slot_execution.assert_not_called()
        service._terminate_on_agent.assert_not_called()
        assert report.stale_slot_executions == 0

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    def test_378_race_skips_when_reverify_shows_still_running(self, mock_db, mock_httpx):
        """#378 core scenario: Phase 0 missed this exec (agent had just
        finished handing it back), but just-in-time re-verify catches
        the agent still has it → Phase 3 skips. No FAILED row written."""
        mock_httpx.return_value.__aenter__ = AsyncMock(
            return_value=AsyncMock()
        )
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        service = self._make_service()
        # Re-verify returns a set containing the exec → still running on agent
        service._get_agent_running_ids = AsyncMock(return_value={"exec-1"})
        service._terminate_on_agent = AsyncMock()

        reclaimed = {"agent-a": ["exec-1"]}
        report = self._make_report()

        asyncio.run(service._process_stale_slot_reclaims(reclaimed, set(), report))

        mock_db.fail_stale_slot_execution.assert_not_called()
        service._terminate_on_agent.assert_not_called()
        assert report.stale_slot_executions == 0

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    def test_proceeds_to_fail_when_reverify_confirms_inactive(self, mock_db, mock_httpx):
        """Re-verify returns set without the exec → agent confirms gone →
        proceed to terminate + fail. Phantom-stale error message emitted."""
        mock_httpx.return_value.__aenter__ = AsyncMock(
            return_value=AsyncMock()
        )
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_db.fail_stale_slot_execution.return_value = True

        service = self._make_service()
        service._get_agent_running_ids = AsyncMock(return_value=set())
        service._terminate_on_agent = AsyncMock(return_value=True)

        reclaimed = {"agent-a": ["exec-1"]}
        report = self._make_report()

        asyncio.run(service._process_stale_slot_reclaims(reclaimed, set(), report))

        service._terminate_on_agent.assert_called_once_with(ANY, "agent-a", "exec-1")
        mock_db.fail_stale_slot_execution.assert_called_once()
        call_kwargs = mock_db.fail_stale_slot_execution.call_args.kwargs
        assert call_kwargs["execution_id"] == "exec-1"
        assert self.PHANTOM_ERROR_PREFIX in call_kwargs["error"]
        assert report.stale_slot_executions == 1

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    def test_skips_when_agent_unreachable(self, mock_db, mock_httpx):
        """Re-verify returns None (agent unreachable) → skip this cycle.
        Phase 1's 120-min stale cleanup is the backstop for truly stuck
        agents — we do NOT maintain cross-cycle defer state because
        cleanup_stale_slots removes reclaimed IDs from Redis permanently."""
        mock_httpx.return_value.__aenter__ = AsyncMock(
            return_value=AsyncMock()
        )
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        service = self._make_service()
        service._get_agent_running_ids = AsyncMock(return_value=None)
        service._terminate_on_agent = AsyncMock()

        reclaimed = {"agent-a": ["exec-1"]}
        report = self._make_report()

        asyncio.run(service._process_stale_slot_reclaims(reclaimed, set(), report))

        mock_db.fail_stale_slot_execution.assert_not_called()
        service._terminate_on_agent.assert_not_called()
        assert report.stale_slot_executions == 0

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    def test_per_agent_batching_single_reverify_call(self, mock_db, mock_httpx):
        """Two stale slots on the same agent → _get_agent_running_ids
        called exactly once for that agent (per-agent batching via
        asyncio.gather, not per-execution)."""
        mock_httpx.return_value.__aenter__ = AsyncMock(
            return_value=AsyncMock()
        )
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_db.fail_stale_slot_execution.return_value = True

        service = self._make_service()
        service._get_agent_running_ids = AsyncMock(return_value=set())  # both inactive
        service._terminate_on_agent = AsyncMock(return_value=True)

        reclaimed = {"agent-a": ["exec-1", "exec-2"]}
        report = self._make_report()

        asyncio.run(service._process_stale_slot_reclaims(reclaimed, set(), report))

        # One re-verify call for the agent, not two
        assert service._get_agent_running_ids.call_count == 1
        # Both executions failed
        assert mock_db.fail_stale_slot_execution.call_count == 2
        assert report.stale_slot_executions == 2

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    def test_multi_agent_reverify_dispatched_in_parallel(self, mock_db, mock_httpx):
        """Two different agents → both re-verify calls dispatched via
        asyncio.gather. Verified by call count — both agents queried
        regardless of order."""
        mock_httpx.return_value.__aenter__ = AsyncMock(
            return_value=AsyncMock()
        )
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_db.fail_stale_slot_execution.return_value = True

        service = self._make_service()
        # Both agents report their execs as inactive — simple fail path
        service._get_agent_running_ids = AsyncMock(return_value=set())
        service._terminate_on_agent = AsyncMock(return_value=True)

        reclaimed = {"agent-a": ["exec-a1"], "agent-b": ["exec-b1"]}
        report = self._make_report()

        asyncio.run(service._process_stale_slot_reclaims(reclaimed, set(), report))

        # Both agents queried
        assert service._get_agent_running_ids.call_count == 2
        called_agents = {
            call.args[1] for call in service._get_agent_running_ids.call_args_list
        }
        assert called_agents == {"agent-a", "agent-b"}
        assert mock_db.fail_stale_slot_execution.call_count == 2
        assert report.stale_slot_executions == 2

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    def test_one_agent_raises_others_proceed(self, mock_db, mock_httpx):
        """One agent's re-verify raises → asyncio.gather(return_exceptions=True)
        captures it → that agent's execs are skipped as unreachable;
        other agents proceed normally."""
        mock_httpx.return_value.__aenter__ = AsyncMock(
            return_value=AsyncMock()
        )
        mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_db.fail_stale_slot_execution.return_value = True

        service = self._make_service()

        async def flaky(_client, name):
            if name == "agent-a":
                raise RuntimeError("boom")
            return set()  # agent-b: exec inactive

        service._get_agent_running_ids = AsyncMock(side_effect=flaky)
        service._terminate_on_agent = AsyncMock(return_value=True)

        reclaimed = {"agent-a": ["exec-a1"], "agent-b": ["exec-b1"]}
        report = self._make_report()

        asyncio.run(service._process_stale_slot_reclaims(reclaimed, set(), report))

        # Only agent-b's exec was failed; agent-a treated as unreachable
        mock_db.fail_stale_slot_execution.assert_called_once()
        assert mock_db.fail_stale_slot_execution.call_args.kwargs["execution_id"] == "exec-b1"
        assert report.stale_slot_executions == 1
