"""per-channel proactive consent for Slack (ent#223)

Adds ``allow_proactive`` to ``slack_channel_agents``. In an open Slack workspace
users never authenticate, so the per-recipient consent model
(``agent_sharing.allow_proactive``, keyed by verified email) has nobody to key
on; for Slack the consent unit is the CHANNEL BINDING.

Default posture (identical to the SQLite track in ``db/migrations.py``):
  * NEW bindings default to 0 (deny) — binding an agent to a channel is not by
    itself consent to unprompted posts.
  * EXISTING bindings are backfilled to 1 (allow) — channel posts had no consent
    gate before this migration, so leaving them at 0 would silently break every
    working integration.

Revision ID: 0030_slack_channel_allow_proactive
Revises: 0029_product_events
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0030_slack_channel_allow_proactive"
down_revision = "0029_product_events"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "slack_channel_agents", "allow_proactive"):
        return
    op.add_column(
        "slack_channel_agents",
        sa.Column("allow_proactive", sa.Integer(), nullable=True, server_default="0"),
    )
    # Preserve today's behavior for bindings that already exist (no silent flip).
    op.execute(
        "UPDATE slack_channel_agents SET allow_proactive = 1 "
        "WHERE allow_proactive IS NULL OR allow_proactive = 0"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "slack_channel_agents", "allow_proactive"):
        op.drop_column("slack_channel_agents", "allow_proactive")
