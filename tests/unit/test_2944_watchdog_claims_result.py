"""#2944 — the cleanup watchdog CLAIMS a finished turn's retained terminal
before it ever writes `failed` over it.

Before: a synchronously dispatched turn (every trigger while DISPATCH_ASYNC is
off) whose backend was recreated mid-run finished on the agent, returned to a
dead socket, and the watchdog — seeing the id in `recently_completed_ids` for
one cycle and nowhere the next — wrote `status=failed, cost=NULL,
response=NULL` for a run that succeeded. It asked the agent for a
`last-error`, never for a result.

Now, for a `running` row the agent reports as recently completed (or does not
report at all) with no live backend dispatcher, the watchdog GETs
`/api/executions/{id}/result` and applies what comes back through
`TaskExecutionService.apply_result` with exactly the result-callback's inputs.
Three properties are load-bearing and each has a test here:

  * the claim is TIME-GATED on the agent's `retained_at` (a live dispatcher is
    not reliably "alive" between its POST returning and its terminal CAS);
  * an OLD image (bare 404 / non-record body) keeps the pre-#2944 behaviour,
    a coded 404 lets the orphan path say "no retained result", and a probe
    that could not be asked withholds the row for a cycle;
  * only a WON CAS counts and broadcasts (every worker runs this loop).

Harness note: `services.cleanup_service` is imported INSIDE each test (the
`test_2433_watchdog_inflight.py` shape — the unit conftest pops `services.*`
between collection and test).
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_backend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)


def _cs():
    from services import cleanup_service as cs  # noqa: WPS433 — lazy on purpose

    return cs


def _past_iso(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _record(*, age=600, status="success", **over):
    base = {
        "status": status, "response": "the $32 answer", "error": None, "error_code": None,
        "terminal_reason": "completed", "metadata": {"cost_usd": 32.0, "session_id": "sess-1"},
        "execution_log": [{"type": "result"}], "session_id": "sess-1",
        "retained_at": _past_iso(age), "truncated_log": False,
    }
    base.update(over)
    return base


def _resp(status_code=200, body=None, *, json_raises=False, headers=None):
    r = MagicMock(status_code=status_code)
    r.headers = headers or {}
    if json_raises:
        r.json.side_effect = ValueError("not json")
    else:
        r.json.return_value = body
    return r


def _mock_httpx_cm(client):
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _svc_result(status="success", error_code=None):
    return SimpleNamespace(status=status, error_code=error_code)


class _Harness:
    """One periodic sweep over a single `running` row on `agent-a`."""

    def __init__(self, mock_db, cap_fn, mock_httpx, *, age=600, timeout=900, triggered_by="mcp"):
        self.cs = _cs()
        self.client = MagicMock()
        mock_httpx.return_value = _mock_httpx_cm(self.client)
        mock_db.get_running_executions_with_agent_info.return_value = [
            {"id": "exec-1", "agent_name": "agent-a", "started_at": _past_iso(age),
             "timeout_seconds": timeout, "schedule_id": "s1", "triggered_by": triggered_by},
        ]
        mock_db.mark_execution_failed_by_watchdog.return_value = True
        mock_db.get_open_activity_id_for_execution.return_value = "act-1"
        self.db = mock_db
        self.capacity = AsyncMock()
        cap_fn.return_value = self.capacity
        self.service = self.cs.CleanupService()
        self.service._broadcast_watchdog_event = AsyncMock()
        self.service._terminate_on_agent = AsyncMock(return_value=True)
        self.apply_result = AsyncMock(return_value=_svc_result())
        self.report = self.cs.CleanupReport()

    def run(self, *, known, verdict="absent", probe=None, bound=100000.0):
        self.service._get_agent_running_ids = AsyncMock(return_value=known)
        if probe is not None:
            self.client.get = probe if isinstance(probe, AsyncMock) else AsyncMock(return_value=probe)
        svc = MagicMock(apply_result=self.apply_result)
        with (
            patch.object(self.cs, "_inflight_verdicts", AsyncMock(return_value={"exec-1": verdict})),
            patch.object(self.cs.agent_call_limiter, "inflight_max_age_seconds", return_value=bound),
            patch("services.task_execution_service.get_task_execution_service", return_value=svc),
            patch("services.task_execution_service.dispatch_breaker_active", return_value=True),
        ):
            return asyncio.run(self.service._reconcile_orphaned_executions(self.report))


def _known(cs, *, recent=(), running=(), pending=()):
    return cs._extract_agent_known_ids({
        "executions": [{"execution_id": e} for e in running],
        "recently_completed_ids": list(recent),
        "pending_ids": list(pending),
    })


# ---------------------------------------------------------------------------
# Recently completed on the agent, no live dispatcher → claim
# ---------------------------------------------------------------------------

@patch("services.cleanup_service.httpx.AsyncClient")
@patch("services.cleanup_service.db")
@patch("services.cleanup_service.get_capacity_manager")
class TestRecentlyCompletedIsClaimed:
    def test_the_reported_case_is_claimed_as_success_not_failed(self, cap_fn, mock_db, mock_httpx):
        """The $32 run: backend recreated mid-turn, agent finished, marker
        fresh, no dispatcher anywhere. Before #2944 this cycle skipped it and
        the next one failed it."""
        h = _Harness(mock_db, cap_fn, mock_httpx, triggered_by="schedule")
        orphaned, terminated, confirmed = h.run(
            known=_known(h.cs, recent=["exec-1"]), probe=_resp(200, _record()),
        )
        h.client.get.assert_awaited_once()
        assert "/api/executions/exec-1/result" in h.client.get.await_args.args[0]
        h.apply_result.assert_awaited_once()
        args, kwargs = h.apply_result.await_args
        assert args[0] == "agent-a"
        envelope = args[1]
        assert envelope.execution_id == "exec-1"
        assert envelope.status == "success"
        assert envelope.metadata["cost_usd"] == 32.0
        assert envelope.session_id == "sess-1"
        assert envelope.response.startswith("> ℹ️ Recovered by the watchdog")
        assert envelope.response.endswith("the $32 answer")
        # EXACTLY the callback's inputs — activity incl. FAILED, breaker, slot.
        assert kwargs == {"activity_id": "act-1", "breaker_enabled": True, "release_slot": True}
        mock_db.get_open_activity_id_for_execution.assert_called_once_with("exec-1", include_failed=True)
        mock_db.mark_execution_failed_by_watchdog.assert_not_called()
        assert orphaned == 0 and terminated == 0
        assert "exec-1" not in confirmed
        assert h.report.results_recovered == 1
        assert h.report.total >= 1
        h.service._broadcast_watchdog_event.assert_awaited_once()
        assert h.service._broadcast_watchdog_event.await_args.args[0] == "result_recovered"

    def test_a_fresh_record_is_deferred_because_a_live_dispatcher_may_still_write_it(
        self, cap_fn, mock_db, mock_httpx
    ):
        """The in-flight entry is popped the instant the POST returns and the
        marker delete flushes on the next tick — `absent` inside that window
        is a healthy turn about to be finalized by its own caller."""
        h = _Harness(mock_db, cap_fn, mock_httpx)
        _, _, confirmed = h.run(
            known=_known(h.cs, recent=["exec-1"]), probe=_resp(200, _record(age=30)),
        )
        h.apply_result.assert_not_awaited()
        mock_db.mark_execution_failed_by_watchdog.assert_not_called()
        assert "exec-1" in confirmed
        assert h.report.result_claims_deferred == 1 and h.report.results_recovered == 0
        h.service._broadcast_watchdog_event.assert_not_awaited()

    def test_grace_is_above_the_marker_ttl_plus_a_tick(self, cap_fn, mock_db, mock_httpx):
        cs = _cs()
        lim = cs.agent_call_limiter
        assert cs.RETAINED_RESULT_CLAIM_GRACE_SECONDS > lim.INFLIGHT_MARKER_TTL_SECONDS + lim.INFLIGHT_TICK_SECONDS

    @pytest.mark.parametrize("verdict", ["alive", "unknown"])
    def test_a_live_or_unverifiable_dispatcher_is_never_raced(self, cap_fn, mock_db, mock_httpx, verdict):
        h = _Harness(mock_db, cap_fn, mock_httpx)
        _, _, confirmed = h.run(
            known=_known(h.cs, recent=["exec-1"]), verdict=verdict, probe=_resp(200, _record()),
        )
        h.client.get.assert_not_awaited()          # not even asked
        h.apply_result.assert_not_awaited()
        assert "exec-1" in confirmed

    def test_a_completed_row_past_its_timeout_is_claimed_not_terminated(self, cap_fn, mock_db, mock_httpx):
        """`is_on_agent` used to include recently-completed ids, so a finished
        row older than its timeout hit the auto-terminate branch — the agent
        404'd and the row was 'deferred to stale cleanup' for two hours."""
        h = _Harness(mock_db, cap_fn, mock_httpx, age=5000, timeout=900)
        orphaned, terminated, _ = h.run(
            known=_known(h.cs, recent=["exec-1"]), probe=_resp(200, _record()),
        )
        h.service._terminate_on_agent.assert_not_awaited()
        h.apply_result.assert_awaited_once()
        assert terminated == 0 and h.report.results_recovered == 1

    def test_no_record_on_a_recently_completed_id_keeps_the_pre_2944_skip(self, cap_fn, mock_db, mock_httpx):
        h = _Harness(mock_db, cap_fn, mock_httpx)
        _, _, confirmed = h.run(
            known=_known(h.cs, recent=["exec-1"]),
            probe=_resp(404, {"detail": {"code": "no_retained_result"}}),
        )
        h.apply_result.assert_not_awaited()
        mock_db.mark_execution_failed_by_watchdog.assert_not_called()
        assert "exec-1" in confirmed

    def test_a_lost_cas_counts_and_broadcasts_nothing(self, cap_fn, mock_db, mock_httpx):
        """Every worker runs this loop; the second claim's apply_result
        reconciles. The row is already right — no second event."""
        from services.execution_envelope import TaskExecutionErrorCode

        h = _Harness(mock_db, cap_fn, mock_httpx)
        h.apply_result.return_value = _svc_result("failed", TaskExecutionErrorCode.RECONCILED)
        h.run(known=_known(h.cs, recent=["exec-1"]), probe=_resp(200, _record()))
        h.apply_result.assert_awaited_once()
        assert h.report.results_recovered == 0
        h.service._broadcast_watchdog_event.assert_not_awaited()

    def test_a_retained_failure_is_applied_as_the_real_error(self, cap_fn, mock_db, mock_httpx):
        h = _Harness(mock_db, cap_fn, mock_httpx)
        h.apply_result.return_value = _svc_result("failed")
        h.run(
            known=_known(h.cs, recent=["exec-1"]),
            probe=_resp(200, _record(status="failed", error="auth failed", error_code="auth",
                                     terminal_reason="auth", response=None)),
        )
        envelope = h.apply_result.await_args.args[1]
        assert envelope.status == "failed"
        assert envelope.error == "auth failed"
        assert envelope.error_code is not None and envelope.error_code.value == "auth"
        assert envelope.response is None      # no notice on a failure
        mock_db.mark_execution_failed_by_watchdog.assert_not_called()


