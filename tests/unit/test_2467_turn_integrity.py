"""#2467 — turn-integrity derivation: a background task killed at CLI exit
leaves a queryable trace instead of a clean lying ``success``.

Backend-side fix, deliberately: the kill lifecycle events (``task_updated
{"status": "killed"}`` + ``task_notification {"status": "stopped"}``) already
ride ``execution_log`` on every deployed agent image, so
``services/execution_integrity.py`` derives the structured record + response
notice at terminal write (``apply_result``) and persists it to the new
``schedule_executions.turn_integrity`` column — no base-image rebuild (the
#1741 ``extract_tool_calls`` precedent). The agent side is byte-identical,
pinned by ``tests/unit/test_2467_bg_kill_agent_negative_controls.py``.

The incident-stream dicts are DUPLICATED from that file rather than imported:
sibling test modules evict ``agent_server*`` from ``sys.modules`` (learnings
2026-07-07), and these are backend tests that must not import the agent tree.

The design trap these tests pin (proven in the agent-side file): the CLI
empties the ledger (``background_tasks_changed []``) BEFORE exiting, so any
detector keyed on ledger snapshots reads 0 at finalize — only the kill
lifecycle events carry the truth.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services.execution_integrity import (  # noqa: E402
    collect_killed_bg_tasks,
    derive_turn_integrity,
    killed_notice,
)

# ---------------------------------------------------------------------------
# Incident stream (sanitized), as the backend receives it: parsed dicts in
# execution_log. Transcribed from the 2026-09-01 live capture.
# ---------------------------------------------------------------------------

ANNOUNCEMENT = "Report delivery moved to background. Waiting for completion notification."


def _incident_log(*, promoted_by_timeout: bool, killed: bool = True):
    """The incident tail. ``promoted_by_timeout=True`` is Incident A (a
    FOREGROUND Bash call promoted by the harness at its tool timeout);
    ``False`` is Incident B (the model passed ``run_in_background=true``).
    ``killed=False`` is the AC-6 negative control: the same stream where the
    task COMPLETES before exit — an implementation that flags on the mere
    presence of a background task must fail on it."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "sess-repro",
         "permissionMode": "bypassPermissions"},
        {"type": "assistant", "message": {"role": "assistant",
         "content": [{"type": "text", "text": "Now delivering the report."}]}},
        {"type": "system", "subtype": "background_tasks_changed",
         "tasks": [{"task_id": "bg1", "task_type": "local_bash",
                    "description": "Deliver report"}]},
    ]
    if promoted_by_timeout:
        events.append({"type": "system", "subtype": "task_updated",
                       "task_id": "bg1", "patch": {"is_backgrounded": True}})
    events += [
        {"type": "assistant", "message": {"role": "assistant",
         "content": [{"type": "text", "text": ANNOUNCEMENT}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "terminal_reason": "completed", "result": ANNOUNCEMENT},
        {"type": "system", "subtype": "background_tasks_changed", "tasks": []},
    ]
    if killed:
        events += [
            {"type": "system", "subtype": "task_updated", "task_id": "bg1",
             "patch": {"status": "killed", "end_time": 1788170254093}},
            {"type": "system", "subtype": "task_notification", "task_id": "bg1",
             "status": "stopped",
             "output_file": "/home/developer/.tmp/tasks/bg1.output"},
        ]
    else:
        events += [
            {"type": "system", "subtype": "task_updated", "task_id": "bg1",
             "patch": {"status": "completed", "end_time": 1788170254093}},
            {"type": "system", "subtype": "task_notification", "task_id": "bg1",
             "status": "completed",
             "output_file": "/home/developer/.tmp/tasks/bg1.output"},
        ]
    return events


# ---------------------------------------------------------------------------
# collect_killed_bg_tasks — the pure scan
# ---------------------------------------------------------------------------


class TestCollectKilledBgTasks:
    pytestmark = pytest.mark.unit

    def test_incident_a_promotion_flagged_as_tool_timeout(self):
        entries = collect_killed_bg_tasks(_incident_log(promoted_by_timeout=True))
        assert entries == [{
            "task_id": "bg1",
            "task_type": "local_bash",
            "was_backgrounded_by": "tool_timeout",
            "final_status": "killed",
            "end_time": 1788170254093,
        }]

    def test_incident_b_requested_flagged_as_requested(self):
        entries = collect_killed_bg_tasks(_incident_log(promoted_by_timeout=False))
        assert len(entries) == 1
        assert entries[0]["was_backgrounded_by"] == "requested"
        assert entries[0]["final_status"] == "killed"

    def test_negative_control_completed_task_is_not_flagged(self):
        """AC #6: the same stream with ``completed`` instead of ``killed``
        (and a non-stopped notification) produces nothing — mere presence of a
        background task never flags."""
        assert collect_killed_bg_tasks(
            _incident_log(promoted_by_timeout=True, killed=False)
        ) == []

    def test_privacy_description_and_output_file_never_persisted(self):
        """#2127 privacy rule: only structural fields reach the record."""
        blob = json.dumps(collect_killed_bg_tasks(_incident_log(promoted_by_timeout=True)))
        assert "Deliver report" not in blob
        assert "output_file" not in blob
        assert "/home/developer" not in blob

    def test_ledger_drain_does_not_erase_the_kill(self):
        """The design trap: ``background_tasks_changed []`` precedes the kill
        events. The scan keys on the kill events, so the drained ledger is
        irrelevant — this is the property the ledger-widening 'fix' lacks."""
        log = _incident_log(promoted_by_timeout=False)
        assert any(
            e.get("subtype") == "background_tasks_changed" and e.get("tasks") == []
            for e in log
        )
        assert len(collect_killed_bg_tasks(log)) == 1

    def test_mixed_stream_lists_only_the_killed_task(self):
        log = [
            {"type": "system", "subtype": "background_tasks_changed",
             "tasks": [{"task_id": "a", "task_type": "local_bash"},
                       {"task_id": "b", "task_type": "local_bash"}]},
            {"type": "system", "subtype": "task_updated", "task_id": "a",
             "patch": {"status": "completed"}},
            {"type": "system", "subtype": "task_updated", "task_id": "b",
             "patch": {"status": "killed"}},
        ]
        entries = collect_killed_bg_tasks(log)
        assert [e["task_id"] for e in entries] == ["b"]

    def test_duplicate_kill_events_dedupe_to_one_entry(self):
        log = [
            {"type": "system", "subtype": "task_updated", "task_id": "x",
             "patch": {"status": "killed"}},
            {"type": "system", "subtype": "task_updated", "task_id": "x",
             "patch": {"status": "killed"}},
            {"type": "system", "subtype": "task_notification", "task_id": "x",
             "status": "stopped"},
        ]
        assert len(collect_killed_bg_tasks(log)) == 1

    def test_kill_without_snapshot_still_flags_with_unknown_type(self):
        """A dropped/unparseable snapshot line must not hide the kill — a
        false negative is the bug itself."""
        log = [{"type": "system", "subtype": "task_updated", "task_id": "ghost",
                "patch": {"status": "killed"}}]
        entries = collect_killed_bg_tasks(log)
        assert entries[0]["task_type"] == "unknown"
        assert entries[0]["final_status"] == "killed"

    def test_notification_stopped_alone_flags_as_stopped(self):
        """Belt against a dropped ``task_updated`` line: the constant-companion
        notification still flags, with the honest weaker final_status."""
        log = [{"type": "system", "subtype": "task_notification",
                "task_id": "n1", "status": "stopped"}]
        entries = collect_killed_bg_tasks(log)
        assert entries[0]["final_status"] == "stopped"

    def test_killed_wins_final_status_regardless_of_event_order(self):
        stopped_then_killed = [
            {"type": "system", "subtype": "task_notification", "task_id": "t",
             "status": "stopped"},
            {"type": "system", "subtype": "task_updated", "task_id": "t",
             "patch": {"status": "killed"}},
        ]
        assert collect_killed_bg_tasks(stopped_then_killed)[0]["final_status"] == "killed"
        killed_then_stopped = list(reversed(stopped_then_killed))
        assert collect_killed_bg_tasks(killed_then_stopped)[0]["final_status"] == "killed"

    def test_malformed_shapes_degrade_to_no_entry_never_crash(self):
        log = [
            "not a dict",
            {"type": "system"},  # no subtype
            {"type": "system", "subtype": "task_updated"},  # no task_id/patch
            {"type": "system", "subtype": "task_updated", "task_id": 42,
             "patch": {"status": "killed"}},  # non-str id
            {"type": "system", "subtype": "task_updated", "task_id": "y",
             "patch": "killed"},  # patch not a dict
            {"type": "system", "subtype": "background_tasks_changed",
             "tasks": {"task_id": "z"}},  # tasks not a list
            {"type": "system", "subtype": "task_notification", "task_id": "y",
             "status": "completed"},  # non-stopped notification
        ]
        assert collect_killed_bg_tasks(log) == []

    def test_non_list_execution_log_returns_empty(self):
        for bad in (None, "log", {"a": 1}, 42):
            assert collect_killed_bg_tasks(bad) == []

    def test_unknown_task_status_is_not_flagged(self):
        log = [{"type": "system", "subtype": "task_updated", "task_id": "p",
                "patch": {"status": "paused"}}]
        assert collect_killed_bg_tasks(log) == []

    def test_invalid_charset_task_id_replaced_not_dropped(self):
        """The flag must survive a mangled/forged id, but the id must not
        reach the column (a stdio MCP child shares the pipe, #640)."""
        log = [{"type": "system", "subtype": "task_updated",
                "task_id": "bg1;<script>alert(1)</script>",
                "patch": {"status": "killed"}}]
        entries = collect_killed_bg_tasks(log)
        assert entries[0]["task_id"] == "invalid"
        assert "<script>" not in json.dumps(entries)

    def test_entry_list_capped_at_twenty(self):
        log = [
            {"type": "system", "subtype": "task_updated", "task_id": f"t{i}",
             "patch": {"status": "killed"}}
            for i in range(25)
        ]
        assert len(collect_killed_bg_tasks(log)) == 20

    def test_bool_end_time_is_not_recorded(self):
        log = [{"type": "system", "subtype": "task_updated", "task_id": "t",
                "patch": {"status": "killed", "end_time": True}}]
        assert collect_killed_bg_tasks(log)[0]["end_time"] is None


# ---------------------------------------------------------------------------
# killed_notice — the visible warning
# ---------------------------------------------------------------------------


class TestKilledNotice:
    pytestmark = pytest.mark.unit

    def test_empty_entries_yield_no_notice(self):
        assert killed_notice([]) is None

    def test_notice_names_count_and_types_never_task_ids(self):
        entries = collect_killed_bg_tasks(_incident_log(promoted_by_timeout=False))
        notice = killed_notice(entries)
        assert "1 background task" in notice
        assert "local_bash" in notice
        assert "bg1" not in notice  # ids never reach prose
        assert "(#2467)" in notice

    def test_promotion_sentence_only_for_tool_timeout(self):
        promoted = collect_killed_bg_tasks(_incident_log(promoted_by_timeout=True))
        requested = collect_killed_bg_tasks(_incident_log(promoted_by_timeout=False))
        assert "auto-promoted" in killed_notice(promoted)
        assert "auto-promoted" not in killed_notice(requested)


# ---------------------------------------------------------------------------
# derive_turn_integrity — the column value + notice pair
# ---------------------------------------------------------------------------


class TestDeriveTurnIntegrity:
    pytestmark = pytest.mark.unit

    def test_healthy_run_yields_none_none(self):
        """AC #4: the happy path stays byte-identical — nothing derived."""
        log = [{"type": "result", "subtype": "success"},
               {"type": "tool_use", "name": "Bash"}]
        assert derive_turn_integrity(log, {"cost_usd": 0.1}) == (None, None)

    def test_negative_control_completed_yields_none_none(self):
        log = _incident_log(promoted_by_timeout=True, killed=False)
        assert derive_turn_integrity(log, {}) == (None, None)

    def test_killed_yields_json_record_and_notice(self):
        log = _incident_log(promoted_by_timeout=True)
        integrity_json, notice = derive_turn_integrity(log, {})
        payload = json.loads(integrity_json)
        assert payload["background_tasks_killed"][0]["was_backgrounded_by"] == "tool_timeout"
        assert notice.startswith("> ⚠️ Background work lost")

    def test_waited_path_pending_persists_without_notice(self):
        """Root cause 3: the #2127 waited-path counter reported in metadata was
        persisted nowhere. It rides the same channel — but produces NO backend
        notice (the agent-side #2127 notice is already inside the response;
        a second one would double up)."""
        integrity_json, notice = derive_turn_integrity(
            [], {"background_tasks_pending_at_exit": 3}
        )
        assert json.loads(integrity_json) == {"background_tasks_pending_at_exit": 3}
        assert notice is None

    def test_pending_bool_zero_negative_and_non_dict_are_ignored(self):
        assert derive_turn_integrity([], {"background_tasks_pending_at_exit": True}) == (None, None)
        assert derive_turn_integrity([], {"background_tasks_pending_at_exit": 0}) == (None, None)
        assert derive_turn_integrity([], {"background_tasks_pending_at_exit": -2}) == (None, None)
        assert derive_turn_integrity([], None) == (None, None)
        assert derive_turn_integrity([], "meta") == (None, None)

    def test_pending_forged_huge_value_is_capped(self):
        integrity_json, _ = derive_turn_integrity(
            [], {"background_tasks_pending_at_exit": 10**9}
        )
        assert json.loads(integrity_json)["background_tasks_pending_at_exit"] == 1000

    def test_killed_and_pending_share_one_object(self):
        log = _incident_log(promoted_by_timeout=False)
        integrity_json, notice = derive_turn_integrity(
            log, {"background_tasks_pending_at_exit": 2}
        )
        payload = json.loads(integrity_json)
        assert set(payload) == {"background_tasks_killed",
                                "background_tasks_pending_at_exit"}
        assert notice is not None


# ---------------------------------------------------------------------------
# apply_result integration — the terminal write carries the flag + notice
# (harness mirrors tests/unit/test_1083_apply_result.py)
# ---------------------------------------------------------------------------


def _await(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _run_apply(envelope):
    from services.task_execution_service import TaskExecutionService

    mock_db = MagicMock()
    mock_db.update_execution_status.return_value = True

    mock_activity = MagicMock(complete_activity=AsyncMock())
    mock_capacity = MagicMock(release=AsyncMock())
    mock_record = AsyncMock()

    with (
        patch("services.task_execution_service.db", mock_db),
        patch("services.task_execution_service.get_capacity_manager", return_value=mock_capacity),
        patch("services.task_execution_service.activity_service", mock_activity),
        patch("services.task_execution_service._record_dispatch_terminal", mock_record),
    ):
        svc = TaskExecutionService()
        result = _await(
            svc.apply_result("test-agent", envelope, activity_id="act-1")
        )
    return result, mock_db


def _success_envelope(**over):
    from services.task_execution_service import TerminalEnvelope, TaskExecutionStatus

    base = dict(
        execution_id="exec-2467",
        status=TaskExecutionStatus.SUCCESS,
        response=ANNOUNCEMENT,
        metadata={"cost_usd": 0.11, "input_tokens": 100, "context_window": 200000,
                  "session_id": "meta-sess"},
        execution_log=_incident_log(promoted_by_timeout=False),
        session_id="resp-sess",
        execution_time_ms=6000,
    )
    base.update(over)
    return TerminalEnvelope(**base)


class TestApplyResultIntegration:
    pytestmark = pytest.mark.unit

    def test_kill_tail_writes_turn_integrity_and_prepends_notice(self):
        result, mdb = _run_apply(_success_envelope())
        kw = mdb.update_execution_status.call_args.kwargs
        payload = json.loads(kw["turn_integrity"])
        assert payload["background_tasks_killed"][0]["final_status"] == "killed"
        # The stored response AND the returned result both carry the notice,
        # with the model's announcement preserved after it.
        assert kw["response"].startswith("> ⚠️ Background work lost")
        assert ANNOUNCEMENT in kw["response"]
        assert result.response == kw["response"]

    def test_healthy_run_is_byte_identical(self):
        """AC #4: no kill events → turn_integrity is None (the conditional
        kwarg leaves the column untouched) and the response is untouched."""
        result, mdb = _run_apply(_success_envelope(
            response="all done",
            execution_log=[{"type": "tool_use", "name": "Bash"}],
        ))
        kw = mdb.update_execution_status.call_args.kwargs
        assert kw["turn_integrity"] is None
        assert kw["response"] == "all done"
        assert result.response == "all done"

    def test_old_image_without_execution_log_degrades_to_none(self):
        """Mixed fleet: an envelope with no transcript (or a very old image)
        writes NULL — 'no evidence', never a crash, never 'verified healthy'."""
        _, mdb = _run_apply(_success_envelope(response="ok", execution_log=None))
        assert mdb.update_execution_status.call_args.kwargs["turn_integrity"] is None

    def test_waited_path_pending_rides_the_column_without_notice(self):
        _, mdb = _run_apply(_success_envelope(
            response="fan-out interim",
            execution_log=[{"type": "result", "subtype": "success"}],
            metadata={"cost_usd": 0.05, "context_window": 200000,
                      "background_tasks_pending_at_exit": 2},
        ))
        kw = mdb.update_execution_status.call_args.kwargs
        assert json.loads(kw["turn_integrity"]) == {
            "background_tasks_pending_at_exit": 2
        }
        assert kw["response"] == "fan-out interim"  # no backend notice


class TestFacadeSignatureParity:
    """The `database.py` DatabaseManager facade forwards
    `update_execution_status` with an EXPLICIT signature — a kwarg added to the
    ScheduleExecutionsMixin but not the facade raises `unexpected keyword
    argument` at the first real terminal write while every mocked-db unit test
    stays green (caught live during #2467's E2E verification)."""

    pytestmark = pytest.mark.unit

    def test_facade_and_mixin_signatures_match(self):
        import inspect

        from database import DatabaseManager
        from db.schedules import ScheduleOperations

        facade = inspect.signature(DatabaseManager.update_execution_status)
        mixin = inspect.signature(ScheduleOperations.update_execution_status)
        assert list(facade.parameters) == list(mixin.parameters), (
            "DatabaseManager.update_execution_status must forward every "
            "parameter ScheduleOperations.update_execution_status accepts"
        )


def _recent_iso(*, minutes_ago: int) -> str:
    """A fresh ISO-Z timestamp shortly in the past, matching utc_now_iso()'s
    format — rows built from it always sit inside a rolling hours=24 window."""
    from datetime import datetime, timedelta, timezone
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class TestListReadersCarryTheColumn:
    """AC #2's real surfaces: BOTH list readers with explicit column lists must
    return `turn_integrity` — `get_fleet_executions` (raw-SQL SELECT) and
    `get_agent_executions_summary` (SQLAlchemy select with an enumerated column
    list, which shipped WITHOUT the column on the first pass of #2467; caught
    in review — the 2026-08-21 'every existing reader' learnings class).
    Drives the REAL ScheduleOperations against a temp SQLite file (the
    test_1474_read_boundary_z harness shape)."""

    pytestmark = pytest.mark.unit

    _TI = '{"background_tasks_killed": [{"task_id": "bg1"}]}'

    @pytest.fixture
    def ops(self, tmp_path, monkeypatch):
        import sqlite3

        db_path = tmp_path / "trinity.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE schedule_executions (
                id TEXT PRIMARY KEY,
                schedule_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                duration_ms INTEGER,
                message TEXT NOT NULL DEFAULT '',
                response TEXT,
                error TEXT,
                triggered_by TEXT NOT NULL DEFAULT 'schedule',
                context_used INTEGER,
                context_max INTEGER,
                cost REAL,
                tool_calls TEXT,
                execution_log TEXT,
                claude_session_id TEXT,
                source_user_id INTEGER,
                source_user_email TEXT,
                source_agent_name TEXT,
                source_mcp_key_id TEXT,
                source_mcp_key_name TEXT,
                model_used TEXT,
                fan_out_id TEXT,
                business_status TEXT,
                validation_execution_id TEXT,
                turn_integrity TEXT,
                queued_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO schedule_executions(id, schedule_id, agent_name, status, "
            "started_at, completed_at, message, triggered_by, turn_integrity) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            # Timestamps are derived from NOW, never hardcoded: get_fleet_executions
            # defaults to hours=24, so a fixed started_at turned this into a time
            # bomb — green until the calendar caught up, then red on every branch
            # (first fired 2026-09-02T10:00Z, fleet-list arm only; the agent-summary
            # arm has no window and kept passing).
            ("e-ti", "s1", "agent-ti", "success",
             _recent_iso(minutes_ago=10), _recent_iso(minutes_ago=9),
             "m", "schedule", self._TI),
        )
        conn.commit()
        conn.close()
        monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))
        monkeypatch.delitem(sys.modules, "db.connection", raising=False)
        try:
            import db.connection as connection_mod
        except ImportError:
            pytest.skip("backend venv required")
        monkeypatch.setattr(connection_mod, "DB_PATH", str(db_path))
        try:
            from db.schedules import ScheduleOperations
            from db.users import UserOperations
            from db.agents import AgentOperations
        except ImportError:
            pytest.skip("backend venv required")
        user_ops = UserOperations()
        agent_ops = AgentOperations(user_ops)
        return ScheduleOperations(user_ops, agent_ops)

    def test_agent_summary_list_returns_turn_integrity(self, ops):
        rows = ops.get_agent_executions_summary("agent-ti", limit=10)
        assert len(rows) == 1
        assert rows[0]["turn_integrity"] == self._TI

    def test_fleet_list_returns_turn_integrity(self, ops):
        rows = ops.get_fleet_executions(agent_names=None, limit=10)
        assert len(rows) == 1
        assert rows[0]["turn_integrity"] == self._TI
