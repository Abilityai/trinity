"""terms_acceptances — terms of use per instance (trinity-enterprise#1107).

PostgreSQL half of the dual-track pair; the SQLite half is
``db/migrations.py::terms_acceptances_table``.

One row per (person, terms version): the record of which text they accepted
and when. ``door`` names the invitation an optional module bound the
acceptance to, else NULL. The text itself is a ``system_settings`` row with a
shipped default, so nothing is seeded here. Chained after #4261's
``0117_operator_queue_wake_state`` (which lands first) so the version-line
keeps ONE head (#2068) — it was ``0116`` off 0115 until the merge train
re-parented it.

Revision ID: 0118_terms_acceptances
Revises: 0117_operator_queue_wake_state
"""
from alembic import op
import sqlalchemy as sa

revision = "0118_terms_acceptances"
down_revision = "0117_operator_queue_wake_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A fresh PostgreSQL database is built from `db/schema.py`'s DDL — which
    # declares this table — and only THEN runs the revisions, so guard on
    # existence rather than create blindly (0046-0058's rule).
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("terms_acceptances"):
        op.create_table(
            "terms_acceptances",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("email", sa.Text(), nullable=True),
            sa.Column("terms_version", sa.Text(), nullable=False),
            sa.Column("accepted_at", sa.Text(), nullable=False),
            sa.Column("door", sa.Text(), nullable=True),
            sa.Column("actor_ip", sa.Text(), nullable=True),
            sa.UniqueConstraint("user_id", "terms_version", name="uq_terms_acceptances_user_version"),
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_terms_acceptances_email ON terms_acceptances(email)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS terms_acceptances")
