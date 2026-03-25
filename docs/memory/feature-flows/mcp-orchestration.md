# Feature: MCP Orchestration

## Overview
External integration layer allowing Claude Code instances to manage Trinity agents via the Model Context Protocol (MCP). Exposes 55 tools for agent lifecycle, chat, system management, credential management (CRED-002), subscription management (SUB-001), SSH access, skills, schedule management, tag management, monitoring (MON-001), and notifications through a FastMCP server with Streamable HTTP transport.

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

### 1. MCP Client → MCP Server Authentication (`server.ts:114-151`)

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

### 2. Tool Execution with Auth Context (`agents.ts:202-248`)

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

**getClient() Helper** (`agents.ts:25-38`):
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

### 3. MCP Server → Backend API Call (`client.ts:103-155`)

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

### 4. Backend Validates MCP API Key (`dependencies.py:104-155`)

FastAPI dependency validates incoming Bearer token:

```python
async def get_current_user(request: Request, token: str = Depends(oauth2_scheme)) -> User:
    """
    FastAPI dependency to get the current authenticated user.
    Validates JWT token OR MCP API key and returns User object.
    """
    credentials_exception = HTTPException(...)

    # Try JWT token first
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        # ... return User object
    except JWTError:
        # JWT failed, try MCP API key
        pass

    # Try MCP API key authentication
    mcp_key_info = db.validate_mcp_api_key(token)
    if mcp_key_info:  # Returns dict if valid, None if invalid
        user_email = mcp_key_info.get("user_email")
        user_id = mcp_key_info.get("user_id")  # This is actually username, not DB id

        # Get full user record - try email first, then username
        user = db.get_user_by_email(user_email) if user_email else db.get_user_by_username(user_id)
        if user:
            return User(...)

    # Both JWT and MCP key failed
    raise credentials_exception
```

**Key Implementation Detail**: Line 139 checks `if mcp_key_info:` - `validate_mcp_api_key()` returns dict if valid, `None` if invalid

### 5. Agent Creation with Correct Owner

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

### Server Configuration (`src/mcp-server/src/server.ts:64-74`)
```typescript
export async function createServer(config: ServerConfig = {}) {
  const {
    name = "trinity-orchestrator",
    version = "1.0.0" as const,
    trinityApiUrl = process.env.TRINITY_API_URL || "http://localhost:8000",
    trinityApiToken = process.env.TRINITY_API_TOKEN,
    trinityUsername = process.env.TRINITY_USERNAME || "admin",
    trinityPassword = process.env.TRINITY_PASSWORD || "changeme",
    port = parseInt(process.env.MCP_PORT || "8080", 10),
    requireApiKey = process.env.MCP_REQUIRE_API_KEY === "true",
  } = config;
}
```

**Note**: When `MCP_REQUIRE_API_KEY=true` (production), NO admin credentials are needed. All backend API calls use the user's MCP API key directly. This eliminates the complexity and recurring issues with admin password authentication.

### API Key Validation (`src/mcp-server/src/server.ts:39-59`)
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

### Tool Registration (`src/mcp-server/src/server.ts:156-192`)
```typescript
// Register agent management tools (11 tools)
const agentTools = createAgentTools(client, requireApiKey);
server.addTool(agentTools.listAgents);
server.addTool(agentTools.getAgent);
// ... more tools

// Register chat tools (3 tools)
const chatTools = createChatTools(client, requireApiKey);
server.addTool(chatTools.chatWithAgent);
// ...

// Register system management tools (4 tools)
const systemTools = createSystemTools(client, requireApiKey);

// Register documentation tools (1 tool)
const docsTools = createDocsTools();

console.log(`Registered ${totalTools} tools`);
```

---

## MCP Tools (39 total)

### Agent Management (`src/mcp-server/src/tools/agents.ts`) - 16 tools

