"""Replace a pending ask — schema, the atomic compare-and-set, the sink (#3247).

A scheduled run that needs a decision must not file a second ask about what it
already asked. An agent may REPLACE one of its own pending asks: the predecessor
ends `cancelled` / `disposed_by='agent'` / `disposition_reason='replaced'` and
the successor is shown instead — never an edit in place, never over a person's
answer, never a row the agent did not raise itself.

Related flow: docs/memory/feature-flows/operating-room.md
Requirement: docs/memory/requirements/security.md §26.10

Harness: the unit island's real per-process SQLite (`init_database()` builds the
full schema). Agent names are unique to this file.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

# The two nullable link columns (T3): `replaces` on the successor (the
# predecessor row's uuid), `replaced_by` on the predecessor (the successor's).
LINK_COLUMNS = ("replaces", "replaced_by")
ALEMBIC_REVISION = "0090_operator_queue_replace"
ALEMBIC_PARENT = "0089_supersede_queue_flood_backlog"


@pytest.fixture
def real_db():
    from database import db as real
    return real


def _pending(real_db, agent, rid, **over):
    item = {"id": rid, "type": "approval", "status": "pending", "priority": "high",
            "title": "Approve payout", "question": "Release 500 USDC?",
            "options": ["approve", "reject"], "context": {},
            "created_at": "2026-10-01T10:00:00Z"}
    item.update(over)
    return real_db.create_operator_queue_item(agent, item)


# ===========================================================================
# 1. Schema — one migration pair, both tracks (CP1)
# ===========================================================================

class TestSchema:
    @pytest.mark.parametrize("column", LINK_COLUMNS)
    def test_every_link_column_is_selectable_on_the_migrated_db(self, real_db, column):
        from sqlalchemy import select
        from db.engine import get_engine
        from db.tables import operator_queue
        with get_engine().connect() as conn:
            conn.execute(select(getattr(operator_queue.c, column)).limit(1)).all()

    def test_every_link_column_rides_the_item_projection(self, real_db):
        uid = _pending(real_db, "agent-3247-schema", "s-1")
        item = real_db.get_operator_queue_item(uid)
        missing = [c for c in LINK_COLUMNS if c not in item]
        assert missing == [], missing
        # nullable, no default, no backfill — a plain row links to nothing
        assert {c: item[c] for c in LINK_COLUMNS} == dict.fromkeys(LINK_COLUMNS)

    def test_the_sqlite_migration_is_registered_by_name_and_runs(self):
        import sqlite3
        from db import migrations
        names = [name for name, _fn in migrations.MIGRATIONS]
        assert names.count("operator_queue_replace") == 1
        # it follows the flood-backlog data migration it chains after on the PG track
        assert names.index("operator_queue_replace") > names.index("supersede_queue_flood_backlog")
        fn = dict(migrations.MIGRATIONS)["operator_queue_replace"]
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute("CREATE TABLE operator_queue (id TEXT PRIMARY KEY)")
        fn(cur, conn)
        fn(cur, conn)  # idempotent: the second run adds nothing and does not raise
        cols = [r[1] for r in cur.execute("PRAGMA table_info(operator_queue)")]
        assert cols == ["id", *LINK_COLUMNS]

    def test_the_alembic_revision_adds_the_same_two_columns(self):
        import importlib.util
        path = os.path.join(_BACKEND, "migrations", "versions", f"{ALEMBIC_REVISION}.py")
        spec = importlib.util.spec_from_file_location("rev_3247_replace", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.revision == ALEMBIC_REVISION
        assert mod.down_revision == ALEMBIC_PARENT
        assert tuple(mod._COLUMNS) == LINK_COLUMNS

    def test_the_schema_ddl_carries_both_columns(self):
        from db.schema import TABLES as SCHEMA
        ddl = SCHEMA["operator_queue"]
        for column in LINK_COLUMNS:
            assert f"{column} TEXT" in ddl
