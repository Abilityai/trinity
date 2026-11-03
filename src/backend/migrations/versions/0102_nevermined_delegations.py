"""nevermined_delegations (trinity-enterprise#966).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::nevermined_delegations``.

A person funds an agent with a card mandate held by Nevermined:

* ``nevermined_organization`` — the instance's one Nevermined organization
  (singleton row), its two secrets in one encrypted envelope;
* ``agent_delegations`` — the delegation id bound to an agent; one active row
  per agent (partial unique index); KEEP on agent delete
  (``db/agent_cleanup.py``), stamped ``agent_deleted_at``.

The statements are shared with the SQLite track
(``db.migrations.NEVERMINED_DELEGATIONS_DDL``). Additive.

Revision ID: 0102_nevermined_delegations
Revises: 0101_held_gate_turns
"""
from alembic import op


revision = "0102_nevermined_delegations"
down_revision = "0101_held_gate_turns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from db.migrations import NEVERMINED_DELEGATIONS_DDL

    # `IF NOT EXISTS` throughout: a fresh PostgreSQL database is built from
    # `db/schema.py`'s DDL, which already declares both tables, before the
    # revisions run (the 0095 shape).
    for statement in NEVERMINED_DELEGATIONS_DDL:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_delegations")
    op.execute("DROP TABLE IF EXISTS nevermined_organization")
