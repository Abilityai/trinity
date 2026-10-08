"""#3247 — an agent replaces one of its OWN pending asks.

Two nullable TEXT link columns on ``operator_queue``: ``replaces`` on the
successor (the predecessor row's uuid) and ``replaced_by`` on the predecessor
(the successor row's uuid), both stamped in the one locked transaction that
ends the predecessor (``cancelled`` / ``disposed_by='agent'`` /
``disposition_reason='replaced'``) and inserts the successor.

No default, no backfill, no index. Mirrors the SQLite
``operator_queue_replace`` migration.

Revision ID: 0097_operator_queue_replace
Revises: 0096_a2a_internal_scope
"""
from alembic import op


revision = "0097_operator_queue_replace"
down_revision = "0096_a2a_internal_scope"
branch_labels = None
depends_on = None

_COLUMNS = (
    "replaces",
    "replaced_by",
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.execute(f"ALTER TABLE operator_queue ADD COLUMN IF NOT EXISTS {column} TEXT")


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE operator_queue DROP COLUMN IF EXISTS {column}")
