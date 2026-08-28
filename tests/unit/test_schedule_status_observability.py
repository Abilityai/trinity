"""
Schedule status observability log (Issue #378)

Regression test for the narrowly-scoped WARNING log in
`db.schedules.ScheduleOperations.update_execution_status`: when a row whose
error matches the Phase-3 phantom-stale pattern is overwritten by SUCCESS,
we emit a log line tagged "residual race condition (#378)" so we can
observe residual races in production without changing update semantics.

Scoped to the stale-slot error pattern so other legitimate FAILED→SUCCESS
transitions (startup recovery, Phase 0 auto-terminate, Phase 1 stale
cleanup) do NOT misfire the log.

Covered scenarios:
1. FAILED row with stale-slot pattern → SUCCESS → log emitted
2. FAILED row with a DIFFERENT error → SUCCESS → log NOT emitted
3. RUNNING row → SUCCESS → log NOT emitted (happy path, no prior failure)
4. FAILED row with stale-slot pattern → FAILED (same status) → log NOT emitted
"""

from __future__ import annotations

import ast
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: make src/backend importable. Copy of the bootstrap from
# tests/unit/test_backlog.py so the same path-shadow issues are handled.
# ---------------------------------------------------------------------------

_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
# #2080: the shadow-eviction loop that used to sit here is GONE. It popped
# `utils` (and the test-helper submodules) from sys.modules to defeat
# `tests/utils` shadowing `src/backend/utils`. That package is now
# `tests/testkit`, so `utils` IS the backend package — and popping it
# evicted the canonical module mid-session, leaving anything that had
# already imported it holding a stale reference (observed as
# `ImportError: module services.subscription_auto_switch not in sys.modules`
# from an importlib.reload several hundred tests later).
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun, scalar as _hscalar  # noqa: E402


PHANTOM_ERROR_PATTERN = "Stale execution — slot TTL expired"
RESIDUAL_LOG_MARKER = "residual race condition (#378)"


@pytest.fixture
def tmp_db(db_backend):
    """Active backend with a fresh full schema (db_harness, #300). Pops any
    sibling-stubbed modules so this file's imports re-resolve fresh. Returns
    the backend marker (the leading positional arg the helpers accept)."""
    # #1481: db.schedules is now a package — evict its children too, else a
    # stale db.schedules.<slice> survives a pop("db.schedules") and shadows the
    # fresh import (prefix match, so a future sub-split needs no further edit).
    for mod in [
        "db.connection",
        *[m for m in sys.modules if m == "db.schedules" or m.startswith("db.schedules.")],
    ]:
        sys.modules.pop(mod, None)
    return db_backend


@pytest.fixture
def schedule_ops(tmp_db):
    """Fresh ScheduleOperations bound to the active backend."""
    from db.schedules import ScheduleOperations

    return ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())


def _insert(_db, *, execution_id: str, status: str, error: str | None):
    """Seed a schedule_executions row with a given status + error."""
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, message, error, triggered_by) "
        "VALUES (:id, 'sched-1', 'agent-a', :st, :sa, 'test message', :err, 'scheduler')",
        id=execution_id, st=status,
        sa=datetime.now(timezone.utc).isoformat(), err=error,
    )


def _get_status(_db, execution_id: str) -> str:
    return _hscalar(
        "SELECT status FROM schedule_executions WHERE id = :id", id=execution_id
    ) or ""


