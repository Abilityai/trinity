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
    from db.tables import metadata, idempotency_keys, schedule_executions, agent_skill_gates
    # trinity-enterprise#753: the dispatch gate reads the skill gate map; an
    # unreadable map refuses (503), so the table must exist (empty = ungated).
    metadata.create_all(eng.get_engine(), tables=[idempotency_keys, schedule_executions,
                                                  agent_skill_gates])
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


# ---------------------------------------------------------------------------
# Admission seam: _reclaim_ended_receipt in begin_task_idempotency (/task) and
# admit_chat_request (/chat)
# ---------------------------------------------------------------------------

SCOPE = f"agent:{AGENT}"
RECEIPT_STATUSES = ["accepted", "queued", "queued_timeout"]


def _store_receipt(svc, key, execution_id=STALE, status="accepted"):
    assert svc.store.idem.claim(SCOPE, key)["state"] == "new"
    svc.store.idem.complete(SCOPE, key, execution_id, _receipt(execution_id, status))


def _row(svc, key):
    """Current (state, execution_id) of the key's row — read without claiming."""
    from sqlalchemy import select
    k = svc.store.keys
    with svc.store.engine.connect() as conn:
        r = conn.execute(select(k.c.status, k.c.execution_id).where(
            (k.c.scope == SCOPE) & (k.c.idempotency_key == key))).first()
    return (r.status, r.execution_id) if r else None


def _task(svc, key):
    return svc.das.begin_task_idempotency(name=AGENT, idempotency_key=key)


def _user():
    return SimpleNamespace(id=1, email="u1@example.com", username="u1", agent_name=None,
                           mcp_key_id=None, mcp_key_name=None, role="user")


def _admit(svc, monkeypatch, key, *, pilot=False, gate=None, gate_exc=None):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from models import ChatMessageRequest
    from services.skill_gate_service import GateDecision
    das = svc.das
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", AGENT if pilot else "")
    monkeypatch.setattr(das, "enforce_inter_agent_depth", AsyncMock(return_value=None))
    monkeypatch.setattr(das.skill_gate_service, "enforce", AsyncMock(
        side_effect=gate_exc, return_value=gate or GateDecision()))
    monkeypatch.setattr(das, "dispatch_breaker_active", lambda name: False)
    cap = MagicMock(acquire=AsyncMock(return_value=SimpleNamespace(
        state="admitted", queue_position=0)))
    monkeypatch.setattr(das, "get_capacity_manager", lambda: cap)
    monkeypatch.setattr(das, "platform_audit_service", MagicMock(log=AsyncMock()))
    monkeypatch.setattr(das, "db", SimpleNamespace(
        get_execution_timeout=lambda name: 900, get_max_parallel_tasks=lambda name: 3))
    return asyncio.run(das.admit_chat_request(
        name=AGENT, request=ChatMessageRequest(message="hi"), current_user=_user(),
        x_source_agent=None, x_via_mcp="true", idempotency_key=key,
    ))


