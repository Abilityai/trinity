"""Single lease-reaper for pull-claimed tasks — Phase 3 (#1081 / #429 / #1402).

The recovery path for a pull worker that dies/hangs, leaving a ``running``
``schedule_executions`` row with a past ``lease_expires_at``. Covers the six
required proofs, no live agent/model turn (pure DB + background sweep):

  1. Under the cap → re-queued as the SAME execution_id, status='queued',
     claim_token/lease_expires_at/claimed_by_worker cleared, redelivery_count++.
  2. At the cap (redelivery_count == MAX_REDELIVERY) → NOT re-queued; terminal
     (FAILED) + an operator_queue park row created.
  3. A lease that is NOT expired → untouched.
  4. A non-pull row (lease_expires_at NULL) → untouched (existing sweeps own it;
     the new reaper never finds or double-processes it).
  5. CAS: a second transition over an already-reaped row is a no-op — no
     double-increment / double-park (mirrors #1082 status-as-projection).
  6. Migration: the redelivery_count column exists and round-trips (dual-track;
     schema-parity + alembic-parity are proven by their own suites in evidence).

Layers, against the real db_harness schema (SQLite always; PostgreSQL when
TEST_POSTGRES_URL is set):
  * DB layer (db/schedules.py) — find_expired_leases + the two CAS transitions.
  * Service layer (services/lease_reaper_service) — the re-queue-vs-park policy
    against MAX_REDELIVERY, over the real db singleton.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: make src/backend importable (mirror test_1081_pull_endpoints.py).
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
for _shadow in ("utils", "utils.api_client", "utils.assertions", "utils.cleanup"):
    sys.modules.pop(_shadow, None)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
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
    re-resolves against the harness engine (mirror test_1081_pull_endpoints)."""
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
def schedule_ops(tmp_db):
    from db.schedules import ScheduleOperations
    from unittest.mock import MagicMock

    return ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())


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
def leased(tmp_db):
    """Insert a `running` pull-claimed schedule_executions row directly.

    ``lease_age_seconds`` > 0 makes the lease already expired (that many seconds
    ago); <= 0 makes it that many seconds in the FUTURE (not yet expired).
    Returns the execution id.
    """
    def _mk(agent_name: str, *, redelivery_count: int = 0,
            lease_age_seconds: int = 60, execution_id: str | None = None) -> str:
        import secrets as _secrets

        eid = execution_id or _secrets.token_urlsafe(12)
        now = datetime.now(timezone.utc)
        lease = now - timedelta(seconds=lease_age_seconds)
        _hrun(
            "INSERT INTO schedule_executions "
            "(id, schedule_id, agent_name, status, started_at, message, "
            " triggered_by, claim_token, lease_expires_at, claimed_by_worker, "
            " redelivery_count) "
            "VALUES (:id, '__manual__', :a, 'running', :sa, 'do stuff', 'manual', "
            " :tok, :lease, :worker, :rc)",
            id=eid, a=agent_name, sa=_iso(now - timedelta(seconds=3600)),
            tok=f"token-{eid}", lease=_iso(lease), worker=f"{agent_name}#w1",
            rc=redelivery_count,
        )
        return eid
    return _mk


def _row(eid: str) -> dict:
    """Read the reaper-relevant columns for an execution directly."""
    return {
        "status": _scalar("SELECT status FROM schedule_executions WHERE id=:i", i=eid),
        "redelivery_count": _scalar(
            "SELECT redelivery_count FROM schedule_executions WHERE id=:i", i=eid),
        "claim_token": _scalar(
            "SELECT claim_token FROM schedule_executions WHERE id=:i", i=eid),
        "lease_expires_at": _scalar(
            "SELECT lease_expires_at FROM schedule_executions WHERE id=:i", i=eid),
        "claimed_by_worker": _scalar(
            "SELECT claimed_by_worker FROM schedule_executions WHERE id=:i", i=eid),
    }


# ===========================================================================
# Proof 1 — under the cap → re-queued as the SAME execution_id
# ===========================================================================


