"""Edge-case matrix (sub-area B) — `db/schedules/analytics.py` (#1771 target 3).

Produced by `/edge-cases` v1.0. Companion files listed in
``test_1771c_schedules_cas_edges.py``; matrix persisted at
``.plan/edge-cases-1771c-matrix.md``.

Subject under test: ``get_agent_analytics`` (#1107), ``get_schedule_analytics``
(#868), ``get_agent_schedules_summary`` (#1115) and the pure leaves
``_bucket_for_trigger`` / ``_schedule_command_label``.

⚠️  THE DATA-SOURCE DISCIPLINE IS LOCKED — a test contradicting it is a WRONG
TEST, not a finding. Quoted verbatim in the ``get_agent_analytics`` docstring:

* counts, per-day type stacks, per-day success-rate, per-day duration AVG and
  per-day context AVG are **full-set** aggregates;
* headline duration ``avg`` and ``context_avg`` are **also full-set** — never
  the capped pool (a sampled average is silently wrong on a busy agent);
* **only** the headline duration ``p95`` uses the newest
  ``_PERCENTILE_ROWSET_CAP`` success rows, with ``sampled=True`` when capped;
* ``success_rate`` is terminal-based (``success / (success + failed)``, where
  failed includes the legacy ``error`` status);
* a day with zero terminal rows reports ``success_rate=None`` — a chart gap, not
  a false 0%;
* ``context_avg`` uses NULL-skipping AVG (unmeasured rows must not read as 0);
* bucketing is UTC-day, gap-filled over a continuous axis;
* ``triggered_by`` is bucketed with an explicit ``Other`` catch-all so a new
  trigger never silently vanishes.

Backend honesty (Invariant #3/#9): SQLite only unless ``TEST_POSTGRES_URL`` is
set. One row is dialect-sensitive and marked **[SQLITE-ONLY]** inline (B9 —
``substr()`` day bucketing on an offset-bearing timestamp); everything else is
standard SQL (``AVG`` NULL-skipping, integer truncation, ``COUNT``).

The three legacy-harness analytics test files (``test_agent_analytics.py``,
``test_schedule_analytics.py``, ``test_1115_schedules_summary.py``) are
deliberately NOT migrated — scope discipline. New tests use ``db_harness``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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

AGENT = "agent-1"
SCHEDULE = "sched-1"


@pytest.fixture
def ops(db_backend):
    for _m in _DB_MODULES:
        sys.modules.pop(_m, None)
    from db.schedules import ScheduleOperations

    _hrun(
        "INSERT INTO agent_schedules (id, agent_name, name, cron_expression, "
        "message, enabled, timezone, owner_id, created_at, updated_at) "
        "VALUES (:i, :a, 'nightly', '0 0 * * *', '/do-the-thing', 1, 'UTC', 1, "
        ":n, :n)",
        i=SCHEDULE,
        a=AGENT,
        n="2026-01-01T00:00:00Z",
    )
    yield ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())
    for _m in _DB_MODULES:
        sys.modules.pop(_m, None)


_seq = iter(range(1_000_000))


def add_execution(
    *,
    status: str = "success",
    triggered_by: str = "schedule",
    started_at: str | None = None,
    duration_ms: int | None = 1000,
    context_used: int | None = None,
    cost: float | None = None,
    tool_calls: str | None = None,
    agent_name: str = AGENT,
    schedule_id: str = SCHEDULE,
) -> str:
    """Insert one execution row through the active engine.

    ``tool_calls`` is passed as a RAW string on purpose: the column is
    agent-written JSON, so the shape guards under test only fire for payloads a
    typed helper would have refused to build.
    """
    if started_at is None:
        started_at = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
    exec_id = f"x-{next(_seq)}"
    _hrun(
        "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, "
        "started_at, duration_ms, context_used, cost, tool_calls, "
        "triggered_by, message) "
        "VALUES (:i, :s, :a, :st, :sa, :d, :c, :co, :tc, :tb, 'm')",
        i=exec_id,
        s=schedule_id,
        a=agent_name,
        st=status,
        sa=started_at,
        d=duration_ms,
        c=context_used,
        co=cost,
        tc=tool_calls,
        tb=triggered_by,
    )
    return exec_id


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ===========================================================================
# B1 — bucket closure: every mapped bucket must be renderable
# ===========================================================================


def test_b1_every_trigger_bucket_is_in_bucket_order(ops):
    """B1 — ``_TRIGGER_BUCKETS.values() ⊆ _BUCKET_ORDER``. REGRESSION GUARD.

    ``by_type`` and every per-day ``by_type`` stack are built by iterating
    ``_BUCKET_ORDER`` — so a bucket that exists in the mapping but is missing
    from the order list has its counts **silently dropped from the chart**
    while still being included in ``total_executions``. The numbers then
    disagree and nothing errors.

    This is not hypothetical: ``_TRIGGER_BUCKETS`` grew ``Loops`` (#1150) and
    ``Reminders`` (#1296) as separate later changes, each of which had to
    remember to touch ``_BUCKET_ORDER`` too. ``test_agent_analytics.py`` asserts
    a hard-coded literal list that OMITS ``"Reminders"``, so it is not a
    completeness check — this is.
    """
    from db.schedules.analytics import _BUCKET_ORDER, _OTHER_BUCKET, _TRIGGER_BUCKETS

    missing = set(_TRIGGER_BUCKETS.values()) - set(_BUCKET_ORDER)
    assert (
        not missing
    ), f"buckets mapped but never rendered (counts vanish from by_type): {missing}"
    assert _OTHER_BUCKET in _BUCKET_ORDER, "the catch-all itself must be renderable"
    assert _BUCKET_ORDER[-1] == _OTHER_BUCKET, "'Other' must sort last in the legend"
    assert len(_BUCKET_ORDER) == len(set(_BUCKET_ORDER)), "duplicate bucket in order"


# ===========================================================================
# B2 — unknown / empty / None triggers fall into "Other" and are never dropped
# ===========================================================================


@pytest.mark.parametrize(
    "trigger, expected_bucket",
    [
        ("schedule", "Scheduled"),
        ("reminder", "Reminders"),
        ("loop", "Loops"),
        ("brand_new_trigger_type", "Other"),
        ("", "Other"),
        (None, "Other"),
        ("SCHEDULE", "Other"),  # case-sensitive by design
        ("  schedule  ", "Other"),  # not trimmed by design
    ],
    ids=[
        "B2-known",
        "B2-reminder",
        "B2-loop",
        "B2-unknown",
        "B2-empty",
        "B2-none",
        "B2-uppercase",
        "B2-padded",
    ],
)
def test_b2_bucket_for_trigger_never_drops_a_value(trigger, expected_bucket):
    """B2 — ``_bucket_for_trigger`` is total: every input maps somewhere.

    The uppercase/padded cases pin that the lookup is an exact-match dict get —
    a value that *looks* known but isn't lands in ``Other`` (visible) rather
    than raising or vanishing.
    """
    from db.schedules.analytics import _bucket_for_trigger

    assert _bucket_for_trigger(trigger) == expected_bucket


def test_b2_unknown_trigger_surfaces_in_by_type(ops):
    """B2b — an unmapped trigger is COUNTED and VISIBLE end-to-end, not dropped."""
    add_execution(triggered_by="a_trigger_from_the_future")
    add_execution(triggered_by="schedule")

    out = ops.get_agent_analytics(AGENT, 168)

    assert out["total_executions"] == 2
    by_bucket = {row["bucket"]: row["total"] for row in out["by_type"]}
    assert by_bucket == {"Scheduled": 1, "Other": 1}
    assert sum(by_bucket.values()) == out["total_executions"]


# ===========================================================================
# B3 / B4 — the "percentage with no denominator" class
# ===========================================================================


def test_b3_zero_terminal_day_reports_none_not_zero(ops):
    """B3 — a day with runs but ZERO terminal runs reports ``success_rate=None``.

    ``running``/``queued`` rows have no verdict yet; reporting 0% would paint a
    healthy agent as totally failing.
    """
    add_execution(status="running")
    add_execution(status="queued")

    out = ops.get_agent_analytics(AGENT, 24)
    populated = [d for d in out["timeline"] if d["total"] > 0]

    assert populated, "expected the seeded rows to land in a timeline day"
    assert sum(d["total"] for d in populated) == 2
    for day in populated:
        assert day["success_rate"] is None
        assert day["success"] == 0
        assert day["failed"] == 0


def test_b4_headline_success_rate_is_zero_on_an_empty_agent_unspecified(ops):
    """B4 — headline ``success_rate`` is ``0.0`` while per-day is ``None``.
    UNSPECIFIED — characterization only.

    The locked discipline mandates ``None`` for *days*; it is **silent** on the
    headline, and the code returns ``0.0`` for a zero-terminal window. So an
    agent that has never run and an agent that failed every run both read
    "0%" at the headline.

    Reported as a spec gap rather than a bug or an xfail: no requirement is
    violated, and changing it to ``None`` is a frontend-visible contract change
    that belongs to a product decision, not to this test-only PR. Pinned so the
    decision — whichever way it goes — is deliberate.
    """
    out = ops.get_agent_analytics("agent-with-no-history", 168)

    assert out["total_executions"] == 0
    assert out["success_rate"] == 0.0  # headline: 0.0
    assert all(d["success_rate"] is None for d in out["timeline"])  # per-day: None


def test_b4b_success_rate_is_terminal_based_not_total_based(ops):
    """B4b — non-terminal rows are excluded from the ``success_rate`` denominator."""
    add_execution(status="success")
    add_execution(status="failed")
    add_execution(status="running")  # no verdict
    add_execution(status="cancelled")  # not a failure

    out = ops.get_agent_analytics(AGENT, 24)

    assert out["total_executions"] == 4
    assert out["success_count"] == 1
    assert out["failed_count"] == 1
    assert out["success_rate"] == 0.5


def test_b4c_legacy_error_status_counts_as_failed(ops):
    """B4c — the legacy ``error`` status is folded into ``failed``.

    Missing this makes a fleet with legacy rows look better than it is.
    """
    add_execution(status="success")
    add_execution(status="error")

    out = ops.get_agent_analytics(AGENT, 24)

    assert out["failed_count"] == 1
    assert out["success_rate"] == 0.5


# ===========================================================================
# B5 — NULL-skipping AVG
# ===========================================================================


def test_b5_context_avg_skips_nulls_and_is_none_when_all_null(ops):
    """B5 — ``context_avg`` averages only measured rows; all-NULL ⇒ ``None``.

    Counting an unmeasured row as 0 would drag the average toward zero and make
    a healthy agent look like it never uses its context window.
    """
    add_execution(context_used=1000)
    add_execution(context_used=3000)
    add_execution(context_used=None)

    out = ops.get_agent_analytics(AGENT, 24)
    assert out["context_avg"] == 2000  # (1000+3000)/2, NOT /3


def test_b5b_all_null_context_reports_none(ops):
    add_execution(context_used=None)
    add_execution(context_used=None)

    out = ops.get_agent_analytics(AGENT, 24)
    assert out["context_avg"] is None


# ===========================================================================
# B6 — the percentile-cap collection boundary
# ===========================================================================


@pytest.mark.parametrize(
    "n_rows, expect_sampled",
    [(2, False), (3, False), (4, True)],
    ids=["B6-below-cap", "B6-at-cap", "B6-above-cap"],
)
def test_b6_sampled_flips_only_strictly_above_the_cap(
    ops, monkeypatch, n_rows, expect_sampled
):
    """B6 — ``sampled`` is True only when rows **exceed** the cap, never at it.

    The implementation fetches ``cap + 1`` rows to detect capping without a
    second COUNT — an off-by-one there would mislabel an exact-cap result as
    sampled (or, worse, a truly-capped one as complete). ``_PERCENTILE_ROWSET_CAP``
    is documented as monkeypatchable precisely for this test.
    """
    import db.schedules.analytics as analytics_mod

    monkeypatch.setattr(analytics_mod, "_PERCENTILE_ROWSET_CAP", 3)
    for _ in range(n_rows):
        add_execution(status="success", duration_ms=1000)

    out = ops.get_agent_analytics(AGENT, 24)

    assert out["sampled"] is expect_sampled
    assert out["sample_size"] == (3 if expect_sampled else n_rows)


def test_b6b_headline_avg_uses_the_full_set_even_when_p95_is_sampled(ops, monkeypatch):
    """B6b — THE locked discipline, mechanised: capping must not touch ``avg``.

    With the cap at 1 the p95 pool sees a single row, but the headline average
    must still be computed over ALL rows. A regression that pointed ``avg`` at
    the capped pool would be invisible on a small dev fleet and badly wrong in
    production — exactly the failure the /autoplan review locked against.
    """
    import db.schedules.analytics as analytics_mod

    monkeypatch.setattr(analytics_mod, "_PERCENTILE_ROWSET_CAP", 1)
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    for i, dur in enumerate((100, 200, 300, 400)):
        add_execution(
            status="success",
            duration_ms=dur,
            started_at=_iso(base + timedelta(minutes=i)),
        )

    out = ops.get_agent_analytics(AGENT, 24)

    assert out["sampled"] is True
    assert out["duration_ms"]["avg"] == 250  # (100+200+300+400)/4 — full set
    assert out["duration_ms"]["p95"] == 400  # newest row only — capped pool


# ===========================================================================
# B7 — duration pool size boundary (0 / 1 / 2 rows)
# ===========================================================================


@pytest.mark.parametrize(
    "durations, expected_p95",
    [([], None), ([500], 500), ([100, 200], 195)],
    ids=["B7-empty", "B7-single", "B7-pair"],
)
def test_b7_percentile_pool_boundaries(ops, durations, expected_p95):
    """B7 — 0 / 1 / 2 success rows must not raise inside ``statistics``.

    ``statistics.quantiles`` raises ``StatisticsError`` on fewer than two data
    points, so the ``>= 2`` / ``== 1`` / else ladder is load-bearing. An
    analytics endpoint that 500s on a brand-new agent is the failure mode.

    The two-row p95 is **195**, not 200: ``method="inclusive"`` *interpolates*
    between the two observations (200 would be the max, i.e. p100). Pinned
    deliberately — the inclusive/exclusive choice is a documented decision in
    the implementation ("matches 'x% of observations were ≤ this value' for
    small N"), and switching methods would silently move every percentile the
    Overview chart renders.
    """
    for dur in durations:
        add_execution(status="success", duration_ms=dur)

    out = ops.get_agent_analytics(AGENT, 24)

    assert out["duration_ms"]["p95"] == expected_p95


def test_b7b_null_durations_are_excluded_from_the_pool(ops):
    """B7b — a success row with NULL ``duration_ms`` never enters the percentile
    pool (``int(None)`` would raise)."""
    add_execution(status="success", duration_ms=None)
    add_execution(status="success", duration_ms=750)

    out = ops.get_agent_analytics(AGENT, 24)

    assert out["duration_ms"]["p95"] == 750
    assert out["sample_size"] == 1


# ===========================================================================
# B9 — day bucketing is a substring, not a timezone conversion  [SQLITE-ONLY]
# ===========================================================================


def test_b9_offset_bearing_started_at_buckets_by_its_literal_prefix(ops):
    """B9 — ``substr(started_at, 1, 10)`` buckets by the string's OWN date.

    **[SQLITE-ONLY]** — asserted through the SQLite ``substr`` path; the
    PostgreSQL rendering of ``func.substr`` is equivalent for TEXT but is not
    exercised without ``TEST_POSTGRES_URL``.

    CHARACTERIZATION, not a bug claim. Day bucketing is documented as UTC-day,
    and it *is* UTC-correct for every value the platform writes — every producer
    goes through ``utc_now_iso()`` / ``to_utc_iso()``, which emit ``Z``. But the
    bucketing is a **string prefix**, not a conversion: an offset-bearing value
    like ``2026-03-02T01:00:00+05:00`` (whose UTC instant is 2026-03-01) would
    be filed under 2026-03-02.

    Pinned because the sibling #1474 work established that naive/offset rows DO
    exist historically, so this is the mechanism by which such a row would land
    in the wrong bucket — worth being explicit about rather than assuming the
    docstring's "UTC-day" claim covers it.

    Dates are derived from *today* rather than hard-coded so the test cannot age
    out of its own window (a fixed 2026-03-01 pair would have needed an
    ever-growing ``hours`` argument, and a 10-year window makes the gap-fill
    loop build ~3650 day-dicts per call).
    """
    # One instant, two spellings. 20:00Z is 01:00 the NEXT day at +05:00, so a
    # UTC-day-correct implementation would file both under the same date.
    instant = (datetime.now(timezone.utc) - timedelta(days=2)).replace(
        hour=20, minute=0, second=0, microsecond=0
    )
    utc_day = instant.date().isoformat()
    offset_spelling = instant.astimezone(timezone(timedelta(hours=5)))
    offset_day = offset_spelling.date().isoformat()
    assert offset_day != utc_day, "fixture must straddle a UTC day boundary"

    add_execution(started_at=_iso(instant), status="success")
    add_execution(
        started_at=offset_spelling.strftime("%Y-%m-%dT%H:%M:%S.%f%z"),
        status="success",
    )

    out = ops.get_agent_analytics(AGENT, 168)
    days = {d["date"]: d["total"] for d in out["timeline"] if d["total"]}

    assert days == {
        utc_day: 1,
        offset_day: 1,
    }, "offset-bearing row bucketed by its literal prefix, not its UTC instant"


# ===========================================================================
# B10 — window boundary is STRICTLY greater-than
# ===========================================================================


def test_b10_window_boundary_is_exclusive(ops, monkeypatch):
    """B10 — the window filter is ``started_at > cutoff``, so a row exactly at
    the cutoff is EXCLUDED while the very next microsecond is included.

    ``iso_cutoff`` is **frozen** here. Computing the cutoff in the test and
    again inside the method yields two strings milliseconds apart, which moves
    the boundary between them and makes a ±1µs assertion a coin-flip (it failed
    exactly that way on the first run). The property under test is the
    strictness of ``>``, not clock arithmetic — freezing is what makes the
    boundary pair meaningful. Invariant #16: the comparison is lexicographic on
    ISO-Z strings, so equal-length microsecond precision is the real boundary.
    """
    import db.schedules.analytics as analytics_mod

    frozen = "2026-03-01T12:00:00.500000Z"
    monkeypatch.setattr(analytics_mod, "iso_cutoff", lambda *a, **k: frozen)

    add_execution(started_at="2026-03-01T12:00:00.499999Z")  # 1µs before
    add_execution(started_at=frozen)  # exactly at
    add_execution(started_at="2026-03-01T12:00:00.500001Z")  # 1µs after

    out = ops.get_agent_analytics(AGENT, 24)

    assert (
        out["total_executions"] == 1
    ), "only the strictly-after row is in window ('>' not '>=')"


# ===========================================================================
# B11 — gap-filled continuous UTC-day timeline
# ===========================================================================


@pytest.mark.parametrize("hours", [24, 168, 336, 720], ids=lambda h: f"B11-{h}h")
def test_b11_timeline_is_contiguous_and_gap_filled(ops, hours):
    """B11 — the timeline is a continuous UTC-day axis with no gaps or dupes.

    A chart that silently omits zero-days compresses the x-axis and misreports
    trend slope. Verifies day-count arithmetic across the real window sizes the
    router accepts (7d/14d/30d) plus 24h.
    """
    add_execution(status="success")

    out = ops.get_agent_analytics(AGENT, hours)
    dates = [d["date"] for d in out["timeline"]]

    assert dates == sorted(dates), "timeline not chronologically ordered"
    assert len(dates) == len(set(dates)), "duplicate day in timeline"

    parsed = [datetime.strptime(d, "%Y-%m-%d").date() for d in dates]
    for earlier, later in zip(parsed, parsed[1:]):
        assert (later - earlier).days == 1, f"gap between {earlier} and {later}"

    today = datetime.now(timezone.utc).date()
    assert parsed[-1] == today
    assert parsed[0] == (datetime.now(timezone.utc) - timedelta(hours=hours)).date()


def test_b11b_zero_days_carry_explicit_empty_values(ops):
    """B11b — a gap-filled day is fully populated with zeros/Nones, never a
    partial dict the frontend would have to guard."""
    out = ops.get_agent_analytics("agent-with-no-history", 168)

    for day in out["timeline"]:
        assert day["total"] == 0
        assert day["success"] == 0
        assert day["failed"] == 0
        assert day["success_rate"] is None
        assert day["duration_avg_ms"] is None
        assert day["context_avg"] is None
        assert day["by_type"] == {}


# ===========================================================================
# B12 — _schedule_command_label string handling
# ===========================================================================


@pytest.mark.parametrize(
    "message, expected",
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("\n\n\n", ""),
        ("\n\n/do-the-thing\nrest", "/do-the-thing"),
        ("   /padded   ", "/padded"),
        ("a" * 80, "a" * 80),  # exactly at the limit
        ("a" * 81, "a" * 79 + "…"),  # one over -> truncate + ellipsis
        ("🚀 ship it", "🚀 ship it"),
        ("é́́ combining", "é́́ combining"),
        ("‮RTL text", "‮RTL text"),
    ],
    ids=[
        "B12-none",
        "B12-empty",
        "B12-spaces",
        "B12-newlines",
        "B12-first-line",
        "B12-padded",
        "B12-at-80",
        "B12-at-81",
        "B12-emoji",
        "B12-combining",
        "B12-rtl",
    ],
)
def test_b12_schedule_command_label(message, expected):
    """B12 — the scorecard headline never raises and never exceeds 80 chars.

    Boundary pair 80/81 pins the truncation arithmetic (``[:79] + "…"`` keeps
    the result at exactly 80). Unicode cases pin that the cut is by **code
    point**, not by grapheme — a combining mark or emoji can be split, which is
    a cosmetic limitation worth documenting rather than a correctness bug.
    """
    from db.schedules.analytics import _schedule_command_label

    result = _schedule_command_label(message)
    assert result == expected
    assert len(result) <= 80


def test_b12b_summary_label_flows_through_for_a_zero_run_schedule(ops):
    """B12b — ``get_agent_schedules_summary`` returns a row (with zeros) for a
    schedule that has never run, carrying its derived command label.

    The zero-run row is the whole point of #1115: a schedule that never fires is
    exactly the one an operator needs to see.
    """
    out = ops.get_agent_schedules_summary(AGENT, 168)

    assert out["schedule_count"] == 1
    row = out["schedules"][0]
    assert row["schedule_id"] == SCHEDULE
    assert row["total_executions"] == 0
    assert row["success_rate"] is None  # no terminal runs -> "—", not 0%
    assert row["avg_duration_ms"] is None
    assert row["command"] == "/do-the-thing"


# ===========================================================================
# B13/B14 — get_schedule_analytics + get_agent_schedules_summary must survive
#           ANY shape of the agent-written `tool_calls` JSON blob
# ===========================================================================
#
# Added at /review (2026-07-26): the /edge-cases matrix flagged these shape
# guards as the one genuinely-residual, un-enumerated coverage gap, and
# `get_schedule_analytics` was named a subject-under-test by the plan (§3) and
# by this file's own docstring while no test actually invoked it. Both are
# closed here.
#
# `tool_calls` is written by the AGENT, so every one of these payloads is
# reachable input, and a regressed guard turns the analytics read into a 500
# rather than a degraded number. The neighbour suites cover only the
# `json.loads` raise ("{not valid json"); the SHAPE guards below — valid JSON
# that is not a list, a list of non-dicts, a dict with no name — were untested
# on both surfaces.

_TOOL_CALL_SHAPES = [
    ("", 0),  # empty string: falsy raw
    ("{not json", 0),  # JSONDecodeError
    ('{"name": "Read"}', 0),  # valid JSON, object not list
    ('"Read"', 0),  # valid JSON, bare string
    ("[1, 2, 3]", 0),  # list of non-dicts
    ('[{"duration_ms": 5}]', 0),  # dict with neither `name` nor `tool`
    ('[{"name": ""}]', 0),  # falsy name
    ('[{"name": "Read"}]', 1),  # counted; no usable duration
    ('[{"tool": "Bash", "duration_ms": "5"}]', 1),  # non-numeric duration
    ('[{"name": "Read"}, "junk", {"tool": "Bash", "duration_ms": 5}]', 2),
]

_TOOL_CALL_IDS = [
    "empty",
    "not-json",
    "object-not-list",
    "string-not-list",
    "list-of-scalars",
    "no-name-key",
    "falsy-name",
    "name-no-duration",
    "non-numeric-duration",
    "mixed-valid-and-junk",
]


@pytest.mark.parametrize("raw, expected_calls", _TOOL_CALL_SHAPES, ids=_TOOL_CALL_IDS)
def test_b13_schedule_analytics_survives_malformed_tool_calls(ops, raw, expected_calls):
    """B13 — ``get_schedule_analytics`` never raises on a malformed
    ``tool_calls`` blob, and counts exactly the well-formed entries.

    The tool-call pool is drawn from ``status='success' AND duration_ms IS NOT
    NULL`` rows, so the seeded row uses the helper defaults deliberately.
    ``total_calls`` counts every entry carrying a name; only entries with a
    positive numeric ``duration_ms`` reach the top-5 ranking — which is why
    ``name-no-duration`` and ``non-numeric-duration`` count but do not rank.
    """
    add_execution(tool_calls=raw)

    out = ops.get_schedule_analytics(SCHEDULE, 24, AGENT)

    assert out is not None
    assert out["tool_calls"]["total_calls"] == expected_calls
    assert all(
        isinstance(t["total_duration_ms"], int) for t in out["tool_calls"]["top"]
    )


def test_b13b_schedule_analytics_top5_ranks_only_positive_numeric_durations(ops):
    """B13b — an entry counts toward ``total_calls`` but only ranks when its
    ``duration_ms`` is numeric AND positive (``0``/negative/str are excluded)."""
    add_execution(
        tool_calls=(
            '[{"name": "Ranked", "duration_ms": 40},'
            ' {"name": "ZeroDur", "duration_ms": 0},'
            ' {"name": "NegDur", "duration_ms": -5},'
            ' {"name": "StrDur", "duration_ms": "40"}]'
        )
    )

    out = ops.get_schedule_analytics(SCHEDULE, 24, AGENT)

    assert out["tool_calls"]["total_calls"] == 4
    assert [t["name"] for t in out["tool_calls"]["top"]] == ["Ranked"]


def test_b13c_schedule_analytics_timeline_counts_failures_and_ignores_the_rest(ops):
    """B13c — the per-schedule timeline's FAILED branch.

    ``get_schedule_analytics`` buckets a day into ``success`` / ``failed`` /
    ``cost``; every other status contributes to ``total_executions`` and to
    ``cost`` but to NEITHER day counter. Previously untested — the whole
    ``failed`` arm of the timeline aggregation had no coverage, so a chart that
    silently stopped drawing failure bars would have shipped green.
    """
    add_execution(status="success", cost=1.0)
    add_execution(status="failed", cost=2.0)
    add_execution(status="cancelled", cost=4.0)
    add_execution(status="running", cost=8.0)

    out = ops.get_schedule_analytics(SCHEDULE, 24, AGENT)
    populated = [d for d in out["timeline"] if d["cost"]]

    assert len(populated) == 1, "all four rows land in the same UTC day"
    day = populated[0]
    assert day["success"] == 1
    assert day["failed"] == 1  # cancelled/running are NOT failures
    assert day["cost"] == 15.0
    assert out["total_executions"] == 4
    assert out["cancelled_count"] == 1


@pytest.mark.parametrize("raw, expected_calls", _TOOL_CALL_SHAPES, ids=_TOOL_CALL_IDS)
def test_b14_schedules_summary_survives_malformed_tool_calls(ops, raw, expected_calls):
    """B14 — the same shape guards on the #1115 summary surface.

    Its ``q_tools`` query pre-filters ``tool_calls IS NOT NULL``, so the
    ``if not raw`` arm is reachable only via an EMPTY string — which is why the
    ``None`` case is deliberately absent from the shared shape table and
    exercised separately below.
    """
    add_execution(tool_calls=raw)

    out = ops.get_agent_schedules_summary(AGENT, 24)

    assert out["schedule_count"] == 1
    assert out["schedules"][0]["tool_call_total"] == expected_calls
    assert out["tool_calls_sampled"] is False


def test_b14b_schedules_summary_null_tool_calls_never_enters_the_loop(ops):
    """B14b — a NULL ``tool_calls`` is filtered out in SQL, not in Python."""
    add_execution(tool_calls=None)

    out = ops.get_agent_schedules_summary(AGENT, 24)

    assert out["schedules"][0]["total_executions"] == 1
    assert out["schedules"][0]["tool_call_total"] == 0


@pytest.mark.parametrize(
    "durations, expected",
    [
        ([], (None, None, None)),
        ([500], (500, 500, 500)),
        ([100, 200], (150, 195, 199)),
        ([100, 200, 300], (200, 290, 298)),
    ],
    ids=["B15-empty", "B15-single", "B15-pair", "B15-triple"],
)
def test_b15_schedule_analytics_percentile_ladder(ops, durations, expected):
    """B15 — the ``>= 2`` / ``== 1`` / ``else`` percentile ladder of
    ``get_schedule_analytics``.

    ``statistics.quantiles`` raises ``StatisticsError`` below two data points,
    so the ladder is load-bearing: a per-schedule analytics read on a brand-new
    or single-run schedule must return ``None``/the single value rather than
    500. Sibling of B7, which pins the same ladder on ``get_agent_analytics`` —
    they are two separate implementations of the identical arithmetic, so
    covering one does not cover the other.

    ``method="inclusive"`` interpolates, which is why the pair yields
    ``p95=195`` rather than the maximum. Pinned so a method switch is visible.
    """
    for dur in durations:
        add_execution(status="success", duration_ms=dur)

    out = ops.get_schedule_analytics(SCHEDULE, 24, AGENT)

    assert (
        out["duration_ms"]["p50"],
        out["duration_ms"]["p95"],
        out["duration_ms"]["p99"],
    ) == expected
    assert out["sampled"] is False
    assert out["sample_size"] == len(durations)
