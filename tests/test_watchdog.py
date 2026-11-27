"""
Watchdog Integration Tests (test_watchdog.py)

Tests for Issue #129: Active watchdog remediation of stuck executions.
Integration tests against the running backend — verify cleanup report
includes watchdog fields.

Issue #921 (revised): the race between agent unregister and backend
success-write is closed agent-side via `process_registry`'s recently-
completed window. The watchdog therefore recovers a true orphan on a
single cycle and never sees the race-window false-positive in the first
place. These tests cover the surviving end-to-end behaviours.

Feature Flow: docs/memory/feature-flows/cleanup-service.md
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from testkit import backend_db
from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_json_response,
)

# trinity-system is a stable, always-running agent on every Trinity instance.
# Its /api/executions/running endpoint returns an empty list when nothing's
# in flight, which is the exact state the watchdog sees during the race.
_SENTINEL_AGENT = "trinity-system"


def _insert_running_row(execution_id: str, age_seconds: int = 120) -> None:
    """Insert a schedule_executions row that looks like an in-flight task.

    `claude_session_id` is set so the #106 no-session fast-fail path doesn't
    fire before the orphan-recovery path can run. Written through the
    backend's own engine, so it lands in whichever database the stack booted
    on (#4620).
    """
    started_at = (
        datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    backend_db.execute(
        "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, "
        "started_at, message, triggered_by, claude_session_id) "
        "VALUES (:id, 'sched-921-int', :agent, 'running', :started_at, "
        "'integration #921', 'manual', 'sess-921-int')",
        id=execution_id, agent=_SENTINEL_AGENT, started_at=started_at,
    )


def _row_state(execution_id: str) -> dict:
    """Return {status, error, response} for a row, or {} if missing."""
    found = backend_db.rows(
        "SELECT status, error, response FROM schedule_executions WHERE id = :id",
        id=execution_id)
    return dict(zip(("status", "error", "response"), found[0])) if found else {}


def _delete_row(execution_id: str) -> None:
    backend_db.execute("DELETE FROM schedule_executions WHERE id = :id", id=execution_id)


class TestWatchdogCleanupReportFields:
    """Tests that cleanup report includes Issue #129 watchdog fields."""

    pytestmark = pytest.mark.smoke

    def test_cleanup_status_includes_watchdog_fields(self, api_client: TrinityApiClient):
        """GET /api/monitoring/cleanup-status report includes watchdog fields."""
        response = api_client.get("/api/monitoring/cleanup-status")
        assert_status(response, 200)
        data = response.json()

        if data.get("last_report"):
            report = data["last_report"]
            assert "orphaned_executions" in report, "Missing orphaned_executions field"
            assert "auto_terminated" in report, "Missing auto_terminated field"
            assert isinstance(report["orphaned_executions"], int)
            assert isinstance(report["auto_terminated"], int)

    def test_cleanup_trigger_includes_watchdog_fields(self, api_client: TrinityApiClient):
        """POST /api/monitoring/cleanup-trigger report includes watchdog fields."""
        response = api_client.post("/api/monitoring/cleanup-trigger")
        assert_status(response, 200)
        data = assert_json_response(response)

        report = data["report"]
        assert "orphaned_executions" in report, "Missing orphaned_executions field"
        assert "auto_terminated" in report, "Missing auto_terminated field"
        assert isinstance(report["orphaned_executions"], int)
        assert isinstance(report["auto_terminated"], int)
        assert report["orphaned_executions"] >= 0
        assert report["auto_terminated"] >= 0

    def test_cleanup_total_includes_watchdog_fields(self, api_client: TrinityApiClient):
        """Cleanup total correctly sums all fields including watchdog additions."""
        response = api_client.post("/api/monitoring/cleanup-trigger")
        assert_status(response, 200)
        report = response.json()["report"]

        expected_total = (
            report["orphaned_executions"]
            + report["auto_terminated"]
            + report["stale_executions"]
            + report["no_session_executions"]
            + report["orphaned_skipped"]
            + report["stale_activities"]
            + report["stale_slots"]
        )
        assert report["total"] == expected_total

    def test_cleanup_trigger_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """Watchdog cleanup trigger requires authentication."""
        response = unauthenticated_client.post("/api/monitoring/cleanup-trigger")
        assert response.status_code in [401, 403]


# ============================================================================
# Single-Cycle Orphan Recovery (Issue #921 — agent-side completion buffer)
# ============================================================================


class TestSingleCycleOrphanRecovery:
    """Live-stack regression for #921.

    The natural-completion race (agent's `finally: unregister()` running
    before the backend writes `success`) is closed agent-side: the
    `process_registry` keeps recently-completed IDs for ~5 min and
    surfaces them via `/api/executions/running`. The watchdog unions
    those IDs into its agent-known set, so a "missing from agent" sighting
    is a true orphan and we recover on a single cycle — no two-cycle
    deferral, no Redis sentinel, no `orphans_suspected` accounting.

    This test exercises the surviving behaviour: trigger cleanup against
    a fabricated running row whose execution_id the agent has never
    heard of (not running, not recently completed) — expect a clean
    single-cycle recovery.
    """

    @pytest.fixture
    def execution_id(self):
        """Unique execution_id; teardown deletes the row even on failure."""
        eid = f"test-921-int-{uuid.uuid4().hex[:12]}"
        yield eid
        try:
            _delete_row(eid)
        except Exception:
            pass

    def test_true_orphan_recovered_on_single_cycle(
        self, api_client: TrinityApiClient, execution_id: str
    ):
        """DB has 'running' row, agent doesn't have it in either running
        OR recently_completed_ids => one cycle marks it failed.

        Under the original #921 fix this required two cycles. Closing the
        race agent-side restored single-cycle recovery — what the watchdog
        was always meant to do."""
        _insert_running_row(execution_id)
        assert _row_state(execution_id)["status"] == "running"

        response = api_client.post("/api/monitoring/cleanup-trigger")
        assert_status(response, 200)
        report = response.json()["report"]
        assert report["orphaned_executions"] >= 1

        after = _row_state(execution_id)
        assert after["status"] == "failed"
        assert "recovered by watchdog" in (after["error"] or "")
