"""ent#335 — canary E-06 must ignore schedules of soft-deleted agents.

``_collect_enabled_schedules`` filtered only on the SCHEDULE's own
``deleted_at``, so schedules belonging to a soft-deleted AGENT stayed "enabled"
for the whole 180-day retention window (#834 Phase 1a deliberately preserves
child rows) with a permanently frozen ``next_run_at``. E-06 flagged every one,
every cycle, forever: on eu2 that was **6,220 of 6,605 total violations (94%)**
in ~13h, from 20 schedules belonging to 4 agents deleted 26 days earlier.

The frozen projection is not a scheduler failure — it is the scheduler
correctly refusing to register a deleted agent's jobs. The authority is
``db/schedules/crud.py::list_all_enabled_schedules``, the list the scheduler
actually arms, which applies BOTH #834 filters. This collector's docstring
already claimed to mirror that predicate and copied half of it.

Tests live under ``tests/unit/`` because CI runs ``pytest unit/`` only —
``tests/test_canary_invariants.py`` is never executed by any workflow.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import pytest


T0 = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


# One literal instant, offsets from it — never a live clock (learnings
# 2026-08-03 / #1909). E-06's grace is 1h, so "2 days ago" is unambiguously
# overdue and "1 hour ahead" unambiguously healthy at every machine speed.
STALE = _iso(T0 - timedelta(days=2))
FUTURE = _iso(T0 + timedelta(hours=1))
DELETED_AT = _iso(T0 - timedelta(days=26))


@pytest.fixture()
def e06_db(monkeypatch):
    """Temp SQLite carrying just the two tables the E-06 collector joins."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE agent_ownership (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT UNIQUE NOT NULL,
            owner_id INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00Z',
            deleted_at TEXT
        );
        CREATE TABLE agent_schedules (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT 'sched',
            cron_expression TEXT NOT NULL DEFAULT '0 9 * * *',
            message TEXT NOT NULL DEFAULT 'go',
            enabled INTEGER DEFAULT 1,
            owner_id INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00Z',
            updated_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00Z',
            next_run_at TEXT,
            deleted_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    import db.engine as engine_mod

    engine_mod.dispose_engines()
    yield path
    engine_mod.dispose_engines()
    os.unlink(path)


def _add_agent(path: str, name: str, *, deleted_at: str | None = None) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO agent_ownership (agent_name, deleted_at) VALUES (?, ?)",
        (name, deleted_at),
    )
    conn.commit()
    conn.close()


def _add_schedule(
    path: str,
    sched_id: str,
    agent: str,
    *,
    next_run_at: str,
    enabled: int = 1,
    deleted_at: str | None = None,
) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO agent_schedules (id, agent_name, next_run_at, enabled, deleted_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (sched_id, agent, next_run_at, enabled, deleted_at),
    )
    conn.commit()
    conn.close()


def _collect(path: str):
    from canary.snapshot import _collect_enabled_schedules

    return _collect_enabled_schedules()


def _check(rows):
    from canary.invariants import e06_no_overdue_next_run as e06
    from canary.snapshot import Snapshot

    return e06.check(Snapshot(snapshot_time=_iso(T0), enabled_schedules=rows))


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------


def test_soft_deleted_agents_schedules_are_excluded(e06_db):
    """THE ent#335 regression: 20 schedules × every cycle × 180 days."""
    _add_agent(e06_db, "ghost", deleted_at=DELETED_AT)
    _add_schedule(e06_db, "s1", "ghost", next_run_at=STALE)

    assert _collect(e06_db) == []
    assert _check(_collect(e06_db)) == []


def test_live_agent_with_frozen_projection_still_fires(e06_db):
    """The #1472 detection net must survive the fix.

    If this ever goes green, E-06 has been silenced rather than corrected.
    """
    _add_agent(e06_db, "live")
    _add_schedule(e06_db, "s1", "live", next_run_at=STALE)

    rows = _collect(e06_db)
    assert [r["schedule_id"] for r in rows] == ["s1"]

    violations = _check(rows)
    assert len(violations) == 1
    assert violations[0].invariant_id == "E-06"
    assert violations[0].observed_state["agent_name"] == "live"


