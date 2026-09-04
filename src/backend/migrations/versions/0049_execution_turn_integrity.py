"""#2467 — queryable turn-integrity flags on the execution row.

A ``claude --print`` turn that ends with a background shell still in flight is
killed by the CLI ~5s after exit; the kill events reach the backend inside
``execution_log`` but nothing structured read them, so the row recorded a
clean ``success`` while the work was silently lost. ``turn_integrity`` is a
nullable TEXT JSON object derived backend-side at terminal write
(``services/execution_integrity.py``): ``background_tasks_killed`` (structural
kill records — never description/command text, the #2127 privacy rule) plus
``background_tasks_pending_at_exit`` (the waited-path counter previously
persisted nowhere). NULL ≡ "no evidence", never "verified healthy".

Mirrors the SQLite ``execution_turn_integrity`` migration.

Revision ID: 0049_execution_turn_integrity
Revises: 0048_channel_report_client
"""
from alembic import op


revision = "0049_execution_turn_integrity"
down_revision = "0048_channel_report_client"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `IF NOT EXISTS`, matching 0046-0048: a fresh PostgreSQL database is built
    # from `db/schema.py`'s DDL — which now declares this column — and only
    # THEN runs the revisions, so a bare `add_column` raises DuplicateColumn on
    # every fresh install (pg-migrations exercises exactly that boot path).
    op.execute(
        "ALTER TABLE schedule_executions "
        "ADD COLUMN IF NOT EXISTS turn_integrity TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE schedule_executions DROP COLUMN IF EXISTS turn_integrity")
