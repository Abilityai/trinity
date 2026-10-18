"""Pair metrics (trinity-enterprise#940, checkpoint B part 2).

The pair row's evidence block beside the checks — never pass/fail, never in
the headline: latency from dispatch to start, start to end and end to the
caller's first read, the callee run's cost, and `verified` (the callee's own
Tier-0 headline passed). Driven through `evaluate_pair` on the real schema,
read back off the written row.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent.parent / "src" / "backend"
for p in (str(_BACKEND), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)
from db_harness import db_backend  # noqa: E402,F401
from test_940_pair_checks import CALLEE, CALLER, CALLER_RUN, _activity, _exec  # noqa: E402
from test_940_pair_checks_part2 import _read_receipt, _tier0_headline  # noqa: E402

pytestmark = pytest.mark.unit

CHILD = "ChildRun00000000000001"
T0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def pe(db_backend, monkeypatch):
    from services import tier0_pair_evaluator as mod
    from database import db
    monkeypatch.setattr(db, "get_audit_entries", lambda **kw: [])
    return mod


def _set(exec_id, **values):
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import schedule_executions
    with get_engine().begin() as conn:
        conn.execute(update(schedule_executions)
                     .where(schedule_executions.c.id == exec_id).values(**values))


def _edge(*, cost=0.42, passed=True):
    _exec(CALLER_RUN, CALLER, message="/plan-sprint", triggered_by="schedule", admitted=900)
    _set(CALLER_RUN, started_at=_iso(T0), queued_at=_iso(T0),
         completed_at=_iso(T0 + timedelta(minutes=10)))
    _exec(CHILD, CALLEE, parent=CALLER_RUN, source=CALLER, cost=cost)
    _set(CHILD, queued_at=_iso(T0 + timedelta(seconds=10)),
         started_at=_iso(T0 + timedelta(seconds=13)),
         completed_at=_iso(T0 + timedelta(seconds=73)))
    _tier0_headline(CHILD, passed)
    _activity(CHILD)


def _metrics(row):
    return row["checks"]["metrics"]


def test_the_row_carries_the_three_latencies_cost_and_verified(pe):
    _edge()
    _read_receipt(CHILD, at=_iso(T0 + timedelta(seconds=80)))
    m = _metrics(pe.evaluate_pair(CHILD))
    assert m["latency_ms"] == {"dispatch_to_start": 3000, "start_to_end": 60000,
                               "end_to_first_read": 7000}
    assert m["cost"] == pytest.approx(0.42)
    assert m["verified"] is True


def test_an_unread_result_has_no_read_latency_and_a_failed_grade_is_unverified(pe):
    _edge(passed=False)
    m = _metrics(pe.evaluate_pair(CHILD))
    assert m["latency_ms"]["end_to_first_read"] is None
    assert m["latency_ms"]["start_to_end"] == 60000
    assert m["verified"] is False


def test_missing_evidence_is_none_never_zero(pe):
    """No admission time, no cost, an ungraded callee headline: each metric
    says "unknown" rather than a number that would pull a mean down."""
    _edge(cost=None)
    _set(CHILD, queued_at=None)
    from database import db
    db.upsert_platform_evaluation(CALLEE, execution_id=CHILD, completion=True,
                                  archetype="message", checks={"schema": 1, "checks": []})
    m = _metrics(pe.evaluate_pair(CHILD))
    assert m["latency_ms"]["dispatch_to_start"] is None
    assert m["cost"] is None
    assert m["verified"] is None


def test_a_read_before_the_callee_ended_is_not_the_first_read(pe):
    """Polling a still-running result is not reading it."""
    _edge()
    _read_receipt(CHILD, at=_iso(T0 + timedelta(seconds=40)), act_id="early")
    _read_receipt(CHILD, at=_iso(T0 + timedelta(seconds=90)), act_id="late")
    assert _metrics(pe.evaluate_pair(CHILD))["latency_ms"]["end_to_first_read"] == 17000


def test_metrics_never_enter_the_headline(pe):
    _edge(passed=False)
    row = pe.evaluate_pair(CHILD)
    ids = {c["id"] for c in row["checks"]["checks"]}
    assert not ids & {"metrics", "latency_ms", "cost", "verified"}
    assert row["checks"]["headline"] == pe.headline(row["checks"]["checks"])
