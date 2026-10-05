"""
#3246 — the upgrade sweep: subject columns, the backlog collapse, the index.

Drives the registered SQLite migration (``platform_alert_subjects``) against
the real ``schema.py`` DDL for ``operator_queue`` with the two new columns
and the new indexes stripped — a pre-upgrade install holding duplicates —
and asserts the ordered contract columns → sweep → index: the newest pending
row per derived subject survives and is stamped, the rest end as ONE batch
authored by the platform in the ent#611 vocabulary, agent-raised / gate /
external rows are untouched, a second run changes nothing, and only after
the sweep does the partial unique index exist and bite.

The decision function is the leaf's ``plan_sweep`` (test_3246_platform_alerts_leaf.py);
this file proves the migration applies it to a database.
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from db.migrations import MIGRATIONS, _migrate_platform_alert_subjects  # noqa: E402
from db.schema import INDEXES, TABLES  # noqa: E402

TS = "2026-10-01T00:00:00.000000Z"
NEW_COLUMNS = ("subject", "last_seen_at")
UNIQUE_INDEX = "uq_operator_queue_pending_subject"
PLAIN_INDEX = "idx_operator_queue_agent_subject"


def _pre_upgrade_ddl() -> str:
    ddl = TABLES["operator_queue"]
    for column in NEW_COLUMNS:
        ddl, n = re.subn(rf"^\s*{column} TEXT,?\n", "", ddl, flags=re.MULTILINE)
        assert n == 1, f"schema.py must declare {column} TEXT on operator_queue"
    return ddl


@pytest.fixture
def conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(_pre_upgrade_ddl())
    for ddl in INDEXES:
        if "operator_queue(" in ddl and UNIQUE_INDEX not in ddl and PLAIN_INDEX not in ddl:
            conn.execute(ddl)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(operator_queue)")}
    assert not cols & set(NEW_COLUMNS)
    return conn


def _add(conn, id_, agent, rid, created, *, status="pending", context=None,
         expires_at=None, disposed_by=None, disposed_at=None, type_="alert"):
    conn.execute(
        "INSERT INTO operator_queue (id, agent_name, request_id, type, status, priority, title, "
        "question, created_at, context, expires_at, disposed_by, disposed_at) "
        "VALUES (?, ?, ?, ?, ?, 'high', 't', 'q', ?, ?, ?, ?, ?)",
        (id_, agent, rid, type_, status, created,
         json.dumps(context) if context is not None else None, expires_at, disposed_by, disposed_at),
    )


def _snapshot(conn):
    return conn.execute("SELECT * FROM operator_queue ORDER BY id").fetchall()


def _run(conn):
    _migrate_platform_alert_subjects(conn.cursor(), conn)


def _seed_duplicates(conn):
    # three readings of one subscription's headroom — warn, warn, crit — plus the fleet one
    _add(conn, "h1", "_sub-headroom", "sub-headroom-sub-abc-123-2026-09-28-warn", "2026-09-28T00:00:00.000000Z")
    _add(conn, "h2", "_sub-headroom", "sub-headroom-sub-abc-123-2026-09-29-warn", "2026-09-29T00:00:00.000000Z")
    _add(conn, "h3", "_sub-headroom", "sub-headroom-sub-abc-123-2026-09-30-crit", "2026-09-30T00:00:00.000000Z",
         context={"tier": "crit"})
    _add(conn, "hf", "_sub-headroom", "sub-headroom-fleet-2026-09-30", "2026-09-30T00:00:00.000000Z")
    # the same circuit-dormant subject on two agents is two groups
    _add(conn, "c1", "x", f"cb-dormant-x-{TS}", "2026-09-20T00:00:00.000000Z")
    _add(conn, "c2", "x", f"cb-dormant-x-2026-09-21T00:00:00.000000Z", "2026-09-21T00:00:00.000000Z")
    _add(conn, "c3", "y", f"cb-dormant-y-{TS}", "2026-09-21T00:00:00.000000Z")
    # opt-out lifetime kind: survives with expires_at NULL
    _add(conn, "p1", "x", "poison-exec-1", "2026-09-21T00:00:00.000000Z")
    # known kind, no derivable subject: lifetime stamp only
    _add(conn, "g1", "x", f"git-bloat-x-{TS}", "2026-09-21T00:00:00.000000Z")
    _add(conn, "g2", "x", f"git-bloat-x-2026-09-22T00:00:00.000000Z", "2026-09-22T00:00:00.000000Z")
    # never touched: an agent's own ask, a gate row, an external-prefix row, a non-pending row
    _add(conn, "own", "x", "my-own-ask", "2026-09-21T00:00:00.000000Z", type_="question")
    _add(conn, "gate", "x", "gate-abc", "2026-09-21T00:00:00.000000Z", type_="approval")
    _add(conn, "r1", "x", "role-drift-x-1", "2026-09-21T00:00:00.000000Z")
    _add(conn, "r2", "x", "role-drift-x-2", "2026-09-22T00:00:00.000000Z")
    _add(conn, "done", "x", f"cb-dormant-x-2026-09-01T00:00:00.000000Z", "2026-09-01T00:00:00.000000Z",
         status="responded", disposed_by="person", disposed_at="2026-09-02T00:00:00.000000Z")


class TestColumnsThenSweep:
    def test_columns_are_added_and_the_migration_is_pragma_guarded(self, conn):
        _run(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(operator_queue)")}
        assert set(NEW_COLUMNS) <= cols
        _run(conn)  # a second run must not re-ALTER

    def test_keeps_the_newest_pending_row_per_subject_and_ends_the_rest_as_one_batch(self, conn):
        _seed_duplicates(conn)
        _run(conn)
        status = dict(conn.execute("SELECT id, status FROM operator_queue").fetchall())
        assert status["h3"] == "pending" and status["h1"] == status["h2"] == "cancelled"
        assert status["hf"] == "pending"
        assert status["c2"] == "pending" and status["c1"] == "cancelled"
        assert status["c3"] == "pending"
        assert status["p1"] == "pending"
        assert status["g1"] == status["g2"] == "pending"          # no subject → never merged on a guess
        assert status["own"] == status["gate"] == status["r1"] == status["r2"] == "pending"
        assert status["done"] == "responded"

        endings = conn.execute(
            "SELECT DISTINCT disposition, disposed_by, disposition_reason, disposed_by_email "
            "FROM operator_queue WHERE status='cancelled'").fetchall()
        assert endings == [("cancelled", "platform", "superseded", None)]
        batches = conn.execute(
            "SELECT DISTINCT batch_id FROM operator_queue WHERE status='cancelled'").fetchall()
        assert len(batches) == 1 and batches[0][0]
        assert all(r[0] for r in conn.execute(
            "SELECT disposed_at FROM operator_queue WHERE status='cancelled'"))

    def test_survivors_are_stamped(self, conn):
        _seed_duplicates(conn)
        _run(conn)
        subject, last_seen, expires, context = conn.execute(
            "SELECT subject, last_seen_at, expires_at, context FROM operator_queue WHERE id='h3'").fetchone()
        assert subject == "subscription_headroom:sub-abc-123"
        assert last_seen == "2026-09-30T00:00:00.000000Z"
        assert expires and expires.endswith("Z")
        assert json.loads(context) == {"tier": "crit", "seen_count": 1}
        # the ended duplicates carry no subject: the partial index sees one pending row
        assert conn.execute("SELECT subject FROM operator_queue WHERE id='h1'").fetchone()[0] is None
        # opt-out kind keeps expires_at NULL; a derived subject still lands
        assert conn.execute(
            "SELECT subject, expires_at FROM operator_queue WHERE id='p1'").fetchone() == ("poison:exec-1", None)
        # known kind without a subject: lifetime stamp only
        for gid in ("g1", "g2"):
            subject, expires = conn.execute(
                "SELECT subject, expires_at FROM operator_queue WHERE id=?", (gid,)).fetchone()
            assert subject is None and expires
        # untouched rows carry nothing
        for rid in ("own", "gate", "r1", "r2"):
            assert conn.execute(
                "SELECT subject, last_seen_at, expires_at FROM operator_queue WHERE id=?", (rid,)
            ).fetchone() == (None, None, None)

    def test_person_ended_row_inside_the_snooze_window_gets_subject_only(self, conn, monkeypatch):
        import services.platform_alerts as pa
        now = datetime.now(timezone.utc)
        recent = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        _add(conn, "acked", "x", f"cb-dormant-x-{TS}", TS, status="responded",
             disposed_by="person", disposed_at=recent)
        _add(conn, "timed", "x", f"cb-dormant-x-2026-09-21T00:00:00.000000Z", TS, status="expired",
             disposed_by="timeout", disposed_at=recent)
        _add(conn, "old", "x", f"cb-dormant-x-2026-09-22T00:00:00.000000Z", TS, status="responded",
             disposed_by="person", disposed_at="2020-01-01T00:00:00.000000Z")
        _run(conn)
        rows = dict(conn.execute("SELECT id, subject FROM operator_queue").fetchall())
        assert rows == {"acked": "circuit_dormant:x", "timed": None, "old": None}
        for rid in ("acked", "timed", "old"):
            assert conn.execute(
                "SELECT status, last_seen_at, expires_at FROM operator_queue WHERE id=?", (rid,)
            ).fetchone()[1:] == (None, None)

    def test_a_second_run_changes_nothing(self, conn):
        _seed_duplicates(conn)
        _run(conn)
        before = _snapshot(conn)
        _run(conn)
        assert _snapshot(conn) == before


class TestIndexAfterSweep:
    def test_the_indexes_exist_only_after_the_sweep_collapsed_duplicates(self, conn):
        _seed_duplicates(conn)
        names = lambda: {r[0] for r in conn.execute(  # noqa: E731
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='operator_queue'")}
        assert UNIQUE_INDEX not in names()
        _run(conn)
        assert {UNIQUE_INDEX, PLAIN_INDEX} <= names()

    def test_the_partial_unique_index_bites_on_a_second_pending_row(self, conn):
        _seed_duplicates(conn)
        _run(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO operator_queue (id, agent_name, request_id, type, status, priority, title, "
                "question, created_at, subject) VALUES ('dup', '_sub-headroom', 'new', 'alert', 'pending', "
                "'high', 't', 'q', ?, 'subscription_headroom:sub-abc-123')", (TS,))
        # ended and subject-less rows are outside the index
        conn.execute(
            "INSERT INTO operator_queue (id, agent_name, request_id, type, status, priority, title, "
            "question, created_at, subject) VALUES ('ended', '_sub-headroom', 'new2', 'alert', 'cancelled', "
            "'high', 't', 'q', ?, 'subscription_headroom:sub-abc-123')", (TS,))
        conn.execute(
            "INSERT INTO operator_queue (id, agent_name, request_id, type, status, priority, title, "
            "question, created_at) VALUES ('nosub', '_sub-headroom', 'new3', 'alert', 'pending', "
            "'high', 't', 'q', ?)", (TS,))

    def test_the_index_ddl_matches_schema_py(self, conn):
        _seed_duplicates(conn)
        _run(conn)
        live = dict(conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND name IN (?, ?)",
            (UNIQUE_INDEX, PLAIN_INDEX)).fetchall())
        declared = {}
        for ddl in INDEXES:
            for name in (UNIQUE_INDEX, PLAIN_INDEX):
                if f" {name} " in ddl:
                    declared[name] = ddl
        assert set(declared) == {UNIQUE_INDEX, PLAIN_INDEX}
        norm = lambda s: " ".join(s.replace("IF NOT EXISTS ", "").split())  # noqa: E731
        for name in (UNIQUE_INDEX, PLAIN_INDEX):
            assert norm(live[name]) == norm(declared[name])


class TestBothTracks:
    def test_registered_on_both_tracks_after_the_flood_sweep(self):
        names = [name for name, _ in MIGRATIONS]
        assert names.index("platform_alert_subjects") > names.index("supersede_queue_flood_backlog")
        versions = Path(_BACKEND) / "migrations" / "versions"
        files = [p for p in versions.glob("*_platform_alert_subjects.py")]
        assert len(files) == 1
        text = files[0].read_text()
        assert 'down_revision = "0089_supersede_queue_flood_backlog"' in text
        assert "plan_sweep" not in text or "run_platform_alert_sweep" in text

    def test_tables_py_declares_columns_and_both_indexes(self):
        from db.tables import operator_queue
        assert set(NEW_COLUMNS) <= set(operator_queue.c.keys())
        idx = {i.name: i for i in operator_queue.indexes}
        assert idx[UNIQUE_INDEX].unique is True
        assert str(idx[UNIQUE_INDEX].dialect_options["sqlite"]["where"]) == \
            "status = 'pending' AND subject IS NOT NULL"
        assert str(idx[UNIQUE_INDEX].dialect_options["postgresql"]["where"]) == \
            "status = 'pending' AND subject IS NOT NULL"
        assert PLAIN_INDEX in idx and idx[PLAIN_INDEX].unique is False
