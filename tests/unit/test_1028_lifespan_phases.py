"""#1028 — `main.py::lifespan` is a thin orchestrator over ordered phase helpers.

WHY THIS TEST EXISTS. `lifespan` was 580 lines at cyclomatic complexity 109 —
the longest function in the backend and the first item the refactor audit named.
Splitting it is easy; keeping it split is the part that needs a guard, and the
thing worth guarding is not the line count. It is **the order**.

Boot ordering is load-bearing in ways that are invisible at the call site. A
reviewer looking at sixteen `await _phase()` lines has no way to see that moving
one of them breaks something, because the coupling lives in the bodies. Before
this refactor the ordering was implicit in a 580-line function nobody could read
in one sitting; after it, the ordering is a list — which is a real improvement
only if something enforces the list.

So this file pins:

1. **The sequence**, exactly, with the reason for each constrained pair recorded
   next to it. A reorder fails here with the reason attached, rather than
   surfacing weeks later as a boot-order bug nobody connects to the refactor.
2. **`yield` sits between startup and shutdown** — the one structural property
   that makes it a lifespan at all. A phase accidentally appended after `yield`
   would silently become shutdown work.
3. **The thresholds the issue actually asked for** (<100 lines, CC <20), so the
   split cannot rot back by accretion — which is exactly how `lifespan` reached
   580 lines from something that was once reasonable.
4. **No orphans and no doubles** — a helper defined but never called is dead
   startup code that reads as live, and a helper called twice would run a phase
   twice.

Deliberately AST-only over the SOURCE: importing `main` pulls the whole backend
(routers, services, Docker, Redis) into a unit test. The properties asserted
here are structural, so the source is the right subject and the test stays in
the fast tier that actually gates a PR.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

MAIN_PY = (
    Path(__file__).resolve().parents[2] / "src" / "backend" / "main.py"
)

# The boot sequence. The comment on a line is the reason it cannot move; a line
# with no comment is ordered for readability and may be reordered freely.
STARTUP_PHASES = [
    # Logging first, unconditionally: a later phase that hangs must not be able
    # to swallow the boot log (#858 — print() is block-buffered to the Docker
    # pipe and silently lost, which is why the first-run notice is a warning).
    "_init_logging_and_first_run_notice",
    # Before the WebSocket endpoints accept clients, so the first connection has
    # a live dispatcher to register with (RELIABILITY-003 / #306).
    "_start_event_bus",
    "_log_startup_environment",
    # Before anything that sweeps or reconciles the fleet.
    "_init_docker_and_system_agent",
    "_start_maintenance_services",
    "_schedule_staggered_services",
    "_start_capacity_and_canary",
    "_schedule_watch_loops",
    # Before the transports: recovery reconciles executions left running by the
    # previous process, and inbound channel traffic creates new ones. Running it
    # after would let a fresh execution race the reconcile.
    "_run_startup_recovery",
    "_start_slack_transport",
    "_start_telegram_transport",
    "_start_whatsapp_transport",
]

SHUTDOWN_PHASES = [
    "_shutdown_background_services",
    "_shutdown_loops_and_transports",
    "_shutdown_probes_and_clients",
    # LAST, always: the bus drains for 2s so late-lifecycle broadcasts emitted
    # while the services above stop still land on the stream (#306).
    "_shutdown_audit_and_event_bus",
]

MAX_LINES = 100
MAX_COMPLEXITY = 20


def _tree() -> ast.Module:
    return ast.parse(MAIN_PY.read_text(encoding="utf-8"))


def _lifespan(tree: ast.Module) -> ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            return node
    raise AssertionError("main.py no longer defines `lifespan`")


def _awaited_names(node: ast.AST) -> list[str]:
    """Names of the helpers awaited directly in `lifespan`'s body, in order.

    Reads the body statements rather than `ast.walk`, because walk does not
    guarantee source order — and source order is the entire subject here.
    """
    out: list[str] = []
    for stmt in node.body:
        if not isinstance(stmt, ast.Expr):
            continue
        value = stmt.value
        if isinstance(value, ast.Await) and isinstance(value.value, ast.Call):
            func = value.value.func
            if isinstance(func, ast.Name):
                out.append(func.id)
    return out


def _complexity(node: ast.AST) -> int:
    score = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While,
                              ast.ExceptHandler, ast.With, ast.AsyncWith,
                              ast.Assert, ast.IfExp)):
            score += 1
        elif isinstance(child, ast.BoolOp):
            score += len(child.values) - 1
        elif isinstance(child, ast.Try):
            score += len(child.handlers)
        elif isinstance(child, ast.comprehension):
            score += 1
    return score


def test_lifespan_runs_the_phases_in_the_pinned_order():
    """The boot sequence is the contract. Read STARTUP_PHASES before changing it."""
    called = _awaited_names(_lifespan(_tree()))
    expected = STARTUP_PHASES + SHUTDOWN_PHASES
    assert called == expected, (
        "main.py's boot sequence changed.\n"
        f"  expected: {expected}\n"
        f"  actual:   {called}\n"
        "Several of these pairs are load-bearing and the reasons are recorded as "
        "comments in STARTUP_PHASES/SHUTDOWN_PHASES above. If the move is "
        "deliberate, change the list AND the reason in the same commit."
    )


def test_yield_separates_startup_from_shutdown():
    """A phase appended after `yield` would silently become shutdown work."""
    fn = _lifespan(_tree())
    yield_index = None
    for i, stmt in enumerate(fn.body):
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Yield):
            assert yield_index is None, "lifespan must yield exactly once"
            yield_index = i
    assert yield_index is not None, "lifespan no longer yields — it is not a lifespan"

    before, after = fn.body[:yield_index], fn.body[yield_index + 1:]
    assert _awaited_names(ast.Module(body=before, type_ignores=[])) == STARTUP_PHASES
    assert _awaited_names(ast.Module(body=after, type_ignores=[])) == SHUTDOWN_PHASES


def test_lifespan_itself_stays_thin():
    """It was 580 lines at CC 109. The whole point is that it is now a list."""
    fn = _lifespan(_tree())
    lines = fn.end_lineno - fn.lineno + 1
    assert lines <= 40, (
        f"`lifespan` is {lines} lines — logic is accreting back into the "
        "orchestrator. New startup work belongs in a phase helper (and in the "
        "pinned order above), not inline here."
    )
    assert _complexity(fn) <= 3, (
        "`lifespan` gained a branch. A conditional phase belongs INSIDE that "
        "phase's helper, so the boot sequence stays a flat readable list."
    )


@pytest.mark.parametrize("name", STARTUP_PHASES + SHUTDOWN_PHASES)
def test_each_phase_is_within_the_thresholds(name):
    """#1028's acceptance criterion: under 100 lines, complexity under 20."""
    fns = {
        n.name: n for n in _tree().body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert name in fns, f"phase helper `{name}` is called but not defined"
    node = fns[name]
    lines = node.end_lineno - node.lineno + 1
    complexity = _complexity(node)
    assert lines <= MAX_LINES, (
        f"`{name}` is {lines} lines (limit {MAX_LINES}). Split it rather than "
        "raising the limit — this is the threshold #1028 was filed against."
    )
    assert complexity <= MAX_COMPLEXITY, (
        f"`{name}` has cyclomatic complexity {complexity} (limit {MAX_COMPLEXITY})."
    )


def test_every_phase_helper_is_called_exactly_once():
    """A helper defined but never called is dead startup code that reads as live."""
    tree = _tree()
    defined = {
        n.name for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name.startswith(("_init_", "_start_", "_schedule_", "_log_startup",
                               "_run_startup", "_shutdown"))
    }
    called = _awaited_names(_lifespan(tree))

    orphaned = defined - set(called)
    assert not orphaned, (
        f"phase helper(s) defined but never awaited by `lifespan`: {sorted(orphaned)}. "
        "Either wire them into the pinned order or delete them."
    )
    doubled = [n for n in set(called) if called.count(n) > 1]
    assert not doubled, f"phase helper(s) awaited more than once: {sorted(doubled)}"


def test_only_lifespan_carries_the_asynccontextmanager_decorator():
    """`@asynccontextmanager` belongs on `lifespan` and on nothing else.

    This is not hypothetical: extracting the phases moved the decorator by one
    definition, so it landed on the FIRST phase helper and `lifespan` was left
    a bare async generator. FastAPI would then have had no usable lifespan —
    a boot-breaking defect that every unit test still passed, because nothing
    in the suite imports `main` and asks what shape `lifespan` is.

    The refactor's own structure invites the mistake (a decorator sits on the
    line above a def, and this change inserts ~700 lines at exactly that seam),
    so it is pinned rather than remembered.
    """
    tree = _tree()
    decorated = {
        node.name: [ast.unparse(d) for d in node.decorator_list]
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.decorator_list
    }
    assert decorated.get("lifespan") == ["asynccontextmanager"], (
        "`lifespan` must carry exactly @asynccontextmanager — FastAPI needs an "
        f"async context manager, not a bare async generator. Found: "
        f"{decorated.get('lifespan')}"
    )
    stray = {
        name: decs for name, decs in decorated.items()
        if name in set(STARTUP_PHASES + SHUTDOWN_PHASES)
        and "asynccontextmanager" in decs
    }
    assert not stray, (
        f"a phase helper carries @asynccontextmanager: {stray}. It almost "
        "certainly slid off `lifespan` onto the definition below it."
    )
