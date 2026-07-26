"""agent_reminders table (#1296)

Durable one-shot deferred self-trigger: while running, an agent schedules a
future re-invocation of itself with a message it picks. The standalone scheduler
arms an APScheduler ``DateTrigger`` per pending row and, on fire, dispatches a
normal execution of the same agent (``triggered_by="reminder"``). Mirrors the
SQLite ``agent_reminders_table`` migration in ``db/migrations.py`` and the DDL in
``db/schema.py`` / MetaData in ``db/tables.py``.

Fresh PG builds already get this table because ``0001_baseline`` iterates
``db/schema.py:TABLES``. This revision exists so an *existing* PG deployment —
stamped at an earlier revision and never re-running baseline — also picks the
table up on ``alembic upgrade head``. ``CREATE TABLE IF NOT EXISTS`` /
``CREATE INDEX IF NOT EXISTS`` keep it a no-op when baseline already created it.

Revision ID: 0028_agent_reminders
Revises: 0027_users_github_pat
Create Date: 2026-07-18
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0028_agent_reminders"
down_revision = "0027_users_github_pat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_reminders (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            message TEXT NOT NULL,
            fire_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            model TEXT,
            timeout_seconds INTEGER,
            allowed_tools TEXT,
            owner_id INTEGER,
            created_by_email TEXT,
            source_agent_name TEXT,
            source_mcp_key_id TEXT,
            execution_id TEXT,
            fire_attempts INTEGER NOT NULL DEFAULT 0,
            firing_at TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            fired_at TEXT,
            cancelled_at TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_reminders_agent "
        "ON agent_reminders(agent_name)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_reminders_active "
        "ON agent_reminders(fire_at) WHERE status IN ('pending', 'firing')"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_reminders")