class TestResidualRaceObservabilityLog:
    """Issue #378: warn when SUCCESS overwrites a Phase-3 phantom-stale FAILED."""

    pytestmark = pytest.mark.unit

    @pytest.mark.skip(
        reason="The #378 residual-race observability WARNING was removed "
        "deliberately by PR #524 (RELIABILITY-005) when SUCCESS writes were "
        "made unconditional via CAS guards. The negative-assertion siblings "
        "in this class still hold value (they assert no misfire on legitimate "
        "FAILED→SUCCESS transitions); only this affirmative-emission test is "
        "obsolete. Remove with the next observability cleanup pass."
    )
    def test_logs_when_success_overwrites_phantom_stale_failed(
        self, tmp_db, schedule_ops, caplog
    ):
        """Row FAILED with stale-slot pattern, then SUCCESS → WARNING log."""
        from models import TaskExecutionStatus

        _insert(
            tmp_db,
            execution_id="exec-378",
            status=TaskExecutionStatus.FAILED,
            error=f"{PHANTOM_ERROR_PATTERN} for agent 'agent-a', cleaned by cleanup service",
        )

        with caplog.at_level(logging.WARNING, logger="db.schedules"):
            updated = schedule_ops.update_execution_status(
                execution_id="exec-378",
                status=TaskExecutionStatus.SUCCESS,
                response="agent returned result",
            )

        assert updated is True
        assert _get_status(tmp_db, "exec-378") == TaskExecutionStatus.SUCCESS

        matching = [r for r in caplog.records if RESIDUAL_LOG_MARKER in r.getMessage()]
        assert len(matching) == 1, (
            f"Expected exactly one #378 residual-race log, got "
            f"{len(matching)}. Messages: {[r.getMessage() for r in caplog.records]}"
        )
        assert "exec-378" in matching[0].getMessage()

    def test_does_not_log_when_failed_error_is_from_other_cleanup_path(
        self, tmp_db, schedule_ops, caplog
    ):
        """Codex Point 6: startup recovery / Phase 0 auto-terminate / Phase 1
        stale cleanup also write FAILED via unguarded update_execution_status.
        Those FAILED→SUCCESS transitions must NOT trigger the #378 log."""
        from models import TaskExecutionStatus

        _insert(
            tmp_db,
            execution_id="exec-other",
            status=TaskExecutionStatus.FAILED,
            error="Execution auto-terminated after 16 minutes by watchdog "
            "(exceeded timeout of 900s)",
        )

        with caplog.at_level(logging.WARNING, logger="db.schedules"):
            updated = schedule_ops.update_execution_status(
                execution_id="exec-other",
                status=TaskExecutionStatus.SUCCESS,
                response="late agent response",
            )

        assert updated is True
        assert _get_status(tmp_db, "exec-other") == TaskExecutionStatus.SUCCESS

        matching = [r for r in caplog.records if RESIDUAL_LOG_MARKER in r.getMessage()]
        assert matching == [], (
            "#378 log misfired on a non-stale-slot FAILED→SUCCESS transition. "
            f"Messages: {[r.getMessage() for r in caplog.records]}"
        )

    def test_does_not_log_on_running_to_success_happy_path(
        self, tmp_db, schedule_ops, caplog
    ):
        """The normal happy path (RUNNING → SUCCESS, no prior failure) must
        not trigger the log."""
        from models import TaskExecutionStatus

        _insert(
            tmp_db,
            execution_id="exec-happy",
            status=TaskExecutionStatus.RUNNING,
            error=None,
        )

        with caplog.at_level(logging.WARNING, logger="db.schedules"):
            updated = schedule_ops.update_execution_status(
                execution_id="exec-happy",
                status=TaskExecutionStatus.SUCCESS,
                response="ok",
            )

        assert updated is True
        assert _get_status(tmp_db, "exec-happy") == TaskExecutionStatus.SUCCESS
        matching = [r for r in caplog.records if RESIDUAL_LOG_MARKER in r.getMessage()]
        assert matching == []

    def test_does_not_log_on_same_status_write(self, tmp_db, schedule_ops, caplog):
        """FAILED → FAILED with stale-slot pattern → no log (not an overwrite
        of FAILED by SUCCESS, just a re-write)."""
        from models import TaskExecutionStatus

        _insert(
            tmp_db,
            execution_id="exec-same",
            status=TaskExecutionStatus.FAILED,
            error=f"{PHANTOM_ERROR_PATTERN} for agent 'agent-a', cleaned by cleanup service",
        )

        with caplog.at_level(logging.WARNING, logger="db.schedules"):
            schedule_ops.update_execution_status(
                execution_id="exec-same",
                status=TaskExecutionStatus.FAILED,
                error="re-fail",
            )

        matching = [r for r in caplog.records if RESIDUAL_LOG_MARKER in r.getMessage()]
        assert matching == []