# ---------------------------------------------------------------------------
# Orphan branch: absent everywhere → ask for the result FIRST
# ---------------------------------------------------------------------------

@patch("services.cleanup_service.httpx.AsyncClient")
@patch("services.cleanup_service.db")
@patch("services.cleanup_service.get_capacity_manager")
class TestOrphanBranchAsksForTheResultFirst:
    def test_found_after_the_marker_expired_is_claimed(self, cap_fn, mock_db, mock_httpx):
        """The marker is five minutes wide; the retained record outlives it."""
        h = _Harness(mock_db, cap_fn, mock_httpx)
        orphaned, _, _ = h.run(known=_known(h.cs), probe=_resp(200, _record(age=3600)))
        h.apply_result.assert_awaited_once()
        mock_db.mark_execution_failed_by_watchdog.assert_not_called()
        assert orphaned == 0 and h.report.results_recovered == 1

    def test_coded_404_fails_the_row_and_says_no_retained_result(self, cap_fn, mock_db, mock_httpx):
        h = _Harness(mock_db, cap_fn, mock_httpx)
        orphaned, _, _ = h.run(
            known=_known(h.cs), probe=_resp(404, {"detail": {"code": "no_retained_result"}}),
        )
        assert orphaned == 1
        msg = mock_db.mark_execution_failed_by_watchdog.call_args.args[1]
        assert "not recently completed, no retained result" in msg
        assert "no live backend dispatcher" in msg
        h.capacity.release_if_matches.assert_awaited_once_with("agent-a", "exec-1")

    @pytest.mark.parametrize("probe", [
        pytest.param(_resp(404, {"detail": "Not Found"}), id="bare-fastapi-404"),
        pytest.param(_resp(404, None, json_raises=True), id="404-no-json"),
        pytest.param(_resp(200, {"executions": []}), id="200-not-a-record"),
        pytest.param(_resp(MagicMock()), id="stub-leak"),
    ])
    def test_an_old_image_keeps_the_2433_string_unchanged(self, cap_fn, mock_db, mock_httpx, probe):
        """No route (or nothing recognizable) is not an observation about
        results — the string must not claim one was made."""
        h = _Harness(mock_db, cap_fn, mock_httpx)
        orphaned, _, _ = h.run(known=_known(h.cs), probe=probe)
        assert orphaned == 1
        msg = mock_db.mark_execution_failed_by_watchdog.call_args.args[1]
        assert "retained" not in msg
        assert msg == h.cs._orphan_error_message("agent-a", True)

    @pytest.mark.parametrize("probe", [
        pytest.param(AsyncMock(side_effect=ConnectionError("agent down")), id="transport-error"),
        pytest.param(_resp(503, {}), id="5xx"),
        pytest.param(_resp(200, None, json_raises=True), id="200-bad-json"),
        pytest.param(_resp(200, _record(response="x" * 4_000_001)), id="response-over-cap"),
        pytest.param(_resp(200, _record(), headers={"content-length": str(30_000_000)}), id="content-length-over-cap"),
    ])
    def test_a_probe_that_could_not_be_asked_withholds_the_row(self, cap_fn, mock_db, mock_httpx, probe):
        h = _Harness(mock_db, cap_fn, mock_httpx)
        orphaned, _, _ = h.run(known=_known(h.cs), probe=probe)
        assert orphaned == 0
        mock_db.mark_execution_failed_by_watchdog.assert_not_called()
        h.apply_result.assert_not_awaited()
        assert h.report.result_probe_deferred == 1

    def test_caps_mirror_the_result_callback_endpoint(self, cap_fn, mock_db, mock_httpx):
        cs = _cs()
        from routers import agents as agents_router

        assert cs._MAX_RETAINED_RESPONSE_CHARS == agents_router._MAX_CALLBACK_RESPONSE_CHARS
        assert cs._MAX_RETAINED_LOG_BYTES == agents_router._MAX_CALLBACK_LOG_BYTES


