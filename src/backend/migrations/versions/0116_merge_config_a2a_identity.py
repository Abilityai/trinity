"""Converge persistent configuration and current upstream migration histories.

Published revisions retain their identities and parent links. This revision
joins their heads without repeating either branch's schema or data changes.
"""

revision = "0107_merge_config_a2a_identity"
down_revision = ("0103_merge_config_admin_email", "0103_a2a_caller_identity")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
