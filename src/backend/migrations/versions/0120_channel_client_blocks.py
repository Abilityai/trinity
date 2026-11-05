"""trinity-enterprise#21 — an owner's per-agent block on one external channel
client.

``channel_client_blocks`` holds one row per (agent, channel, client id) the
agent's owner has blocked; the channel access gate checks it on every message.
``client_id`` is the channel-native sender id the gate sees.

Mirrors the SQLite ``channel_client_blocks`` migration. Chained after 0119_terms_acceptances so
the version-line keeps ONE head (#2068).

Revision ID: 0120_channel_client_blocks
Revises: 0119_terms_acceptances
"""
import sqlalchemy as sa
from alembic import op


revision = "0120_channel_client_blocks"
down_revision = "0119_terms_acceptances"
branch_labels = None
depends_on = None

_TABLE = "channel_client_blocks"


def upgrade() -> None:
    # Guarded: 0001_baseline builds every table from the live db/tables.py
    # metadata, so on a FRESH PostgreSQL database it already exists here.
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("agent_name", sa.Text(), nullable=False),
            sa.Column("channel", sa.Text(), nullable=False),
            sa.Column("client_id", sa.Text(), nullable=False),
            sa.Column("blocked_at", sa.Text(), nullable=False),
            sa.Column("blocked_by_id", sa.Integer()),
            sa.Column("blocked_by_email", sa.Text()),
            sa.Column("reason", sa.Text()),
            sa.UniqueConstraint("agent_name", "channel", "client_id"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(_TABLE):
        op.drop_table(_TABLE)
