# Feature: MCP API Keys Management

## Overview

MCP API Keys enable authentication for the Trinity MCP server, allowing Claude Code and other MCP clients to securely access agent management and orchestration tools. Users can create, list, revoke, and delete API keys through a dedicated management interface.

## User Stories

| ID | Story | Status |
|----|-------|--------|
| MCP-001 | As a user, I want to create MCP API keys so that I can authenticate Claude Code with the Trinity MCP server | Implemented |
| MCP-002 | As a user, I want to list my API keys so that I can see which keys I have created | Implemented |
| MCP-003 | As a user, I want to revoke an API key so that I can disable access without deleting the key | Implemented |
| MCP-004 | As a user, I want to delete an API key so that I can permanently remove it | Implemented |
| MCP-005 | As a user, I want to copy my MCP configuration so that I can easily configure Claude Code | Implemented |
| MCP-006 | As a first-time user, I want a default key created automatically so that I can start using MCP immediately | Implemented |
| MCP-007 | As an operator, I want a **read-only machine credential** for monitoring and ops tooling, so that enabling enforced 2FA does not cost me observability and I never have to hand a dashboard an unlimited admin key (#2323) | Implemented |

## Key scopes — and which one is a machine identity (#2323)

A key's `scope` decides what it can reach. The two an operator mints by hand:

| Scope | Reach | Mint |
|---|---|---|
| `user` | **The owner's full role.** On an admin owner that is every admin gate in the platform, plus every write. Never expires. Already exempt from interactive 2FA — the MFA gate sits on the two login routes and key validation never passes through it | any user, for themselves |
| `ops` | **Read-only, route-fenced**: fleet health, host/container telemetry, the agent roster and capacity, execution history and the live log relay, subscription reads. Every write 403s | admin only, and human-only (an interactive session — a key cannot mint one) |

Trinity always had a machine identity for admin/ops surfaces: `user` is one. What
it lacked was bounds, attribution and expiry, so the only 2FA-surviving option was
a permanent, unattributable, unlimited admin credential — a worse posture than the
control it worked around. `ops` is the bounded alternative; `user` is unchanged
(narrowing it in place would break every existing integration).

Two properties make `ops` a machine identity rather than a human's proxy:

* It is **fenced at the single auth entry point**, not per-endpoint, so an
  endpoint doing its own inline access check cannot be reached by it either.
* It is kept **out of the admin-gate allowlist**, so an admin-gated ops route
  must opt it in explicitly (`assert_admin(..., allow_scopes={"ops"})`, or
  `Depends(require_admin_allowing("ops"))` on a route gated that way). The opt-in
  flips the failure direction: a new ops route is inaccessible until someone
  grants it, rather than silently reachable.

**It does not survive its owner's offboarding, and an earlier revision of this
document said it did.** The opt-in ADMITS the scope; `role == "admin"` is still
enforced afterwards, so demoting the owner 403s the key at every admin-gated ops
read — and `get_current_user` rejects a principal whose owner carries
`suspended_at` (#995), so suspension kills the key at the auth entry point
before any of this runs. Dropping the role check for an opted-in scope was
considered and refused: it would make this bounded tier *harder* to revoke than
the unbounded `user`-scoped key it exists to displace. #2323 asked for a
credential that survives enforced **2FA** — which it does, key validation never
passing through the MFA gate — not one that survives its **owner**.

*Operator consequence, for the offboarding runbook:* an ops key is bound to the
account that minted it. Mint it under a dedicated service-admin account that is
not offboarded with people, and treat revoke-and-re-mint as part of offboarding
any admin who holds one. Pinned by `test_2323_machine_identities.py` so the
claim cannot drift back.

Every fence entry is a `GET`, asserted by a test that imports the constant. The
route set was derived from the **measured** read set of the real consumer; the
issue's own wording named only `/api/ops/*`, which would have shipped a
credential unable to run the dashboard it exists for.

The keys page badges the scope, because an unbadged ops key is visually identical
to an unbounded personal one on the page where credentials are audited.

## Attribution (#2323)

Every key-authenticated call now records **which** key acted (`mcp_key_id`,
`mcp_key_name`, `mcp_scope` on `audit_log`), derived from the presented bearer.
Before this, an admin action taken with a machine key was byte-identical in the
audit log to the owner clicking in a browser, so a leaked key could not be scoped
after the fact. `GET /api/audit-log` filters on both, which is what makes "what
did that key touch?" answerable.

`actor_type` stays `user`: the owner is the accountable party, is the only branch
that yields an email, and the enterprise user-activity view matches on it.

## Entry Points

- **UI**: `/api-keys` route - `src/frontend/src/views/ApiKeys.vue`
- **NavBar**: MCP Keys link - `src/frontend/src/components/NavBar.vue:47-49`
- **API**: All endpoints under `POST|GET|DELETE /api/mcp/keys/*`

---

## Frontend Layer

### Components

| Component | Path | Description |
|-----------|------|-------------|
| `ApiKeys.vue` | `src/frontend/src/views/ApiKeys.vue` | Main page for MCP API key management |
| `NavBar.vue` | `src/frontend/src/components/NavBar.vue:47-49` | Navigation link to MCP Keys page |
| `ConfirmDialog.vue` | `src/frontend/src/components/ConfirmDialog.vue` | Confirmation modal for revoke/delete |

### Page Structure (ApiKeys.vue)

```
Line 1-308: Template
  - MCP Connection Info panel (26-51)
  - API Keys List (54-135)
  - Create API Key Modal (139-188)
  - Show Created Key Modal (190-296)
  - Confirm Dialog (298-306)

Line 310-559: Script setup
  - State management (318-342)
  - Computed properties (344-374)
  - API methods (376-545)
  - Lifecycle (553-558)
```

### State Management

```javascript
// ApiKeys.vue:318-342
const apiKeys = ref([])           // List of user's API keys
const loading = ref(true)          // Loading state
const showCreateModal = ref(false) // Create modal visibility
const showKeyModal = ref(false)    // Created key modal visibility
const creating = ref(false)        // Creation in progress
const createdApiKey = ref('')      // Newly created key (shown once)
const showApiKey = ref(false)      // Toggle key visibility
const copied = ref(false)          // Copy feedback for key
const copiedConfig = ref(false)    // Copy feedback for config
const isAdmin = ref(false)         // Admin role check

const newKey = ref({
  name: '',
  description: ''
})
```

### Key Methods

| Method | Line | Description |
|--------|------|-------------|
| `fetchUserRole()` | 376-390 | Check if current user is admin |
| `fetchApiKeys()` | 392-407 | Load all API keys for user |
| `ensureDefaultKey()` | 410-430 | Auto-create default key for first-time users |
| `createKey()` | 432-465 | Create new API key |
| `revokeKey(keyId)` | 467-489 | Revoke (deactivate) a key |
| `deleteKey(keyId)` | 491-513 | Permanently delete a key |
| `copyApiKey()` | 515-525 | Copy raw API key to clipboard |
| `copyMcpConfig()` | 527-537 | Copy full MCP JSON config to clipboard |
| `getMcpConfig(apiKey)` | 362-374 | Generate MCP configuration JSON |

### API Calls

```javascript
// List keys - ApiKeys.vue:394-400
const response = await fetch('/api/mcp/keys', {
  headers: { 'Authorization': `Bearer ${authStore.token}` }
})

// Create key - ApiKeys.vue:436-446
const response = await fetch('/api/mcp/keys', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${authStore.token}`
  },
  body: JSON.stringify({ name: newKey.value.name, description: newKey.value.description || null })
})

