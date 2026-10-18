"""Delegation link: schema + server-side stamping (ent#935 addendum A).

When agent A delegates to agent B, B's execution row must say which of A's
executions asked for it, so a later evaluator can join child -> parent and
grade the caller's delegation, not only the callee's run.

M.1 (schema): `schedule_executions.parent_execution_id`, plus
`agent_evaluations.{caller_agent, parent_execution_id, delegation_mode}` and an
index on `agent_evaluations(caller_agent, agent_name, created_at)`, on BOTH
tracks (SQLite migration + Alembic revision, Invariant #9).

M.2 (stamping): the parent id is persisted at admission from the
`X-Trinity-Execution-Id` header / `ParallelTaskRequest.parent_execution_id`,
and kept ONLY when the parent row's agent is the child's authenticated source
agent — the `_inherited_channel_context` provenance rule. A forged or missing
parent stores NULL.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _BACKEND / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _columns(cur, table):
    cur.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


# --- M.1: SQLite track ------------------------------------------------------

class TestSqliteMigration:
    """The registered SQLite migration adds the columns and index to a
    pre-existing database, and is idempotent."""

    def _run_registered(self, cur, conn):
        migrations = _load("db/migrations.py", "ent935_migrations")
        fns = dict(migrations.MIGRATIONS)
        assert "delegation_link" in fns, "migration is not registered"
        fns["delegation_link"](cur, conn)

    def _pre_change_db(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "pre.db")
        cur = conn.cursor()
        cur.execute("CREATE TABLE schedule_executions (id TEXT PRIMARY KEY, "
                    "agent_name TEXT, source_agent_name TEXT)")
        cur.execute("CREATE TABLE agent_evaluations (id TEXT PRIMARY KEY, "
                    "agent_name TEXT NOT NULL, execution_id TEXT, "
                    "evaluator TEXT NOT NULL, created_at TEXT NOT NULL)")
        conn.commit()
        return conn, cur

    def test_adds_columns_and_index(self, tmp_path):
        conn, cur = self._pre_change_db(tmp_path)
        self._run_registered(cur, conn)
        assert "parent_execution_id" in _columns(cur, "schedule_executions")
        assert {"caller_agent", "parent_execution_id", "delegation_mode"} <= _columns(
            cur, "agent_evaluations")
        cur.execute("PRAGMA index_info(idx_agent_evaluations_caller)")
        assert [r[2] for r in cur.fetchall()] == ["caller_agent", "agent_name", "created_at"]

    def test_is_idempotent_and_keeps_rows(self, tmp_path):
        conn, cur = self._pre_change_db(tmp_path)
        cur.execute("INSERT INTO schedule_executions (id, agent_name) VALUES ('e1', 'b')")
        conn.commit()
        self._run_registered(cur, conn)
        self._run_registered(cur, conn)
        cur.execute("SELECT parent_execution_id FROM schedule_executions WHERE id='e1'")
        assert cur.fetchone() == (None,)   # existing rows read as "not delegated"

    def test_one_table_present_still_gets_its_half(self, tmp_path):
        """A DB holding agent_evaluations but not schedule_executions must not
        skip the evaluations half — init_schema then indexes caller_agent."""
        conn = sqlite3.connect(tmp_path / "one.db")
        cur = conn.cursor()
        cur.execute("CREATE TABLE agent_evaluations (id TEXT PRIMARY KEY, "
                    "agent_name TEXT NOT NULL, created_at TEXT NOT NULL)")
        self._run_registered(cur, conn)
        assert "caller_agent" in _columns(cur, "agent_evaluations")

    def test_fresh_ddl_declares_the_same_shape(self, tmp_path):
        """A fresh build (schema.py DDL + index list) lands in the same place."""
        schema = _load("db/schema.py", "ent935_schema")
        conn = sqlite3.connect(tmp_path / "fresh.db")
        cur = conn.cursor()
        cur.execute("CREATE TABLE agent_schedules (id TEXT PRIMARY KEY)")
        for table in ("schedule_executions", "agent_evaluations"):
            cur.execute(schema.TABLES[table])
        for stmt in schema.INDEXES:
            if "agent_evaluations" in stmt:
                cur.execute(stmt)
        assert "parent_execution_id" in _columns(cur, "schedule_executions")
        assert {"caller_agent", "parent_execution_id", "delegation_mode"} <= _columns(
            cur, "agent_evaluations")
        cur.execute("PRAGMA index_info(idx_agent_evaluations_caller)")
        assert [r[2] for r in cur.fetchall()] == ["caller_agent", "agent_name", "created_at"]


# --- M.1: PostgreSQL track + model ------------------------------------------

def test_alembic_revision_chains_after_0099_and_adds_the_columns():
    versions = _BACKEND / "migrations" / "versions"
    hits = [p for p in versions.glob("*.py")
            if 'down_revision = "0099_agent_peer_skill_grants"' in p.read_text()]
    assert len(hits) == 1, f"expected one revision after 0099, got {hits}"
    rev = _load(str(hits[0].relative_to(_BACKEND)), "ent935_rev")

    executed: list[str] = []

    class _Op:
        def execute(self, sql):
            executed.append(" ".join(str(sql).split()))

    rev.op = _Op()
    rev.upgrade()
    sql = "\n".join(executed)
    assert "schedule_executions ADD COLUMN IF NOT EXISTS parent_execution_id" in sql
    for col in ("caller_agent", "parent_execution_id", "delegation_mode"):
        assert f"agent_evaluations ADD COLUMN IF NOT EXISTS {col}" in sql
    assert ("idx_agent_evaluations_caller ON agent_evaluations"
            "(caller_agent, agent_name, created_at)") in sql


def test_tables_model_declares_the_columns():
    from db.tables import agent_evaluations, schedule_executions
    assert "parent_execution_id" in schedule_executions.c
    for col in ("caller_agent", "parent_execution_id", "delegation_mode"):
        assert col in agent_evaluations.c