| Tool | Line | Parameters | Backend Endpoint |
|------|------|------------|------------------|
| `list_agents` | 44-79 | `{}` | `GET /api/agents` |
| `get_agent` | 84-98 | `{name}` | `GET /api/agents/{name}` |
| `get_agent_info` | 103-145 | `{name}` | `GET /api/agents/{name}/info` |
| `create_agent` | 150-249 | `{name, type?, template?, resources?, tools?, mcp_servers?, custom_instructions?}` | `POST /api/agents` |
| `delete_agent` | 254-281 | `{name}` | `DELETE /api/agents/{name}` |
| `start_agent` | 286-301 | `{name}` | `POST /api/agents/{name}/start` |
| `stop_agent` | 306-321 | `{name}` | `POST /api/agents/{name}/stop` |
| `list_templates` | 326-339 | `{}` | `GET /api/templates` |
| `get_credential_status` | 344-362 | `{name}` | `GET /api/agents/{name}/credentials/status` |
| `inject_credentials` | 365-392 | `{name, files}` | `POST /api/agents/{name}/credentials/inject` |
| `export_credentials` | 395-416 | `{name}` | `POST /api/agents/{name}/credentials/export` |
| `import_credentials` | 420-442 | `{name}` | `POST /api/agents/{name}/credentials/import` |
| `get_credential_encryption_key` | 446-462 | `{}` | `GET /api/credentials/encryption-key` |
| `get_agent_ssh_access` | 466-503 | `{agent_name, ttl_hours?, auth_method?}` | `POST /api/agents/{name}/ssh-access` |
| `deploy_local_agent` | 506-607 | `{archive, credentials?, name?}` | `POST /api/agents/deploy-local` |
| `initialize_github_sync` | 611-686 | `{agent_name, repo_owner, repo_name, create_repo?, private?, description?}` | `POST /api/agents/{name}/git/initialize` |

> **CRED-002 Update (2026-02-05)**: The old `reload_credentials` tool has been replaced with 4 new credential management tools:
> - `inject_credentials` - Directly inject files into agent workspace
> - `export_credentials` - Export to encrypted `.credentials.enc` file
> - `import_credentials` - Import from encrypted `.credentials.enc` file
> - `get_credential_encryption_key` - Get platform encryption key for local agents

### Chat Tools (`src/mcp-server/src/tools/chat.ts`)

| Tool | Line | Parameters | Backend Endpoint |
|------|------|------------|------------------|
| `chat_with_agent` | 132-270 | `{agent_name, message, parallel?, model?, allowed_tools?, system_prompt?, timeout_seconds?}` | `POST /api/agents/{name}/chat` or `/task` |
| `get_chat_history` | 275-292 | `{agent_name}` | `GET /api/agents/{name}/chat/history` |
| `get_agent_logs` | 297-326 | `{agent_name, lines?}` | `GET /api/agents/{name}/logs` |

### System Tools (`src/mcp-server/src/tools/systems.ts`)

| Tool | Line | Parameters | Description |
|------|------|------------|-------------|
| `deploy_system` | 44-100 | `{manifest, dry_run?}` | Deploy multi-agent system from YAML |
| `list_systems` | 105-133 | `{}` | List deployed systems grouped by prefix |
| `restart_system` | 138-167 | `{system_name}` | Restart all agents in system |
| `get_system_manifest` | 172-202 | `{system_name}` | Export system as YAML manifest |

### Documentation Tools (`src/mcp-server/src/tools/docs.ts`)

| Tool | Line | Parameters | Description |
|------|------|------------|-------------|
| `get_agent_requirements` | 97-109 | `{}` | Get Trinity Compatible Agent Guide |

### Tag Tools (`src/mcp-server/src/tools/tags.ts`) - ORG-001 Phase 3

