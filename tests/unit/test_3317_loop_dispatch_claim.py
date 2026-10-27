"""#3317 — the loop-run claim and the locked re-arm hold under real lock contention.

`db/loops.py` makes two decisions under the `agent_loops` row's lock:

* `start_loop_run` — inserting run K is a claim (write first, then refuse if a
  run numbered K or above exists, then insert);
* `rearm_lost_dispatch` — reconcile's re-arm is classified on the loop and run
  rows read while the row is held.

Both are only as good as two facts this file pins:

1. **Every inserter of `agent_loop_runs` takes the parent row first.** That holds
   while `start_loop_run` is the only inserter, so a static guard fails the build
   on any other insert it can see — raw SQL (schema-qualified, quoted or not) and
   SQLAlchemy inserts through any import or table alias — anywhere in the backend
   (the private submodule included when it is mounted) or the scheduler.
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

# Raw SQL anywhere in the file text: INSERT [OR …] INTO [schema.]agent_loop_runs,
# each identifier optionally quoted (escaped or not, as it appears in a Python
# string).
_Q_OPEN, _Q_CLOSE = r"\\?[\"'`\[]?", r"\\?[\"'`\]]?"
_SQL_INSERT = re.compile(
    r"\binsert\s+(?:or\s+\w+\s+)?into\s+"
    rf"(?:{_Q_OPEN}\w+{_Q_CLOSE}\s*\.\s*)?"
    rf"{_Q_OPEN}agent_loop_runs\b",
    re.IGNORECASE,
)

_TABLE = "agent_loop_runs"
_ALLOWED = {("src/backend/db/loops.py", "LoopOperations.start_loop_run")}


def _sqlalchemy_insert_lines(tree: ast.AST) -> list:
    """Lines of SQLAlchemy inserts into the run table, through any import alias
    (`insert as ins`, `make_insert`, a dialect `insert`, `sa.insert`) and any
    module-level or local alias of the table (`... import agent_loop_runs as t`,
    `t = agent_loop_runs`, `t: Table = agent_loop_runs`, `tables.agent_loop_runs`).
    Not resolved: a table name
    built at runtime, a reflected `Table("agent_loop_runs", …)`, or a table passed
    in as a function argument — this guard cannot see those."""
    table_names, insert_names = {_TABLE}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == _TABLE:
                    table_names.add(a.asname or a.name)
                if "insert" in a.name.lower():
                    insert_names.add(a.asname or a.name)
    changed = True
    while changed:  # follow `t = agent_loop_runs` / `t2 = t` to a fixed point
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if _is_table(value, table_names):
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in table_names:
                        table_names.add(target.id)
                        changed = True

    def _is_insert_fn(func) -> bool:
        if isinstance(func, ast.Name):
            return func.id in insert_names or "insert" in func.id.lower()
        return isinstance(func, ast.Attribute) and "insert" in func.attr.lower()

    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "insert"
                and _is_table(func.value, table_names)):
            lines.append(node.lineno)  # agent_loop_runs.insert()
        elif _is_insert_fn(func):
            args = list(node.args) + [k.value for k in node.keywords if k.arg == "table"]
            if args and _is_table(args[0], table_names):
                lines.append(node.lineno)  # insert(agent_loop_runs)
    return lines


def _is_table(node, table_names) -> bool:
    if isinstance(node, ast.Name):
        return node.id in table_names
    return isinstance(node, ast.Attribute) and node.attr == _TABLE


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


def _sites_in(text: str) -> tuple:
    """(tree, sorted insert lines) for one module's source."""
    tree = ast.parse(text)
    lines = {text.count("\n", 0, m.start()) + 1 for m in _SQL_INSERT.finditer(text)}
    lines.update(_sqlalchemy_insert_lines(tree))
    return tree, sorted(lines)


