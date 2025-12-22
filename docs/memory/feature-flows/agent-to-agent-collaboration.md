# Agent-to-Agent Collaboration via Platform MCP

**Status**: ✅ Implemented
**Date**: 2025-11-29
**Priority**: High
**Last Updated**: 2025-12-02 (Auth context fix)

---

## Problem Statement

Agents on the Trinity platform cannot communicate with each other. While the Trinity MCP server supports `chat_with_agent`, agents don't have the MCP server configured and there's no access control for inter-agent communication.

---

## Current State

### What Works
- Trinity MCP Server running (production: `http://your-server-ip:8007/mcp`)
- External clients can chat with agents via `chat_with_agent` tool
- All 12 MCP tools functional: list_agents, create_agent, start/stop, etc.

### What's Missing
| Component | Current State | Required |
|-----------|--------------|----------|
| Trinity MCP in agent `.mcp.json` | Not present | Add to templates |
| Agent MCP API Key | Not provisioned | Generate per-agent keys |
| Internal MCP URL | N/A | `http://mcp-server:8080/mcp` |
| Access Control | None | Enforce sharing rules |

### Evidence from Testing
```
Agent: git-test
- .mcp.json: DOES NOT EXIST
- .mcp.json.template: EXISTS (no Trinity MCP entry)
- credential_count: 0
- mcp__trinity__list_agents: FAILED (tool not available to agent)
```

---

## Requirements

### REQ-A2A-1: Trinity MCP Configuration in Agents

**Goal**: Agents must have Trinity MCP server configured to use platform tools.

**Implementation**:
1. Add Trinity MCP entry to `.mcp.json.template`:
```json
{
  "mcpServers": {
    "trinity": {
      "type": "http",
      "url": "${TRINITY_MCP_URL}",
      "headers": {
        "Authorization": "Bearer ${TRINITY_MCP_API_KEY}"
      }
    }
  }
}
```

2. Add required credentials to templates:
   - `TRINITY_MCP_URL` - Internal: `http://mcp-server:8080/mcp`
   - `TRINITY_MCP_API_KEY` - Agent-specific MCP API key

3. Inject during agent creation via existing credential hot-reload mechanism

---

### REQ-A2A-2: Agent-Scoped MCP API Keys

**Goal**: Each agent gets its own MCP API key that identifies it for access control.

**Current MCP Key Schema** (user-scoped):
```python
mcp_api_keys = {
    key_hash: {
        "user_id": "user-123",
        "user_email": "user@example.com",
        "key_name": "my-key"
    }
}
```

**Proposed MCP Key Schema** (agent-aware):
```python
mcp_api_keys = {
    key_hash: {
        "user_id": "user-123",
        "user_email": "user@example.com",
        "key_name": "agent-ruby-key",
        "agent_name": "ruby",           # NEW: Which agent owns this key
        "scope": "agent"                # NEW: "user" | "agent"
    }
}
```

**Key Generation Flow**:
1. Agent created → Generate agent-scoped MCP API key
2. Store key with `agent_name` and `scope: "agent"`
3. Inject key into agent's `.mcp.json` via credential system
4. Agent uses key for all Trinity MCP requests

---

### REQ-A2A-3: Access Control for Inter-Agent Communication

**Goal**: Enforce sharing rules when agents communicate with each other.

**Sharing Model** (existing):
- `owner`: User who owns the agent
- `is_shared`: Whether agent is accessible to others
- `is_owner`: Whether current user owns it

**Access Control Rules**:

| Caller Agent Owner | Target Agent | Access Granted? |
|--------------------|--------------|-----------------|
| user@x.com | owner: user@x.com | YES - Same owner |
| user@x.com | is_shared: true | YES - Shared agent |
| user@x.com | owner: other@y.com, is_shared: false | NO - Different owner, not shared |
| admin | any | YES - Admin bypass |

**Enforcement Point**: MCP Server `chat_with_agent` tool