| Tool | Line | Parameters | Description |
|------|------|------------|-------------|
| `list_tags` | 40-54 | `{}` | List all unique tags with agent counts |
| `get_agent_tags` | 59-77 | `{agent_name}` | Get tags for a specific agent |
| `tag_agent` | 82-118 | `{agent_name, tag}` | Add a tag to an agent (with validation) |
| `untag_agent` | 123-145 | `{agent_name, tag}` | Remove a tag from an agent |
| `set_agent_tags` | 150-189 | `{agent_name, tags}` | Replace all tags for an agent |

See [Agent Tags & System Views](agent-tags.md) for full tag system documentation.

### Schedule Tools (`src/mcp-server/src/tools/schedules.ts`) - 8 tools

| Tool | Parameters | Backend Endpoint |
|------|------------|------------------|
| `list_agent_schedules` | `{agent_name}` | `GET /api/agents/{name}/schedules` |
| `create_agent_schedule` | `{agent_name, name, cron_expression, message, timezone?, enabled?, description?, timeout_seconds?, allowed_tools?}` | `POST /api/agents/{name}/schedules` |
| `get_agent_schedule` | `{agent_name, schedule_id}` | `GET /api/agents/{name}/schedules/{id}` |
| `update_agent_schedule` | `{agent_name, schedule_id, ...fields}` | `PUT /api/agents/{name}/schedules/{id}` |
| `delete_agent_schedule` | `{agent_name, schedule_id}` | `DELETE /api/agents/{name}/schedules/{id}` |
| `toggle_agent_schedule` | `{agent_name, schedule_id, enabled}` | `POST /api/agents/{name}/schedules/{id}/enable` or `/disable` |
| `trigger_agent_schedule` | `{agent_name, schedule_id}` | `POST /api/agents/{name}/schedules/{id}/trigger` |
| `get_schedule_executions` | `{agent_name, schedule_id?, limit?}` | `GET /api/agents/{name}/schedules/{id}/executions` |

> **Per-Schedule Configuration (2026-02-20)**: `create_agent_schedule` and `update_agent_schedule` now support:
> - `timeout_seconds` - Execution timeout (default 900s, options: 300, 900, 1800, 3600, 7200)
> - `allowed_tools` - Optional list of permitted tools (null = all tools allowed)

See [Agent Scheduling](scheduling.md) for full scheduling system documentation.

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
- Creates `schedule_executions` record (visible in Tasks tab)

**When `parallel: false` (default)** *(updated 2025-12-30)*:
- Calls `POST /api/agents/{name}/chat`
- Uses execution queue (one at a time per agent)
- Maintains conversation context with `--continue` flag
- Agent-to-agent calls (with `X-Source-Agent` header) now create `schedule_executions` record (visible in Tasks tab)

See [Parallel Headless Execution](parallel-headless-execution.md) for full details.

### Async Mode (Fire-and-Forget) (Added 2026-01-30)

When `parallel=true`, you can additionally specify `async=true` for fire-and-forget execution:

```typescript
chat_with_agent({
  agent_name: "worker-1",
  message: "Process large dataset",
  parallel: true,              // Required for async mode
  async: true,                 // Return immediately with execution_id
  timeout_seconds: 3600        // Long-running task
})
// Returns immediately:
// {
//   "status": "accepted",
//   "execution_id": "abc123xyz",
//   "agent_name": "worker-1",
//   "message": "Task accepted. Poll GET /api/agents/{name}/executions/{execution_id} for results.",
//   "async_mode": true
// }
```

**When `async: true` (with `parallel: true`)**:
- Backend spawns task in background thread via `asyncio.create_task()`
- Returns immediately with `execution_id` for polling
- Execution status set to "running" in database
- Poll `GET /api/agents/{name}/executions/{execution_id}` for status and results
- Background task updates execution record when complete (success/failed)
- Activities tracked and completed asynchronously

**Use cases for async mode**:
- Fan-out patterns: spawn multiple long-running tasks without waiting
- Non-blocking orchestration: orchestrator continues while workers process
- Background jobs: trigger maintenance/analysis tasks that run independently
- Load distribution: batch processing across multiple agents

