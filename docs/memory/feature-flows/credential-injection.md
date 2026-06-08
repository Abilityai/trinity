# Feature: Credential Injection (CRED-002)

## Overview

Simplified credential management system using encrypted files stored in git. Credentials are injected directly as files into agent workspaces, with optional encryption for git storage.

> **Refactored 2026-02-05**: Replaced complex Redis-based assignment system with simplified file injection. See [CREDENTIAL_SYSTEM_REFACTOR.md](../../requirements/CREDENTIAL_SYSTEM_REFACTOR.md) for specification.

## User Story
As a platform user, I want to add credentials to my agent so that MCP servers and tools can authenticate with external services, with credentials persisting across agent restarts via encrypted git storage.

---

## Key Concepts

### Old System (Deprecated)
- Credentials stored in Redis with metadata/secret separation
- Assignment model: create credential → assign to agent → apply to agent
- Template substitution for `.mcp.json.template` files
- Global `/credentials` page for credential management

### New System (CRED-002)
- Credentials = files written directly to agent workspace
- No Redis assignments - direct file injection
- Encrypted backup in `.credentials.enc` for git storage
- Per-agent credential management only (no global page)

### Credential Flow

```
┌─────────────────────────────────────────────────────────────┐
│                     User Actions                             │
├─────────────────────────────────────────────────────────────┤
│  Quick Inject: Paste .env text → Write to agent .env         │
│  File Inject: Upload files → Write to agent workspace        │
│  Export: Agent files → Encrypt → .credentials.enc in git     │
│  Import: .credentials.enc → Decrypt → Write to agent         │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    Agent Workspace                           │
├─────────────────────────────────────────────────────────────┤
│  /home/developer/                                            │
│  ├── .env                    # KEY=VALUE credentials         │
│  ├── .mcp.json               # MCP server configuration      │
│  ├── .credentials.enc        # Encrypted backup (in git)     │
│  └── .config/                # Service-specific configs      │
└─────────────────────────────────────────────────────────────┘
```

---

## Entry Points

| UI Location | API Endpoint | Purpose |
|-------------|--------------|---------|
| Agent Detail → Credentials Tab | `POST /api/agents/{name}/credentials/inject` | Inject files directly |
| Agent Detail → Credentials Tab | `POST /api/agents/{name}/credentials/export` | Export to encrypted file |
| Agent Detail → Credentials Tab | `POST /api/agents/{name}/credentials/import` | Import from encrypted file |
| Agent Startup | `POST /api/internal/decrypt-and-inject` | Auto-import on start |

---

## Flow 1: Quick Inject (Paste .env Text)

### Overview
User pastes KEY=VALUE text, which gets merged into the agent's `.env` file.

### Frontend (`src/frontend/src/composables/useAgentCredentials.js:177-237`)
```javascript
const quickAddCredentials = async () => {
  if (!agentRef.value || agentRef.value.status !== 'running') return
  if (!quickAddText.value.trim()) return

  // Parse .env-style text to key-value pairs
  const newCredentials = parseEnvText(quickAddText.value)

  // Get existing .env content and merge
  let existingEnv = {}
  try {
    const content = await agentsStore.downloadAgentFile(
      agentRef.value.name,
      '/home/developer/.env'
    )
    existingEnv = parseEnvText(content)
  } catch (e) {
    // File doesn't exist, start fresh
  }

  // Merge new credentials (overwrite existing keys)
  const merged = { ...existingEnv, ...newCredentials }
  const envContent = formatEnvContent(merged)

  // Inject the merged .env file
  await agentsStore.injectCredentials(agentRef.value.name, {
    '.env': envContent
  })
}
```

### Store Action (`src/frontend/src/stores/agents.js`)
```javascript
async injectCredentials(name, files) {
  const response = await axios.post(
    `/api/agents/${name}/credentials/inject`,
    { files },
    { headers: authStore.authHeader }
  )
  return response.data
}
```

