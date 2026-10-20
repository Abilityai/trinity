"""trinity-enterprise#729 — a corrected value restates its metric point (R45).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::metric_points_restatement``.

* ``revision BIGINT NOT NULL DEFAULT 0`` — accepted corrections, 0 at insert.
  A constant default is catalog-only on PostgreSQL 11+, so existing rows read 0
  without a table rewrite, and a writer from before this change still inserts.
* ``recorded_at TEXT`` — the write time of the value the row holds. Nullable,
  NO backfill: NULL means "written before ent#729" (read it as ``created_at``),
  and an ``UPDATE`` over every row would rewrite the table at boot under the
  migration advisory lock.

``IF NOT EXISTS`` because a fresh database is built from ``db/schema.py``'s DDL
(``0001_baseline``), which already declares both columns.

Revision ID: 0086_metric_points_restatement
Revises: 0085_ent720_email_identity
"""
from alembic import op


revision = "0086_metric_points_restatement"
down_revision = "0085_ent720_email_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE metric_points "
        "ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE metric_points ADD COLUMN IF NOT EXISTS recorded_at TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE metric_points DROP COLUMN IF EXISTS recorded_at")
    op.execute("ALTER TABLE metric_points DROP COLUMN IF EXISTS revision")
