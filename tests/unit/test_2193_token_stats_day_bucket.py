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
import re
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

# ---------------------------------------------------------------------------
# sys.modules hygiene (#762 lint). The `ops` fixture must evict `db.*` so
# `from db.schedules import ...` re-imports against the harness-bound engine —
# and `monkeypatch.delitem` records NO undo for a key that was absent on entry,
# so the freshly imported, harness-bound module would stay resident for later
# files. That is strictly worse isolation than the explicit pop it would
# replace. Snapshot/restore covers both the present- and absent-on-entry cases,
# which is the leak the lint actually guards against.
# Precedent: tests/unit/test_1771c_schedules_analytics_edges.py.
# ---------------------------------------------------------------------------
_STUBBED_MODULE_NAMES = [*_DB_MODULES]

AGENT = "corbin"
SCHEDULE = "sched-2193"


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot the evicted modules before each test and restore after.

    Purely a teardown-time guarantee: the snapshot is taken before the test
    body, so no in-test behaviour (and no assertion) changes.
    """
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
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


def _slot(stats, when: datetime) -> dict:
    """The axis bucket a given instant belongs to.

    Tests must NOT assume "a row 2h old is in the last slot". Run at 00:30 UTC
    that row belongs to YESTERDAY, and the assertion fails for ~8% of clock
    positions — a flake that would land in CI, not here. Looking the bucket up
    by its own UTC date is correct at every hour.
    """
    key = when.strftime("%Y-%m-%d")
    match = [d for d in stats["daily_breakdown"] if d["date"] == key]
    assert match, f"{key} is outside the 7-day axis {[d['date'] for d in stats['daily_breakdown']]}"
    return match[0]


# ===========================================================================
# The CI-effective guards — backend-independent
# ===========================================================================


class TestNoDateFunctionInDayBucket:
    """No SQL date function may bucket an ISO-Z TEXT column in this package.

    This is the guard that actually protects the class on SQLite-only CI. The
    behavioural cases below all PASS against the broken query on SQLite, so
    without this a reintroduction ships green.

    It sweeps the whole `db/schedules/` package rather than the one accessor:
    scoping it to `get_agent_token_stats` would protect the instance, not the
    class, and #250 surviving #1540 is precisely what a per-accessor guard
    fails to prevent.
    """

    # `\b` matters: `update(` ends in "date(" and would otherwise match.
    _BANNED = re.compile(
        r"\b(?:date|to_date|date_trunc|datetime|strftime)\s*\(|::\s*date\b",
        re.IGNORECASE,
    )

    def _package_dir(self) -> Path:
        import db.schedules

        return Path(db.schedules.__file__).parent

    def _sql_literals(self, path: Path) -> list[tuple[int, str]]:
        """Every string literal in a module, docstrings and comments excluded.

        Deliberately NOT a grep over source text. The first draft was, and it
        failed against its own fix — the comment explaining why
        `DATE(started_at)` was wrong contains the banned token. A guard that
        fires on its own rationale is one nobody keeps, so this reads what the
        database actually receives.
        """
        tree = ast.parse(path.read_text())
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            )
            and ast.get_docstring(node) is not None
        }
        return [
            (node.lineno, node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ]

    def test_no_sql_date_function_anywhere_in_db_schedules(self):
        """The class-level guard. A new accessor inherits it for free."""
        offenders = [
            f"{path.name}:{lineno}: {m.group(0)!r}"
            for path in sorted(self._package_dir().glob("*.py"))
            for lineno, literal in self._sql_literals(path)
            for m in [self._BANNED.search(literal)]
            if m
        ]
        assert not offenders, (
            "SQL date functions are dialect-dependent on an ISO-Z TEXT column "
            "(Invariant #16) — bucket with substr(). See #2193.\n  "
            + "\n  ".join(offenders)
        )

    def test_the_token_stats_day_bucket_is_the_shared_idiom(self):
        """Pinned to `substr(started_at, 1, 10)` — the expression
        `get_agent_analytics` uses, so the two day series cannot drift.

        Deliberately NOT pinned to a `replace(...)` variant. An earlier draft
        was, having copied ent#326's HOUR bucket rationale; for a 10-char slice
        the separator sits outside the window, so `replace` is a provable no-op
        and the guard rejected the codebase's own idiom with a false reason.
        """
        from db.schedules.stats import ScheduleStatsMixin

        src = textwrap.dedent(
            inspect.getsource(ScheduleStatsMixin.get_agent_token_stats)
        )
        tree = ast.parse(src)
        fn = tree.body[0]
        body = fn.body[1:] if ast.get_docstring(fn) is not None else fn.body
        sql = "\n".join(
            node.value.lower()
            for stmt in body
            for node in ast.walk(stmt)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
        assert sql.count("substr(started_at, 1, 10)") == 2, (
            "the day bucket must appear in both SELECT and GROUP BY, spelled "
            "exactly as get_agent_analytics spells it (#2193)"
        )
        assert "replace(started_at" not in sql, (
            "a 10-char slice never spans the separator — `replace` here is a "
            "no-op that diverges this expression from the shared idiom (#2193)"
        )

    def test_the_guard_reads_sql_not_prose(self):
        """Meta-check on the extractor, asserted against a SYNTHETIC module.

        An earlier draft asserted that the real accessor's comment still
        contained the banned token, which coupled the suite to the wording of
        an explanation — rewording it, changing nothing, went red.
        """
        import tempfile

        source = textwrap.dedent(
            '''
            def f():
                """DATE(started_at) is banned — this docstring must be ignored."""
                # DATE(started_at) in a comment must be ignored too
                return "SELECT substr(started_at, 1, 10) FROM t"
            '''
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write(source)
            tmp = Path(fh.name)
        try:
            literals = "\n".join(v for _, v in self._sql_literals(tmp))
        finally:
            tmp.unlink()

        assert "substr(started_at" in literals, "real SQL must be extracted"
        assert not self._BANNED.search(literals), (
            "a banned token in a comment or docstring must not reach the guard"
        )

    def test_the_banned_pattern_catches_the_evasions(self):
        """Table alias, whitespace and CAST forms — the shapes a literal
        prefix match (`"date(started_at" in sql`) would miss."""
        for evasion in (
            "GROUP BY DATE(started_at)",
            "GROUP BY DATE(e.started_at)",
            "GROUP BY DATE( started_at )",
            "GROUP BY date_trunc('day', started_at)",
            "GROUP BY started_at::date",
            "GROUP BY to_date(started_at, 'YYYY-MM-DD')",
        ):
            assert self._BANNED.search(evasion), evasion
        assert not self._BANNED.search("GROUP BY substr(started_at, 1, 10)")
        assert not self._BANNED.search("UPDATE (x)"), "`update(` is not `date(`"


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
        placed = {
            now - timedelta(hours=2): 100.0,
            now - timedelta(days=1): 40.0,
            now - timedelta(days=3): 25.0,
        }
        for when, cost in placed.items():
            add_execution(started_at=_iso(when), cost=cost)

        stats = ops.get_agent_token_stats(AGENT)

        assert len(stats["daily_breakdown"]) == 7
        assert any(c > 0 for c in _costs(stats)), (
            "every bucket zero while the scalars are non-zero is exactly the "
            "flat-sparkline failure (#2193)"
        )
        # Each cost lands in the bucket for its OWN date. Summed per date so a
        # run started within 2h of UTC midnight — where `now-2h` and `now-1d`
        # share a bucket — asserts the true total rather than flaking.
        expected: dict = {}
        for when, cost in placed.items():
            expected[when.strftime("%Y-%m-%d")] = (
                expected.get(when.strftime("%Y-%m-%d"), 0.0) + cost
            )
        for key, total in expected.items():
            assert _slot(stats, datetime.strptime(key, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            ))["cost"] == pytest.approx(total)

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
        so losing it from the series would be the chart contradicting the
        number above it, by a second route.

        ⚠️  HONEST SCOPE — this case does NOT discriminate `substr` variants.
        The separator sits at position 11, outside a 10-char slice, so it
        passes with or without a `replace(...)`. An earlier draft billed it as
        the coverage for that `replace`, which was a vacuous claim: the test
        could not fail when its supposed subject was deleted. It IS a real
        case for the query as a whole — the legacy shape must not be dropped,
        mis-bucketed, or crash a date parse — and `TestDayBucketKey` plus the
        hour-bucket note in the accessor carry the separator reasoning.
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
        assert _slot(stats, yesterday)["cost"] == pytest.approx(9.0)

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
        when = datetime.now(timezone.utc) - timedelta(hours=1)
        add_execution(started_at=_iso(when), cost=None, context_used=None)
        today = _slot(ops.get_agent_token_stats(AGENT), when)

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
