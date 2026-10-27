"""#3317 — the loop-run claim and the locked re-arm hold under real lock contention.

`db/loops.py` makes two decisions under the `agent_loops` row's lock:

* `start_loop_run` — inserting run K is a claim (write first, then refuse if a
  run numbered K or above exists, then insert);
* `rearm_lost_dispatch` — reconcile's re-arm is classified on the loop and run
  rows read while the row is held.

Both are only as good as two facts this file pins:

1. **Every inserter of `agent_loop_runs` takes the parent row first.** That holds
   while `start_loop_run` is the only inserter, so a static guard fails the build
   on any other insert, in any spelling, anywhere in the backend (the private
   submodule included when it is mounted) or the scheduler.
2. **A second writer really waits on the first one's row lock, then decides on
   the committed state.** Four two-connection races, run against the real SQL: a
   holder opens a transaction and takes the loop row exactly as the competing
   operation's first statement does; the contender, the real method, starts on
   its own connection in a second thread. The test proves the contender is
   WAITING before it lets the holder commit — on PostgreSQL by finding a backend
   whose `pg_blocking_pids()` names the holder's pid, on SQLite by seeing the
   contender issue its lock-taking UPDATE (a `before_cursor_execute` flag on its
   thread) and not return. Then it checks the contender's outcome.

The races carry `requires_postgres`: `schema-parity.yml`'s PostgreSQL tier
selects that marker and sets TEST_POSTGRES_URL, so `db_backend` runs them on both
backends there (CI runs every other loop test on SQLite only).
"""
from __future__ import annotations

