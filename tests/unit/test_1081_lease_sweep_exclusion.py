"""Non-lease cleanup sweeps must EXCLUDE pull-leased rows (#1081 Phase 3).

Follow-up to #1081 Phase 3. The lease-reaper (``cleanup_service._sweep_expired_leases``
+ ``services/lease_reaper_service``) is the SOLE authority over pull-claimed
("leased") ``schedule_executions`` rows — keyed off ``lease_expires_at IS NOT
NULL``. A pull-claimed row is ``running`` with a lease but a NULL
``claude_session_id`` until its worker begins the turn, so every OTHER cleanup
sweep / stale-row selector that picks ``running`` rows for a destructive
transition MUST exclude leased rows, or it would FAIL a legitimate lease before
it expires — defeating the reaper.

Invariant enforced (two lines):
  * Leased rows (``lease_expires_at IS NOT NULL``) are owned EXCLUSIVELY by the
    lease-reaper.
  * Every other cleanup sweep / stale-row selector excludes them
    (``lease_expires_at IS NULL``); NULL-lease (non-pull) rows are handled as
    before — no regression.

Pure DB (real ``db_harness`` schema; SQLite always, PostgreSQL when
``TEST_POSTGRES_URL`` is set) — no live agent/model turn. Each sweep gets a
leased-excluded proof AND a non-leased-still-handled regression proof.

Sweeps / selectors covered:
  * ``mark_no_session_executions_failed`` — the #106 no-session sweep (PRIMARY).
  * ``mark_stale_executions_failed``       — the generic stale-execution sweep.
  * ``get_running_executions``             — startup orphan-recovery selector.
  * ``get_running_executions_with_agent_info`` — periodic watchdog selector.
  * ``fail_stale_slot_execution``          — the #1083 stale-slot writer.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: make src/backend importable (mirror test_1081_lease_reaper.py).
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
# NB: do NOT pop `utils` from sys.modules here. tests/unit/conftest.py already
# installs src/backend/utils as the canonical `utils` package via an importlib
# file loader precisely so sys.path ordering cannot shadow it. Evicting that
# registration leaves `utils` unbound, and pytest's prepend import mode puts
# `tests/` back at sys.path[0] — so the next module to import `utils` fresh
# binds the tests/utils helper package instead, and every later
# `from utils.url_validation import ...` in backend code dies at collection.
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun, scalar as _scalar  # noqa: E402

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# sys.modules hygiene (Issue #762): this file evicts shadowing/cached backend
# modules (at import time above, and in the db fixtures below) so production
# code re-resolves against the db_harness engine. Snapshot + restore those
# names around every test so the eviction never leaks to other test files.
# The _STUBBED_MODULE_NAMES + _restore_sys_modules pair is the lint-recognised
# precedent (tests/unit/test_telegram_webhook_backfill.py, tests/lint_sys_modules.py).
# ---------------------------------------------------------------------------
_STUBBED_MODULE_NAMES = [
    "utils",
    "utils.api_client",
    "utils.assertions",
    "utils.cleanup",
    "db.connection",
    "db.schedules",
    "db.operator_queue",
    "db.agent_settings.resources",
    "database",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot the churned backend modules before each test and restore them
    after, so this file's import-time + fixture sys.modules eviction cannot
    pollute unrelated tests in the same session (Issue #762)."""
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(db_backend):
    """Fresh full production schema; pop cached db modules so production code
    re-resolves against the harness engine (mirror test_1081_lease_reaper)."""
    def _evict():
        for mod in ("db.connection", "db.schedules", "db.operator_queue",
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
    """Insert a `running` schedule_executions row directly.

    ``leased=True`` stamps a past ``lease_expires_at`` (+ claim token/worker) so
    the row is a pull-claimed lease; ``leased=False`` leaves the lease columns
    NULL (a legacy push / #1083 row). ``session=None`` leaves ``claude_session_id``
    NULL. ``age_seconds`` sets how long ago the row started. Returns the id.
    """
    def _mk(agent_name: str, *, leased: bool, session: str | None = None,
            age_seconds: int = 3600, execution_id: str | None = None) -> str:
        import secrets as _secrets

        eid = execution_id or _secrets.token_urlsafe(12)
        now = datetime.now(timezone.utc)
        started = now - timedelta(seconds=age_seconds)
        if leased:
            # A lease expired 60s ago — old enough that the reaper WOULD act, but
            # the point of these tests is that NON-lease sweeps must NOT.
            lease = _iso(now - timedelta(seconds=60))
            tok = f"token-{eid}"
            worker = f"{agent_name}#w1"
        else:
            lease = None
            tok = None
            worker = None
        _hrun(
            "INSERT INTO schedule_executions "
            "(id, schedule_id, agent_name, status, started_at, message, "
            " triggered_by, claude_session_id, claim_token, lease_expires_at, "
            " claimed_by_worker, redelivery_count) "
            "VALUES (:id, '__manual__', :a, 'running', :sa, 'do stuff', 'manual', "
            " :sess, :tok, :lease, :worker, 0)",
            id=eid, a=agent_name, sa=_iso(started), sess=session,
            tok=tok, lease=lease, worker=worker,
        )
        return eid
    return _mk


def _status(eid: str) -> str:
    return _scalar("SELECT status FROM schedule_executions WHERE id=:i", i=eid)


# ===========================================================================
# 1. no-session sweep (#106) — PRIMARY fix
# ===========================================================================


class TestNoSessionSweep:
    def test_leased_no_session_row_is_not_failed(self, seed_agent, running_row):
        """A leased pull row (session NULL, lease NOT NULL) survives the
        no-session sweep — left for the lease-reaper."""
        seed_agent("alpha")
        eid = running_row("alpha", leased=True, session=None, age_seconds=3600)
        from database import db

        failed = db.mark_no_session_executions_failed(timeout_seconds=60)

        assert failed == 0
        assert _status(eid) == "running"

    def test_non_leased_no_session_row_is_still_failed(self, seed_agent, running_row):
        """Regression: a legacy non-pull row (session NULL, lease NULL) is still
        failed — the sweep's real job is unchanged."""
        seed_agent("alpha")
        eid = running_row("alpha", leased=False, session=None, age_seconds=3600)
        from database import db

        failed = db.mark_no_session_executions_failed(timeout_seconds=60)

        assert failed == 1
        assert _status(eid) == "failed"

    def test_mixed_fleet_only_non_leased_row_failed(self, seed_agent, running_row):
        """Both rows present: only the NULL-lease one is swept."""
        seed_agent("alpha")
        leased = running_row("alpha", leased=True, session=None, age_seconds=3600)
        legacy = running_row("alpha", leased=False, session=None, age_seconds=3600)
        from database import db

        assert db.mark_no_session_executions_failed(timeout_seconds=60) == 1
        assert _status(leased) == "running"
        assert _status(legacy) == "failed"


# ===========================================================================
# 2. generic stale-execution sweep
# ===========================================================================


class TestStaleExecutionSweep:
    def test_leased_stale_row_is_not_failed(self, seed_agent, running_row):
        seed_agent("alpha")
        # 1h old, past a 1-min stale window — but leased ⇒ reaper-owned.
        eid = running_row("alpha", leased=True, session="sess-x", age_seconds=3600)
        from database import db

        failed = db.mark_stale_executions_failed(timeout_minutes=1)

        assert failed == 0
        assert _status(eid) == "running"

    def test_non_leased_stale_row_is_still_failed(self, seed_agent, running_row):
        seed_agent("alpha")
        eid = running_row("alpha", leased=False, session="sess-x", age_seconds=3600)
        from database import db

        failed = db.mark_stale_executions_failed(timeout_minutes=1)

        assert failed == 1
        assert _status(eid) == "failed"


# ===========================================================================
# 3. startup orphan-recovery selector (get_running_executions)
# ===========================================================================


class TestStartupRecoverySelector:
    def test_selector_excludes_leased_includes_non_leased(self, seed_agent, running_row):
        seed_agent("alpha")
        leased = running_row("alpha", leased=True, session=None)
        legacy = running_row("alpha", leased=False, session=None)
        from database import db

        ids = {r["id"] for r in db.get_running_executions()}

        assert legacy in ids            # startup recovery still reconciles it
        assert leased not in ids        # lease-reaper owns it


# ===========================================================================
# 4. periodic watchdog selector (get_running_executions_with_agent_info)
# ===========================================================================


class TestWatchdogSelector:
    def test_selector_excludes_leased_includes_non_leased(self, seed_agent, running_row):
        seed_agent("alpha")
        leased = running_row("alpha", leased=True, session=None)
        legacy = running_row("alpha", leased=False, session=None)
        from database import db

        ids = {r["id"] for r in db.get_running_executions_with_agent_info()}

        assert legacy in ids            # watchdog still reconciles it
        assert leased not in ids        # lease-reaper owns it


# ===========================================================================
# 5. #1083 stale-slot writer (fail_stale_slot_execution)
# ===========================================================================


class TestStaleSlotWriter:
    def test_leased_row_not_failed_by_stale_slot_writer(self, seed_agent, running_row):
        """Even if a pull-leased row's Redis slot TTL is reclaimed, the writer
        declines (CAS gated on NULL lease) — leaving it for the lease-reaper."""
        seed_agent("alpha")
        eid = running_row("alpha", leased=True, session=None)
        from database import db

        updated = db.fail_stale_slot_execution(eid, "stale slot reclaimed")

        assert updated is False
        assert _status(eid) == "running"

    def test_non_leased_row_still_failed_by_stale_slot_writer(self, seed_agent, running_row):
        seed_agent("alpha")
        eid = running_row("alpha", leased=False, session=None)
        from database import db

        updated = db.fail_stale_slot_execution(eid, "stale slot reclaimed")

        assert updated is True
        assert _status(eid) == "failed"
