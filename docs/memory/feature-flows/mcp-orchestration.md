# Feature: MCP Orchestration

## Overview
External integration layer allowing Claude Code instances to manage Trinity agents via the Model Context Protocol (MCP). Exposes 12 tools for agent lifecycle, chat, and credential management through a FastMCP server with Streamable HTTP transport.

**Important**: Agent chat via MCP (`chat_with_agent` tool) goes through the [Execution Queue System](execution-queue.md) with graceful 429 handling for busy agents.

## User Story
As a Claude Code user (head agent), I want to use MCP tools to create, manage, and communicate with Trinity sub-agents so that I can orchestrate multiple specialized agents for complex tasks.

## Entry Points
- **External MCP Client**: Claude Code via `.mcp.json` configuration
- **MCP Endpoint**: `http://localhost:8080/mcp` (local) or `http://your-server-ip:8007/mcp` (production)
- **API Key Management UI**: `src/frontend/src/views/ApiKeys.vue`

---

## Architecture Flow

```
┌─────────────────────────────────────────┐
│           Head Agent (Claude)           │
│  Uses MCP tools to orchestrate agents   │
└────────────────────┬────────────────────┘
                     │ MCP Protocol (Streamable HTTP)
                     │ Authorization: Bearer <API_KEY>
                     ▼
┌─────────────────────────────────────────┐
│         Trinity MCP Server              │
│      (FastMCP @ port 8080/8007)         │
│   src/mcp-server/src/server.ts          │
│                                         │
│  ┌────────────────────────────────┐   │
│  │  FastMCP authenticate callback │   │
│  │  validates API key, returns    │   │
│  │  McpAuthContext stored in      │   │
│  │  context.session               │   │
│  └────────────────────────────────┘   │
└────────────────────┬────────────────────┘
                     │ HTTP API (REST)
                     │ Bearer Token (MCP API Key)
                     ▼
┌─────────────────────────────────────────┐
│       Trinity Backend API               │
│       (FastAPI @ port 8000/8005)        │
│                                         │
│  ┌────────────────────────────────┐   │
│  │  get_current_user() dependency │   │
│  │  validates MCP API key and     │   │
│  │  returns User object           │   │
│  └────────────────────────────────┘   │
└────────────────────┬────────────────────┘
                     │ Docker API / HTTP Proxy
                     ▼
┌─────────────────────────────────────────┐
│         Agent Containers                │
│   Running Claude Code instances         │
└─────────────────────────────────────────┘
```

---

## Authentication Flow (Critical)

### 1. MCP Client → MCP Server Authentication (`server.ts:105-141`)

When a request arrives at the MCP server:

```typescript
// FastMCP authenticate callback - validates API key
authenticate: requireApiKey
  ? async (request) => {
      // Extract API key from Authorization header
      const authHeader = request.headers["authorization"] as string | undefined;
      if (!authHeader || !authHeader.startsWith("Bearer ")) {
        throw new Error("Missing or invalid Authorization header");
      }

      const apiKey = authHeader.substring(7);

      // Validate against Trinity backend
      const result = await validateMcpApiKey(trinityApiUrl, apiKey);

      if (result && result.valid) {
        // Create auth context object (stored in FastMCP session)
        const authContext: McpAuthContext = {
          userId: result.user_id || "unknown",
          userEmail: result.user_email,
          keyName: result.key_name || "unknown",
          agentName: result.agent_name,  // For agent-to-agent
          scope: result.scope as "user" | "agent",
          mcpApiKey: apiKey,  // Store for backend API calls
        };
        return authContext;  // FastMCP stores this in session
      }

      throw new Error("Invalid API key");
    }
  : undefined,
```

**Key Points**:
- FastMCP's `authenticate` callback is called **per-request**
- Returns `McpAuthContext` object (not boolean)
- FastMCP stores this in `context.session` for tool execution
- **Request-scoped**: Each request gets its own auth context (no race conditions)

### 2. Tool Execution with Auth Context (`agents.ts:120-166`)

Tools receive auth context via FastMCP's context parameter:

