"""public_user_memory_writes — write history for the per-user memory's agent_notes.

PostgreSQL half of the dual-track pair (trinity-enterprise#637); the SQLite half
is ``db/migrations.py::public_user_memory_writes_table``.

A schedule that names a user (ent#498's ``deliver_to_workspace_email``) may now
write that user's MEM-001 memory from the run it triggers. The person must be
able to see that a scheduled run touched their memory — what, when, which run —
and undo it, so every agent-notes write through the one boundary
(``POST /api/agents/{name}/user-memory``) records the notes before and after,
the execution, its trigger and the schedule. Also ent#419's third layer (write
history with rollback).

``agent_name`` is a CASCADE entry in ``db/agent_cleanup.py`` so the rows follow
the agent's lifecycle like ``public_user_memory`` itself.

Revision ID: 0066_public_user_memory_writes
Revises: 0065_agent_skills_delivery_status
"""
from alembic import op
import sqlalchemy as sa

revision = "0066_public_user_memory_writes"
down_revision = "0065_agent_skills_delivery_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A fresh PostgreSQL database is built from `db/schema.py`'s DDL — which
    # declares this table — and only THEN runs the revisions (0046-0058's rule),
    # so guard on existence rather than create blindly.
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("public_user_memory_writes"):
        op.create_table(
            "public_user_memory_writes",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("agent_name", sa.Text(), nullable=False),
            sa.Column("user_email", sa.Text(), nullable=False),
            sa.Column("execution_id", sa.Text(), nullable=True),
            sa.Column("triggered_by", sa.Text(), nullable=False),
            sa.Column("schedule_id", sa.Text(), nullable=True),
            sa.Column("previous_notes", sa.Text(), nullable=False, server_default=""),
            sa.Column("new_notes", sa.Text(), nullable=False, server_default=""),
            sa.Column("written_at", sa.Text(), nullable=False),
            sa.Column("undone_at", sa.Text(), nullable=True),
            sa.Column("undone_by", sa.Text(), nullable=True),
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_public_user_memory_writes_lookup "
        "ON public_user_memory_writes(agent_name, user_email, written_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_public_user_memory_writes_lookup")
    op.execute("DROP TABLE IF EXISTS public_user_memory_writes")
