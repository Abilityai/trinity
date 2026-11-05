"""mcp_external_access_backfill — existing installs keep external MCP access on
(abilityai/trinity-enterprise#1101).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::mcp_external_access_backfill``. Both run the shared
``run_mcp_external_access_backfill``: no ``mcp_external_access`` row AND at
least one ``users`` row → insert ``'true'``; otherwise nothing. A fresh
database has no users at migration time, so it gets the default (off). An
existing row is never touched. Data only — no DDL. Chained after 0117 so the
version-line keeps ONE head (#2068).

Revision ID: 0118_mcp_external_access_backfill
Revises: 0117_operator_queue_wake_state
"""
from alembic import op
from sqlalchemy import text


revision = "0118_mcp_external_access_backfill"
down_revision = "0117_operator_queue_wake_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from db.migrations import run_mcp_external_access_backfill
    from utils.helpers import utc_now_iso

    bind = op.get_bind()

    def _run(sql, params, fetch=False):
        result = bind.execute(text(sql), params)
        return result.fetchall() if fetch else None

    run_mcp_external_access_backfill(_run, now_iso=utc_now_iso())


def downgrade() -> None:
    # Deliberately a no-op: removing the row would flip a live install's
    # external MCP access off, disconnecting every off-box client.
    pass
