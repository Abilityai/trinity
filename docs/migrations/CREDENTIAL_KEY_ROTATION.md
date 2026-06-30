# Credential Encryption Key Rotation (#267)

Trinity encrypts persisted secrets — agent `.credentials.enc` archives and the
DB token columns (Invariant #12) — with AES-256-GCM under a single master key,
`CREDENTIAL_ENCRYPTION_KEY`. Before #267 there was **no way to rotate it**:
changing the key bricked every existing secret. This runbook rotates the key
**online, with zero downtime and no data loss**, using a decrypt-only secondary
key plus a re-encryption sweep.

## When to rotate
- The key may have been exposed (it is passed as an env var — visible in
  `docker inspect` / `/proc/<pid>/environ`, the finding behind #267).
- Routine hygiene (e.g. annual).

> Moving the key off a plaintext env var entirely (Docker/Swarm secrets or an
> external secret manager) is **out of scope** here and tracked separately — this
> document covers *rotation*, which is the gap #267 closes.

## How it works
- `CREDENTIAL_ENCRYPTION_KEY` is the **primary** key: every new write encrypts
  with it.
- `CREDENTIAL_ENCRYPTION_KEY_SECONDARY` (optional) is a **decrypt-only** fallback.
  During rotation it holds the **old** key, so ciphertext written under the old
  key still opens while the primary is the new key.
- `scripts/deploy/rotate-credential-key.py` re-encrypts (`rewrap`) every DB
  secret onto the primary. Once it completes, nothing depends on the old key and
  the secondary can be removed.

## Procedure

```bash
# 0. Back up the DB first.
scripts/deploy/backup-database.sh

# 1. Generate the NEW key.
NEW=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# 2. In .env, set the new key primary and the CURRENT key secondary:
#      CREDENTIAL_ENCRYPTION_KEY=<NEW>
#      CREDENTIAL_ENCRYPTION_KEY_SECONDARY=<the previous CREDENTIAL_ENCRYPTION_KEY>
#    (Both forwarded to the backend by docker-compose.)

# 3. Restart the backend so it loads both keys.
docker compose up -d backend

#    At this point the platform is fully functional: old secrets decrypt via the
#    secondary, all NEW writes already use the new key.

# 4. Dry-run the sweep (no writes) — review the counts.
docker compose exec backend python scripts/deploy/rotate-credential-key.py

# 5. Apply — re-encrypt all DB secrets onto the new key.
docker compose exec backend python scripts/deploy/rotate-credential-key.py --apply

# 6. Remove CREDENTIAL_ENCRYPTION_KEY_SECONDARY from .env and restart.
docker compose up -d backend
```

## Agent `.credentials.enc` files
These live **inside agent containers**, not the DB, so the sweep does not touch
them. While the secondary key is configured they keep decrypting under the old
key; each re-encrypts onto the new key on its **next credential operation**
(`inject` / `export`, or auto-export on the next sync). To force-migrate an
agent immediately, re-inject its credentials or run an export. (Automating an
in-container re-encryption pass is a fast-follow.)

## Safety notes
- The sweep is **idempotent** and safe to re-run.
- Rows that are not a readable envelope (e.g. a legacy plaintext Slack `xoxb-`
  token awaiting #453 re-encryption) are **skipped, never corrupted** — they
  re-encrypt on the next backend restart.
- Keep the backup from step 0 until you have verified agents and channels work
  after step 6.
- Decryption is **fail-open across keys only** in the sense that it *tries* both;
  a value that opens under neither key surfaces a clear error rather than silent
  data loss.