### Backend (`src/backend/routers/credentials.py`)
```python
@router.post("/agents/{agent_name}/credentials/inject", response_model=CredentialInjectResponse)
async def inject_credentials(
    agent_name: str,
    request_body: CredentialInjectRequest,
    current_user: User = Depends(get_current_user)
):
    """Inject credential files directly into a running agent."""
    encryption_service = CredentialEncryptionService()
    result = await encryption_service.write_agent_credential_files(
        agent_name, request_body.files
    )
    return CredentialInjectResponse(
        status="success",
        files_written=list(request_body.files.keys()),
        message=f"Injected {len(request_body.files)} file(s)"
    )
```

### Agent Server (`docker/base-image/agent_server/routers/credentials.py`)
```python
@router.post("/api/credentials/inject")
async def inject_credentials(request: CredentialInjectRequest):
    """Write credential files to agent workspace."""
    home_dir = Path("/home/developer")
    files_written = []

    for file_path, content in request.files.items():
        target = home_dir / file_path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        target.chmod(0o600)  # Restrictive permissions
        files_written.append(str(target))

    return {"status": "success", "files_written": files_written}
```

---

## Flow 2: Export to Encrypted File

### Overview
Reads credential files from agent, encrypts them, and writes `.credentials.enc` to the agent workspace for git storage.

### Frontend
```javascript
const exportToGit = async () => {
  const result = await agentsStore.exportCredentials(agentRef.value.name)
  showNotification(`Exported ${result.files_exported} file(s) to .credentials.enc`, 'success')
}
```

### Backend (`src/backend/routers/credentials.py`)
```python
@router.post("/agents/{agent_name}/credentials/export", response_model=CredentialExportResponse)
async def export_credentials(agent_name: str, current_user: User = Depends(get_current_user)):
    """Export agent credentials to encrypted file in workspace."""
    encryption_service = CredentialEncryptionService()

    # Read credential files from agent
    file_paths = ['.env', '.mcp.json']
    files = await encryption_service.read_agent_credential_files(agent_name, file_paths)

    # Encrypt and write to agent
    encrypted = encryption_service.encrypt(files)
    await encryption_service.write_agent_credential_files(
        agent_name, {'.credentials.enc': encrypted}
    )

    return CredentialExportResponse(
        status="success",
        files_exported=len(files),
        encrypted_file=".credentials.enc"
    )
```

### Encryption Service (`src/backend/services/credential_encryption.py`)
```python
class CredentialEncryptionService:
    def __init__(self):
        key_str = os.environ.get('CREDENTIAL_ENCRYPTION_KEY')
        if not key_str:
            # Generate deterministic key from JWT secret
            jwt_secret = os.environ.get('JWT_SECRET', 'default-secret')
            key_bytes = hashlib.sha256(jwt_secret.encode()).digest()
            self.key = key_bytes
        else:
            self.key = base64.b64decode(key_str)

    def encrypt(self, files: Dict[str, str]) -> str:
        """Encrypt files dict to JSON string."""
        nonce = os.urandom(12)
        aesgcm = AESGCM(self.key)
        plaintext = json.dumps(files).encode()
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)

        return json.dumps({
            "version": 1,
            "algorithm": "AES-256-GCM",
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode()
        })

    def decrypt(self, encrypted: str) -> Dict[str, str]:
        """Decrypt JSON string back to files dict."""
        data = json.loads(encrypted)
        nonce = base64.b64decode(data['nonce'])
        ciphertext = base64.b64decode(data['ciphertext'])

        aesgcm = AESGCM(self.key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return json.loads(plaintext.decode())
```

---

## Flow 3: Import from Encrypted File

### Overview
Reads `.credentials.enc` from agent workspace, decrypts it, and writes credential files.

### Frontend
```javascript
const importFromGit = async () => {
  const result = await agentsStore.importCredentials(agentRef.value.name)
  showNotification(`Imported ${result.files_imported.length} file(s)`, 'success')
}
```

