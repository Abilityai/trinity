# Agent-to-Agent Collaboration via Platform MCP

**Status**: Implemented
**Date**: 2025-11-29
**Priority**: High
**Last Updated**: 2026-06-21 (Pull-pilot routing #946 added)

---

## Overview

Agents on the Trinity platform communicate with each other via the Trinity MCP server. Each agent receives an agent-scoped MCP API key at creation time, which identifies the agent for access control when calling other agents.

---

## User Story

As an orchestrator agent, I want to delegate tasks to specialized worker agents so that complex work can be decomposed and executed by the most appropriate agent.

---

## Entry Points

- **Agent Code**: Claude Code (or Gemini CLI) invokes `mcp__trinity__chat_with_agent` tool
- **MCP Endpoint**: `POST /mcp` (HTTP transport with Bearer token auth)
- **Backend API**: `POST /api/agents/{name}/chat` (with X-Source-Agent header)

---

## Architecture

```
Agent A Container (Source)
  |
  +-> Claude Code: mcp__trinity__chat_with_agent(agent_name="agent-b", message="...")
       |
       +-> MCP Client: POST http://mcp-server:8080/mcp
            |  Authorization: Bearer trinity_mcp_<agent-a-key>
            |
            +-> MCP Server (server.ts:111-152)
                 |  authenticate() validates key via /api/mcp/validate
                 |  Returns McpAuthContext with agentName, scope="agent"
                 |
                 +-> chat_with_agent tool (chat.ts:186-269)
                      |  checkAgentAccess() enforces permissions
                      |  Calls backend with X-Source-Agent header
                      |
                      +-> Backend: POST /api/agents/agent-b/chat (chat.py:106-416)
                           |  Detects collaboration via X-Source-Agent header
                           |  Broadcasts WebSocket event
                           |  Routes to agent container
                           |
                           +-> Agent B Container receives message
                                |
                                +-> Claude Code processes, returns response
```

---

## Frontend Layer

Not applicable - agent-to-agent communication is triggered from agent containers, not the UI. However, collaboration events are broadcast to the UI for visualization.

### WebSocket Events
Frontend clients receive real-time collaboration events:
```json
{
  "type": "agent_collaboration",
  "source_agent": "agent-a",
  "target_agent": "agent-b",
  "action": "chat",
  "timestamp": "2026-01-23T10:30:00Z"
}
```

---

## MCP Layer

### Server Authentication
**File**: `src/mcp-server/src/server.ts`
**Lines**: 111-152

```typescript
const server = new FastMCP({
  name,
  version,
  authenticate: requireApiKey
    ? async (request) => {
        const authHeader = request.headers["authorization"] as string | undefined;
        if (!authHeader || !authHeader.startsWith("Bearer ")) {
          throw new Error("Missing or invalid Authorization header");
        }

        const apiKey = authHeader.substring(7);
        const result = await validateMcpApiKey(trinityApiUrl, apiKey);

        if (result && result.valid) {
          const authContext: McpAuthContext = {
            userId: result.user_id || "unknown",
            userEmail: result.user_email,
            keyName: result.key_name || "unknown",
            agentName: result.agent_name,  // Agent name if scope is 'agent' or 'system'
            scope: scope as "user" | "agent" | "system",
            mcpApiKey: apiKey,
          };
          return authContext;  // FastMCP stores in session
        }
        throw new Error("Invalid API key");
      }
    : undefined,
});
```

### Chat Tool Access Control
**File**: `src/mcp-server/src/tools/chat.ts`
**Lines**: 29-100

```typescript
async function checkAgentAccess(
  client: TrinityClient,
  authContext: McpAuthContext | undefined,
  targetAgentName: string
): Promise<AgentAccessCheckResult> {
  // If no auth context, allow (auth may be disabled)
  if (!authContext) {
    return { allowed: true };
  }

  // Phase 11.1: System-scoped keys bypass ALL permission checks
  if (authContext.scope === "system") {
    return { allowed: true };
  }

  // Phase 9.10: Agent-scoped keys use permission system
  if (authContext.scope === "agent" && authContext.agentName) {
    const callerAgentName = authContext.agentName;
    if (callerAgentName === targetAgentName) {
      return { allowed: true };  // Self-call always allowed
    }
    const isPermitted = await client.isAgentPermitted(callerAgentName, targetAgentName);
    if (isPermitted) {
      return { allowed: true };
    }
    return { allowed: false, reason: `Permission denied: Agent '${callerAgentName}' is not permitted...` };
  }

  // User-scoped keys: check ownership/sharing
  const callerOwner = authContext.userId;
  const targetAgent = await client.getAgentAccessInfo(targetAgentName);

  if (callerOwner === targetAgent.owner) return { allowed: true };
  if (targetAgent.is_shared) return { allowed: true };
  if (callerOwner === "admin") return { allowed: true };

  return { allowed: false, reason: `Access denied...` };
}
```

### Chat Tool Execution
**File**: `src/mcp-server/src/tools/chat.ts`
**Lines**: 186-269

```typescript
execute: async ({ agent_name, message, parallel, ... }, context: any) => {
  // Get auth context from FastMCP session
  const authContext = requireApiKey ? context?.session : undefined;
  const apiClient = getClient(authContext);

  const accessCheck = await checkAgentAccess(apiClient, authContext, agent_name);
  if (!accessCheck.allowed) {
    return JSON.stringify({ error: "Access denied", reason: accessCheck.reason });
  }

  // Log collaboration
  if (authContext?.scope === "agent") {
    console.log(`[Agent Collaboration] ${authContext.agentName} -> ${agent_name}`);
  }

  const sourceAgent = authContext?.scope === "agent" ? authContext.agentName : undefined;

  if (parallel) {
    const response = await apiClient.task(agent_name, message, options, sourceAgent);
    return JSON.stringify(response, null, 2);
  }

  const response = await apiClient.chat(agent_name, message, sourceAgent);
  return JSON.stringify(response, null, 2);
}
```

### MCP Client X-Source-Agent Header
**File**: `src/mcp-server/src/client.ts`
**Lines**: 336-378

```typescript
async chat(name: string, message: string, sourceAgent?: string): Promise<ChatResponse | QueueStatus> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(this.token && { Authorization: `Bearer ${this.token}` }),
    "X-Via-MCP": "true",  // Mark as MCP call for task tracking
  };

  // Add X-Source-Agent header for collaboration tracking
  if (sourceAgent) {
    headers["X-Source-Agent"] = sourceAgent;
  }

  const response = await fetch(`${this.baseUrl}/api/agents/${name}/chat`, {
    method: "POST",
    headers,
    body: JSON.stringify({ message }),
  });
  // ...
}
```

---

## Pull-Pilot Routing (#946)

**Phase 2 PoC for pull / work-stealing coordination** (Epic #1045, umbrella #1081 — see `docs/planning/TARGET_ARCHITECTURE.md`). A flag-gated routing fork on the EXISTING sequential agent→agent `chat_with_agent` path described above.

### What it does

When `MCP_AGENT_CHAT_PULL_ENABLED` is ON, a **sequential** (`parallel=false`) agent→agent (`scope='agent'`, non-self) `chat_with_agent` call is routed by the MCP server through the durable async `/task` path (`apiClient.task(..., {async_mode: true})`) **instead of** the synchronous held `/chat` call. The caller gets an immediate `{status: "accepted" | "queued", execution_id, agent_name, ...}` receipt and **polls `get_execution_result(execution_id)`** for the result.

- **Polling is the contract** — the backend emits no completion event for this path; the caller must poll.
- **Default OFF.** Rollback is a flag flip + MCP routing revert (no schema, no data migration).

### What stays UNCHANGED (deliberate exclusions)

| Case | Behavior | Why excluded |
|------|----------|--------------|
| Flag OFF | Synchronous `/chat` | Default; pilot is opt-in |
| `scope='user'` (human-facing chat) | Synchronous `/chat` | A human's chat turn expects a synchronous reply; blast radius is agent→agent delegation only |
| Self-task (`sourceAgent === agent_name`, SELF-EXEC-001) | Existing path | `inject_result`/`chat_session_id` write the result back into the caller's OWN chat session — an interactive contract a fire-and-forget receipt would silently break |
| `parallel=true` | Already uses `/task` | The pilot only changes the sequential branch |

### MCP Routing Fork

**File**: `src/mcp-server/src/tools/chat.ts`
**Function**: `createChatTools(client, requireApiKey, agentChatPullEnabled = false)` — chat.ts:125-129

- **Auth context read unconditionally** — `const authContext = context?.session;` (chat.ts:277). Previously `requireApiKey ? context?.session : undefined`; now read unconditionally (mirrors `operator_queue.ts`) so the scope-based routing is decidable in tests. Production is behaviour-equivalent: with `requireApiKey=false` FastMCP installs no `authenticate` callback, so the session carries no `scope`/`mcpApiKey` and `getClient()` still returns the base client — agent-scoped keys only exist on the `requireApiKey=true` path (chat.ts:270-276).
- **Routing predicate** — chat.ts:313-317:
  ```typescript
  const usePullRouting =
    !parallel &&
    agentChatPullEnabled &&
    authContext?.scope === "agent" &&
    !isSelfTask;
  ```
- **Pull branch** — chat.ts:391-402: calls `apiClient.task(agent_name, message, { async_mode: true }, sourceAgent, mcpKeyInfo, idempotencyKey)` and returns the receipt JSON. Only `async_mode` is forwarded; `model`/`allowed_tools`/`system_prompt` are parallel-only and were never applied in sequential mode, so omitting them preserves sequential semantics (agent defaults).

### D8 — Route is part of the idempotency identity

**File**: `src/mcp-server/src/tools/chat.ts`
**Lines**: 334-354

```typescript
const routeToken = usePullRouting ? "chat-pull-task" : (parallel ? "task" : "chat");
const idempotencyKey = deriveMcpIdempotencyKey([
  sourceAgent || authContext?.userId,
  agent_name,
  routeToken,
  model,
  asyncMode ? "async" : "sync",
  message,
]);
```

**Why**: the backend derives the same `agent:{name}` idempotency scope for BOTH `/chat` and `/task`. A sequential agent→agent call dispatched as sync `/chat` (flag OFF) vs async `/task` (flag ON) must NOT share an idempotency key — else a flag flip mid-soak could replay a wrong-shape snapshot across endpoints. The pull-routed call therefore gets a distinct `"chat-pull-task"` token. A transport retry WITHIN one mode keeps the same key (still dedupes). Existing parallel / self-task / `scope='user'` tokens are unchanged, so no in-flight key is invalidated on deploy. (See `idempotency-keys.md`, Invariant #18.)

### Flag plumbing

**MCP server** — `src/mcp-server/src/server.ts`:
- `ServerConfig.agentChatPullEnabled` — server.ts:42-48
- `agentChatPullEnabled = process.env.MCP_AGENT_CHAT_PULL_ENABLED === "true"` — server.ts:99-102 (mirrors `requireApiKey ← MCP_REQUIRE_API_KEY`)
- Threaded into `createChatTools(client, requireApiKey, agentChatPullEnabled)` — server.ts:219
- Startup log of routing mode for the soak's control/treatment window — server.ts:194-196:
  `Agent→agent chat pull routing (#946): ON (async /task) | OFF (sync /chat)`

**Backend** — `src/backend/config.py`:
- `MCP_AGENT_CHAT_PULL_ENABLED = os.getenv("MCP_AGENT_CHAT_PULL_ENABLED", "false").lower() == "true"` — config.py:151. This is the **canonical registry entry**. Both services read the SAME env key, so a single-`.env` deploy can't drift. The actual routing gate is MCP-side.
- Surfaced as `mcp_agent_chat_pull_enabled` in `get_public_feature_flags` / `GET /api/settings/feature-flags` — settings.py:110-145 (auth-gated, **observability-only** — lets an operator confirm whether the treatment window is active; NOT a UI surface).

### T5 — Deny-path idempotency claim release

**File**: `src/backend/routers/chat.py`
**Function**: `execute_parallel_task` (chat.py:1289)

The pull pilot routes agent→agent sequential chat through the async `/task` path, so it exercises the two dispatch-breaker-open (`CircuitOpen`) deny branches. Both now call `idempotency_service.fail(idem)` before `_raise_circuit_open_503(...)` to release the `in_flight` claim:
- Async path — chat.py:1585-1595
- Sync path — chat.py:1707-1714

This mirrors the existing `/chat` (chat.py:242) and `CapacityFull` (chat.py:1577/1699) releases. Without it, a breaker-open reject would leave the `in_flight` idempotency row in place and silently block every same-key retry for 24h. (See `dispatch-circuit-breaker.md`.)

### Tests

| Test | Covers |
|------|--------|
| `src/mcp-server/src/chat.test.ts` | Routing fork + D8 dispatch-mode idempotency key (`#946 chat_with_agent pull routing`, `#946 D8 dispatch-mode idempotency key`) |
| `tests/unit/test_946_task_idempotency_on_deny.py` | T5 — `/task` CircuitOpen deny path releases the idempotency claim |
| `tests/test_platform_default_model.py` | `feature-flags` exposes `mcp_agent_chat_pull_enabled`; defaults OFF |

### Reference docs

- `docs/planning/PULL_PILOT_946_SOAK.md` — soak harness + go/no-go criteria
- `docs/planning/ACTOR_MODEL_POSTCARD.md`, `docs/planning/TARGET_ARCHITECTURE.md` — pull-coordination direction (Epic #1045 / #1081)
- `mcp-orchestration.md`, `task-execution-service.md`, `idempotency-keys.md`, `dispatch-circuit-breaker.md`

---

## Backend Layer

### CORS Configuration for X-Source-Agent
**File**: `src/backend/main.py`
**Line**: 273

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Source-Agent", "Accept"],
)
```

### Chat Endpoint
**File**: `src/backend/routers/chat.py`
**Lines**: 106-416

```python
@router.post("/{name}/chat")
async def chat_with_agent(
    name: str,
    request: ChatMessageRequest,
    current_user: User = Depends(get_current_user),
    x_source_agent: Optional[str] = Header(None),  # Line 111
    x_via_mcp: Optional[str] = Header(None)        # Line 112
):
    """
    Headers:
    - X-Source-Agent: Set when one agent calls another (agent-to-agent)
    - X-Via-MCP: Set for all MCP calls (both user and agent-scoped)
    """
    # Determine execution source
    if x_source_agent:
        source = ExecutionSource.AGENT  # Line 134
    else:
        source = ExecutionSource.USER
    # ...
```

### Collaboration Event Broadcasting
**File**: `src/backend/routers/chat.py`
**Lines**: 91-104

```python
async def broadcast_collaboration_event(source_agent: str, target_agent: str, action: str = "chat"):
    """Broadcast agent collaboration event to all WebSocket clients."""
    if _websocket_manager:
        event = {
            "type": "agent_collaboration",
            "source_agent": source_agent,
            "target_agent": target_agent,
            "action": action,
            "timestamp": datetime.utcnow().isoformat()
        }
        await _websocket_manager.broadcast(json.dumps(event))
```

**Called at**: Lines 189-193 (chat) and 483-487 (parallel task)

```python
# Broadcast collaboration event if this is agent-to-agent communication
if x_source_agent:
    await broadcast_collaboration_event(
        source_agent=x_source_agent,
        target_agent=name,
        action="chat"  # or "parallel_task"
    )
```

### Activity Tracking for Collaboration
**File**: `src/backend/routers/chat.py`
**Lines**: 196-210

```python
# Track agent collaboration activity
collaboration_activity_id = await activity_service.track_activity(
    agent_name=x_source_agent,  # Activity belongs to source agent
    activity_type=ActivityType.AGENT_COLLABORATION,
    user_id=current_user.id,
    triggered_by="agent",
    related_execution_id=task_execution_id,
    details={
        "source_agent": x_source_agent,
        "target_agent": name,
        "action": "chat",
        "message_preview": request.message[:100],
        "execution_id": task_execution_id,
        "queue_status": queue_result
    }
)
```

---

## Agent Layer

### Trinity MCP Injection
**File**: `docker/base-image/agent_server/services/trinity_mcp.py`
**Lines**: 15-81

```python
def inject_trinity_mcp_if_configured() -> bool:
    """Inject Trinity MCP server - runtime aware."""
    trinity_mcp_url = os.getenv("TRINITY_MCP_URL")
    trinity_mcp_api_key = os.getenv("TRINITY_MCP_API_KEY")

    if not trinity_mcp_url or not trinity_mcp_api_key:
        logger.info("Trinity MCP not configured - skipping injection")
        return False

    runtime = os.getenv("AGENT_RUNTIME", "claude-code").lower()
    if runtime == "gemini-cli":
        return _inject_gemini_mcp(trinity_mcp_url, trinity_mcp_api_key)
    else:
        return _inject_claude_mcp(trinity_mcp_url, trinity_mcp_api_key)


def _inject_claude_mcp(trinity_mcp_url: str, trinity_mcp_api_key: str) -> bool:
    """Inject Trinity MCP into Claude Code's .mcp.json file."""
    home_dir = Path("/home/developer")
    mcp_file = home_dir / ".mcp.json"

    trinity_mcp_entry = {
        "trinity": {
            "type": "http",
            "url": trinity_mcp_url,
            "headers": {
                "Authorization": f"Bearer {trinity_mcp_api_key}"
            }
        }
    }
    # Merge with existing .mcp.json and write
```

### Agent MCP Key Generation (Agent Creation)
**File**: `src/backend/services/agent_service/crud.py`
**Lines**: 270-321

```python
# Phase: Agent-to-Agent Collaboration
# Generate agent-scoped MCP API key for Trinity MCP access
agent_mcp_key = None
trinity_mcp_url = os.getenv('TRINITY_MCP_URL', 'http://mcp-server:8080/mcp')
try:
    agent_mcp_key = db.create_agent_mcp_api_key(
        agent_name=config.name,
        owner_username=current_user.username,
        description=f"Auto-generated Trinity MCP key for agent {config.name}"
    )
    if agent_mcp_key:
        logger.info(f"Created MCP API key for agent {config.name}: {agent_mcp_key.key_prefix}...")
except Exception as e:
    logger.warning(f"Failed to create MCP API key for agent {config.name}: {e}")

# ...

# Phase: Agent-to-Agent Collaboration - Inject Trinity MCP credentials
if agent_mcp_key:
    env_vars['TRINITY_MCP_URL'] = trinity_mcp_url
    env_vars['TRINITY_MCP_API_KEY'] = agent_mcp_key.api_key
```

---

## Database Operations

### MCP Key Table Schema
**File**: `src/backend/database.py`
**Lines**: 161-168

```python
"""Add agent_name and scope columns to mcp_api_keys table."""
migrations = [
    ("agent_name", "TEXT"),  # Agent name for agent-scoped keys
    ("scope", "TEXT DEFAULT 'user'")  # "user" or "agent"
]
```

### Create Agent MCP API Key
**File**: `src/backend/db/mcp_keys.py`
**Lines**: 107-161

```python
def create_agent_mcp_api_key(self, agent_name: str, owner_username: str, description: Optional[str] = None) -> Optional[McpApiKeyWithSecret]:
    """Create an agent-scoped MCP API key for agent-to-agent collaboration."""
    # Generates key with scope='agent' and agent_name set
```

### Validate MCP API Key
**File**: `src/backend/db/mcp_keys.py`
**Lines**: 190-236

Returns:
- `key_id`, `key_name`: Key identifiers
- `user_id`, `user_email`: Owner info
- `agent_name`: Agent name if scope is 'agent'
- `scope`: 'user', 'agent', or 'system'

### Backend Validation Endpoint
**File**: `src/backend/routers/mcp_keys.py`
**Lines**: 144-180

```python
@router.post("/validate")
async def validate_mcp_api_key_http_endpoint(request: Request):
    """Validate MCP API key - returns scope, agent_name, user info."""
    # Called by MCP server to validate incoming requests
```

---

## Type Definitions

### McpAuthContext
**File**: `src/mcp-server/src/types.ts`
**Lines**: 64-71

```typescript
export interface McpAuthContext extends Record<string, unknown> {
  userId: string;        // Username of the key owner
  userEmail?: string;    // Email of the key owner
  keyName: string;       // Name of the MCP API key
  agentName?: string;    // Agent name if scope is 'agent' or 'system'
  scope: "user" | "agent" | "system";
  mcpApiKey?: string;    // The actual MCP API key
}
```

### ActivityType
**File**: `src/backend/models.py`
**Line**: 135

```python
class ActivityType(str, Enum):
    AGENT_COLLABORATION = "agent_collaboration"  # Line 135
```

---

## Access Control Matrix

### For User-scoped Keys

| Caller Type | Target Agent Owner | Target is_shared | Access |
|-------------|-------------------|------------------|--------|
| User (same owner) | Same | Any | Allowed |
| User (different owner) | Different | true | Allowed |
| User (different owner) | Different | false | Denied |
| Admin user | Any | Any | Allowed |

### For Agent-scoped Keys (Phase 9.10)

| Caller Type | Target Agent | Access |
|-------------|--------------|--------|
| Agent (self) | Same agent | Allowed |
| Agent | Target in permissions list | Allowed |
| Agent | Target NOT in permissions list | Denied |

### For System-scoped Keys (Phase 11.1)

| Caller Type | Target Agent | Access |
|-------------|--------------|--------|
| System agent | Any | Allowed (bypasses ALL checks) |

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Agent not found | 404 | Agent not found |
| Agent not running | 503 | Agent is not running |
| Permission denied | 200* | Access denied (in JSON response) |
| Queue full | 429 | Agent queue is full |
| Timeout | 504 | Task execution timed out |
| Connection error | 503 | Failed to communicate with agent |

*Note: MCP tools return errors in JSON response body, not HTTP status codes.

---

## Security Considerations

1. **Agent-scoped API Keys**: Each agent has its own MCP API key (scope="agent") generated at creation
2. **Session-Based Auth Context**: FastMCP stores authentication context in session for the tool execution lifecycle
3. **Permission System**: Agent-scoped keys require explicit permissions; same-owner access is not automatic
4. **System Agent Bypass**: System-scoped keys (Phase 11.1) bypass all permission checks
5. **Audit Trail**: All collaboration events tracked via ActivityService

---

## Related Flows

- **Downstream**: Activity Monitoring - collaboration events appear in activity stream
- **Upstream**: Agent Lifecycle - MCP API keys generated on agent creation
- **Related**: MCP Orchestration - Trinity MCP server provides chat_with_agent tool and 8 schedule management tools
- **Related**: Parallel Headless Execution - Use `parallel: true` for concurrent delegation
- **Related**: Scheduling - System agents can manage schedules on all agents via MCP schedule tools

---

## System Agent Schedule Management (Added 2026-01-29)

System-scoped agents (like `trinity-system`) can manage schedules across all agents via MCP schedule tools. This enables centralized automation management.

### Available Schedule Tools

| Tool | Description |
|------|-------------|
| `list_agent_schedules` | List all schedules for any agent |
| `create_agent_schedule` | Create cron-based schedules on any agent |
| `toggle_agent_schedule` | Enable/disable schedules |
| `trigger_agent_schedule` | Manually trigger execution |
| `delete_agent_schedule` | Remove schedules |

### Example: System Agent Managing Worker Schedules

```javascript
// System agent (trinity-system) creating recurring task on worker
await mcp.call("create_agent_schedule", {
  agent_name: "daily-report-agent",
  name: "Morning Report",
  cron_expression: "0 9 * * 1-5",  // Weekdays at 9 AM
  message: "Generate and email the morning status report",
  timezone: "America/New_York",
  enabled: true
});

// Pause schedules during maintenance
await mcp.call("toggle_agent_schedule", {
  agent_name: "daily-report-agent",
  schedule_id: "abc123",
  enabled: false
});
```

See `scheduling.md` and `mcp-orchestration.md` for full details on schedule management.

---

## Design Limitation: 60-Second MCP Call Timeout

Claude Code enforces a hardcoded 60-second timeout on all MCP HTTP tool calls. This means any synchronous `chat_with_agent` call that takes longer than 60 seconds will fail — the calling agent's MCP client drops the connection regardless of the `timeout_seconds` parameter passed to Trinity.

**Impact**: The `timeout_seconds` parameter controls the backend execution timeout (how long Trinity waits for the target agent), but Claude Code kills the HTTP connection after 60s on the client side.

**Workarounds**:
1. Design tasks to complete within 60 seconds
2. Use `async=true` with `parallel=true` for long-running tasks (returns `execution_id` immediately)
3. Use shared folders for result exchange instead of synchronous return values

See [Multi-Agent System Guide](../../../docs/MULTI_AGENT_SYSTEM_GUIDE.md#design-limitation-60-second-mcp-call-timeout) for detailed patterns.

**Related**: [GitHub Issue #104](https://github.com/abilityai/trinity/issues/104), [Claude Code #16837](https://github.com/anthropics/claude-code/issues/16837)

---

## Parallel Delegation Mode

For orchestrator-worker patterns, use the `parallel` parameter:

```python
# Orchestrator sends 5 parallel tasks (no queue blocking)
for worker in ["worker-1", "worker-2", "worker-3", "worker-4", "worker-5"]:
    mcp__trinity__chat_with_agent(
        agent_name=worker,
        message="Process your assigned batch",
        parallel=true,  # Bypass queue, run stateless
        timeout_seconds=300
    )
```

**Key Benefits**:
- All workers execute concurrently (no serial queue)
- Each task runs in isolation (no conversation context)
- Orchestrator can fan-out work and collect results

**Trade-off**: Parallel mode is stateless. For multi-turn collaborative reasoning, use standard chat mode.

### Async Mode (Fire-and-Forget) - Added 2026-01-30

For non-blocking delegation where you don't need to wait for results:

```python
# Spawn long-running task, continue immediately
result = mcp__trinity__chat_with_agent(
    agent_name="analysis-agent",
    message="Analyze entire codebase",
    parallel=true,   # Required for async
    async=true,      # Return immediately
    timeout_seconds=3600
)
# Returns: { "status": "accepted", "execution_id": "abc123", ... }

# Poll later for results
# GET /api/agents/analysis-agent/executions/abc123
```

**When to use async mode**:
- Fan-out patterns where orchestrator spawns many workers
- Background jobs that run independently
- Long-running analysis that doesn't need immediate response
- Load distribution across multiple agents

See [Parallel Headless Execution](parallel-headless-execution.md) for complete async mode documentation.

---

## Revision History

| Date | Changes |
|------|---------|
| 2025-11-29 | Initial implementation |
| 2025-11-30 | Testing verified - same-owner, denied access, audit logs |
| 2025-12-02 | Auth context fix - context.session pattern |
| 2025-12-22 | Added parallel execution mode |
| 2025-12-30 | Line numbers updated |
| 2026-01-23 | Full review: verified all line numbers, added code snippets, documented CORS config, activity tracking details |
| 2026-01-29 | **MCP Schedule Management (MCP-SCHED-001)**: Added System Agent Schedule Management section - system-scoped agents can now manage schedules across all agents via 8 new MCP schedule tools. Updated Related Flows to reference scheduling.md |
| 2026-01-30 | **Async Mode**: Added async mode (fire-and-forget) section to Parallel Delegation Mode. When `async=true` with `parallel=true`, orchestrator receives `execution_id` immediately and can poll for results later. |
| 2026-06-21 | **Pull-Pilot Routing (#946)**: Added Pull-Pilot Routing section (Phase 2 PoC, Epic #1045/#1081). Flag-gated (`MCP_AGENT_CHAT_PULL_ENABLED`, default OFF) MCP routing fork — sequential agent→agent (`scope='agent'`, non-self) `chat_with_agent` routed through async `/task` with a poll-for-result receipt. Documents the D8 idempotency route token, flag plumbing, feature-flag exposure, and the T5 `/task` deny-path claim release. |
