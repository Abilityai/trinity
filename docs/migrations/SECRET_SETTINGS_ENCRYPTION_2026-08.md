# Credential settings encrypted at rest — operator note (ent#435, 2026-08)

**Severity:** medium (defence-in-depth; needs DB or host access to exploit)
**Class:** CWE-312, cleartext storage of sensitive information
**Action required:** ROTATE the affected credentials after upgrading.

## What was wrong

Six `system_settings` rows held **live third-party credentials in cleartext**:

| setting key | credential |
|---|---|
| `anthropic_api_key` | Anthropic API key |
| `github_pat` | GitHub Personal Access Token (platform-wide) |
| `google_api_key` | Google / Gemini API key |
| `slack_app_token` | Slack app-level token (`xapp-…`, Socket Mode) |
| `slack_client_secret` | Slack OAuth client secret |
| `slack_signing_secret` | Slack request-signing secret |

Their siblings already encrypted — `elevenlabs_api_key_encrypted` (ent#117),
`a2a_outbound_endpoints_encrypted` (#736), and every AES-256-GCM token column
under Architectural Invariant #12 — so the platform's encryption-at-rest posture
was only partially true, and a single `SELECT` contradicted any statement of it.

Consequences: every DB dump, backup, replica and snapshot carried usable tokens
(backups being exactly the artifact most likely to travel — operator laptops,
object storage, snapshots), and any read path to the database yielded working
credentials **without** needing `CREDENTIAL_ENCRYPTION_KEY`.

`slack_client_id` is deliberately **not** in the list: an OAuth client_id is a
public identifier that Trinity puts verbatim in the browser-visible authorize
URL. It is recorded as a reviewed exemption in
`services/secret_settings.py::PUBLIC_CREDENTIAL_SHAPED_KEYS`.

## What the upgrade does

On first boot after upgrading, a one-shot migration
(`secret_settings_encryption` on SQLite, `0041_secret_settings_encryption` on
PostgreSQL) rewrites each of those rows as an AES-256-GCM envelope under
`<key>_encrypted` and **deletes the cleartext row**.

* Idempotent — an already-encrypted row is skipped; a half-applied sweep converges.
* Write-then-delete per row, so a crash mid-sweep leaves cleartext intact rather
  than losing a credential.
* Names only in the logs, never values.

From then on the credentials resolve `encrypted row → env var → unset`, and the
`system_settings` write path **refuses** a cleartext write to any
credential-bearing key (including through the generic `PUT /api/settings/{key}`).

Nothing changes for you operationally: the admin UI, the `/api/settings/api-keys/*`
and `/api/settings/slack*` routes, and the `ANTHROPIC_API_KEY` / `GITHUB_PAT` /
`GOOGLE_API_KEY` / `SLACK_*` environment-variable fallbacks all behave exactly as
before.

## Prerequisite: `CREDENTIAL_ENCRYPTION_KEY`

The migration **hard-fails** if `CREDENTIAL_ENCRYPTION_KEY` is unset *and* there
are credential rows to encrypt — the backend refuses to start rather than boot
with the secrets still in cleartext. (A fresh install with no credential rows is
never blocked.) This matches the #453 Slack sweep and every Invariant #12 helper.

`scripts/deploy/start.sh` generates the key automatically
(`ensure_hex32_secret CREDENTIAL_ENCRYPTION_KEY`), so a deployment started the
supported way already has one. If you launch `docker compose` directly, confirm
the variable is set in `.env` before upgrading:

```bash
grep -q '^CREDENTIAL_ENCRYPTION_KEY=.\+' .env || \
  echo "CREDENTIAL_ENCRYPTION_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >> .env
```

## Required follow-up: rotate the credentials

Encryption protects the database **going forward**. It cannot un-leak what has
already been written: every backup, snapshot and replica taken before this
upgrade still contains the plaintext tokens, and those artifacts are the ones
most likely to have travelled.

Rotate each credential that was configured through Settings (not the ones that
only ever lived in environment variables):

1. **Anthropic** — issue a new key in the Anthropic Console, set it in
   Settings → API Keys, revoke the old one.
2. **GitHub PAT** — issue a new token with the same scopes, set it in
   Settings → API Keys, revoke the old one. Per-agent PATs (#347) and per-user
   PATs (ent#162) were already encrypted and are unaffected.
3. **Google / Gemini** — issue a new key, set it, revoke the old one.
4. **Slack** — in the Slack app config: regenerate the client secret, regenerate
   the signing secret, and revoke + reissue the app-level token. Set all three in
   Settings → Slack. Existing workspace bot tokens (`xoxb-…`) were already
   encrypted (#453) and are unaffected.

## Verifying

The reporter's own check — it should return **no rows**:

```sql
SELECT key FROM system_settings
WHERE key IN ('anthropic_api_key','github_pat','google_api_key',
              'slack_app_token','slack_client_secret','slack_signing_secret');
```

And the encrypted rows should be envelopes, not readable values:

```sql
SELECT key, substr(value, 1, 40) FROM system_settings WHERE key LIKE '%\_encrypted' ESCAPE '\';
-- → {"version": 1, "algorithm": "AES-256-…
```

## What encryption does *not* undo

Rotation is not optional cleanup, it is the actual remediation. Two reasons:

1. **Historical artifacts.** Every backup, snapshot and replica taken before the
   upgrade contains the plaintext, and nothing this migration does can reach them.
2. **Residue in the live file.** Deleting a row does not necessarily scrub the
   bytes — SQLite reuses freed pages and `secure_delete` is off by default, and
   PostgreSQL keeps dead tuples until `VACUUM`. Observed behaviour on a small
   test DB was that the plaintext left the SQLite file immediately, but that is a
   consequence of page layout, not a guarantee. Trinity already runs a nightly
   `VACUUM` (`db_vacuum_service`, 04:30 UTC), which helps — it is not a
   substitute for rotating.

## Key rotation

`scripts/deploy/rotate-credential-key.py` now sweeps these settings rows as well
as the token columns, so a `CREDENTIAL_ENCRYPTION_KEY` rotation
(`docs/migrations/CREDENTIAL_KEY_ROTATION.md`) re-encrypts them onto the new key.
This also closes a pre-existing gap: `elevenlabs_api_key_encrypted` and
`a2a_outbound_endpoints_encrypted` were envelope-in-a-row and therefore invisible
to the column-only sweep, so a "completed" rotation left them readable only via
the secondary key — and unreadable the moment it was removed. If you have rotated
the encryption key in the past, re-run the sweep once after upgrading.