class TestTaskSeamAC1:
    @pytest.mark.parametrize("receipt_status", RECEIPT_STATUSES)
    @pytest.mark.parametrize("status", ["failed", "cancelled", "skipped"])
    def test_ended_run_dispatches_fresh(self, svc, status, receipt_status):
        _insert_exec(svc.store, STALE, status, error="boom", completed_at=T0)
        _store_receipt(svc, "k-ac1", status=receipt_status)
        idem, replay = _task(svc, "k-ac1")
        assert replay is None and idem.enabled and not idem.replay
        assert _row(svc, "k-ac1") == ("in_flight", None)
        assert svc.db.get_execution_gate_state.calls == 1

    @pytest.mark.parametrize("receipt_status", RECEIPT_STATUSES)
    @pytest.mark.parametrize("status", ["queued", "running", "pending_retry", "success"])
    def test_live_or_succeeded_run_replays(self, svc, status, receipt_status):
        """Guard (green on dev too): a run that can still succeed replays."""
        _insert_exec(svc.store, STALE, status, completed_at=T0)
        _store_receipt(svc, "k-ac1", status=receipt_status)
        idem, replay = _task(svc, "k-ac1")
        assert replay is not None and replay.execution_id == STALE and not replay.in_flight
        assert _row(svc, "k-ac1") == ("completed", STALE)

    def test_gone_row_dispatches_fresh(self, svc):
        _store_receipt(svc, "k-gone")
        assert _task(svc, "k-gone")[1] is None
        assert svc.db.get_execution_gate_state.calls == 1

    def test_pull_routed_task_receipt_shapes_dispatch_fresh(self, svc):
        """#946's pull-routed /task stores the same accepted/queued receipt."""
        _insert_exec(svc.store, STALE, "failed", error="boom", completed_at=T0)
        for key, st in (("k-946-a", "accepted"), ("k-946-q", "queued")):
            _store_receipt(svc, key, status=st)
            assert _task(svc, key)[1] is None

    @pytest.mark.parametrize("case", ["raises", "foreign", "legacy_status"])
    def test_cannot_tell_replays(self, svc, case):
        if case == "raises":
            svc.db.get_execution_gate_state.raises = RuntimeError("db down")
        elif case == "foreign":
            _insert_exec(svc.store, STALE, "failed", agent="other-agent", completed_at=T0)
        else:
            _insert_exec(svc.store, STALE, "error", completed_at=T0)
        _store_receipt(svc, "k-edge")
        assert _task(svc, "k-edge")[1].execution_id == STALE
        assert _row(svc, "k-edge") == ("completed", STALE)

    def test_manual_execution_id_replays(self, svc):
        _store_receipt(svc, "k-manual", execution_id="manual")
        assert _task(svc, "k-manual")[1].execution_id == "manual"
        assert svc.db.get_execution_gate_state.calls == 0

    @pytest.mark.parametrize("snapshot", [
        {"response": "done", "task_execution_id": STALE},
        {"execution": {"task_execution_id": STALE}},
        {"status": "accepted", "execution_id": STALE, "async_mode": False},
        {"status": "success", "execution_id": STALE, "async_mode": True},
    ])
    def test_non_receipt_snapshot_replays_without_a_read(self, svc, snapshot):
        _insert_exec(svc.store, STALE, "failed", error="boom", completed_at=T0)
        svc.store.idem.claim(SCOPE, "k-sync")
        svc.store.idem.complete(SCOPE, "k-sync", STALE, snapshot)
        assert _task(svc, "k-sync")[1].snapshot == snapshot
        assert svc.db.get_execution_gate_state.calls == 0

    def test_pre_fix_row_written_by_raw_sql_dispatches_fresh(self, svc):
        import json
        _insert_exec(svc.store, STALE, "failed", error="boom", completed_at=T0)
        with svc.store.engine.begin() as conn:
            conn.execute(insert(svc.store.keys).values(
                scope=SCOPE, idempotency_key="k-legacy", execution_id=STALE,
                status="completed", response_snapshot=json.dumps(_receipt(STALE)),
                created_at="2026-10-06T11:00:00Z", updated_at="2026-10-06T11:00:00Z"))
        assert _task(svc, "k-legacy")[1] is None

    def test_scheduler_key_is_never_reclaimed(self, svc):
        _insert_exec(svc.store, STALE, "failed", error="boom", completed_at=T0)
        _store_receipt(svc, f"sched:{STALE}")
        assert _task(svc, f"sched:{STALE}")[1].execution_id == STALE
        assert svc.db.get_execution_gate_state.calls == 0


