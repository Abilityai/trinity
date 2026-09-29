"""agent_sync_state divergence / dirt episode columns (trinity-enterprise#706)

Adds four nullable columns on the PostgreSQL backend:

- ``diverged_since``  — first poll at which the working tuple (origin on the
  agent's own branch) showed ahead or behind; cleared when both return to 0.
- ``dirty_files``     — the porcelain change count from ``GET /api/git/status``.
- ``dirty_since``     — first poll at which ``dirty_files > 0``; cleared at 0.
- ``last_successful_push_at`` — the last push that landed (heartbeat or an
  operator Push).

No backfill: the clocks cannot be known retroactively, and starting them at the
first post-upgrade poll is the 24 h soak before any divergence freeze fires.
DDL-only, so ``downgrade()`` is a clean drop.

Mirrors the SQLite ``agent_sync_state_divergence`` migration in
``db/migrations.py`` and the DDL in ``db/schema.py`` / MetaData in
``db/tables.py``. Fresh PG builds already get the columns via
``0001_baseline``'s reuse of the schema DDL; ``ADD COLUMN IF NOT EXISTS`` keeps
this a no-op there.

Revision ID: 0082_agent_sync_state_divergence
Revises: 0081_portal_messages_unread_idx
Create Date: 2026-09-27
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0082_agent_sync_state_divergence"
down_revision = "0081_portal_messages_unread_idx"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_sync_state ADD COLUMN IF NOT EXISTS diverged_since TEXT"
    )
    op.execute(
        "ALTER TABLE agent_sync_state ADD COLUMN IF NOT EXISTS dirty_files INTEGER"
    )
    op.execute(
        "ALTER TABLE agent_sync_state ADD COLUMN IF NOT EXISTS dirty_since TEXT"
    )
    op.execute(
        "ALTER TABLE agent_sync_state "
        "ADD COLUMN IF NOT EXISTS last_successful_push_at TEXT"
    )

def downgrade() -> None:
    op.execute("ALTER TABLE agent_sync_state DROP COLUMN IF EXISTS diverged_since")
    op.execute("ALTER TABLE agent_sync_state DROP COLUMN IF EXISTS dirty_files")
    op.execute("ALTER TABLE agent_sync_state DROP COLUMN IF EXISTS dirty_since")
    op.execute(
        "ALTER TABLE agent_sync_state DROP COLUMN IF EXISTS last_successful_push_at"
    )
