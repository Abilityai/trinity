"""#1832 — a clock-skewed ``started_at`` must never persist a negative ``duration_ms``.

``started_at`` and ``completed_at`` are written by **different processes** (the
backend runs ``--workers 2``; the standalone ``src/scheduler`` container is a
separate image), so an unguarded ``completed_at - started_at`` can go negative
whenever the finalizing process's clock trails the one that opened the row. The
value then flowed unguarded into ``get_agent_analytics`` and out to the Agent
Detail Overview chart. Canary G-03 *detects* the skew at severity ``minor`` but
prevents nothing.

The fix clamps at the **write**, in all three writers, so no reader has to know:

* ``src/backend/db/schedules/executions.py`` — ``update_execution_status``
* ``src/scheduler/database.py`` — the ``schedule_executions`` finalizer
* ``src/scheduler/database.py`` — the ``process_schedule_executions`` finalizer

The issue named the first two; the third is the same unguarded subtraction on
the process-execution table and is fixed here too, which is why this file guards
the invariant at **source level** as well as behaviourally — the two scheduler
writers live in a separate image that these unit tests cannot import, and a
fourth call-site added later would otherwise reintroduce the bug silently.

Trade-off recorded deliberately: a skewed row clamps to exactly ``0``, which
makes it indistinguishable from a genuine sub-millisecond execution. Preserving
the distinction would need a separate column or a sentinel, which is a schema
change well beyond a p2 reliability fix.

Companion coverage lives in ``test_1771c_schedules_cas_edges.py`` (A6), where the
original ``strict=True`` xfail was retired by this fix.

#2434 extends this guard to the OTHER end of the same range, and to the sites it
could not see:

* ``max(0, …)`` is no longer an accepted shape. It guards only the low end —
  ``max(0, 2_850_000_000)`` is still 2.85 bn, which the PostgreSQL ``INTEGER``
  column cannot hold — so every measured writer must call
  ``utils.helpers.duration_ms_between`` (mirrored in ``src/scheduler/utils.py``),
  which guards BOTH ends.
* The scanner now sees ``ast.keyword`` sinks (``.values(duration_ms=…)``), not
  only ``ast.Assign``. That is the actual sink at all six watchdog-sweep sites in
  ``db/schedules/cleanup.py`` and ``db/activities.py``, which are therefore added
  to ``_WRITERS``.
* Acceptance is classified per site rather than blanket-accepting ``None``:
  fabricated → ``None``, skipped → literal ``0``, measured → the helper. A
  blanket rule would let a measured writer silently discard a real datum.

The behavioural half of #2434 lives in ``test_2434_duration_overflow.py``, which
needs real PostgreSQL — SQLite stores an 8-byte int and cannot fail.
"""

from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: mirror test_1771c_schedules_cas_edges.py so `src/backend` imports
# resolve and `tests/utils/` does not shadow `src/backend/utils/`.
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_ROOT = _THIS.parent.parent.parent
_BACKEND = _ROOT / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
# #2080: the shadow-eviction loop that used to sit here is GONE. It popped
# `utils` (and the test-helper submodules) from sys.modules to defeat
# `tests/utils` shadowing `src/backend/utils`. That package is now
# `tests/testkit`, so `utils` IS the backend package — and popping it
# evicted the canonical module mid-session, leaving anything that had
# already imported it holding a stale reference (observed as
# `ImportError: module services.subscription_auto_switch not in sys.modules`
# from an importlib.reload several hundred tests later).
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun, scalar as _hscalar  # noqa: E402,F401

_DB_MODULES = ("db.connection", "db.schedules", "db.activities", "database")
_STUBBED_MODULE_NAMES = [
    "utils",
    "utils.api_client",
    "utils.assertions",
    "utils.cleanup",
    *_DB_MODULES,
]

AGENT = "agent-1832"