**Implementation**:
```typescript
async function checkAgentAccess(
  client: TrinityClient,
  authContext: McpAuthContext | undefined,
  targetAgentName: string
): Promise<AgentAccessCheckResult> {
  // Extract caller identity from session auth context
  let callerOwner: string;
  if (authContext?.scope === "agent" && authContext.agentName) {
    // Agent-scoped key: get the agent's owner
    const callerAgent = await client.getAgentAccessInfo(authContext.agentName);
    callerOwner = callerAgent.owner;
  } else {
    // User-scoped key: use the user from the key
    callerOwner = authContext.userId;
  }

  // Get target agent
  const targetAgent = await client.getAgentAccessInfo(targetAgentName);

  // Enforce access rules
  if (callerOwner === targetAgent.owner) return { allowed: true };
  if (targetAgent.is_shared) return { allowed: true };
  if (callerOwner === "admin") return { allowed: true };

  return { allowed: false, reason: "Access denied" };
}
```

---

### REQ-A2A-4: Audit Logging for Inter-Agent Communication

**Goal**: Track all agent-to-agent interactions for debugging and security.

**Log Events**:
```python
{
    "event_type": "agent_collaboration",
    "action": "chat",
    "caller_agent": "ruby",
    "caller_owner": "user@example.com",
    "target_agent": "cornelius",
    "target_owner": "user@example.com",
    "result": "success" | "denied",
    "denial_reason": "different_owner_not_shared" | null,
    "timestamp": "2025-11-29T22:30:00Z"
}
```

---

## Implementation Plan

### Phase 1: Template & Credential Updates
- [x] ~~Add Trinity MCP to `.mcp.json.template` in agent templates~~ (Dynamic injection instead)
- [x] Add `TRINITY_MCP_URL` and `TRINITY_MCP_API_KEY` to env vars
- [x] Update credential injection to include Trinity MCP credentials

### Phase 2: Agent-Scoped MCP Keys
- [x] Extend MCP key schema with `agent_name` and `scope` fields
- [x] Create `create_agent_mcp_api_key()` function
- [x] Auto-generate key during agent creation
- [x] Store key association in database

### Phase 3: Access Control Enforcement
- [x] Modify MCP server to extract agent identity from API key
- [x] Implement sharing rule checks in `chat_with_agent`
- [x] Add audit logging for collaboration events (console logs)
- [x] Handle edge cases (admin bypass)

### Phase 4: Testing
- [x] 2025-11-30: Test same-owner agent communication ✅
  - Agent A (admin) → Agent B (admin): SUCCESS
  - Cost: $0.0892, Duration: 17s
  - MCP logs: `[Agent Collaboration] collab-agent-a -> collab-agent-b`
- [ ] Test shared agent access
- [x] 2025-11-30: Test denied access (different owner, not shared) ✅
  - user@example.com → admin agents: ACCESS DENIED (working correctly)
- [ ] Test admin bypass
- [x] 2025-11-30: Verify audit logs ✅
  - MCP server logs show authentication and collaboration events
  - Agent-scoped keys (`scope=agent`) authenticating properly

---

## Files Modified

| File | Changes | Status |
|------|---------|--------|
| `src/backend/routers/mcp_keys.py` | Add agent_name, scope fields | ✅ Done |
| `src/backend/routers/agents.py` | Generate agent MCP key on creation | ✅ Done |
| `src/backend/database.py` | Store agent-key associations | ✅ Done |
| `src/mcp-server/src/tools/chat.ts` | Add access control checks | ✅ Done |
| `src/mcp-server/src/server.ts` | Pass auth context to tools | ✅ Done |
| `src/mcp-server/src/types.ts` | Add McpAuthContext, AgentAccessInfo | ✅ Done |
| `src/mcp-server/src/client.ts` | Add getAgentAccessInfo() method | ✅ Done |
| `docker/base-image/agent-server.py` | Inject Trinity MCP + load MCP config | ✅ Done (2025-11-30) |

---

## Open Questions

1. **Key Rotation**: How do we handle MCP key rotation for agents?
2. **Revocation**: If an agent is deleted, do we auto-revoke its MCP key?
3. **Shared Agent Scope**: Can a shared agent initiate chats, or only receive?
4. **Rate Limiting**: Should we limit inter-agent chat frequency?
5. **Message Size**: Any limits on message size between agents?

