"""Unit tests for #1804 — a CAS-won terminal write owns closing its paired
``agent_activities`` dispatch row.

Before this, only the dispatching coroutine closed the activity, and only when
it won the CAS. Every recovery writer (watchdog, startup recovery, the two bulk
sweeps, both backend-shutdown ``CancelledError`` handlers, the lease reaper, the
pull sink) wrote the execution terminal and walked away — the activity stayed
``started`` until a generic 120-minute backstop closed it with a fabricated
``duration_ms``.

Layout:
  * ``db_layer``   — the lattice CAS + tri-state outcome + widened lookup +
                     the set-wise bulk close, against a real schema (db_harness).
  * ``service``    — ``activity_service.close_execution_activity`` / the sync
                     spawn wrapper (mocked db).
  * ``cas_loss`` / ``shutdown`` / ``cleanup`` / ``pull`` / ``requeue`` — the
    wired terminal writers.

Mandatory regressions (plan §4): R1 FAILED→COMPLETED upgrade *through the
lookup*; R2 terminate-then-CAS-loss double close; R3 an already-closed close
never clobbers; R4 terminate keeps its #1332 behaviour; R5 the shutdown
handlers close.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend, run as _hrun  # noqa: E402,F401  (pytest fixture)


def _await(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ===========================================================================
# db layer — real schema
# ===========================================================================
@pytest.fixture
def tmp_db(db_backend, monkeypatch):
    """Active backend with a fresh full production schema (db_harness, #300)."""
    for mod in ("db.connection", "db.schedules", "db.activities", "database"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    return db_backend


@pytest.fixture
def activity_ops(tmp_db):
    from db.activities import ActivityOperations

    return ActivityOperations()


def _insert_activity(
    *,
    act_id: str,
    exec_id: str,
    activity_type: str = "chat_start",
    activity_state: str = "started",
    started_at: str | None = None,
    created_at: str | None = None,
    completed_at: str | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
):
    started_at = started_at or _ago_iso(60)
    created_at = created_at or started_at
    _hrun(
        "INSERT INTO agent_activities "
        "(id, agent_name, activity_type, activity_state, started_at, completed_at, "
        " duration_ms, triggered_by, related_execution_id, error, created_at) "
        "VALUES (:id, 'test-agent', :atype, :astate, :sa, :ca_at, :dur, 'schedule', "
        " :eid, :err, :ca)",
        id=act_id, atype=activity_type, astate=activity_state, sa=started_at,
        ca_at=completed_at, dur=duration_ms, eid=exec_id, err=error, ca=created_at,
    )


def _fetch_activity(act_id: str) -> dict:
    from sqlalchemy import text
    from db.engine import get_engine

    with get_engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT activity_state, completed_at, duration_ms, error "
                "FROM agent_activities WHERE id = :id"
            ),
            {"id": act_id},
        ).mappings().first()
    return dict(row) if row else {}


