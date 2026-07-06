# Credential Management

Add, edit, and hot-reload credentials on agents without restarting them.

## Concepts

- **Credential Injection (CRED-002)** -- Direct file injection system. Credentials are written as `.env` (KEY=VALUE) and `.mcp.json` (generated from template) directly to the agent container.
- **Credential Requirements** -- Extracted from `.mcp.json.template`. The UI shows each credential as configured (green) or missing (red).
- **Encrypted Storage** -- Credentials can be exported to `.credentials.enc` files (AES-256-GCM encryption) for backup and import.

## How It Works

1. Open the agent detail page and click the **Credentials** tab.
2. View required credentials extracted from the agent's `.mcp.json.template`.
3. Status shows **configured** or **missing** per credential.
4. Add credentials using one of three methods:
   - **Manual entry** -- Name, value, and service fields.
   - **Bulk import** -- Paste `.env`-style KEY=VALUE pairs.
   - **From encrypted backup** -- Import a `.credentials.enc` file.
5. **Hot-reload** -- Paste or edit credentials on a running agent. The `.env` file is updated and `.mcp.json` is regenerated immediately. No restart needed.

### Credential Pattern in the Agent

```
.env                    # Source of truth (KEY=VALUE)
.mcp.json.template      # Template with ${VAR} placeholders
.mcp.json               # Generated at runtime from template + .env
```

### Which Files Can Be Injected

Injection accepts a curated set of credential file types, not just `.env`. Anything outside the allow-list — and anything on the deny-list — is rejected with a 400.

**Allowed:**

| Path | Typical use |
|------|-------------|
| `.env`, `.credentials.enc`, `.mcp.json` | Core credential files (workspace root only) |
| `.config/gcloud/**` | Google Cloud SDK credentials / service-account JSON |
| `.kube/config` | Kubernetes kubeconfig |
| `*.pem`, `*.key`, `*.crt`, `*.cert`, `*.p12`, `*.pfx` | TLS certificates and private keys |
| `.ssh/id_*` | SSH key pairs (keys only — not `authorized_keys` or `config`) |

**Always blocked** (deny takes precedence): anything executed or sourced at startup — shell startup files (`.bashrc`, `.profile`, `.zshrc`, …), agent instruction files (`CLAUDE.md`, `AGENTS.md`, `.claude/**`), `.mcp.json.template`, `.ssh/authorized_keys` / `.ssh/config`, `.git/**` and `.gitconfig`, anything under `bin/`, plus absolute paths and `..` traversal. `.mcp.json` content is structurally validated before it is written.

**Binary credentials** (certificates, keystores, service-account bundles) round-trip as base64 via the `files_b64` field on the inject endpoint.

### Export and Import

- **Export** creates an encrypted `.credentials.enc` file for backup. It captures the **full injected credential set** — every allow-listed credential file present in the agent (discovered live), text and binary alike — not just `.env` and `.mcp.json`.
- **Import** decrypts and injects credentials from an encrypted file. The archive is re-validated against the same path policy on the way in.
- **Auto-import** runs on agent startup via `POST /api/internal/decrypt-and-inject`.

### Rotating the Encryption Key

The platform encryption key (`CREDENTIAL_ENCRYPTION_KEY`) can be rotated online, with zero downtime and no data loss:

1. Back up the database (`scripts/deploy/backup-database.sh`).
2. Generate a new key: `python3 -c "import secrets; print(secrets.token_hex(32))"`.
3. In `.env`, set the new key as `CREDENTIAL_ENCRYPTION_KEY` and move the previous key to `CREDENTIAL_ENCRYPTION_KEY_SECONDARY` (a decrypt-only fallback).
4. Restart the backend — existing secrets keep decrypting via the secondary key; all new writes use the new key.
5. Re-encrypt persisted secrets onto the new key: `docker compose exec backend python scripts/deploy/rotate-credential-key.py` (dry-run), then re-run with `--apply`.
6. Remove `CREDENTIAL_ENCRYPTION_KEY_SECONDARY` from `.env` and restart.

The sweep re-encrypts every database-persisted token (subscriptions, channel bot tokens, GitHub PATs, payment credentials). Per-agent `.credentials.enc` files re-encrypt onto the new key on their next credential operation; they keep opening via the secondary key until then. Full runbook: `docs/migrations/CREDENTIAL_KEY_ROTATION.md`.

### Security

Credential values are never logged. All operations use structured logging with values masked.

## For Agents

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/credentials/status` | GET | Check credential files |
| `/api/agents/{name}/credentials/inject` | POST | Inject files directly |
| `/api/agents/{name}/credentials/export` | POST | Export to `.credentials.enc` |
| `/api/agents/{name}/credentials/import` | POST | Import from encrypted file |

### MCP Tools

- `get_credential_status(name)` -- Check credential file status.
- `inject_credentials(name, credentials)` -- Inject credentials into the agent.
- `export_credentials(name)` -- Export credentials to encrypted file.
- `import_credentials(name)` -- Import credentials from encrypted file.
- `get_credential_encryption_key()` -- Retrieve the encryption key.

## See Also

- [Agent Configuration](../agents/agent-configuration.md)
- [Subscription Credentials](subscription-credentials.md)
- [OAuth Credentials](oauth-credentials.md)
