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

Scope: the eight AES-256-GCM-wrapped DB token columns (Invariant #12). Agent
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
    ("slack_workspaces", "id", "bot_token"),
    ("slack_link_connections", "id", "slack_bot_token"),
    ("telegram_bindings", "id", "bot_token_encrypted"),
    ("whatsapp_bindings", "id", "auth_token_encrypted"),
    ("voip_bindings", "id", "auth_token_encrypted"),
]


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
