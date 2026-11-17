"""access_requests requester identity (trinity-enterprise#737).

An access request records who asked, as the channel saw them: the channel's
stable user id, the handle, the display name, and the chat / thread the request
came from. All nullable — a field the channel does not expose stays empty.

Mirrors the SQLite ``access_requests_requester_identity`` migration. Chained
after 0114 so the version-line keeps ONE head (#2068).

Revision ID: 0115_access_requests_requester_identity
Revises: 0114_room_participant_visible_from
"""
import sqlalchemy as sa
from alembic import op


revision = "0115_access_requests_requester_identity"
down_revision = "0114_room_participant_visible_from"
branch_labels = None
depends_on = None

_COLUMNS = ("requester_channel_id", "requester_handle", "requester_name", "requester_chat_id", "requester_thread_id")


def _columns(bind) -> set:
    return {c["name"] for c in sa.inspect(bind).get_columns("access_requests")}


def upgrade() -> None:
    # Guarded: 0001_baseline builds every table from the live db/tables.py
    # metadata, so on a FRESH PostgreSQL database the columns already exist.
    bind = op.get_bind()
    have = _columns(bind)
    for column in _COLUMNS:
        if column not in have:
            op.add_column("access_requests", sa.Column(column, sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    have = _columns(bind)
    for column in _COLUMNS:
        if column in have:
            op.drop_column("access_requests", column)