### Backend (`src/backend/routers/credentials.py`)
```python
@router.post("/agents/{agent_name}/credentials/import", response_model=CredentialImportResponse)
async def import_credentials(agent_name: str, current_user: User = Depends(get_current_user)):
    """Import credentials from encrypted file in agent workspace."""
    encryption_service = CredentialEncryptionService()

    # Read encrypted file
    enc_files = await encryption_service.read_agent_credential_files(
        agent_name, ['.credentials.enc']
    )

    if '.credentials.enc' not in enc_files:
        raise HTTPException(status_code=404, detail="No .credentials.enc file found")

    # Decrypt
    files = encryption_service.decrypt(enc_files['.credentials.enc'])

    # Write decrypted files to agent
    await encryption_service.write_agent_credential_files(agent_name, files)

    return CredentialImportResponse(
        status="success",
        files_imported=list(files.keys()),
        message=f"Imported {len(files)} file(s)"
    )
```

---

## Flow 4: Auto-Import on Agent Startup

### Overview
When an agent starts and has `.credentials.enc` but no `.env`, automatically decrypt and inject credentials.

### Startup Script (`docker/base-image/startup.sh`)
```bash
# Auto-import encrypted credentials if .env doesn't exist
if [ -f ".credentials.enc" ] && [ ! -f ".env" ]; then
    echo "Found .credentials.enc without .env, importing credentials..."
    curl -s -X POST "http://backend:8000/api/internal/decrypt-and-inject" \
         -H "Content-Type: application/json" \
         -d "{\"agent_name\": \"$AGENT_NAME\"}" || echo "Failed to import credentials"
fi
```

### Internal Endpoint (`src/backend/routers/internal.py`)
```python
@router.post("/decrypt-and-inject")
async def decrypt_and_inject(request: InternalDecryptInjectRequest):
    """Internal endpoint for agent startup - no auth required."""
    encryption_service = CredentialEncryptionService()

    # Read encrypted file from agent
    enc_files = await encryption_service.read_agent_credential_files(
        request.agent_name, ['.credentials.enc']
    )

    if '.credentials.enc' not in enc_files:
        return {"status": "skipped", "reason": "No .credentials.enc found"}

    # Decrypt and write
    files = encryption_service.decrypt(enc_files['.credentials.enc'])
    await encryption_service.write_agent_credential_files(request.agent_name, files)

    return {"status": "success", "files_written": list(files.keys())}
```

---

## MCP Tools

### inject_credentials
```typescript
// src/mcp-server/src/tools/agents.ts
{
  name: "inject_credentials",
  description: "Inject credential files into a running agent",
  inputSchema: {
    type: "object",
    properties: {
      agent_name: { type: "string", description: "Name of the agent" },
      files: {
        type: "object",
        description: "Map of file paths to content",
        additionalProperties: { type: "string" }
      }
    },
    required: ["agent_name", "files"]
  }
}
```

### export_credentials
```typescript
{
  name: "export_credentials",
  description: "Export agent credentials to encrypted .credentials.enc file",
  inputSchema: {
    type: "object",
    properties: {
      agent_name: { type: "string", description: "Name of the agent" }
    },
    required: ["agent_name"]
  }
}
```

### import_credentials
```typescript
{
  name: "import_credentials",
  description: "Import credentials from .credentials.enc file",
  inputSchema: {
    type: "object",
    properties: {
      agent_name: { type: "string", description: "Name of the agent" }
    },
    required: ["agent_name"]
  }
}
```

---

## Agent Container File Structure

```
/home/developer/
├── .env                    # KEY=VALUE credentials (source of truth)
├── .mcp.json               # MCP server configuration
├── .credentials.enc        # Encrypted backup (safe to commit to git)
└── .config/                # Service-specific config files
    └── gcloud/
        └── application_default_credentials.json
```

### Encrypted File Format
```json
{
  "version": 1,
  "algorithm": "AES-256-GCM",
  "nonce": "base64-encoded-12-byte-nonce",
  "ciphertext": "base64-encoded-encrypted-data"
}
```

