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


# ---------------------------------------------------------------------------
# Service: execution_liveness / discard_replay_of
# ---------------------------------------------------------------------------

T0 = "2026-10-06T12:00:00Z"          # completed_at the slot reaper wrote
TIMEOUT = 600                         # agent timeout; window = 600 + 300 = 900s
TAGGED = "lease_expired: Stale execution — slot TTL expired for agent 'agent-3245'"


def _at(seconds):
    from datetime import datetime, timedelta, timezone
    return datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)


class _Spy:
    """Counts calls; optionally raises."""

    def __init__(self, fn, raises=None):
        self.fn, self.raises, self.calls = fn, raises, 0

    def __call__(self, *a, **kw):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.fn(*a, **kw)


@pytest.fixture
def svc(store, monkeypatch):
    """`idempotency_service` (the very module the admission seam reads) over a
    db stand-in whose every method is the REAL db op on the temp file, wrapped
    in a call-counting spy. `timeout` is the one non-table value."""
    import services.dispatch_admission_service as das
    isvc = das.idempotency_service
    state = SimpleNamespace(timeout=TIMEOUT)
    fake = SimpleNamespace(
        idempotency_claim=store.idem.claim,
        idempotency_complete=store.idem.complete,
        idempotency_release=store.idem.release,
        idempotency_attach_execution=store.idem.attach_execution,
        idempotency_discard_completed=store.idem.discard_completed,
        idempotency_discard_completed_if_execution=_Spy(
            getattr(store.idem, "discard_completed_if_execution", None)),
        get_execution=store.sched.get_execution,
        get_execution_gate_state=_Spy(store.sched.get_execution_gate_state),
        get_execution_failure_stamp=_Spy(
            getattr(store.sched, "get_execution_failure_stamp", None)),
        get_execution_timeout=_Spy(lambda name: state.timeout),
    )
    monkeypatch.setattr(isvc, "db", fake)
    return SimpleNamespace(isvc=isvc, das=das, db=fake, state=state, store=store)


def _verdict(svc, execution_id=STALE, *, now=None, agent=AGENT):
    return svc.isvc.execution_liveness(execution_id, agent, now=now).verdict


class TestExecutionLivenessVerdicts:
    @pytest.mark.parametrize("status,expected", [
        ("queued", "live"), ("running", "live"), ("pending_retry", "live"),
        ("success", "succeeded"),
        ("cancelled", "ended"), ("skipped", "ended"),
        ("error", "indeterminate"),          # legacy / unknown status
    ])
    def test_status_matrix(self, svc, status, expected):
        _insert_exec(svc.store, STALE, status, completed_at=T0)
        assert _verdict(svc, now=_at(1)) == expected

    @pytest.mark.parametrize("error", [
        "boom", None, "Marked as failed by cleanup: exceeded 3600s stale timeout",
    ])
    def test_untagged_failed_is_ended_at_once(self, svc, error):
        _insert_exec(svc.store, STALE, "failed", error=error, completed_at=T0)
        assert _verdict(svc, now=_at(1)) == "ended"

    def test_no_row_is_gone(self, svc):
        assert _verdict(svc, "exec-vanished-404") == "gone"

    def test_resolver_unknown_is_verdict_gone(self, svc):
        """The effect resolver's word for "no such execution" is `unknown`;
        the liveness verdict for the same fact is `gone` (decision 3)."""
        assert svc.isvc._resolve_execution_with_reason(
            "exec-vanished-404", AGENT, log=False)[1] == "unknown"
        assert _verdict(svc, "exec-vanished-404") == "gone"

    def test_foreign_owner_is_indeterminate(self, svc):
        _insert_exec(svc.store, STALE, "failed", agent="some-other-agent", completed_at=T0)
        assert _verdict(svc) == "indeterminate"

    @pytest.mark.parametrize("execution_id", [None, "", "manual"])
    def test_no_usable_id_is_indeterminate_without_a_read(self, svc, execution_id):
        assert _verdict(svc, execution_id) == "indeterminate"
        assert svc.db.get_execution_gate_state.calls == 0

    def test_gate_read_raising_is_indeterminate(self, svc):
        svc.db.get_execution_gate_state.raises = RuntimeError("db down")
        assert _verdict(svc) == "indeterminate"

    def test_only_a_failed_row_pays_the_second_read(self, svc):
        _insert_exec(svc.store, STALE, "running")
        _verdict(svc)
        assert svc.db.get_execution_failure_stamp.calls == 0