```typescript
execute: async (args: {...}, context: any) => {
  // Get auth context from FastMCP session (set by authenticate callback)
  const authContext = requireApiKey ? context?.session : undefined;

  console.log("[CREATE_AGENT] Auth context:", {
    userId: authContext?.userId,
    userEmail: authContext?.userEmail,
    scope: authContext?.scope,
    hasMcpApiKey: !!authContext?.mcpApiKey,
  });

  // Create new client authenticated with user's MCP API key
  const apiClient = getClient(authContext);

  // Call backend (apiClient has user's MCP key as Bearer token)
  const agent = await apiClient.createAgent(config);
  return JSON.stringify(agent, null, 2);
}
```

**getClient() Helper** (`agents.ts:25-34`):
```typescript
const getClient = (authContext?: McpAuthContext): TrinityClient => {
  if (authContext?.mcpApiKey) {
    // Create new client instance authenticated with user's MCP API key
    const userClient = new TrinityClient(client.getBaseUrl());
    userClient.setToken(authContext.mcpApiKey);
    return userClient;
  }
  // Fall back to admin-authenticated client
  return client;
};
```

### 3. MCP Server → Backend API Call (`client.ts:86-128`)

TrinityClient includes MCP API key in request:

```typescript
private async request<T>(method: string, path: string, body?: unknown, isRetry: boolean = false): Promise<T> {
  if (!this.token) {
    throw new Error("Not authenticated. Call authenticate() first or setToken().");
  }

  const headers: Record<string, string> = {
    Authorization: `Bearer ${this.token}`,  // MCP API key
  };

  console.log(`[CLIENT] ${method} ${path} - Token: ${this.token.substring(0, 20)}...`);

  const response = await fetch(`${this.baseUrl}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  // Handle 401 - attempt re-authentication once
  if (response.status === 401 && !isRetry) {
    console.log("Token expired, attempting re-authentication...");
    const success = await this.reauthenticate();
    if (success) {
      return this.request<T>(method, path, body, true);
    }
  }
}
```

### 4. Backend Validates MCP API Key (`dependencies.py:47-121`)

FastAPI dependency validates incoming Bearer token:

```python
async def get_current_user(request: Request, token: str = Depends(oauth2_scheme)) -> User:
    """
    Validates JWT token OR MCP API key and returns User object.
    """
    print(f"[AUTH DEBUG] Received token: {token[:20]}... (length: {len(token)})")

    # Try JWT token first
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        # ... return User object
    except JWTError as e:
        print(f"[AUTH DEBUG] JWT decode failed: {e}, trying MCP API key...")

    # Try MCP API key authentication
    print(f"[AUTH DEBUG] Validating as MCP API key...")
    mcp_key_info = db.validate_mcp_api_key(token)
    print(f"[AUTH DEBUG] MCP validation result: {mcp_key_info}")

    # CRITICAL FIX: Check if mcp_key_info exists (not if it has "valid" key)
    if mcp_key_info:  # Returns dict if valid, None if invalid
        user_email = mcp_key_info.get("user_email")
        user_id = mcp_key_info.get("user_id")

        # Get full user record
        user = db.get_user_by_email(user_email) if user_email else db.get_user_by_id(user_id)
        if user:
            return User(
                id=user["id"],
                username=user["username"],
                email=user.get("email"),
                role=user["role"]
            )

    # Both JWT and MCP key failed
    raise credentials_exception
```

**Critical Fix**: Line 95 now checks `if mcp_key_info:` instead of `if mcp_key_info.get("valid"):`
- `validate_mcp_api_key()` returns dict if valid, `None` if invalid
- Old code checked for non-existent "valid" key, always evaluated to False
- This caused all MCP-authenticated requests to fail validation

### 5. Agent Creation with Correct Owner (`routers/agents.py:376`)

Agent is registered with correct user:

```python
# Line 376 - After container creation
db.register_agent_owner(config.name, current_user.username)
```

`current_user` comes from `get_current_user()` dependency, which now correctly returns the user who owns the MCP API key.

---

## The Bug That Was Fixed

### Problem: Race Condition with Module-Level State

**Before** (BROKEN):
```typescript
// ❌ Module-level variable - shared across all requests
let currentAuthContext: McpAuthContext | undefined = undefined;

// authenticate callback
if (result && result.valid) {
  currentAuthContext = authContext;  // Race condition!
  return true;
}

