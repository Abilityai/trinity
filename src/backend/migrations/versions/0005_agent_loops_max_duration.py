"""agent_loops.max_duration_seconds (#1156)

Adds the loop-level wall-clock deadline column on the PostgreSQL backend.
Mirrors the SQLite ``agent_loops_max_duration`` migration in
``db/migrations.py`` and the DDL in ``db/schema.py``.

Fresh PG builds already get this column because ``0001_baseline`` iterates
``db/schema.py:TABLES`` (whose ``agent_loops`` DDL now includes it). This
revision exists so an *existing* PG deployment — stamped at an earlier revision
and never re-running baseline — also picks the column up on
``alembic upgrade head``. ``ADD COLUMN IF NOT EXISTS`` keeps it a no-op when the
baseline already created the table with the column.

Revision ID: 0005_agent_loops_max_duration
Revises: 0004_agent_ownership_voice_name
Create Date: 2026-06-23
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0005_agent_loops_max_duration"
down_revision = "0004_agent_ownership_voice_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_loops ADD COLUMN IF NOT EXISTS max_duration_seconds INTEGER"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE agent_loops DROP COLUMN IF EXISTS max_duration_seconds"
    )
