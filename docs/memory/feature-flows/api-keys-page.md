# Feature: API Keys Page

## Overview

The `/api-keys` page provides a user-friendly interface for managing MCP API keys that allow external Claude Code clients to connect to the Trinity MCP server. Users can create, view, copy, revoke, and delete API keys through this dedicated management page.

## User Story

As a Trinity user, I want to manage my MCP API keys from a dedicated page so that I can easily configure Claude Code and other MCP clients to connect to the Trinity platform.

## Entry Points

- **NavBar**: `src/frontend/src/components/NavBar.vue:39-45` - "Keys" navigation link
- **Route**: `/api-keys` - `src/frontend/src/router/index.js:132-136`
- **Direct URL**: Navigate to `http://localhost/api-keys`

---

## 1. Navigation Link (NavBar)

### Entry Point
```vue
<!-- NavBar.vue:39-45 -->
<router-link
  to="/api-keys"
  class="... inline-flex items-center px-1 pt-1 border-b-2 text-sm font-medium"
  :class="{ 'border-blue-500 ...': $route.path === '/api-keys' }"
>
  Keys
</router-link>
```

### Route Definition
```javascript
// router/index.js:132-136
{
  path: '/api-keys',
  name: 'ApiKeys',
  component: () => import('../views/ApiKeys.vue'),
  meta: { requiresAuth: true }
}
```

---

## 2. Page Load Flow

### Lifecycle (onMounted)
```javascript
// ApiKeys.vue
onMounted(async () => {
  await Promise.all([fetchMcpUrl(), fetchUserRole(), fetchApiKeys()])
  await ensureDefaultKey()  // Auto-create default key for first-time users
})
```

**MCP URL**: `fetchMcpUrl()` calls `GET /api/settings/mcp-url` to get the admin-configured URL. If set, `mcpServerUrl` computed uses it; otherwise falls back to auto-detect from `window.location.hostname`. (#76)

### Step 1: Fetch User Role
```javascript
// ApiKeys.vue:376-390
const fetchUserRole = async () => {
  const response = await fetch('/api/users/me', {
    headers: { 'Authorization': `Bearer ${authStore.token}` }
  })
  if (response.ok) {
    const userData = await response.json()
    isAdmin.value = userData.role === 'admin'
  }
}
```

**Backend**: `GET /api/users/me` returns current user with `role` field.

### Step 2: Fetch API Keys
```javascript
// ApiKeys.vue:392-407
const fetchApiKeys = async () => {
  const response = await fetch('/api/mcp/keys', {
    headers: { 'Authorization': `Bearer ${authStore.token}` }
  })
  if (response.ok) {
    apiKeys.value = await response.json()
  }
}
```

**Backend**: `GET /api/mcp/keys` (`mcp_keys.py:37-51`)
- Admin users: Returns ALL keys via `db.list_all_mcp_api_keys()`
- Regular users: Returns only their keys via `db.list_mcp_api_keys(username)`

### Step 3: Ensure Default Key (First-Time Users)
```javascript
// ApiKeys.vue:410-430
const ensureDefaultKey = async () => {
  const response = await fetch('/api/mcp/keys/ensure-default', {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${authStore.token}` }
  })
  if (response.ok) {
    const data = await response.json()
    if (data && data.api_key) {
      createdApiKey.value = data.api_key
      showKeyModal.value = true  // Show the key modal
      await fetchApiKeys()
    }
  }
}
```

**Backend**: `POST /api/mcp/keys/ensure-default` (`mcp_keys.py:54-96`)
- Checks if user has any active user-scoped keys
- If no keys exist, creates "Default MCP Key" automatically
- Returns full key on creation (shown once)

---

## 3. UI Components

### Page Structure (ApiKeys.vue:1-308)

```
+------------------------------------------+
| NavBar                                    |
+------------------------------------------+
| MCP API Keys                [Create Key] |
|   Manage API keys for accessing Trinity  |
+------------------------------------------+
| MCP Connection Info (blue info box)      |
|   .mcp.json configuration example        |
+------------------------------------------+
| API Keys List                            |
|   +------------------------------------+ |
|   | Key Name   [Active]  [Agent/User] | |
|   | trinity_mcp_abc...                 | |
|   | Created: Jan 21  Last used: 2m ago | |
|   | 45 requests      [Revoke] [Delete] | |
|   +------------------------------------+ |
|   | ...more keys...                    | |
+------------------------------------------+
```

### MCP Connection Info Panel (lines 26-51)
Displays example `.mcp.json` configuration with dynamic MCP server URL:

```javascript
// ApiKeys.vue:344-351
const mcpServerUrl = computed(() => {
  const host = window.location.hostname
  if (host === 'localhost' || host === '127.0.0.1') {
    return 'http://localhost:8080/mcp'
  }
  return `http://${host}:8080/mcp`  // Production: port 8080
})
```

### Key List Display (lines 68-134)
Each key displays:
- **Name** and **Status Badge** (Active=green, Revoked=red)
- **Scope Badge** (Agent=purple, System=orange) - admin only
- **Description** (if provided)
- **Key Prefix**: `trinity_mcp_abc...` (first 20 chars)
- **Owner Email** (admin view only)
- **Created Date** and **Last Used Date**
- **Usage Count**: "45 requests"
- **Action Buttons**: Revoke (if active), Delete

### Admin vs User View
```javascript
// ApiKeys.vue:354-359
const displayedKeys = computed(() => {
  if (isAdmin.value) {
    return apiKeys.value  // Admin sees all keys including agent-scoped
  }
  return apiKeys.value.filter(k => k.scope !== 'agent')  // Users don't see agent keys
})
```

---

## 4. Create API Key Flow

### Trigger
```vue
<!-- ApiKeys.vue:14-22 -->
<button @click="showCreateModal = true" class="...">
  <svg><!-- Plus icon --></svg>
  Create API Key