**Polling for results via MCP (MCP-007)**:
```typescript
// Poll using get_execution_result MCP tool
get_execution_result({
  agent_name: "worker-1",
  execution_id: "abc123xyz"
})
// Returns: { "status": "running", ... }  -- keep polling
// Returns: { "status": "success", "response": "...", "cost": 0.05, ... }  -- done

// Also available: list_recent_executions to find execution IDs
list_recent_executions({ agent_name: "worker-1", limit: 5 })

// And: get_agent_activity_summary for monitoring
get_agent_activity_summary({ agent_name: "worker-1", hours: 24 })
```

**Polling for results via REST API**:
```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/agents/worker-1/executions/abc123xyz
```

See [Parallel Headless Execution](parallel-headless-execution.md) for implementation details.

### Dynamic GitHub Templates (Added 2025-12-30)

The `create_agent` tool now supports creating agents from **any** GitHub repository - not just pre-defined templates:

```typescript
create_agent({
  name: "my-custom-agent",
  template: "github:myorg/my-agent-repo"  // Any repo the PAT can access
})
```

**How it works**:
1. If `template` matches a pre-defined template from `list_templates`, use that configuration
2. Otherwise, parse `github:owner/repo` format and use the system GitHub PAT
3. The PAT must be configured in Settings or via `GITHUB_PAT` environment variable
4. Repository is cloned at agent startup, just like pre-defined templates

**Requirements**:
- System GitHub PAT must be configured (Settings → API Keys)
- PAT must have `repo` scope and access to the target repository
- Repository should be a valid Trinity-compatible agent (CLAUDE.md recommended)

**Template formats**:
- `github:owner/repo` - Any GitHub repository (dynamic)
- `github:abilityai/agent-ruby` - Pre-defined template (from `list_templates`)
- `local:template-name` - Local template from config directory

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

**Client-side 429 handling** (`client.ts:355-370`):
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

### Agent-to-Agent Access Control (`chat.ts:29-100`)
```typescript
async function checkAgentAccess(
  client: TrinityClient,
  authContext: McpAuthContext | undefined,
  targetAgentName: string
): Promise<AgentAccessCheckResult> {
  // Access rules for System-scoped keys (Phase 11.1):
  // - ALWAYS allowed - system agent bypasses all permission checks

  // Access rules for User-scoped keys:
  // - Same owner: Always allowed
  // - Shared agent: Allowed
  // - Admin: Always allowed (bypass)
  // - Otherwise: Denied

  // Access rules for Agent-scoped keys (Phase 9.10):
  // - Self: Always allowed
  // - Target in permitted list: Allowed
  // - Otherwise: Denied (even if same owner)
}
```

---

## Trinity Client (`src/mcp-server/src/client.ts`)

### Authentication (lines 46-69)
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

