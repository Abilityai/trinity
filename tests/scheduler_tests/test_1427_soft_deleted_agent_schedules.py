"""Soft-deleted agent's schedule stops firing on the live-sync path (#1427).

Soft-deleting an agent leaves its child schedule row untouched (enabled=1,
deleted_at NULL, same updated_at). The sync loop's `list_all_schedules()` used
to filter only the schedule's own `deleted_at`, so the transition diff saw no
change and never removed the already-registered APScheduler job — it kept
firing into the now-nonexistent container (ConnectError every cron tick, 26
failures on eu2). `list_all_schedules()` now reports such a schedule as
disabled (folding the agent's `agent_ownership.deleted_at`), so the existing
enabled→disabled transition removes the job, and agent recovery flips it back.
"""

import sys
import sqlite3
from pathlib import Path

_src_path = str(Path(__file__).resolve().parent.parent.parent / "src")
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

import pytest  # noqa: E402


def _set_agent_deleted(db_path: str, agent_name: str, deleted_at):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE agent_ownership SET deleted_at = ? WHERE agent_name = ?",
            (deleted_at, agent_name),
        )
        conn.commit()
    finally:
        conn.close()


def _enabled_by_id(schedules):
    return {s.id: s.enabled for s in schedules}


class TestSoftDeletedAgentSchedules:
    def test_live_agent_schedules_report_enabled(self, db_with_data):
        en = _enabled_by_id(db_with_data.list_all_schedules())
        assert en["schedule-1"] is True
        assert en["schedule-2"] is True
        assert en["schedule-3"] is False  # was created disabled

    def test_soft_deleted_agent_schedules_report_disabled(self, db_with_data):
        """The fix: enabled schedules of a soft-deleted agent flip to disabled in
        the sync view (→ the sync removes their jobs), without touching the rows."""
        _set_agent_deleted(db_with_data.database_path, "test-agent", "2026-06-29T00:00:00Z")

        en = _enabled_by_id(db_with_data.list_all_schedules())
        # Present in the sync set (row not deleted) but reported disabled → jobs removed.
        assert en["schedule-1"] is False
        assert en["schedule-2"] is False
        assert en["schedule-3"] is False

        # The rows themselves are untouched (recoverable per #834).
        conn = sqlite3.connect(db_with_data.database_path)
        try:
            rows = dict(conn.execute(
                "SELECT id, enabled FROM agent_schedules WHERE agent_name='test-agent'"
            ).fetchall())
        finally:
            conn.close()
        assert rows["schedule-1"] == 1 and rows["schedule-2"] == 1  # DB enabled unchanged

    def test_recovery_reenables_in_sync_view(self, db_with_data):
        """Clearing agent_ownership.deleted_at (agent recovery) flips the schedules
        back to enabled → the disabled→enabled transition re-adds the jobs."""
        path = db_with_data.database_path
        _set_agent_deleted(path, "test-agent", "2026-06-29T00:00:00Z")
        assert _enabled_by_id(db_with_data.list_all_schedules())["schedule-1"] is False

        _set_agent_deleted(path, "test-agent", None)  # recover
        en = _enabled_by_id(db_with_data.list_all_schedules())
        assert en["schedule-1"] is True
        assert en["schedule-2"] is True

    def test_soft_deleted_agent_excluded_from_enabled_startup_set(self, db_with_data):
        """Sanity: the startup load (`list_all_enabled_schedules`) already drops
        them entirely — this test guards that the two paths agree."""
        _set_agent_deleted(db_with_data.database_path, "test-agent", "2026-06-29T00:00:00Z")
        ids = {s.id for s in db_with_data.list_all_enabled_schedules()}
        assert "schedule-1" not in ids and "schedule-2" not in ids