### Decrypted Content (Internal)
```json
{
  ".env": "HEYGEN_API_KEY=\"sk-xxx\"\nANTHROPIC_API_KEY=\"sk-ant-xxx\"\n",
  ".mcp.json": "{\"mcpServers\": {...}}"
}
```

---

## Frontend UI

### CredentialsPanel.vue Structure

```
┌─────────────────────────────────────────────────────────────┐
│ Credentials                                     [Refresh]    │
├─────────────────────────────────────────────────────────────┤
│ Credential Files                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ .env            ✓ exists    lines: 5                    │ │
│ │ .mcp.json       ✓ exists    servers: 3                  │ │
│ │ .credentials.enc ✓ exists   encrypted backup            │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                              │
│ [Export to Git]  [Import from Git]                          │
│                                                              │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Quick Inject (.env format)                              │ │
│ │ ┌─────────────────────────────────────────────────────┐ │ │
│ │ │ HEYGEN_API_KEY=sk-xxx                               │ │ │
│ │ │ ANTHROPIC_API_KEY=sk-ant-xxx                        │ │ │
│ │ └─────────────────────────────────────────────────────┘ │ │
│ │ 2 credential(s) found                    [Inject]       │ │
│ └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

---

## Security Considerations

1. **Encryption Key**: AES-256-GCM key derived from `CREDENTIAL_ENCRYPTION_KEY` env var or JWT secret. The `GET /credentials/encryption-key` endpoint is admin-only (C-001, 2026-03-09).
2. **Agent Access Control**: All credential endpoints (`inject`, `export`, `import`, `status`) require `get_owned_agent_by_name` dependency — only agent owners and admins can manage credentials (M-006 upgraded to owner-only in #174, 2026-03-26).
3. **File Path Allowlist + Content Validation** (pentest 3.2.6 / #183 + AISEC-C2 / #590 / #598):
   - **Backend (user-facing) — path layer** — `ALLOWED_CREDENTIAL_PATHS = {.env, .credentials.enc, .mcp.json}`. `.mcp.json.template` stays blocked because the envsubst flow it feeds doesn't sanitize attacker JSON. Requests with any other path are rejected with HTTP 400.
   - **Backend (user-facing) — content layer** (#598, Layer 2 of AISEC-C2 closure) — `.mcp.json` content is structure-validated by `services.mcp_validator.validate_mcp_config` before forwarding to the agent. Validates:
     - Server names: `^[a-zA-Z0-9_-]{1,64}$`, `trinity` reserved (auto-injected entry — can't be clobbered)
     - Stdio transport: `command` ∈ allowlist `{npx, uvx, python, python3, node, bun, deno, docker}`, no path separators, ASCII-only; args without shell metacharacters; no inline-exec flags (`-c`/`--eval`/`-p`/`eval`) as first positional
     - http/sse transport: HTTPS only, no userinfo, hostname must NOT resolve to private/loopback/link-local (SSRF guard mirroring #179); header names from a small allowlist
     - env values: `${VAR}` references with valid POSIX-shape names, NOT in `RESERVED_ENV_REFS` (PATH, LD_PRELOAD, PYTHONPATH, TRINITY_MCP_API_KEY, etc.); literal substrings checked for shell metachars and credential patterns from `guardrails-baseline.json`
     - Closed schema: only `command/args/env/url/headers/type` keys allowed; only `mcpServers` at the root
     - Bounded: 64KB content cap, 32 servers max, 64 args max, 4096-char env values
   - **Agent-server (platform-internal)** — `ALLOWED_CREDENTIAL_PATHS = {.env, .mcp.json, .mcp.json.template, .credentials.enc}`. Intentionally broader so platform services (`template_service`, `credential_encryption`, `github_pat_propagation`) can write `.mcp.json` on behalf of the platform. Reachable only on the internal Docker network; cross-tenant exposure tracked separately in #589 (Redis no-auth).
4. **File Permissions**: All credential files written with 600 permissions (owner read/write only)
5. **Internal API**: `/api/internal/*` endpoints require `X-Internal-Secret` header (C-003, 2026-03-09)
6. **No Secret Logging**: Credential values never logged, only file names and counts
7. **Git Safety**: `.credentials.enc` is safe to commit - encrypted with platform key

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Disallowed file path | 400 | "Disallowed file path(s): [...]. Allowed: [...]" |
| Agent not running | 400 | "Agent is not running" |
| No encrypted file | 404 | "No .credentials.enc file found" |
| Decryption failed | 400 | "Failed to decrypt credentials" |
| Agent unreachable | 503 | "Failed to connect to agent" — applied symmetrically across `inject_credentials`, `import_credentials` (#35d4e78), and `export_credentials` (#35d4e78). Triggered when the agent container is running but its internal FastAPI server isn't bound to port 8000 yet (`httpx.ConnectError` / `TimeoutException` / `ReadError`). Mirrors the pattern in `routers/agent_files.py:82`. |

---

## Testing

### Prerequisites
- Trinity platform running
- Test agent created and running
- User logged in

### Test: Quick Inject
1. Go to Agent Detail → Credentials tab
2. Paste into Quick Inject textarea:
   ```
   HEYGEN_API_KEY=sk-test-123
   ANTHROPIC_API_KEY=sk-ant-test
   ```
3. Click "Inject"
4. Verify: Agent's `.env` file contains the credentials

### Test: Export/Import Cycle
1. Inject credentials as above
2. Click "Export to Git"
3. Verify: `.credentials.enc` appears in agent Files tab
4. Delete `.env` from agent (via Files tab or SSH)
5. Click "Import from Git"
6. Verify: `.env` is restored with original credentials

### Test: Auto-Import on Startup
1. Ensure agent has `.credentials.enc` but no `.env`
2. Restart agent
3. Verify: `.env` is automatically created from encrypted file

### MCP Test
```bash
# Via MCP
inject_credentials("my-agent", {".env": "KEY=value\n"})
export_credentials("my-agent")
import_credentials("my-agent")
```

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-05-17 | **503 mapping on `import_credentials` / `export_credentials`** (commit 35d4e78e): both endpoints previously surfaced transient agent-server connectivity failures as 500. Now catch `httpx.RequestError` and map to 503 with a warning log, matching the pre-existing pattern in `inject_credentials` and `routers/agent_files.py`. `CredentialsFileNotFoundError(ValueError)` is unaffected — when the agent server is reachable but `.credentials.enc` is missing, the 400 path still fires. |
| 2026-02-16 | **Security Fix (Credential Sanitization Cache Refresh)**: After credential injection, the agent-side credential sanitizer cache is now refreshed via `refresh_credential_values()` (routers/credentials.py:96, 298). This ensures newly injected credentials are immediately added to the sanitization pattern list, preventing them from appearing in subsequent execution logs. See `docker/base-image/agent_server/utils/credential_sanitizer.py`. |
| 2026-02-15 | **Claude Max subscription support**: Added documentation about OAuth session authentication as an alternative to API key injection. When "Authenticate in Terminal" is enabled, user can log in via `/login` in web terminal. The OAuth session stored in `~/.claude.json` is then used for all Claude Code executions (including headless), eliminating the need for `ANTHROPIC_API_KEY`. |
| 2026-02-05 | **Bug fix**: Removed orphaned credential injection loop in `crud.py:312-332` that referenced undefined `agent_credentials` variable. Added comment explaining that credentials are injected post-creation per CRED-002 design. |
| 2026-02-05 | **Complete rewrite for CRED-002**: Replaced Redis-based assignment system with encrypted file injection. Removed all Redis credential management documentation. New flows: Quick Inject, Export, Import, Auto-Import. New MCP tools. |
| 2026-01-23 | Previous version with Redis-based assignment system |

---

## Claude Max Subscription Authentication (Alternative to API Key)

**Added 2026-02-15**

As an alternative to injecting `ANTHROPIC_API_KEY`, agents can use Claude Pro/Max subscription authentication:

### Setup Flow
1. Agent owner sets "Authenticate in Terminal" in Terminal tab (disables platform API key injection)
2. User restarts the agent (container recreated without `ANTHROPIC_API_KEY`)
3. User connects to web terminal and runs `/login` in Claude Code TUI
4. OAuth flow completes, session stored in `~/.claude.json`

### What This Enables
- **Interactive terminal**: Claude Code TUI works with subscription
- **Headless executions**: Scheduled tasks use subscription instead of API billing
- **MCP-triggered calls**: `chat_with_agent` and parallel tasks use subscription
- **Persistence**: OAuth session survives container restarts (in persistent volume)

### Technical Details
- OAuth session stored in `/home/developer/.claude.json` (persistent volume)
- The mandatory `ANTHROPIC_API_KEY` check was removed from:
  - `execute_claude_code()` (line 410-414 removed)
  - `execute_headless_task()` (line 586-590 removed)
- Location: `docker/base-image/agent_server/services/claude_code.py`

### Use Cases
- **Cost management**: Use Claude Max subscription instead of API billing
- **Personal agents**: Each user authenticates with their own subscription
- **Development**: Test agents without platform API key configured

---

## Related Flows

- **Agent Lifecycle** - Credentials auto-imported on agent start
- **Subscription Management** - SUB-001: Centralized Claude Max/Pro subscription credentials. See [subscription-management.md](subscription-management.md). Subscriptions are a separate system from CRED-002 credentials - subscriptions inject `~/.claude/.credentials.json` OAuth tokens, while CRED-002 handles `.env` and `.mcp.json` files.
- **Template Processing** - Templates may include `.mcp.json.template` files
- **Settings Management** - Platform API keys in system_settings table
- **Agent Terminal** - OAuth login flow via `/login` command

---

## Legacy System (Removed)

**As of 2026-02-05**, the old Redis-based credential system has been completely removed:

| Removed Feature | Replacement |
|-----------------|-------------|
| Global `/credentials` page | Per-agent CredentialsPanel only |
| Redis credential storage (`credentials.py`) | Direct file injection |
| Credential assignments | No assignments - inject directly |
| `reload_credentials` MCP tool | `import_credentials` tool |
| Template substitution | Direct `.mcp.json` editing |
| OAuth token storage | Use external OAuth flows, inject tokens |
| `initialize_github_pat()` | GitHub PAT read directly from env/settings |
| Legacy credential injection loop in `create_agent` | Removed - credentials injected post-creation |

### Bug Fix: Orphaned Credential Loop (2026-02-05)

During the CRED-002 refactor (commit `6821f0d`), an orphaned code block was left behind in `src/backend/services/agent_service/crud.py` (lines 312-332). This code attempted to iterate over an undefined `agent_credentials` variable that had been removed earlier in the same refactor.

**The bug**: The variable `agent_credentials` was removed from lines ~183-192, but a loop that tried to iterate over `agent_credentials.items()` to inject credentials as Docker environment variables was left behind.

**The fix**: The orphaned loop was removed. Per CRED-002 design, credentials are NOT injected during agent creation. They are injected after creation via:
- `inject_credentials` endpoint (Quick Inject in Credentials tab)
- `.credentials.enc` auto-import on agent startup
- `inject_credentials` MCP tool

**Location**: `src/backend/services/agent_service/crud.py:312-315` now contains a comment explaining the design decision.

### New MCP Tool: get_credential_encryption_key

For local agents that need to encrypt/decrypt `.credentials.enc` files locally:

```typescript
{
  name: "get_credential_encryption_key",
  description: "Get the platform's credential encryption key for local agents",
  inputSchema: {
    type: "object",
    properties: {}
  }
}
```

Returns:
```json
{
  "key": "64-character-hex-string",
  "algorithm": "AES-256-GCM",
  "key_format": "hex (64 characters)",
  "note": "Store securely. Never commit to git."
}
```
