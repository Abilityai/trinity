"""#3064 — the Workspace unread count must not full-scan the message table.

`count_unread_by_session` runs on every 20 s Workspace poll, per open tab. Its
message arm filters `enterprise_portal_messages` by the viewer's `client_email`,
`role = 'assistant'`, the thread and `created_at` after a read cursor — and no
index served that predicate, so SQLite's plan was `SCAN m`: the cost of every
poll grew with the whole install's Workspace message volume.

The plan is read off the REAL query: the SQL `count_unread_by_session` actually
sends is captured at the cursor and EXPLAINed, against a throwaway sqlite built
from the real `db/schema.py` DDL (the fresh-install track) and, separately,
against a database upgraded by the real SQLite migration (the upgrade track).
Pinning the SQL text by hand would let the test and the query drift apart.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

INDEX = "idx_portal_messages_unread"
# ent#610 (D3): the unread count also counts reports addressed to the viewer and
# stamped to their chat, so the real query reads `agent_reports` too — the
# fresh track builds every table that query touches, or it cannot run at all.
TABLES = ("enterprise_portal_messages", "enterprise_portal_chat_state", "enterprise_portal_sessions",
          "agent_reports")


def _fresh(db_file, *, with_indexes=True):
    """The fresh-install track: the three tables + their indexes from schema.py."""
    from db import schema
    conn = sqlite3.connect(db_file)
    for t in TABLES:
        conn.execute(schema.TABLES[t])
    if with_indexes:
        for ddl in schema.INDEXES:
            if any(f" {t}(" in ddl or f" {t} (" in ddl for t in TABLES):
                conn.execute(ddl)
    conn.commit()
    return conn


def _captured_unread_sql(db_file, monkeypatch):
    """Run the real `count_unread_by_session` and return the SQL + params it sent."""
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    from sqlalchemy import event
    from db.engine import get_engine
    from client_portal import db as pdb
    engine = get_engine()
    seen = []

    def grab(conn, cursor, statement, parameters, context, executemany):
        if "enterprise_portal_messages" in statement:
            seen.append((statement, parameters))
    event.listen(engine, "before_cursor_execute", grab)
    try:
        pdb.count_unread_by_session("alice@example.com")
    finally:
        event.remove(engine, "before_cursor_execute", grab)
    assert seen, "count_unread_by_session sent no query touching the message table"
    return seen[0]


def _plan(db_file, sql, params):
    conn = sqlite3.connect(db_file)
    try:
        return [row[-1] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)]
    finally:
        conn.close()


def _scans_messages(plan):
    """A full scan of the messages table (alias `m`), as opposed to a SEARCH/covering index."""
    return [p for p in plan if p.startswith("SCAN m") or p.startswith("SCAN enterprise_portal_messages")]


def test_the_unread_message_arm_uses_the_index_not_a_scan(tmp_path, monkeypatch):
    db_file = tmp_path / "fresh.db"
    _fresh(db_file).close()
    sql, params = _captured_unread_sql(db_file, monkeypatch)
    plan = _plan(db_file, sql, params)
    assert not _scans_messages(plan), f"message table full-scanned: {plan}"
    assert any(INDEX in p for p in plan), f"{INDEX} not used: {plan}"


def test_without_the_index_the_same_query_scans(tmp_path, monkeypatch):
    """Negative control: the assertion above can fail. Drop only the new index and
    the planner falls back to the scan #3064 reported."""
    db_file = tmp_path / "control.db"
    conn = _fresh(db_file)
    conn.execute(f"DROP INDEX IF EXISTS {INDEX}")
    conn.commit(); conn.close()
    sql, params = _captured_unread_sql(db_file, monkeypatch)
    assert _scans_messages(_plan(db_file, sql, params))


def test_the_sqlite_migration_adds_the_index_to_an_existing_install(tmp_path):
    """The upgrade track: an install that predates the index gets it from the
    versioned migration, idempotently."""
    from db import migrations
    db_file = tmp_path / "upgrade.db"
    conn = _fresh(db_file, with_indexes=False)
    fn = dict(migrations.MIGRATIONS)["portal_messages_unread_index"]
    for _ in range(2):  # re-running is a no-op
        fn(conn.cursor(), conn)
        conn.commit()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='enterprise_portal_messages'")}
    conn.close()
    assert INDEX in names


def test_the_alembic_revision_creates_the_same_index_on_the_current_head():
    import re
    versions = _BACKEND / "migrations" / "versions"
    hits = [p for p in versions.glob("*.py") if INDEX in p.read_text()]
    assert len(hits) == 1, f"expected one Alembic revision carrying {INDEX}, found {hits}"
    src = hits[0].read_text()
    assert re.search(r"CREATE INDEX IF NOT EXISTS " + INDEX + r"\s*\"\s*\n?\s*\"ON enterprise_portal_messages\(client_email, role, session_id, created_at\)", src)
    assert "DROP INDEX IF EXISTS " + INDEX in src
