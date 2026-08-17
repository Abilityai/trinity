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
)


def _duration_assignments(path: Path):
    """Yield (lineno, node) for every ``duration_ms = <expr>`` assignment."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "duration_ms":
                yield node.lineno, node.value


def _is_clamped(value: ast.AST) -> bool:
    """True when the assigned expression is a literal 0 or wrapped in max()."""
    if isinstance(value, ast.Constant) and value.value == 0:
        return True  # the `skipped` rows write a literal 0
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "max"
    )


@pytest.mark.parametrize("path", _WRITERS, ids=lambda p: p.name)
def test_1832_every_duration_writer_is_clamped(path):
    """No writer may assign an unguarded ``completed_at - started_at``.

    The two ``src/scheduler`` writers ship in a separate image that these unit
    tests cannot import, so the invariant is enforced against the source. This
    also catches a fourth call-site added later, which is how the reported bug
    survived being fixed in one place.
    """
    unclamped = [
        lineno
        for lineno, value in _duration_assignments(path)
        if not _is_clamped(value)
    ]

    assert not unclamped, (
        f"{path.relative_to(_ROOT)} assigns duration_ms unguarded at "
        f"line(s) {unclamped} — wrap in max(0, ...) so clock skew cannot "
        f"persist a negative duration (#1832)"
    )


def test_1832_source_guard_would_catch_a_regression():
    """The guard must fail on an unclamped assignment, not vacuously pass."""
    tree = ast.parse("duration_ms = int((completed_at - started_at).seconds)")
    value = tree.body[0].value

    assert _is_clamped(value) is False
