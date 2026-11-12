"""
Issue #497 — cleanup watchdog re-verify defers indefinitely when agent
unreachable, leaving the DB execution row in `running` until Phase 1's
120-minute stale-cleanup window.

This file covers ``_process_stale_slot_reclaims`` (the Phase 3 cleanup
branch added by #378). The scenario:

1. ``capacity.reclaim_stale()`` returned ``{"agent-x": ["exec-1"]}`` —
   slot's TTL elapsed and the slot was removed from Redis.
2. ``_get_agent_running_ids("agent-x")`` raises / returns ``None`` (agent
   unreachable — CPU pinned, network stuck, container hung).
3. The current code logs "agent unreachable during re-verify" and
   ``continue``s, leaving the DB row in `running`.

Before the #497 fix: no FAIL is written; the row sits as zombie `running`
metadata for up to ``EXECUTION_STALE_TIMEOUT_MINUTES`` (120 min) until
Phase 1 picks it up. Dashboards lie, capacity reporting lies, and the
operator-queue confidence in execution state is undermined.

After the #497 fix: ``db.fail_stale_slot_execution`` IS called for the
orphan — the same race-guarded writer Phase 3 already uses for the
"re-verify confirmed inactive" branch. Race-safe via the existing
``WHERE status='running'`` guard, which prevents overwriting a SUCCESS
that landed between the slot reclaim and the cleanup write.

The narrow flicker risk (agent unreachable → backend force-fails →
agent recovers and writes SUCCESS via ``update_execution_status`` which
DOES overwrite FAILED per #378's design) is accepted as documented
behavior: by definition the execution already ran past ``timeout +
buffer``, so the agent's late SUCCESS is reporting a deliverable that
exceeded its budget. Documented in the PR body.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Module preload — mirror tests/test_watchdog_unit.py
# ---------------------------------------------------------------------------

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# The unit test venv does NOT have the `docker` Python package installed,
# but `services/__init__.py` eagerly imports `services.docker_service` which
# imports `docker`. Stub both BEFORE the test method imports cleanup_service.
# Issue #762 lint forbids bare `sys.modules` mutations outside conftest, so
# install via an autouse monkeypatch fixture — the imports inside each test
# (`from services.cleanup_service import CleanupService`) and `@patch(...)`
# resolution both happen after fixture setup, so the stubs land in time.
@pytest.fixture(autouse=True)
def _stub_docker_and_database(monkeypatch):
    if "docker" not in sys.modules:
        monkeypatch.setitem(sys.modules, "docker", MagicMock(name="docker_stub"))
    if "services.docker_service" not in sys.modules:
        _ds_stub = types.ModuleType("services.docker_service")
        _ds_stub.get_docker_client = MagicMock(return_value=MagicMock())
        _ds_stub.get_agent_container = lambda *a, **kw: None
        _ds_stub.get_agent_status_from_container = lambda *a, **kw: "stopped"
        _ds_stub.list_all_agents = lambda *a, **kw: []
        _ds_stub.get_agent_by_name = lambda *a, **kw: None
        _ds_stub.get_next_available_port = lambda *a, **kw: 2222

        async def _exec(*a, **kw):
            return {"exit_code": 0, "output": ""}

        _ds_stub.execute_command_in_container = _exec
        monkeypatch.setitem(sys.modules, "services.docker_service", _ds_stub)
    # database.db is hit transitively; pre-stub it before cleanup_service loads.
    if "database" not in sys.modules:
        monkeypatch.setitem(sys.modules, "database", MagicMock(name="database_stub"))


# ─── sys.modules snapshot/restore (Issue #762) ───────────────────────────
#
# The three preload blocks above plant stubs at module-import time —
# monkeypatch is fixture-scoped and structurally cannot rewind them, and
# moving them to conftest would leak the stubs across the whole tests/unit/
# directory (the exact #762 failure mode). Adopt the named snapshot/restore
# escape hatch documented in `tests/lint_sys_modules.py` (precedent:
# `tests/unit/test_telegram_webhook_backfill.py`): the autouse fixture
# snapshots these slots at test setup and restores them at teardown so
# the stubs cannot leak into other tests in the same pytest session.

_STUBBED_MODULE_NAMES = [
    "docker",
    "services.docker_service",
    "database",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot sys.modules before each test and restore after.

    Bounds the blast radius of this file's import-time stubs so they
    cannot leak into other test files in the same pytest session.
    """
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _iso_past_minutes(minutes: int) -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0) - _td(minutes=minutes)
    ).isoformat().replace("+00:00", "Z")


