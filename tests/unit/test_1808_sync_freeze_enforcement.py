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

import os
import sqlite3
import sys
from pathlib import Path

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


def _seed(db_path: Path, *, freeze: int, status: str | None, failures: int | None) -> None:
    """Minimal schema + one agent's git config and sync state."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE agent_git_config (agent_name TEXT PRIMARY KEY, "
        "freeze_schedules_if_sync_failing INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE agent_sync_state (agent_name TEXT PRIMARY KEY, "
        "last_sync_status TEXT, consecutive_failures INTEGER)"
    )
    conn.execute(
        "INSERT INTO agent_git_config VALUES (?, ?)", ("a1", freeze)
    )
    if status is not None:
        conn.execute(
            "INSERT INTO agent_sync_state VALUES (?, ?, ?)", ("a1", status, failures)
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
    """The scheduler's threshold must stay in step with the backend endpoint."""
    threshold = _scheduler_database_module().SYNC_FAILURE_FREEZE_THRESHOLD
    backend = (_REPO / "src" / "backend" / "routers" / "internal.py").read_text()
    # internal.py expresses the same rule inline.
    assert f">= {threshold}" in backend