# ===========================================================================
# Issue #1082: status-as-projection — every status write is CAS-guarded
# ===========================================================================
#
# The single fact "is execution X running?" must never be resurrected or
# clobbered by a stale writer. `schedule_executions.status` is a CAS-guarded
# *projection* of the execution's terminal event — every `update(...)` that
# writes `status` must carry a status precondition in its WHERE (or be the
# atomic claim whose precondition lives in its sub-query) so it is a no-op
# against a row that has already moved on.
#
# Two layers below:
#   1. A STATIC AST guard (`TestStatusWriteProjectionGuard`) that fails CI if a
#      future blind write adds an unguarded status writer — it needs no one to
#      remember to hand-write a behavioural test.
#   2. BEHAVIOURAL no-op proofs (`TestStatusWriteNoOpOnTerminalRow`) that drive
#      a real row to a terminal state and assert each writer leaves it untouched
#      — these survive refactors of the underlying SQL.
#
# Static-guard BLIND SPOTS (it is a tripwire, not a proof — do not read a green
# guard as "every status write in the system is CAS-protected"):
#   - File-scoped to db/schedules.py. It does NOT parse the standalone scheduler
#     process (src/scheduler/database.py), which writes the SAME trinity.db with
#     un-CAS-guarded raw SQL (`update_execution_status`, `schedule_retry`) — a
#     tracked #1082 follow-up (see feature-flows/status-as-projection.md
#     "Known gap"). Nor does it see metadata.py's rename-cascade update (harmless
#     — writes agent_name, not status).
#   - Recognizes only the `update(schedule_executions)` call shape. Raw
#     `text("UPDATE schedule_executions ...")`, the executemany form
#     `conn.execute(update(...), rows)`, and an aliased table (`t =
#     schedule_executions; update(t)`) all evade detection.
#   - `_has_status_precondition` accepts ANY `schedule_executions.c.status`
#     comparison in the function — a check-then-blind-update (read status, then
#     UPDATE with no status in the WHERE) would pass. `_writes_status` also only
#     sees `.values(status=...)` / a Dict literal with a constant "status" key,
#     so an imperatively-built `vals["status"]=...; .values(**vals)` evades it.
# The inventory tripwire (test_update_site_inventory_is_complete) still forces a
# human to classify any NEW update(schedule_executions) site, which is the
# backstop for these shape gaps within this file.

_SCHEDULES_PKG = _BACKEND / "db" / "schedules"

# Every db/schedules/ package method that issues an `update(schedule_executions)`.
# A new update site that is not in this inventory fails
# `test_update_site_inventory_is_complete`, forcing the author to classify it
# and (if it writes `status`) add a behavioural no-op proof below. Keep this in
# sync deliberately — that friction is the point (#1082).
_EXPECTED_UPDATE_SITES = {
    # --- status writers: MUST carry a status precondition (CAS projection) ---
    "update_execution_to_queued",          # #1082: AND status == RUNNING
    "release_claim_to_queued",
    "requeue_expired_lease",               # #1081 Phase 3: AND status == RUNNING + lease-past
    "park_expired_lease",                  # #1081 Phase 3: AND status == RUNNING + lease-past
    "cancel_queued_execution",
    "cancel_queued_for_agent",
    "fail_queued_for_agent",
    "fail_all_nonterminal_for_agent",      # trinity-enterprise#69: ghost discard; AND status IN (queued, running, pending_retry) — terminal rows untouched (no-op proof: test_69_ephemeral_agents.py::test_fail_all_nonterminal_for_agent)
    "expire_stale_queued",
    "update_execution_status",             # RELIABILITY-005 CAS (#524)
    "mark_stale_executions_failed",
    "mark_no_session_executions_failed",
    "fail_stale_slot_execution",
    "finalize_orphaned_skipped_executions",
    "mark_execution_failed_by_watchdog",
    # --- atomic claim: precondition is the WHERE sub-query (status == queued) -
    "claim_next_queued",
    # --- non-status updates: write other columns, `status` untouched ----------
    "mark_execution_dispatched",           # sets claude_session_id
    "update_business_status",              # sets business_status
    "prune_execution_logs",                # nulls execution_log AND tool_calls (#1741 — a transcript copy must not outlive the transcript)
    "resummarize_legacy_tool_calls",       # #1741: rewrites tool_calls to the summary shape; `status` untouched (no status in the WHERE either)
    "scrub_terminal_backlog_metadata",     # #1449: nulls backlog_metadata (reads status in a WHERE filter only)
    "restamp_execution_dispatch",          # #2433: re-anchors started_at/queued_at at dispatch after a backend-queue park; CAS on status == RUNNING + NULL lease, `status` untouched
}


def _is_update_schedule_executions(node: ast.AST) -> bool:
    """True for an `update(schedule_executions)` call node."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "update"
        and bool(node.args)
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "schedule_executions"
    )


def _is_status_column(node: ast.AST) -> bool:
    """True for the attribute chain `schedule_executions.c.status`."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "status"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "c"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "schedule_executions"
    )


def _writes_status(fn: ast.FunctionDef) -> bool:
    """True if the function sets `status` on an UPDATE.

    Covers both `.values(status=...)` (keyword form) and `.values(**values)`
    where `values` is a dict literal carrying a constant ``"status"`` key
    (the form `update_execution_status` uses).
    """
    for n in ast.walk(fn):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "values"
            and any(kw.arg == "status" for kw in n.keywords)
        ):
            return True
        if isinstance(n, ast.Dict) and any(
            isinstance(k, ast.Constant) and k.value == "status" for k in n.keys
        ):
            return True
    return False


