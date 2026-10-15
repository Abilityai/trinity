"""abilityai/trinity-enterprise#693 — an execution names the kind of key that
started it.

One nullable column, ``source_mcp_scope``, on ``schedule_executions``: the scope
of ``source_mcp_key_id``, resolved at write time so a controller-started run
stays attributed to the controller after its key is deleted.

Additive, no backfill: old rows stay NULL.

Mirrors the SQLite ``execution_source_mcp_scope`` migration.

Revision ID: 0099_execution_source_mcp_scope
Revises: 0098_proactive_consent_not_asked
"""
from alembic import op


revision = "0099_execution_source_mcp_scope"
down_revision = "0098_proactive_consent_not_asked"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS`, as in 0095: a fresh PostgreSQL database is built from
    # `db/schema.py`'s DDL, which already declares the column, before the
    # revisions run.
    op.execute("ALTER TABLE schedule_executions ADD COLUMN IF NOT EXISTS source_mcp_scope TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE schedule_executions DROP COLUMN IF EXISTS source_mcp_scope")
