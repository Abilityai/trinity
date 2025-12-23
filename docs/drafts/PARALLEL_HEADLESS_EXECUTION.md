# Parallel Headless Execution

> **Status**: Implemented
> **Created**: 2025-12-22
> **Priority**: High
> **Requirement ID**: 12.1

## Overview

Enable Trinity agents to execute multiple independent tasks in parallel without blocking the main conversation context. This unlocks agent-to-agent orchestration patterns where an orchestrator can delegate to multiple workers simultaneously.

## Problem Statement

**Current Limitation**: Trinity agents can only process one request at a time due to:
1. Platform-level execution queue (Redis) that serializes requests
2. Container-level execution lock (`asyncio.Lock`)
3. Use of `--continue` flag which requires sequential execution to maintain conversation context

**Impact**:
- Orchestrator agents cannot delegate to multiple workers in parallel
- Batch processing requires sequential execution
- Agent-to-agent collaboration is bottlenecked

## Research: Claude Code Parallel Execution

Based on official Claude Code documentation analysis:

### Headless Mode
```bash
claude -p "task description" --output-format stream-json --verbose
```
- Runs non-interactively, returns structured JSON
- Each invocation is independent (no shared state)
- Can run N instances in parallel
- No `--continue` flag = no conversation memory

### Key CLI Flags for Headless
| Flag | Purpose |
|------|---------|
| `-p, --print` | Non-interactive mode |
| `--output-format stream-json` | Structured streaming output |
| `--verbose` | Detailed execution logging |
| `--allowedTools` | Restrict available tools |
| `--mcp-config` | Load MCP servers |
| `--append-system-prompt` | Add custom instructions |

### Session Management
- Each headless call can return a `session_id`
- Sessions can be resumed with `--resume <session_id>`
- Useful for multi-turn workflows that need to be paused/resumed

## Proposed Solution

### Two Execution Modes

| Mode | Endpoint | Context | Parallel | Use Case |
|------|----------|---------|----------|----------|
| **Sequential Chat** | `POST /api/agents/{name}/chat` | Maintains conversation | No | Interactive chat, multi-turn reasoning |
| **Parallel Task** | `POST /api/agents/{name}/task` | Stateless | Yes | Agent delegation, batch processing |

### Architecture

```
                                    ┌─────────────────────────────┐
                                    │     Sequential Chat         │
User/Agent ──► POST /chat ─────────►│  Execution Queue (Redis)    │──► claude --continue
                                    │  One at a time              │
                                    └─────────────────────────────┘

                                    ┌─────────────────────────────┐
                                    │     Parallel Task           │
User/Agent ──► POST /task ─────────►│  No queue, no lock          │──► claude -p (headless)
              (can send N)          │  N concurrent allowed       │    (N processes)
                                    └─────────────────────────────┘
```

## Functional Requirements

### 12.1.1 Agent Server: Parallel Task Endpoint
- **Status**: ⏳ Not Started
- **Priority**: High
- **Description**: New endpoint in agent-server for stateless task execution

**Acceptance Criteria**:
- [ ] `POST /api/task` endpoint in agent-server
- [ ] Does NOT acquire execution lock (parallel allowed)
- [ ] Does NOT use `--continue` flag (stateless)
- [ ] Each call gets unique session ID
- [ ] Returns response, execution_log, metadata (same schema as /chat)
- [ ] Loads MCP config if present (agent can use Trinity MCP tools)
- [ ] Supports `--allowedTools` restriction via request parameter
- [ ] Supports `--append-system-prompt` via request parameter
- [ ] Timeout configurable (default 5 minutes)

**Request Schema**:
```python
class TaskRequest(BaseModel):
    message: str                          # The task to execute
    model: Optional[str] = None           # Model override (sonnet, opus, haiku)
    allowed_tools: Optional[List[str]] = None  # Tool restrictions
    system_prompt: Optional[str] = None   # Additional instructions
    timeout_seconds: Optional[int] = 300  # Execution timeout
```

**Response Schema**:
```python
class TaskResponse(BaseModel):
    response: str                         # Claude's response
    execution_log: List[ExecutionLogEntry]  # Tool calls
    metadata: ExecutionMetadata           # Cost, tokens, duration
    session_id: str                       # Unique session ID
    timestamp: str                        # ISO timestamp
```

### 12.1.2 Backend: Parallel Task Proxy
- **Status**: ⏳ Not Started
- **Priority**: High
- **Description**: Backend endpoint that proxies to agent without execution queue

**Acceptance Criteria**:
- [ ] `POST /api/agents/{name}/task` endpoint in backend
- [ ] Does NOT use execution queue (bypass for parallel)
- [ ] Validates agent exists and is running
- [ ] Validates user has access (owner, shared, or admin)
- [ ] Proxies request to agent's `/api/task` endpoint
- [ ] Returns agent response directly
- [ ] Audit logging for task execution
- [ ] WebSocket broadcast for task events (optional)

**Request Schema** (mirrors agent-server):
```python
class AgentTaskRequest(BaseModel):
    message: str
    model: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    system_prompt: Optional[str] = None
    timeout_seconds: Optional[int] = 300
```

### 12.1.3 MCP Tool: Parallel Chat Option
- **Status**: ⏳ Not Started
- **Priority**: High
- **Description**: Update `chat_with_agent` MCP tool to support parallel mode

**Acceptance Criteria**:
- [ ] Add `parallel: boolean` parameter (default: false)
- [ ] When `parallel=false`: Use existing `/chat` endpoint (sequential)
- [ ] When `parallel=true`: Use new `/task` endpoint (parallel)
- [ ] Add `allowed_tools` parameter for tool restrictions
- [ ] Add `system_prompt` parameter for custom instructions
- [ ] Update tool description to explain parallel vs sequential