def test_mixed_fleet_reports_only_the_live_agent(e06_db):
    """eu2's shape: healthy agents, plus deleted ones dominating the noise."""
    _add_agent(e06_db, "live")
    _add_agent(e06_db, "healthy")
    _add_agent(e06_db, "ghost", deleted_at=DELETED_AT)

    _add_schedule(e06_db, "s-live", "live", next_run_at=STALE)
    _add_schedule(e06_db, "s-healthy", "healthy", next_run_at=FUTURE)
    for i in range(8):
        _add_schedule(e06_db, f"s-ghost-{i}", "ghost", next_run_at=STALE)

    violations = _check(_collect(e06_db))
    assert [v.observed_state["schedule_id"] for v in violations] == ["s-live"]


# ---------------------------------------------------------------------------
# Boundaries of the new predicate
# ---------------------------------------------------------------------------


def test_orphan_schedule_is_left_to_l03(e06_db):
    """A schedule whose agent row is GONE is L-03's orphan, not E-06's.

    The INNER join drops it here; `ORPHAN_SCAN_TABLES` carries
    `("agent_schedules", "agent_name", None)` unfiltered, and
    `_collect_known_agents` deliberately includes soft-deleted agents, so a
    genuinely absent owner is reported there. (learnings 2026-08-03: a cleanup
    deferral is only as true as the component it names — this one is checked.)
    """
    _add_schedule(e06_db, "s-orphan", "vanished", next_run_at=STALE)

    assert _collect(e06_db) == []

    from canary.snapshot import ORPHAN_SCAN_TABLES

    entry = [t for t in ORPHAN_SCAN_TABLES if t[0] == "agent_schedules"]
    assert entry == [("agent_schedules", "agent_name", None)], (
        "E-06 defers orphan schedules to L-03; if L-03's scan of "
        "agent_schedules is narrowed or removed, they become invisible."
    )


def test_join_cannot_duplicate_schedule_rows(e06_db):
    """`agent_ownership.agent_name` is UNIQUE, so the join is at most 1:1.

    A cardinality change would silently multiply every violation.
    """
    _add_agent(e06_db, "live")
    for i in range(3):
        _add_schedule(e06_db, f"s{i}", "live", next_run_at=STALE)

    rows = _collect(e06_db)
    assert len(rows) == 3
    assert len({r["schedule_id"] for r in rows}) == 3


def test_soft_deleted_schedule_of_a_live_agent_still_excluded(e06_db):
    """The pre-existing #834 Phase 1b filter must not regress."""
    _add_agent(e06_db, "live")
    _add_schedule(e06_db, "s1", "live", next_run_at=STALE, deleted_at=DELETED_AT)

    assert _collect(e06_db) == []


def test_disabled_schedule_still_excluded(e06_db):
    _add_agent(e06_db, "live")
    _add_schedule(e06_db, "s1", "live", next_run_at=STALE, enabled=0)

    assert _collect(e06_db) == []


def test_collector_predicate_agrees_with_the_scheduler_accessor():
    """E-06's SELECT and `list_all_enabled_schedules` must stay in step.

    The canary keeps its own query on purpose (it is a low-dependency leaf, and
    independent code paths are the harness's design stance — cf. B-01). The
    cost of that independence is drift: this collector already went quiet once
    by mirroring only half the predicate. Pin the three clauses so a third
    #834-style filter landing in the accessor fails here instead of silently
    re-opening ent#335.
    """
    import inspect

    from canary import snapshot as snap_mod
    from db.schedules import crud as sched_crud

    collector = inspect.getsource(snap_mod._collect_enabled_schedules)
    accessor = inspect.getsource(sched_crud.ScheduleCrudMixin.list_all_enabled_schedules)

    for clause in (
        "agent_schedules.c.enabled == 1",
        "agent_schedules.c.deleted_at.is_(None)",
        "agent_ownership.c.deleted_at.is_(None)",
    ):
        assert clause in accessor, f"accessor no longer applies {clause}"
        assert clause in collector, f"E-06 collector no longer applies {clause}"
