"""#3245: a failed async run stops replaying its receipt.

An async ``/task`` (or a timed-out sync ``/task`` / pulled ``/chat``) stores a
*receipt* — ``{"status": "accepted"|"queued"|"queued_timeout", "execution_id",
"async_mode": True}`` — as the ``completed`` idempotency row. Before #3245
nothing revisited it, so for 24h an identical request got the dead run's
receipt back. The fix checks, at replay time in the shared admission seam,
whether the run can still succeed; if it ended (or its row is gone) the row is
compare-and-deleted and the request dispatches fresh.

Runs on a throwaway SQLite file (``DATABASE_URL`` + ``dispose_engines``) with
the real ``IdempotencyOperations`` / ``ScheduleOperations``. No ``sys.modules``
surgery; the service under test is patched through the module object the code
actually reads (``dispatch_admission_service.idempotency_service``).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import insert, update

STALE = "exec-stale-7f3"
FRESH = "exec-fresh-b21"
AGENT = "agent-3245"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(monkeypatch, tmp_path):
    """Real idempotency + execution tables on a temp SQLite file."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't3245.db'}")
    from db import engine as eng
    eng.dispose_engines()
    from db.tables import metadata, idempotency_keys, schedule_executions
    metadata.create_all(eng.get_engine(), tables=[idempotency_keys, schedule_executions])
    from db.idempotency import IdempotencyOperations
    from db.schedules import ScheduleOperations
    yield SimpleNamespace(
        idem=IdempotencyOperations(),
        sched=ScheduleOperations(None, None),
        engine=eng.get_engine(),
        executions=schedule_executions,
        keys=idempotency_keys,
    )
    eng.dispose_engines()


def _insert_exec(store, execution_id, status, *, agent=AGENT, error=None,
                 completed_at=None, started_at="2026-10-06T11:00:00Z"):
    with store.engine.begin() as conn:
        conn.execute(insert(store.executions).values(
            id=execution_id, agent_name=agent, status=status, error=error,
            started_at=started_at, completed_at=completed_at, triggered_by="agent",
        ))


def _set_exec(store, execution_id, **values):
    with store.engine.begin() as conn:
        conn.execute(update(store.executions)
                     .where(store.executions.c.id == execution_id).values(**values))


def _receipt(execution_id, status="accepted"):
    return {"status": status, "execution_id": execution_id,
            "agent_name": AGENT, "async_mode": True}


# ---------------------------------------------------------------------------
# DB layer: compare-and-delete + the narrow failure-stamp read
# ---------------------------------------------------------------------------

class TestDiscardCompletedIfExecution:
    S, K = f"agent:{AGENT}", "k-cas"

    def test_deletes_only_the_row_still_naming_that_execution(self, store):
        assert store.idem.claim(self.S, self.K)["state"] == "new"
        store.idem.complete(self.S, self.K, STALE, _receipt(STALE))
        assert store.idem.discard_completed_if_execution(self.S, self.K, FRESH) is False
        assert store.idem.claim(self.S, self.K)["execution_id"] == STALE
        assert store.idem.discard_completed_if_execution(self.S, self.K, STALE) is True
        assert store.idem.claim(self.S, self.K)["state"] == "new"

    def test_slow_retry_cannot_erase_the_fast_retrys_fresh_record(self, store):
        """Dossier §8: A and B both saw the completed row naming STALE. A
        discards and re-completes as FRESH; B's late discard must be a no-op."""
        store.idem.claim(self.S, self.K)
        store.idem.complete(self.S, self.K, STALE, _receipt(STALE))
        assert store.idem.discard_completed_if_execution(self.S, self.K, STALE) is True  # A
        assert store.idem.claim(self.S, self.K)["state"] == "new"                        # A
        store.idem.complete(self.S, self.K, FRESH, _receipt(FRESH))                      # A
        assert store.idem.discard_completed_if_execution(self.S, self.K, STALE) is False  # B
        after = store.idem.claim(self.S, self.K)
        assert after["state"] == "completed" and after["execution_id"] == FRESH

    def test_never_deletes_an_in_flight_claim(self, store):
        store.idem.claim(self.S, self.K)
        store.idem.attach_execution(self.S, self.K, STALE)
        assert store.idem.discard_completed_if_execution(self.S, self.K, STALE) is False
        assert store.idem.claim(self.S, self.K)["state"] == "in_flight"

    def test_facade_delegates(self, store):
        from database import DatabaseManager
        fake_self = SimpleNamespace(_idempotency_ops=store.idem)
        store.idem.claim(self.S, self.K)
        store.idem.complete(self.S, self.K, STALE, _receipt(STALE))
        assert DatabaseManager.idempotency_discard_completed_if_execution(
            fake_self, self.S, self.K, STALE) is True


class TestExecutionFailureStamp:
    def test_returns_status_error_and_completed_at(self, store):
        _insert_exec(store, STALE, "failed", error="lease_expired: Stale execution",
                     completed_at="2026-10-06T12:00:00Z")
        assert store.sched.get_execution_failure_stamp(STALE) == (
            "failed", "lease_expired: Stale execution", "2026-10-06T12:00:00Z")

    def test_missing_row_is_none(self, store):
        assert store.sched.get_execution_failure_stamp("exec-nope-000") is None

    def test_facade_delegates(self, store):
        from database import DatabaseManager
        _insert_exec(store, STALE, "failed", error=None, completed_at="2026-10-06T12:00:00Z")
        assert DatabaseManager.get_execution_failure_stamp(
            SimpleNamespace(_schedule_ops=store.sched), STALE) == (
            "failed", None, "2026-10-06T12:00:00Z")
