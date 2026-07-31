"""channel completion report-back — Telegram leg + binding identity (ent#265)

Two columns, one atomic revision (they ship as one feature):

* ``schedule_executions.source_channel_agent`` (nullable TEXT) — "the agent
  whose channel binding owns this execution's inherited context". Written only
  at the /task row-creation point when channel context is inherited from a
  parent execution; NULL for direct rows (the completion reporter falls back to
  the executing agent — byte-identical legacy behavior).

* ``telegram_group_configs.allow_proactive`` (INTEGER, server_default 1) — per-
  group consent for completion reports, the Telegram analog of ent#223's Slack
  channel-binding flag. ``server_default="1"`` fills existing rows on ADD
  COLUMN, so no backfill UPDATE is needed. Allow is the deliberate default
  (unlike Slack's new-deny split): the reporter posts only into chats that
  initiated the work, and the toggle is an opt-out mute.

Identical to the SQLite track's ``channel_report_back_columns`` entry in
``db/migrations.py``.

Revision ID: 0031_channel_report_back
Revises: 0030_slack_channel_allow_proactive
Create Date: 2026-07-30
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0031_channel_report_back"
down_revision = "0030_slack_channel_allow_proactive"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "schedule_executions", "source_channel_agent"):
        op.add_column(
            "schedule_executions",
            sa.Column("source_channel_agent", sa.Text(), nullable=True),
        )
    if not _has_column(bind, "telegram_group_configs", "allow_proactive"):
        op.add_column(
            "telegram_group_configs",
            sa.Column(
                "allow_proactive", sa.Integer(), nullable=True, server_default="1"
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "telegram_group_configs", "allow_proactive"):
        op.drop_column("telegram_group_configs", "allow_proactive")
    if _has_column(bind, "schedule_executions", "source_channel_agent"):
        op.drop_column("schedule_executions", "source_channel_agent")
