"""The native ask create is atomic per agent on BOTH backends (trinity-enterprise#611, PR B).

`create_native_item` checks for a replay, counts the agent's pending asks and
inserts under ONE per-agent lock: SQLite `BEGIN IMMEDIATE`, PostgreSQL
`pg_advisory_xact_lock` keyed on the agent. Without it, concurrent creates at the
cap all read the same count and all insert, and the depth cap degrades into a
rate limit. The SQLite half is pinned again by test_ent611_native_ask.py on the
per-process database; this module is the PostgreSQL guard: `schema-parity.yml`
selects the `requires_postgres` marker and runs it with TEST_POSTGRES_URL set,
so `db_backend` parametrizes onto real PostgreSQL.

Harness: tests/db_harness.py — a fresh schema from `db.tables` per test, on
each available backend. `db.operator_queue` needs nothing heavier.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

_BACKEND_STR = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, scalar  # noqa: E402,F401

pytestmark = pytest.mark.requires_postgres

AGENT = "agent-611-pg"


def _ops():
    from db.operator_queue import OperatorQueueOperations
    return OperatorQueueOperations()


@pytest.fixture
def held_insert(monkeypatch):
    """Hold every create INSIDE its critical section — after the replay check
    and the count, before the insert — so the other threads arrive while it
    holds. With the lock they wait and then read the committed row; without it
    they all read the same empty state, deterministically. (A plain race is not
    enough: the threads serialize on opening pool connections, so a missing
    PostgreSQL lock passed 3 of 3 unforced runs.)"""
    import time
    import db.operator_queue as dbq

    real = dbq.make_insert

    def held(table):
        time.sleep(0.15)
        return real(table)

    monkeypatch.setattr(dbq, "make_insert", held)


def _create(ops, request_id, *, cap, agent=AGENT):
    return ops.create_native_item(
        agent,
        {"id": request_id, "type": "question", "title": "t", "question": "q", "context": {}},
        max_pending=cap, channel="mcp", raised_by="agent", to_role="operator",
        resolved_to=None, proposal=None, supersedes_expired=None,
    )


def _race(ops, n, *, cap, agent=AGENT, same_id=False):
    """n threads released together by a barrier, each creating one ask."""
    barrier = threading.Barrier(n)
    outcomes, errors = [], []

    def worker(i):
        try:
            barrier.wait()
            outcomes.append(_create(ops, "same" if same_id else f"race-{i}", cap=cap, agent=agent)["outcome"])
        except Exception as e:  # noqa: BLE001 — the assertion reports it
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sorted(outcomes), errors


def _rows(agent=AGENT):
    return scalar("SELECT COUNT(*) FROM operator_queue WHERE agent_name = :a", a=agent)


def test_concurrent_creates_at_the_cap_admit_exactly_one(db_backend, held_insert):
    outcomes, errors = _race(_ops(), 6, cap=1)
    assert errors == []
    assert outcomes == ["created"] + ["queue_full"] * 5
    assert _rows() == 1


def test_concurrent_retries_of_one_ask_make_one_row(db_backend, held_insert):
    outcomes, errors = _race(_ops(), 6, cap=25, same_id=True)
    assert errors == []
    assert outcomes == ["created"] + ["replayed"] * 5
    assert _rows() == 1


def test_one_agents_full_queue_never_holds_back_another(db_backend):
    ops = _ops()
    assert _create(ops, "a-1", cap=1)["outcome"] == "created"
    assert _create(ops, "a-2", cap=1)["outcome"] == "queue_full"
    assert _create(ops, "b-1", cap=1, agent="agent-611-pg-b")["outcome"] == "created"


def test_the_file_create_reports_whether_it_inserted(db_backend):
    """The poller's create reads the INSERT's own rowcount under
    `ON CONFLICT DO NOTHING`: True once, False on the repeat, one row, one id."""
    ops = _ops()
    item = {"id": "file-1", "type": "question", "title": "t", "question": "q", "context": {}}
    first = ops.create_item_with_outcome(AGENT, dict(item), channel="file", raised_by="agent")
    again = ops.create_item_with_outcome(AGENT, dict(item), channel="file", raised_by="agent")
    assert (first[1], again[1]) == (True, False)
    assert again[0] == first[0]
    assert _rows() == 1