---

## Parallel Delegation Mode (Added 2025-12-22)

For orchestrator-worker patterns where an agent delegates to multiple workers simultaneously, use the `parallel` parameter:

```python
# Orchestrator sends 5 parallel tasks (no queue blocking)
for worker in ["worker-1", "worker-2", "worker-3", "worker-4", "worker-5"]:
    mcp__trinity__chat_with_agent(
        agent_name=worker,
        message=f"Process your assigned batch",
        parallel=true,  # Bypass queue, run stateless
        timeout_seconds=300
    )
```

**Key Benefits for Collaboration**:
- All 5 workers execute concurrently (no serial queue)
- Each task runs in isolation (no conversation context pollution)
- Orchestrator can fan-out work and collect results

**Trade-off**: Parallel mode is stateless. For multi-turn collaborative reasoning, use standard chat mode.

See [Parallel Headless Execution](parallel-headless-execution.md) for full details.

---

## Agent Discovery

Agents can discover other available agents using the Trinity MCP `list_agents` tool:

```python
# Example: Agent A discovers available agents
agents = mcp__trinity__list_agents()
# Returns array of all agents with:
# - name, status, type, owner, is_shared
# - Filtered by access control rules
```

**Discovery Features**:
- `list_agents()`: Returns all accessible agents (respects ownership/sharing)
- `get_agent(name)`: Get details about specific agent
- `list_templates()`: Discover available agent templates
- Agents see `is_owner` and `is_shared` flags for access awareness

**Use Cases**:
- Agents can discover collaboration partners
- Check if specific agents are available/running
- Route tasks to appropriate specialized agents
- Dynamic team formation based on available capabilities

---

## Related Requirements

- REQ-4.1: MCP Server Integration (implemented)
- REQ-6.1: Agent Sharing (implemented)
- REQ-7.1: Audit Logging (implemented)
- REQ-9.4: Agent-to-Agent Collaboration (implemented)

---

## Feature Flow Summary

### Complete Vertical Slice

**Entry Point**: Agent A sends message to Agent B

```
Agent A Container
  └─> Claude Code executes: Use trinity_chat_with_agent tool
       └─> MCP Client calls Trinity MCP Server (http://mcp-server:8080/mcp)
            └─> MCP Server authenticates agent-scoped API key
                 └─> FastMCP authenticate() returns McpAuthContext with agentName, scope
                      └─> FastMCP stores context in session
                           └─> Tool execution receives context.session (McpAuthContext)
                                └─> Access control check (same owner or shared)
                                     └─> POST /api/agents/{agent_b}/chat with X-Source-Agent header
                                          └─> Backend detects collaboration, broadcasts WebSocket event
                                               └─> Agent B's agent-server.py receives message
                                                    └─> Claude Code processes request
                                                         └─> Response returned to Agent A
```

### Authentication Flow (Updated 2025-12-02)

**Critical Change**: Authentication context now flows via FastMCP session pattern:

1. **MCP Server Authentication** (`src/mcp-server/src/server.ts:106-139`):
   ```typescript
   authenticate: async (request) => {
     const apiKey = extractBearerToken(request);
     const result = await validateMcpApiKey(trinityApiUrl, apiKey);

     // Return McpAuthContext - FastMCP stores in session
     const authContext: McpAuthContext = {
       userId: result.user_id,
       userEmail: result.user_email,
       keyName: result.key_name,
       agentName: result.agent_name,  // Set for agent-scoped keys
       scope: result.scope as "user" | "agent",
       mcpApiKey: apiKey
     };
     return authContext;  // FastMCP stores this in session
   }
   ```

2. **Tool Execution Context** (`src/mcp-server/src/tools/chat.ts:100-112`):
   ```typescript
   execute: async ({ agent_name, message }, context: any) => {
     // Get auth context from FastMCP session (NOT getAuthContext)
     const authContext = requireApiKey ? context?.session : undefined;

     // Access control uses session data
     const accessCheck = await checkAgentAccess(client, authContext, agent_name);
   }
   ```

