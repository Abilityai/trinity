"""ent#1102 — agent_ownership.wake_on_ending (tri-state per-agent override).

NULL = no opinion, follow the instance default ``wake_on_ending_default`` (ON);
0 = never wake this agent when a run it delegated ends; 1 = always wake it.

Nullable with NO server default, unlike every boolean beside it: that is what
makes "the owner said off" distinguishable from "nobody ever touched this", and
distinguishing them is the whole reason ent#790's default could be flipped on
without discarding an owner's earlier decision.

Mirrors the SQLite ``agent_ownership_wake_on_ending`` migration. Chained after
0120 so the version-line keeps ONE head (#2068).

Revision ID: 0121_agent_ownership_wake_on_ending
Revises: 0120_public_user_memory_writes_section
"""
import sqlalchemy as sa
from alembic import op


revision = "0121_agent_ownership_wake_on_ending"
down_revision = "0120_public_user_memory_writes_section"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    # Guarded for the same reason as 0042/0109: 0001_baseline already carries
    # the column on a database built fresh from the current DDL.
    bind = op.get_bind()
    if not _has_column(bind, "agent_ownership", "wake_on_ending"):
        op.add_column(
            "agent_ownership",
            sa.Column("wake_on_ending", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "agent_ownership", "wake_on_ending"):
        op.drop_column("agent_ownership", "wake_on_ending")
