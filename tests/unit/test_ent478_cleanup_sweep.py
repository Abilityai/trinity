"""The metric-point retention sweep (trinity-enterprise#478, C7).

The sweep is a destructive background loop, so what is tested here is the
WIRING that bounds the destruction: the literal setting key (which
`test_1771a_retention_edges` asserts must pair one-to-one with a
`RETENTION_OPS_KEYS` entry), the table-proportionate floor, the disabled case,
and the two rules that differ from the sibling sweeps — the per-cycle chunk
bound and when the single-use acknowledgement is consumed.

The store is monkeypatched: these assertions are about the sweep's decisions,
and the SQL they drive is proved against real rows in
`test_ent478_metric_points_store.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytest.importorskip("sqlalchemy", reason="backend venv required")

import services.cleanup_service as cleanup  # noqa: E402
from services.retention_guard import FLOOR_METRIC_POINTS  # noqa: E402


class _Calls:
    def __init__(self):
        self.guard = []
        self.pruned = []
        self.acks = []
        self.candidates = []


@pytest.fixture
def sweep(monkeypatch):
    """The sweep with its guard, store and ack consumer observable."""
    calls = _Calls()

    class _Db:
        remaining_after_prune = 0

        def count_metric_points_candidates(self, days, limit):
            calls.candidates.append((days, limit))
            return self.remaining_after_prune

        def prune_metric_points(self, retention_days, chunk_size):
            calls.pruned.append((retention_days, chunk_size))
            return 42

    fake_db = _Db()
    monkeypatch.setattr(cleanup, "db", fake_db)

    def _guard_allows(key, table, days, count_fn, floor=None):
        calls.guard.append({"key": key, "table": table, "days": days,
                            "floor": floor})
        count_fn(1)  # the guard always asks; prove the count fn is wired
        return _guard_allows.verdict
    _guard_allows.verdict = True
    monkeypatch.setattr(cleanup, "_guard_allows", _guard_allows)
    monkeypatch.setattr(cleanup, "_after_guarded_prune",
                        lambda key: calls.acks.append(key))
    monkeypatch.setattr(cleanup, "_log_prune", lambda *a, **k: None)

    class Ctx:
        service = cleanup.CleanupService()
        report = cleanup.CleanupReport()

    ctx = Ctx()
    ctx.calls = calls
    ctx.db = fake_db
    ctx.guard = _guard_allows

    def _window(days):
        monkeypatch.setattr(cleanup, "_read_retention_setting", lambda key: days)
    ctx.set_window = _window
    _window(365)
    return ctx


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_the_sweep_guards_on_its_own_literal_setting_key(sweep):
    """`test_1771a_retention_edges` asserts set-equality between
    `RETENTION_OPS_KEYS` and the `_guard_allows` call sites, so a key without
    its site (or a site under a different spelling) reds that suite three files
    away from the cause."""
    sweep.service._sweep_metric_points(sweep.report)

    assert sweep.calls.guard[0]["key"] == "metrics_retention_days"
    assert sweep.calls.guard[0]["table"] == "metric_points"


def test_the_guard_floor_is_sized_to_the_table(sweep):
    """TD-5: at the default `MAX_ROWS_PER_SWEEP = 1000`, any fleet above about
    288k points a day has more than a thousand rows fall out of the window
    every cycle — so the guard would refuse EVERY cycle, alarm once, and then
    sit blocked behind single-use acks while the table grew."""
    sweep.service._sweep_metric_points(sweep.report)
    assert sweep.calls.guard[0]["floor"] == FLOOR_METRIC_POINTS
    assert FLOOR_METRIC_POINTS == 100_000


def test_the_prune_runs_when_the_guard_allows(sweep):
    sweep.service._sweep_metric_points(sweep.report)

    assert sweep.calls.pruned == [(365, cleanup.RETENTION_CHUNK_SIZE_PER_CYCLE)]
    assert sweep.report.metric_points_pruned == 42


def test_a_refused_sweep_deletes_nothing(sweep):
    sweep.guard.verdict = False
    sweep.service._sweep_metric_points(sweep.report)

    assert sweep.calls.pruned == []
    assert sweep.report.metric_points_pruned == 0
    assert sweep.calls.acks == [], "nothing to acknowledge; nothing ran"


def test_a_window_of_zero_disables_the_sweep_entirely(sweep):
    """`0` means keep forever on every sibling window. It must not even ask the
    guard — an evaluation is a count over the table."""
    sweep.set_window(0)
    sweep.service._sweep_metric_points(sweep.report)

    assert sweep.calls.guard == [] and sweep.calls.pruned == []


def test_a_store_failure_is_contained_to_this_sweep(sweep, monkeypatch):
    """One failing sweep must not abort the cleanup cycle and leave the other
    sweeps unrun for as long as the failure lasts."""
    def _boom(**kwargs):
        raise RuntimeError("store down")
    monkeypatch.setattr(sweep.db, "prune_metric_points", _boom)

    sweep.service._sweep_metric_points(sweep.report)  # must not raise
    assert sweep.report.metric_points_pruned == 0


# ---------------------------------------------------------------------------
# The two rules that differ from the siblings (TD-14)
# ---------------------------------------------------------------------------

def test_the_ack_is_consumed_once_the_backlog_is_under_the_floor(sweep):
    sweep.db.remaining_after_prune = 0
    sweep.service._sweep_metric_points(sweep.report)
    assert sweep.calls.acks == ["metrics_retention_days"]


def test_the_ack_survives_a_prune_that_did_not_finish_the_backlog(sweep):
    """The prune is bounded per call, so the first cycle after an approval may
    leave most of the backlog. Consuming the ack there would ask the operator
    to approve the same intent again for the remainder."""
    sweep.db.remaining_after_prune = FLOOR_METRIC_POINTS + 1
    sweep.service._sweep_metric_points(sweep.report)
    assert sweep.calls.acks == []


def test_the_remaining_count_is_bounded(sweep):
    """It is asked on every allowed cycle, so it must cost the floor, not the
    table: `LIMIT floor + 1` answers "still over?" without counting 36 million
    rows."""
    sweep.service._sweep_metric_points(sweep.report)
    limits = [limit for _days, limit in sweep.calls.candidates]
    assert FLOOR_METRIC_POINTS + 1 in limits


# ---------------------------------------------------------------------------
# The report (E18 — three sum sites)
# ---------------------------------------------------------------------------

def test_the_pruned_count_reaches_all_three_report_sums():
    """`total`, `to_dict` and the `retention_total` that gates the SQLite WAL
    checkpoint are three separate sums; a new counter added to one of them
    reads as "nothing was cleaned" in the other two."""
    report = cleanup.CleanupReport()
    report.metric_points_pruned = 7

    assert report.total == 7
    assert report.to_dict()["metric_points_pruned"] == 7
    assert report.to_dict()["total"] == 7


def test_the_wal_checkpoint_sum_counts_this_sweep(monkeypatch):
    """Proved by RUNNING the branch that computes it: a reclaiming sweep that
    is missing from `retention_total` leaves the SQLite file at its high-water
    mark until some other sweep happens to run on the same cycle."""
    checkpoints = []
    monkeypatch.setattr(cleanup, "_wal_checkpoint_truncate",
                        lambda: checkpoints.append(True))

    report = cleanup.CleanupReport()
    report.metric_points_pruned = 5
    cleanup.CleanupService()._maybe_wal_checkpoint(report)

    assert checkpoints == [True], (
        "the retention_total that gates _wal_checkpoint_truncate must include "
        "this sweep"
    )

    checkpoints.clear()
    cleanup.CleanupService()._maybe_wal_checkpoint(cleanup.CleanupReport())
    assert checkpoints == [], "an empty cycle must not checkpoint"


@pytest.mark.asyncio
async def test_the_sweep_is_wired_into_the_cleanup_cycle(sweep, monkeypatch):
    """A sweep method nothing calls is a sweep that never runs — so the cycle
    itself is driven, with every OTHER sweep neutralised, and this one is
    observed to have fired."""
    service = sweep.service
    for attribute in dir(service):
        if attribute.startswith("_sweep_") and attribute != "_sweep_metric_points":
            target = getattr(service, attribute)
            if callable(target):
                monkeypatch.setattr(
                    service, attribute,
                    _noop_async(target) if _is_async(target) else (lambda *a, **k: None))
    monkeypatch.setattr(service, "_maybe_wal_checkpoint", lambda *a, **k: None)

    report = await service._run_cleanup_inner()
    assert report.metric_points_pruned == 42


def _is_async(fn):
    import inspect
    return inspect.iscoroutinefunction(fn)


def _noop_async(_fn):
    async def _inner(*a, **k):
        return None
    return _inner