import ast
import os
import re
import sys
import threading
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND_STR = str(_REPO / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db_harness import db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. The static guard: `start_loop_run` is the only inserter of agent_loop_runs
# ---------------------------------------------------------------------------

# Scanned: the backend (the `enterprise` submodule lives under it, so a mounted
# private tree is covered too) and the scheduler, which shares the database.
_SCAN_ROOTS = (_REPO / "src" / "backend", _REPO / "src" / "scheduler")

_INSERT = re.compile(
    # insert(agent_loop_runs …), make_insert(…), pg_insert(tables.agent_loop_runs …)
    r"insert\s*\(\s*(?:[\w.]+\.)?agent_loop_runs\b"
    # agent_loop_runs.insert(…)
    r"|\bagent_loop_runs\s*\.\s*insert\s*\("
    # raw SQL: INSERT [OR …] INTO [quote, escaped or not]agent_loop_runs
    r"|\binsert\s+(?:or\s+\w+\s+)?into\s+\\?[\"'`\[]?agent_loop_runs\b",
    re.IGNORECASE,
)

_ALLOWED = {("src/backend/db/loops.py", "LoopOperations.start_loop_run")}


def _enclosing_def(tree: ast.AST, line: int) -> str:
    """Qualified name of the innermost def/class containing ``line``."""
    best: list = []

    def visit(node, trail):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if child.lineno <= line <= (child.end_lineno or child.lineno):
                    best[:] = trail + [child.name]
                    visit(child, trail + [child.name])
            else:
                visit(child, trail)

    visit(tree, [])
    return ".".join(best) or "<module>"


def _insert_sites() -> tuple[list, int]:
    sites, scanned = [], 0
    for root in _SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if any(part in {"node_modules", "__pycache__"} for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            scanned += 1
            hits = list(_INSERT.finditer(text))
            if not hits:
                continue
            tree = ast.parse(text, filename=str(path))
            rel = path.relative_to(_REPO).as_posix()
            for m in hits:
                line = text.count("\n", 0, m.start()) + 1
                sites.append((rel, _enclosing_def(tree, line), line))
    return sites, scanned


def test_start_loop_run_is_the_only_inserter_of_agent_loop_runs():
    """The claim's "no run numbered K or above" check runs under the parent
    row's lock, which is sound only because every inserter takes that lock
    first. A second inserter (a restore path, a repair, a bulk copy) would
    reopen the double dispatch #3317 closed. Route it through
    `LoopOperations.start_loop_run`, or make it take the loop row first and
    extend this allowlist with the reason."""
    sites, scanned = _insert_sites()
    assert scanned > 100, f"scanned only {scanned} files — the scan roots moved"
    assert {(f, d) for f, d, _ in sites} == _ALLOWED, sites
    assert len(sites) == 1, sites


@pytest.mark.parametrize(
    "src",
    [
        "insert(agent_loop_runs).values(x=1)",
        "stmt = insert( agent_loop_runs )",
        "make_insert(agent_loop_runs)",
        "pg_insert(tables.agent_loop_runs)",
        "agent_loop_runs.insert().values(x=1)",
        '"INSERT INTO agent_loop_runs (id) VALUES (?)"',
        "'insert or ignore into agent_loop_runs values (1)'",
        '"INSERT INTO \\"agent_loop_runs\\" (id)"',
    ],
)
def test_the_guard_matches_every_insert_spelling(src):
    assert _INSERT.search(src), src


@pytest.mark.parametrize(
    "src",
    [
        "select(agent_loop_runs)",
        "update(agent_loop_runs).where(x)",
        '"DELETE FROM agent_loop_runs WHERE loop_id = ?"',
        '"CREATE TABLE IF NOT EXISTS agent_loop_runs ("',
        "insert(agent_loops).values(x=1)",
    ],
)
def test_the_guard_ignores_what_is_not_an_insert_into_the_run_table(src):
    assert not _INSERT.search(src), src


# ---------------------------------------------------------------------------
# 2. Two connections: the second writer waits on the row, then decides
# ---------------------------------------------------------------------------

_PARK = "2000-01-01T00:00:00.000000Z"


def _ops():
    from db.loops import LoopOperations

    return LoopOperations()


def _loop_after_run1() -> str:
    """A running loop whose run 1 a worker claimed, advanced and closed."""
    ops = _ops()
    lid = ops.create_loop(agent_name="a3317", message_template="m", max_runs=5)["id"]
    ops.mark_loop_running(lid)
    rid = ops.start_loop_run(lid, 1, execution_id="e_run1")
    assert rid is not None and ops.claim_loop_advance(lid, 1)
    assert ops.finalize_loop_run(rid, status="completed", response="r", error=None,
                                 cost=0.0, duration_ms=1)
    return lid


def _hold_claim(lid):
    """What a claim of run 2 does before it commits: take the row, insert run 2."""
    from sqlalchemy import insert, update

    from db.tables import agent_loop_runs, agent_loops

    def hold(conn):
        conn.execute(update(agent_loops).where(agent_loops.c.id == lid).values(next_run_at=None))
        conn.execute(insert(agent_loop_runs).values(
            id="lr_holder", loop_id=lid, run_number=2, execution_id="e_holder",
            status="running", started_at="2026-10-27T12:00:00.000000Z",
        ))

    return hold


def _hold_rearm(lid):
    """What a re-arm does before it commits: take the row, write the park."""
    from sqlalchemy import and_, update

    from db.tables import agent_loops

    def hold(conn):
        conn.execute(update(agent_loops).where(and_(
            agent_loops.c.id == lid, agent_loops.c.next_run_at.is_(None),
        )).values(next_run_at=agent_loops.c.next_run_at))
        conn.execute(update(agent_loops).where(agent_loops.c.id == lid).values(next_run_at=_PARK))

    return hold


def _waiting_on(holder_pid: int) -> bool:
    """PostgreSQL: some backend is blocked by the holder's transaction."""
    import sqlalchemy as sa

    eng = sa.create_engine(os.environ["TEST_POSTGRES_URL"])
    try:
        with eng.connect() as c:
            return bool(c.execute(
                sa.text("SELECT count(*) FROM pg_stat_activity "
                        "WHERE :holder = ANY(pg_blocking_pids(pid))"),
                {"holder": holder_pid},
            ).scalar())
    finally:
        eng.dispose()


def _race(db_backend, hold, contend):  # noqa: F811
    """Holder takes the row and keeps its transaction open; the contender runs
    on its own connection in a second thread. Prove the contender waits, then
    commit the holder and return the contender's result."""
    from sqlalchemy import event

    from db.engine import get_engine

    engine = get_engine()
    box: dict = {}
    issued = threading.Event()

    def _flag(conn, cursor, statement, params, context, executemany):
        if threading.current_thread() is contender and statement.lstrip().upper().startswith(
            "UPDATE AGENT_LOOPS"
        ):
            issued.set()

    def _run_contender():
        try:
            box["result"] = contend()
        except BaseException as exc:  # noqa: BLE001 — reported by the assertion below
            box["error"] = exc

    contender = threading.Thread(target=_run_contender, name="contender")
    event.listen(engine, "before_cursor_execute", _flag)
    try:
        with engine.connect() as holder:
            tx = holder.begin()
            holder_pid = (holder.exec_driver_sql("SELECT pg_backend_pid()").scalar()
                          if db_backend == "postgres" else None)
            hold(holder)
            contender.start()
            deadline = time.monotonic() + 15
            waited = False
            while time.monotonic() < deadline and not waited:
                if db_backend == "postgres":
                    waited = _waiting_on(holder_pid)
                else:
                    # Issued its lock-taking write, and still has no answer.
                    waited = issued.is_set() and (time.sleep(0.3) or contender.is_alive())
                if not waited:
                    time.sleep(0.05)
            box["waited"] = waited
            tx.commit()
        contender.join(30)
    finally:
        event.remove(engine, "before_cursor_execute", _flag)
    assert not contender.is_alive(), "the contender never finished"
    assert "error" not in box, box.get("error")
    assert box["waited"], "the contender never waited on the holder's row lock"
    return box["result"]


def _row(lid):
    return _ops().get_loop(lid)


def _runs(lid):
    return [(r["run_number"], r["execution_id"], r["status"]) for r in _ops().list_runs(lid)]


def _never(_loop, _runs):  # a due_for that must not be reached
    raise AssertionError("due_for called on a refused re-arm")


@pytest.mark.requires_postgres
def test_claim_vs_claim_the_second_claim_waits_and_loses(db_backend):  # noqa: F811
    lid = _loop_after_run1()
    got = _race(db_backend, _hold_claim(lid),
                lambda: _ops().start_loop_run(lid, 2, execution_id="e_second"))
    assert got is None
    assert _runs(lid) == [(1, "e_run1", "completed"), (2, "e_holder", "running")]


@pytest.mark.requires_postgres
def test_rearm_vs_inflight_claim_waits_and_finds_the_run_in_flight(db_backend):  # noqa: F811
    lid = _loop_after_run1()
    outcome, run = _race(db_backend, _hold_claim(lid),
                         lambda: _ops().rearm_lost_dispatch(lid, due_for=_never))
    assert (outcome, run["run_number"], run["execution_id"]) == ("open_run", 2, "e_holder")
    assert _row(lid)["next_run_at"] is None


@pytest.mark.requires_postgres
def test_claim_vs_inflight_rearm_waits_then_wins_and_spends_the_park(db_backend):  # noqa: F811
    lid = _loop_after_run1()
    got = _race(db_backend, _hold_rearm(lid),
                lambda: _ops().start_loop_run(lid, 2, execution_id="e_claim"))
    assert got is not None
    assert _row(lid)["next_run_at"] is None
    assert _runs(lid) == [(1, "e_run1", "completed"), (2, "e_claim", "running")]


@pytest.mark.requires_postgres
def test_rearm_vs_rearm_the_second_waits_and_finds_it_parked(db_backend):  # noqa: F811
    lid = _loop_after_run1()
    got = _race(db_backend, _hold_rearm(lid),
                lambda: _ops().rearm_lost_dispatch(lid, due_for=_never))
    assert got == ("parked", None)
    assert _row(lid)["next_run_at"] == _PARK
