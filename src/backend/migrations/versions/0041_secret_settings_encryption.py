"""Encrypt cleartext credential rows in system_settings (trinity-enterprise#435)

Six ``system_settings`` rows held LIVE third-party credentials in cleartext
(``anthropic_api_key``, ``github_pat``, ``google_api_key``, ``slack_app_token``,
``slack_client_secret``, ``slack_signing_secret``), so every DB dump, backup,
replica and snapshot carried usable tokens and any read path to the DB yielded
them without needing ``CREDENTIAL_ENCRYPTION_KEY`` (CWE-312). This is the
PostgreSQL half of the one-shot sweep: each value moves to an AES-256-GCM
envelope under ``<key>_encrypted`` and the cleartext row is DELETED.

This is a DATA migration, so `scripts/ci/check_alembic_parity.py` — which keys
on DDL keywords — does not force it. It is required anyway: the reporter
observed the defect on a PostgreSQL install, so this track is the primary one,
not the afterthought. Mirrors the SQLite ``secret_settings_encryption``
migration; both call ``services.secret_settings.plan_migration`` so the policy
(which keys, envelope shape, skip rules) cannot drift between tracks even though
the SQL cannot be shared (Invariant #9).

Hard-fails on a missing ``CREDENTIAL_ENCRYPTION_KEY`` — the same choice #453
made for the Slack sweep. Refusing to boot is correct here: the alternative is
booting with the credentials still in cleartext, which is the defect.

Idempotent: an already-encrypted row is skipped, so a re-run or a half-applied
sweep converges. Write-then-delete per row, so a crash mid-sweep leaves
cleartext intact rather than losing a credential.

``downgrade`` is deliberately a no-op — see the note on the function.

Revision ID: 0041_secret_settings_encryption
Revises: 0040_rl_events_failure_kind
Create Date: 2026-08-20
"""
import logging

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = "0041_secret_settings_encryption"
down_revision = "0040_rl_events_failure_kind"
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)


def upgrade() -> None:
    from services.secret_settings import SECRET_SETTING_KEYS, plan_migration
    from utils.helpers import utc_now_iso

    conn = op.get_bind()
    keys = sorted(SECRET_SETTING_KEYS)
    rows = conn.execute(
        text("SELECT key, value FROM system_settings WHERE key = ANY(:keys)"),
        {"keys": keys},
    ).fetchall()

    # Only reached when there is something to encrypt, so a fresh install with
    # no credential rows is never blocked from booting by a key it does not need.
    plan = plan_migration([(r[0], r[1]) for r in rows]) if rows else []

    now = utc_now_iso()
    for legacy_key, encrypted_key, envelope in plan:
        conn.execute(
            text(
                "INSERT INTO system_settings (key, value, updated_at) "
                "VALUES (:k, :v, :t) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                "updated_at = EXCLUDED.updated_at"
            ),
            {"k": encrypted_key, "v": envelope, "t": now},
        )
        conn.execute(
            text("DELETE FROM system_settings WHERE key = :k"), {"k": legacy_key}
        )

    if plan:
        # Names only — never values (the canary G-04 rule: a record of the
        # problem must not become a second copy of the secret).
        logger.warning(
            "ent#435 migration: encrypted %d cleartext credential setting(s): %s. "
            "ROTATE these credentials — historical backups still hold the plaintext.",
            len(plan),
            ", ".join(sorted(k for k, _, _ in plan)),
        )
    else:
        logger.info(
            "ent#435 migration: no cleartext credential settings found "
            "(%d row(s) already encrypted or empty)",
            len(rows),
        )


def downgrade() -> None:
    """Intentionally a no-op.

    The honest inverse of this migration is "write these live credentials back
    to disk in cleartext", which is the vulnerability. A downgrade that silently
    re-exposed them would be worse than one that does nothing, and an operator
    rolling back to a pre-ent#435 build should re-enter the credentials (or set
    the matching env vars) rather than have the platform undo the fix for them.
    """
