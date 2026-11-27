"""
Tests for the conditional schedule pre-check hook (#454, SCHED-COND-001).

Covers:
- `_run_pre_check` translates backend `docker exec` responses correctly:
  hook absent → None, non-zero exit → None, empty stdout → skip,
  non-empty stdout → fire with message override.
- `_execute_schedule_with_lock` honours the translated decision: skips
  when `fire=False`, fires with override when `fire=True` carries a
  message, fail-opens when `_run_pre_check` returns None, bypasses
  pre-check entirely for manual triggers.
"""

# Path setup must happen before scheduler imports
import sys
from pathlib import Path

_this_file = Path(__file__).resolve()
_src_path = str(_this_file.parent.parent.parent / "src")
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from scheduler.models import ExecutionStatus


# ---------------------------------------------------------------------------
# _run_pre_check translation layer (backend JSON → scheduler decision)
# ---------------------------------------------------------------------------


def _build_svc(db_with_data):
    """SchedulerService instance with just the dependencies _run_pre_check needs."""
    from scheduler.service import SchedulerService

    svc = SchedulerService.__new__(SchedulerService)
    svc.db = db_with_data
    return svc


def _mock_httpx_post(response_json=None, status_code=200, raise_exc=None):
    """Build the `async with httpx.AsyncClient() as client` patch target."""
    response = MagicMock()
    response.status_code = status_code
    if response_json is not None:
        response.json = MagicMock(return_value=response_json)
    client_ctx = AsyncMock()
    if raise_exc is not None:
        client_ctx.__aenter__.return_value.post = AsyncMock(side_effect=raise_exc)
    else:
        client_ctx.__aenter__.return_value.post = AsyncMock(return_value=response)
    return patch("scheduler.service.httpx.AsyncClient", return_value=client_ctx)


class TestRunPreCheckTranslation:
    @pytest.mark.asyncio
    async def test_no_hook_returns_none(self, db_with_data):
        svc = _build_svc(db_with_data)
        with _mock_httpx_post({"hook_present": False}):
            decision = await svc._run_pre_check("test-agent")
        assert decision is None

    @pytest.mark.asyncio
    async def test_nonzero_exit_returns_none_failopen(self, db_with_data):
        svc = _build_svc(db_with_data)
        body = {
            "hook_present": True,
            "exit_code": 2,
            "stdout": "",
            "stderr": "ModuleNotFoundError: foo",
        }
        with _mock_httpx_post(body):
            decision = await svc._run_pre_check("test-agent")
        assert decision is None

    @pytest.mark.asyncio
    async def test_empty_stdout_skips(self, db_with_data):
        svc = _build_svc(db_with_data)
        body = {"hook_present": True, "exit_code": 0, "stdout": "  \n", "stderr": ""}
        with _mock_httpx_post(body):
            decision = await svc._run_pre_check("test-agent")
        assert decision == {"fire": False, "reason": "pre-check returned empty stdout"}

    @pytest.mark.asyncio
    async def test_nonempty_stdout_fires_with_override(self, db_with_data):
        svc = _build_svc(db_with_data)
        body = {
            "hook_present": True,
            "exit_code": 0,
            "stdout": "Review PR #1\n",
            "stderr": "",
        }
        with _mock_httpx_post(body):
            decision = await svc._run_pre_check("test-agent")
        assert decision == {"fire": True, "message": "Review PR #1"}

    @pytest.mark.asyncio
    async def test_backend_404_returns_none(self, db_with_data):
        svc = _build_svc(db_with_data)
        with _mock_httpx_post(None, status_code=404):
            decision = await svc._run_pre_check("missing-agent")
        assert decision is None

    @pytest.mark.asyncio
    async def test_backend_5xx_returns_none(self, db_with_data):
        svc = _build_svc(db_with_data)
        with _mock_httpx_post({}, status_code=502):
            decision = await svc._run_pre_check("test-agent")
        assert decision is None

    @pytest.mark.asyncio
    async def test_connection_error_returns_none(self, db_with_data):
        svc = _build_svc(db_with_data)
        with _mock_httpx_post(raise_exc=Exception("connection refused")):
            decision = await svc._run_pre_check("test-agent")
        assert decision is None

    @pytest.mark.asyncio
    async def test_malformed_json_returns_none(self, db_with_data):
        """Backend returns 200 but body isn't valid JSON → fail-open."""
        svc = _build_svc(db_with_data)
        response = MagicMock()
        response.status_code = 200
        response.json = MagicMock(side_effect=ValueError("not json"))
        client_ctx = AsyncMock()
        client_ctx.__aenter__.return_value.post = AsyncMock(return_value=response)
        with patch(
            "scheduler.service.httpx.AsyncClient", return_value=client_ctx
        ):
            decision = await svc._run_pre_check("test-agent")
        assert decision is None


