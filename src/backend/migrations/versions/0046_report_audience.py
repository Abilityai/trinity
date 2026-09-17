"""Reports gain an audience and the chat that produced them (ent#365).

The PostgreSQL half of `db/migrations.py::_migrate_report_audience`. Both
columns are nullable with no default, so every existing row keeps its meaning:
an operator-scoped report, tied to no chat.

Revision ID: 0046_report_audience
Revises: 0045_merge_rooms_hotfix
"""
from alembic import op
import sqlalchemy as sa

revision = "0046_report_audience"
down_revision = "0045_merge_rooms_hotfix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS` because an install may have been built from the current
    # `db/schema.py` DDL rather than migrated up to it.
    op.execute("ALTER TABLE agent_reports ADD COLUMN IF NOT EXISTS addressed_to_email TEXT")
    op.execute("ALTER TABLE agent_reports ADD COLUMN IF NOT EXISTS portal_session_id TEXT")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_reports_audience "
        "ON agent_reports(addressed_to_email, agent_name, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_reports_portal_session "
        "ON agent_reports(portal_session_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agent_reports_portal_session")
    op.execute("DROP INDEX IF EXISTS idx_agent_reports_audience")
    op.execute("ALTER TABLE agent_reports DROP COLUMN IF EXISTS portal_session_id")
    op.execute("ALTER TABLE agent_reports DROP COLUMN IF EXISTS addressed_to_email")
