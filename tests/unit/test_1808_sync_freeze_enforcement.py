"""
Regression for #1808 — `freeze_schedules_if_sync_failing` must actually freeze.

The flag (#389) was settable, persisted, reported back as enabled, and the
backend even computed the decision at
`/api/internal/agents/{name}/sync-health-status` — but nothing consumed it. The
scheduler never looked at sync state, so schedules kept firing against agents
whose git sync was broken, while
`docs/user-docs/faq/troubleshooting.md` told users the freeze worked.

These tests pin the predicate. The most important case is the fail-open one: a
freeze-on-error would silently stop every schedule in the fleet the moment this
query broke, which is strictly worse than the bug being fixed.

`src/scheduler` is a standalone package (it cannot import the backend), so the
DB object is exercised directly against a temp SQLite file rather than mocked.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[2]

# The `src.scheduler` namespace-package import below resolves only when the
# repo root is on sys.path. That is true for a repo-root `pytest` run but NOT
# in CI, which runs with rootdir `tests/` (the conftests put `src/backend` on
# the path, never the repo root) — the 9-failure `ModuleNotFoundError: src`
# regression-diff on this PR's first push. Appending (not inserting at 0) so
# the repo root can never shadow the conftest-managed `src/backend` entries.
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))

# `src/scheduler/config.py` reads these at import time (#589 makes the Redis
# credentials mandatory), so they must exist before the package is imported.
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")


def _scheduler_database_module():
    """Import the scheduler's `database` module as part of its package.

    It uses relative imports (`from .config import config`), so it cannot be
    loaded standalone by path — and a bare `import database` would resolve to
    `src/backend/database.py`, which is on the pytest path ahead of it.
    Imported through the repo root (appended to sys.path above) so the name is
    unambiguous with no sys.modules mutation (tests/lint_sys_modules.py).
    """
    import src.scheduler.database as scheduler_database

    return scheduler_database


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _seed(
    db_path: Path,
    *,
    freeze: int,
    status: str | None,
    failures: int | None,
    source_mode: int = 0,
    auto_sync: int = 1,
    ahead: int = 0,
    behind: int = 0,
    diverged_since: str | None = None,
    last_check_at: str | None = "now",
) -> None:
    """Minimal schema + one agent's git config and sync state.

    The columns are the ones the gate reads, named as in `db/schema.py`
    (trinity-enterprise#706 added the divergence inputs). `last_check_at`
    defaults to "now": every real row carries it, because the poller's upsert
    stamps it on every write.
    """
    if last_check_at == "now":
        last_check_at = _iso(datetime.now(timezone.utc) - timedelta(seconds=30))
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE agent_git_config (agent_name TEXT PRIMARY KEY, "
        "freeze_schedules_if_sync_failing INTEGER DEFAULT 0, "
        "source_mode INTEGER DEFAULT 0, auto_sync_enabled INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE agent_sync_state (agent_name TEXT PRIMARY KEY, "
        "last_sync_status TEXT, consecutive_failures INTEGER, "
        "ahead_main INTEGER DEFAULT 0, behind_main INTEGER DEFAULT 0, "
        "ahead_working INTEGER DEFAULT 0, behind_working INTEGER DEFAULT 0, "
        "diverged_since TEXT, last_check_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_git_config VALUES (?, ?, ?, ?)",
        ("a1", freeze, source_mode, auto_sync),
    )
    if status is not None:
        conn.execute(
            "INSERT INTO agent_sync_state (agent_name, last_sync_status, "
            "consecutive_failures, ahead_working, behind_working, diverged_since, "
            "last_check_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("a1", status, failures, ahead, behind, diverged_since, last_check_at),
        )
    conn.commit()
    conn.close()


def _db(db_path: Path):
    return _scheduler_database_module().SchedulerDatabase(str(db_path))


@pytest.mark.parametrize(
    ("freeze", "status", "failures", "expected"),
    [
        # The bug: opted in AND genuinely failing -> must freeze.
        (1, "failed", 3, True),
        (1, "failed", 9, True),
        # Opted in but sync is fine -> must fire.
        (1, "success", 0, False),
        # Failing but below the threshold -> must fire (a blip is not an outage).
        (1, "failed", 2, False),
        # Not opted in -> must fire no matter how broken sync is.
        (0, "failed", 99, False),
        # Opted in but no sync state row at all -> nothing to trip on.
        (1, None, None, False),
    ],
)
def test_freeze_predicate(tmp_path, freeze, status, failures, expected):
    db_path = tmp_path / "t.db"
    _seed(db_path, freeze=freeze, status=status, failures=failures)
    assert _db(db_path).should_freeze_schedules("a1") is expected


def test_unknown_agent_does_not_freeze(tmp_path):
    """An agent with no git config must never be frozen."""
    db_path = tmp_path / "t.db"
    _seed(db_path, freeze=1, status="failed", failures=5)
    assert _db(db_path).should_freeze_schedules("someone-else") is False


def test_fails_open_when_the_query_breaks(tmp_path):
    """A broken/missing table must fire the schedule, never freeze the fleet.

    Freezing on error would turn any schema drift into a silent, fleet-wide
    halt of all scheduled work — worse than the bug this fixes.
    """
    db_path = tmp_path / "t.db"
    sqlite3.connect(db_path).close()  # valid DB, no tables at all
    assert _db(db_path).should_freeze_schedules("a1") is False


def test_threshold_matches_the_backend(tmp_path):
    """The scheduler's threshold must stay in step with the backend's.

    trinity-enterprise#706: both read it from ONE policy module (vendored
    byte-identically; parity in test_ent706_sync_policy_parity.py). The
    backend's internal sync-health endpoint that once restated it was removed
    in #3434 (no caller); the backend verdict is `sync_health_view`, which
    delegates to the canonical policy module checked here.
    """
    import re

    threshold = _scheduler_database_module().SYNC_FAILURE_FREEZE_THRESHOLD
    assert threshold == 3
    policy = (_REPO / "src" / "backend" / "services" / "sync_freeze_policy.py").read_text()
    m = re.search(r"^SYNC_FAILURE_FREEZE_THRESHOLD = (\d+)", policy, re.M)
    assert m and int(m.group(1)) == threshold
    view = (_REPO / "src" / "backend" / "services" / "sync_health_view.py").read_text()
    assert "sync_freeze_policy" in view


def test_mapping_only_rows_still_freeze(tmp_path, monkeypatch):
    """PostgreSQL rows (#300) are RealDictCursor mappings with NO positional
    access — `row[0]` raises KeyError there, which the fail-open except would
    swallow, leaving the freeze silently inert on every PG deploy. Pin that the
    predicate reads columns by name (`row["col"]`, the module convention)."""
    from contextlib import contextmanager

    db_path = tmp_path / "t.db"
    _seed(db_path, freeze=1, status="failed", failures=5)
    db = _db(db_path)

    class _DictRowCursor:
        """sqlite3 cursor whose rows are plain dicts (mapping-only access)."""

        def __init__(self, cur):
            self._cur = cur

        def execute(self, sql, params=()):
            self._cur.execute(sql, params)
            return self

        def fetchone(self):
            row = self._cur.fetchone()
            return None if row is None else dict(row)

    @contextmanager
    def dict_row_connection():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield type(
                "Conn", (), {"cursor": lambda self: _DictRowCursor(conn.cursor())}
            )()
        finally:
            conn.close()

    monkeypatch.setattr(db, "get_connection", dict_row_connection)
    assert db.should_freeze_schedules("a1") is True


# ---------------------------------------------------------------------------
# trinity-enterprise#706: the freeze also keys on divergence age (work agents)
# ---------------------------------------------------------------------------

def _diverged_seed(db_path, *, age: timedelta, **kw):
    kw.setdefault("freeze", 1)
    _seed(
        db_path,
        status="success",
        failures=0,
        ahead=kw.pop("ahead", 7),
        diverged_since=_iso(datetime.now(timezone.utc) - age) if age is not None else None,
        **kw,
    )


def test_divergence_past_24h_freezes_a_work_agent(tmp_path):
    db_path = tmp_path / "t.db"
    _diverged_seed(db_path, age=timedelta(hours=24, seconds=1))
    db = _db(db_path)
    reason = db.sync_freeze_reason("a1")
    assert reason is not None and reason.startswith("diverged 0 behind / 7 ahead for 24h")
    assert db.should_freeze_schedules("a1") is True


def test_divergence_under_24h_fires(tmp_path):
    db_path = tmp_path / "t.db"
    _diverged_seed(db_path, age=timedelta(hours=24) - timedelta(seconds=1))
    assert _db(db_path).sync_freeze_reason("a1") is None


def test_control_arm_no_divergence_clock_fires(tmp_path):
    """The SAME row with diverged_since NULL fires — the freeze above comes
    from divergence, not from anything ambient in the seed."""
    db_path = tmp_path / "t.db"
    _diverged_seed(db_path, age=None)
    assert _db(db_path).sync_freeze_reason("a1") is None


def test_a_deployment_never_divergence_freezes(tmp_path):
    db_path = tmp_path / "t.db"
    _diverged_seed(db_path, age=timedelta(hours=300), source_mode=1, auto_sync=0)
    assert _db(db_path).sync_freeze_reason("a1") is None


def test_fork_to_own_divergence_freezes(tmp_path):
    db_path = tmp_path / "t.db"
    _diverged_seed(db_path, age=timedelta(hours=30), source_mode=1, auto_sync=1)
    assert _db(db_path).should_freeze_schedules("a1") is True


def test_a_stale_observation_does_not_divergence_freeze(tmp_path):
    """D6: fail open when the poller has not seen the agent for 15+ minutes."""
    db_path = tmp_path / "t.db"
    _diverged_seed(
        db_path, age=timedelta(hours=30),
        last_check_at=_iso(datetime.now(timezone.utc) - timedelta(minutes=16)),
    )
    assert _db(db_path).sync_freeze_reason("a1") is None


def test_divergence_needs_the_owner_opt_in(tmp_path):
    db_path = tmp_path / "t.db"
    _diverged_seed(db_path, age=timedelta(hours=30), freeze=0)
    assert _db(db_path).sync_freeze_reason("a1") is None


def test_sync_failing_reason_names_the_failures(tmp_path):
    db_path = tmp_path / "t.db"
    _seed(db_path, freeze=1, status="failed", failures=4)
    assert _db(db_path).sync_freeze_reason("a1").startswith(
        "last sync failed (seen on 4 polls)")


def test_mapping_only_rows_divergence_freeze(tmp_path, monkeypatch):
    """The PG RealDictCursor shape (#300) for the new divergence columns."""
    from contextlib import contextmanager

    db_path = tmp_path / "t.db"
    _diverged_seed(db_path, age=timedelta(hours=30))
    db = _db(db_path)

    class _DictRowCursor:
        def __init__(self, cur):
            self._cur = cur

        def execute(self, sql, params=()):
            self._cur.execute(sql, params)
            return self

        def fetchone(self):
            row = self._cur.fetchone()
            return None if row is None else dict(row)

    @contextmanager
    def dict_row_connection():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield type(
                "Conn", (), {"cursor": lambda self: _DictRowCursor(conn.cursor())}
            )()
        finally:
            conn.close()

    monkeypatch.setattr(db, "get_connection", dict_row_connection)
    assert db.should_freeze_schedules("a1") is True


# ---------------------------------------------------------------------------
# The service gate writes the reason into the skipped row
# ---------------------------------------------------------------------------

class _GateDB:
    """Just enough of SchedulerDatabase to reach the #1808 gate."""

    def __init__(self, reason):
        self._reason = reason

    def get_schedule(self, schedule_id):
        return SimpleNamespace(id=schedule_id, agent_name="a1", enabled=True)

    def get_autonomy_enabled(self, agent_name):
        return True

    def sync_freeze_reason(self, agent_name):
        return self._reason


def _gate_service(reason):
    import src.scheduler.service as scheduler_service

    svc = scheduler_service.SchedulerService(database=_GateDB(reason), lock_manager=object())
    calls = []
    svc._record_skipped_agent_schedule = lambda sid, skip_reason=None, event_reason=None: \
        calls.append({"skip_reason": skip_reason, "event_reason": event_reason})
    svc._advance_next_run_only = lambda schedule: None

    async def _hold(schedule, triggered_by):
        return False  # stop right after the freeze gate

    svc._apply_readiness_gate = _hold
    return svc, calls


def test_the_skipped_row_carries_the_freeze_reason():
    svc, calls = _gate_service("diverged 0 behind / 7 ahead for 26h")
    asyncio.run(svc._execute_schedule_with_lock("s1", triggered_by="schedule"))
    assert calls == [{
        "skip_reason": "Git sync frozen: diverged 0 behind / 7 ahead for 26h",
        "event_reason": "Git sync frozen (schedules paused)",
    }]


def test_control_arm_no_reason_no_skip_row():
    svc, calls = _gate_service(None)
    asyncio.run(svc._execute_schedule_with_lock("s1", triggered_by="schedule"))
    assert calls == []


def test_a_manual_trigger_is_never_frozen():
    svc, calls = _gate_service("diverged 0 behind / 7 ahead for 26h")
    asyncio.run(svc._execute_schedule_with_lock("s1", triggered_by="manual"))
    assert calls == []
