"""#1644 — retention blast-radius guard.

Guards a class of bug that has already cost a production instance ~3 months of
history (#1638). Two properties matter more than any individual assertion here:

1. **Predicate parity** — the guard's count and the prune's delete must describe
   the SAME row set. They share a predicate by construction (see
   `db/schedules.py:_execution_row_prune_predicate`), and the parity tests below
   assert that construction actually holds against a live DB. A guard that counts
   a different set than the prune deletes reports a blast radius that isn't the
   one about to happen.

2. **Fail-closed** — every error path must REFUSE. A guard that fails open is
   worse than no guard because it manufactures confidence. #1638 shipped green
   because a test asserted the destructive behaviour AS the requirement; these
   tests exist so that can't happen twice.
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db_harness import db_backend, run as _hrun, scalar as _hscalar  # noqa: E402,F401

from utils.helpers import iso_cutoff  # noqa: E402


def _days_ago_iso(days: int) -> str:
    return iso_cutoff(hours=days * 24)


@pytest.fixture
def ops(db_backend):
    """Fresh operation classes bound to the harness engine."""
    for mod in ("db.connection", "db.schedules", "db.monitoring", "db.reports"):
        sys.modules.pop(mod, None)
    from db.schedules import ScheduleOperations
    from db.monitoring import MonitoringOperations

    return ScheduleOperations(None, None), MonitoringOperations()


def _seed_execution(eid: str, days_old: int, status: str = "success", log: str = "x"):
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, completed_at, message, "
        " triggered_by, execution_log) "
        "VALUES (:id, 's1', 'a1', :st, :ts, :ts, 'm', 'schedule', :log)",
        id=eid, st=status, ts=_days_ago_iso(days_old), log=log,
    )


# ---------------------------------------------------------------------------
# Property 1: predicate parity — count == what the prune actually touches
# ---------------------------------------------------------------------------

class TestPredicateParity:
    """The count and the prune must never describe different row sets."""

    def test_execution_rows_count_matches_prune(self, ops):
        sched, _ = ops
        for i in range(7):
            _seed_execution(f"old{i}", days_old=100)
        for i in range(3):
            _seed_execution(f"new{i}", days_old=1)
        # A running row is not terminal — neither side may touch it.
        _seed_execution("running", days_old=100, status="running")

        counted = sched.count_execution_row_candidates(90, limit=1000)
        pruned = sched.prune_execution_rows(90, chunk_size=1000)
        assert counted == pruned == 7
        assert _hscalar("SELECT COUNT(*) FROM schedule_executions") == 4

    def test_execution_log_count_matches_prune(self, ops):
        sched, _ = ops
        for i in range(4):
            _seed_execution(f"old{i}", days_old=100, log="transcript")
        # Already-nulled rows are excluded by the shared predicate's
        # `execution_log IS NOT NULL` term — both sides must agree on that.
        _seed_execution("nulled", days_old=100, log=None)

        counted = sched.count_execution_log_candidates(90, limit=1000)
        pruned = sched.prune_execution_logs(90, chunk_size=1000)
        assert counted == pruned == 4

    def test_health_check_count_matches_prune(self, ops):
        _, mon = ops
        for i in range(5):
            _hrun(
                "INSERT INTO agent_health_checks "
                "(id, agent_name, check_type, status, checked_at) "
                "VALUES (:id, 'a1', 'periodic', 'healthy', :ts)",
                id=f"hc-old-{i}", ts=_days_ago_iso(30),
            )
        _hrun(
            "INSERT INTO agent_health_checks "
            "(id, agent_name, check_type, status, checked_at) "
            "VALUES ('hc-new', 'a1', 'periodic', 'healthy', :ts)",
            ts=_days_ago_iso(1),
        )
        counted = mon.count_health_check_candidates(7, limit=1000)
        pruned = mon.cleanup_old_records(7, chunk_size=1000)
        assert counted == pruned == 5

    def test_count_is_bounded_by_limit(self, ops):
        """The count stops at `limit` — it answers 'more than N?', not 'how many?'."""
        sched, _ = ops
        for i in range(20):
            _seed_execution(f"old{i}", days_old=100)
        # limit=6 (threshold 5 + 1) must return exactly 6, not 20.
        assert sched.count_execution_row_candidates(90, limit=6) == 6

    def test_disabled_window_counts_nothing(self, ops):
        sched, _ = ops
        _seed_execution("old", days_old=100)
        assert sched.count_execution_row_candidates(0, limit=1000) == 0

    def test_empty_table_counts_zero_and_does_not_raise(self, ops):
        """No zero-division: the guard holds no denominator by design."""
        sched, _ = ops
        assert sched.count_execution_row_candidates(90, limit=1000) == 0


# ---------------------------------------------------------------------------
# Property 2: fail-closed. Every error path refuses.
# ---------------------------------------------------------------------------

class TestFailsClosed:
    """A guard that fails open is worse than no guard (#1644)."""

    def _guard(self):
        import services.retention_guard as rg
        return rg

    def test_count_error_refuses(self):
        rg = self._guard()

        def boom(limit):
            raise RuntimeError("db is down")

        with patch.object(rg, "MAX_ROWS_PER_SWEEP", 1000):
            v = rg.evaluate("execution_row_retention_days", 5, boom)
        assert v.allowed is False
        assert v.reason == "count_failed"

    def test_ack_lookup_error_refuses(self):
        """The dangerous one: `except: return True` here auto-approves mass deletion."""
        rg = self._guard()
        with patch.object(rg, "MAX_ROWS_PER_SWEEP", 10), \
             patch.object(rg, "is_acknowledged", side_effect=RuntimeError("boom")):
            v = rg.evaluate("execution_row_retention_days", 5, lambda limit: 999)
        assert v.allowed is False
        assert v.reason == "ack_lookup_failed"

    def test_alarm_failure_never_flips_the_refusal(self):
        """A failed alarm must not become a permitted prune."""
        rg = self._guard()
        v = rg.GuardVerdict(False, 5000, 1000, "over_threshold")
        with patch.object(rg.db, "create_operator_queue_item",
                          side_effect=RuntimeError("queue down")), \
             patch.object(rg.db, "get_setting_value", return_value=None):
            rg.announce_refusal("execution_row_retention_days", "rows", 5, v)
        assert v.allowed is False


# ---------------------------------------------------------------------------
# Guard decision logic
# ---------------------------------------------------------------------------

class TestGuardDecisions:

    def _guard(self):
        import services.retention_guard as rg
        return rg

    def test_under_threshold_proceeds_silently(self):
        """No alarm fatigue: the steady-state trickle must never trip the guard."""
        rg = self._guard()
        with patch.object(rg, "MAX_ROWS_PER_SWEEP", 1000):
            v = rg.evaluate("execution_row_retention_days", 90, lambda limit: 12)
        assert v.allowed is True
        assert v.reason == "under_threshold"

    def test_over_threshold_refuses(self):
        rg = self._guard()
        with patch.object(rg, "MAX_ROWS_PER_SWEEP", 1000), \
             patch.object(rg, "is_acknowledged", return_value=False):
            v = rg.evaluate("execution_row_retention_days", 5, lambda limit: 1001)
        assert v.allowed is False
        assert v.reason == "over_threshold"

    def test_acknowledged_proceeds(self):
        rg = self._guard()
        with patch.object(rg, "MAX_ROWS_PER_SWEEP", 1000), \
             patch.object(rg, "is_acknowledged", return_value=True):
            v = rg.evaluate("execution_row_retention_days", 5, lambda limit: 1001)
        assert v.allowed is True
        assert v.reason == "acknowledged"

    def test_agent_floor_is_zero_so_any_purge_acks(self):
        """#1581: every agent purge destroys volumes. One candidate is enough."""
        rg = self._guard()
        with patch.object(rg, "is_acknowledged", return_value=False):
            v = rg.evaluate("agent_soft_delete_retention_days", 5,
                            lambda limit: 1, floor=rg.FLOOR_AGENTS)
        assert v.allowed is False, "a single volume destruction must be acked"

    def test_agent_sweep_with_no_candidates_proceeds(self):
        rg = self._guard()
        v = rg.evaluate("agent_soft_delete_retention_days", 180,
                        lambda limit: 0, floor=rg.FLOOR_AGENTS)
        assert v.allowed is True


# ---------------------------------------------------------------------------
# The ack: bound to the window, single-use
# ---------------------------------------------------------------------------

class TestAcknowledgement:

    def test_ack_is_bound_to_the_window(self, db_backend):
        """Approving a prune at 30d must NOT authorize one at 5d."""
        import services.retention_guard as rg
        rg.record_acknowledgement("execution_row_retention_days", 30)
        assert rg.is_acknowledged("execution_row_retention_days", 30) is True
        assert rg.is_acknowledged("execution_row_retention_days", 5) is False

    def test_ack_is_scoped_to_its_setting(self, db_backend):
        import services.retention_guard as rg
        rg.record_acknowledgement("execution_row_retention_days", 5)
        assert rg.is_acknowledged("health_check_retention_days", 5) is False

    def test_ack_is_single_use(self, db_backend):
        """Consumed after the prune runs, so the guard re-arms."""
        import services.retention_guard as rg
        rg.record_acknowledgement("execution_row_retention_days", 5)
        assert rg.is_acknowledged("execution_row_retention_days", 5) is True
        rg.consume_acknowledgement("execution_row_retention_days")
        assert rg.is_acknowledged("execution_row_retention_days", 5) is False

    def test_missing_ack_is_not_acknowledged(self, db_backend):
        import services.retention_guard as rg
        assert rg.is_acknowledged("execution_row_retention_days", 5) is False


# ---------------------------------------------------------------------------
# The guard's own #1638 exposure — it is a destructive-action constant too
# ---------------------------------------------------------------------------

class TestGuardIsNotItselfA1638:

    def test_threshold_is_a_constant_not_an_operator_setting(self):
        """The threshold was briefly a Settings knob. That reproduced #1638 one
        level up: a mutable constant read at ACTION time gating a destructive
        operation, where raising it silently disarms the guard fleet-wide. It is
        now a fixed constant — unreachable from any endpoint, so there is nothing
        to clamp, blocklist, or typo."""
        import services.retention_guard as rg
        from services.settings_service import OPS_SETTINGS_DEFAULTS

        assert isinstance(rg.MAX_ROWS_PER_SWEEP, int)
        assert not hasattr(rg, "THRESHOLD_SETTING_KEY"), (
            "the threshold must not be settings-backed"
        )
        assert not any("retention_guard" in k for k in OPS_SETTINGS_DEFAULTS), (
            "the guard threshold must not be an ops setting — an operator who "
            "raises it disarms the guard, which is the #1638 pattern"
        )

    def test_threshold_direction_is_pinned_not_its_value(self):
        """#1638 shipped green because a test asserted the DESTRUCTIVE value as
        the requirement. Pin the safe DIRECTION instead: this may be lowered
        freely, but raising it weakens every install at once."""
        from services.retention_guard import MAX_ROWS_PER_SWEEP
        PREVIOUS = 1000
        assert MAX_ROWS_PER_SWEEP <= PREVIOUS, (
            "Raising the guard threshold weakens every install. If intentional, "
            "it needs a reviewer and a docs/migrations/ note, not a one-line diff."
        )


# ---------------------------------------------------------------------------
# Cross-module invariants
# ---------------------------------------------------------------------------

class TestCrossModuleInvariants:

    def test_sentinel_matches_the_canary_exclusion(self):
        """canary/snapshot.py duplicates the sentinel as a literal (it must not
        import a service). If they drift, L-03 fires forever on the alarm."""
        from services.retention_guard import ALARM_AGENT_NAME
        from canary.snapshot import _RETENTION_GUARD_AGENT
        assert ALARM_AGENT_NAME == _RETENTION_GUARD_AGENT

    def test_sentinel_is_uncreatable_as_an_agent_name(self):
        """A real agent with this name would inherit the alarm into its owner's
        ACL and start receiving it in its queue file via the 5s sync loop."""
        from services.retention_guard import ALARM_AGENT_NAME
        from utils.helpers import sanitize_agent_name
        assert sanitize_agent_name(ALARM_AGENT_NAME) != ALARM_AGENT_NAME

    def test_every_retention_window_is_in_retention_ops_keys(self):
        """#1644: `agent_reports_` and `operator_queue_` were retention windows
        missing from this tuple, so /ops/reset deleted them while reporting
        'retention windows unchanged', and boot logging never showed them."""
        from services.settings_service import (
            RETENTION_OPS_KEYS, OPS_SETTINGS_DEFAULTS,
        )
        windows = {
            k for k in OPS_SETTINGS_DEFAULTS
            if k.endswith("_retention_days") and not k.startswith("ops_")
        }
        assert windows - set(RETENTION_OPS_KEYS) == set(), (
            "a retention window is missing from RETENTION_OPS_KEYS — "
            "/ops/reset will delete it and /api/settings/retention will hide it"
        )

    def test_all_retention_ops_keys_have_defaults(self):
        from services.settings_service import (
            RETENTION_OPS_KEYS, OPS_SETTINGS_DEFAULTS,
        )
        for key in RETENTION_OPS_KEYS:
            assert key in OPS_SETTINGS_DEFAULTS


# ---------------------------------------------------------------------------
# The #1638 scenario, end to end
# ---------------------------------------------------------------------------

class TestPrevents1638(object):

    def test_the_1638_scenario_is_refused(self, ops):
        """5352 of 5593 rows (95.7%), the real incident. Nothing may be deleted."""
        import services.retention_guard as rg
        sched, _ = ops

        for i in range(120):
            _seed_execution(f"old{i}", days_old=100)
        for i in range(5):
            _seed_execution(f"recent{i}", days_old=1)
        before = _hscalar("SELECT COUNT(*) FROM schedule_executions")

        # Window narrows 90 -> 5. Guard sees the blast radius first.
        with patch.object(rg, "MAX_ROWS_PER_SWEEP", 100):
            verdict = rg.evaluate(
                "execution_row_retention_days", 5,
                lambda limit: sched.count_execution_row_candidates(5, limit),
            )
        assert verdict.allowed is False, "#1638 must be refused"
        assert _hscalar("SELECT COUNT(*) FROM schedule_executions") == before, (
            "evaluate() must never delete anything"
        )

    def test_after_acknowledgement_the_prune_proceeds(self, ops):
        import services.retention_guard as rg
        sched, _ = ops
        for i in range(120):
            _seed_execution(f"old{i}", days_old=100)

        rg.record_acknowledgement("execution_row_retention_days", 5)
        with patch.object(rg, "MAX_ROWS_PER_SWEEP", 100):
            verdict = rg.evaluate(
                "execution_row_retention_days", 5,
                lambda limit: sched.count_execution_row_candidates(5, limit),
            )
        assert verdict.allowed is True
        assert sched.prune_execution_rows(5, chunk_size=1000) == 120