// Tool execution
const authContext = currentAuthContext;  // Wrong user!
```

**Issue**: Module-level `currentAuthContext` was shared across all concurrent requests:
- Request A (user1) sets `currentAuthContext = user1`
- Request B (user2) sets `currentAuthContext = user2` (overwrites!)
- Request A reads `currentAuthContext` → gets user2's context
- Agent created by user1 gets assigned to user2 (or last authenticated user)

### Solution: FastMCP Session Mechanism

**After** (FIXED):
```typescript
// ✅ No module-level state

// authenticate callback
if (result && result.valid) {
  return authContext;  // FastMCP stores in request-scoped session
}

// Tool execution
const authContext = context?.session;  // Each request has own context
```

**Key Benefits**:
- Auth context is **request-scoped** (stored in FastMCP's session)
- No shared state between concurrent requests
- Each tool execution gets the correct user's context
- Thread-safe by design

---

## MCP Server Layer

### Server Configuration (`src/mcp-server/src/server.ts:62-72`)
```typescript
export async function createServer(config: ServerConfig = {}) {
  const {
    name = "trinity-orchestrator",
    version = "1.0.0" as const,
    trinityApiUrl = process.env.TRINITY_API_URL || "http://localhost:8000",
    port = parseInt(process.env.MCP_PORT || "8080", 10),
    requireApiKey = process.env.MCP_REQUIRE_API_KEY === "true",
  } = config;
}
```

**Note**: When `MCP_REQUIRE_API_KEY=true` (production), NO admin credentials are needed. All backend API calls use the user's MCP API key directly. This eliminates the complexity and recurring issues with admin password authentication.

### API Key Validation (`src/mcp-server/src/server.ts:37-57`)
```typescript
async function validateMcpApiKey(
  trinityApiUrl: string,
  apiKey: string
): Promise<McpApiKeyValidationResult | null> {
  try {
    const response = await fetch(`${trinityApiUrl}/api/mcp/validate`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiKey}` },
    });

    if (response.ok) {
      return (await response.json()) as McpApiKeyValidationResult;
    }
    return null;
  } catch (error) {
    console.error("Failed to validate MCP API key:", error);
    return null;
  }
}
```

---

## The 12 MCP Tools

### Agent Management (`src/mcp-server/src/tools/agents.ts`)

| Tool | Line | Parameters | Backend Endpoint |
|------|------|------------|------------------|
| `list_agents` | 40-49 | `{}` | `GET /api/agents` |
| `get_agent` | 55-66 | `{name}` | `GET /api/agents/{name}` |
| `create_agent` | 72-167 | `{name, type?, template?, resources?}` | `POST /api/agents` |
| `delete_agent` | 172-184 | `{name}` | `DELETE /api/agents/{name}` |
| `start_agent` | 190-202 | `{name}` | `POST /api/agents/{name}/start` |
| `stop_agent` | 208-220 | `{name}` | `POST /api/agents/{name}/stop` |
| `list_templates` | 226-236 | `{}` | `GET /api/templates` |
| `reload_credentials` | 242-255 | `{name}` | `POST /api/agents/{name}/credentials/reload` |
| `get_credential_status` | 261-273 | `{name}` | `GET /api/agents/{name}/credentials/status` |

### Chat Tools (`src/mcp-server/src/tools/chat.ts`)

| Tool | Line | Parameters | Backend Endpoint |
|------|------|------------|------------------|
| `chat_with_agent` | 88-131 | `{agent_name, message, parallel?, ...}` | `POST /api/agents/{name}/chat` or `/task` |
| `get_chat_history` | 137-151 | `{agent_name}` | `GET /api/agents/{name}/chat/history` |
| `get_agent_logs` | 158-182 | `{agent_name, lines?}` | `GET /api/agents/{name}/logs` |

### Parallel Mode (Added 2025-12-22)

The `chat_with_agent` tool now supports a `parallel` parameter for stateless, concurrent execution:

```typescript
chat_with_agent({
  agent_name: "worker-1",
  message: "Process this task",
  parallel: true,              // Bypass queue, run stateless
  model: "sonnet",             // Optional: model override
  allowed_tools: ["Read"],     // Optional: tool restrictions
  system_prompt: "Be concise", // Optional: additional instructions
  timeout_seconds: 300         // Optional: timeout (default 5 min)
})
```

**When `parallel: true`**:
- Calls `POST /api/agents/{name}/task` instead of `/chat`
- Bypasses execution queue entirely
- Runs without `--continue` flag (no conversation context)
- Allows N concurrent tasks per agent

See [Parallel Headless Execution](parallel-headless-execution.md) for full details.

### Queue-Aware Chat (429 Handling) - Added 2025-12-06

When the target agent's execution queue is full, the `chat_with_agent` tool returns a structured busy response instead of failing:

```typescript
// chat.ts:101-140
if ('queue_status' in response) {
    return JSON.stringify({
        status: "agent_busy",
        agent: agent_name,
        queue_status: "queue_full",
        retry_after_seconds: 30,
        message: `Agent '${agent_name}' is currently busy...`,
    }, null, 2);
}
```

**Client-side 429 handling** (`client.ts:253-296`):
```typescript
if (response.status === 429) {
    return {
        error: "Agent is busy",
        queue_status: "queue_full",
        retry_after: details.retry_after || 30,
        agent: name,
    };
}
```

### Agent-to-Agent Access Control (`chat.ts:26-78`)
```typescript
async function checkAgentAccess(
  client: TrinityClient,
  authContext: McpAuthContext | undefined,
  targetAgentName: string
): Promise<AgentAccessCheckResult> {
  // Access rules:
  // 1. Same owner - allowed
  // 2. Shared agent - allowed
  // 3. Admin bypass - allowed
  // Otherwise - denied
}
```

---

## Trinity Client (`src/mcp-server/src/client.ts`)

### Authentication (lines 32-55)
```typescript
async authenticate(username: string, password: string): Promise<void> {
  // Store credentials for re-authentication
  this.username = username;
  this.password = password;

  const formData = new URLSearchParams();
  formData.append("username", username);
  formData.append("password", password);

  const response = await fetch(`${this.baseUrl}/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: formData,
  });

  const data = (await response.json()) as TokenResponse;
  this.token = data.access_token;
}
```

### Request Pattern with Auto-Retry (lines 86-128)
```typescript
private async request<T>(method: string, path: string, body?: unknown, isRetry = false) {
  const response = await fetch(`${this.baseUrl}${path}`, {
    method,
    headers: { Authorization: `Bearer ${this.token}` },
    body: body ? JSON.stringify(body) : undefined,
  });

  // Handle 401 with re-authentication
  if (response.status === 401 && !isRetry) {
    await this.reauthenticate();
    return this.request<T>(method, path, body, true);
  }
}
```

### Client Factory Pattern (agents.ts:25-38, chat.ts:86-99)
```typescript
const getClient = (authContext?: McpAuthContext): TrinityClient => {
  if (requireApiKey) {
    // MCP API key is REQUIRED - no fallback
    if (!authContext?.mcpApiKey) {
      throw new Error("MCP API key authentication required but no API key found");
    }
    // Create new client instance authenticated with user's MCP API key
    const userClient = new TrinityClient(client.getBaseUrl());
    userClient.setToken(authContext.mcpApiKey);
    return userClient;
  }
  // API key auth disabled - use base client (backward compatibility for local dev)
  return client;
};
```

**Key Change (2025-12-03)**: When `requireApiKey=true`, the MCP server no longer falls back to an admin-authenticated client. All API calls MUST include the user's MCP API key. This eliminates the need for admin username/password credentials between MCP server and backend.

---

## Backend Layer

### MCP API Key Validation (`routers/mcp_keys.py:154-211`)
```python
@router.post("/validate")
async def validate_mcp_api_key_http_endpoint(request: Request):
    """
    Validate an MCP API key (called by the MCP server).
    NOT protected by JWT - validates MCP API keys.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")

    api_key = auth_header[7:]  # Remove "Bearer " prefix
    result = db.validate_mcp_api_key(api_key)

    if not result:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")

    return {
        "valid": True,
        "user_id": result.get("user_id"),
        "user_email": result.get("user_email"),
        "key_name": result.get("key_name"),
        "agent_name": result.get("agent_name"),  # For agent-to-agent
        "scope": result.get("scope", "user")  # 'user' or 'agent'
    }
```

### MCP API Key Management (`routers/mcp_keys.py`)

| Line | Endpoint | Method | Purpose |
|------|----------|--------|---------|
| 15-54 | `/api/mcp/keys` | POST | Create API key |
| 57-79 | `/api/mcp/keys` | GET | List user's keys |
| 82-103 | `/api/mcp/keys/{id}` | GET | Get key details |
| 106-127 | `/api/mcp/keys/{id}/revoke` | POST | Revoke key |
| 130-151 | `/api/mcp/keys/{id}` | DELETE | Delete key |

---

## Database Layer (`src/backend/database.py`)

### MCP API Keys Table (lines 212-227)
```sql
CREATE TABLE IF NOT EXISTS mcp_api_keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    key_prefix TEXT NOT NULL,
    key_hash TEXT UNIQUE NOT NULL,  -- SHA-256 hash
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    usage_count INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    user_id INTEGER NOT NULL REFERENCES users(id),
    agent_name TEXT,  -- For agent-scoped keys
    scope TEXT DEFAULT 'user'  -- 'user' or 'agent'
);
```

### Key Operations
| Method | Purpose |
|--------|---------|
| `create_mcp_api_key()` | Generate and store key with SHA-256 hash |
| `validate_mcp_api_key()` | Validate key, update usage count and last_used_at |
| `list_mcp_api_keys()` | List user's keys (without secrets) |
| `revoke_mcp_api_key()` | Deactivate key (set is_active=0) |
| `delete_mcp_api_key()` | Permanently delete key |

### Key Format
```python
def _generate_mcp_api_key(self) -> str:
    return f"trinity_mcp_{secrets.token_urlsafe(32)}"
```

---

## Frontend Layer (API Key Management)

### ApiKeys.vue (`src/frontend/src/views/ApiKeys.vue`)

```javascript
// List keys
const fetchApiKeys = async () => {
  const response = await fetch('/api/mcp/keys', {
    headers: { 'Authorization': `Bearer ${authStore.token}` }
  })
  apiKeys.value = await response.json()
}

// Create key (shown only once!)
const createKey = async () => {
  const response = await fetch('/api/mcp/keys', {
    method: 'POST',
    body: JSON.stringify({ name: newKey.value.name })
  })
  createdApiKey.value = data.api_key  // Display full key once
}
```

---

## Docker Configuration

### Local (`docker-compose.yml:94-113`)
```yaml
mcp-server:
  ports:
    - "8080:8080"
  environment:
    - TRINITY_API_URL=http://backend:8000
    - MCP_REQUIRE_API_KEY=${MCP_REQUIRE_API_KEY:-false}
    # Note: TRINITY_USERNAME/PASSWORD only needed when MCP_REQUIRE_API_KEY=false
```

### Production (`docker-compose.prod.yml:141-165`)
```yaml
mcp-server:
  ports:
    - "8007:8080"  # External port 8007
  environment:
    - MCP_REQUIRE_API_KEY=${MCP_REQUIRE_API_KEY:-true}
    # No admin credentials needed - uses user's MCP API key for all backend calls
```

**Important**: In production (`MCP_REQUIRE_API_KEY=true`), the MCP server does NOT need `TRINITY_USERNAME` or `TRINITY_PASSWORD`. All backend API calls are authenticated using the user's MCP API key from each request.

---

## Client Configuration

### Claude Code `.mcp.json`
```json
{
  "mcpServers": {
    "trinity": {
      "url": "http://localhost:8080/mcp",
      "headers": {
        "Authorization": "Bearer trinity_mcp_YOUR_API_KEY"
      }
    }
  }
}
```

---

## Side Effects

### Usage Tracking
Every API key validation increments usage counter:
```python
cursor.execute("""
    UPDATE mcp_api_keys
    SET last_used_at = ?, usage_count = usage_count + 1
    WHERE id = ?
