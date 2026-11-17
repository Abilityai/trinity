"""The per-person memory blob is read-modify-written under a row lock, on BOTH backends (trinity-enterprise#962).

Three writers replace the whole `public_user_memory.memory_text` blob: the
agent's notes write, the summarizer's summary write, and undo. Interleaved,
one silently loses the other's half: an undo reverted, or a poisoned note
recorded as the "previous" an undo later restores. `_read_blob_for_update`
takes the lock first — PostgreSQL `SELECT … FOR UPDATE`, SQLite a no-op write
that takes the RESERVED lock before the read.

`schema-parity.yml` selects the `requires_postgres` marker with
TEST_POSTGRES_URL set, so `db_backend` parametrizes onto real PostgreSQL in
CI; locally the SQLite leg always runs. The interleaving is FORCED: every
writer is held inside its critical section (between the read and the write),
so without the lock both threads read the same blob, deterministically.
(A plain race is not enough — the ent#611 note: pool connections serialize.)
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

_BACKEND_STR = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, scalar  # noqa: E402,F401

pytestmark = pytest.mark.requires_postgres

AGENT = "agent-962-lock"
PERSON = "ann@example.com"
HOLD_S = 0.3


def _ops():
    from db.public_links import PublicLinkOperations
    return PublicLinkOperations()


@pytest.fixture
def held_writes(monkeypatch):
    """Hold every blob writer INSIDE its critical section: `_encode_memory_blob`
    runs after the read and before the UPDATE on all three paths. Patched on
    the dict the running methods read (the module-identity gotcha)."""
    from db.public_links import PublicLinkOperations

    g = PublicLinkOperations.write_user_memory_agent_notes.__globals__
    real = g["_encode_memory_blob"]

    def held(notes, summary):
        time.sleep(HOLD_S)
        return real(notes, summary)

    monkeypatch.setitem(g, "_encode_memory_blob", held)


def _together(*calls):
    """Run each call on its own thread, released together by a barrier."""
    barrier = threading.Barrier(len(calls))
    errors = []

    def worker(fn):
        try:
            barrier.wait()
            fn()
        except Exception as e:  # noqa: BLE001 — the assertion reports it
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(fn,)) for fn in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def _blob():
    from db.public_links import _parse_memory_blob
    raw = scalar("SELECT memory_text FROM public_user_memory WHERE agent_name = :a AND user_email = :e",
                 a=AGENT, e=PERSON)
    return _parse_memory_blob(raw)


def test_a_notes_write_and_a_summary_write_never_lose_each_other(db_backend, held_writes):
    ops = _ops()
    ops.write_user_memory_agent_notes(AGENT, PERSON, "notes v1", execution_id="e0", triggered_by="public")
    ops.update_user_memory_conversation_summary(AGENT, PERSON, "summary v1")

    errors = _together(
        lambda: ops.write_user_memory_agent_notes(AGENT, PERSON, "notes v2", execution_id="e1", triggered_by="public"),
        lambda: ops.update_user_memory_conversation_summary(AGENT, PERSON, "summary v2"),
    )
    assert errors == []
    assert _blob() == {"agent_notes": "notes v2", "conversation_summary": "summary v2"}


def test_an_undo_racing_a_summary_write_keeps_both(db_backend, held_writes):
    ops = _ops()
    ops.write_user_memory_agent_notes(AGENT, PERSON, "clean", execution_id="e0", triggered_by="public")
    poisoned = ops.write_user_memory_agent_notes(AGENT, PERSON, "poisoned", execution_id="e1", triggered_by="public")

    outcome = []
    errors = _together(
        lambda: outcome.append(ops.undo_user_memory_write(AGENT, PERSON, poisoned["write_id"], undone_by="op@example.com")),
        lambda: ops.update_user_memory_conversation_summary(AGENT, PERSON, "- a new summary"),
    )
    assert errors == [] and outcome == ["undone"]
    assert _blob() == {"agent_notes": "clean", "conversation_summary": "- a new summary"}
