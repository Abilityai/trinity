# Feature: Subscription Management (SUB-002)

## Overview

Centralized management of Claude Max/Pro subscription tokens. Admins register long-lived tokens (from `claude setup-token`, ~1 year lifetime) and assign them to agents. Tokens are injected as the `CLAUDE_CODE_OAUTH_TOKEN` env var on agent containers at creation time -- no file injection needed.

## User Story

As a Trinity platform admin, I want to register long-lived Claude subscription tokens centrally so that I can assign them to multiple agents without re-entering credentials, using subscription billing instead of per-API-call billing.

---

## SUB-001 to SUB-002 Migration Summary

| Aspect | SUB-001 | SUB-002 |
|--------|---------|---------|
| **Registration input** | `credentials_json` (raw JSON blob from `~/.claude/.credentials.json`) | `token` (string from `claude setup-token`, prefix `sk-ant-oat01-`) |
| **Storage format** | `encrypt({".credentials.json": json_blob})` | `encrypt({"token": token_string})` |
| **Injection method** | HTTP POST to agent `/api/credentials/inject` writing `.claude/.credentials.json` file | `CLAUDE_CODE_OAUTH_TOKEN` env var set on container creation |
| **Token lifetime** | ~8-15 hours (short-lived OAuth) | ~1 year (long-lived setup token) |
| **File injection** | Required on every start + hot-injection to running agents | Not needed -- env var persists across restarts |
| **Deleted functions** | -- | `inject_subscription_to_agent()`, `inject_subscription_on_start()` removed entirely |
| **Auth mode detection** | DB state + HTTP call to agent `/api/credentials/status` | Pure DB-state detection only |
| **Monitoring** | `.credentials.json` file checks, auto-remediation, credential alerts | Removed -- env var does not expire mid-session |
| **Frontend input** | File upload (drag-and-drop `.credentials.json`) | Password input field for token string |
| **Validation** | `JSON.parse()` on uploaded file | `sk-ant-oat01-` prefix check (frontend + Pydantic validator) |

---

## Key Concepts

### Authentication Hierarchy (Claude Code Credential Priority)

Claude Code checks credentials in this order (highest priority first):

1. **API Key** (`ANTHROPIC_API_KEY` env var) -- If present, Claude Code uses this and **ignores** the OAuth token
2. **Subscription Token** (`CLAUDE_CODE_OAUTH_TOKEN` env var) -- Long-lived token from `claude setup-token`

**Critical**: Because `ANTHROPIC_API_KEY` takes precedence, Trinity must **remove** the API key env var from the container when a subscription is assigned. If both exist, Claude Code uses the API key and never falls back to the token.

This mutual exclusion is enforced by:
- `check_api_key_env_matches()` in `src/backend/services/agent_service/helpers.py:367-406` -- detects subscription/API key conflicts on agent start
- `recreate_container_with_updated_config()` in `src/backend/services/agent_service/lifecycle.py:243-403` -- sets `CLAUDE_CODE_OAUTH_TOKEN` and removes `ANTHROPIC_API_KEY` when subscription is assigned
- `assign_subscription_to_agent` endpoint in `src/backend/routers/subscriptions.py:190-253` -- restarts running agents to apply env var changes
- `clear_agent_subscription` endpoint in `src/backend/routers/subscriptions.py:255-304` -- restarts running agents to restore API key

### Data Model

```
subscription_credentials          agent_ownership
+------------------------+        +------------------------+
| id (PK, UUID)          |        | agent_name (PK)        |
| name (UNIQUE)          |        | owner_id (FK)          |
| encrypted_credentials  |<-------| subscription_id (FK)   |
| subscription_type      |        | ...                    |
| rate_limit_tier        |        +------------------------+
| owner_id (FK users)    |
| created_at, updated_at |
+------------------------+
```

### Token Flow (SUB-002)

```
Admin's Machine                  Trinity Backend                    Agent Container
+----------------+              +-------------------+               +------------------+
| claude         |  register    | subscription_     |   env var     | CLAUDE_CODE_     |
| setup-token    | -----------> | credentials       | ------------> | OAUTH_TOKEN=     |
| sk-ant-oat01-  |  (encrypt)   | (encrypted AES)   |  (on create)  | sk-ant-oat01-... |
+----------------+              +-------------------+               +------------------+
```

### Auto-Assign on Agent Creation (#74)