class TestUnderCapRequeue:
    def test_expired_lease_under_cap_requeues_same_id(self, seed_agent, leased):
        seed_agent("alpha")
        eid = leased("alpha", redelivery_count=0, lease_age_seconds=60)
        from services import lease_reaper_service as lrs
        from database import db

        report = lrs.reap_expired_leases(db, max_redelivery=3)

        assert report.requeued == 1
        assert report.parked == 0
        after = _row(eid)
        # HARD invariant: the SAME row/id is re-queued — no new execution minted.
        assert _scalar("SELECT COUNT(*) FROM schedule_executions") == 1
        assert after["status"] == "queued"
        assert after["redelivery_count"] == 1                # incremented
        # #1081 B2/B5: claim_token is DELIBERATELY KEPT (not nulled) so a
        # genuinely-late result from the original worker can still CAS-match
        # while the row sits queued; the lease/worker columns ARE cleared.
        assert after["claim_token"] == f"token-{eid}"
        assert after["lease_expires_at"] is None
        assert after["claimed_by_worker"] is None
        # Still discoverable by its ORIGINAL id (execution_id preserved).
        assert db.get_execution(eid).status == "queued"

    def test_below_cap_boundary_requeues(self, seed_agent, leased):
        """redelivery_count == MAX-1 is still under the cap → re-queue (→ MAX)."""
        seed_agent("alpha")
        eid = leased("alpha", redelivery_count=2, lease_age_seconds=30)
        from services import lease_reaper_service as lrs
        from database import db

        report = lrs.reap_expired_leases(db, max_redelivery=3)
        assert report.requeued == 1 and report.parked == 0
        assert _row(eid)["status"] == "queued"
        assert _row(eid)["redelivery_count"] == 3


# ===========================================================================
# Proof 2 — at the cap → FAILED + operator_queue park row
# ===========================================================================


class TestAtCapPark:
    def test_expired_lease_at_cap_parks_and_fails(self, seed_agent, leased):
        seed_agent("alpha")
        eid = leased("alpha", redelivery_count=3, lease_age_seconds=90)
        from services import lease_reaper_service as lrs
        from database import db

        report = lrs.reap_expired_leases(db, max_redelivery=3)

        assert report.parked == 1
        assert report.requeued == 0
        assert eid in report.parked_execution_ids
        after = _row(eid)
        assert after["status"] == "failed"
        # redelivery_count is NOT bumped at park (audit trail shows it hit the cap).
        assert after["redelivery_count"] == 3
        # Poison-park is tagged + human-facing.
        err = _scalar("SELECT error FROM schedule_executions WHERE id=:i", i=eid)
        assert "poison_lease" in (err or "")
        # An operator_queue park item was created for this execution.
        park = db.get_operator_queue_item(f"poison-{eid}")
        assert park is not None
        assert park["agent_name"] == "alpha"
        assert park["type"] == "alert"
        assert park["status"] == "pending"
        assert park["context"]["execution_id"] == eid
        assert park["context"]["reason"] == "poison_lease"


# ===========================================================================
# Proof 3 — a lease that is NOT expired → untouched
# ===========================================================================


class TestNotExpired:
    def test_future_lease_is_untouched(self, seed_agent, leased):
        seed_agent("alpha")
        # lease_age_seconds negative ⇒ lease is in the FUTURE.
        eid = leased("alpha", redelivery_count=0, lease_age_seconds=-3600)
        from services import lease_reaper_service as lrs
        from database import db

        report = lrs.reap_expired_leases(db, max_redelivery=3)

        assert report.requeued == 0 and report.parked == 0
        after = _row(eid)
        assert after["status"] == "running"          # untouched
        assert after["redelivery_count"] == 0
        assert after["claim_token"] == f"token-{eid}"  # lease intact
        assert after["lease_expires_at"] is not None


# ===========================================================================
# Proof 4 — a non-pull row (no lease) → untouched, never double-processed
# ===========================================================================


class TestNonPullRowUntouched:
    def test_running_row_without_lease_is_ignored(self, seed_agent):
        seed_agent("alpha")
        # A normal push / #1083 row: running, OLD, but lease columns NULL.
        old = _iso(datetime.now(timezone.utc) - timedelta(hours=6))
        _hrun(
            "INSERT INTO schedule_executions "
            "(id, schedule_id, agent_name, status, started_at, message, triggered_by) "
            "VALUES ('push-1', '__manual__', 'alpha', 'running', :sa, 'm', 'schedule')",
            sa=old,
        )
        from services import lease_reaper_service as lrs
        from database import db

        # find_expired_leases must not surface a NULL-lease row.
        assert db.find_expired_leases() == []
        report = lrs.reap_expired_leases(db, max_redelivery=3)
        assert report.requeued == 0 and report.parked == 0
        # The row the existing sweeps own is untouched by the new reaper.
        assert _row("push-1")["status"] == "running"


# ===========================================================================
# Proof 5 — CAS: a second transition over an already-reaped row is a no-op
# ===========================================================================


