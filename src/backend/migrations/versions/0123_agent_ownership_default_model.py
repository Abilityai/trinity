"""ent#1202 — agent_ownership.default_model (the agent's own model).

NULL = no opinion: a turn that names no model runs on the platform default,
exactly as before. A set value is stored as written and checked against the
model catalog on read (``services/agent_model_service.py``).

Mirrors the SQLite ``agent_ownership_default_model`` migration. Chained after
0122 so the version-line keeps ONE head (#2068).

Revision ID: 0123_agent_ownership_default_model
Revises: 0122_channel_client_blocks
"""
import sqlalchemy as sa
from alembic import op


revision = "0123_agent_ownership_default_model"
down_revision = "0122_channel_client_blocks"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    # Guarded for the same reason as 0042/0109/0121: 0001_baseline already
    # carries the column on a database built fresh from the current DDL.
    bind = op.get_bind()
    if not _has_column(bind, "agent_ownership", "default_model"):
        op.add_column(
            "agent_ownership",
            sa.Column("default_model", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "agent_ownership", "default_model"):
        op.drop_column("agent_ownership", "default_model")
