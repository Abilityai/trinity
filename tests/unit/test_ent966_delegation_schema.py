"""
Schema for Nevermined card delegation (trinity-enterprise#966).

Executes the SQLite migration on a fresh database, the partial unique index,
the KEEP registry entry and its `agent_deleted_at` stamp, and the Alembic
revision's place in the version line.
"""
import ast
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "src" / "backend"
REVISION = BACKEND / "migrations" / "versions" / "0102_nevermined_delegations.py"

pytestmark = pytest.mark.unit


def _migrated(tmp_path) -> sqlite3.Connection:
    from db import migrations

    conn = sqlite3.connect(tmp_path / "t.db")
    migrations._migrate_nevermined_delegations(conn.cursor(), conn)
    return conn


def _insert(conn, row_id, agent="alpha", revoked_at=None, deleted_at=None):
    conn.execute(
        "INSERT INTO agent_delegations (id, agent_name, delegation_id, environment,"
        " created_by, created_at, revoked_at, agent_deleted_at)"
        " VALUES (?, ?, ?, 'sandbox', 'ada@example.com', '2026-10-22T20:00:00Z', ?, ?)",
        (row_id, agent, f"dlg-{row_id}", revoked_at, deleted_at),
    )


class TestSqliteMigration:
    def test_creates_both_tables_and_is_idempotent(self, tmp_path):
        from db import migrations

        conn = _migrated(tmp_path)
        migrations._migrate_nevermined_delegations(conn.cursor(), conn)  # second run
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"nevermined_organization", "agent_delegations"} <= tables
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_delegations)")}
        assert "replaced_at" not in cols  # ruling P7: a second bind is refused, never "replaced"
        assert {"api_key_injected", "revoked_at", "agent_deleted_at"} <= cols

    def test_registered_in_the_runner(self):
        from db import migrations

        names = [name for name, _ in migrations.MIGRATIONS]
        assert names.count("nevermined_delegations") == 1

    def test_second_active_row_for_one_agent_is_rejected(self, tmp_path):
        conn = _migrated(tmp_path)
        _insert(conn, "a")
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, "b")

    def test_revoked_or_deleted_rows_are_not_active(self, tmp_path):
        conn = _migrated(tmp_path)
        _insert(conn, "a", revoked_at="2026-10-22T21:00:00Z")
        _insert(conn, "b", deleted_at="2026-10-22T21:00:00Z")
        _insert(conn, "c")  # the one active row
        _insert(conn, "d", agent="beta")  # another agent's active row
        assert conn.execute("SELECT COUNT(*) FROM agent_delegations").fetchone()[0] == 4


class TestCleanupRegistry:
    def test_agent_delegations_is_keep(self):
        from db import agent_cleanup

        refs = [r for r in agent_cleanup.AGENT_REFS if r.table == "agent_delegations"]
        assert [(r.column, r.policy) for r in refs] == [("agent_name", agent_cleanup.Policy.KEEP)]

    def test_cascade_delete_keeps_and_stamps_the_row(self, tmp_path):
        from db import agent_cleanup, migrations

        engine = create_engine(f"sqlite:///{tmp_path / 'c.db'}")
        with engine.begin() as conn:
            for stmt in migrations.NEVERMINED_DELEGATIONS_DDL:
                conn.execute(text(stmt))
            from db.tables import metadata
            metadata.tables["agent_tags"].create(conn)  # the reports-to sweep reads it
            conn.execute(text(
                "INSERT INTO agent_delegations (id, agent_name, delegation_id, environment,"
                " created_by, created_at) VALUES ('r1', 'alpha', 'dlg-1', 'sandbox', 'ada', 'now')"
            ))
            agent_cleanup.cascade_delete(conn, "alpha")
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT agent_deleted_at FROM agent_delegations")).fetchall()
        assert len(rows) == 1 and rows[0][0]  # kept, and no longer active
        with engine.begin() as conn:  # a later agent of the same name can bind
            conn.execute(text(
                "INSERT INTO agent_delegations (id, agent_name, delegation_id, environment,"
                " created_by, created_at) VALUES ('r2', 'alpha', 'dlg-2', 'sandbox', 'ada', 'now')"
            ))


class TestAlembicRevision:
    def test_revision_chains_after_0101(self):
        tree = ast.parse(REVISION.read_text())
        values = {
            t.id: node.value.value
            for node in tree.body if isinstance(node, ast.Assign)
            for t in node.targets if isinstance(t, ast.Name) and isinstance(node.value, ast.Constant)
        }
        assert values["revision"] == "0102_nevermined_delegations"
        assert values["down_revision"] == "0101_held_gate_turns"

    def test_single_head(self):
        out = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "ci" / "check_alembic_heads.py"),
             str(BACKEND / "migrations" / "versions")],
            cwd=REPO, capture_output=True, text=True,
        )
        assert out.returncode == 0, out.stdout + out.stderr

    def test_revision_executes_the_shared_ddl(self, monkeypatch):
        """The PG revision runs exactly the SQLite track's statements."""
        import importlib.util
        from db import migrations

        executed = []
        fake_op = type("Op", (), {"execute": staticmethod(executed.append)})
        import alembic
        monkeypatch.setattr(alembic, "op", fake_op, raising=False)
        spec = importlib.util.spec_from_file_location("rev0102", REVISION)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.op = fake_op
        mod.upgrade()
        assert executed == list(migrations.NEVERMINED_DELEGATIONS_DDL)


class TestCompensation:
    """M3: the bind's compensation deletes ONLY the row it inserted, so a
    concurrent bind that took the agent's slot in between is left active."""

    AGENT = "ent966-comp"

    @pytest.fixture
    def clean(self):
        from db.engine import get_engine

        def wipe():
            with get_engine().begin() as conn:
                conn.execute(text("DELETE FROM agent_delegations WHERE agent_name = :a"), {"a": self.AGENT})
                conn.execute(text("DELETE FROM agent_ownership WHERE agent_name = :a"), {"a": self.AGENT})

        wipe()
        with get_engine().begin() as conn:  # active reads join the live ownership row (H3)
            conn.execute(text("INSERT INTO agent_ownership (agent_name, owner_id, created_at)"
                              " VALUES (:a, 1, 'now')"), {"a": self.AGENT})
        yield
        wipe()

    def test_compensation_tolerates_active_row(self, clean):
        from database import db

        mine = db.insert_agent_delegation(
            self.AGENT, "dlg-mine-0001", None, None, "sandbox", True, "ada@example.com")
        # Between this bind's insert and its failed inject: a revoke stamps its
        # row, and a second bind takes the agent's active slot.
        assert db.revoke_agent_delegation(mine["id"], "admin@example.com")
        theirs = db.insert_agent_delegation(
            self.AGENT, "dlg-theirs-001", None, None, "sandbox", False, "admin@example.com")

        # The first bind's compensation runs now — no IntegrityError, and only
        # its own row goes.
        assert db.delete_agent_delegation_row(mine["id"]) is True
        active = db.get_active_agent_delegation(self.AGENT)
        assert active is not None and active["id"] == theirs["id"]
        assert [h["delegation_id"] for h in db.list_agent_delegation_history(self.AGENT)] == ["dlg-theirs-001"]
        # A repeated compensation is a no-op, never an error.
        assert db.delete_agent_delegation_row(mine["id"]) is False
        assert db.get_active_agent_delegation(self.AGENT)["id"] == theirs["id"]
