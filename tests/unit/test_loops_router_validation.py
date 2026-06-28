"""Validation tests for the loops router — max_duration_seconds deadline (#1156).

Calls the ``start_loop`` endpoint coroutine directly with mocked auth/db/service
so the cross-field validation (deadline must be >= the effective per-run
timeout) can be exercised without a live FastAPI app or database.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytestmark = pytest.mark.unit


def _load_router(monkeypatch):
    from routers import loops as loops_router

    fake_db = MagicMock()
    fake_db.get_execution_timeout.return_value = 900  # agent default for tests
    monkeypatch.setattr(loops_router, "db", fake_db)

    fake_service = MagicMock()
    fake_service.start_loop = AsyncMock(
        return_value={"id": "loop_x", "status": "queued"}
    )
    monkeypatch.setattr(loops_router, "get_loop_service", lambda: fake_service)

    return loops_router, fake_db, fake_service


def _user():
    u = MagicMock()
    u.id = 1
    u.email = "u@example.com"
    return u


async def _call(loops_router, payload):
    return await loops_router.start_loop(
        payload=payload,
        name="a1",
        current_user=_user(),
        x_source_agent=None,
        x_mcp_key_id=None,
        x_mcp_key_name=None,
    )


class TestMaxDurationValidation:
    def test_rejects_deadline_below_explicit_timeout_per_run(self, monkeypatch):
        loops_router, _, service = _load_router(monkeypatch)
        payload = loops_router.StartLoopRequest(
            message="m", max_runs=5, timeout_per_run=600, max_duration_seconds=300,
        )
        with pytest.raises(loops_router.HTTPException) as exc:
            __import__("asyncio").run(_call(loops_router, payload))
        assert exc.value.status_code == 400
        assert "max_duration_seconds" in exc.value.detail
        service.start_loop.assert_not_called()

    def test_rejects_deadline_below_agent_timeout_when_per_run_unset(self, monkeypatch):
        loops_router, fake_db, service = _load_router(monkeypatch)
        payload = loops_router.StartLoopRequest(
            message="m", max_runs=5, max_duration_seconds=100,  # < 900 agent default
        )
        with pytest.raises(loops_router.HTTPException) as exc:
            __import__("asyncio").run(_call(loops_router, payload))
        assert exc.value.status_code == 400
        fake_db.get_execution_timeout.assert_called_once_with("a1")
        service.start_loop.assert_not_called()

    def test_accepts_deadline_at_or_above_timeout(self, monkeypatch):
        loops_router, _, service = _load_router(monkeypatch)
        payload = loops_router.StartLoopRequest(
            message="m", max_runs=5, timeout_per_run=600, max_duration_seconds=600,
        )
        resp = __import__("asyncio").run(_call(loops_router, payload))
        assert resp.loop_id == "loop_x"
        # deadline threaded through to the service
        assert service.start_loop.await_args.kwargs["max_duration_seconds"] == 600

    def test_no_deadline_skips_validation(self, monkeypatch):
        loops_router, fake_db, service = _load_router(monkeypatch)
        payload = loops_router.StartLoopRequest(message="m", max_runs=5)
        resp = __import__("asyncio").run(_call(loops_router, payload))
        assert resp.loop_id == "loop_x"
        fake_db.get_execution_timeout.assert_not_called()
        assert service.start_loop.await_args.kwargs["max_duration_seconds"] is None


class TestMaxCostValidation:
    """Pydantic gt=0 on max_cost_usd (#1155).

    Asserts the model-layer constraint directly (StartLoopRequest(...) raising
    ValidationError) — matching this file's convention. There is no endpoint
    cross-field check for the budget, so this is NOT an HTTP-422 path; it's the
    Pydantic field validator. A live 422 would require a TestClient.
    """

    def test_rejects_zero(self, monkeypatch):
        loops_router, _, _ = _load_router(monkeypatch)
        with pytest.raises(ValidationError):
            loops_router.StartLoopRequest(message="m", max_runs=5, max_cost_usd=0)

    def test_rejects_negative(self, monkeypatch):
        loops_router, _, _ = _load_router(monkeypatch)
        with pytest.raises(ValidationError):
            loops_router.StartLoopRequest(message="m", max_runs=5, max_cost_usd=-1)

    def test_accepts_positive(self, monkeypatch):
        loops_router, _, _ = _load_router(monkeypatch)
        payload = loops_router.StartLoopRequest(
            message="m", max_runs=5, max_cost_usd=0.5,
        )
        assert payload.max_cost_usd == 0.5

    def test_threaded_through_to_service(self, monkeypatch):
        loops_router, _, service = _load_router(monkeypatch)
        payload = loops_router.StartLoopRequest(
            message="m", max_runs=5, max_cost_usd=2.5,
        )
        resp = __import__("asyncio").run(_call(loops_router, payload))
        assert resp.loop_id == "loop_x"
        assert service.start_loop.await_args.kwargs["max_cost_usd"] == 2.5


class TestTotalCostOnRead:
    """total_cost is the sum of run costs, computed on read (#1155)."""

    @staticmethod
    def _loop(**over):
        base = {
            "id": "loop_x", "agent_name": "a1", "status": "completed",
            "max_runs": 3, "runs_completed": 3, "stop_reason": "max_runs_reached",
            "last_response": None, "error": None, "created_at": "t",
            "started_at": "t", "completed_at": "t", "max_duration_seconds": None,
            "max_cost_usd": None,
        }
        base.update(over)
        return base

    @staticmethod
    def _run_row(n, cost):
        return {
            "run_number": n, "execution_id": None, "status": "completed",
            "response": None, "cost": cost, "duration_ms": 10, "error": None,
            "started_at": "t", "completed_at": "t",
        }

    def test_sums_run_costs_and_echoes_budget(self, monkeypatch):
        loops_router, fake_db, _ = _load_router(monkeypatch)
        fake_db.list_loop_runs.return_value = [
            self._run_row(1, 0.01), self._run_row(2, 0.02), self._run_row(3, None),
        ]
        resp = loops_router._build_status_response(
            self._loop(max_cost_usd=0.5)
        )
        assert resp.total_cost == pytest.approx(0.03)  # None → 0
        assert resp.max_cost_usd == 0.5

    def test_zero_run_loop_reports_zero(self, monkeypatch):
        loops_router, fake_db, _ = _load_router(monkeypatch)
        fake_db.list_loop_runs.return_value = []
        resp = loops_router._build_status_response(self._loop(runs_completed=0))
        assert resp.total_cost == 0.0
        assert resp.max_cost_usd is None
class TestNoProgressThreshold:
    """#1157 — no-progress threshold model default + validation + wiring.

    The default lives in the Pydantic model and is applied at the router
    boundary; K=1 is rejected by the model's field_validator (→ FastAPI 422).
    """

    def test_default_is_three_and_threaded_to_service(self, monkeypatch):
        loops_router, _, service = _load_router(monkeypatch)
        # Omit no_progress_threshold → model default 3.
        payload = loops_router.StartLoopRequest(message="m", max_runs=5)
        assert payload.no_progress_threshold == 3
        __import__("asyncio").run(_call(loops_router, payload))
        assert service.start_loop.await_args.kwargs["no_progress_threshold"] == 3

    def test_explicit_zero_disables_and_is_threaded(self, monkeypatch):
        loops_router, _, service = _load_router(monkeypatch)
        payload = loops_router.StartLoopRequest(
            message="m", max_runs=5, no_progress_threshold=0,
        )
        assert payload.no_progress_threshold == 0
        __import__("asyncio").run(_call(loops_router, payload))
        assert service.start_loop.await_args.kwargs["no_progress_threshold"] == 0

    def test_explicit_two_is_threaded(self, monkeypatch):
        loops_router, _, service = _load_router(monkeypatch)
        payload = loops_router.StartLoopRequest(
            message="m", max_runs=5, no_progress_threshold=2,
        )
        __import__("asyncio").run(_call(loops_router, payload))
        assert service.start_loop.await_args.kwargs["no_progress_threshold"] == 2

    def test_k_equals_one_rejected_422(self):
        with pytest.raises(ValidationError) as exc:
            from models import StartLoopRequest
            StartLoopRequest(message="m", max_runs=5, no_progress_threshold=1)
        assert "no_progress_threshold" in str(exc.value)

    def test_negative_rejected(self):
        with pytest.raises(ValidationError):
            from models import StartLoopRequest
            StartLoopRequest(message="m", max_runs=5, no_progress_threshold=-1)

    def test_status_response_echoes_threshold(self, monkeypatch):
        """_build_status_response must surface no_progress_threshold from the
        loop dict (guards the GET wiring alongside _loop_row_to_dict)."""
        loops_router, fake_db, _ = _load_router(monkeypatch)
        fake_db.list_loop_runs.return_value = []
        loop = {
            "id": "loop_x", "agent_name": "a1", "status": "stopped",
            "max_runs": 5, "runs_completed": 2, "stop_reason": "no_progress",
            "last_response": "same", "error": None, "created_at": "now",
            "started_at": "now", "completed_at": "now",
            "max_duration_seconds": None, "no_progress_threshold": 2,
        }
        resp = loops_router._build_status_response(loop)
        assert resp.no_progress_threshold == 2
        assert resp.stop_reason == "no_progress"