@pytest.mark.unit
class TestCompleteActivityCas:
    """``db.complete_activity`` is a lattice CAS returning a tri-state outcome."""

    def test_db_layer_started_row_is_updated(self, tmp_db, activity_ops):
        from db.activities import ActivityCloseOutcome

        _insert_activity(act_id="act-1", exec_id="exec-1")
        outcome = activity_ops.complete_activity("act-1", "completed")
        assert outcome is ActivityCloseOutcome.UPDATED
        row = _fetch_activity("act-1")
        assert row["activity_state"] == "completed"
        assert row["completed_at"] is not None
        assert row["duration_ms"] is not None

    def test_db_layer_missing_row_is_not_found(self, tmp_db, activity_ops):
        """[404 semantics] routers/internal.py 404s on NOT_FOUND, and only there."""
        from db.activities import ActivityCloseOutcome

        assert activity_ops.complete_activity("nope", "completed") is ActivityCloseOutcome.NOT_FOUND

    def test_db_layer_already_closed_never_clobbers(self, tmp_db, activity_ops):
        """[R3] The double-close hazard the CAS exists to defuse: a second closer
        must not overwrite completed_at / duration_ms / error."""
        from db.activities import ActivityCloseOutcome

        _insert_activity(
            act_id="act-2", exec_id="exec-2", activity_state="completed",
            completed_at="2026-07-28T10:00:00Z", duration_ms=900_000, error=None,
        )
        before = _fetch_activity("act-2")
        outcome = activity_ops.complete_activity("act-2", "failed", error="late failure")
        assert outcome is ActivityCloseOutcome.ALREADY_CLOSED
        assert _fetch_activity("act-2") == before

    def test_db_layer_failed_row_upgrades_to_completed(self, tmp_db, activity_ops):
        """[R1] An authoritative close MAY upgrade a provisional FAILED — the
        #1083 late-SUCCESS-after-lease-expiry path."""
        from db.activities import ActivityCloseOutcome

        _insert_activity(
            act_id="act-3", exec_id="exec-3", activity_state="failed",
            completed_at="2026-07-28T10:00:00Z", duration_ms=7_200_000,
            error="lease_expired",
        )
        outcome = activity_ops.complete_activity("act-3", "completed")
        assert outcome is ActivityCloseOutcome.UPDATED
        row = _fetch_activity("act-3")
        assert row["activity_state"] == "completed"
        assert row["duration_ms"] != 7_200_000
        assert row["error"] is None

    def test_db_layer_failed_row_refuses_second_failed(self, tmp_db, activity_ops):
        """[R1] A provisional close never overwrites a provisional close."""
        from db.activities import ActivityCloseOutcome

        _insert_activity(
            act_id="act-4", exec_id="exec-4", activity_state="failed",
            completed_at="2026-07-28T10:00:00Z", duration_ms=1_000, error="first",
        )
        assert activity_ops.complete_activity(
            "act-4", "failed", error="second"
        ) is ActivityCloseOutcome.ALREADY_CLOSED
        assert _fetch_activity("act-4")["error"] == "first"

    def test_db_layer_cancelled_row_refuses_completed(self, tmp_db, activity_ops):
        """Nothing overwrites an authoritative close — mirrors the execution CAS,
        where a SUCCESS write loses only to CANCELLED (#671/#1332)."""
        from db.activities import ActivityCloseOutcome

        _insert_activity(
            act_id="act-5", exec_id="exec-5", activity_state="cancelled",
            completed_at="2026-07-28T10:00:00Z", duration_ms=1_000,
            error="Execution terminated by user",
        )
        assert activity_ops.complete_activity(
            "act-5", "completed"
        ) is ActivityCloseOutcome.ALREADY_CLOSED
        assert _fetch_activity("act-5")["activity_state"] == "cancelled"

    def test_db_layer_details_merge_preserved_on_update(self, tmp_db, activity_ops):
        """The pre-existing detail-merge behaviour survives the CAS rewrite."""
        from sqlalchemy import text
        from db.engine import get_engine
        import json

        _insert_activity(act_id="act-6", exec_id="exec-6")
        with get_engine().begin() as conn:
            conn.execute(
                text("UPDATE agent_activities SET details = :d WHERE id = 'act-6'"),
                {"d": json.dumps({"keep": 1})},
            )
        activity_ops.complete_activity("act-6", "completed", details={"add": 2})
        with get_engine().connect() as conn:
            raw = conn.execute(
                text("SELECT details FROM agent_activities WHERE id = 'act-6'")
            ).scalar()
        assert json.loads(raw) == {"keep": 1, "add": 2}


