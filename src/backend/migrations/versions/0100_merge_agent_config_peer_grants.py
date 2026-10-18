"""Converge persistent configuration and peer-skill migration histories.

Keep both published revisions valid for databases already on either branch.
The disjoint schema changes are applied by their original revisions.
"""

revision = "0100_merge_agent_config_peer_grants"
down_revision = ("0099_agent_config_storage_format", "0099_agent_peer_skill_grants")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
