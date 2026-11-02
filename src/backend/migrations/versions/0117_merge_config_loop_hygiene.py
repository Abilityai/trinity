"""Join published configuration and upstream loop-hygiene migration histories.

All published revision identities and parent links remain unchanged.
"""

revision = "0117_merge_config_loop_hygiene"
down_revision = ("0107_merge_config_a2a_identity", "0112_loop_runs_stale_duplicates")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
