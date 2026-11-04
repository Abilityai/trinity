"""Converge persistent configuration and admin-email migration histories.

Keep all published revision identities and parent links valid. The original
revisions own their schema/data changes; this revision only joins their heads.
"""

revision = "0103_merge_config_admin_email"
down_revision = ("0100_merge_agent_config_peer_grants", "0100_admin_email_not_username")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
