"""per-binding toggle for the Telegram in-progress indicator (ent#264)

Adds ``progress_indicator_enabled`` to ``telegram_bindings``. Default ON for
everyone (the AC) — server_default="1" populates existing rows at migration
time, so no backfill UPDATE is needed (unlike ent#223's deny-for-new posture
on ``slack_channel_agents.allow_proactive``). The Python-side
``v is None or v != 0`` read predicate is defense-in-depth for edge writes,
not the backfill mechanism.

Identical to the SQLite track in ``db/migrations.py``
(``telegram_progress_indicator``).

Revision ID: 0032_telegram_progress_indicator
Revises: 0031_channel_report_back
Create Date: 2026-07-31
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0032_telegram_progress_indicator"
down_revision = "0031_channel_report_back"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "telegram_bindings", "progress_indicator_enabled"):
        return
    op.add_column(
        "telegram_bindings",
        sa.Column(
            "progress_indicator_enabled",
            sa.Integer(),
            nullable=True,
            server_default="1",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "telegram_bindings", "progress_indicator_enabled"):
        op.drop_column("telegram_bindings", "progress_indicator_enabled")