3. **Access Control** (`src/mcp-server/src/tools/chat.ts:21-73`):
   ```typescript
   async function checkAgentAccess(
     client: TrinityClient,
     authContext: McpAuthContext | undefined,  // From context.session
     targetAgentName: string
   ): Promise<AgentAccessCheckResult> {
     // Extract caller identity
     if (authContext?.scope === "agent" && authContext.agentName) {
       const callerAgent = await client.getAgentAccessInfo(authContext.agentName);
       callerOwner = callerAgent.owner;
     }
     // ... check access rules
   }
   ```

**Key Insight**: The `context.session` pattern ensures caller identity (agent name, scope) is preserved across the entire tool execution chain. This fix (2025-12-02) resolved issues where auth context was undefined, breaking access control.

### Key Implementation Files

| Layer | File | Key Lines | Purpose |
|-------|------|-----------|---------|
| **Agent Layer** | `docker/base-image/agent-server.py` | 420-470 | Trinity MCP injection, .mcp.json loading |
| **MCP Auth** | `src/mcp-server/src/server.ts` | 106-139 | FastMCP authenticate callback, returns McpAuthContext |
| **MCP Tools** | `src/mcp-server/src/tools/chat.ts` | 111 | Gets auth from `context.session` |
| **Access Control** | `src/mcp-server/src/tools/chat.ts` | 21-73 | checkAgentAccess using session context |
| **Types** | `src/mcp-server/src/types.ts` | 64-71 | McpAuthContext interface |
| **Client** | `src/mcp-server/src/client.ts` | 152-162 | getAgentAccessInfo method |
| **Backend** | `src/backend/routers/mcp_keys.py` | 1-200 | Agent-scoped MCP API key generation |
| **Backend** | `src/backend/routers/chat.py` | 29-42, 59-66 | Collaboration event broadcasting |
| **Database** | `src/backend/database.py` | 1-100 | MCP key storage with agent_name field |

### Access Control Matrix

| Caller Type | Target Agent Owner | Target is_shared | Access |
|-------------|-------------------|------------------|---------|
| Agent (same owner) | Same | Any | ✅ Allowed |
| Agent (different owner) | Different | true | ✅ Allowed |
| Agent (different owner) | Different | false | ❌ Denied |
| Admin agent/user | Any | Any | ✅ Allowed |

### WebSocket Events

Collaboration events broadcast to all connected clients:
```json
{
  "type": "agent_collaboration",
  "source_agent": "agent-a",
  "target_agent": "agent-b",
  "action": "chat",
  "timestamp": "2025-12-02T10:30:00Z"
}
```

Visualized in real-time on the [Collaboration Dashboard](collaboration-dashboard.md).

---

## Security Considerations

1. **Session-Based Auth Context**: FastMCP stores authentication context in session, ensuring it's available throughout tool execution lifecycle
2. **Agent Identity Verification**: Agent-scoped API keys (`scope: "agent"`) are validated against backend database before accepting collaboration requests
3. **Owner-Based Access Control**: Access decisions based on agent ownership, not just API key validity
4. **Admin Bypass Safeguards**: Admin users can access any agent for operational purposes
5. **Audit Logging**: All collaboration attempts (successful and denied) logged via console for debugging

---

## Related Flows

- **Downstream**: [Collaboration Dashboard](collaboration-dashboard.md) - Real-time visualization of agent interactions
- **Upstream**: [MCP Orchestration](mcp-orchestration.md) - Trinity MCP server provides chat_with_agent tool (shares auth pattern)
- **Related**: [Agent Lifecycle](agent-lifecycle.md) - MCP API keys generated on agent creation
- **Related**: [Parallel Headless Execution](parallel-headless-execution.md) - Use `parallel: true` for concurrent delegation (Added 2025-12-22)

---

## Status
✅ **Implemented and Tested** - Agent-to-agent collaboration working as of 2025-11-30
- Same-owner communication: ✅ Verified
- Access denial (different owner, not shared): ✅ Verified
- Audit logging: ✅ Verified
- Authentication context fix: ✅ Updated 2025-12-02 (context.session pattern)
- All line numbers accurate as of 2025-12-02