class TestLeaseExpiredAtTheSeam:
    @pytest.mark.parametrize("offset,fresh", [(899, False), (900, True), (901, True)])
    def test_time_axis(self, svc, monkeypatch, caplog, offset, fresh):
        _insert_exec(svc.store, STALE, "failed", error=TAGGED, completed_at=T0)
        _store_receipt(svc, "k-uc1")
        monkeypatch.setattr(svc.isvc, "_now", lambda: _at(offset))
        with caplog.at_level("INFO"):
            idem, replay = _task(svc, "k-uc1")
        assert (replay is None) is fresh
        if not fresh:
            assert replay.execution_id == STALE
            assert "receipt_hold" in caplog.text
            assert "verdict=maybe_alive" in caplog.text
            assert f"until={_at(900).isoformat()}" in caplog.text
            assert "k-uc1" not in caplog.text

    @pytest.mark.parametrize("error", [
        "boom", None, "Marked as failed by cleanup: exceeded 3600s stale timeout"])
    def test_untagged_failure_is_fresh_at_once(self, svc, monkeypatch, error):
        _insert_exec(svc.store, STALE, "failed", error=error, completed_at=T0)
        _store_receipt(svc, "k-uc1")
        monkeypatch.setattr(svc.isvc, "_now", lambda: _at(1))
        assert _task(svc, "k-uc1")[1] is None


class TestChatSeam:
    def test_failed_queued_timeout_receipt_admits_fresh(self, svc, monkeypatch):
        from services.chat_signals import ChatAdmission
        _insert_exec(svc.store, STALE, "failed", error="boom", completed_at=T0)
        _store_receipt(svc, "k-chat", status="queued_timeout")
        admission = _admit(svc, monkeypatch, "k-chat")
        assert isinstance(admission, ChatAdmission)
        assert admission.idem.enabled and not admission.idem.replay
        assert _row(svc, "k-chat") == ("in_flight", None)

    def test_running_receipt_still_replays(self, svc, monkeypatch):
        from services.chat_signals import ChatAdmissionReplay
        _insert_exec(svc.store, STALE, "running")
        _store_receipt(svc, "k-chat", status="queued_timeout")
        out = _admit(svc, monkeypatch, "k-chat")
        assert isinstance(out, ChatAdmissionReplay) and out.execution_id == STALE

    def test_scheduler_key_is_never_reclaimed(self, svc, monkeypatch):
        from services.chat_signals import ChatAdmissionReplay
        _insert_exec(svc.store, STALE, "failed", error="boom", completed_at=T0)
        _store_receipt(svc, f"sched:{STALE}", status="queued_timeout")
        assert isinstance(_admit(svc, monkeypatch, f"sched:{STALE}"), ChatAdmissionReplay)

    @pytest.mark.parametrize("which", ["decided", "pending"])
    def test_gate_record_answers_before_the_receipt(self, svc, monkeypatch, which):
        from services.skill_gate_errors import SkillApprovalRequired, SkillGateRefused
        exc = (SkillGateRefused(409, "request_denied", "denied") if which == "decided"
               else SkillApprovalRequired(request_id="req-1", agent_name=AGENT,
                                          skills=["deploy"], approver_role="owner",
                                          expires_at=None))
        _insert_exec(svc.store, STALE, "failed", error="boom", completed_at=T0)
        _store_receipt(svc, "k-gated", status="queued_timeout")
        with pytest.raises(type(exc)):
            _admit(svc, monkeypatch, "k-gated", gate_exc=exc)
        assert _row(svc, "k-gated") == ("completed", STALE)
        assert svc.db.get_execution_gate_state.calls == 0

    def test_self_approver_reaches_the_reclaim(self, svc, monkeypatch):
        from services.skill_gate_service import GateDecision
        gate = GateDecision(skills=("deploy",), self_approved_by="u1")
        _insert_exec(svc.store, STALE, "failed", error="boom", completed_at=T0)
        _store_receipt(svc, "k-gated", status="queued_timeout")
        admission = _admit(svc, monkeypatch, "k-gated", gate=gate)
        assert admission.gate is gate and not admission.idem.replay

    def test_pulled_chat_timeout_receipt_then_failure_admits_fresh(self, svc, monkeypatch):
        """#3145 (required): the receipt is written by the REAL
        `run_pulled_chat_turn` wait-timeout branch through the real idempotency
        layer; once its row fails, an identical /chat on the pilot is admitted
        fresh onto the queue (`capacity=None`)."""
        import asyncio
        import importlib
        from unittest.mock import AsyncMock, MagicMock
        from models import ChatMessageRequest
        from services.chat_signals import ChatAdmission, ChatDispatchError
        from services.execution_envelope import TaskExecutionErrorCode, TaskExecutionResult

        first = _admit(svc, monkeypatch, "k-pulled", pilot=True)
        assert isinstance(first, ChatAdmission) and first.capacity is None
        eid = first.execution_id
        _insert_exec(svc.store, eid, "running")

        ce = importlib.import_module("services.chat_execution_service")
        tes = importlib.import_module("services.task_execution_service")
        monkeypatch.setattr(ce, "idempotency_service", svc.isvc)
        monkeypatch.setattr(ce, "db", SimpleNamespace(
            get_chat_session_claude_id=lambda sid: None))
        timed_out = TaskExecutionResult(
            execution_id=eid, status="failed", response="", error="wait timed out",
            error_code=TaskExecutionErrorCode.TIMEOUT)
        monkeypatch.setattr(ce.session_turn_service, "run_resumable_turn",
                            AsyncMock(return_value=SimpleNamespace(result=timed_out)))
        monkeypatch.setattr(tes, "result_from_execution_row", lambda execution_id: None)
        with pytest.raises(ChatDispatchError) as exc:
            asyncio.run(ce.run_pulled_chat_turn(
                name=AGENT, request=ChatMessageRequest(message="hi"), current_user=_user(),
                x_source_agent=None, triggered_by="mcp", task_execution_id=eid,
                _chat_subscription_id=None, chat_activity_id="act-chat",
                collaboration_activity_id=None, session=SimpleNamespace(id="cs-1"),
                execution=SimpleNamespace(id="q1"), queue_result="queued", idem=first.idem,
            ))
        assert exc.value.status_code == 504
        assert _row(svc, "k-pulled") == ("completed", eid)

        _set_exec(svc.store, eid, status="failed", error="boom", completed_at=T0)
        second = _admit(svc, monkeypatch, "k-pulled", pilot=True)
        assert isinstance(second, ChatAdmission) and second.capacity is None
        assert second.execution_id != eid and not second.idem.replay