def _has_status_precondition(fn: ast.FunctionDef) -> bool:
    """True if the function compares `schedule_executions.c.status` anywhere.

    A status precondition shows up as either a `Compare` (``status == X``,
    ``status != X``) or a method call on the column (``status.notin_(...)``,
    ``status.in_(...)``, ``status.is_(...)``). A `.values(status=...)` write
    uses ``status`` as a *keyword argument name*, never as the column
    attribute, so a value-write alone is never mistaken for a predicate.
    """
    for n in ast.walk(fn):
        if isinstance(n, ast.Compare):
            if _is_status_column(n.left) or any(
                _is_status_column(c) for c in n.comparators
            ):
                return True
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and _is_status_column(n.func.value)
        ):
            return True
    return False


def _update_site_functions() -> dict[str, ast.FunctionDef]:
    """Map function name -> FunctionDef for every update(schedule_executions)
    across the whole `db/schedules/` package (#1481 split — the writers no
    longer live in one file). `rglob` (not `glob`) so a future nested
    sub-split is still discovered rather than silently skipped."""
    sites: dict[str, ast.FunctionDef] = {}
    for path in sorted(_SCHEDULES_PKG.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and any(
                _is_update_schedule_executions(n) for n in ast.walk(fn)
            ):
                sites[fn.name] = fn
    assert sites, (
        f"no update(schedule_executions) sites found under {_SCHEDULES_PKG} — "
        "package glob is broken; the CAS-precondition guard would pass vacuously"
    )
    return sites


class TestStatusWriteProjectionGuard:
    """#1082: static guarantee that no status write escapes the CAS guard."""

    pytestmark = pytest.mark.unit

    def test_update_site_inventory_is_complete(self):
        """A new update(schedule_executions) site must be classified here.

        This is the tripwire: if someone adds an UPDATE to schedule_executions
        in a new function, this fails until they add it to
        `_EXPECTED_UPDATE_SITES` — which forces them past the projection
        contract and (for status writers) a behavioural no-op proof.
        """
        found = set(_update_site_functions())
        missing = found - _EXPECTED_UPDATE_SITES
        stale = _EXPECTED_UPDATE_SITES - found
        assert not missing, (
            "New update(schedule_executions) site(s) not classified in "
            f"_EXPECTED_UPDATE_SITES: {sorted(missing)}. Classify each (status "
            "writer? atomic claim? non-status?) and, if it writes `status`, add "
            "a behavioural no-op proof in TestStatusWriteNoOpOnTerminalRow."
        )
        assert not stale, (
            "_EXPECTED_UPDATE_SITES lists functions that no longer issue an "
            f"update(schedule_executions): {sorted(stale)}. Remove them."
        )

    def test_every_status_write_carries_a_precondition(self):
        """The core projection invariant, asserted structurally.

        Every function that writes `status` on an UPDATE must also reference
        `schedule_executions.c.status` in a predicate — i.e. it can only move a
        row that is in an expected prior state, never a terminal one it has no
        business touching. The pre-#1082 `update_execution_to_queued` (no
        WHERE-status clause) would have failed this.
        """
        offenders = [
            name
            for name, fn in _update_site_functions().items()
            if _writes_status(fn) and not _has_status_precondition(fn)
        ]
        assert offenders == [], (
            "Status writer(s) with no status precondition (would clobber/"
            f"resurrect a terminal row, breaking the #1082 projection): "
            f"{sorted(offenders)}"
        )

    def test_guard_detects_an_unguarded_status_write(self):
        """Meta-test: prove the guard actually fires on a bad writer.

        A guard that can't fail is worthless. Synthesize the pre-#1082 shape —
        `update(schedule_executions).where(id==...).values(status=...)` with no
        status predicate — and confirm the detectors flag it.
        """
        bad = ast.parse(
            "def bad(self, eid):\n"
            "    conn.execute(\n"
            "        update(schedule_executions)\n"
            "        .where(schedule_executions.c.id == eid)\n"
            "        .values(status=TaskExecutionStatus.QUEUED)\n"
            "    )\n"
        ).body[0]
        assert _writes_status(bad) is True
        assert _has_status_precondition(bad) is False

        good = ast.parse(
            "def good(self, eid):\n"
            "    conn.execute(\n"
            "        update(schedule_executions)\n"
            "        .where(and_(\n"
            "            schedule_executions.c.id == eid,\n"
            "            schedule_executions.c.status == TaskExecutionStatus.RUNNING,\n"
            "        ))\n"
            "        .values(status=TaskExecutionStatus.QUEUED)\n"
            "    )\n"
        ).body[0]
        assert _writes_status(good) is True
        assert _has_status_precondition(good) is True


class TestStatusWriteNoOpOnTerminalRow:
    """#1082: behavioural proof that each status writer is a no-op against a
    row that has already reached a terminal state. These exercise the real SQL,
    so they hold even if the WHERE clause is refactored."""

    pytestmark = pytest.mark.unit

    _TERMINAL = ("success", "failed", "cancelled", "skipped")

    @pytest.mark.parametrize("terminal", _TERMINAL)
    def test_requeue_against_terminal_row_is_noop(
        self, tmp_db, schedule_ops, terminal
    ):
        """Step 1 gap (#1082): the overflow re-queue must NOT resurrect a
        terminal row into `queued` (the E-02 phantom-reversal class)."""
        eid = f"req-{terminal}"
        _insert(tmp_db, execution_id=eid, status=terminal, error=None)
        now = datetime.now(timezone.utc).isoformat()

        ok = schedule_ops.update_execution_to_queued(eid, '{"k":1}', now)

        assert ok is False
        assert _get_status(tmp_db, eid) == terminal
        assert schedule_ops.get_queued_count("agent-a") == 0

    def test_requeue_against_running_row_still_works(self, tmp_db, schedule_ops):
        """Happy path unchanged: a RUNNING row still spills into the backlog."""
        eid = "req-running"
        _insert(tmp_db, execution_id=eid, status="running", error=None)
        now = datetime.now(timezone.utc).isoformat()

        ok = schedule_ops.update_execution_to_queued(eid, '{"k":1}', now)

        assert ok is True
        assert _get_status(tmp_db, eid) == "queued"
        assert schedule_ops.get_queued_count("agent-a") == 1

    def test_success_does_not_overwrite_a_cancelled_row(self, tmp_db, schedule_ops):
        """#671 / RELIABILITY-005: a late agent 'success' must not flip a
        user-cancelled row — the cancel is authoritative."""
        from models import TaskExecutionStatus

        _insert(
            tmp_db, execution_id="cas-cancel",
            status=TaskExecutionStatus.CANCELLED, error="user cancelled",
        )

        ok = schedule_ops.update_execution_status(
            execution_id="cas-cancel",
            status=TaskExecutionStatus.SUCCESS,
            response="late 'I am done'",
        )

        assert ok is False
        assert _get_status(tmp_db, "cas-cancel") == TaskExecutionStatus.CANCELLED

    @pytest.mark.parametrize("terminal", ["success", "failed", "cancelled", "skipped"])
    def test_failed_does_not_overwrite_a_terminal_row(
        self, tmp_db, schedule_ops, terminal
    ):
        """RELIABILITY-005: a non-success terminal write must not clobber an
        already-terminal row (a cleanup path can't bury a real completion)."""
        from models import TaskExecutionStatus

        _insert(tmp_db, execution_id=f"cas-{terminal}", status=terminal, error=None)

        ok = schedule_ops.update_execution_status(
            execution_id=f"cas-{terminal}",
            status=TaskExecutionStatus.FAILED,
            error="cleanup misfire",
        )

        assert ok is False
        assert _get_status(tmp_db, f"cas-{terminal}") == terminal

    def test_release_claim_against_terminal_row_is_noop(self, tmp_db, schedule_ops):
        """release_claim_to_queued only moves a RUNNING row back to the queue."""
        _insert(tmp_db, execution_id="rel-term", status="success", error=None)
        assert schedule_ops.release_claim_to_queued("rel-term") is False
        assert _get_status(tmp_db, "rel-term") == "success"

    def test_cancel_queued_against_terminal_row_is_noop(self, tmp_db, schedule_ops):
        """cancel_queued_execution only cancels a still-QUEUED row."""
        _insert(tmp_db, execution_id="can-term", status="success", error=None)
        assert schedule_ops.cancel_queued_execution("can-term") is False
        assert _get_status(tmp_db, "can-term") == "success"

    def test_watchdog_fail_against_terminal_row_is_noop(self, tmp_db, schedule_ops):
        """mark_execution_failed_by_watchdog only fails a RUNNING row."""
        _insert(tmp_db, execution_id="wd-term", status="success", error=None)
        assert (
            schedule_ops.mark_execution_failed_by_watchdog("wd-term", "timeout")
            is False
        )
        assert _get_status(tmp_db, "wd-term") == "success"
