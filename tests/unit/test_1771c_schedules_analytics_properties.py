"""Hypothesis properties (sub-area B) — analytics aggregation (#1771 target 3).

Properties P-B1…P-B4 from the `/edge-cases` matrix
(``.plan/edge-cases-1771c-matrix.md``). Discrete companions live in
``test_1771c_schedules_analytics_edges.py``.

⚠️  The locked data-source discipline (quoted in full in the edges file) is the
oracle here. A property that contradicts it — e.g. asserting the headline
duration ``avg`` tracks the capped percentile pool — is a WRONG TEST, not a
finding.

CI bounds: ``max_examples`` 100 (DB-touching) / 200 (pure), ``deadline=None``.

Hypothesis ↔ function-scoped fixture: the health check is suppressed, so the DB
is built ONCE and shared by every example. Correctness therefore rests on each
example using a **unique agent name** — the analytics queries are all
``agent_name``-scoped, so per-example isolation is exact rather than
best-effort.

Backend honesty (Invariant #3/#9): SQLite only unless ``TEST_POSTGRES_URL`` is
set. P-B1/P-B2 lean on ``COUNT``/``CASE`` and P-B3 is pure Python date
arithmetic — all backend-agnostic. P-B4 touches no database at all.
"""

from __future__ import annotations

import itertools
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Bootstrap (see the CAS edges file for the tests/utils shadowing rationale)
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
for _shadow in ("utils", "utils.api_client", "utils.assertions", "utils.cleanup"):
    sys.modules.pop(_shadow, None)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun  # noqa: E402,F401

_DB_MODULES = ("db.connection", "db.schedules", "db.activities", "database")

_counter = itertools.count()

STATUSES = [
    "success",
    "failed",
    "error",
    "cancelled",
    "skipped",
    "running",
    "queued",
    "pending_retry",
]


@pytest.fixture
def ops(db_backend):
    for _m in _DB_MODULES:
        sys.modules.pop(_m, None)
    from db.schedules import ScheduleOperations

    yield ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())
    for _m in _DB_MODULES:
        sys.modules.pop(_m, None)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _seed(agent: str, rows) -> None:
    """Insert ``rows`` of ``(status, triggered_by)`` for ``agent``."""
    now = datetime.now(timezone.utc)
    for i, (status, trigger) in enumerate(rows):
        _hrun(
            "INSERT INTO schedule_executions (id, schedule_id, agent_name, "
            "status, started_at, duration_ms, triggered_by, message) "
            "VALUES (:i, 'sched-1', :a, :st, :sa, 100, :tb, 'm')",
            i=f"p-{next(_counter)}",
            a=agent,
            st=status,
            sa=_iso(now - timedelta(minutes=i + 1)),
            tb=trigger,
        )


# `triggered_by` is arbitrary text on the way in, but surrogates cannot be
# encoded for the DB driver — excluding them is an *insert-layer* constraint,
# not a weakening of the property (the bucketing logic never sees them).
_TRIGGER_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    max_size=40,
)
_TRIGGERS = st.one_of(
    _TRIGGER_TEXT,
    st.sampled_from(
        [
            "schedule",
            "manual",
            "chat",
            "mcp",
            "telegram",
            "loop",
            "reminder",
            "voip",
            "webhook",
            "fan_out",
            "totally_unknown",
            "",
        ]
    ),
)
_ROWS = st.lists(
    st.tuples(st.sampled_from(STATUSES), _TRIGGERS), min_size=0, max_size=12
)