def _insert_sites(roots=_SCAN_ROOTS) -> tuple[list, int]:
    sites, scanned = [], 0
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if any(part in {"node_modules", "__pycache__"} for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            scanned += 1
            if _TABLE not in text.lower():  # SQL identifiers are case-insensitive
                continue
            tree, lines = _sites_in(text)
            rel = (path.relative_to(_REPO) if path.is_relative_to(_REPO) else path).as_posix()
            sites.extend((rel, _enclosing_def(tree, line), line) for line in lines)
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


_T = "from db.tables import agent_loop_runs\n"


@pytest.mark.parametrize(
    "src",
    [
        "from sqlalchemy import insert\n" + _T + "insert(agent_loop_runs).values(x=1)",
        "from sqlalchemy import insert as ins\nfrom db.tables import agent_loop_runs as alr\nins(alr)",
        "import sqlalchemy as sa\nfrom db import tables\nsa.insert(tables.agent_loop_runs)",
        "from db.engine import make_insert\n" + _T + "make_insert(agent_loop_runs)",
        "from sqlalchemy.dialects.postgresql import insert as pg_insert\n" + _T + "pg_insert(agent_loop_runs)",
        _T + "t = agent_loop_runs\nu = t\nfrom sqlalchemy import insert\ninsert(u)",
        "from sqlalchemy import insert\nfrom sqlalchemy.sql.schema import Table\n" + _T
        + "t: Table = agent_loop_runs\ninsert(t)",
        _T + "agent_loop_runs.insert().values(x=1)",
        "from sqlalchemy import insert\n" + _T + "insert(table=agent_loop_runs)",
        'q = "INSERT INTO agent_loop_runs (id) VALUES (?)"',
        'q = "INSERT INTO AGENT_LOOP_RUNS (id) VALUES (?)"',
        "q = 'insert or ignore into agent_loop_runs values (1)'",
        'q = "INSERT INTO \\"agent_loop_runs\\" (id)"',
        'q = "INSERT INTO public.agent_loop_runs (id) VALUES (1)"',
        'q = "INSERT INTO \\"public\\".\\"agent_loop_runs\\" (id)"',
        "q = 'INSERT INTO [dbo].[agent_loop_runs] (id)'",
    ],
)
def test_the_guard_finds_every_spelling_it_claims(src):
    assert _sites_in(src)[1], src


def test_the_scanner_reports_an_insert_in_a_file_it_walks(tmp_path):
    """The entry point, not just the matcher: a module that names the table
    only in upper case must still be read (a case-sensitive prefilter skipped it)."""
    (tmp_path / "other_writer.py").write_text(
        'def restore(conn):\n    conn.exec_driver_sql("INSERT INTO AGENT_LOOP_RUNS (id) VALUES (1)")\n'
    )
    (tmp_path / "reader.py").write_text('Q = "SELECT * FROM agent_loop_runs"\n')
    sites, scanned = _insert_sites((tmp_path,))
    assert scanned == 2
    assert [(f.rsplit("/", 1)[-1], d, line) for f, d, line in sites] == [("other_writer.py", "restore", 2)]


@pytest.mark.parametrize(
    "src",
    [
        "from sqlalchemy import select\n" + _T + "select(agent_loop_runs)",
        "from sqlalchemy import update\n" + _T + "update(agent_loop_runs).where(x)",
        'q = "DELETE FROM agent_loop_runs WHERE loop_id = ?"',
        'q = "CREATE TABLE IF NOT EXISTS agent_loop_runs ("',
        "from sqlalchemy import insert\nfrom db.tables import agent_loops\ninsert(agent_loops)",
        'q = "INSERT INTO agent_loop_runs_archive (id) VALUES (1)"',
    ],
)
def test_the_guard_ignores_what_is_not_an_insert_into_the_run_table(src):
    assert not _sites_in(src)[1], src


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


# The test's own PostgreSQL probes never wait unboundedly.
_BOUNDED = {"connect_timeout": 5, "options": "-c statement_timeout=5000 -c lock_timeout=5000"}


def _cancel_backend(pid: int) -> None:
    import sqlalchemy as sa

    eng = sa.create_engine(os.environ["TEST_POSTGRES_URL"], connect_args=_BOUNDED)
    try:
        with eng.connect() as c:
            c.execute(sa.text("SELECT pg_cancel_backend(:p)"), {"p": pid})
    finally:
        eng.dispose()


def _waiting_on(holder_pid: int) -> bool:
    """PostgreSQL: some backend is blocked by the holder's transaction."""
    import sqlalchemy as sa

    eng = sa.create_engine(os.environ["TEST_POSTGRES_URL"], connect_args=_BOUNDED)
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
        if threading.current_thread() is not contender:
            return
        if db_backend == "postgres" and "pid" not in box:
            box["pid"] = cursor.connection.get_backend_pid()  # to cancel it if it hangs
        if statement.lstrip().upper().startswith("UPDATE AGENT_LOOPS"):
            issued.set()

    def _run_contender():
        try:
            box["result"] = contend()
        except BaseException as exc:  # noqa: BLE001 — reported by the assertion below
            box["error"] = exc

    # A daemon thread, and joined in `finally`: whatever fails below, the holder's
    # transaction ends (commit, or rollback on the way out), so the contender is
    # never left blocked on our lock and cannot outlive the test's teardown.
    contender = threading.Thread(target=_run_contender, name="contender", daemon=True)
    event.listen(engine, "before_cursor_execute", _flag)
    try:
        with engine.connect() as holder:
            tx = holder.begin()
            try:
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
            finally:
                if tx.is_active:
                    tx.rollback()
    finally:
        if contender.ident is not None:
            contender.join(30)
            if contender.is_alive() and "pid" in box:
                _cancel_backend(box["pid"])  # never leave it holding a connection
                contender.join(10)
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


def test_reconcile_and_the_claim_reach_the_real_sql_through_the_facade(db_backend, monkeypatch):  # noqa: F811
    """The loop suites drive `LoopOperations` directly; production goes through
    the `database.py` facade. Build that facade over the test engine (not the
    process-wide `database.db`, which other tests may replace) and drive
    reconcile and a claim through it, so a broken pass-through fails here."""
    import asyncio

    from database import DatabaseManager
    from db.loops import LoopOperations
    from services import loop_service as ls

    facade = object.__new__(DatabaseManager)
    facade._loop_ops = LoopOperations()
    monkeypatch.setattr(ls, "db", facade)
    lid = facade.create_loop(agent_name="a3317f", message_template="m", max_runs=3)["id"]
    facade.mark_loop_running(lid)

    assert asyncio.run(ls.LoopService().reconcile_after_restart()) == 1
    assert facade.get_loop(lid)["next_run_at"] is not None
    assert facade.rearm_lost_dispatch(lid, due_for=_never) == ("parked", None)
    assert facade.start_loop_run(lid, 2, execution_id="e_wrong") is None
    assert facade.start_loop_run(lid, 1, execution_id="e_facade") is not None
    assert facade.get_loop(lid)["next_run_at"] is None  # the claim spent the park
    assert [r["execution_id"] for r in facade.list_loop_runs(lid)] == ["e_facade"]


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