class TestRetryGenerationsAndRaces:
    def test_each_dead_generation_is_reclaimed_once(self, svc):
        e1, e2 = "exec-gen1-c11", "exec-gen2-c22"
        _insert_exec(svc.store, e1, "failed", error="boom", completed_at=T0)
        _store_receipt(svc, "k-gen", execution_id=e1)
        a, _ = _task(svc, "k-gen")                                  # A: E1 → E2
        svc.isvc.complete(a, e2, _receipt(e2))
        _insert_exec(svc.store, e2, "running")
        assert _task(svc, "k-gen")[1].execution_id == e2           # E2 live → replay
        _set_exec(svc.store, e2, status="failed", error="boom", completed_at=T0)
        b, replay = _task(svc, "k-gen")                             # B: E2 → E3
        assert replay is None and not b.replay

    def _two_observers(self, svc):
        """A and B both begin() and see the completed row naming the dead STALE."""
        _insert_exec(svc.store, STALE, "failed", error="boom", completed_at=T0)
        _store_receipt(svc, "k-race")
        isvc = svc.isvc
        a = isvc.begin(SCOPE, "k-race")
        b = isvc.begin(SCOPE, "k-race")
        assert a.replay and b.replay and a.execution_id == b.execution_id == STALE
        return a, b

    def _new_count(self, *decisions):
        return sum(1 for d in decisions if d.enabled and not d.replay)

    def test_a_completes_before_b_reclaims(self, svc):
        a, b = self._two_observers(svc)
        a2 = svc.das._reclaim_ended_receipt(a, name=AGENT)
        svc.isvc.complete(a2, FRESH, _receipt(FRESH))
        _insert_exec(svc.store, FRESH, "running")
        b2 = svc.das._reclaim_ended_receipt(b, name=AGENT)
        assert self._new_count(a2, b2) == 1
        assert b2.replay and not b2.in_flight and b2.execution_id == FRESH

    def test_b_reclaims_while_a_is_in_flight(self, svc):
        a, b = self._two_observers(svc)
        a2 = svc.das._reclaim_ended_receipt(a, name=AGENT)
        b2 = svc.das._reclaim_ended_receipt(b, name=AGENT)
        assert self._new_count(a2, b2) == 1
        assert b2.replay and b2.in_flight

    def test_an_unconditional_delete_would_start_two_runs(self, svc, monkeypatch):
        """Teeth: swap the CAS for #2040's unconditional `discard_completed`
        and the first interleave starts two runs — what the CAS prevents."""
        a, b = self._two_observers(svc)
        svc.db.idempotency_discard_completed_if_execution.fn = (
            lambda s, k, eid: svc.store.idem.discard_completed(s, k) or True)
        a2 = svc.das._reclaim_ended_receipt(a, name=AGENT)
        svc.isvc.complete(a2, FRESH, _receipt(FRESH))
        _insert_exec(svc.store, FRESH, "running")
        b2 = svc.das._reclaim_ended_receipt(b, name=AGENT)
        assert self._new_count(a2, b2) == 2


