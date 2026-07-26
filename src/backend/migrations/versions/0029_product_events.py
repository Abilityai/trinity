"""product_events table (ent#184)

Local product-event capture — activation funnel, Tier-1. Local-only, default-on,
zero network egress. Records onboarding-wizard step transitions; first-value
events (first_agent_created, first_chat, ...) are derived on read from
audit_log/agent_activities, never re-emitted. Mirrors the SQLite
``product_events_table`` migration in ``db/migrations.py`` and the DDL in
``db/schema.py`` / MetaData in ``db/tables.py``.

Fresh PG builds already get this table because ``0001_baseline`` iterates
``db/schema.py:TABLES``. This revision exists so an *existing* PG deployment —
stamped at an earlier revision and never re-running baseline — also picks the
table up on ``alembic upgrade head``. ``CREATE TABLE IF NOT EXISTS`` /
``CREATE INDEX IF NOT EXISTS`` keep it a no-op when baseline already created it.

Revision ID: 0029_product_events
Revises: 0028_agent_reminders
Create Date: 2026-07-21
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0029_product_events"
down_revision = "0028_agent_reminders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS product_events (
            id SERIAL PRIMARY KEY,
            installation_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_context TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_product_events_type_created "
        "ON product_events(event_type, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_product_events_created "
        "ON product_events(created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS product_events")
