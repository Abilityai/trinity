"""Agent-header 7d cost sparkline — dialect-agnostic day bucketing (#2193).

Subject under test: ``db/schedules/stats.py::get_agent_token_stats`` (#250) and
its leaf ``_day_bucket_key``.

The bug: the per-day series was bucketed with ``DATE(started_at)`` on an ISO-Z
**TEXT** column. On SQLite that yields TEXT; on PostgreSQL ``date(x)`` is the
type-name-as-function cast, so psycopg returns a ``datetime.date``. The
gap-filled axis is keyed by ``strftime("%Y-%m-%d")`` strings, and a ``str`` vs
``date`` dict lookup does not raise — it MISSES. Every bucket read as a
legitimate zero, so the sparkline went permanently flat while ``cost_24h`` /
``cost_7d`` / ``lifetime_*`` (a different query, no date function) stayed
correct. Nothing errored on either backend.

⚠️  BACKEND HONESTY (Invariant #3/#9). ``db_backend`` yields **SQLite only**
unless ``TEST_POSTGRES_URL`` is set, and no CI workflow sets it. So the
behavioural cases below CANNOT catch a reintroduction of this bug in CI — on
SQLite the broken query passes all of them. That is why this file leads with
two backend-independent guards that DO run everywhere:

* ``TestNoDateFunctionInDayBucket`` — a static guard on the accessor's SQL.
  It is the actual CI protection for the class.
* ``TestDayBucketKey``             — the pure normalizer, exercised with the
  ``datetime.date`` a PostgreSQL driver hands back.

The behavioural cases are the proof on a real engine; they were run against
PostgreSQL 16 by hand and fail on the pre-fix query there.
"""