""", (now, row["id"]))
```

### Audit Logging
```python
await log_audit_event(
    event_type="mcp_api_key",
    action="create|validate|revoke|delete",
    user_id=current_user.username,
    resource=f"mcp_key-{key_id}",
    ip_address=request.client.host
)
```

---

## Error Handling

| Error Case | HTTP Status | Source |
|------------|-------------|--------|
| Missing Authorization | 401 | MCP Server |
| Invalid API key | 401 | Backend validation |
| Agent not found | 404 | Backend |
| Chat timeout | 503 | Backend (120s) |
| Access denied (agent-to-agent) | 403 | MCP Chat Tool |

---

## Security Considerations

1. **API Key Hashing**: SHA-256, never stored in plaintext
2. **One-time Display**: Full key only shown at creation
3. **Key Prefix Tracking**: Only `trinity_mcp_XXXX...` shown in UI
4. **Usage Audit**: All usage tracked with timestamps and IP
5. **Token Refresh**: Auto-refresh on 401 in Trinity client
6. **Network Isolation**: Agent chat via internal Docker network
7. **Agent-to-Agent Access Control**: Validates ownership/sharing before allowing communication
8. **Request-Scoped Auth**: No shared state between concurrent requests (race condition fixed)

---

## Troubleshooting

### Race Condition Bug (Fixed 2025-12-02)

**Symptoms**:
- Agents created via MCP assigned to wrong user (usually 'admin' or last authenticated user)
- Multiple concurrent MCP requests interfere with each other

**Root Cause**:
- Module-level `currentAuthContext` variable shared across all requests
- Request A sets context, Request B overwrites it, Request A reads wrong context

**Fix**:
- Removed module-level state
- Use FastMCP's `context.session` for request-scoped auth context
- Each request gets its own isolated auth context
- No race conditions possible

**Validation**:
```bash
# Create agents from two different users simultaneously
# Both agents should be owned by their respective creators, not admin
```

### Backend Validation Bug (Fixed 2025-12-02)

**Symptoms**:
- MCP API keys always fail validation
- Backend logs show "MCP validation result: {...}" but auth still fails

**Root Cause**:
```python
# ❌ BEFORE (BROKEN)
if mcp_key_info.get("valid"):  # Dict doesn't have "valid" key!
    # This block never executes

# ✅ AFTER (FIXED)
if mcp_key_info:  # validate_mcp_api_key() returns dict if valid, None if invalid
    # This correctly checks if key is valid
```

**Fix Location**: `src/backend/dependencies.py:95`

---

## Testing

### Manual Testing
```bash
# Create API key
curl -X POST http://localhost:8000/api/mcp/keys \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-orchestrator-key", "description": "For head agent"}'

# List keys
curl http://localhost:8000/api/mcp/keys \
  -H "Authorization: Bearer $TOKEN"

# Validate key (MCP server calls this)
curl -X POST http://localhost:8000/api/mcp/validate \
  -H "Authorization: Bearer trinity_mcp_YOUR_KEY"

# Test MCP tool via Claude Code
# In Claude Code with .mcp.json configured:
# "List all Trinity agents"
# "Create an agent named test-agent"
# "Chat with test-agent and ask it to list files"
```

### Integration Testing
```bash
# Start MCP server
cd src/mcp-server
npm run build
npm start

# Test with MCP inspector
npx @modelcontextprotocol/inspector http://localhost:8080/mcp

# Verify 12 tools are registered:
# - list_agents, get_agent, create_agent, delete_agent
# - start_agent, stop_agent, list_templates
# - reload_credentials, get_credential_status
# - chat_with_agent, get_chat_history, get_agent_logs
```

### Race Condition Testing
```bash
# Test concurrent agent creation from different users
# All agents should be owned by their respective creators

# Terminal 1 (user1)
USER1_KEY="trinity_mcp_user1_key"
claude --mcp-key "$USER1_KEY" "Create an agent named user1-agent"

# Terminal 2 (user2) - run simultaneously
USER2_KEY="trinity_mcp_user2_key"
claude --mcp-key "$USER2_KEY" "Create an agent named user2-agent"

# Verify ownership
curl http://localhost:8000/api/agents/user1-agent | jq .owner  # Should be user1
curl http://localhost:8000/api/agents/user2-agent | jq .owner  # Should be user2
```

---

## Status
✅ **Working** - All 12 MCP tools functional with API key authentication, agent-to-agent access control, and race condition fixed

---

## Related Flows

- **Upstream**: Auth0 Authentication (user creates keys)
- **Integrates With**:
  - Execution Queue (`execution-queue.md`) - `chat_with_agent` calls go through queue with 429 handling (unless `parallel: true`)
  - Parallel Headless Execution (`parallel-headless-execution.md`) - When `parallel: true`, bypasses queue and uses `/task` endpoint (Added 2025-12-22)
- **Downstream**: Agent Lifecycle, Agent Chat, Credential Injection, Agent Sharing (access control)
