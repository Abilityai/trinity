"""Telegram group conversation context (ent#600)

Three additive columns. PostgreSQL half of the dual-track pair; the SQLite half
is ``db/migrations.py::telegram_group_context``.

* ``telegram_bindings.can_read_all_group_messages`` — Telegram's ``getMe`` fact
  (Privacy Mode off ⇒ 1). Nullable: NULL means "never checked".
* ``telegram_group_configs.last_untagged_seen_at`` — when an un-tagged message
  last reached the bot in that group; the per-group proof it sees the
  conversation.
* ``telegram_group_configs.context_enabled`` — per-group opt-out for the
  recorded context. ``server_default="1"`` populates existing rows at
  migration time (every current group gets context ON), so no backfill UPDATE.

Revision ID: 0059_telegram_group_context
Revises: 0058_portal_file_dismissals
Create Date: 2026-09-11
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0059_telegram_group_context"
down_revision = "0058_portal_file_dismissals"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    # A fresh PostgreSQL database is built from `db/schema.py`'s DDL, which
    # already declares these columns — guard on existence rather than add blindly.
    if not _has_column(bind, "telegram_bindings", "can_read_all_group_messages"):
        op.add_column(
            "telegram_bindings",
            sa.Column("can_read_all_group_messages", sa.Integer(), nullable=True),
        )
    if not _has_column(bind, "telegram_group_configs", "last_untagged_seen_at"):
        op.add_column(
            "telegram_group_configs",
            sa.Column("last_untagged_seen_at", sa.Text(), nullable=True),
        )
    if not _has_column(bind, "telegram_group_configs", "context_enabled"):
        op.add_column(
            "telegram_group_configs",
            sa.Column("context_enabled", sa.Integer(), nullable=True, server_default="1"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "telegram_group_configs", "context_enabled"):
        op.drop_column("telegram_group_configs", "context_enabled")
    if _has_column(bind, "telegram_group_configs", "last_untagged_seen_at"):
        op.drop_column("telegram_group_configs", "last_untagged_seen_at")
    if _has_column(bind, "telegram_bindings", "can_read_all_group_messages"):
        op.drop_column("telegram_bindings", "can_read_all_group_messages")
