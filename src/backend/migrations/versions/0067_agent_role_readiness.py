"""agent_role_readiness — the agent owner's readiness stamp for a role companion.

PostgreSQL half of the dual-track pair (trinity-enterprise#527 / #663); the
SQLite half is ``db/migrations.py::agent_role_readiness_table``.

``template.yaml``'s ``x-role.status`` is agent-writable, and the 2026-09-20
ruling is that only the agent OWNER flips a companion ``calibrating → ready``
and the agent never can — so the stamp lives platform-side: one row per agent
with the state, when it changed and who flipped it.

``agent_name`` is a CASCADE entry in ``db/agent_cleanup.py``.

Revision ID: 0067_agent_role_readiness
Revises: 0066_public_user_memory_writes
"""
from alembic import op
import sqlalchemy as sa

revision = "0067_agent_role_readiness"
down_revision = "0066_public_user_memory_writes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A fresh PostgreSQL database is built from `db/schema.py`'s DDL — which
    # declares this table — and only THEN runs the revisions, so guard on
    # existence rather than create blindly (0046-0058's rule).
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("agent_role_readiness"):
        op.create_table(
            "agent_role_readiness",
            sa.Column("agent_name", sa.Text(), primary_key=True),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("changed_at", sa.Text(), nullable=False),
            sa.Column("changed_by", sa.Text(), nullable=False),
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_role_readiness")
