"""trinity-enterprise#1022 — agent_ownership.memory_opt_out (per-agent opt-out).

The platform switch (``trinity_memory_enabled``, default off) gates retrieval
memory globally; this column lets an owner keep one agent out once the platform
is on. Default 0 = follows the platform.

Mirrors the SQLite ``agent_ownership_memory_opt_out`` migration. Chained after
0108 so the version-line keeps ONE head (#2068).

Revision ID: 0109_agent_ownership_memory_opt_out
Revises: 0108_agent_memory_facts
"""
import sqlalchemy as sa
from alembic import op


revision = "0109_agent_ownership_memory_opt_out"
down_revision = "0108_agent_memory_facts"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    # Guarded for the same reason as 0042: 0001_baseline already carries the
    # column on a fresh PostgreSQL database.
    bind = op.get_bind()
    if not _has_column(bind, "agent_ownership", "memory_opt_out"):
        op.add_column(
            "agent_ownership",
            sa.Column("memory_opt_out", sa.Integer(), nullable=True, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "agent_ownership", "memory_opt_out"):
        op.drop_column("agent_ownership", "memory_opt_out")
