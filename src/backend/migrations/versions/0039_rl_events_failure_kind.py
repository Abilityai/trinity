"""subscription_rate_limit_events.failure_kind — split 429s from auth failures (#471)

Adds the nullable ``failure_kind`` column ("rate_limit" | "auth") on PostgreSQL.
The SUB-003 writer has carried the param since #441/#792 but never persisted it,
so the table conflated broken-token auth failures with genuine quota 429s. NULL
= pre-#471 row (kind unknown; the 24h sweep retires these within a day).
Mirrors the SQLite ``rate_limit_events_failure_kind`` migration +
``db/schema.py`` / ``db/tables.py``.

Revision ID: 0039_rl_events_failure_kind
Revises: 0038_portal_chat_state
Create Date: 2026-08-19
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0039_rl_events_failure_kind"
down_revision = "0038_portal_chat_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE subscription_rate_limit_events ADD COLUMN IF NOT EXISTS failure_kind TEXT"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE subscription_rate_limit_events DROP COLUMN IF EXISTS failure_kind"
    )