### Request Pattern with Auto-Retry (lines 103-155)
```typescript
async request<T>(method: string, path: string, body?: unknown, isRetry = false): Promise<T> {
  if (!this.token) {
    throw new Error("Not authenticated. Call authenticate() first or setToken().");
  }

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

### Chat with Queue Handling (lines 336-378)
```typescript
async chat(name: string, message: string, sourceAgent?: string): Promise<ChatResponse | QueueStatus> {
  // Handle 429 Too Many Requests (agent queue full)
  if (response.status === 429) {
    return {
      error: "Agent is busy",
      queue_status: "queue_full",
      retry_after: details.retry_after || 30,
      agent: name,
    };
  }
}
```

### Parallel Task Execution (lines 393-445)
```typescript
async task(name: string, message: string, options?: TaskOptions, sourceAgent?: string): Promise<ChatResponse> {
  // Stateless execution, no queue, can run N tasks concurrently
  const response = await fetch(`${this.baseUrl}/api/agents/${name}/task`, ...);
}
```

### Client Factory Pattern (agents.ts:25-38, chat.ts:113-126)
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

### MCP API Key Validation (`routers/mcp_keys.py:144-180`)
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
| 14-34 | `/api/mcp/keys` | POST | Create API key |
| 37-51 | `/api/mcp/keys` | GET | List user's keys |
| 54-96 | `/api/mcp/keys/ensure-default` | POST | Ensure default key exists |
| 99-111 | `/api/mcp/keys/{id}` | GET | Get key details |
| 114-126 | `/api/mcp/keys/{id}/revoke` | POST | Revoke key |
| 129-141 | `/api/mcp/keys/{id}` | DELETE | Delete key |
| 144-180 | `/api/mcp/validate` | POST | Validate API key (MCP server) |

---

## Database Layer (`src/backend/database.py`)

### MCP API Keys Table (lines 356-371)
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
    user_id INTEGER NOT NULL,
    agent_name TEXT,  -- For agent-scoped keys
    scope TEXT DEFAULT 'user',  -- 'user', 'agent', or 'system'
    FOREIGN KEY (user_id) REFERENCES users(id)
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
      "type": "http",
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

### Dashboard Timeline Visualization
MCP executions appear on the Dashboard Timeline with distinct **pink** color (`#ec4899`) when `triggered_by='mcp'`. This distinguishes MCP-initiated tasks from manual (green), scheduled (purple), and agent-triggered (cyan) executions. See [Dashboard Timeline View](dashboard-timeline-view.md) for full color scheme.

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

**Fix Location**: `src/backend/dependencies.py:139`

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

# Verify 44 tools are registered:
# Agent tools (16):
# - list_agents, get_agent, get_agent_info, create_agent, delete_agent
# - start_agent, stop_agent, list_templates
# - get_credential_status, inject_credentials, export_credentials
# - import_credentials, get_credential_encryption_key
# - get_agent_ssh_access, deploy_local_agent, initialize_github_sync
# Chat tools (3):
# - chat_with_agent, get_chat_history, get_agent_logs
# System tools (4):
# - deploy_system, list_systems, restart_system, get_system_manifest
# Docs tools (1):
# - get_agent_requirements
# Skills tools (7):
# - list_skills, get_skill, get_skills_library_status
# - assign_skill_to_agent, set_agent_skills, sync_agent_skills, get_agent_skills
# Schedule tools (8):
# - list_agent_schedules, create_agent_schedule, get_agent_schedule
# - update_agent_schedule, delete_agent_schedule, toggle_agent_schedule
# - trigger_agent_schedule, get_schedule_executions
# Tag tools (5) - ORG-001 Phase 3:
# - list_tags, get_agent_tags, tag_agent, untag_agent, set_agent_tags
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

## Revision History