@pytest.fixture(autouse=True)
def _restore_sys_modules():
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
    """Composed ``ScheduleOperations`` bound to a fresh production schema."""
    for _m in _DB_MODULES:
        sys.modules.pop(_m, None)
    from db.schedules import ScheduleOperations

    yield ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())
    for _m in _DB_MODULES:
        sys.modules.pop(_m, None)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _insert(exec_id: str, *, started_at: str, status: str = "running") -> str:
    cols = {
        "id": exec_id,
        "schedule_id": "sched-1832",
        "agent_name": AGENT,
        "status": status,
        "started_at": started_at,
        "message": "do the thing",
        "triggered_by": "schedule",
    }
    names = ", ".join(cols)
    binds = ", ".join(f":{k}" for k in cols)
    _hrun(f"INSERT INTO schedule_executions ({names}) VALUES ({binds})", **cols)
    return exec_id


def _duration(exec_id: str):
    return _hscalar(
        "SELECT duration_ms FROM schedule_executions WHERE id = :i", i=exec_id
    )


# ---------------------------------------------------------------------------
# Behaviour — the reported path
# ---------------------------------------------------------------------------


def test_1832_future_started_at_clamps_to_zero(ops):
    """The reported reproduction: a row opened 300s in the future."""
    future = _iso(datetime.now(timezone.utc) + timedelta(seconds=300))
    eid = _insert("c1832-skew", started_at=future)

    assert ops.update_execution_status(eid, "success", response="ok") is True

    assert _duration(eid) == 0


def test_1832_normal_duration_is_untouched(ops):
    """The clamp must not flatten real durations — only negative ones."""
    past = _iso(datetime.now(timezone.utc) - timedelta(seconds=5))
    eid = _insert("c1832-normal", started_at=past)

    assert ops.update_execution_status(eid, "success", response="ok") is True

    duration = _duration(eid)
    assert 4_000 <= duration <= 6_000, duration


@pytest.mark.parametrize(
    "terminal_status",
    ["success", "failed", "error"],
    ids=["to-success", "to-failed", "to-error"],
)
def test_1832_clamp_applies_to_every_terminal_transition(ops, terminal_status):
    """The subtraction is shared by all terminal writes, not just success."""
    future = _iso(datetime.now(timezone.utc) + timedelta(seconds=300))
    eid = _insert(f"c1832-{terminal_status}", started_at=future)

    assert ops.update_execution_status(eid, terminal_status) is True

    assert _duration(eid) == 0


# ---------------------------------------------------------------------------
# Behaviour — the consumption path the bug actually reached
# ---------------------------------------------------------------------------


def test_1832_analytics_never_reports_a_negative_duration(ops):
    """A skewed row must not poison the Overview chart's avg/p95."""
    future = _iso(datetime.now(timezone.utc) + timedelta(seconds=300))
    eid = _insert("c1832-analytics", started_at=future)
    assert ops.update_execution_status(eid, "success", response="ok") is True

    out = ops.get_agent_analytics(AGENT, 168)

    duration = out["duration_ms"]
    assert duration["avg"] >= 0, duration
    assert duration["p95"] >= 0, duration


# ---------------------------------------------------------------------------
# Source guard — every writer, including the two this suite cannot import
# ---------------------------------------------------------------------------

_WRITERS = (
    _ROOT / "src" / "backend" / "db" / "schedules" / "executions.py",
    _ROOT / "src" / "scheduler" / "database.py",
    # #2434: the six fabrication sites. Their sink is an ``ast.keyword`` —
    # ``.values(duration_ms=...)`` — not an ``ast.Assign``, which is exactly the
    # blind spot ``_duration_expressions`` below now closes.
    _ROOT / "src" / "backend" / "db" / "schedules" / "cleanup.py",
    _ROOT / "src" / "backend" / "db" / "activities.py",
)


