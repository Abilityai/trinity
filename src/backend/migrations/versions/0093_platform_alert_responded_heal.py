"""platform_alert_responded_heal — acknowledged platform alerts leave `responded` (#2372).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::platform_alert_responded_heal``. Both use the same
statements and the same id filter (``platform_alert_heal_ids``), so the set of
healed rows cannot drift between them: every ``responded`` row whose
``request_id`` carries a platform prefix moves to ``acknowledged`` with
``acknowledged_at`` = now. Data only — no DDL.

Revision ID: 0093_platform_alert_responded_heal
Revises: 0092_portal_messages_attachments
"""
from alembic import op
from sqlalchemy import text


revision = "0093_platform_alert_responded_heal"
down_revision = "0092_portal_messages_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from db.migrations import (
        PLATFORM_ALERT_HEAL_SELECT_SQL,
        PLATFORM_ALERT_HEAL_UPDATE_SQL,
        platform_alert_heal_ids,
    )
    from utils.helpers import utc_now_iso

    bind = op.get_bind()
    now = utc_now_iso()
    rows = bind.execute(text(PLATFORM_ALERT_HEAL_SELECT_SQL)).fetchall()
    for row_id in platform_alert_heal_ids(rows):
        bind.execute(text(PLATFORM_ALERT_HEAL_UPDATE_SQL), {"now": now, "id": row_id})


def downgrade() -> None:
    # Deliberately a no-op: the inverse parks acknowledged alerts back in a
    # state nothing ever leaves.
    pass