// Ensure default key - ApiKeys.vue:412-417
const response = await fetch('/api/mcp/keys/ensure-default', {
  method: 'POST',
  headers: { 'Authorization': `Bearer ${authStore.token}` }
})

// Revoke key - ApiKeys.vue:474-477
const response = await fetch(`/api/mcp/keys/${keyId}/revoke`, {
  method: 'POST',
  headers: { 'Authorization': `Bearer ${authStore.token}` }
})

// Delete key - ApiKeys.vue:498-501
const response = await fetch(`/api/mcp/keys/${keyId}`, {
  method: 'DELETE',
  headers: { 'Authorization': `Bearer ${authStore.token}` }
})
```

### MCP Configuration Generation

```javascript
// ApiKeys.vue:344-351
const mcpServerUrl = computed(() => {
  const host = window.location.hostname
  if (host === 'localhost' || host === '127.0.0.1') {
    return 'http://localhost:8080/mcp'
  }
  return `http://${host}:8080/mcp`
})

// ApiKeys.vue:362-374
const getMcpConfig = (apiKey) => {
  return JSON.stringify({
    mcpServers: {
      trinity: {
        type: "http",
        url: mcpServerUrl.value,
        headers: {
          Authorization: `Bearer ${apiKey}`
        }
      }
    }
  }, null, 2)
}
```

### Admin vs User View

```javascript
// ApiKeys.vue:354-359 - Filter agent-scoped keys for non-admins
const displayedKeys = computed(() => {
  if (isAdmin.value) {
    return apiKeys.value
  }
  return apiKeys.value.filter(k => k.scope !== 'agent')
})
```

---

## Backend Layer

### Router Registration

```python
# src/backend/main.py (router include)
from routers.mcp_keys import router as mcp_keys_router
app.include_router(mcp_keys_router)
```

### Endpoints

| Method | Endpoint | Handler | Line | Description |
|--------|----------|---------|------|-------------|
| `POST` | `/api/mcp/keys` | `create_mcp_api_key_endpoint()` | `mcp_keys.py:14-34` | Create new API key |
| `GET` | `/api/mcp/keys` | `list_mcp_api_keys_endpoint()` | `mcp_keys.py:37-51` | List user's keys (admin sees all) |
| `POST` | `/api/mcp/keys/ensure-default` | `ensure_default_mcp_api_key()` | `mcp_keys.py:54-96` | Auto-create default key |
| `GET` | `/api/mcp/keys/{key_id}` | `get_mcp_api_key_endpoint()` | `mcp_keys.py:99-111` | Get single key details |
| `POST` | `/api/mcp/keys/{key_id}/revoke` | `revoke_mcp_api_key_endpoint()` | `mcp_keys.py:114-126` | Revoke (deactivate) key |
| `DELETE` | `/api/mcp/keys/{key_id}` | `delete_mcp_api_key_endpoint()` | `mcp_keys.py:129-141` | Permanently delete key |
| `POST` | `/api/mcp/validate` | `validate_mcp_api_key_http_endpoint()` | `mcp_keys.py:144-180` | Validate key (used by MCP server) |

### Business Logic

#### Create API Key (POST /api/mcp/keys)
```python
# mcp_keys.py:14-34
@router.post("/keys", response_model=McpApiKeyWithSecret)
async def create_mcp_api_key_endpoint(
    key_data: McpApiKeyCreate,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    api_key = db.create_mcp_api_key(current_user.username, key_data)
    if not api_key:
        raise HTTPException(status_code=400, detail="Failed to create API key")
    return api_key
```

#### List API Keys (GET /api/mcp/keys)
```python
# mcp_keys.py:37-51
@router.get("/keys", response_model=List[McpApiKey])
async def list_mcp_api_keys_endpoint(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    if current_user.role == "admin":
        keys = db.list_all_mcp_api_keys()  # Admin sees all
    else:
        keys = db.list_mcp_api_keys(current_user.username)  # User sees own
    return keys
```

#### Ensure Default Key (POST /api/mcp/keys/ensure-default)
```python
# mcp_keys.py:54-96
@router.post("/keys/ensure-default", response_model=McpApiKeyWithSecret | None)
async def ensure_default_mcp_api_key(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    # Check if user has any active user-scoped keys
    keys = db.list_mcp_api_keys(current_user.username)
    user_keys = [k for k in keys if k.scope == "user" and k.is_active]

    if user_keys:
        return None  # Already has a key

    # Create default key
    key_data = McpApiKeyCreate(
        name="Default MCP Key",
        description="Auto-generated key for MCP access"
    )
    return db.create_mcp_api_key(current_user.username, key_data)
```

#### Validate API Key (POST /api/mcp/validate)
```python
# mcp_keys.py:144-180 - Called by MCP server, NOT JWT-protected
@router.post("/validate")
async def validate_mcp_api_key_http_endpoint(request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    api_key = auth_header[7:]  # Remove "Bearer " prefix
    result = db.validate_mcp_api_key(api_key)

    if not result:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")

    return {
        "valid": True,
        "user_id": result.get("user_id"),
        "user_email": result.get("user_email"),
        "key_name": result.get("key_name"),
        "agent_name": result.get("agent_name"),  # For agent-to-agent auth
        "scope": result.get("scope", "user")
    }
```

---

## Data Layer

### Database Schema

```sql
-- database.py:356-370
CREATE TABLE IF NOT EXISTS mcp_api_keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    key_prefix TEXT NOT NULL,       -- First 20 chars for display
    key_hash TEXT UNIQUE NOT NULL,  -- SHA-256 hash of full key
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    usage_count INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1,    -- 0 = revoked
    user_id INTEGER NOT NULL,
    agent_name TEXT,                -- For agent-scoped keys
    scope TEXT DEFAULT 'user',      -- 'user', 'agent', or 'system'
    FOREIGN KEY (user_id) REFERENCES users(id)
)
```

### Pydantic Models

```python
# db_models.py:69-104
class McpApiKeyCreate(BaseModel):
    name: str
    description: Optional[str] = None

class McpApiKey(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    key_prefix: str
    created_at: datetime
    last_used_at: Optional[datetime] = None
    usage_count: int = 0
    is_active: bool = True
    user_id: int
    username: str
    user_email: Optional[str] = None
    agent_name: Optional[str] = None
    scope: str = "user"

class McpApiKeyWithSecret(McpApiKey):
    api_key: str  # Only returned on creation
```

### Database Operations (db/mcp_keys.py)

| Method | Line | Description |
|--------|------|-------------|
| `_generate_id()` | 25-27 | Generate unique ID via `secrets.token_urlsafe(16)` |
| `_generate_mcp_api_key()` | 29-32 | Generate key with `trinity_mcp_` prefix |
| `_hash_api_key(api_key)` | 34-37 | SHA-256 hash for secure storage |
| `_row_to_mcp_api_key(row)` | 39-61 | Convert DB row to Pydantic model |
| `create_mcp_api_key()` | 63-105 | Create user-scoped key |
| `create_agent_mcp_api_key()` | 107-161 | Create agent-scoped key |
| `get_agent_mcp_api_key()` | 163-176 | Get active key for an agent |
| `delete_agent_mcp_api_key()` | 178-188 | Delete all keys for an agent |
| `validate_mcp_api_key()` | 190-236 | Validate key and update usage stats |
| `get_mcp_api_key()` | 238-253 | Get key by ID (ownership check) |
| `list_mcp_api_keys()` | 255-270 | List keys for a user |
| `list_all_mcp_api_keys()` | 272-282 | List all keys (admin) |
| `revoke_mcp_api_key()` | 284-302 | Set `is_active = 0` |
| `delete_mcp_api_key()` | 304-322 | Permanently delete key |

### Key Generation and Security

```python
# db/mcp_keys.py:29-37
@staticmethod
def _generate_mcp_api_key() -> str:
    """Generate a new MCP API key with prefix."""
    return f"trinity_mcp_{secrets.token_urlsafe(32)}"

@staticmethod
def _hash_api_key(api_key: str) -> str:
    """Hash an API key for secure storage."""
    return hashlib.sha256(api_key.encode()).hexdigest()
```

**Key Format**: `trinity_mcp_<44 random base64url characters>`
**Storage**: Only SHA-256 hash stored in database; raw key returned once on creation

### Validation Flow

```python
# db/mcp_keys.py:190-236
def validate_mcp_api_key(self, api_key: str) -> Optional[Dict]:
    key_hash = self._hash_api_key(api_key)

    # Lookup by hash
    cursor.execute("""
        SELECT k.id, k.name, k.user_id, k.is_active, k.agent_name, k.scope,
               u.username, u.email
        FROM mcp_api_keys k
        JOIN users u ON k.user_id = u.id
        WHERE k.key_hash = ?
    """, (key_hash,))

    # Update usage stats on successful validation
    cursor.execute("""
        UPDATE mcp_api_keys
        SET last_used_at = ?, usage_count = usage_count + 1
        WHERE id = ?
    """, (now, row["id"]))

    return {
        "key_id": row["id"],
        "key_name": row["name"],
        "user_id": row["username"],
        "user_email": row["email"],
        "agent_name": row["agent_name"],
        "scope": row["scope"] or "user"
    }
```

---

## MCP Server Integration

### Server-Side Validation (src/mcp-server/src/server.ts)

```typescript
// server.ts:27-34
export interface McpApiKeyValidationResult {
  valid: boolean;
  user_id?: string;
  user_email?: string;
  key_name?: string;
  agent_name?: string;
  scope?: "user" | "agent" | "system";
}

// server.ts:39-59
async function validateMcpApiKey(
  trinityApiUrl: string,
  apiKey: string
): Promise<McpApiKeyValidationResult | null> {
  const response = await fetch(`${trinityApiUrl}/api/mcp/validate`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
    },
  });

  if (response.ok) {
    return await response.json();
  }
  return null;
}
```

### Authentication Middleware

```typescript
// server.ts:114-151
const server = new FastMCP({
  name,
  version,
  authenticate: requireApiKey
    ? async (request) => {
        const authHeader = request.headers["authorization"];
        if (!authHeader || !authHeader.startsWith("Bearer ")) {
          throw new Error("Missing or invalid Authorization header");
        }

        const apiKey = authHeader.substring(7);
        const result = await validateMcpApiKey(trinityApiUrl, apiKey);

        if (result && result.valid) {
          // Return auth context for use in tools
          return {
            userId: result.user_id || "unknown",
            userEmail: result.user_email,
            keyName: result.key_name || "unknown",
            agentName: result.agent_name,
            scope: result.scope,
            mcpApiKey: apiKey,
          };
        }

        throw new Error("Invalid API key");
      }
    : undefined,
});
```

---

## Error Handling

| Error Case | HTTP Status | Message | Handler |
|------------|-------------|---------|---------|
| Missing auth header | 401 | Missing or invalid Authorization header | `validate_mcp_api_key_http_endpoint` |
| Invalid API key | 401 | Invalid or inactive API key | `validate_mcp_api_key_http_endpoint` |
| Key not found | 404 | MCP API key not found | `get_mcp_api_key_endpoint` |
| Revoke not found | 404 | MCP API key not found | `revoke_mcp_api_key_endpoint` |
| Delete not found | 404 | MCP API key not found | `delete_mcp_api_key_endpoint` |
| Creation failed | 400 | Failed to create API key | `create_mcp_api_key_endpoint` |
| Server error | 500 | Failed to [action] MCP API key: {error} | All endpoints |

---

## Security Considerations

1. **Key Storage**: Only SHA-256 hash stored; raw key shown once on creation
2. **Key Prefix**: `trinity_mcp_` prefix for easy identification
3. **Ownership Check**: Users can only see/modify their own keys (unless admin)
4. **Revocation vs Deletion**: Revoke deactivates (audit trail); delete removes permanently
5. **Validation Endpoint**: `/api/mcp/validate` is NOT JWT-protected (validates MCP keys, not JWTs)
6. **Scope Separation**: User keys vs agent keys vs system keys for different access levels
7. **Usage Tracking**: `last_used_at` and `usage_count` updated on each validation

---

## Key Scopes

| Scope | Purpose | Created By |
|-------|---------|------------|
| `user` | Human users accessing MCP from Claude Code | User via UI |
| `agent` | Agent-to-agent communication | System when agent is created |
| `system` | System agent with full access | System for trinity-system agent |

### Agent-Scoped Key Authentication Context (NOTIF-003)

When an agent-scoped MCP API key is used for authentication, the `agent_name` from the key is propagated to the `User` model. This enables downstream features to know which agent is making the request.

**Authentication Flow** (`src/backend/dependencies.py:137-155`):

```python
# Try MCP API key authentication
mcp_key_info = db.validate_mcp_api_key(token)
if mcp_key_info:
    user = db.get_user_by_email(user_email) if user_email else db.get_user_by_username(user_id)
    if user:
        # For agent-scoped keys, include the agent_name
        agent_name = mcp_key_info.get("agent_name") if mcp_key_info.get("scope") == "agent" else None
        return User(
            id=user["id"],
            username=user["username"],
            email=user.get("email"),
            role=user["role"],
            agent_name=agent_name  # Populated for agent-scoped keys only
        )
```

**User Model** (`src/backend/models.py:56-63`):
```python
class User(BaseModel):
    """Authenticated user."""
    id: int
    username: str
    email: Optional[str] = None
    role: str = "user"
    # For agent-scoped MCP API keys, this is the agent name
    agent_name: Optional[str] = None
```

**Use Cases**:
- **Notifications** (NOTIF-001): `current_user.agent_name` ensures notifications are attributed to the correct agent
- **Activity Tracking**: Agent-initiated activities can be correctly attributed
- **Audit Logging**: Actions performed by agents via MCP are traceable to the specific agent

---

## Testing

### Prerequisites
- Trinity backend running (`./scripts/deploy/start.sh`)
- User logged in with valid JWT

### Test Steps

**MCP-001: Create API Key**
1. Navigate to `/api-keys`
2. Click "Create API Key" button
3. Enter name: "Test Key"
4. Enter description: "Testing"
5. Click "Create"
6. **Expected**: Modal shows with MCP configuration JSON
7. **Verify**: Key prefix shown in list (e.g., `trinity_mcp_abc...`)

**MCP-002: List API Keys**
1. Navigate to `/api-keys`
2. **Expected**: See list of created keys with name, prefix, created date, usage count
3. **Verify**: Admin users see all keys; regular users see only their own

**MCP-003: Revoke API Key**
1. Find active key in list
2. Click "Revoke" button
3. Confirm in dialog
4. **Expected**: Key status changes to "Revoked" (red badge)
5. **Verify**: Using revoked key in MCP returns 401

**MCP-004: Delete API Key**
1. Find any key in list
2. Click "Delete" button
3. Confirm in dialog
4. **Expected**: Key removed from list
5. **Verify**: Key no longer exists in database

**MCP-005: Copy MCP Configuration**
1. Create new API key
2. In modal, click "Copy Config"
3. **Expected**: JSON copied to clipboard
4. **Verify**: Paste into `.mcp.json` and test with Claude Code

**MCP-006: Ensure Default Key**
1. Create new user account
2. Navigate to `/api-keys` for first time
3. **Expected**: Modal appears with auto-generated "Default MCP Key"
4. **Verify**: Only happens if user has no active user-scoped keys

### Edge Cases
- Creating key with empty name (should fail validation)
- Revoking already-revoked key (should succeed silently)
- Deleting key while MCP connection active (connection should fail on next request)
- Admin viewing agent-scoped keys (should be visible)
- Non-admin viewing agent-scoped keys (should be filtered out)

### Verification Commands

```bash
# Test key validation via API
curl -X POST http://localhost:8000/api/mcp/validate \
  -H "Authorization: Bearer trinity_mcp_YOUR_KEY_HERE"

# List keys (requires JWT)
curl http://localhost:8000/api/mcp/keys \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

---

## Trinity Connect Authentication (Added 2026-02-05)

MCP API keys are also used to authenticate WebSocket connections to the `/ws/events` endpoint for Trinity Connect.

### WebSocket Authentication Flow

**Location**: `src/backend/main.py:370-435`

```python
# Client connects with MCP API key as query parameter
ws://localhost:8000/ws/events?token=trinity_mcp_xxx

# Server validates key via db.validate_mcp_api_key()
result = db.validate_mcp_api_key(token)
if not result:
    await websocket.close(code=4001, reason="Invalid or inactive MCP API key")
```

### Access Control

The validated key provides user identity, which determines accessible agents:

```python
# Get user's accessible agents for event filtering
accessible_agents = db.get_accessible_agent_names(user_email, is_admin)
```

### Key Scopes for Trinity Connect

| Scope | Access |
|-------|--------|
| `user` | Events for owned + shared agents |
| `agent` | Events for specific agent only (not recommended for Trinity Connect) |
| `system` | All events (admin-equivalent) |

### Related Documentation

- **Trinity Connect**: [trinity-connect.md](trinity-connect.md) - Full feature flow for `/ws/events` endpoint

---

## Related Flows

- **Upstream**: [email-authentication.md](email-authentication.md) - User authentication for JWT
- **Downstream**: [mcp-orchestration.md](mcp-orchestration.md) - MCP tools that use these keys
- **Related**: [agent-to-agent-collaboration.md](agent-to-agent-collaboration.md) - Agent-scoped key usage
- **Related**: [trinity-connect.md](trinity-connect.md) - WebSocket authentication for event listening (Added 2026-02-05)

---

## Revision History

| Date | Change |
|------|--------|
| 2026-01-13 | Initial documentation |
| 2026-02-05 | **Trinity Connect Authentication**: Added section documenting MCP API key usage for `/ws/events` WebSocket authentication. Keys provide user identity for server-side event filtering. |
| 2026-02-20 | **NOTIF-003 Agent Attribution**: Added "Agent-Scoped Key Authentication Context" section. Agent-scoped keys now populate `User.agent_name` field during authentication (dependencies.py:147-154). Enables correct attribution for notifications, activity tracking, and audit logging. |