from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: make src/backend importable (see test_1771c_* for the sys.path
# rationale; `tests/` is auto-added by pytest and must not win).
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun  # noqa: E402,F401

_DB_MODULES = ("db.connection", "db.schedules", "db.activities", "database")

AGENT = "corbin"
SCHEDULE = "sched-2193"


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _DB_MODULES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


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


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def add_execution(
    *,
    started_at: str,
    cost: float | None = None,
    context_used: int | None = None,
    status: str = "success",
    agent_name: str = AGENT,
) -> str:
    """Insert one execution row through the active engine.

    ``started_at`` is passed as a RAW string on purpose — the point of several
    cases below is a timestamp SHAPE the current writers no longer emit but
    that is still on disk (pre-#1474 scheduler rows).
    """
    exec_id = f"x2193-{next(_seq)}"
    _hrun(
        "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, "
        "started_at, cost, context_used, triggered_by, message) "
        "VALUES (:i, :s, :a, :st, :sa, :c, :cu, 'schedule', 'm')",
        i=exec_id,
        s=SCHEDULE,
        a=agent_name,
        st=status,
        sa=started_at,
        c=cost,
        cu=context_used,
    )
    return exec_id


def _costs(stats) -> list[float]:
    return [d["cost"] for d in stats["daily_breakdown"]]


# ===========================================================================
# The CI-effective guards — backend-independent
# ===========================================================================


class TestNoDateFunctionInDayBucket:
    """The accessor must bucket by substring, never a SQL date function.

    This is the guard that actually protects the class on SQLite-only CI. The
    behavioural cases below all PASS against the broken query on SQLite, so
    without this a reintroduction ships green.
    """

    def _source(self) -> str:
        from db.schedules.stats import ScheduleStatsMixin

        return textwrap.dedent(
            inspect.getsource(ScheduleStatsMixin.get_agent_token_stats)
        )

    def _sql_literals(self) -> str:
        """Every SQL string the accessor issues, comments and docstring excluded.

        Deliberately NOT a grep over the function source. The first draft was,
        and it failed against the fix — the comment explaining *why*
        `DATE(started_at)` was wrong contains the banned token. A prose guard
        that fires on its own rationale is a guard nobody can keep, so this
        reads the literals the database actually receives.
        """
        tree = ast.parse(self._source())
        fn = tree.body[0]
        body = fn.body[1:] if ast.get_docstring(fn) is not None else fn.body
        return "\n".join(
            node.value.lower()
            for stmt in body
            for node in ast.walk(stmt)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )

    def test_day_bucket_uses_substr_not_a_date_function(self):
        """`DATE(...)`/`to_date`/`::date`/`date_trunc` are dialect-dependent here."""
        sql = self._sql_literals()
        for banned in ("date(started_at", "to_date(", "::date", "date_trunc("):
            assert banned not in sql, (
                f"{banned!r} is dialect-dependent on an ISO-Z TEXT column "
                "(Invariant #16) — bucket with substr(). See #2193."
            )
        assert "substr(replace(started_at" in sql, (
            "the day bucket must slice the stored UTC string; `replace` keeps "
            "pre-#1474 space-separator rows in their bucket (#2193)"
        )

    def test_the_guard_reads_sql_not_prose(self):
        """Meta-check: the extractor must ignore comments.

        Without this, a later edit that "fixes" the guard by dropping the AST
        walk for a cheap grep passes silently until someone documents the bug
        again — which is how the first draft of this file broke.
        """
        assert "date(started_at" in self._source().lower(), (
            "the accessor no longer explains the #2193 failure — if the "
            "comment was deliberately removed, delete this meta-check too"
        )
        assert "date(started_at" not in self._sql_literals()

    def test_the_join_key_is_normalized_not_trusted(self):
        """The Python side must not key `raw_days` on the raw column value.

        A `str`/`date` mismatch MISSES rather than raising, so the failure is
        indistinguishable from real zeros. AST-checked so a comment mentioning
        the helper cannot satisfy it.
        """
        tree = ast.parse(self._source())
        called = {
            n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "_day_bucket_key" in called, (
            "get_agent_token_stats must normalize the SQL day bucket before "
            "joining it to the strftime axis (#2193)"
        )


class TestDayBucketKey:
    """The normalizer — the half a SQLite-only CI can exercise directly."""

    def test_text_bucket_passes_through(self):
        from db.schedules.stats import _day_bucket_key

        assert _day_bucket_key("2026-08-14") == "2026-08-14"

    def test_postgres_date_object_normalizes_to_the_axis_key(self):
        """THE regression. This is what psycopg returns for `date(text)`."""
        from db.schedules.stats import _day_bucket_key

        assert _day_bucket_key(date(2026, 8, 14)) == "2026-08-14"

    def test_datetime_object_normalizes_too(self):
        from db.schedules.stats import _day_bucket_key

        assert _day_bucket_key(datetime(2026, 8, 14, 20, 30, 54)) == "2026-08-14"

    def test_axis_key_and_normalized_key_agree(self):
        """Pinned against the axis generator itself, not a literal.

        The two are only useful if they produce the SAME text; asserting a
        hard-coded string on each side would pass even if the axis format
        changed.
        """
        from db.schedules.stats import _day_bucket_key

        now = datetime.now(timezone.utc)
        assert _day_bucket_key(now.date()) == now.strftime("%Y-%m-%d")

    def test_unexpected_type_degrades_to_a_non_matching_string(self):
        """Never unhashable, never a type-based silent miss."""
        from db.schedules.stats import _day_bucket_key

        key = _day_bucket_key(None)
        assert isinstance(key, str)
        assert key != datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ===========================================================================
# Behavioural — real engine (SQLite always; PostgreSQL with TEST_POSTGRES_URL)
# ===========================================================================


class TestDailyBreakdown:
    def test_costs_land_on_their_own_days(self, ops):
        """The sparkline's actual payload. Flat-all-zeros was the bug."""
        now = datetime.now(timezone.utc)
        add_execution(started_at=_iso(now - timedelta(hours=2)), cost=100.0)
        add_execution(started_at=_iso(now - timedelta(days=1)), cost=40.0)
        add_execution(started_at=_iso(now - timedelta(days=3)), cost=25.0)

        stats = ops.get_agent_token_stats(AGENT)
        costs = _costs(stats)

        assert len(costs) == 7
        assert any(c > 0 for c in costs), (
            "every bucket zero while the scalars are non-zero is exactly the "
            "flat-sparkline failure (#2193)"
        )
        assert costs[-1] == pytest.approx(100.0)   # today
        assert costs[-2] == pytest.approx(40.0)    # yesterday
        assert costs[-4] == pytest.approx(25.0)    # three days back

    def test_breakdown_reconciles_with_the_scalar_beside_it(self, ops):
        """The header renders both; disagreement is the visible symptom.

        Scoped to rows inside the 7-CALENDAR-day axis so the rolling-168h
        `cost_7d` window and the axis cover the same set — the pre-existing
        partial-8th-day edge is out of scope here.
        """
        now = datetime.now(timezone.utc)
        for days_ago, cost in ((0, 12.5), (1, 7.25), (2, 3.0), (5, 0.5)):
            add_execution(
                started_at=_iso(now - timedelta(days=days_ago, hours=1)),
                cost=cost,
            )

        stats = ops.get_agent_token_stats(AGENT)
        assert sum(_costs(stats)) == pytest.approx(stats["cost_7d"])

    def test_axis_dates_are_plain_iso_days_in_order(self, ops):
        """The frontend maps this array positionally; a date object would
        also serialize, so shape is asserted, not just truthiness."""
        add_execution(
            started_at=_iso(datetime.now(timezone.utc) - timedelta(hours=1)),
            cost=1.0,
        )
        days = [d["date"] for d in ops.get_agent_token_stats(AGENT)["daily_breakdown"]]

        assert days == sorted(days)
        for d in days:
            assert isinstance(d, str)
            datetime.strptime(d, "%Y-%m-%d")  # raises if the format drifts

    def test_pre_1474_space_separator_row_is_bucketed_not_dropped(self, ops):
        """`2026-08-14 20:30:54` still on disk from the old scheduler.

        It clears the lexicographic cutoff and IS counted by the scalar query,
        so a bucket key of `2026-08-14 ` would drop it from the chart alone —
        the chart contradicting the number above it, by a second route.
        """
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        add_execution(
            started_at=yesterday.strftime("%Y-%m-%d %H:%M:%S"), cost=9.0
        )

        stats = ops.get_agent_token_stats(AGENT)
        assert stats["cost_7d"] == pytest.approx(9.0)
        assert sum(_costs(stats)) == pytest.approx(9.0), (
            "the legacy-shaped row was counted by the scalar query but lost "
            "from the series (#2193)"
        )
        assert _costs(stats)[-2] == pytest.approx(9.0)

    def test_gaps_are_real_zeros_not_missing_entries(self, ops):
        """A skip and a zero read identically in a sparkline, so the axis is
        always 7 continuous days (#1107 convention)."""
        add_execution(
            started_at=_iso(datetime.now(timezone.utc) - timedelta(days=4)),
            cost=6.0,
        )
        breakdown = ops.get_agent_token_stats(AGENT)["daily_breakdown"]

        assert len(breakdown) == 7
        assert all(
            set(d) == {"date", "cost", "context_tokens", "executions"}
            for d in breakdown
        )
        assert sum(1 for d in breakdown if d["cost"] == 0) == 6

    def test_only_this_agents_rows_count(self, ops):
        """`agent_name` is the tenant boundary of the header strip."""
        now = datetime.now(timezone.utc)
        add_execution(started_at=_iso(now - timedelta(hours=1)), cost=5.0)
        add_execution(
            started_at=_iso(now - timedelta(hours=1)),
            cost=999.0,
            agent_name="someone-else",
        )

        assert sum(_costs(ops.get_agent_token_stats(AGENT))) == pytest.approx(5.0)

    def test_non_terminal_rows_are_excluded_from_the_series(self, ops):
        """`running`/`queued` carry no settled cost; the scalar query filters
        them and the series must agree."""
        now = datetime.now(timezone.utc)
        add_execution(started_at=_iso(now - timedelta(hours=1)), cost=4.0)
        add_execution(
            started_at=_iso(now - timedelta(hours=1)), cost=77.0, status="running"
        )

        stats = ops.get_agent_token_stats(AGENT)
        assert sum(_costs(stats)) == pytest.approx(4.0)
        assert stats["cost_7d"] == pytest.approx(4.0)

    def test_null_cost_rows_count_as_executions_not_as_cost(self, ops):
        """An unmeasured run must not read as $0 spend *or* vanish."""
        add_execution(
            started_at=_iso(datetime.now(timezone.utc) - timedelta(hours=1)),
            cost=None,
            context_used=None,
        )
        today = ops.get_agent_token_stats(AGENT)["daily_breakdown"][-1]

        assert today["executions"] == 1
        assert today["cost"] == 0
        assert today["context_tokens"] == 0

    def test_agent_with_no_rows_returns_a_full_zero_axis(self, ops):
        """The empty state must still be renderable (the header hides the row
        on `lifetime_executions == 0`, but the payload must not be ragged)."""
        stats = ops.get_agent_token_stats(AGENT)

        assert stats["lifetime_executions"] == 0
        assert len(stats["daily_breakdown"]) == 7
        assert sum(_costs(stats)) == 0