# ===========================================================================
# P-B1 — Conservation: no execution may vanish from the type breakdown
# ===========================================================================


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(rows=_ROWS)
@example(rows=[("success", "reminder")])  # #1296 bucket (B1 regression)
@example(rows=[("success", "loop")])  # #1150 bucket
@example(rows=[("success", "brand_new_trigger")])  # unmapped -> Other
@example(rows=[("success", "")])  # empty -> Other
def test_p_b1_by_type_conserves_total_executions(ops, rows):
    """P-B1 — ``sum(by_type[*].total) == total_executions``, for ANY multiset of
    arbitrary ``triggered_by`` strings.

    This is the mechanised form of the locked rule *"a new trigger never
    silently vanishes"*. ``by_type`` is assembled by iterating ``_BUCKET_ORDER``,
    so it fails **iff** some bucket reachable from ``_TRIGGER_BUCKETS`` is
    missing from that order list — the exact defect that would let a future
    ``_TRIGGER_BUCKETS`` entry be counted in the total yet dropped from the
    chart, leaving the two numbers quietly inconsistent.

    Random unicode triggers matter here: they all have to land in ``Other``,
    which proves the catch-all is genuinely total rather than merely covering
    the strings someone thought of.
    """
    agent = f"pb1-{next(_counter)}"
    _seed(agent, rows)

    out = ops.get_agent_analytics(agent, 24)

    # Coverage markers — `--hypothesis-show-statistics` then PROVES the
    # interesting partitions were actually reached, instead of leaving "it
    # passed" to hide a run that only ever drew empty row-lists.
    event("rows: empty" if not rows else "rows: non-empty")
    if any(r["bucket"] == "Other" for r in out["by_type"]):
        event("reached the Other catch-all")
    if len(out["by_type"]) > 1:
        event("multiple buckets present")

    assert sum(r["total"] for r in out["by_type"]) == out["total_executions"]
    assert out["total_executions"] == len(rows)
    # `buckets` is the legend; it must agree with the data it labels.
    assert [r["bucket"] for r in out["by_type"]] == out["buckets"]


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(rows=_ROWS)
def test_p_b1b_per_day_stacks_conserve_their_day_total(ops, rows):
    """P-B1b — within every timeline day, the stacked ``by_type`` values sum to
    that day's ``total``.

    The headline breakdown and the per-day stacks are built from the same rows
    by two separate loops, so conservation has to hold twice. A stacked bar
    whose segments do not add up to its own height is the visible symptom.
    """
    agent = f"pb1b-{next(_counter)}"
    _seed(agent, rows)

    out = ops.get_agent_analytics(agent, 24)

    for day in out["timeline"]:
        assert sum(day["by_type"].values()) == day["total"], day
    assert sum(d["total"] for d in out["timeline"]) == out["total_executions"]


# ===========================================================================
# P-B2 — Bounds: rates are real rates, sub-counts never exceed their total
# ===========================================================================


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(rows=_ROWS)
@example(rows=[])  # empty agent
@example(rows=[("running", "schedule")])  # zero-terminal day
@example(rows=[("error", "schedule")])  # legacy failed alias
def test_p_b2_rates_and_counts_stay_in_bounds(ops, rows):
    """P-B2 — ``success_rate`` ∈ [0,1] (or ``None`` per-day), and no sub-count
    can exceed the total it partitions.

    Catches the whole "percentage with no denominator" family — a division that
    escapes its guard yields ``inf``/``nan``/negative, and a count that double-
    tallies (e.g. ``error`` folded into ``failed`` *and* counted separately)
    breaks ``success + failed <= total``.
    """
    agent = f"pb2-{next(_counter)}"
    _seed(agent, rows)

    out = ops.get_agent_analytics(agent, 24)

    terminals = sum(1 for s, _ in rows if s in ("success", "failed", "error"))
    event(f"terminal rows: {'0' if terminals == 0 else 'some'}")
    event(f"non-terminal rows present: {terminals < len(rows)}")

    assert 0.0 <= out["success_rate"] <= 1.0
    assert out["success_count"] >= 0 and out["failed_count"] >= 0
    assert out["success_count"] + out["failed_count"] <= out["total_executions"]

    for day in out["timeline"]:
        rate = day["success_rate"]
        assert rate is None or 0.0 <= rate <= 1.0
        assert day["success"] <= day["total"]
        assert day["failed"] <= day["total"]
        assert day["success"] + day["failed"] <= day["total"]
        # The locked rule: a zero-terminal day is a GAP, never a false 0%.
        if day["success"] + day["failed"] == 0:
            assert rate is None
        else:
            assert rate is not None


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(rows=_ROWS, cap=st.integers(min_value=1, max_value=6))
@example(rows=[("success", "schedule")] * 4, cap=2)  # pool strictly OVER the cap
@example(rows=[("success", "schedule")] * 2, cap=2)  # pool exactly AT the cap
@example(rows=[("running", "schedule")], cap=1)  # empty pool
def test_p_b2b_sampling_metadata_is_self_consistent(ops, monkeypatch, rows, cap):
    """P-B2b — ``sample_size`` never exceeds the cap and never claims more rows
    than exist; ``sampled`` is True **iff** the eligible pool exceeds the cap.

    ``sample_size`` is what tells the UI whether to caption a percentile as
    approximate, so an inconsistent pair mislabels the number's trustworthiness.

    THE CAP IS DRAWN, NOT LEFT AT ITS PRODUCTION VALUE. With
    ``_PERCENTILE_ROWSET_CAP == 5000`` and at most 12 seeded rows, ``eligible >
    cap`` is False for **every** example, so the ``sampled is True`` half of the
    biconditional would pass vacuously — a property that only ever asserts one
    side of an "iff" is a green tick that proves nothing. Shrinking the cap into
    the row range is what makes both arms reachable (the ``@example`` pins keep
    the over/at/empty trio deterministic even if random search misses them).
    """
    import db.schedules.analytics as analytics_mod

    monkeypatch.setattr(analytics_mod, "_PERCENTILE_ROWSET_CAP", cap)

    agent = f"pb2b-{next(_counter)}"
    _seed(agent, rows)
    eligible = sum(1 for status, _ in rows if status == "success")

    out = ops.get_agent_analytics(agent, 24)

    event(f"pool over cap: {eligible > cap}")
    assert out["sample_size"] <= cap
    assert out["sample_size"] <= eligible
    assert out["sampled"] is (eligible > cap)
    if eligible == 0:
        assert out["duration_ms"]["p95"] is None