class TestCasNoDoubleAct:
    def test_requeue_is_idempotent_no_double_increment(self, seed_agent, leased):
        seed_agent("alpha")
        eid = leased("alpha", redelivery_count=0, lease_age_seconds=60)
        from database import db

        # First transition wins; the row is now `queued` with the lease cleared.
        assert db.requeue_expired_lease(eid) is True
        # A second reaper pass over the SAME (now non-running) row loses the CAS.
        assert db.requeue_expired_lease(eid) is False
        assert _row(eid)["redelivery_count"] == 1     # incremented exactly once

    def test_second_reap_pass_finds_nothing(self, seed_agent, leased):
        """End-to-end: two full reaper passes over one expired row act once."""
        seed_agent("alpha")
        eid = leased("alpha", redelivery_count=0, lease_age_seconds=60)
        from services import lease_reaper_service as lrs
        from database import db

        first = lrs.reap_expired_leases(db, max_redelivery=3)
        second = lrs.reap_expired_leases(db, max_redelivery=3)
        assert first.requeued == 1
        assert second.requeued == 0 and second.parked == 0
        assert _row(eid)["redelivery_count"] == 1

    def test_park_is_idempotent_no_double_park(self, seed_agent, leased):
        seed_agent("alpha")
        eid = leased("alpha", redelivery_count=3, lease_age_seconds=60)
        from database import db

        assert db.park_expired_lease(eid, "poison_lease: x") is True
        # Second park loses the CAS (row already terminal).
        assert db.park_expired_lease(eid, "poison_lease: again") is False
        assert _row(eid)["status"] == "failed"


# ===========================================================================
# Proof 6 — the dual-track migration column exists and round-trips
# ===========================================================================


class TestMigrationColumn:
    def test_redelivery_count_column_present(self, tmp_db):
        from db.engine import get_engine, is_sqlite

        if is_sqlite():
            with get_engine().connect() as conn:
                cols = {
                    r[1] for r in conn.exec_driver_sql(
                        "PRAGMA table_info(schedule_executions)"
                    ).fetchall()
                }
            assert "redelivery_count" in cols
        # Backend-agnostic round-trip (also the PostgreSQL path).
        _hrun(
            "INSERT INTO schedule_executions "
            "(id, schedule_id, agent_name, status, started_at, message, "
            " triggered_by, redelivery_count) "
            "VALUES ('rc-1', '__m__', 'a', 'running', :sa, 'm', 'manual', 2)",
            sa=_iso(datetime.now(timezone.utc)),
        )
        assert _scalar(
            "SELECT redelivery_count FROM schedule_executions WHERE id='rc-1'"
        ) == 2

    def test_default_is_zero(self, tmp_db):
        """A row inserted without redelivery_count defaults to 0 (DDL default)."""
        _hrun(
            "INSERT INTO schedule_executions "
            "(id, schedule_id, agent_name, status, started_at, message, triggered_by) "
            "VALUES ('rc-2', '__m__', 'a', 'running', :sa, 'm', 'manual')",
            sa=_iso(datetime.now(timezone.utc)),
        )
        assert _scalar(
            "SELECT redelivery_count FROM schedule_executions WHERE id='rc-2'"
        ) == 0


# ===========================================================================
# #1081 B2/B5 — the reaper KEEPS claim_token, so a genuinely-late result from
# the ORIGINAL worker (which still holds that token) can still CAS-match and win
# over the reaper's terminal/queued row (no re-run, no swallowed SUCCESS).
# Regression: pre-fix the park/requeue nulled claim_token, so a late SUCCESS
# found no matching token → CAS lost → the row was stuck failed and the late
# result swallowed as `replayed`.
# ===========================================================================


class TestLateResultAfterReapWins:
    def test_late_success_after_park_wins(self, seed_agent, leased):
        """B2: park FAILs the row but keeps its token; the original worker's late
        SUCCESS (same token) then CAS-overwrites the poison-parked FAILED row."""
        seed_agent("alpha")
        eid = leased("alpha", redelivery_count=3, lease_age_seconds=90)  # at cap
        from services import lease_reaper_service as lrs
        from services import pull_coordination_service as pcs
        from database import db

        assert lrs.reap_expired_leases(db, max_redelivery=3).parked == 1
        assert _row(eid)["status"] == "failed"
        assert _row(eid)["claim_token"] == f"token-{eid}"      # KEPT by park (B2)

        # The original worker's genuinely-late SUCCESS still holds token-{eid}.
        outcome = pcs.apply_task_result(
            eid, f"token-{eid}", status="success", content="done"
        )
        # Pre-fix this was swallowed as `replayed` with the row stuck failed.
        assert outcome.kind == "applied"
        assert _row(eid)["status"] == "success"
        assert db.get_execution(eid).response == "done"

    def test_late_success_after_requeue_wins(self, seed_agent, leased):
        """B5 lost-result: requeue moves the SAME row to queued but keeps its
        token; the original worker's late SUCCESS finalizes that queued row in
        place (no re-run, execution_id preserved)."""
        seed_agent("alpha")
        eid = leased("alpha", redelivery_count=0, lease_age_seconds=60)  # under cap
        from services import lease_reaper_service as lrs
        from services import pull_coordination_service as pcs
        from database import db

        assert lrs.reap_expired_leases(db, max_redelivery=3).requeued == 1
        assert _row(eid)["status"] == "queued"
        assert _row(eid)["claim_token"] == f"token-{eid}"      # KEPT by requeue (B5)

        outcome = pcs.apply_task_result(
            eid, f"token-{eid}", status="success", content="done"
        )
        assert outcome.kind == "applied"
        assert _row(eid)["status"] == "success"
        assert db.get_execution(eid).response == "done"
        # No new execution minted — the SAME row was finalized.
        assert _scalar("SELECT COUNT(*) FROM schedule_executions") == 1

    def test_wrong_token_after_requeue_is_not_applied(self, seed_agent, leased):
        """B5 negative: the kept token gates the CAS to the ORIGINAL worker only —
        a WRONG token cannot finalize the queued row (it stays claimable)."""
        seed_agent("alpha")
        eid = leased("alpha", redelivery_count=0, lease_age_seconds=60)
        from services import lease_reaper_service as lrs
        from services import pull_coordination_service as pcs
        from database import db

        assert lrs.reap_expired_leases(db, max_redelivery=3).requeued == 1
        assert _row(eid)["status"] == "queued"

        outcome = pcs.apply_task_result(
            eid, "not-the-token", status="success", content="x"
        )
        # Row still non-terminal + token mismatch → conflict, never a silent write.
        assert outcome.kind == "conflict"
        assert _row(eid)["status"] == "queued"                # NOT success