</button>
```

### Create Modal (lines 140-188)
Form fields:
- **Name** (required): Text input, e.g., "My Claude Code Key"
- **Description** (optional): Textarea for notes

### Create Handler
```javascript
// ApiKeys.vue:432-465
const createKey = async () => {
  creating.value = true
  const response = await fetch('/api/mcp/keys', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${authStore.token}`
    },
    body: JSON.stringify({
      name: newKey.value.name,
      description: newKey.value.description || null
    })
  })

  if (response.ok) {
    const data = await response.json()
    createdApiKey.value = data.api_key  // Full key only on creation
    showCreateModal.value = false
    showKeyModal.value = true           // Show success modal
    await fetchApiKeys()
  }
}
```

### Backend: `POST /api/mcp/keys` (`mcp_keys.py:14-34`)
1. Validates `McpApiKeyCreate` (name, description)
2. Calls `db.create_mcp_api_key(username, key_data)`
3. Returns `McpApiKeyWithSecret` including full `api_key`

### Database: `create_mcp_api_key()` (`db/mcp_keys.py:63-105`)
```python
def create_mcp_api_key(self, username: str, key_data: McpApiKeyCreate):
    key_id = self._generate_id()                    # secrets.token_urlsafe(16)
    api_key = self._generate_mcp_api_key()          # "trinity_mcp_" + token_urlsafe(32)
    key_hash = self._hash_api_key(api_key)          # SHA-256 hash

    cursor.execute("""
        INSERT INTO mcp_api_keys (id, name, description, key_prefix, key_hash,
                                   created_at, user_id, agent_name, scope)
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'user')
    """, (key_id, key_data.name, key_data.description, api_key[:20], key_hash, now, user["id"]))

    return McpApiKeyWithSecret(api_key=api_key, ...)  # Full key returned once
```

---

## 5. Show Created Key Modal (lines 191-296)

### Warning Banner
Yellow warning box (lines 206-217):
> "Copy the configuration below before closing - the key won't be shown again!"

### MCP Configuration Display
```javascript
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

### Copy MCP Config Button (lines 227-241)
```javascript
// ApiKeys.vue:527-537
const copyMcpConfig = async () => {
  await navigator.clipboard.writeText(getMcpConfig(createdApiKey.value))
  copiedConfig.value = true
  setTimeout(() => { copiedConfig.value = false }, 2000)
}
```

### Raw API Key Display (lines 247-282)
- Password field with show/hide toggle
- Copy button for raw key only

### Close Modal
```javascript
// ApiKeys.vue:539-545
const closeKeyModal = () => {
  showKeyModal.value = false
  createdApiKey.value = ''     // Clear the key - never shown again
  showApiKey.value = false
  copied.value = false
  copiedConfig.value = false
}
```

---

## 6. Revoke API Key Flow

### Trigger
```vue
<!-- ApiKeys.vue:118-124 -->
<button v-if="key.is_active" @click="revokeKey(key.id)" class="...">
  Revoke
</button>
```

### Confirm Dialog
```javascript
// ApiKeys.vue:467-489
const revokeKey = (keyId) => {
  confirmDialog.title = 'Revoke API Key'
  confirmDialog.message = 'Are you sure you want to revoke this API key? It will no longer work.'
  confirmDialog.confirmText = 'Revoke'
  confirmDialog.variant = 'warning'
  confirmDialog.onConfirm = async () => {
    await fetch(`/api/mcp/keys/${keyId}/revoke`, {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${authStore.token}` }
    })
    await fetchApiKeys()
  }
  confirmDialog.visible = true
}
```

### Backend: `POST /api/mcp/keys/{key_id}/revoke` (`mcp_keys.py:114-126`)
```python
@router.post("/keys/{key_id}/revoke")
async def revoke_mcp_api_key_endpoint(key_id: str, ...):
    success = db.revoke_mcp_api_key(key_id, current_user.username)
    if not success:
        raise HTTPException(status_code=404, detail="MCP API key not found")
    return {"message": f"MCP API key {key_id} revoked"}
```

### Database: `revoke_mcp_api_key()` (`db/mcp_keys.py:284-302`)
```python
def revoke_mcp_api_key(self, key_id: str, username: str) -> bool:
    # Check ownership (unless admin)
    if user["role"] != "admin":
        # Verify ownership
        ...
    cursor.execute("UPDATE mcp_api_keys SET is_active = 0 WHERE id = ?", (key_id,))
    return cursor.rowcount > 0
```

---

## 7. Delete API Key Flow

### Trigger
```vue
<!-- ApiKeys.vue:125-130 -->
<button @click="deleteKey(key.id)" class="...">
  Delete
</button>
```

### Confirm Dialog
```javascript
// ApiKeys.vue:491-513
const deleteKey = (keyId) => {
  confirmDialog.title = 'Delete API Key'
  confirmDialog.message = 'Are you sure you want to permanently delete this API key?'
  confirmDialog.confirmText = 'Delete'
  confirmDialog.variant = 'danger'
  confirmDialog.onConfirm = async () => {
    await fetch(`/api/mcp/keys/${keyId}`, {
      method: 'DELETE',
      headers: { 'Authorization': `Bearer ${authStore.token}` }
    })
    await fetchApiKeys()
  }
  confirmDialog.visible = true
}
```

### Backend: `DELETE /api/mcp/keys/{key_id}` (`mcp_keys.py:129-141`)
- Verifies ownership (or admin role)
- Permanently deletes from database

### Database: `delete_mcp_api_key()` (`db/mcp_keys.py:304-322`)
```python
def delete_mcp_api_key(self, key_id: str, username: str) -> bool:
    cursor.execute("DELETE FROM mcp_api_keys WHERE id = ?", (key_id,))
    return cursor.rowcount > 0
```

---

## 8. Key Generation and Security

### Key Format
```python
# db/mcp_keys.py:30-32
@staticmethod
def _generate_mcp_api_key() -> str:
    return f"trinity_mcp_{secrets.token_urlsafe(32)}"
```
**Result**: `trinity_mcp_<44 base64url characters>` (total ~56 chars)

### Hash Storage
```python
# db/mcp_keys.py:34-37
@staticmethod
def _hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()
```
- Only SHA-256 hash stored in `key_hash` column
- First 20 chars stored in `key_prefix` for display
- Full key returned ONLY on creation

### Database Schema
```sql
-- database.py:354-371
CREATE TABLE IF NOT EXISTS mcp_api_keys (
    id TEXT PRIMARY KEY,              -- Random ID (token_urlsafe(16))
    name TEXT NOT NULL,               -- User-provided name
    description TEXT,                 -- Optional description
    key_prefix TEXT NOT NULL,         -- First 20 chars for display
    key_hash TEXT UNIQUE NOT NULL,    -- SHA-256 hash for lookup
    created_at TEXT NOT NULL,         -- ISO timestamp
    last_used_at TEXT,                -- Updated on each validation
    usage_count INTEGER DEFAULT 0,    -- Incremented on each use
    is_active INTEGER DEFAULT 1,      -- 0 = revoked
    user_id INTEGER NOT NULL,         -- Owner user ID
    agent_name TEXT,                  -- NULL for user keys
    scope TEXT DEFAULT 'user',        -- 'user', 'agent', or 'system'
    FOREIGN KEY (user_id) REFERENCES users(id)
)
```

---

## 9. Key Scopes

| Scope | Purpose | Created By | Visible To |
|-------|---------|------------|------------|
| `user` | Human users accessing MCP from Claude Code | User via API Keys page | Owner + Admin |
| `agent` | Agent-to-agent communication | System when agent is created | Admin only |
| `system` | System agent with full access | System for trinity-system | Admin only |

### Scope Badge Display (lines 87-94)
```vue
<span v-if="key.scope === 'agent'" class="... bg-purple-100 text-purple-800">
  Agent
</span>
<span v-else-if="key.scope === 'system'" class="... bg-orange-100 text-orange-800">
  System
</span>
```

---

## 10. Usage Tracking

### Display (lines 108-114)
```vue
<span class="flex items-center">
  <svg><!-- Chart icon --></svg>
  {{ key.usage_count }} requests
</span>
```

### Backend Update on Validation
```python
# db/mcp_keys.py:220-225
cursor.execute("""
    UPDATE mcp_api_keys
    SET last_used_at = ?, usage_count = usage_count + 1
    WHERE id = ?
""", (now, row["id"]))
```

---

## Error Handling

| Error Case | HTTP Status | UI Response |
|------------|-------------|-------------|
| Create without name | Client | Disabled button (form validation) |
| Create fails | 400 | Alert: "Failed to create API key" |
| Key not found | 404 | Toast: "MCP API key not found" |
| Not authorized | 401 | Redirect to login |
| Server error | 500 | Alert: "Failed to [action] MCP API key: {error}" |

---

## Security Considerations

1. **Never Store Raw Keys**: Only SHA-256 hash stored; key shown once on creation
2. **Key Prefix Display**: Shows `trinity_mcp_abc...` - enough to identify, not enough to use
3. **Ownership Enforcement**: Users can only see/modify their own keys (unless admin)
4. **Revoke vs Delete**: Revoke deactivates (preserves audit trail); Delete removes permanently
5. **Validate Endpoint Unprotected**: `POST /api/mcp/validate` validates MCP keys, not JWTs
6. **Scope Separation**: Agent keys hidden from non-admin users
7. **Copy to Clipboard**: Uses `navigator.clipboard.writeText()` - requires HTTPS in production

---

## Related Flows

- **Upstream**: [email-authentication.md](email-authentication.md) - User login to access page
- **Backend**: [mcp-api-keys.md](mcp-api-keys.md) - Complete backend documentation
- **Downstream**: [mcp-orchestration.md](mcp-orchestration.md) - MCP tools using these keys
- **Related**: [agent-to-agent-collaboration.md](agent-to-agent-collaboration.md) - Agent-scoped keys

---

## Testing

### Prerequisites
- Trinity running (`./scripts/deploy/start.sh`)
- User logged in

### Test Steps

**UI-001: Navigate to Page**
1. Click "Keys" in NavBar
2. **Expected**: Page loads with MCP connection info and key list
3. **Verify**: URL is `/api-keys`

**UI-002: First-Time User Default Key**
1. Log in as new user (no existing keys)
2. Navigate to `/api-keys`
3. **Expected**: Modal appears with "Default MCP Key" and full config
4. **Verify**: Key appears in list after closing modal

**UI-003: Create New Key**
1. Click "Create API Key"
2. Enter Name: "Test Key"
3. Enter Description: "Testing"
4. Click "Create"
5. **Expected**: Success modal with MCP configuration JSON
6. **Verify**: Key prefix visible in list (`trinity_mcp_...`)

**UI-004: Copy MCP Config**
1. Create new key
2. Click "Copy Config" in success modal
3. **Expected**: Button shows checkmark and "Copied!"
4. **Verify**: Paste contains valid JSON with Bearer token

**UI-005: Copy Raw Key**
1. Create new key
2. Click eye icon to show key
3. Click copy button
4. **Expected**: Key copied to clipboard
5. **Verify**: Paste shows `trinity_mcp_...` string

**UI-006: Revoke Key**
1. Find active key in list
2. Click "Revoke"
3. Confirm in dialog
4. **Expected**: Key shows "Revoked" badge (red)
5. **Verify**: Using revoked key returns 401 from MCP

**UI-007: Delete Key**
1. Find any key in list
2. Click "Delete"
3. Confirm in dialog
4. **Expected**: Key removed from list
5. **Verify**: Key count decreases

**UI-008: Admin View**
1. Log in as admin
2. Navigate to `/api-keys`
3. **Expected**: See all users' keys with owner email
4. **Verify**: Agent-scoped keys visible with purple badge

### Edge Cases
- Empty state (no keys) shows helpful message
- Very long key names truncate properly
- Revoking already-revoked key (silent success)
- Creating key while disconnected (error handling)

---

## Revision History

| Date | Change |
|------|--------|
| 2026-01-21 | Initial documentation - Complete UI flow for API Keys page |