@pytest.mark.unit
class TestOpenActivityLookup:
    """The lookup must agree with the CAS, or the widened predicate is inert."""

    def test_lookup_authoritative_finds_failed_row(self, tmp_db, activity_ops):
        """[R1] The decisive pairing: an authoritative close searches
        ``started|failed`` so it can actually reach the row it may upgrade."""
        _insert_activity(act_id="act-f", exec_id="exec-f", activity_state="failed")
        assert activity_ops.get_open_activity_id_for_execution("exec-f") is None
        assert (
            activity_ops.get_open_activity_id_for_execution("exec-f", include_failed=True)
            == "act-f"
        )

    def test_lookup_prefers_started_over_failed(self, tmp_db, activity_ops):
        """With both present the OPEN row wins regardless of created_at order."""
        _insert_activity(
            act_id="act-open", exec_id="exec-both", activity_state="started",
            created_at=_ago_iso(120),
        )
        _insert_activity(
            act_id="act-closed", exec_id="exec-both", activity_state="failed",
            created_at=_ago_iso(10),
        )
        assert (
            activity_ops.get_open_activity_id_for_execution("exec-both", include_failed=True)
            == "act-open"
        )

    def test_lookup_never_returns_completed_or_cancelled(self, tmp_db, activity_ops):
        _insert_activity(act_id="act-c", exec_id="exec-c", activity_state="completed")
        _insert_activity(act_id="act-x", exec_id="exec-x", activity_state="cancelled")
        for eid in ("exec-c", "exec-x"):
            assert activity_ops.get_open_activity_id_for_execution(eid, include_failed=True) is None

    def test_lookup_excludes_shared_eid_tool_call_row(self, tmp_db, activity_ops):
        """Codex #8 (#1083) preserved under the widened lookup."""
        _insert_activity(
            act_id="act-dispatch", exec_id="exec-shared",
            activity_type="chat_start", activity_state="failed",
            created_at=_ago_iso(30),
        )
        _insert_activity(
            act_id="act-tool", exec_id="exec-shared",
            activity_type="tool_call", activity_state="started",
            created_at=_ago_iso(1),
        )
        assert (
            activity_ops.get_open_activity_id_for_execution("exec-shared", include_failed=True)
            == "act-dispatch"
        )


@pytest.mark.unit
class TestBulkCloseOpenActivities:
    """The bulk sweeps close set-wise in one transaction, no per-row WS."""

    def test_db_layer_bulk_closes_every_open_row_for_the_id_set(self, tmp_db, activity_ops):
        """Set-wise (Codex 6): a re-queued execution can own more than one open
        dispatch activity — an ``eid → one activity_id`` map would drop the rest."""
        _insert_activity(act_id="a1", exec_id="e1")
        _insert_activity(act_id="a2", exec_id="e1", activity_type="schedule_start")
        _insert_activity(act_id="b1", exec_id="e2")
        _insert_activity(act_id="untouched", exec_id="e3")

        closed = activity_ops.close_open_activities_for_executions(
            ["e1", "e2"], "failed", error="marked failed by cleanup sweep"
        )
        assert closed == 3
        for act_id in ("a1", "a2", "b1"):
            row = _fetch_activity(act_id)
            assert row["activity_state"] == "failed"
            assert row["duration_ms"] is not None
            assert row["error"] == "marked failed by cleanup sweep"
        assert _fetch_activity("untouched")["activity_state"] == "started"

    def test_db_layer_bulk_skips_already_closed_rows(self, tmp_db, activity_ops):
        _insert_activity(
            act_id="done", exec_id="e9", activity_state="completed",
            completed_at="2026-07-28T10:00:00Z", duration_ms=42,
        )
        assert activity_ops.close_open_activities_for_executions(["e9"], "failed") == 0
        assert _fetch_activity("done")["duration_ms"] == 42

    def test_db_layer_bulk_empty_input_is_zero(self, tmp_db, activity_ops):
        assert activity_ops.close_open_activities_for_executions([], "failed") == 0

    def test_db_layer_bulk_chunks_past_the_host_param_cap(
        self, tmp_db, activity_ops, monkeypatch
    ):
        """The IN (...) list is chunked at ``_SQLITE_MAX_IN_VARS`` (precedent:
        db/schedules/git_config.py) — monkeypatched small to exercise it."""
        import db.activities as activities_mod

        monkeypatch.setattr(activities_mod, "_SQLITE_MAX_IN_VARS", 2)
        ids = [f"bulk-{i}" for i in range(5)]
        for i, eid in enumerate(ids):
            _insert_activity(act_id=f"bulk-act-{i}", exec_id=eid)

        assert activity_ops.close_open_activities_for_executions(ids, "failed") == 5
        for i in range(5):
            assert _fetch_activity(f"bulk-act-{i}")["activity_state"] == "failed"
