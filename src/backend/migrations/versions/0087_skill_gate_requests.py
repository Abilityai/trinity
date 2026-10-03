"""trinity-enterprise#751 — a gated-skill request frozen while its approval is open.

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::skill_gate_requests_table``.

The ask (``operator_queue``) records the DECISION; this row records the EFFECT,
so an approval runs the request exactly once: ``pending → dispatching`` is a
compare-and-set and ``dispatched_execution_id`` is UNIQUE.

``IF NOT EXISTS`` because a fresh database is built from ``db/schema.py``'s DDL
(``0001_baseline``), which already declares the table.

Revision ID: 0087_skill_gate_requests
Revises: 0086_metric_points_restatement
"""
from alembic import op


revision = "0087_skill_gate_requests"
down_revision = "0086_metric_points_restatement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_gate_requests (
            request_id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            ask_item_id TEXT,
            skills TEXT NOT NULL,
            request_text TEXT NOT NULL,
            fingerprints TEXT,
            requester_kind TEXT NOT NULL,
            requester_key TEXT NOT NULL,
            source_agent TEXT,
            requester_email TEXT,
            requester_execution_id TEXT,
            requester_mcp_key_id TEXT,
            origin_execution_id TEXT,
            triggered_by TEXT,
            dispatch TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            state_detail TEXT,
            dispatched_execution_id TEXT UNIQUE,
            created_at TEXT NOT NULL,
            decided_at TEXT,
            dispatched_at TEXT,
            notified_at TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_gate_requests_agent_state "
        "ON skill_gate_requests(agent_name, state)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_gate_requests_requester "
        "ON skill_gate_requests(agent_name, requester_key, state)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_gate_requests_state "
        "ON skill_gate_requests(state, decided_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS skill_gate_requests")