def _duration_expressions(path: Path):
    """Yield ``(lineno, value_node)`` for every expression bound to duration_ms.

    Two sinks, not one (#2434):

    * ``ast.Assign`` — ``duration_ms = <expr>``, the local the writers compute.
    * ``ast.keyword`` — ``.values(duration_ms=<expr>)`` / ``foo(duration_ms=…)``,
      which is where the value actually reaches the column at every site in
      ``cleanup.py`` and ``activities.py``.

    Scanning only ``Assign`` made the guard satisfiable by a redundant
    ``duration_ms = None`` local whose sole purpose is to feed a scanner — a line
    the next reviewer or formatter inlines, silently neutering the guard on all
    six sites at once. Pass-through keyword values are skipped — see
    ``_is_pass_through`` for the two shapes and why the skip is kept narrow.
    """
    yield from _duration_expressions_from_source(path.read_text(encoding="utf-8"))


def _duration_expressions_from_source(source: str):
    """``_duration_expressions`` over a source string — the guard's own unit."""
    yield from _scan_scope(ast.parse(source), None)


def _scan_scope(node: ast.AST, func_name: "str | None"):
    """Walk ``node``, carrying the name of the nearest enclosing function.

    The name is what makes acceptance *per site* rather than per shape: only the
    six sweeps that FABRICATE an end time may persist ``None`` (see
    ``_FABRICATION_SITES``). A flat ``ast.walk`` cannot say which function a sink
    sits in, and ``db/activities.py`` holds a fabricator and the measured
    ``complete_activity`` in the same file — so file granularity would let a
    measured writer drop a real datum and still pass.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from _scan_scope(child, child.name)
            continue
        if isinstance(child, ast.Assign):
            for target in child.targets:
                if isinstance(target, ast.Name) and target.id == "duration_ms":
                    yield child.lineno, child.value, func_name
        elif isinstance(child, ast.keyword) and child.arg == "duration_ms":
            if not _is_pass_through(child.value):
                yield child.value.lineno, child.value, func_name
        yield from _scan_scope(child, func_name)


# The six watchdog sweeps that INVENT a row's end time. They — and only they —
# may persist ``None``. Everything else that binds duration_ms is a measurement
# and must route through ``duration_ms_between`` (or write the literal 0 that a
# skipped row legitimately ran for). Blanket-accepting ``None`` everywhere would
# let a future edit to ``update_execution_status`` or either scheduler finalizer
# silently discard a real number and still pass this guard.
_FABRICATION_SITES = frozenset({
    "mark_stale_executions_failed",
    "mark_no_session_executions_failed",
    "fail_stale_slot_execution",
    "mark_execution_failed_by_watchdog",
    "close_open_activities_for_executions",
    "mark_stale_activities_failed",
})


def _is_pass_through(value: ast.AST) -> bool:
    """True for a keyword value that moves an existing duration, never computes one.

    Exactly two shapes, kept deliberately narrow so the guard stays fail-CLOSED
    (anything it does not recognise must satisfy ``_is_guarded``):

    * ``duration_ms=duration_ms`` — the local already judged at its ``Assign``.
      Demanding the guard shape here too would flag the sink of a correctly
      guarded writer. **Only that exact name.** ``_duration_expressions`` judges
      an ``Assign`` only when its target is literally ``duration_ms``, so
      skipping any ``ast.Name`` would let ``elapsed = int((c - s)…)`` followed by
      ``.values(duration_ms=elapsed)`` past the guard entirely — a shape the
      codebase already writes elsewhere (``fan_out_service`` builds its result
      that way).
    * ``duration_ms=row["duration_ms"]`` — reading the column straight back out
      of a row mapping to build a model (``_row_to_execution`` and friends).
      That is a READ, not a writer, and it cannot produce a value the column did
      not already hold. Only the identical key is skipped: ``row["anything_else"]``
      still has to satisfy the guard.
    """
    if isinstance(value, ast.Name):
        return value.id == "duration_ms"
    return (
        isinstance(value, ast.Subscript)
        and isinstance(value.slice, ast.Constant)
        and value.slice.value == "duration_ms"
    )


def _is_guarded(value: ast.AST, *, allow_none: bool = True) -> bool:
    """True when the expression is one of the three accepted shapes.

    Acceptance is classified by shape AND by site, deliberately NOT a blanket
    "anything constant is fine" (#2434):

    * ``None`` — a **fabricated** duration. The sweep invented the end time, so
      there is nothing to record. Legitimate ONLY at the six sweep sites, which
      is what ``allow_none`` carries: at a measured writer, ``None`` is a
      discarded measurement, not a guard.
    * literal ``0`` — a **skipped** row: it ran for zero time, which IS a
      measurement.
    * ``duration_ms_between(...)`` — a **measured** writer, guarded at both ends
      (``max(0, …)`` low per #1832, ``None`` above the int4 ceiling per #2434).

    A bare ``max(...)`` is NO LONGER accepted: it guards only the low end, and
    ``max(0, 2_850_000_000)`` is still 2.85 bn — the #2434 defect itself.
    """
    if isinstance(value, ast.Constant) and value.value is None:
        return allow_none  # fabricated → NULL (#2434); a measurement → not a guard
    if isinstance(value, ast.Constant) and value.value == 0:
        return True  # the `skipped` rows write a literal 0
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "duration_ms_between"
    )


@pytest.mark.parametrize("path", _WRITERS, ids=lambda p: p.name)
def test_1832_every_duration_writer_is_clamped(path):
    """No writer may bind duration_ms to an unguarded ``completed_at - started_at``.

    The two ``src/scheduler`` writers ship in a separate image that these unit
    tests cannot import, so the invariant is enforced against the source. This
    also catches a call-site added later, which is how the reported bug survived
    being fixed in one place — twice (#1832 low end, #2434 high end).
    """
    unguarded = [
        lineno
        for lineno, value, func_name in _duration_expressions(path)
        if not _is_guarded(value, allow_none=func_name in _FABRICATION_SITES)
    ]

    assert not unguarded, (
        f"{path.relative_to(_ROOT)} binds duration_ms unguarded at "
        f"line(s) {unguarded} — a measured writer must call "
        f"duration_ms_between(started_at, completed_at) (clamps negative skew "
        f"per #1832 AND returns None above the PostgreSQL int4 ceiling per "
        f"#2434); a sweep that FABRICATES the end time must write None."
    )


def test_1832_source_guard_would_catch_a_regression():
    """The guard must fail on an unclamped assignment, not vacuously pass."""
    tree = ast.parse("duration_ms = int((completed_at - started_at).seconds)")
    value = tree.body[0].value

    assert _is_guarded(value) is False


def test_2434_source_guard_catches_an_inlined_keyword_regression():
    """The keyword extension must fire on the shape it exists to catch.

    Without this, adding the two sweep files to ``_WRITERS`` would be an
    unverified change: the guard would pass because every site writes
    ``duration_ms=None``, and nothing would prove it can still fail when someone
    inlines the subtraction straight into ``.values(...)`` — the regression the
    #2434 fix is protecting against.
    """
    src = (
        "conn.execute(update(t).values("
        "duration_ms=int((completed_at - started_at).total_seconds() * 1000)))"
    )
    found = list(_duration_expressions_from_source(src))

    assert found, "the keyword sink was not seen at all"
    assert not any(_is_guarded(value) for _, value, _fn in found)


def test_2434_bare_max_is_no_longer_accepted():
    """``max(0, …)`` guards only the LOW end — accepting it re-opens #2434."""
    tree = ast.parse(
        "duration_ms = max(0, int((completed_at - started_at).total_seconds() * 1000))"
    )

    assert _is_guarded(tree.body[0].value) is False


def test_2434_pass_through_keyword_is_not_double_judged():
    """A pass-through moves an existing duration; it never computes one.

    ``duration_ms=duration_ms`` is judged at the local's ``Assign``, and
    ``duration_ms=row["duration_ms"]`` is a row->model READ that cannot produce a
    value the column did not already hold. Both are skipped — but narrowly: a
    different key, or any computation, still has to satisfy the guard.
    """
    skipped = [
        "conn.execute(update(t).values(duration_ms=duration_ms))",
        "ScheduleExecution(duration_ms=row['duration_ms'])",
    ]
    for src in skipped:
        assert list(_duration_expressions_from_source(src)) == [], src

    not_skipped = [
        "ScheduleExecution(duration_ms=row['elapsed'])",
        "ScheduleExecution(duration_ms=int((c - s).total_seconds() * 1000))",
        # A DIFFERENTLY-NAMED local is not a pass-through: nothing judged it,
        # because `_duration_expressions` only judges an `Assign` whose target
        # is literally `duration_ms`. Skipping every `ast.Name` would let
        # `elapsed = int((c - s)...)` + `.values(duration_ms=elapsed)` — the
        # shape `fan_out_service` already uses — through the guard untouched.
        "conn.execute(update(t).values(duration_ms=elapsed_ms))",
    ]
    for src in not_skipped:
        found = list(_duration_expressions_from_source(src))
        assert found, src
        assert not any(_is_guarded(v) for _, v, _fn in found), src


def test_2434_accepted_shapes_are_exactly_three():
    """None (fabricated), literal 0 (skipped), duration_ms_between (measured)."""
    accepted = [
        "duration_ms = None",
        "duration_ms = 0",
        "duration_ms = duration_ms_between(started_at, completed_at)",
    ]
    for src in accepted:
        assert _is_guarded(ast.parse(src).body[0].value) is True, src

    rejected = [
        "duration_ms = int((c - s).total_seconds() * 1000)",
        "duration_ms = max(0, int((c - s).total_seconds() * 1000))",
        "duration_ms = min(2**31 - 1, ms)",
        "duration_ms = 1",
    ]
    for src in rejected:
        assert _is_guarded(ast.parse(src).body[0].value) is False, src


def test_2434_none_is_only_accepted_at_a_fabrication_site():
    """`None` is a guard at a sweep and a DISCARDED MEASUREMENT anywhere else.

    Blanket-accepting `None` was the loophole Codex named: it would let a future
    edit replace `duration_ms_between(...)` with `None` inside
    `update_execution_status` or either scheduler finalizer — throwing away a
    real number — and this guard would still pass.
    """
    value = ast.parse("duration_ms = None").body[0].value

    assert _is_guarded(value, allow_none=True) is True
    assert _is_guarded(value, allow_none=False) is False

    # literal 0 and the helper stay accepted on BOTH sides of the split.
    for src in ("duration_ms = 0", "duration_ms = duration_ms_between(s, c)"):
        node = ast.parse(src).body[0].value
        assert _is_guarded(node, allow_none=False) is True, src


def test_2434_scanner_reports_the_enclosing_function():
    """The per-site rule needs the site, so the scanner must carry it.

    Also pins the `db/activities.py` shape specifically: a fabricator and the
    measured `complete_activity` live in ONE file, so file granularity would be
    too coarse to catch a measured writer that starts persisting `None`.
    """
    src = (
        "class Ops:\n"
        "    def mark_stale_activities_failed(self):\n"
        "        conn.execute(update(t).values(duration_ms=None))\n"
        "    def complete_activity(self):\n"
        "        conn.execute(update(t).values(duration_ms=None))\n"
    )
    by_func = {fn: value for _, value, fn in _duration_expressions_from_source(src)}

    assert set(by_func) == {"mark_stale_activities_failed", "complete_activity"}
    assert "mark_stale_activities_failed" in _FABRICATION_SITES
    assert "complete_activity" not in _FABRICATION_SITES
    assert _is_guarded(by_func["mark_stale_activities_failed"], allow_none=True) is True
    assert _is_guarded(by_func["complete_activity"], allow_none=False) is False