class TestReclaimLog:
    def _log(self, caplog):
        lines = [r.getMessage() for r in caplog.records if "receipt_reclaim" in r.getMessage()]
        assert len(lines) == 1, lines
        assert "k-log" not in lines[0]
        return lines[0]

    def _setup(self, svc):
        _insert_exec(svc.store, STALE, "failed", error="boom", completed_at=T0)
        _store_receipt(svc, "k-log")
        return svc.isvc.begin(SCOPE, "k-log")

    def test_deleted_new(self, svc, caplog):
        idem = self._setup(svc)
        with caplog.at_level("INFO"):
            svc.das._reclaim_ended_receipt(idem, name=AGENT)
        line = self._log(caplog)
        assert f"agent={AGENT}" in line and f"stale_exec={STALE}" in line
        assert "verdict=ended" in line and "cas=deleted" in line and "outcome=new" in line

    def test_no_match_in_flight(self, svc, caplog):
        idem = self._setup(svc)
        svc.store.idem.discard_completed(SCOPE, "k-log")
        svc.store.idem.claim(SCOPE, "k-log")                     # another retry won
        with caplog.at_level("INFO"):
            out = svc.das._reclaim_ended_receipt(idem, name=AGENT)
        assert out.in_flight
        line = self._log(caplog)
        assert "cas=no_match" in line and "outcome=in_flight" in line

    def test_no_match_completed_names_the_winner(self, svc, caplog):
        idem = self._setup(svc)
        svc.store.idem.discard_completed(SCOPE, "k-log")
        svc.store.idem.claim(SCOPE, "k-log")
        svc.store.idem.complete(SCOPE, "k-log", FRESH, _receipt(FRESH))
        with caplog.at_level("INFO"):
            out = svc.das._reclaim_ended_receipt(idem, name=AGENT)
        assert out.execution_id == FRESH
        line = self._log(caplog)
        assert "cas=no_match" in line and "outcome=completed" in line
        assert f"replay_exec={FRESH}" in line

    def test_delete_error_replays_the_original_without_a_second_begin(self, svc, caplog,
                                                                         monkeypatch):
        idem = self._setup(svc)
        svc.db.idempotency_discard_completed_if_execution.raises = RuntimeError("locked")
        claims = _Spy(svc.store.idem.claim)
        monkeypatch.setattr(svc.db, "idempotency_claim", claims)
        with caplog.at_level("INFO"):
            out = svc.das._reclaim_ended_receipt(idem, name=AGENT)
        assert out is idem and claims.calls == 0
        line = self._log(caplog)
        assert "cas=error" in line and "outcome=replay_original" in line
        assert f"replay_exec={STALE}" in line

    def test_second_begin_failing_open_is_no_dedup(self, svc, caplog, monkeypatch):
        idem = self._setup(svc)
        monkeypatch.setattr(svc.db, "idempotency_claim", _Spy(None, raises=RuntimeError("x")))
        with caplog.at_level("INFO"):
            out = svc.das._reclaim_ended_receipt(idem, name=AGENT)
        assert not out.enabled and not out.replay
        assert "outcome=no_dedup" in self._log(caplog)


