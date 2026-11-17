"""operator_resume_default_on — agents wake on an answered ask by default (abilityai/trinity-enterprise#1108).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::operator_resume_default_on``.

``agent_ownership.operator_resume_enabled`` becomes tri-state: NULL is the
platform default — ON — 0 the owner's explicit OFF, 1 the owner's explicit ON.
The 0042 column carried ``server_default '0'``, so every row holds 0 whether or
not an owner ever touched it. The toggle is audited (``audit_log``
``operator_resume_config``, ``details.enabled``), so an agent whose LATEST
toggle said ``false`` keeps its 0; every other 0 moves to NULL and inherits the
default. The server default is dropped so a writer that omits the column lands
on the default rather than on an explicit OFF. Data + a default; no column
change. Chained after 0115 so the version-line keeps ONE head (#2068).

Revision ID: 0116_operator_resume_default_on
Revises: 0115_access_requests_requester_identity
"""
import sqlalchemy as sa
from alembic import op


revision = "0116_operator_resume_default_on"
down_revision = "0115_access_requests_requester_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("audit_log"):
        bind.execute(
            sa.text(
                """
                UPDATE agent_ownership SET operator_resume_enabled = NULL
                WHERE operator_resume_enabled = 0
                  AND agent_name NOT IN (
                      SELECT a.target_id FROM audit_log a
                      WHERE a.event_action = 'operator_resume_config'
                        AND a.target_type = 'agent'
                        AND a.details LIKE '%"enabled": false%'
                        AND a.timestamp = (
                            SELECT MAX(b.timestamp) FROM audit_log b
                            WHERE b.event_action = 'operator_resume_config'
                              AND b.target_type = 'agent'
                              AND b.target_id = a.target_id
                        )
                  )
                """
            )
        )
    else:
        bind.execute(
            sa.text(
                "UPDATE agent_ownership SET operator_resume_enabled = NULL"
                " WHERE operator_resume_enabled = 0"
            )
        )
    # NULL is now the default; a baked '0' would stamp an explicit OFF on any
    # writer that omits the column.
    op.alter_column("agent_ownership", "operator_resume_enabled", server_default=None)


def downgrade() -> None:
    # Not reversible without a record of which rows were moved; a NULL reads as
    # OFF under the pre-#1108 getter, which is what those rows were.
    op.alter_column("agent_ownership", "operator_resume_enabled", server_default="0")