class TestRunPreCheckTimeout:
    """#1022: the pre-check POST deadline is config-driven (was a 70.0 literal),
    and a blank-stringifying timeout — the exact #1022 trigger — still fails
    open instead of raising. (TestRunPreCheckTranslation already covers the
    fail-open path with a *non-blank* exception; this pins the blank case.)"""

    @pytest.mark.asyncio
    async def test_post_uses_configured_pre_check_timeout(self, db_with_data):
        """The POST timeout comes from config.pre_check_timeout, not a literal."""
        svc = _build_svc(db_with_data)

        response = MagicMock()
        response.status_code = 200
        response.json = MagicMock(return_value={"hook_present": False})
        post = AsyncMock(return_value=response)
        client_ctx = AsyncMock()
        client_ctx.__aenter__.return_value.post = post

        with patch("scheduler.service.httpx.AsyncClient", return_value=client_ctx), \
             patch("scheduler.service.config.pre_check_timeout", 42.0):
            await svc._run_pre_check("test-agent")

        assert post.call_args.kwargs["timeout"] == 42.0

    @pytest.mark.asyncio
    async def test_blank_stringifying_timeout_fails_open(self, db_with_data):
        """A blank httpx timeout (str()=='') must fail open (return None),
        never propagate — fail-open is structural for the pre-check gate."""
        svc = _build_svc(db_with_data)
        with _mock_httpx_post(raise_exc=httpx.ReadTimeout("")):
            decision = await svc._run_pre_check("test-agent")
        assert decision is None


# ---------------------------------------------------------------------------
# SchedulerService._execute_schedule_with_lock pre-check branch
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_scheduler(db_with_data):
    """Build a SchedulerService with mocked I/O deps so we can drive the
    pre-check branch of `_execute_schedule_with_lock` without real networking."""
    from scheduler.service import SchedulerService

    svc = SchedulerService.__new__(SchedulerService)
    svc.db = db_with_data
    svc.lock_manager = MagicMock()
    svc._call_backend_execute_task = AsyncMock(
        return_value={"status": "dispatched"}
    )
    svc._get_next_run_time = MagicMock(return_value=None)
    return svc


class TestSchedulerPreCheckBranch:
    @pytest.mark.asyncio
    async def test_skip_when_fire_false(self, mock_scheduler, db_with_data):
        """pre-check fire=False → create skipped execution, no chat dispatch."""
        mock_scheduler._run_pre_check = AsyncMock(
            return_value={"fire": False, "reason": "no new PRs"}
        )

        await mock_scheduler._execute_schedule_with_lock("schedule-1")

        mock_scheduler._call_backend_execute_task.assert_not_called()

        with db_with_data.get_connection() as conn:
            rows = conn.execute(
                "SELECT error FROM schedule_executions "
                "WHERE schedule_id = 'schedule-1' AND status = ?",
                (ExecutionStatus.SKIPPED,),
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["error"] == "pre-check: no new PRs"

    @pytest.mark.asyncio
    async def test_fire_with_override_message(self, mock_scheduler, db_with_data):
        """pre-check fire=True with message → chat dispatched with override."""
        mock_scheduler._run_pre_check = AsyncMock(
            return_value={"fire": True, "message": "Review abilityai/trinity#371"}
        )

        await mock_scheduler._execute_schedule_with_lock("schedule-1")

        mock_scheduler._call_backend_execute_task.assert_awaited_once()
        kwargs = mock_scheduler._call_backend_execute_task.await_args.kwargs
        assert kwargs["message"] == "Review abilityai/trinity#371"

    @pytest.mark.asyncio
    async def test_fail_open_when_pre_check_returns_none(
        self, mock_scheduler, db_with_data
    ):
        """pre-check returns None (e.g. no hook, exec error) → fire with schedule.message."""
        mock_scheduler._run_pre_check = AsyncMock(return_value=None)

        await mock_scheduler._execute_schedule_with_lock("schedule-1")

        mock_scheduler._call_backend_execute_task.assert_awaited_once()
        kwargs = mock_scheduler._call_backend_execute_task.await_args.kwargs
        assert kwargs["message"] == "Run morning report"

    @pytest.mark.asyncio
    async def test_fire_true_without_message_uses_schedule_message(
        self, mock_scheduler, db_with_data
    ):
        mock_scheduler._run_pre_check = AsyncMock(return_value={"fire": True})

        await mock_scheduler._execute_schedule_with_lock("schedule-1")

        mock_scheduler._call_backend_execute_task.assert_awaited_once()
        kwargs = mock_scheduler._call_backend_execute_task.await_args.kwargs
        assert kwargs["message"] == "Run morning report"

    @pytest.mark.asyncio
    async def test_manual_trigger_bypasses_pre_check(
        self, mock_scheduler, db_with_data
    ):
        """Manual triggers are explicit operator intent — pre-check must not run."""
        mock_scheduler._run_pre_check = AsyncMock()

        await mock_scheduler._execute_schedule_with_lock(
            "schedule-1", triggered_by="manual"
        )

        mock_scheduler._run_pre_check.assert_not_called()
        mock_scheduler._call_backend_execute_task.assert_awaited_once()