def _td(**kwargs):
    from datetime import timedelta
    return timedelta(**kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_reclaim_batch(agent: str, execution_ids: list[str]) -> dict[str, list[str]]:
    """Mirror what capacity.reclaim_stale() returns."""
    return {agent: list(execution_ids)}


def _run(coro):
    """Synchronous wrapper for awaitable returned from the service."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Bug repro — pre-#497 behavior: skip when agent unreachable.
# ---------------------------------------------------------------------------
#
# We don't gate this on "before the fix" because the post-fix code will
# call fail_stale_slot_execution. Instead, the post-fix assertions in
# section 2 will catch the change. Section 1 is the negative pin: it
# documents the OLD bug shape so future maintainers see what was wrong.


# ---------------------------------------------------------------------------
# 2. Post-fix behavior — force-fail unreachable orphans.
# ---------------------------------------------------------------------------


class TestProcessStaleSlotReclaimsUnreachable:
    """``_process_stale_slot_reclaims`` Phase 3 re-verify branch (#497)."""

    pytestmark = pytest.mark.unit

    def _service(self):
        from services.cleanup_service import CleanupService
        return CleanupService()

    @patch("services.cleanup_service.db")
    def test_agent_unreachable_force_fails_orphan(self, mock_db):
        """When re-verify can't reach the agent, the orphan must be marked
        failed via the race-guarded ``fail_stale_slot_execution`` writer.

        Before #497 this branch ``continue``-d, leaving the row in `running`
        for up to 120 minutes (Phase 1 stale cleanup). After #497, it fails
        immediately — slot was reclaimed because TTL expired, so the
        execution is by definition older than ``timeout + buffer`` and
        Phase 1 is just a longer-range version of the same condition.
        """
        mock_db.fail_stale_slot_execution.return_value = True

        service = self._service()
        # Re-verify returns None → agent unreachable.
        service._get_agent_running_ids = AsyncMock(return_value=None)
        # Best-effort terminate is a no-op here — the agent is unreachable.
        service._terminate_on_agent = AsyncMock(return_value=False)

        reclaimed = _make_reclaim_batch("agent-x", ["exec-1"])
        confirmed_running: set = set()
        from services.cleanup_service import CleanupReport
        report = CleanupReport()

        _run(service._process_stale_slot_reclaims(
            reclaimed, confirmed_running, report
        ))

        # Race-guarded writer SHOULD have been invoked for exec-1.
        mock_db.fail_stale_slot_execution.assert_called_once()
        call_args = mock_db.fail_stale_slot_execution.call_args
        assert call_args.kwargs["execution_id"] == "exec-1"
        # Error message must clearly indicate the unreachable code path so
        # log searches and operator-queue history identify the cause.
        err = call_args.kwargs["error"].lower()
        assert "unresponsive" in err or "unreachable" in err, (
            f"error message should mention the unreachable cause; got: {err!r}"
        )
        # The report increments stale_slot_executions for observability.
        assert report.stale_slot_executions == 1

    @patch("services.cleanup_service.db")
    def test_agent_reachable_says_still_running_no_action(self, mock_db):
        """#378 path unchanged: agent reachable and reports the execution
        is still running → cleanup must NOT mark failed."""
        service = self._service()
        service._get_agent_running_ids = AsyncMock(return_value={"exec-1"})
        service._terminate_on_agent = AsyncMock(return_value=False)

        reclaimed = _make_reclaim_batch("agent-x", ["exec-1"])
        from services.cleanup_service import CleanupReport
        report = CleanupReport()
        _run(service._process_stale_slot_reclaims(reclaimed, set(), report))

        mock_db.fail_stale_slot_execution.assert_not_called()
        assert report.stale_slot_executions == 0

    @patch("services.cleanup_service.db")
    def test_agent_reachable_says_not_running_fails(self, mock_db):
        """Existing behavior preserved: agent reachable and says exec is
        gone → cleanup marks failed."""
        mock_db.fail_stale_slot_execution.return_value = True

        service = self._service()
        service._get_agent_running_ids = AsyncMock(return_value=set())
        service._terminate_on_agent = AsyncMock(return_value=True)

        reclaimed = _make_reclaim_batch("agent-x", ["exec-1"])
        from services.cleanup_service import CleanupReport
        report = CleanupReport()
        _run(service._process_stale_slot_reclaims(reclaimed, set(), report))

        mock_db.fail_stale_slot_execution.assert_called_once()
        assert report.stale_slot_executions == 1

    @patch("services.cleanup_service.db")
    def test_confirmed_running_skipped_even_on_unreachable(self, mock_db):
        """#226 / #378 layered safety: if Phase 0's watchdog already
        confirmed the execution as running, Phase 3 must skip — including
        when the per-cycle re-verify is unreachable. (Phase 0 ran with a
        valid agent response; we trust it.)"""
        service = self._service()
        service._get_agent_running_ids = AsyncMock(return_value=None)
        service._terminate_on_agent = AsyncMock(return_value=False)

        reclaimed = _make_reclaim_batch("agent-x", ["exec-confirmed"])
        confirmed_running = {"exec-confirmed"}
        from services.cleanup_service import CleanupReport
        report = CleanupReport()
        _run(service._process_stale_slot_reclaims(reclaimed, confirmed_running, report))

        mock_db.fail_stale_slot_execution.assert_not_called()
        assert report.stale_slot_executions == 0

    @patch("services.cleanup_service.db")
    def test_race_guard_no_increment_when_db_says_already_terminal(self, mock_db):
        """``fail_stale_slot_execution`` returns False when the row already
        moved to a terminal state between slot reclaim and cleanup write.
        The report counter MUST NOT increment in that case — pinning the
        race-guard semantics so a future refactor can't silently double-count."""
        mock_db.fail_stale_slot_execution.return_value = False

        service = self._service()
        service._get_agent_running_ids = AsyncMock(return_value=None)
        service._terminate_on_agent = AsyncMock(return_value=False)

        reclaimed = _make_reclaim_batch("agent-x", ["exec-1"])
        from services.cleanup_service import CleanupReport
        report = CleanupReport()
        _run(service._process_stale_slot_reclaims(reclaimed, set(), report))

        mock_db.fail_stale_slot_execution.assert_called_once()
        # Race guard refused the update — counter stays 0.
        assert report.stale_slot_executions == 0

    @patch("services.cleanup_service.db")
    def test_mixed_batch_some_unreachable_some_confirmed(self, mock_db):
        """Multi-agent batch: agent-a unreachable (force-fail), agent-b
        confirmed running by Phase 0 (skip). Pin the per-execution
        decision matrix."""
        mock_db.fail_stale_slot_execution.return_value = True

        service = self._service()

        async def fake_running_ids(client, agent_name):
            return None if agent_name == "agent-a" else {"exec-b"}

        service._get_agent_running_ids = AsyncMock(side_effect=fake_running_ids)
        service._terminate_on_agent = AsyncMock(return_value=False)

        reclaimed = {
            "agent-a": ["exec-a"],
            "agent-b": ["exec-b"],
        }
        from services.cleanup_service import CleanupReport
        report = CleanupReport()
        _run(service._process_stale_slot_reclaims(reclaimed, set(), report))

        # exec-a: unreachable → force-fail
        # exec-b: agent says still running → skip
        mock_db.fail_stale_slot_execution.assert_called_once()
        assert (
            mock_db.fail_stale_slot_execution.call_args.kwargs["execution_id"]
            == "exec-a"
        )
        assert report.stale_slot_executions == 1
