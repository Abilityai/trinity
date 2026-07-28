"""Bulk watchdog sweeps emit task-completion events (#1714, the #1578 residual).

`#1578` made every individually-reaped terminal writer emit
`agent.task.completed`/`.failed` so a subscribed orchestrator is woken. Bulk
watchdog sweeps (`mark_stale_executions_failed` / `mark_no_session_executions_failed`)
failed many rows with a COUNT-style bulk write and no per-row context, so they
emitted nothing — a bulk-swept execution never woke its subscriber. `#1714`
closes that: the two bulk-fail db fns now collect the CAS-won `(execution_id,
agent_name)` rows, and `cleanup_service._emit_bulk_terminal_events` emits the
same `agent.task.failed` per row — gated, paced, fail-open.

Two layers:
  * DB (real `db_harness` schema) — `collect_failed` collects ONLY CAS-won rows;
    the int-count return contract is unchanged; the cheap subscriber gate works.
  * Service (mocked) — the emit is subscriber-gated (nothing spawned when nobody
    listens), emits FAILED per row when someone does, and is fail-open.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: make src/backend importable (mirror test_1081_lease_sweep_exclusion).
# ---------------------------------------------------------------------------
_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun, scalar as _scalar  # noqa: E402

pytestmark = pytest.mark.unit

_STUBBED_MODULE_NAMES = [
    "utils", "utils.api_client", "utils.assertions", "utils.cleanup",
    "db.connection", "db.schedules", "db.operator_queue",
    "db.agent_settings.resources", "database",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@pytest.fixture
def tmp_db(db_backend):
    def _evict():
        for mod in ("db.connection", "db.schedules", "db.event_subscriptions",
                    "db.agent_settings.resources", "database"):
            sys.modules.pop(mod, None)
    _evict()
    try:
        yield db_backend
    finally:
        _evict()


@pytest.fixture
def seed_agent(tmp_db):
    def _seed(name: str):
        _hrun(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
            "VALUES (:n, 1, '2026-01-01T00:00:00Z')",
            n=name,
        )
    return _seed


@pytest.fixture
def running_row(tmp_db):
    """Insert a non-leased `running` row (session optional). Returns its id."""
    def _mk(agent_name: str, *, session: str | None = None, age_seconds: int = 3600) -> str:
        import secrets as _secrets
        eid = _secrets.token_urlsafe(12)
        started = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        _hrun(
            "INSERT INTO schedule_executions "
            "(id, schedule_id, agent_name, status, started_at, message, "
            " triggered_by, claude_session_id, redelivery_count) "
            "VALUES (:id, '__manual__', :a, 'running', :sa, 'do stuff', 'manual', :sess, 0)",
            id=eid, a=agent_name, sa=_iso(started), sess=session,
        )
        return eid
    return _mk


# ===========================================================================
# DB layer — collect_failed collects CAS-won rows; count contract preserved
# ===========================================================================


class TestCollectFailedStale:
    def test_collects_cas_won_rows(self, seed_agent, running_row):
        seed_agent("alpha")
        e1 = running_row("alpha", session="s1", age_seconds=3600)
        e2 = running_row("alpha", session="s2", age_seconds=3600)
        from database import db

        collected: list = []
        count = db.mark_stale_executions_failed(timeout_minutes=1, collect_failed=collected)

        assert count == 2                                   # int contract unchanged
        assert set(collected) == {(e1, "alpha"), (e2, "alpha")}
        assert all(isinstance(t, tuple) and len(t) == 2 for t in collected)

    def test_count_contract_unchanged_without_collect(self, seed_agent, running_row):
        seed_agent("alpha")
        running_row("alpha", session="s1", age_seconds=3600)
        from database import db
        # No collect_failed passed → still returns the int count, no crash.
        assert db.mark_stale_executions_failed(timeout_minutes=1) == 1

    def test_fresh_row_not_collected(self, seed_agent, running_row):
        """A row inside its window is neither failed nor collected."""
        seed_agent("alpha")
        running_row("alpha", session="s1", age_seconds=5)  # 5s old, 1-min window
        from database import db
        collected: list = []
        assert db.mark_stale_executions_failed(timeout_minutes=1, collect_failed=collected) == 0
        assert collected == []


class TestCollectFailedNoSession:
    def test_collects_cas_won_rows(self, seed_agent, running_row):
        seed_agent("beta")
        e1 = running_row("beta", session=None, age_seconds=3600)
        from database import db
        collected: list = []
        count = db.mark_no_session_executions_failed(timeout_seconds=60, collect_failed=collected)
        assert count == 1
        assert collected == [(e1, "beta")]


class TestSubscriberGate:
    def test_false_when_no_subs(self, tmp_db):
        from database import db
        assert db.has_task_terminal_subscribers() is False

    def test_true_with_enabled_task_failed_sub(self, seed_agent, tmp_db):
        seed_agent("alpha")
        seed_agent("watcher")
        _hrun(
            "INSERT INTO agent_event_subscriptions "
            "(id, subscriber_agent, source_agent, event_type, target_message, "
            " enabled, created_at, updated_at, created_by) "
            "VALUES ('sub1', 'watcher', 'alpha', 'agent.task.failed', 'go', 1, "
            " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '1')"
        )
        from database import db
        assert db.has_task_terminal_subscribers() is True

    def test_false_when_sub_disabled(self, seed_agent, tmp_db):
        seed_agent("alpha")
        _hrun(
            "INSERT INTO agent_event_subscriptions "
            "(id, subscriber_agent, source_agent, event_type, target_message, "
            " enabled, created_at, updated_at, created_by) "
            "VALUES ('sub2', 'watcher', 'alpha', 'agent.task.failed', 'go', 0, "
            " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '1')"
        )
        from database import db
        assert db.has_task_terminal_subscribers() is False


# ===========================================================================
# Service layer — _emit_bulk_terminal_events (mocked)
# ===========================================================================


def _svc_and_module():
    from services.cleanup_service import CleanupService
    cs_mod = sys.modules[CleanupService.__module__]
    return CleanupService(poll_interval=300), cs_mod


class TestBulkEmit:
    def test_empty_rows_no_work(self):
        svc, cs = _svc_and_module()
        with patch.object(cs, "db") as mdb, patch.object(cs, "event_dispatch_service") as med:
            asyncio.run(svc._emit_bulk_terminal_events([], "reason"))
            mdb.has_task_terminal_subscribers.assert_not_called()
            med.spawn_task_terminal_event.assert_not_called()

    def test_no_subscribers_skips_all_per_row_work(self):
        svc, cs = _svc_and_module()
        with patch.object(cs, "db") as mdb, patch.object(cs, "event_dispatch_service") as med:
            mdb.has_task_terminal_subscribers.return_value = False
            asyncio.run(svc._emit_bulk_terminal_events([("e1", "a"), ("e2", "a")], "r"))
            med.spawn_task_terminal_event.assert_not_called()

    def test_emits_failed_per_row_when_subscribed(self):
        svc, cs = _svc_and_module()
        with patch.object(cs, "db") as mdb, patch.object(cs, "event_dispatch_service") as med:
            mdb.has_task_terminal_subscribers.return_value = True
            rows = [("e1", "alpha"), ("e2", "beta")]
            asyncio.run(svc._emit_bulk_terminal_events(rows, "swept"))
            assert med.spawn_task_terminal_event.call_count == 2
            # every emit is FAILED, carries the right (agent, execution_id)
            called = {
                (c.args[1], c.args[0]): c.kwargs
                for c in med.spawn_task_terminal_event.call_args_list
            }
            assert set(called) == {("e1", "alpha"), ("e2", "beta")}
            from models import TaskExecutionStatus
            for kw in called.values():
                assert kw["terminal_status"] == TaskExecutionStatus.FAILED
                assert kw["summary_or_error"] == "swept"

    def test_skips_rows_with_missing_ids(self):
        svc, cs = _svc_and_module()
        with patch.object(cs, "db") as mdb, patch.object(cs, "event_dispatch_service") as med:
            mdb.has_task_terminal_subscribers.return_value = True
            asyncio.run(svc._emit_bulk_terminal_events([(None, "a"), ("e2", None), ("e3", "c")], "r"))
            assert med.spawn_task_terminal_event.call_count == 1

    def test_fail_open_on_spawn_error(self):
        svc, cs = _svc_and_module()
        with patch.object(cs, "db") as mdb, patch.object(cs, "event_dispatch_service") as med:
            mdb.has_task_terminal_subscribers.return_value = True
            med.spawn_task_terminal_event.side_effect = RuntimeError("boom")
            # Must NOT raise — the terminal write already committed; emit is best-effort.
            asyncio.run(svc._emit_bulk_terminal_events([("e1", "a")], "r"))

    def test_paces_large_batch_without_error(self):
        svc, cs = _svc_and_module()
        with patch.object(cs, "db") as mdb, patch.object(cs, "event_dispatch_service") as med:
            mdb.has_task_terminal_subscribers.return_value = True
            rows = [(f"e{i}", "a") for i in range(120)]  # > 2 batches of 50
            asyncio.run(svc._emit_bulk_terminal_events(rows, "r"))
            assert med.spawn_task_terminal_event.call_count == 120