# ===========================================================================
# #1081 B2 (column-level) — park KEEPS claim_token while clearing lease/worker.
# ===========================================================================


class TestParkKeepsClaimToken:
    def test_park_keeps_token_clears_lease_and_worker(self, seed_agent, leased):
        seed_agent("alpha")
        eid = leased("alpha", redelivery_count=3, lease_age_seconds=90)
        from services import lease_reaper_service as lrs
        from database import db

        assert lrs.reap_expired_leases(db, max_redelivery=3).parked == 1
        after = _row(eid)
        assert after["status"] == "failed"
        assert after["claim_token"] == f"token-{eid}"         # KEPT (B2)
        assert after["lease_expires_at"] is None              # cleared
        assert after["claimed_by_worker"] is None             # cleared


# ===========================================================================
# #1081 B3 — the operator alert is created BEFORE the park (FAILED) write, and
# the row is parked ONLY if the alert persisted. A failed alert must never yield
# an invisible poison row: park flips status→failed, which drops the row from
# find_expired_leases forever, so an alert-less park would be unrecoverable AND
# unseen. Alert-first inverts this to a benign retry: the row stays running+
# expired and is re-found next pass.
# ===========================================================================


class TestAlertBeforePark:
    def test_park_skipped_when_alert_persist_fails(self, seed_agent, leased, monkeypatch):
        seed_agent("alpha")
        eid = leased("alpha", redelivery_count=3, lease_age_seconds=90)
        from services import lease_reaper_service as lrs
        from database import db

        original_create = db.create_operator_queue_item

        def _boom(*_a, **_k):
            raise RuntimeError("operator_queue write failed")

        monkeypatch.setattr(db, "create_operator_queue_item", _boom)
        report = lrs.reap_expired_leases(db, max_redelivery=3)

        # Alert could not persist → NOT parked; the row stays running+expired and
        # is STILL a reapable candidate (never a silent, invisible poison row).
        assert report.parked == 0
        assert _row(eid)["status"] == "running"
        assert eid in [c["id"] for c in db.find_expired_leases()]
        assert db.get_operator_queue_item(f"poison-{eid}") is None

        # Alert recovers → the next pass creates the alert AND parks the row.
        monkeypatch.setattr(db, "create_operator_queue_item", original_create)
        report2 = lrs.reap_expired_leases(db, max_redelivery=3)
        assert report2.parked == 1
        assert _row(eid)["status"] == "failed"
        assert db.get_operator_queue_item(f"poison-{eid}") is not None

    def test_alert_is_created_before_the_park_write(self, seed_agent, leased, monkeypatch):
        """Ordering proof: at the instant park is invoked the alert is already
        durable (spy the park call and read the operator item from inside it)."""
        seed_agent("alpha")
        eid = leased("alpha", redelivery_count=3, lease_age_seconds=90)
        from services import lease_reaper_service as lrs
        from database import db

        original_park = db.park_expired_lease
        observed: dict = {}

        def _spy_park(execution_id, error, now_iso=None):
            observed["item"] = db.get_operator_queue_item(f"poison-{execution_id}")
            return original_park(execution_id, error, now_iso=now_iso)

        monkeypatch.setattr(db, "park_expired_lease", _spy_park)
        report = lrs.reap_expired_leases(db, max_redelivery=3)

        assert report.parked == 1
        assert observed["item"] is not None                   # alert BEFORE park
        assert observed["item"]["status"] == "pending"
        assert _row(eid)["status"] == "failed"
