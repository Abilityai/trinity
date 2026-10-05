"""supersede_queue_flood_backlog — one pending flood alert per agent (#3130).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::supersede_queue_flood_backlog``. Both run the same
statement (``SUPERSEDE_QUEUE_FLOOD_BACKLOG_SQL``), so the survivor rule cannot
drift between them: per agent, the newest pending ``queue-flood-`` row (by
``created_at``, then ``id``) stays; the rest end ``cancelled`` with
``disposed_by = 'platform'`` and reason ``superseded``. Data only — no DDL.

Revision ID: 0089_supersede_queue_flood_backlog
Revises: 0088_skill_gate_requests
"""
import uuid

from alembic import op
from sqlalchemy import text


revision = "0089_supersede_queue_flood_backlog"
down_revision = "0088_skill_gate_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from db.migrations import SUPERSEDE_QUEUE_FLOOD_BACKLOG_SQL
    from utils.helpers import utc_now_iso

    op.get_bind().execute(
        text(SUPERSEDE_QUEUE_FLOOD_BACKLOG_SQL),
        {"now": utc_now_iso(), "batch_id": uuid.uuid4().hex},
    )


def downgrade() -> None:
    # Deliberately a no-op: the inverse is "re-open hundreds of duplicate
    # platform alarms", which no rollback wants.
    pass
