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
