"""#2433 — the cleanup watchdog treats a LIVE BACKEND DISPATCHER as proof of
life, and says what it observed when it does orphan a row.

Before: "running row ∧ absent from the agent ∧ age ≥ 60s" was a true orphan.
An admitted row parked in the backend agent-call queue (or queued on the agent
behind its thread pool) matched that predicate — false FAILED, released slot,
turn ran anyway. Now orphan = the agent does not know it AND no live dispatcher
owns it, read with one MGET per sweep, tri-state:
  alive   → withhold (in-process registry or cross-worker marker)
  unknown → withhold while a dispatcher COULD still own the row (Redis unreadable)
  absent  → orphan, with an honest error string

Covers ``_extract_agent_known_ids`` (+ ``pending_ids``), ``_inflight_verdict_map``
(stub-leak guards), ``_inflight_skip``, ``_orphan_error_message``,
``_reconcile_orphaned_executions``, ``_process_stale_slot_reclaims``, the
startup ``recover_orphaned_executions`` path and ``CleanupReport``.

Module under test: src/backend/services/cleanup_service.py

Harness note: ``services.cleanup_service`` is imported INSIDE each test (the
``test_watchdog_unit.py`` shape). The unit conftest pops ``services.*`` between
collection and test, so a module-level reference would be a stale object that
``patch("services.cleanup_service.X")`` never touches — the reconcile would
then read a MagicMock ``db``, iterate nothing and return silently.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_backend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)


def _cs():
    from services import cleanup_service as cs  # noqa: WPS433 — lazy on purpose (see module docstring)
    return cs


def _past_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mock_httpx_cm():
    mock_client = AsyncMock()
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_cm


# ---------------------------------------------------------------------------
# _extract_agent_known_ids
# ---------------------------------------------------------------------------

class TestExtractAgentKnownIds:
    def test_unions_pending_ids_and_tags_capability(self):
        cs = _cs()
        ids = cs._extract_agent_known_ids({
            "executions": [{"execution_id": "run-1"}],
            "recently_completed_ids": ["done-1"],
            "pending_ids": ["pend-1"],
        })
        assert ids == {"run-1", "done-1", "pend-1"}
        assert ids.reports_pending is True

    def test_old_image_without_pending_degrades(self):
        cs = _cs()
        ids = cs._extract_agent_known_ids({"executions": [{"execution_id": "run-1"}]})
        assert ids == {"run-1"}
        assert ids.reports_pending is False

    def test_non_list_fields_are_ignored_not_iterated_as_characters(self):
        cs = _cs()
        ids = cs._extract_agent_known_ids({
            "executions": [{"execution_id": "run-1"}, "garbage", None],
            "recently_completed_ids": "abc",
            "pending_ids": {"nested": True},
        })
        assert ids == {"run-1"}
        assert ids.reports_pending is False


# ---------------------------------------------------------------------------
# verdict helpers
# ---------------------------------------------------------------------------

class TestVerdictHelpers:
    def test_verdict_map_passes_through_valid_verdicts(self):
        cs = _cs()
        with patch.object(cs, "_inflight_verdicts", AsyncMock(return_value={"a": "alive", "b": "unknown", "c": "absent"})):
            out = asyncio.run(cs._inflight_verdict_map(["a", "b", "c", "", None]))
        assert out == {"a": "alive", "b": "unknown", "c": "absent"}

    def test_verdict_map_guards_against_stub_leaks(self):
        cs = _cs()
        with patch.object(cs, "_inflight_verdicts", AsyncMock(return_value=MagicMock())):
            assert asyncio.run(cs._inflight_verdict_map(["a"])) == {"a": "absent"}
        with patch.object(cs, "_inflight_verdicts", AsyncMock(return_value={"a": MagicMock()})):
            assert asyncio.run(cs._inflight_verdict_map(["a"])) == {"a": "absent"}

    def test_verdict_map_failure_is_fail_open(self):
        cs = _cs()
        with patch.object(cs, "_inflight_verdicts", AsyncMock(side_effect=RuntimeError("boom"))):
            assert asyncio.run(cs._inflight_verdict_map(["a"])) == {"a": "absent"}

    def test_inflight_skip_rules(self):
        cs = _cs()
        with patch.object(cs.agent_call_limiter, "inflight_max_age_seconds", return_value=1000.0):
            assert cs._inflight_skip("alive", 99999) is True
            assert cs._inflight_skip("unknown", 500) is True
            assert cs._inflight_skip("unknown", 5000) is False
            assert cs._inflight_skip("absent", 10) is False

    def test_orphan_message_states_observations(self):
        cs = _cs()
        msg = cs._orphan_error_message("agent-a", agent_reports_pending=True)
        assert "not tracked by agent 'agent-a'" in msg
        assert "not pending" in msg
        assert "no live backend dispatcher" in msg
        assert "completed on agent" not in msg
        old = cs._orphan_error_message("agent-a", agent_reports_pending=False)
        assert "not pending" not in old

    def test_report_counter_in_dict_not_in_total(self):
        cs = _cs()
        report = cs.CleanupReport()
        report.dispatch_inflight_skipped = 3
        assert report.to_dict()["dispatch_inflight_skipped"] == 3
        assert report.total == 0


# ---------------------------------------------------------------------------
# _reconcile_orphaned_executions
# ---------------------------------------------------------------------------

class TestReconcileWithInflight:
    def _run(self, mock_db, mock_capacity_fn, mock_httpx, verdicts, *, age=600, known=None, bound=100000.0):
        cs = _cs()
        mock_httpx.return_value = _mock_httpx_cm()
        mock_db.get_running_executions_with_agent_info.return_value = [
            {"id": "exec-1", "agent_name": "agent-a", "started_at": _past_iso(age), "timeout_seconds": 900, "schedule_id": "s1"},
        ]
        mock_db.mark_execution_failed_by_watchdog.return_value = True
        mock_capacity = AsyncMock()
        mock_capacity_fn.return_value = mock_capacity
        service = cs.CleanupService()
        service._get_agent_running_ids = AsyncMock(
            return_value=known if known is not None
            else cs._extract_agent_known_ids({"executions": [], "pending_ids": []})
        )
        service._broadcast_watchdog_event = AsyncMock()
        report = cs.CleanupReport()
        with (
            patch.object(cs, "_inflight_verdicts", AsyncMock(return_value=verdicts)),
            patch.object(cs.agent_call_limiter, "inflight_max_age_seconds", return_value=bound),
        ):
            orphaned, terminated, confirmed = asyncio.run(service._reconcile_orphaned_executions(report))
        return orphaned, report, mock_capacity

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_capacity_manager")
    def test_live_dispatcher_withholds_recovery_and_slot(self, cap_fn, mock_db, mock_httpx):
        orphaned, report, capacity = self._run(mock_db, cap_fn, mock_httpx, {"exec-1": "alive"})
        assert orphaned == 0
        assert report.dispatch_inflight_skipped == 1
        mock_db.mark_execution_failed_by_watchdog.assert_not_called()
        capacity.release_if_matches.assert_not_called()

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_capacity_manager")
    def test_absent_everywhere_is_orphaned_with_honest_string(self, cap_fn, mock_db, mock_httpx):
        orphaned, report, capacity = self._run(mock_db, cap_fn, mock_httpx, {"exec-1": "absent"})
        assert orphaned == 1
        assert report.dispatch_inflight_skipped == 0
        args, _ = mock_db.mark_execution_failed_by_watchdog.call_args
        assert "not tracked by agent 'agent-a'" in args[1]
        assert "not pending" in args[1]
        assert "no live backend dispatcher" in args[1]
        capacity.release_if_matches.assert_called_once_with("agent-a", "exec-1")

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_capacity_manager")
    def test_unknown_withholds_only_while_a_dispatcher_could_own_it(self, cap_fn, mock_db, mock_httpx):
        orphaned, report, _ = self._run(mock_db, cap_fn, mock_httpx, {"exec-1": "unknown"}, age=600, bound=1000.0)
        assert orphaned == 0 and report.dispatch_inflight_skipped == 1
        mock_db.reset_mock()
        orphaned, report, _ = self._run(mock_db, cap_fn, mock_httpx, {"exec-1": "unknown"}, age=5000, bound=1000.0)
        assert orphaned == 1 and report.dispatch_inflight_skipped == 0

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_capacity_manager")
    def test_pending_on_agent_is_proof_of_life(self, cap_fn, mock_db, mock_httpx):
        cs = _cs()
        known = cs._extract_agent_known_ids({"executions": [], "pending_ids": ["exec-1"]})
        orphaned, report, _ = self._run(mock_db, cap_fn, mock_httpx, {}, known=known)
        assert orphaned == 0
        mock_db.mark_execution_failed_by_watchdog.assert_not_called()

    @patch("services.cleanup_service.httpx.AsyncClient")
    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_capacity_manager")
    def test_old_image_string_does_not_claim_pending_was_checked(self, cap_fn, mock_db, mock_httpx):
        cs = _cs()
        known = cs._extract_agent_known_ids({"executions": []})
        self._run(mock_db, cap_fn, mock_httpx, {"exec-1": "absent"}, known=known)
        args, _ = mock_db.mark_execution_failed_by_watchdog.call_args
        assert "not pending" not in args[1]


# ---------------------------------------------------------------------------
# _process_stale_slot_reclaims
# ---------------------------------------------------------------------------

class TestStaleSlotReclaimWithInflight:
    def _run(self, mock_db, verdict):
        cs = _cs()
        mock_db.fail_stale_slot_execution.return_value = True
        service = cs.CleanupService()
        service._get_agent_running_ids = AsyncMock(return_value=set())
        service._terminate_on_agent = AsyncMock(return_value=True)
        service._close_stale_slot_activity = AsyncMock()
        report = cs.CleanupReport()
        with (
            patch.object(cs, "_inflight_verdicts", AsyncMock(return_value={"exec-1": verdict})),
            patch.object(cs.event_dispatch_service, "spawn_task_terminal_event", MagicMock()),
            patch("services.cleanup_service.httpx.AsyncClient", return_value=_mock_httpx_cm()),
        ):
            asyncio.run(service._process_stale_slot_reclaims({"agent-a": ["exec-1"]}, set(), report))
        return report

    @patch("services.cleanup_service.db")
    def test_live_dispatcher_is_not_stale(self, mock_db):
        report = self._run(mock_db, "alive")
        mock_db.fail_stale_slot_execution.assert_not_called()
        assert report.dispatch_inflight_skipped == 1

    @patch("services.cleanup_service.db")
    def test_absent_fails_as_before(self, mock_db):
        report = self._run(mock_db, "absent")
        mock_db.fail_stale_slot_execution.assert_called_once()
        assert report.stale_slot_executions == 1


# ---------------------------------------------------------------------------
# startup recovery
# ---------------------------------------------------------------------------

class TestStartupRecoveryWithInflight:
    def _run(self, mock_db, cap_fn, verdict):
        cs = _cs()
        mock_db.get_running_executions.return_value = [
            {"id": "exec-1", "agent_name": "agent-a", "started_at": _past_iso(600)},
        ]
        mock_db.mark_execution_failed_by_watchdog.return_value = True
        cap_fn.return_value = AsyncMock()
        container = MagicMock(status="running")
        client = MagicMock()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"executions": [], "pending_ids": []}
        client.get = AsyncMock(return_value=resp)
        with (
            patch("services.docker_service.get_agent_container", return_value=container),
            patch("services.agent_client.get_agent_client", return_value=client),
            patch.object(cs, "_reconcile_orphaned_slots", AsyncMock(return_value={})),
            patch.object(cs, "_inflight_verdicts", AsyncMock(return_value={"exec-1": verdict})),
            patch.object(cs.agent_call_limiter, "inflight_max_age_seconds", return_value=100000.0),
        ):
            return asyncio.run(cs.recover_orphaned_executions())

    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_capacity_manager")
    def test_other_workers_live_dispatcher_counts_as_still_running(self, cap_fn, mock_db):
        result = self._run(mock_db, cap_fn, "alive")
        assert result["still_running"] == 1 and result["recovered"] == 0
        mock_db.mark_execution_failed_by_watchdog.assert_not_called()

    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_capacity_manager")
    def test_absent_is_recovered_on_startup(self, cap_fn, mock_db):
        result = self._run(mock_db, cap_fn, "absent")
        assert result["recovered"] == 1
