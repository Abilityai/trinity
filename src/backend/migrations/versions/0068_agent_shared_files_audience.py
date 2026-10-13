"""ent#549 — a shared file is for the person the turn was for.

``agent_shared_files`` was scoped by agent alone, so the Workspace Files tab
listed every active share of an agent to everyone on its roster. Three nullable
columns say who a file is for: ``addressed_to_email`` decides whose Files tab
lists it, ``addressed_to_channel`` is the channel identity the owner's panel
shows (display only — nothing filters on it), and ``audience_source`` records
how the addressee was decided.

No default and no backfill, on purpose: NULL email + NULL channel means "the
owner only", which is what an existing row has to become — its recipient is
unknowable, and every share expires within seven days.

Mirrors the SQLite ``agent_shared_files_audience`` migration.

Revision ID: 0068_agent_shared_files_audience
Revises: 0067_agent_role_readiness
"""
from alembic import op


revision = "0068_agent_shared_files_audience"
down_revision = "0067_agent_role_readiness"
branch_labels = None
depends_on = None

_COLUMNS = ("addressed_to_email", "addressed_to_channel", "audience_source")


def upgrade() -> None:
    for column in _COLUMNS:
        op.execute(f"ALTER TABLE agent_shared_files ADD COLUMN IF NOT EXISTS {column} TEXT")


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE agent_shared_files DROP COLUMN IF EXISTS {column}")
