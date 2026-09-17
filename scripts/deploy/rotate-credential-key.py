#!/usr/bin/env python3
"""
Credential encryption key rotation sweep (#267).

Re-encrypts every persisted secret onto the PRIMARY key
(``CREDENTIAL_ENCRYPTION_KEY``), decrypting via the rotation fallback
(``CREDENTIAL_ENCRYPTION_KEY_SECONDARY``) when a row was written under the
previous key. Run it during a maintenance window as the final step of the
rotation runbook (docs/migrations/CREDENTIAL_KEY_ROTATION.md).

    # 1. set the NEW key primary, the OLD key secondary, restart backend
    # 2. dry-run (no writes) — see what would migrate
    python scripts/deploy/rotate-credential-key.py
    # 3. apply
    python scripts/deploy/rotate-credential-key.py --apply
    # 4. remove CREDENTIAL_ENCRYPTION_KEY_SECONDARY, restart

Scope: the AES-256-GCM-wrapped DB token columns (Invariant #12) PLUS the
credential-bearing ``system_settings`` rows, whose secrets live in a *value*
rather than a dedicated column and so are invisible to the column sweep
(ent#435). That gap predates ent#435: ``elevenlabs_api_key_encrypted`` (ent#117)
and ``a2a_outbound_endpoints_encrypted`` (#736) were already envelope-in-a-row
and already missed, meaning a completed rotation left them readable only via the
secondary key — and unreadable the moment it was removed. Agent
``.credentials.enc`` files live inside agent containers and re-encrypt onto the
primary key on their next credential operation (or `inject`/`export`); they keep
opening via the secondary key until then, so they are intentionally out of this
DB sweep. Rows that are not a valid envelope (legacy plaintext, e.g. a Slack
``xoxb-`` token awaiting the #453 re-encrypt) are skipped, never corrupted.

Idempotent and safe to re-run: a row already on the primary key round-trips to an
equivalent envelope.
"""
import argparse
import os
import sys
from pathlib import Path

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from sqlalchemy import text  # noqa: E402

from db.engine import get_engine  # noqa: E402
from services.credential_encryption import (  # noqa: E402
    CredentialEncryptionService,
    ENCRYPTION_KEY_ENV,
    SECONDARY_ENCRYPTION_KEY_ENV,
)

# (table, primary-key column, encrypted column) — keep in sync with Invariant #12.
ENCRYPTED_COLUMNS = [
    ("subscription_credentials", "id", "encrypted_credentials"),
    ("nevermined_agent_config", "id", "encrypted_credentials"),
    ("agent_git_config", "id", "github_pat_encrypted"),
    ("users", "id", "github_pat_encrypted"),  # ent#162 — per-user GitHub PAT
    ("slack_workspaces", "id", "bot_token"),
    ("slack_link_connections", "id", "slack_bot_token"),
    ("telegram_bindings", "id", "bot_token_encrypted"),
    ("whatsapp_bindings", "id", "auth_token_encrypted"),
    ("voip_bindings", "id", "auth_token_encrypted"),
]

# ent#435: envelope-bearing ``system_settings`` rows. Keyed by row, not column —
# only these keys hold envelopes; every other settings row is plain config and
# must not be touched. Membership is DERIVED from the policy module for the
# ent#435 keys so a newly-registered credential setting joins the rotation
# automatically; the two pre-existing rows are named explicitly because they
# predate that registry.
_STANDALONE_ENVELOPE_SETTINGS = (
    "elevenlabs_api_key_encrypted",  # ent#117
    "a2a_outbound_endpoints_encrypted",  # #736
)


def _envelope_setting_keys() -> list:
    try:
        from services.secret_settings import ENCRYPTED_SETTING_KEYS
    except Exception:  # pragma: no cover — pre-ent#435 checkout
        ENCRYPTED_SETTING_KEYS = frozenset()
    return sorted(set(ENCRYPTED_SETTING_KEYS) | set(_STANDALONE_ENVELOPE_SETTINGS))


def _table_exists(conn, table: str) -> bool:
    try:
        conn.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
        return True
    except Exception:
        return False


def rotate(apply: bool) -> int:
    if not os.getenv(ENCRYPTION_KEY_ENV):
        print(f"ERROR: {ENCRYPTION_KEY_ENV} is not set — nothing to rotate onto.")
        return 2
    if not os.getenv(SECONDARY_ENCRYPTION_KEY_ENV):
        print(
            f"WARNING: {SECONDARY_ENCRYPTION_KEY_ENV} is not set. Rows written under a "
            f"previous key will fail to decrypt and be skipped. Set it to the OLD key "
            f"if you are mid-rotation."
        )

    svc = CredentialEncryptionService()
    engine = get_engine()
    total_migrated = total_skipped = 0

    with engine.begin() as conn:
        for table, pk, col in ENCRYPTED_COLUMNS:
            if not _table_exists(conn, table):
                print(f"  {table:28} — absent, skipped")
                continue
            rows = conn.execute(
                text(f"SELECT {pk} AS pk, {col} AS enc_value FROM {table} WHERE {col} IS NOT NULL")
            ).mappings().all()
            migrated = skipped = 0
            for row in rows:
                try:
                    rewrapped = svc.rewrap(row["enc_value"])
                except Exception as e:
                    skipped += 1
                    print(f"    [skip] {table}.{pk}={row['pk']}: not a readable envelope ({e})")
                    continue
                if apply:
                    conn.execute(
                        text(f"UPDATE {table} SET {col} = :v WHERE {pk} = :k"),
                        {"v": rewrapped, "k": row["pk"]},
                    )
                migrated += 1
            total_migrated += migrated
            total_skipped += skipped
            print(f"  {table:28} {migrated:4} re-encrypted, {skipped} skipped")

        # ent#435: the row-keyed settings pass.
        if _table_exists(conn, "system_settings"):
            keys = _envelope_setting_keys()
            migrated = skipped = 0
            for key in keys:
                row = conn.execute(
                    text("SELECT value FROM system_settings WHERE key = :k"),
                    {"k": key},
                ).first()
                if not row or not row[0]:
                    continue
                try:
                    rewrapped = svc.rewrap(row[0])
                except Exception as e:
                    skipped += 1
                    print(f"    [skip] system_settings[{key}]: not a readable envelope ({e})")
                    continue
                if apply:
                    conn.execute(
                        text("UPDATE system_settings SET value = :v WHERE key = :k"),
                        {"v": rewrapped, "k": key},
                    )
                migrated += 1
            total_migrated += migrated
            total_skipped += skipped
            print(f"  {'system_settings (rows)':28} {migrated:4} re-encrypted, {skipped} skipped")
        else:
            print(f"  {'system_settings (rows)':28} — absent, skipped")

        if not apply:
            # Roll the (no-op) transaction back explicitly for clarity in dry-run.
            conn.rollback()

    verb = "re-encrypted" if apply else "would re-encrypt (dry-run)"
    print(f"\n{total_migrated} secrets {verb}; {total_skipped} skipped.")
    if not apply:
        print("Dry-run only — re-run with --apply to write.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Rotate credential encryption key (#267).")
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry-run).")
    args = ap.parse_args()
    print(f"Credential key rotation sweep ({'APPLY' if args.apply else 'DRY-RUN'})\n")
    return rotate(apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