# ===========================================================================
# P-B3 — Timeline is a contiguous, duplicate-free UTC-day axis
# ===========================================================================


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(hours=st.integers(min_value=1, max_value=1000))
@example(hours=24)
@example(hours=168)  # 7d
@example(hours=336)  # 14d
@example(hours=720)  # 30d
def test_p_b3_timeline_is_a_contiguous_utc_day_axis(ops, hours):
    """P-B3 — for every valid window the timeline is strictly increasing,
    duplicate-free, one-day-stepped, and spans exactly ``[now-hours, now]``.

    A missing day compresses the chart's x-axis and misreports trend slope; a
    duplicate day double-plots. Property-testing the *arithmetic* (rather than
    three hard-coded windows) is what catches an off-by-one that only appears at
    an unusual window or across a month/year boundary.
    """
    agent = f"pb3-{next(_counter)}"

    out = ops.get_agent_analytics(agent, hours)
    dates = [d["date"] for d in out["timeline"]]
    parsed = [datetime.strptime(d, "%Y-%m-%d").date() for d in dates]

    assert parsed, "timeline is never empty"
    assert parsed == sorted(parsed)
    assert len(parsed) == len(set(parsed))
    for earlier, later in zip(parsed, parsed[1:]):
        assert (later - earlier).days == 1

    now = datetime.now(timezone.utc)
    assert parsed[0] == (now - timedelta(hours=hours)).date()
    assert parsed[-1] == now.date()
    assert len(parsed) == (parsed[-1] - parsed[0]).days + 1


# ===========================================================================
# P-B4 — No-crash-total on the pure string leaves
# ===========================================================================


@settings(max_examples=200, deadline=None)
@given(trigger=st.one_of(st.none(), st.text()))
@example(trigger="\x00")
@example(trigger="​")  # zero-width space
@example(trigger="‮")  # RTL override
@example(trigger="x" * 10_000)
def test_p_b4_bucket_for_trigger_is_total(trigger):
    """P-B4a — ``_bucket_for_trigger`` never raises and always returns a bucket
    that the renderer can actually draw.

    Totality is the point: this function sits on the path of every analytics
    read, and ``triggered_by`` is a free-text column. Returning a value outside
    ``_BUCKET_ORDER`` would drop the row from ``by_type`` (see P-B1) rather than
    error, so the assertion checks membership, not merely "returns a string".
    """
    from db.schedules.analytics import _BUCKET_ORDER, _bucket_for_trigger

    result = _bucket_for_trigger(trigger)

    assert isinstance(result, str)
    assert result in _BUCKET_ORDER


@settings(max_examples=200, deadline=None)
@given(message=st.one_of(st.none(), st.text()))
@example(message="\x00\x00")
@example(message="\n\n\n")
@example(message="   ")
@example(message="a" * 80)
@example(message="a" * 81)
@example(message="🚀" * 200)
def test_p_b4_schedule_command_label_is_total_and_bounded(message):
    """P-B4b — ``_schedule_command_label`` never raises, always returns ``str``,
    and never exceeds the 80-char scorecard budget.

    Arbitrary unicode is the realistic input: the message is operator-authored
    free text. The length bound is the load-bearing half — an unbounded label
    breaks the fixed-width scorecard layout, and the truncation arithmetic
    (``[:79] + "…"``) has to land at exactly 80, not 81.
    """
    from db.schedules.analytics import _SCHEDULE_LABEL_MAX, _schedule_command_label

    result = _schedule_command_label(message)

    assert isinstance(result, str)
    assert len(result) <= _SCHEDULE_LABEL_MAX
    assert "\n" not in result and "\r" not in result
    if result:
        assert result == result.strip() or result.endswith("…")