| Date | Changes |
|------|---------|
| 2026-02-22 | **Subscription tools (SUB-001)**: Tool count updated from 45 to 51. Added 6 subscription management tools: `register_subscription`, `list_subscriptions`, `assign_subscription`, `clear_agent_subscription`, `get_agent_auth`, `delete_subscription`. See [subscription-management.md](subscription-management.md) for full documentation. |
| 2026-02-20 | **Notification tools (NOTIF-001)**: Tool count updated from 44 to 45. Added `send_notification` tool. |
| 2026-02-20 | **Schedule tools: Per-schedule execution config**: `create_agent_schedule` and `update_agent_schedule` now support `timeout_seconds` and `allowed_tools` parameters for per-schedule execution configuration. Added dedicated Schedule Tools section with parameter table. |
| 2026-02-17 | **Tag tools (ORG-001 Phase 3)**: Tool count updated from 39 to 44. Added 5 tag management tools: `list_tags`, `get_agent_tags`, `tag_agent`, `untag_agent`, `set_agent_tags`. See [agent-tags.md](agent-tags.md) for full documentation. |
| 2026-02-13 | **SSH host detection fix (cross-ref)**: The `get_agent_ssh_access` tool now returns correct host in production deployments. See [ssh-access.md](ssh-access.md) for details on the `FRONTEND_URL` domain extraction fix. |
| 2026-02-05 | **CRED-002 Credential System**: Tool count updated from 36 to 39 (16 agent, 3 chat, 4 system, 1 docs, 7 skills, 8 schedule). Replaced `reload_credentials` with 4 new credential tools: `inject_credentials`, `export_credentials`, `import_credentials`, `get_credential_encryption_key`. Updated all line numbers for agents.ts. |
| 2026-01-30 | **Async mode (fire-and-forget)**: Added `async` parameter to `chat_with_agent` tool. When `parallel=true` and `async=true`, returns immediately with `execution_id` for polling. Backend spawns background task via `asyncio.create_task()`. Poll `GET /api/agents/{name}/executions/{id}` for results. |
| 2026-01-29 | **Schedule tools (MCP-SCHED-001)**: Added 8 schedule management tools - list_agent_schedules, create_agent_schedule, get_agent_schedule, update_agent_schedule, delete_agent_schedule, toggle_agent_schedule, trigger_agent_schedule, get_schedule_executions. Total now 36 tools (13 agent, 3 chat, 4 system, 1 docs, 7 skills, 8 schedule). |
| 2026-01-23 | **Flow verification**: Updated all line numbers to match current implementation. Verified 21 tools (13 agent, 3 chat, 4 system, 1 docs). Updated client.ts line refs (authenticate: 46-69, request: 103-155, chat: 336-378, task: 393-445). Updated dependencies.py refs (104-155). Updated mcp_keys.py refs (144-180). Added ensure-default endpoint. Fixed database table line (356-371). |
| 2026-01-15 | **Docs**: Added Dashboard Timeline Visualization section noting pink (#ec4899) color for MCP executions |
| 2026-01-03 | **get_agent_info tool**: Added new tool to retrieve full template.yaml metadata for agents. Supports access control - agent-scoped keys can only access self + permitted agents. Returns capabilities, commands, MCP servers, tools, skills, use cases. |
| 2026-01-03 | **Tool count update**: 21 tools total (13 agent, 3 chat, 4 system, 1 docs). Added `get_agent_info` (agents.ts:103-145) and `get_agent_ssh_access` (agents.ts:385-420). Updated all line numbers. |
| 2025-12-30 | **Agent-to-agent chat tracking**: Non-parallel `chat_with_agent` calls now create `schedule_executions` records when agent-scoped, ensuring all MCP agent communications appear in Tasks tab. |
| 2025-12-30 | **Dynamic GitHub Templates**: `create_agent` now supports any `github:owner/repo` format - not just pre-defined templates. Uses system GITHUB_PAT for access. |
| 2025-12-22 | Added parallel mode to chat_with_agent tool |
| 2025-12-03 | MCP API key authentication deployed to production |
| 2025-12-02 | Fixed race condition bug in auth context handling |

---

## Status
Working - All 51 MCP tools functional with API key authentication, agent-to-agent access control, system agent bypass, CRED-002 credential system, SUB-001 subscription management, tag management (ORG-001), NOTIF-001 notifications, and race condition fixed

---

## Related Flows

- **Upstream**: Auth0 Authentication (user creates keys)
- **Integrates With**:
  - Execution Queue (`execution-queue.md`) - `chat_with_agent` calls go through queue with 429 handling (unless `parallel: true`)
  - Parallel Headless Execution (`parallel-headless-execution.md`) - When `parallel: true`, bypasses queue and uses `/task` endpoint (Added 2025-12-22)
  - Agent Permissions (`agent-permissions.md`) - Agent-scoped keys use permission system (Phase 9.10)
  - SSH Access (`ssh-access.md`) - `get_agent_ssh_access` tool implementation with host detection priority
- **Downstream**: Agent Lifecycle, Agent Chat, Credential Injection, Agent Sharing (access control)
