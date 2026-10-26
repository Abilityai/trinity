"""#3298 — an idempotency claim names its owner.

``idempotency_keys.claim_token``: attach/complete/release compare-and-set on
it, so a claim left ``in_flight`` by a dead process can be reclaimed without
the original request (if it was only slow) overwriting or deleting the
reclaimer's row. Nullable, no backfill.

Mirrors the SQLite ``idempotency_claim_token`` migration.

Revision ID: 0103_idempotency_claim_token
Revises: 0102_nevermined_delegations
"""
from alembic import op


revision = "0103_idempotency_claim_token"
down_revision = "0102_nevermined_delegations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE idempotency_keys ADD COLUMN IF NOT EXISTS claim_token TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE idempotency_keys DROP COLUMN IF EXISTS claim_token")