class TestAC2EndToEnd:
    """AC2 over the real `/task` endpoint (`routers.chat.execute_parallel_task`),
    collaborators mocked as in test_1483, the idempotency layer real."""

    def test_dispatch_fail_resend_new_id_then_replay_the_new_run(self, svc, monkeypatch):
        import asyncio
        import sys
        from unittest.mock import AsyncMock, MagicMock
        from fastapi.responses import JSONResponse
        from routers.chat import execute_parallel_task
        from models import ParallelTaskRequest
        from services import chat_persistence_service
        import services.chat_execution_service as ce

        router_mod = sys.modules[execute_parallel_task.__module__]
        ids = iter(["exec-first-a1", "exec-second-b2"])

        def _create(*a, **kw):
            eid = next(ids)
            _insert_exec(svc.store, eid, "running")
            return MagicMock(id=eid)

        db = MagicMock()
        db.get_execution_timeout.return_value = 3600
        db.get_max_parallel_tasks.return_value = 3
        db.get_agent_subscription_id.return_value = None
        db.create_task_execution.side_effect = _create
        cap = MagicMock(acquire=AsyncMock(return_value=MagicMock(state="admitted",
                                                                 queue_position=0)),
                        release=AsyncMock(), force_release=AsyncMock())
        monkeypatch.setattr(router_mod, "get_agent_container",
                            lambda name: MagicMock(status="running"))
        monkeypatch.setattr(router_mod, "db", db)
        monkeypatch.setattr(ce, "db", db)
        monkeypatch.setattr(ce, "idempotency_service", svc.isvc)
        monkeypatch.setattr(svc.das, "platform_audit_service", MagicMock(log=AsyncMock()))
        monkeypatch.setattr(ce, "get_capacity_manager", lambda: cap)
        monkeypatch.setattr(ce, "dispatch_breaker_active", lambda name: False)
        monkeypatch.setattr(ce, "activity_service", MagicMock(
            track_activity=AsyncMock(return_value="act1"), complete_activity=AsyncMock()))
        monkeypatch.setattr(chat_persistence_service, "persist_chat_session",
                            AsyncMock(return_value="cs1"))
        monkeypatch.setattr(ce, "run_async_task", AsyncMock())

        def send():
            return asyncio.run(execute_parallel_task(
                request=ParallelTaskRequest(message="do it", async_mode=True), name=AGENT,
                current_user=MagicMock(id=1, email="u@e.com", username="u", role="user",
                                       agent_name=None),
                x_source_agent=None, x_via_mcp=None, idempotency_key="k-ac2",
                x_event_trigger=None, x_internal_secret=None,
            ))

        first = send()
        assert first["execution_id"] == "exec-first-a1" and first["async_mode"] is True
        _set_exec(svc.store, "exec-first-a1", status="failed", error="boom", completed_at=T0)
        second = send()
        assert not isinstance(second, JSONResponse)          # no X-Idempotent-Replay
        assert second["execution_id"] == "exec-second-b2"
        third = send()                                       # exec-second-b2 is running
        assert isinstance(third, JSONResponse)
        assert third.headers.get("X-Idempotent-Replay") == "true"
        import json
        assert json.loads(third.body)["execution_id"] == "exec-second-b2"


