"""A person seated in a room reads it from their join point (trinity-enterprise#701)

One additive column, ``enterprise_room_participants.visible_from_seq``: the
first message seq the participant may read. ``server_default`` 0 keeps every
existing row reading the whole transcript. PostgreSQL half of the dual-track
pair; the SQLite half is ``db/migrations.py::room_participant_visible_from``.

Revision ID: 0108_room_participant_visible_from
Revises: 0107_skill_sources_follow_bundled_pin
Create Date: 2026-10-26
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0108_room_participant_visible_from"
down_revision = "0107_skill_sources_follow_bundled_pin"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    # A fresh PostgreSQL database is built from `db/schema.py`'s DDL, which
    # already declares the column — guard on existence rather than add blindly.
    if not _has_column(bind, "enterprise_room_participants", "visible_from_seq"):
        op.add_column(
            "enterprise_room_participants",
            sa.Column("visible_from_seq", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    op.drop_column("enterprise_room_participants", "visible_from_seq")
