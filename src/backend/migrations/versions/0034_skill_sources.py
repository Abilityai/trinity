"""multi-source skills library (ent#237)

Creates ``skill_sources`` (one row per git repo the skills library syncs from,
replacing the single ``skills_library_url`` system setting) and adds
``agent_skills.source_id``.

Two design points mirrored from the SQLite track in ``db/migrations.py``:

  * ``agent_skills`` keeps its ``UNIQUE(agent_name, skill_name)`` — names are a
    flat namespace (custom-wins precedence, AC#4), because the agent-side
    identity is the directory ``.claude/skills/<name>/`` and both the ent#139
    runner and the ent#178 A2A card resolve by bare name. ``source_id`` records
    which source an assignment resolved from so a cross-source swap is
    detectable; keying on it would permit two rows that cannot coexist on disk.
  * ``ref``/``ref_type`` carry a branch OR tag. The bundled community source
    pins to a tag (AC#5) so a merged upstream PR never reaches a fleet
    unattended; custom sources track a branch.

Adopting a pre-existing ``skills_library_url`` as a source is deliberately NOT
done here — the legacy clone at /data/skills-library/ must move into a
per-source subdir in the same operation, so it lives in ``skill_service``
where both halves succeed or fail together.

Revision ID: 0034_skill_sources
Revises: 0033_agent_evaluations
Create Date: 2026-07-29

Renumbered 0031 -> 0034 when dev was merged in: this branch cut its revision off
0030, and dev independently cut 0031_channel_report_back off the same parent, so
the two files (which never conflict textually — they are separate paths) forked
the revision graph into TWO heads. `alembic upgrade head` refuses a multi-head
argument, so PostgreSQL boot would fail in `init_database()`. Re-parented onto
the current dev head rather than merged with an Alembic merge revision: this
table has no relationship to the channel/telegram/evaluations chain, and a
linear history is what the `schema-parity` and `pg-migrations` jobs assert.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0034_skill_sources"
down_revision = "0033_agent_evaluations"
branch_labels = None
depends_on = None


def _has_table(bind, table: str) -> bool:
    return sa.inspect(bind).has_table(table)


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "skill_sources"):
        op.create_table(
            "skill_sources",
            sa.Column("id", sa.Text(), primary_key=True),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("ref", sa.Text(), nullable=False, server_default="main"),
            sa.Column("ref_type", sa.Text(), nullable=False, server_default="branch"),
            sa.Column("is_default", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("enabled", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
            sa.Column("last_sync_at", sa.Text()),
            sa.Column("last_sync_status", sa.Text()),
            sa.Column("last_commit_sha", sa.Text()),
            sa.Column("last_error", sa.Text()),
            sa.Column("created_by", sa.Text()),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.Text(), nullable=False),
            sa.UniqueConstraint("url", "ref", name="uq_skill_sources_url_ref"),
        )
        op.create_index(
            "idx_skill_sources_resolution",
            "skill_sources",
            ["priority", "created_at"],
            postgresql_where=sa.text("enabled = 1"),
            sqlite_where=sa.text("enabled = 1"),
        )
        op.create_index(
            "idx_skill_sources_one_default",
            "skill_sources",
            ["is_default"],
            unique=True,
            postgresql_where=sa.text("is_default = 1"),
            sqlite_where=sa.text("is_default = 1"),
        )

    # Nullable, no backfill: NULL means "assigned before multi-source, or the
    # source row is gone" and resolves by precedence like any other bare name.
    if not _has_column(bind, "agent_skills", "source_id"):
        op.add_column("agent_skills", sa.Column("source_id", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "agent_skills", "source_id"):
        op.drop_column("agent_skills", "source_id")
    if _has_table(bind, "skill_sources"):
        op.drop_index("idx_skill_sources_one_default", table_name="skill_sources")
        op.drop_index("idx_skill_sources_resolution", table_name="skill_sources")
        op.drop_table("skill_sources")
