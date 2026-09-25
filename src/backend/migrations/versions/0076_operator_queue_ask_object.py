"""trinity-enterprise#611 — the ask object: how an ask ended, and who raised it.

Twelve nullable TEXT columns on ``operator_queue``, one revision for everything
#611 writes.

The endings ledger: ``disposition`` (answered | cancelled | expired),
``disposed_at``, ``disposed_by`` (person | timeout), ``disposed_by_email``,
``disposition_reason`` and ``batch_id`` (one uuid per bulk-cancel sweep).

The agent-raised ask: ``raised_by`` (agent | gate), ``channel`` (file | mcp),
``to_role``, ``resolved_to`` (JSON), ``proposal`` (JSON) and
``supersedes_expired`` (the predecessor row's uuid).

No default and no backfill: a row that ended before the ledger keeps a NULL
disposition and reads from ``status``.

Mirrors the SQLite ``operator_queue_ask_object`` migration.

Revision ID: 0076_operator_queue_ask_object
Revises: 0075_auto_sync_enabled_backfill
"""
from alembic import op


revision = "0076_operator_queue_ask_object"
down_revision = "0075_auto_sync_enabled_backfill"
branch_labels = None
depends_on = None

_COLUMNS = (
    "disposition",
    "disposed_at",
    "disposed_by",
    "disposed_by_email",
    "disposition_reason",
    "batch_id",
    "raised_by",
    "channel",
    "to_role",
    "resolved_to",
    "proposal",
    "supersedes_expired",
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.execute(f"ALTER TABLE operator_queue ADD COLUMN IF NOT EXISTS {column} TEXT")


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE operator_queue DROP COLUMN IF EXISTS {column}")
