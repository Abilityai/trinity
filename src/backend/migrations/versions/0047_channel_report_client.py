"""ent#457 review — record WHICH client a portal channel context belongs to.

`source_channel_agent` (ent#265) records the agent whose binding owns the
context; nothing recorded the human. The portal leg files into a per-client
thread, and its authorization to do so came from a guard that checks the AGENT
only — so an agent shared with two clients could cite one client's execution id
while serving the other and route a report into the wrong person's thread.

Nullable, no default: pre-existing rows report NULL and the portal resolver
fails CLOSED on NULL rather than delivering unverified.

Mirrors the SQLite `channel_report_client` migration.

Revision ID: 0047_channel_report_client
Revises: 0046_report_audience
"""
from alembic import op
import sqlalchemy as sa


revision = "0047_channel_report_client"
down_revision = "0046_report_audience"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "schedule_executions",
        sa.Column("source_channel_client", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("schedule_executions", "source_channel_client")
