"""agent_canvases — the durable agent canvas (ent#438).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::agent_canvases_table``. One row per
(agent_name, canvas_id) — the composite primary key is what makes a canvas
write an upsert rather than an append, and what makes the surface addressable.

Revision ID: 0050_agent_canvases
Revises: 0049_execution_turn_integrity
"""
from alembic import op
import sqlalchemy as sa

revision = "0050_agent_canvases"
down_revision = "0049_execution_turn_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("agent_canvases"):
        return
    op.create_table(
        "agent_canvases",
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("canvas_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("blocks", sa.Text(), nullable=False),
        sa.Column("audience", sa.Text(), nullable=False, server_default="operator"),
        sa.Column("schema_version", sa.Integer(), server_default="1"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("updated_by_execution_id", sa.Text()),
        sa.PrimaryKeyConstraint("agent_name", "canvas_id"),
    )
    op.create_index(
        "idx_agent_canvases_agent",
        "agent_canvases",
        ["agent_name", sa.text("updated_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_agent_canvases_agent", table_name="agent_canvases")
    op.drop_table("agent_canvases")
