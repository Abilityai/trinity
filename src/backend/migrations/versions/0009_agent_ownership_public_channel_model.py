"""agent_ownership.public_channel_model (#894)

Adds the per-agent public-channel model override column on the PostgreSQL
backend. Mirrors the SQLite ``agent_ownership_public_channel_model`` migration in
``db/migrations.py`` and the DDL in ``db/schema.py``.

Fresh PG builds already get this column because ``0001_baseline`` iterates
``db/schema.py:TABLES`` (whose ``agent_ownership`` DDL now includes it). This
revision exists so an *existing* PG deployment — stamped at an earlier revision
and never re-running baseline — also picks the column up on
``alembic upgrade head``. ``ADD COLUMN IF NOT EXISTS`` keeps it a no-op when the
baseline already created the table with the column.

Revision ID: 0009_agent_ownership_public_channel_model
Revises: 0008_agent_loops_max_cost
Create Date: 2026-06-29
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0009_agent_ownership_public_channel_model"
# #1420: rechained onto the alembic_version widen so the column is VARCHAR(255)
# before this 41-char revision id is stamped (was 0008_agent_loops_max_cost).
down_revision = "0008a_widen_alembic_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_ownership ADD COLUMN IF NOT EXISTS public_channel_model TEXT"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE agent_ownership DROP COLUMN IF EXISTS public_channel_model"
    )