class TestTaskGateSeam:
    """The `/task` twin of `TestChatSeam::test_gate_record_answers_before_the_receipt`:
    `dispatch_parallel_task` runs `skill_gate_service.enforce` before
    `begin_task_idempotency`, so a gated request gets the gate's answer and never
    reaches the receipt reclaim. Driven through the real
    `routers.chat.execute_parallel_task`, the idempotency layer real."""

    @pytest.mark.parametrize("which", ["decided", "pending"])
    def test_gate_record_answers_before_the_receipt(self, svc, monkeypatch, which):
        import asyncio
        import sys
        from unittest.mock import AsyncMock, MagicMock
        from routers.chat import execute_parallel_task
        from models import ParallelTaskRequest
        from services.skill_gate_errors import SkillApprovalRequired, SkillGateRefused
        import services.chat_execution_service as ce

        exc = (SkillGateRefused(409, "request_denied", "denied") if which == "decided"
               else SkillApprovalRequired(request_id="req-task-9c4", agent_name=AGENT,
                                          skills=["deploy"], approver_role="owner",
                                          expires_at=None))
        _insert_exec(svc.store, STALE, "failed", error="boom", completed_at=T0)
        _store_receipt(svc, "k-task-gated", status="accepted")

        router_mod = sys.modules[execute_parallel_task.__module__]
        db = MagicMock()
        db.get_execution_timeout.return_value = 3600
        db.get_max_parallel_tasks.return_value = 3
        monkeypatch.setattr(router_mod, "get_agent_container",
                            lambda name: MagicMock(status="running"))
        monkeypatch.setattr(router_mod, "db", db)
        monkeypatch.setattr(ce, "db", db)
        monkeypatch.setattr(ce, "idempotency_service", svc.isvc)
        monkeypatch.setattr(svc.das, "platform_audit_service", MagicMock(log=AsyncMock()))
        enforce = AsyncMock(side_effect=exc)
        monkeypatch.setattr(ce.skill_gate_service, "enforce", enforce)

        with pytest.raises(type(exc)):
            asyncio.run(execute_parallel_task(
                request=ParallelTaskRequest(message="deploy it", async_mode=True), name=AGENT,
                current_user=MagicMock(id=1, email="u@e.com", username="u", role="user",
                                       agent_name=None),
                x_source_agent=None, x_via_mcp=None, idempotency_key="k-task-gated",
                x_event_trigger=None, x_internal_secret=None,
            ))
        assert enforce.await_count == 1
        assert _row(svc, "k-task-gated") == ("completed", STALE)
        assert svc.db.get_execution_gate_state.calls == 0
        db.create_task_execution.assert_not_called()


# ---------------------------------------------------------------------------
# Structural guards (supplementary pins over the behaviour above)
# ---------------------------------------------------------------------------

def _fn(tree, name):
    import ast
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def test_both_seams_reclaim_right_after_begin():
    import ast
    tree = ast.parse((_backend_root() / "services" / "dispatch_admission_service.py").read_text())
    for seam in ("admit_chat_request", "begin_task_idempotency"):
        body = _fn(tree, seam).body
        idx = next(i for i, s in enumerate(body)
                   if isinstance(s, ast.Assign) and isinstance(s.value, ast.Call)
                   and ast.unparse(s.value.func) == "idempotency_service.begin")
        nxt = body[idx + 1]
        assert isinstance(nxt, ast.Assign) and ast.unparse(nxt) == \
            "idem = _reclaim_ended_receipt(idem, name=name)", seam


_CLAIM_WRITERS = {"complete", "fail", "upgrade_snapshot", "attach_execution"}


def _claim_writes(node):
    import ast
    return [c for c in ast.walk(node) if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute) and c.func.attr in _CLAIM_WRITERS
            and isinstance(c.func.value, ast.Name) and c.func.value.id == "idempotency_service"]


def test_nothing_writes_a_claim_after_its_receipt():
    """The compare-and-delete is safe only because no late writer touches a
    claim once its receipt is stored (a late `complete` would resurrect or
    overwrite it)."""
    import ast
    root = _backend_root() / "services"
    ce = ast.parse((root / "chat_execution_service.py").read_text())
    assert _claim_writes(_fn(ce, "run_async_task")) == []
    for mod in ("backlog_service.py", "pull_coordination_service.py"):
        assert _claim_writes(ast.parse((root / mod).read_text())) == [], mod
    receipts = 0
    for node in ast.walk(ce):
        for field in ("body", "orelse", "finalbody"):
            stmts = getattr(node, field, None)
            if not isinstance(stmts, list):
                continue
            for i, s in enumerate(stmts):
                if (isinstance(s, ast.Expr) and _claim_writes(s)
                        and ast.unparse(s.value).endswith(", receipt)")):
                    receipts += 1
                    rest = stmts[i + 1:]
                    assert isinstance(rest[-1], ast.Raise), ast.unparse(s)
                    assert all(isinstance(r, (ast.Assign, ast.Raise)) and not _claim_writes(r)
                               for r in rest), ast.unparse(s)
    assert receipts == 2, "the sync /task and pulled /chat receipt writers moved"