# ---------------------------------------------------------------------------
# One classification for the callback endpoint and the watchdog
# ---------------------------------------------------------------------------

class TestSharedTerminalMapping:
    def _payload(self, **over):
        base = dict(status="success", response="r", error=None, error_code=None,
                    terminal_reason="completed", metadata={"cost_usd": 1.0},
                    execution_log=[{"a": 1}], session_id="s", execution_time_ms=10)
        base.update(over)
        return SimpleNamespace(**base)

    def test_success_cancelled_failed_map_three_ways(self):
        from services.execution_envelope import terminal_from_callback_payload

        assert terminal_from_callback_payload(self._payload(), "e").status == "success"
        assert terminal_from_callback_payload(self._payload(status="cancelled"), "e").status == "cancelled"
        assert terminal_from_callback_payload(self._payload(status="failed"), "e").status == "failed"
        assert terminal_from_callback_payload(self._payload(status="something-new"), "e").status == "failed"

    def test_auth_is_never_a_cancellation(self):
        from services.execution_envelope import terminal_from_callback_payload

        by_code = terminal_from_callback_payload(self._payload(status="cancelled", error_code="auth"), "e")
        by_reason = terminal_from_callback_payload(self._payload(status="cancelled", terminal_reason="rate_limit"), "e")
        assert by_code.status == "failed" and by_code.error_code.value == "auth"
        assert by_reason.status == "failed"

    def test_unknown_error_code_is_dropped_not_fatal(self):
        from services.execution_envelope import terminal_from_callback_payload

        env = terminal_from_callback_payload(self._payload(status="failed", error_code="from-the-future"), "e")
        assert env.status == "failed" and env.error_code is None

    def test_fields_are_carried_verbatim(self):
        from services.execution_envelope import terminal_from_callback_payload

        env = terminal_from_callback_payload(self._payload(), "exec-9")
        assert (env.execution_id, env.response, env.metadata, env.execution_log, env.session_id,
                env.execution_time_ms) == ("exec-9", "r", {"cost_usd": 1.0}, [{"a": 1}], "s", 10)


# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------

class TestStartupRecoveryClaims:
    def _run(self, mock_db, cap_fn, *, registry_body, probe_resp, verdict="absent",
             apply_result=None):
        cs = _cs()
        mock_db.get_running_executions.return_value = [
            {"id": "exec-1", "agent_name": "agent-a", "started_at": _past_iso(600)},
        ]
        mock_db.mark_execution_failed_by_watchdog.return_value = True
        mock_db.get_open_activity_id_for_execution.return_value = "act-1"
        cap_fn.return_value = AsyncMock()
        container = MagicMock(status="running")
        client = MagicMock()

        async def _get(path, timeout=None, **kw):
            if path.endswith("/running"):
                return _resp(200, registry_body)
            return probe_resp

        client.get = AsyncMock(side_effect=_get)
        apply_result = apply_result or AsyncMock(return_value=_svc_result())
        svc = MagicMock(apply_result=apply_result)
        with (
            patch("services.docker_service.get_agent_container", return_value=container),
            patch("services.agent_client.get_agent_client", return_value=client),
            patch.object(cs, "_reconcile_orphaned_slots", AsyncMock(return_value={})),
            patch.object(cs, "_inflight_verdicts", AsyncMock(return_value={"exec-1": verdict})),
            patch.object(cs.agent_call_limiter, "inflight_max_age_seconds", return_value=100000.0),
            patch("services.task_execution_service.get_task_execution_service", return_value=svc),
            patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
        ):
            return asyncio.run(cs.recover_orphaned_executions()), apply_result

    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_capacity_manager")
    def test_recently_completed_at_boot_is_claimed_immediately(self, cap_fn, mock_db):
        """This worker just booted — it has no dispatcher for the row by
        construction, so the result is claimed here, not left to the sweep."""
        result, apply_result = self._run(
            mock_db, cap_fn,
            registry_body={"executions": [], "recently_completed_ids": ["exec-1"], "pending_ids": []},
            probe_resp=_resp(200, _record()),
        )
        apply_result.assert_awaited_once()
        assert result["recovered_results"] == 1 and result["recovered"] == 0
        mock_db.mark_execution_failed_by_watchdog.assert_not_called()

    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_capacity_manager")
    def test_absent_everywhere_at_boot_asks_before_failing(self, cap_fn, mock_db):
        result, apply_result = self._run(
            mock_db, cap_fn,
            registry_body={"executions": [], "recently_completed_ids": [], "pending_ids": []},
            probe_resp=_resp(200, _record(age=7200)),
        )
        apply_result.assert_awaited_once()
        assert result["recovered_results"] == 1 and result["recovered"] == 0

    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_capacity_manager")
    def test_a_fresh_record_at_boot_is_left_for_the_sweep(self, cap_fn, mock_db):
        """A worker-recycle boot runs beside live workers — same grace."""
        result, apply_result = self._run(
            mock_db, cap_fn,
            registry_body={"executions": [], "recently_completed_ids": ["exec-1"], "pending_ids": []},
            probe_resp=_resp(200, _record(age=20)),
        )
        apply_result.assert_not_awaited()
        assert result["still_running"] == 1 and result["recovered"] == 0

    @patch("services.cleanup_service.db")
    @patch("services.cleanup_service.get_capacity_manager")
    def test_an_old_image_at_boot_recovers_as_before(self, cap_fn, mock_db):
        result, apply_result = self._run(
            mock_db, cap_fn,
            registry_body={"executions": [], "recently_completed_ids": [], "pending_ids": []},
            probe_resp=_resp(404, {"detail": "Not Found"}),
        )
        apply_result.assert_not_awaited()
        assert result["recovered"] == 1