class TestLeaseExpiredWindow:
    """Decision 1 (UC1): a `lease_expired` FAILED may still report success, so it
    replays until completed_at + timeout + SLOT_TTL_BUFFER, then goes fresh."""

    def test_time_axis_boundary(self, svc):
        _insert_exec(svc.store, STALE, "failed", error=TAGGED, completed_at=T0)
        held = svc.isvc.execution_liveness(STALE, AGENT, now=_at(899))
        assert held.verdict == "maybe_alive" and held.hold_until == _at(900)
        assert _verdict(svc, now=_at(900)) == "ended"
        assert _verdict(svc, now=_at(901)) == "ended"

    def test_window_uses_the_real_slot_buffer(self, svc):
        from services.slot_service import SLOT_TTL_BUFFER
        _insert_exec(svc.store, STALE, "failed", error=TAGGED, completed_at=T0)
        assert svc.isvc.execution_liveness(STALE, AGENT, now=_at(1)).hold_until == _at(
            TIMEOUT + SLOT_TTL_BUFFER)

    def test_late_success_inside_the_window_is_succeeded(self, svc):
        _insert_exec(svc.store, STALE, "failed", error=TAGGED, completed_at=T0)
        assert _verdict(svc, now=_at(10)) == "maybe_alive"
        _set_exec(svc.store, STALE, status="success")
        assert _verdict(svc, now=_at(10)) == "succeeded"

    def test_status_flip_between_the_two_reads_is_indeterminate(self, svc):
        _insert_exec(svc.store, STALE, "success", error=TAGGED, completed_at=T0)
        svc.db.get_execution_gate_state.fn = lambda eid: (AGENT, "failed")
        assert _verdict(svc, now=_at(1)) == "indeterminate"

    @pytest.mark.parametrize("completed_at", [None, "not-a-timestamp"])
    def test_tagged_without_a_usable_stamp_is_indeterminate(self, svc, completed_at):
        _insert_exec(svc.store, STALE, "failed", error=TAGGED, completed_at=completed_at)
        assert _verdict(svc, now=_at(10_000)) == "indeterminate"

    def test_timeout_lookup_raising_is_indeterminate(self, svc):
        _insert_exec(svc.store, STALE, "failed", error=TAGGED, completed_at=T0)
        svc.db.get_execution_timeout.raises = RuntimeError("settings unreadable")
        assert _verdict(svc, now=_at(10_000)) == "indeterminate"

    def test_second_read_raising_is_indeterminate(self, svc):
        _insert_exec(svc.store, STALE, "failed", error=TAGGED, completed_at=T0)
        svc.db.get_execution_failure_stamp.raises = RuntimeError("db down")
        assert _verdict(svc, now=_at(10_000)) == "indeterminate"

    def test_tag_is_checked_before_the_stamp_or_the_timeout(self, svc):
        """Codex finding 4: an ordinary failure is never held by a broken
        timestamp or a timeout lookup error."""
        _insert_exec(svc.store, STALE, "failed", error="boom", completed_at=None)
        svc.db.get_execution_timeout.raises = RuntimeError("settings unreadable")
        assert _verdict(svc, now=_at(1)) == "ended"
        assert svc.db.get_execution_timeout.calls == 0

    def test_naive_stored_completed_at_is_read_as_utc(self, svc):
        _insert_exec(svc.store, STALE, "failed", error=TAGGED,
                     completed_at="2026-10-06T12:00:00")
        assert _verdict(svc, now=_at(899)) == "maybe_alive"
        assert _verdict(svc, now=_at(900)) == "ended"

    def test_horizon_uses_the_current_timeout(self, svc):
        """Documented choice: the agent's timeout NOW, not at dispatch."""
        _insert_exec(svc.store, STALE, "failed", error=TAGGED, completed_at=T0)
        assert _verdict(svc, now=_at(899)) == "maybe_alive"
        svc.state.timeout = 60                       # window shrinks to 360s
        assert _verdict(svc, now=_at(899)) == "ended"

    def test_clock_hook_is_used_only_when_no_clock_is_given(self, svc, monkeypatch):
        _insert_exec(svc.store, STALE, "failed", error=TAGGED, completed_at=T0)
        clock = _Spy(lambda: _at(899))
        monkeypatch.setattr(svc.isvc, "_now", clock)
        assert svc.isvc.execution_liveness(STALE, AGENT).verdict == "maybe_alive"
        assert clock.calls == 1
        assert _verdict(svc, now=_at(901)) == "ended"
        assert clock.calls == 1


class TestDiscardReplayOf:
    S, K = f"agent:{AGENT}", "k-discard"

    def test_true_false_and_none(self, svc, caplog):
        svc.store.idem.claim(self.S, self.K)
        svc.store.idem.complete(self.S, self.K, STALE, _receipt(STALE))
        assert svc.isvc.discard_replay_of(self.S, self.K, FRESH) is False
        assert svc.isvc.discard_replay_of(self.S, self.K, STALE) is True
        svc.db.idempotency_discard_completed_if_execution.raises = RuntimeError("locked")
        with caplog.at_level("WARNING"):
            assert svc.isvc.discard_replay_of(self.S, self.K, STALE) is None
        assert "discard_replay_of" in caplog.text and self.K not in caplog.text


# ---------------------------------------------------------------------------
# Guards: the `lease_expired` tag the window keys on
# ---------------------------------------------------------------------------

def _backend_root():
    import pathlib
    import services
    return pathlib.Path(services.__file__).resolve().parent.parent


def test_lease_expired_tag_parity():
    """`cleanup_service._LEASE_EXPIRED_TAG` (private, the writer's spelling) must
    equal the enum value the liveness check derives its prefix from."""
    import ast
    from services.execution_envelope import TaskExecutionErrorCode
    tree = ast.parse((_backend_root() / "services" / "cleanup_service.py").read_text())
    values = [n.value.value for n in ast.walk(tree)
              if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == "_LEASE_EXPIRED_TAG" for t in n.targets)]
    assert values == [TaskExecutionErrorCode.LEASE_EXPIRED.value]


def test_every_slot_reaper_failure_carries_the_tag():
    """UC1 covers exactly the rows whose error starts `lease_expired: `. A new
    `fail_stale_slot_execution(` writer without the tag would silently drop out
    of the hold window — fail it here instead."""
    import ast
    root = _backend_root()
    found = 0
    for path in sorted(root.rglob("*.py")):
        if path.name == "database.py" or "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "fail_stale_slot_execution"):
                continue
            found += 1
            err = next((k.value for k in node.keywords if k.arg == "error"), None)
            where = f"{path.relative_to(root)}:{node.lineno}"
            assert isinstance(err, ast.JoinedStr), where
            first, second = err.values[0], err.values[1]
            assert isinstance(first, ast.FormattedValue) and isinstance(first.value, ast.Name) \
                and first.value.id == "_LEASE_EXPIRED_TAG", where
            assert isinstance(second, ast.Constant) and second.value.startswith(": "), where
    assert found >= 2, "the slot-reaper writers were not found — the guard went blind"
