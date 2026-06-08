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

import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_json_response,
)

_BACKEND_CONTAINER = os.getenv("TRINITY_BACKEND_CONTAINER", "trinity-backend")
# trinity-system is a stable, always-running agent on every Trinity instance.
# Its /api/executions/running endpoint returns an empty list when nothing's
# in flight, which is the exact state the watchdog sees during the race.
_SENTINEL_AGENT = "trinity-system"


def _exec_backend(python_code: str) -> str:
    """Run Python inside the trinity-backend container — used for DB/Redis
    fixture setup. Mirrors the pattern in test_operator_queue.py."""
    result = subprocess.run(
        ["docker", "exec", _BACKEND_CONTAINER, "python3", "-c", python_code],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Backend exec failed: {result.stderr}")
    return result.stdout.strip()


def _insert_running_row(execution_id: str, age_seconds: int = 120) -> None:
    """Insert a schedule_executions row that looks like an in-flight task.

    `claude_session_id` is set so the #106 no-session fast-fail path doesn't
    fire before the orphan-recovery path can run.
    """
    started_at = (
        datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    _exec_backend(f"""
import sqlite3
c = sqlite3.connect('/data/trinity.db')
c.execute(
    'INSERT INTO schedule_executions (id, schedule_id, agent_name, status, '
    'started_at, message, triggered_by, claude_session_id) '
    'VALUES (?,?,?,?,?,?,?,?)',
    ('{execution_id}', 'sched-921-int', '{_SENTINEL_AGENT}', 'running',
     '{started_at}', 'integration #921', 'manual', 'sess-921-int'),
)
c.commit(); c.close()
print('OK')
""")


def _row_state(execution_id: str) -> dict:
    """Return {status, error, response} for a row, or {} if missing."""
    out = _exec_backend(f"""
import json, sqlite3
c = sqlite3.connect('/data/trinity.db'); c.row_factory = sqlite3.Row
r = c.execute('SELECT status, error, response FROM schedule_executions WHERE id=?', ('{execution_id}',)).fetchone()
print(json.dumps(dict(r) if r else {{}}))
c.close()
""")
    import json as _json
    return _json.loads(out)


def _delete_row(execution_id: str) -> None:
    _exec_backend(f"""
import sqlite3
c = sqlite3.connect('/data/trinity.db')
c.execute('DELETE FROM schedule_executions WHERE id=?', ('{execution_id}',))
c.commit(); c.close()
print('OK')
""")


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