**Updated Tool Schema**:
```typescript
{
  name: "chat_with_agent",
  description: "Send a message to an agent. Use parallel=true for independent tasks that don't need conversation history.",
  parameters: {
    agent_name: { type: "string", required: true },
    message: { type: "string", required: true },
    parallel: { type: "boolean", default: false },
    allowed_tools: { type: "array", items: { type: "string" } },
    system_prompt: { type: "string" }
  }
}
```

### 12.1.4 Concurrency Limits
- **Status**: ⏳ Not Started
- **Priority**: Medium
- **Description**: Configurable limits on parallel task execution

**Acceptance Criteria**:
- [ ] Agent-level concurrency limit (default: 5 parallel tasks)
- [ ] Configurable via template.yaml `resources.max_parallel_tasks`
- [ ] Platform-level global limit (default: 50 total parallel tasks)
- [ ] Configurable via system settings `ops_max_parallel_tasks`
- [ ] Return 429 Too Many Requests when limit exceeded
- [ ] Include `Retry-After` header with estimated wait time

### 12.1.5 Activity Tracking for Parallel Tasks
- **Status**: ⏳ Not Started
- **Priority**: Medium
- **Description**: Track parallel task execution in activity stream

**Acceptance Criteria**:
- [ ] New activity type: `parallel_task`
- [ ] Track: agent_name, session_id, start_time, end_time, status
- [ ] Link to parent activity if called from another agent
- [ ] WebSocket broadcast for parallel task events
- [ ] Visible in Dashboard activity timeline
- [ ] Query API supports filtering by activity type

## Implementation Plan

### Phase 1: Agent Server (Core)
1. Create `TaskRequest` and `TaskResponse` models
2. Implement `execute_headless_task()` function (no lock, no --continue)
3. Add `POST /api/task` endpoint
4. Add unit tests

### Phase 2: Backend Proxy
1. Add `POST /api/agents/{name}/task` endpoint
2. Implement agent proxy (no execution queue)
3. Add access control validation
4. Add audit logging
5. Add integration tests

### Phase 3: MCP Integration
1. Update `chat_with_agent` tool with `parallel` parameter
2. Route to appropriate endpoint based on parameter
3. Update tool documentation
4. Test agent-to-agent parallel delegation

### Phase 4: Concurrency & Observability
1. Implement concurrency limits
2. Add activity tracking
3. Add WebSocket broadcasts
4. Update Dashboard with parallel task visibility

## Testing Requirements

### Unit Tests
- [ ] `execute_headless_task()` runs without lock
- [ ] Multiple parallel calls don't block each other
- [ ] Session IDs are unique per call
- [ ] Tool restrictions work via `--allowedTools`
- [ ] Timeout kills long-running tasks

### Integration Tests
- [ ] Backend proxy forwards to agent correctly
- [ ] Access control enforced (owner, shared, admin)
- [ ] Audit logging captures parallel tasks
- [ ] Concurrency limits enforce 429 response

### E2E Tests
- [ ] MCP `chat_with_agent` with `parallel=true` works
- [ ] Orchestrator can spawn 5 parallel worker tasks
- [ ] Parallel tasks complete independently
- [ ] Main conversation context unaffected by parallel tasks

### Performance Tests
- [ ] Verify N parallel tasks run concurrently (not sequentially)
- [ ] Measure latency vs sequential execution
- [ ] Verify memory usage with N concurrent processes

## Security Considerations

1. **Access Control**: Same rules as chat - owner, shared, or admin
2. **Rate Limiting**: Concurrency limits prevent resource exhaustion
3. **Audit Trail**: All parallel tasks logged with user attribution
4. **Tool Restrictions**: `allowed_tools` parameter for sandboxing
5. **Timeout**: Hard limit prevents runaway tasks

## Cost Considerations

- Parallel tasks consume API tokens simultaneously
- N parallel tasks = N× rate limit consumption
- Consider cost warnings in documentation
- Optional: Add cost estimation before parallel batch

## Migration & Compatibility

- **Backward Compatible**: Existing `/chat` endpoint unchanged
- **Opt-in**: Parallel mode requires explicit `parallel=true`
- **No Breaking Changes**: All existing integrations continue to work

## Related Requirements

- **9.4 Agent-to-Agent Collaboration**: Parallel tasks enable true multi-agent orchestration
- **9.7 Unified Activity Stream**: Track parallel task activities
- **11.2 System Agent Operations**: System agent can spawn parallel health checks

## Open Questions

1. **Session Persistence**: Should parallel task sessions be persisted for resume?
   - Current proposal: No, tasks are stateless
   - Alternative: Optional session persistence with TTL

2. **Result Aggregation**: Should platform provide batch API?
   - Current proposal: No, caller handles aggregation
   - Alternative: `POST /api/agents/{name}/batch` for multiple tasks

3. **Priority Queue**: Should parallel tasks have priority levels?
   - Current proposal: No, first-come-first-served
   - Alternative: Priority parameter for urgent tasks

## References

- Claude Code Headless Mode: https://docs.anthropic.com/en/docs/claude-code/headless
- Claude Agent SDK: https://docs.anthropic.com/en/docs/agent-sdk/overview
- Trinity Execution Queue: `docs/memory/feature-flows/execution-queue.md`
- Trinity MCP Orchestration: `docs/memory/feature-flows/mcp-orchestration.md`