New agents are automatically assigned the subscription with fewest agents (round-robin). Subscriptions that failed within the last 2h — rate-limit OR auth (#2352) — are skipped. Falls back to platform API key if no viable subscription exists.

```
create_agent_internal()
  ├── db.get_least_used_subscription()       ← SQL: COUNT + ORDER BY agent_count ASC, name ASC
  │     └── skip recently-failed, any kind (has_recent_subscription_failures)
  ├── db.get_subscription_token(id)          ← AES-256 decrypt
  ├── env_vars['CLAUDE_CODE_OAUTH_TOKEN'] = token
  ├── env_vars.pop('ANTHROPIC_API_KEY')
  ├── ... container creation ...
  ├── db.register_agent_owner()
  └── db.assign_subscription_to_agent()      ← persist assignment
```

**Files:**
- `src/backend/db/subscriptions.py:540` — `get_least_used_subscription()` (iterates candidates, skips rate-limited)
- `src/backend/services/agent_service/crud.py:301` — lookup + env var injection before container creation
- `src/backend/services/agent_service/crud.py:507` — DB persist after `register_agent_owner()`

**Edge cases:**
- No subscriptions → skip, use `ANTHROPIC_API_KEY`
- Token decrypt fails → warn, keep `ANTHROPIC_API_KEY`
- Exception → warn, keep `ANTHROPIC_API_KEY`
- System agents → separate `_create_system_agent()` path, unaffected
- Equal agent count → alphabetical tie-break (`s.name ASC`)

---

## Entry Points

| UI Location | API Endpoint | Purpose |
|-------------|--------------|---------|
| **Agent Detail: AgentHeader subscription switcher** | `GET /api/subscriptions/agents/{agent_name}/auth` | Show auth badge; admins see dropdown to switch subscription inline |
| **Agent Detail: AgentHeader subscription switcher** | `GET /api/subscriptions` | Load available subscriptions for dropdown (admin-only) |
| **Agent Detail: AgentHeader subscription switcher** | `PUT /api/subscriptions/agents/{agent_name}?subscription_name={name}` | Assign subscription from dropdown |
| **Agent Detail: AgentHeader subscription switcher** | `DELETE /api/subscriptions/agents/{agent_name}` | Revert to API Key from dropdown |
| **Settings Page: Claude Subscriptions** | `GET /api/subscriptions/encryption-status` | Check if CREDENTIAL_ENCRYPTION_KEY is configured (shows warning banner if not) |
| **Settings Page: Claude Subscriptions** | `GET /api/subscriptions` | List subscriptions (Settings UI) |
| **Settings Page: Add Subscription** | `POST /api/subscriptions` | Register via token input (returns 503 if encryption key missing) |
| **Settings Page: Delete Button** | `DELETE /api/subscriptions/{id}` | Delete with cascade confirmation |
| MCP Tool: `register_subscription` | `POST /api/subscriptions` | Register new subscription |
| MCP Tool: `list_subscriptions` | `GET /api/subscriptions` | List all subscriptions with agents |
| MCP Tool: `assign_subscription` | `PUT /api/subscriptions/agents/{agent_name}` | Assign subscription to agent |
| MCP Tool: `clear_agent_subscription` | `DELETE /api/subscriptions/agents/{agent_name}` | Clear subscription from agent |
| MCP Tool: `get_agent_auth` | `GET /api/subscriptions/agents/{agent_name}/auth` | Get agent auth status |
| MCP Tool: `delete_subscription` | `DELETE /api/subscriptions/{subscription_id}` | Delete subscription |
| Fleet Dashboard | `GET /api/ops/auth-report` | Fleet-wide auth status report |
| Admin: subscription usage | `GET /api/subscriptions/{subscription_id}/usage` | Rolling 5h/7d token and cost totals for one subscription (SUB-004) |

---

## Frontend UI (Settings Page)

### Overview

The Settings page (`/settings`) includes a "Claude Subscriptions" section for managing subscription tokens through a web UI. This provides an alternative to MCP tools for subscription management.

### Location

- **File**: `src/frontend/src/views/Settings.vue`
- **Template**: Lines 483-775 (Claude Subscriptions section)
- **State**: Lines 1422-1441
- **Methods**: Lines 2082-2249

### UI Components

#### 1. Add Subscription Form (`Settings.vue:512-591`)

Form fields:
- **Name** (`v-model="newSubscription.name"`) -- Unique identifier (e.g., "eugene-max")
- **Type** (`v-model="newSubscription.type"`) -- Dropdown: Max / Pro / Unknown
- **Token** (`v-model="newSubscription.token"`) -- Password input for `sk-ant-oat01-...` token

```html
<input
  type="password"
  v-model="newSubscription.token"
  placeholder="sk-ant-oat01-..."
  :class="{ 'border-red-400': newSubscription.token && !newSubscription.token.startsWith('sk-ant-oat01-') }"
/>
```

Validation:
- Frontend: inline prefix check with red border and error text if token does not start with `sk-ant-oat01-`
- Register button disabled until name is provided AND token starts with `sk-ant-oat01-`

Buttons:
- **Clear** -- Reset form (visible when form has data)
- **Register Subscription** -- Submit (disabled until name + valid token provided)

#### 2. Subscriptions Table (`Settings.vue:594-737`)

Columns:
| Column | Data |
|--------|------|
| Name | Subscription name with expand chevron |
| Type | Badge: purple (Max) or blue (Pro) |
| Agents | Count badge: "N agent(s)" |
| Created | Formatted date |
| Actions | Delete button |

Features:
- **Loading spinner** when `loadingSubscriptions` is true
- **Empty state** when no subscriptions registered
- **Expandable rows** showing owner, rate limit tier, assigned agents
- **Delete confirmation** with cascade warning (agent count)

#### 3. Expanded Details Row (`Settings.vue:662-733`)

Shows when row is clicked (`expandedSubscriptions.has(sub.id)`):
- Owner email
- Rate limit tier (if set)
- **Assigned agents with X buttons** — Each agent badge includes a small X button that calls `unassignAgentFromSubscription()`. Shows per-agent spinner during removal.
- **Agent assignment dropdown + Assign button** — `<select>` listing available agents (excludes agents already on this subscription). Agents assigned to other subscriptions show `"(on sub-name)"` suffix. Plus an "Assign" button that calls `assignAgentToSubscription()`.
- Empty state: "No agents assigned yet."

### State Variables (`Settings.vue:1422-1452`)

```javascript
// Subscriptions state (SUB-002)
const subscriptions = ref([])
const loadingSubscriptions = ref(false)
const addingSubscription = ref(false)
const deletingSubscription = ref(null)
const expandedSubscriptions = ref(new Set())
const newSubscription = ref({
  name: '',
  type: 'max',
  token: ''              // Long-lived token from `claude setup-token`
})

// Agent assignment state (SUB-003)
const allAgents = ref([])           // Cached from GET /api/agents
const loadingAgents = ref(false)
const assigningAgent = ref(null)    // Agent name being assigned (spinner)
const unassigningAgent = ref(null)  // Agent name being removed (spinner)
const selectedAgentToAssign = ref({}) // Map of sub.id -> dropdown selection

// Computed: builds { agentName: subscriptionName } from all subscriptions
const agentSubscriptionMap = computed(() => { ... })
```

### Methods

#### `fetchAgentList()` (`Settings.vue:2191-2204`)

Fetches agent list on first row expand. Cached — returns immediately if already loaded.

```javascript
async function fetchAgentList() {
  if (allAgents.value.length > 0 || loadingAgents.value) return
  loadingAgents.value = true
  try {
    const response = await axios.get('/api/agents', { headers: authStore.authHeader })
    allAgents.value = response.data || []
  } finally {
    loadingAgents.value = false
  }
}
```

#### `getAvailableAgents(subId)` (`Settings.vue:2206-2216`)

Returns agents not assigned to this subscription, sorted unassigned-first (agents on other subs sorted to end).

#### `assignAgentToSubscription(subName, agentName)` (`Settings.vue:2218-2233`)

Calls `PUT /api/subscriptions/agents/{name}?subscription_name=X`. Reloads subscriptions and clears dropdown selections on success.

#### `unassignAgentFromSubscription(agentName)` (`Settings.vue:2235-2249`)

Shows confirm dialog, then calls `DELETE /api/subscriptions/agents/{name}`. Reloads subscriptions on success.

#### `loadSubscriptions()`

Fetches all subscriptions on page load.

```javascript
async function loadSubscriptions() {
  loadingSubscriptions.value = true
  try {
    const response = await axios.get('/api/subscriptions', {
      headers: authStore.authHeader
    })
    subscriptions.value = response.data || []
  } catch (e) {
    if (e.response?.status !== 403) {
      error.value = e.response?.data?.detail || 'Failed to load subscriptions'
    }
  } finally {
    loadingSubscriptions.value = false
  }
}
```

#### `clearNewSubscription()` (`Settings.vue:2112-2118`)

Resets the add form.

```javascript
function clearNewSubscription() {
  newSubscription.value = {
    name: '',
    type: 'max',
    token: ''
  }
}
```

#### `addSubscription()` (`Settings.vue:2120-2148`)

Registers a new subscription via API.

```javascript
async function addSubscription() {
  if (!newSubscription.value.name || !newSubscription.value.token.startsWith('sk-ant-oat01-')) return

  addingSubscription.value = true
  try {
    await axios.post('/api/subscriptions', {
      name: newSubscription.value.name,
      token: newSubscription.value.token,
      subscription_type: newSubscription.value.type || null
    }, { headers: authStore.authHeader })

    clearNewSubscription()
    await loadSubscriptions()
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to register subscription'
  } finally {
    addingSubscription.value = false
  }
}
```

#### `deleteSubscription(subscription)` (`Settings.vue:2150-2178`)

Deletes subscription with cascade confirmation.

```javascript
async function deleteSubscription(subscription) {
  if (!confirm(`Delete subscription "${subscription.name}"?\n\nThis will clear the subscription from all ${subscription.agent_count || 0} assigned agent(s).`)) {
    return
  }
  deletingSubscription.value = subscription.id
  try {
    await axios.delete(`/api/subscriptions/${subscription.id}`, {
      headers: authStore.authHeader
    })
    expandedSubscriptions.value.delete(subscription.id)
    await loadSubscriptions()
  } catch (e) {
    error.value = e.response?.data?.detail || 'Failed to delete subscription'
  } finally {
    deletingSubscription.value = null
  }
}
```

#### `toggleSubscriptionDetails(subscriptionId)` (`Settings.vue:2180-2189`)

Toggles row expansion with forced reactivity update. When expanding, also calls `fetchAgentList()` to load available agents for the dropdown.

### Data Flow

```
User Action                Frontend                      Backend
    |                         |                             |
    | Enter token string      |                             |
    |------------------------>|                             |
    |                         | Inline prefix validation    |
    |                         |                             |
    | Click "Register"        |                             |
    |------------------------>|                             |
    |                         | POST /api/subscriptions     |
    |                         | {name, token, sub_type}     |
    |                         |----------------------------->|
    |                         |                             | Pydantic: validate sk-ant-oat01-
    |                         |                             | Encrypt {"token": token}
    |                         |                             | Upsert to DB
    |                         |<-----------------------------|
    |                         | loadSubscriptions()         |
    |                         | GET /api/subscriptions      |
    |                         |----------------------------->|
    |                         |<-----------------------------|
    | See new subscription    |                             |
    |<------------------------|                             |
```

---

## Frontend UI: Agent Detail Subscription Switcher

### Overview

On the Agent Detail page, admins see the auth method badge in the `AgentHeader` component as a clickable subscription switcher. Non-admins (and unauthenticated users) see the badge as a static read-only indicator. The switcher allows reassigning or clearing a subscription without leaving the page.

### Location

- **AgentHeader badge/switcher**: `src/frontend/src/components/AgentHeader.vue:109-145`
- **AgentDetail wiring**: `src/frontend/src/views/AgentDetail.vue:27-67` (template), `307-309` (state), `784-817` (functions), `927-928` (onMounted), `840` (route-change watch)

### UI Behavior

The auth badge renders inside a `<div class="relative inline-flex items-center">` wrapper at `AgentHeader.vue:110`. The badge text and color reflect the current auth mode:

| auth_mode | Badge color | Badge text |
|-----------|-------------|------------|
| `subscription` | Amber | Subscription name |
| `api_key` | Gray | API Key |
| _(none)_ | Red | No Auth |

When `subscriptionChanging` is true, a spinner replaces the normal badge text prefix.

A chevron-down icon is shown (and the invisible `<select>` is rendered) only when both conditions are true:
- `subscriptions !== null` — admin-only; non-admins get a 403 on `GET /api/subscriptions` and `availableSubscriptions` stays `null`
- `agent.can_share` — agent is owned by the current user or user is admin

The `<select>` is overlaid absolutely on the badge with `opacity-0` so it uses the native OS picker UI while the badge shows the styled display.

Options in the dropdown:
- `<option value="">API Key</option>` — clears subscription
- One `<option>` per subscription from `availableSubscriptions`

### State Variables (`AgentDetail.vue:307-309`)

```javascript
const authStatus = ref(null)              // Current auth badge state
const availableSubscriptions = ref(null)  // null = non-admin (hide dropdown), [] or [...] = admin
const subscriptionChanging = ref(false)   // Spinner while API call in flight
```

### Functions

#### `loadAvailableSubscriptions()` (`AgentDetail.vue:784-794`)

Called once in `onMounted` (line 928) after `loadAuthStatus()`. Calls `GET /api/subscriptions`. On 403, sets `availableSubscriptions.value = null` (hides dropdown for non-admins). On success, sets array (may be empty).

Not called on agent route change (subscriptions are global, the list does not vary per agent). When navigating between agents, only `loadAuthStatus` is re-fetched (route-change watch, `AgentDetail.vue:839`) to refresh the badge for the new agent.

#### `changeSubscription(subscriptionName)` (`AgentDetail.vue:796-817`)

Wired to AgentHeader `@change-subscription` event (`AgentDetail.vue:64`). Logic:
- If `subscriptionName` is non-empty: `PUT /api/subscriptions/agents/{name}?subscription_name={subscriptionName}`
- If empty string (user selected "API Key"): `DELETE /api/subscriptions/agents/{name}`
- Sets `subscriptionChanging = true` before call, `false` in `finally`
- Calls `loadAuthStatus()` on success to refresh the badge
- Shows error notification on failure

### Props Passed to AgentHeader (`AgentDetail.vue:27-67`)

| Prop | Source in AgentDetail | Purpose |
|------|-----------------------|---------|
| `:auth-status="authStatus"` | `authStatus` ref (307) | Current badge state from `GET /api/subscriptions/agents/{name}/auth` |
| `:subscriptions="availableSubscriptions"` | `availableSubscriptions` ref (308) | Full subscription list; `null` hides dropdown for non-admins |
| `:subscription-changing="subscriptionChanging"` | `subscriptionChanging` ref (309) | Shows spinner on badge during API call |

New emit on `AgentHeader`: `change-subscription` — payload is the selected `<option value>` string (subscription name or empty string for API Key).

### Data Flow

```
User selects option in badge dropdown
          |
          | @change event on invisible <select>
          v
AgentHeader emits 'change-subscription' with value (name or "")
          |
          v
AgentDetail.changeSubscription(subscriptionName)
          |
    subscriptionChanging = true
          |
    if name:  PUT /api/subscriptions/agents/{name}?subscription_name={name}
    if "":    DELETE /api/subscriptions/agents/{name}
          |
    await loadAuthStatus()   <-- refreshes authStatus badge
          |
    subscriptionChanging = false
```

---

## Flow 1: Register Subscription

### Overview

Admin registers a long-lived Claude subscription token generated via `claude setup-token`. Token is validated for `sk-ant-oat01-` prefix, encrypted using AES-256-GCM, and stored in the database.

### Sequence Diagram

```
Admin             MCP/Claude Code          Backend                     Database
  |                    |                      |                            |
  | claude setup-token |                      |                            |
  | -> sk-ant-oat01-...|                      |                            |
  |                    |                      |                            |
  | register_          |                      |                            |
  | subscription       |                      |                            |
  |------------------->|                      |                            |
  |                    | POST /api/           |                            |
  |                    | subscriptions        |                            |
  |                    | {name, token, type}  |                            |
  |                    |--------------------->|                            |
  |                    |                      | Pydantic: sk-ant-oat01-    |
  |                    |                      | Encrypt {"token": token}   |
  |                    |                      | Upsert by name             |
  |                    |                      |--------------------------->|
  |                    |                      |<---------------------------|
  |                    |<---------------------|                            |
  |<-------------------|                      |                            |
  | "Subscription      |                      |                            |
  |  registered"       |                      |                            |
```

### Pydantic Validation (`src/backend/db_models.py:623-635`)

```python
class SubscriptionCredentialCreate(BaseModel):
    """Request model for registering a subscription token from `claude setup-token`."""
    name: str
    token: str  # Long-lived token from `claude setup-token` (sk-ant-oat01-...)
    subscription_type: Optional[str] = None
    rate_limit_tier: Optional[str] = None

    @field_validator('token')
    @classmethod
    def validate_token_prefix(cls, v: str) -> str:
        if not v.startswith('sk-ant-oat01-'):
            raise ValueError("Token must start with 'sk-ant-oat01-' (from `claude setup-token`)")
        return v
```

### MCP Tool (`src/mcp-server/src/tools/subscriptions.ts:42-80`)

```typescript
registerSubscription: {
  name: "register_subscription",
  description: "Register a Claude Max/Pro subscription token...",
  parameters: z.object({
    name: z.string().describe("Unique name for the subscription (e.g., 'eugene-max')"),
    token: z.string().describe("Long-lived token from `claude setup-token` (sk-ant-oat01-...)"),
    subscription_type: z.string().optional().describe("Type: 'max' or 'pro'"),
    rate_limit_tier: z.string().optional().describe("Rate limit tier if known"),
  }),
  execute: async (params, context) => {
    const apiClient = getClient(context?.session);
    const result = await apiClient.registerSubscription(
      params.name,
      params.token,
      params.subscription_type,
      params.rate_limit_tier
    );
    return JSON.stringify({ success: true, subscription: result }, null, 2);
  },
}
```

### MCP Client (`src/mcp-server/src/client.ts:1079-1101`)

```typescript
async registerSubscription(
  name: string,
  token: string,
  subscriptionType?: string,
  rateLimitTier?: string
): Promise<{id: string; name: string; ...}> {
  return this.request(
    "POST",
    "/api/subscriptions",
    {
      name,
      token,
      subscription_type: subscriptionType,
      rate_limit_tier: rateLimitTier,
    }
  );
}
```

### Encryption Status Check (`src/backend/routers/subscriptions.py:41-48`)

```python
@router.get("/encryption-status")
async def get_encryption_status(current_user: User = Depends(get_current_user)):
    """Check if credential encryption is configured for subscriptions."""
    require_admin(current_user)
    key = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
    return {"configured": bool(key and len(key) >= 64)}
```

Frontend calls this on Settings page load. If `configured: false`, a yellow warning banner is shown and the Register button is disabled.

### Backend Endpoint (`src/backend/routers/subscriptions.py:51-99`)

```python
@router.post("", response_model=SubscriptionCredential)
async def register_subscription(
    request: SubscriptionCredentialCreate,
    current_user: User = Depends(get_current_user)
):
    """Register a new subscription token. Admin-only.
    Token must start with `sk-ant-oat01-` (Claude Code OAuth access token)."""
    require_admin(current_user)

    # Early validation — returns 503 with actionable instructions if key missing
    encryption_key = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
    if not encryption_key:
        raise HTTPException(status_code=503, detail="...")

    user = db.get_user_by_username(current_user.username)
    subscription = db.create_subscription(...)
    return subscription
```

### Database Operations (`src/backend/db/subscriptions.py:66-137`)

```python
def create_subscription(
    self,
    name: str,
    token: str,
    owner_id: int,
    subscription_type: Optional[str] = None,
    rate_limit_tier: Optional[str] = None,
) -> SubscriptionCredential:
    """Create or update (upsert by name) a subscription credential."""

    # Encrypt the token using AES-256-GCM
    encryption_service = self._get_encryption_service()
    encrypted = encryption_service.encrypt({"token": token})

    # Check for existing subscription with same name
    cursor.execute("SELECT id FROM subscription_credentials WHERE name = ?", (name,))
    existing = cursor.fetchone()

    if existing:
        # Update existing
        cursor.execute("""
            UPDATE subscription_credentials
            SET encrypted_credentials = ?, subscription_type = ?, ...
            WHERE id = ?
        """, (encrypted, subscription_type, ..., existing["id"]))
    else:
        # Insert new
        subscription_id = str(uuid.uuid4())
        cursor.execute("""
            INSERT INTO subscription_credentials
            (id, name, encrypted_credentials, ...) VALUES (?, ?, ?, ...)
        """, (subscription_id, name, encrypted, ...))
```

### Token Retrieval with Legacy Detection (`src/backend/db/subscriptions.py:189-233`)

```python
def get_subscription_token(self, subscription_id: str) -> Optional[str]:
    """Get the decrypted token for a subscription. INTERNAL USE ONLY."""

    # Decrypt credentials
    decrypted = encryption_service.decrypt(row["encrypted_credentials"])

    # SUB-002 format: {"token": "sk-ant-oat01-..."}
    token = decrypted.get("token")
    if token:
        return token

    # Legacy SUB-001 format: {".credentials.json": "..."} -- return None with warning
    if ".credentials.json" in decrypted:
        _logger.warning(
            f"Subscription '{row['name']}' uses legacy .credentials.json format. "
            f"Re-register with `claude setup-token`."
        )
        return None

    _logger.warning(f"Subscription '{row['name']}' has unknown credential format")
    return None
```

### Encryption (`src/backend/services/credential_encryption.py`)

Tokens are encrypted using the same AES-256-GCM encryption service as other credentials:

- Key derived from `CREDENTIAL_ENCRYPTION_KEY` env var or JWT secret
- 12-byte random nonce per encryption
- Ciphertext + nonce stored as JSON

---

## Flow 2: Assign Subscription to Agent

### Overview

Admin assigns a registered subscription to an agent. If the agent is running, it is **restarted** (stop then start) so the container is recreated with `CLAUDE_CODE_OAUTH_TOKEN` env var set and `ANTHROPIC_API_KEY` removed.

### Sequence Diagram

```
Admin            MCP/Claude Code        Backend                         Agent Container
  |                   |                    |                                |
  | assign_           |                    |                                |
  | subscription      |                    |                                |
  |------------------>|                    |                                |
  |                   | PUT /api/sub/      |                                |
  |                   | agents/{name}      |                                |
  |                   | ?sub_name=xxx      |                                |
  |                   |------------------>|                                |
  |                   |                    | Check access (owner)           |
  |                   |                    | Get subscription ID            |
  |                   |                    | Update agent_ownership         |
  |                   |                    |                                |
  |                   |                    | If agent running:              |
  |                   |                    |   container_stop()             |
  |                   |                    |------------------------------->| STOP
  |                   |                    |                                |
  |                   |                    |   start_agent_internal()       |
  |                   |                    |   check_api_key_env_matches()  |
  |                   |                    |     -> sub assigned, token     |
  |                   |                    |        missing -> MISMATCH     |
  |                   |                    |   recreate_container()         |
  |                   |                    |     -> set CLAUDE_CODE_OAUTH_TOKEN
  |                   |                    |     -> remove ANTHROPIC_API_KEY|
  |                   |                    |------------------------------->| NEW CONTAINER
  |                   |                    |   container_start()            |
  |                   |                    |------------------------------->|
  |                   |<-------------------|                                |
  |<------------------|                    |                                |
  | "Assigned +       |                    |                                |
  |  restarted"       |                    |                                |
```

Note: No file injection step exists in SUB-002. The token is part of the container environment from creation.

### Backend Endpoint (`src/backend/routers/subscriptions.py:190-253`)

```python
@router.put("/agents/{agent_name}")
async def assign_subscription_to_agent(
    agent_name: str,
    subscription_name: str = Query(..., description="Name of subscription to assign"),
    current_user: User = Depends(get_current_user)
):
    """Assign a subscription to an agent. Owner access required.
    If running, agent is restarted with CLAUDE_CODE_OAUTH_TOKEN env var."""

    # Owner or admin only — shared users must not mutate subscription assignments (#182)
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the agent owner or an admin can manage subscriptions")

    subscription = db.get_subscription_by_name(subscription_name)
    if not subscription:
        raise HTTPException(status_code=404, detail=f"Subscription '{subscription_name}' not found")

    db.assign_subscription_to_agent(agent_name, subscription.id)

    # Restart running agent so container is recreated with token env var
    container = get_agent_container(agent_name)
    restart_result = None
    if container and agent_status.status == "running":
        try:
            await container_stop(container)
            await start_agent_internal(agent_name)
            restart_result = "success"
        except Exception as e:
            restart_result = f"failed: {e}"

    return {
        "success": True,
        "message": f"Subscription '{subscription_name}' assigned to agent '{agent_name}'",
        "agent_name": agent_name,
        "subscription_name": subscription_name,
        "restart_result": restart_result,
    }
```

### Clear Subscription Endpoint (`src/backend/routers/subscriptions.py:255-304`)

When a subscription is cleared from a running agent, the agent is restarted so `ANTHROPIC_API_KEY` is restored and `CLAUDE_CODE_OAUTH_TOKEN` is removed.

```python
@router.delete("/agents/{agent_name}")
async def clear_agent_subscription(agent_name: str, ...):
    """Clear subscription assignment from an agent."""
    db.clear_agent_subscription(agent_name)

    # Restart running agent so ANTHROPIC_API_KEY is restored
    container = get_agent_container(agent_name)
    if container and agent_status.status == "running":
        await container_stop(container)
        await start_agent_internal(agent_name)  # Recreates container with API key
```

Response includes `restart_result` field ("success" or "failed: {error}").

---

## Flow 3: Agent Start with Subscription Token

### Overview

When an agent starts, the lifecycle service checks whether the container needs recreation (including subscription-aware env var logic). If a subscription is assigned, `CLAUDE_CODE_OAUTH_TOKEN` is set and `ANTHROPIC_API_KEY` is removed from the container environment. No post-start injection is needed.

### Subscription-Aware Container Recreation

Before starting, `check_api_key_env_matches()` (`src/backend/services/agent_service/helpers.py:367-406`) detects conflicts:

```python
def check_api_key_env_matches(container, agent_name: str) -> bool:
    """
    Check if container's auth env vars match the current setting.
    SUB-002: When a subscription is assigned, CLAUDE_CODE_OAUTH_TOKEN must be
    present with the correct value, and ANTHROPIC_API_KEY must be absent.
    """
    env_dict = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in env_list if "=" in e}

    has_api_key = "ANTHROPIC_API_KEY" in env_dict and env_dict["ANTHROPIC_API_KEY"]
    has_oauth_token = "CLAUDE_CODE_OAUTH_TOKEN" in env_dict and env_dict["CLAUDE_CODE_OAUTH_TOKEN"]

    # Subscription takes priority -- must have token and NOT have API key
    subscription_id = db.get_agent_subscription_id(agent_name)
    if subscription_id is not None:
        if has_api_key:
            return False  # API key must be absent
        if not has_oauth_token:
            return False  # Token must be present
        # Verify token value matches DB
        expected_token = db.get_subscription_token(subscription_id)
        if expected_token and env_dict.get("CLAUDE_CODE_OAUTH_TOKEN") != expected_token:
            return False  # Token value changed
        return True

    use_platform_key = db.get_use_platform_api_key(agent_name)
    if use_platform_key:
        if has_oauth_token:
            return False  # OAuth token must be absent for API key mode
        expected_key = get_anthropic_api_key()
        return env_dict.get("ANTHROPIC_API_KEY", "") == expected_key
    else:
        return not has_api_key and not has_oauth_token
```

If mismatch detected, `recreate_container_with_updated_config()` (`src/backend/services/agent_service/lifecycle.py:243-403`) handles the env vars:

```python
# Update auth env vars based on current setting (SUB-002).
subscription_id = db.get_agent_subscription_id(agent_name)
has_subscription = subscription_id is not None

if has_subscription:
    # Subscription assigned -- inject token, remove API key
    token = db.get_subscription_token(subscription_id)
    if token:
        env_vars['CLAUDE_CODE_OAUTH_TOKEN'] = token
    env_vars.pop('ANTHROPIC_API_KEY', None)
elif use_platform_key:
    # No subscription, use platform API key
    env_vars['ANTHROPIC_API_KEY'] = get_anthropic_api_key()
    env_vars.pop('CLAUDE_CODE_OAUTH_TOKEN', None)
else:
    # No subscription, no platform key
    env_vars.pop('ANTHROPIC_API_KEY', None)
    env_vars.pop('CLAUDE_CODE_OAUTH_TOKEN', None)
```

### Sequence Diagram

```
API Request        lifecycle.py                 helpers.py                Agent
     |                  |                           |                      |
     | start_agent()    |                           |                      |
     |----------------->|                           |                      |
     |                  | check_api_key_env_matches |                      |
     |                  |-------------------------->|                      |
     |                  |   sub assigned + no token |                      |
     |                  |   in env = MISMATCH       |                      |
     |                  |<--------------------------|                      |
     |                  |                           |                      |
     |                  | recreate_container()      |                      |
     |                  | (set CLAUDE_CODE_OAUTH_TOKEN,                    |
     |                  |  remove ANTHROPIC_API_KEY) |                      |
     |                  |--------------------------------------------->| NEW
     |                  |                           |                      |
     |                  | container.start()         |                      |
     |                  |--------------------------------------------->|
     |                  |                           |                      |
     |                  | inject_trinity_meta_prompt |                      |
     |                  | inject_assigned_credentials|                      |
     |                  | inject_assigned_skills     |                      |
     |                  | (NO subscription injection)|                      |
     |<-----------------|                           |                      |
```

### Lifecycle Integration (`src/backend/services/agent_service/lifecycle.py:168-241`)

```python
async def start_agent_internal(agent_name: str) -> dict:
    container = get_agent_container(agent_name)

    # Check if container needs recreation (includes subscription-aware env check)
    needs_recreation = (
        not shared_folder_match or
        not check_api_key_env_matches(container, agent_name) or
        not check_resource_limits_match(container, agent_name) or
        not check_full_capabilities_match(container, agent_name)
    )
    if needs_recreation:
        await recreate_container_with_updated_config(agent_name, container, "system")
        container = get_agent_container(agent_name)

    await container_start(container)

    # Post-start injections (NO subscription injection in SUB-002)
    # NOTE: Trinity platform instructions now injected at runtime via
    # --append-system-prompt on every chat/task request (Issue #136).
    credentials_result = await inject_assigned_credentials(agent_name)
    skills_result = await inject_assigned_skills(agent_name)

    return {
        "message": f"Agent {agent_name} started",
        "credentials_injection": credentials_result.get("status", "unknown"),
        "skills_injection": skills_result.get("status", "unknown"),
        # No subscription_injection field -- handled at container level
    }
```

---

## Flow 4: Auth Status Detection

### Overview

Determines how an agent is authenticated (subscription, API key, or not configured) by checking **database state only** (no HTTP calls to the agent). The auth status is displayed as a badge in the AgentHeader.

### Subscription Service (`src/backend/services/subscription_service.py:20-57`)

```python
async def get_agent_auth_mode(agent_name: str) -> AgentAuthStatus:
    """
    Detect the authentication mode for an agent.
    Determines auth purely from DB state (no HTTP calls to agent).
    """
    subscription = db.get_agent_subscription(agent_name)
    has_subscription = subscription is not None

    has_api_key = db.get_use_platform_api_key(agent_name) or False

    if has_subscription:
        auth_mode = "subscription"
    elif has_api_key:
        auth_mode = "api_key"
    else:
        auth_mode = "not_configured"

    return AgentAuthStatus(
        agent_name=agent_name,
        auth_mode=auth_mode,
        subscription_name=subscription.name if subscription else None,
        subscription_id=subscription.id if subscription else None,
        has_api_key=has_api_key,
    )
```

### Backend Endpoint (`src/backend/routers/subscriptions.py:307-328`)

```python
@router.get("/agents/{agent_name}/auth", response_model=AgentAuthStatus)
async def get_agent_auth_status(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Access denied to this agent")

    from services.subscription_service import get_agent_auth_mode
    return await get_agent_auth_mode(agent_name)
```

### Frontend: Auth Badge in AgentHeader

The Agent Detail page loads the auth status on mount and displays it as a colored badge in Row 1 of the AgentHeader.

#### AgentDetail.vue - State & Loader (`src/frontend/src/views/AgentDetail.vue`)

```javascript
const authStatus = ref(null)

async function loadAuthStatus() {
  if (!agent.value?.name) return
  try {
    const response = await axios.get(`/api/subscriptions/agents/${agent.value.name}/auth`, {
      headers: authStore.authHeader
    })
    authStatus.value = response.data
  } catch (err) {
    authStatus.value = null  // Non-critical
  }
}
```

#### AgentHeader.vue - Badge Display (`src/frontend/src/components/AgentHeader.vue`)

| Auth Mode | Badge Color | Badge Text | Tooltip |
|-----------|-------------|------------|---------|
| `subscription` | Amber (`bg-amber-100`) | Subscription name (e.g., "eugene-max") | "Using subscription: {name}" |
| `api_key` | Gray (`bg-gray-100`) | "API Key" | "Using platform API key" |
| `not_configured` | Red (`bg-red-100`) | "No Auth" | "No auth configured" |

---

## Flow 5: Fleet Auth Report

### Overview

Admin views fleet-wide authentication status report showing how many agents use each authentication method.

### Backend Endpoint (`src/backend/routers/ops.py:934`)

```python
@router.get("/auth-report")
async def get_auth_report(request: Request, current_user: User = Depends(get_current_user)):
    """Get authentication status report for all agents."""
    agents = list_all_agents_fast()

    for agent in agents:
        subscription = db.get_agent_subscription(agent.name)
        has_api_key = db.get_use_platform_api_key(agent.name) or False

        if subscription:
            subscription_agents.append(agent_info)
        elif has_api_key:
            api_key_agents.append(agent_info)
        else:
            not_configured_agents.append(agent_info)

    return {
        "summary": {
            "using_subscription": len(subscription_agents),
            "using_api_key": len(api_key_agents),
            "not_configured": len(not_configured_agents),
        },
        "by_auth_mode": {...},
        "subscriptions": [...],
    }
```

---

## Flow 6: Subscription Usage Query (SUB-004)

### Overview

Admin fetches rolling token and cost aggregates for a single subscription. Two pre-defined windows are returned: the last 5 hours (rate-limit window) and the last 7 days (billing window). The data is sourced from `subscription_id` fields that are snapshotted onto `chat_messages` and `schedule_executions` at record time.

### Backend Endpoint (`src/backend/routers/subscriptions.py:118-146`)

```python
@router.get("/{subscription_id}/usage", response_model=SubscriptionUsage)
async def get_subscription_usage(
    subscription_id: str,
    current_user: User = Depends(get_current_user)
):
    """Admin-only. Returns token and cost aggregates across two rolling windows."""
    require_admin(current_user)

    # Resolve by ID or name
    subscription = db.get_subscription(subscription_id)
    if not subscription:
        subscription = db.get_subscription_by_name(subscription_id)

    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")

    return db.get_subscription_usage(subscription.id)
```

Note: `subscription_id` in the path accepts either the UUID or the human-readable name (name lookup is the fallback).

### Database Method (`src/backend/db/subscriptions.py:606-677`)

`get_subscription_usage(subscription_id)` executes two window queries for each of `chat_messages` and `schedule_executions`, then merges the results:

```
window_5h  cutoff = now - 5h
window_7d  cutoff = now - 168h

chat_messages query (per window):
  WHERE subscription_id = ? AND role = 'assistant' AND timestamp >= ?
  → SUM(context_used) as input_tokens, SUM(output_tokens), SUM(cost), COUNT(*)

schedule_executions query (per window):
  WHERE subscription_id = ? AND started_at >= ? AND status NOT IN ('running', 'pending')
  → SUM(context_used) as input_tokens, SUM(cost), COUNT(*)

Merged window = chat totals + execution totals

agents = SELECT agent_name FROM agent_ownership WHERE subscription_id = ?
```

`context_used` on `chat_messages` stores input tokens; `output_tokens` is a separate column added in SUB-004. Schedule executions do not track output tokens separately.

### subscription_id Snapshotting

`subscription_id` is recorded at write time on both tables so historical queries remain stable even after reassignment:

- **`chat_messages.subscription_id`** (`src/backend/db_models.py:243`) — snapshotted when the assistant message is stored after a chat turn
- **`schedule_executions.subscription_id`** (`src/backend/db_models.py:172`) — snapshotted when the execution record is created
- **`chat_sessions.subscription_id`** (`src/backend/db_models.py:223`) — snapshotted at session creation (informational; not used by the usage query)

### Response Shape

```json
{
  "subscription_id": "uuid",
  "window_5h": {
    "input_tokens": 12000,
    "output_tokens": 3400,
    "cost_usd": 0.18,
    "message_count": 7
  },
  "window_7d": {
    "input_tokens": 450000,
    "output_tokens": 98000,
    "cost_usd": 5.62,
    "message_count": 210
  },
  "agents": ["agent-ruby", "agent-sage"]
}
```

`message_count` includes both chat assistant messages and completed schedule executions.

---

## Agent-Side Diagnostics

### Claude Code Execution Diagnostics (`docker/base-image/agent_server/services/claude_code.py:685-710`)

When Claude Code exits with an error and stderr is empty, the agent server checks for the `CLAUDE_CODE_OAUTH_TOKEN` env var to provide diagnostic hints:

```python
def _diagnose_exit_failure(return_code: int) -> str:
    """Diagnose common Claude Code exit failures when stderr is empty."""
    has_api_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_oauth_token = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))

    if not has_api_key and not has_oauth_token:
        return "No authentication configured. Set ANTHROPIC_API_KEY or assign a subscription token."
    if not has_api_key and has_oauth_token:
        return "Subscription token may be expired or revoked. Generate a new one with 'claude setup-token'."

    # Exit code hints for other failure modes...
```

---

## Database Schema

### Table: subscription_credentials (`src/backend/db/migrations.py:359-379`)

```sql
CREATE TABLE IF NOT EXISTS subscription_credentials (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    encrypted_credentials TEXT NOT NULL,  -- AES-256-GCM encrypted JSON: {"token": "sk-ant-oat01-..."}
    subscription_type TEXT,                -- "max", "pro", etc.
    rate_limit_tier TEXT,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_name ON subscription_credentials(name);
CREATE INDEX IF NOT EXISTS idx_subscriptions_owner ON subscription_credentials(owner_id);
```

### Column: agent_ownership.subscription_id (`src/backend/db/migrations.py:393`)

```sql
ALTER TABLE agent_ownership ADD COLUMN subscription_id TEXT REFERENCES subscription_credentials(id);
```

---

## Pydantic Models (`src/backend/db_models.py:623-663`)

```python
class SubscriptionCredentialCreate(BaseModel):
    """Request model for registering a subscription token."""
    name: str                              # Unique name (e.g., "eugene-max")
    token: str                             # Long-lived token from `claude setup-token`
    subscription_type: Optional[str] = None  # "max", "pro"
    rate_limit_tier: Optional[str] = None

    @field_validator('token')
    @classmethod
    def validate_token_prefix(cls, v: str) -> str:
        if not v.startswith('sk-ant-oat01-'):
            raise ValueError("Token must start with 'sk-ant-oat01-' (from `claude setup-token`)")
        return v

class SubscriptionCredential(BaseModel):
    """A registered Claude subscription credential."""
    id: str
    name: str
    subscription_type: Optional[str] = None
    rate_limit_tier: Optional[str] = None
    owner_id: int
    owner_email: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    agent_count: int = 0

class SubscriptionWithAgents(SubscriptionCredential):
    """Subscription with list of assigned agents."""
    agents: List[str] = []

class AgentAuthStatus(BaseModel):
    """Auth status for an agent."""
    agent_name: str
    auth_mode: str  # "subscription", "api_key", "not_configured"
    subscription_name: Optional[str] = None
    subscription_id: Optional[str] = None
    has_api_key: bool = False

class SubscriptionUsageWindow(BaseModel):
    """Usage totals for a rolling time window. (SUB-004)"""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    message_count: int = 0

class SubscriptionUsage(BaseModel):
    """Per-subscription usage across rolling time windows. (SUB-004)"""
    subscription_id: str
    window_5h: SubscriptionUsageWindow
    window_7d: SubscriptionUsageWindow
    agents: List[str] = []  # agents currently assigned to this subscription
```

---

## TypeScript Types (`src/mcp-server/src/types.ts`)

```typescript
export interface Subscription {
  id: string;
  name: string;
  subscription_type?: string;
  rate_limit_tier?: string;
  owner_id: number;
  owner_email?: string;
  created_at: string;
  updated_at: string;
  agent_count: number;
}

export interface SubscriptionWithAgents extends Subscription {
  agents: string[];
}

export interface AgentAuthStatus {
  agent_name: string;
  auth_mode: "subscription" | "api_key" | "not_configured";
  subscription_name?: string;
  subscription_id?: string;
  has_api_key: boolean;
}
```

---

## MCP Tools Summary

All 6 subscription tools defined in `src/mcp-server/src/tools/subscriptions.ts`:

| Tool | Description | Parameters | Access |
|------|-------------|------------|--------|
| `register_subscription` | Register long-lived token from `claude setup-token` | `name`, `token`, `subscription_type?`, `rate_limit_tier?` | Admin |
| `list_subscriptions` | List all subscriptions with assigned agents | (none) | Admin |
| `assign_subscription` | Assign subscription to agent (restarts if running) | `agent_name`, `subscription_name` | Owner |
| `clear_agent_subscription` | Clear subscription from agent (restarts if running) | `agent_name` | Owner |
| `get_agent_auth` | Get auth status for an agent | `agent_name` | Owner |
| `delete_subscription` | Delete subscription (cascades to clear agent assignments) | `subscription_name` | Admin |

MCP client methods: `src/mcp-server/src/client.ts:1079-1196`

---

## Security Considerations

1. **Encryption at Rest**: Tokens encrypted with AES-256-GCM before database storage
2. **Admin-Only Registration**: Only admins can register new subscriptions
3. **Owner/Admin Access for Assignment**: Only agent owners and admins can assign or clear subscriptions (`can_user_share_agent`); shared users with read access cannot mutate assignments (#182)
4. **No Token Exposure**: API never returns decrypted tokens -- only metadata
5. **Password Input**: Frontend uses `type="password"` for token field
6. **Prefix Validation**: Both frontend and backend validate `sk-ant-oat01-` prefix
7. **Audit Logging**: Subscription operations logged via structured logging
8. **Cascade Delete**: Deleting subscription clears all agent assignments
9. **Legacy Detection**: `get_subscription_token()` logs warnings for SUB-001 format credentials

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Not admin (register/delete) | 403 | "Admin access required" |
| Not owner (assign) | 403 | "Access denied to this agent" |
| Invalid token prefix | 400 | "Token must start with 'sk-ant-oat01-' (from `claude setup-token`)" |
| Subscription not found | 404 | "Subscription '{name}' not found" |
| Agent not found | 400 | "Agent {name} not found in ownership table" |
| User not found | 404 | "User not found" |
| Subscription not found (usage) | 404 | "Subscription not found" |
| Usage query failure | 500 | "Failed to retrieve usage data" |
| Subscription usage exhausted | 429 | "Subscription usage limit: [reset message]. To resolve: (1) wait for reset, (2) set ANTHROPIC_API_KEY, or (3) assign different subscription" |
| Failed registration | 500 | "Failed to register subscription: {error}" |

### Rate Limit Detection

When Claude Code hits subscription usage limits, the error is detected from the stream-json output:
- `assistant` messages with `"error": "rate_limit"` field
- `result` messages with `"is_error": true` and text matching usage limit patterns

The error flows through: Claude Code → `process_stream_line()` (stores in `metadata.error_type`) → `execute_claude_code()`/`execute_headless_task()` (raises HTTP 429) → backend gateway (preserves 429) → frontend (amber warning styling with "Subscription Usage Limit" header).

---

## Testing

### Prerequisites
- Trinity platform running
- Admin user logged in
- At least one agent created
- Long-lived token from `claude setup-token` (starts with `sk-ant-oat01-`)

---

### UI Testing (Settings Page)

#### Test: Register via Token Input
1. Navigate to Settings page (`/settings`)
2. Scroll to "Claude Subscriptions" section
3. In "Add Subscription" form:
   - Enter name: "test-subscription"
   - Select type: "Max"
   - Paste token starting with `sk-ant-oat01-` into password field
4. Verify: No red border on token input (valid prefix)
5. Click "Register Subscription"
6. Verify: Loading spinner on button
7. Verify: New subscription appears in table with correct name, type, "0 agents"
8. Verify: Form is cleared after success

#### Test: Invalid Token Prefix
1. Enter a token NOT starting with `sk-ant-oat01-`
2. Verify: Red border on token input
3. Verify: Error text "Token must start with sk-ant-oat01-"
4. Verify: Register button is disabled

#### Test: View Subscriptions List
1. Navigate to Settings page
2. Verify: Table shows loading spinner briefly, then list (or empty state)
3. Verify: Non-admin users see empty section (403 handled gracefully)

#### Test: Expand Subscription Row
1. Click on a subscription row in the table
2. Verify: Chevron rotates 90 degrees
3. Verify: Details row expands showing owner, rate limit tier, assigned agents
4. Click row again
5. Verify: Row collapses

#### Test: Delete Subscription
1. Click "Delete" button on a subscription row
2. Verify: Confirmation dialog shows subscription name and agent count
3. Click OK
4. Verify: Button shows "Deleting..."
5. Verify: Subscription removed from list after delete completes

#### Test: Assign Agent via Expanded Row (SUB-003)
1. Expand a subscription row
2. Verify: Dropdown shows available agents (fetched from `GET /api/agents`)
3. Select an agent, click "Assign"
4. Verify: Spinner on Assign button during request
5. Verify: Agent badge appears, agent count increments
6. Verify: Agent removed from dropdown options

#### Test: Unassign Agent via X Button (SUB-003)
1. Expand a subscription row with at least one assigned agent
2. Click X button on an agent badge
3. Verify: Confirmation dialog: 'Remove "agent-name"...'
4. Click OK
5. Verify: Spinner on the specific badge being removed
6. Verify: Badge removed, agent count decrements

#### Test: Agent on Other Subscription Shown in Dropdown (SUB-003)
1. Register two subscriptions (sub-A and sub-B)
2. Assign an agent to sub-A
3. Expand sub-B row
4. Verify: Dropdown shows the agent with "(on sub-A)" suffix
5. Select it and click Assign
6. Verify: Agent moves from sub-A to sub-B (backend handles reassignment)

#### Test: Empty Agent List (SUB-003)
1. Assign all agents to the current subscription
2. Verify: Dropdown shows no options, Assign button is disabled

---

### UI Testing (Agent Detail -- Auth Badge)

#### Test: Auth Badge Shows Subscription Name
1. Assign a subscription to an agent via MCP: `assign_subscription(agent_name: "my-agent", subscription_name: "eugene-max")`
2. Navigate to Agent Detail page for "my-agent"
3. Verify: Amber badge in header row shows "eugene-max"
4. Verify: Hovering badge shows tooltip "Using subscription: eugene-max"

#### Test: Auth Badge Shows API Key
1. Ensure agent has platform API key enabled (default) and no subscription assigned
2. Navigate to Agent Detail page
3. Verify: Gray badge in header row shows "API Key"

#### Test: Auth Badge Shows No Auth
1. Clear subscription and disable platform API key for an agent
2. Navigate to Agent Detail page
3. Verify: Red badge in header row shows "No Auth"

#### Test: Subscription Switcher Dropdown (Admin Only)
1. Register at least one subscription in Settings
2. Navigate to Agent Detail page as admin (agent you own)
3. Verify: Auth badge shows a chevron-down icon (dropdown is available)
4. Click the badge to open the native `<select>` dropdown
5. Verify: Dropdown shows "API Key" option plus one entry per registered subscription
6. Select a different subscription name
7. Verify: Spinner appears on badge (`subscriptionChanging = true`)
8. Verify: After API call completes, badge updates to amber with the new subscription name
9. Select "API Key" option from the dropdown
10. Verify: `DELETE /api/subscriptions/agents/{name}` is called
11. Verify: Badge reverts to gray "API Key"

#### Test: Non-Admin Cannot See Dropdown
1. Log in as a non-admin user who owns an agent
2. Navigate to Agent Detail page for that agent
3. Verify: Auth badge does NOT show chevron-down icon (no dropdown rendered)
4. Verify: `GET /api/subscriptions` returns 403 and `availableSubscriptions` stays `null`

---

### MCP Testing

#### Test: Register Subscription
1. Generate token: `claude setup-token`
2. Via MCP: `register_subscription(name: "test-max", token: "sk-ant-oat01-...", subscription_type: "max")`
3. Verify: `list_subscriptions()` shows new subscription

#### Test: Assign to Agent
1. Start an agent
2. Via MCP: `assign_subscription(agent_name: "my-agent", subscription_name: "test-max")`
3. Verify: Response includes `restart_result: "success"` (if agent was running)
4. Verify: Agent container has `CLAUDE_CODE_OAUTH_TOKEN` env var set
5. Verify: Agent container does NOT have `ANTHROPIC_API_KEY` env var

#### Test: Agent Start with Subscription
1. Assign subscription to stopped agent
2. Start agent via UI or API
3. Verify: Container is created with `CLAUDE_CODE_OAUTH_TOKEN` env var

#### Test: Auth Report
1. Via API: `GET /api/ops/auth-report`
2. Verify: Agent shows under `by_auth_mode.subscription`

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-03-26 | **Line number refresh**: Updated all stale line references across Settings.vue (template +100, state +390, methods +600), subscriptions.py (+20), helpers.py (+54), lifecycle.py (-30 to +90), crud.py (+7), claude_code.py (+116), db_models.py, migrations.py, and MCP client.ts (+133). Updated `start_agent_internal` code snippet to reflect removal of `inject_trinity_meta_prompt` (now runtime via `--append-system-prompt`). |
| 2026-03-18 | **Subscription switcher dropdown on Agent Detail** (commit d166976): Updated doc to reflect current `AgentHeader.vue` and `AgentDetail.vue` wiring — corrected line numbers, clarified that `loadAvailableSubscriptions` is called only once in `onMounted` (not on route change), documented the three props passed to `AgentHeader` (`:auth-status`, `:subscriptions`, `:subscription-changing`), and added switcher test cases for admin and non-admin paths. |
| 2026-03-03 | **SUB-003 Agent assignment UI**: Added assign/unassign controls to expanded subscription rows in Settings. Agent badges have X buttons for removal, dropdown + Assign button for adding agents, agents on other subs shown with "(on sub-name)" suffix. No backend changes. |
| 2026-03-03 | **SUB-002 complete rewrite**: Token-based auth replacing file-injection. Registration takes `token` (from `claude setup-token`, prefix `sk-ant-oat01-`) instead of `credentials_json`. Storage encrypts `{"token": ...}` instead of `{".credentials.json": ...}`. Injection via `CLAUDE_CODE_OAUTH_TOKEN` env var on container creation -- no file injection needed. Removed `inject_subscription_to_agent()`, `inject_subscription_on_start()`, HTTP credential verification, and monitoring/auto-remediation. `get_agent_auth_mode()` simplified to pure DB-state detection. Frontend changed from file upload to password input with prefix validation. |
| 2026-03-02 | **Auth method badge in AgentHeader**: Added frontend documentation for Flow 4. |
| 2026-03-02 | **Credential priority fix (Issue #57)**: Corrected Authentication Hierarchy. |
| 2026-02-23 | Added Frontend UI section for Settings page integration |
| 2026-02-22 | Initial documentation for SUB-001 implementation |

---

## Related Flows

- **Credential Injection** (`credential-injection.md`) -- General credential system (CRED-002)
- **Agent Lifecycle** (`agent-lifecycle.md`) -- Container recreation with subscription env vars
- **MCP Orchestration** (`mcp-orchestration.md`) -- MCP tools including subscription management
- **Platform Settings** (`platform-settings.md`) -- Platform API key configuration
