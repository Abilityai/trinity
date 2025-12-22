# Feature Flow: Parallel Headless Execution

> **Requirement**: 12.1 - Parallel Headless Execution
> **Status**: Implemented
> **Created**: 2025-12-22

## Overview

This feature enables Trinity agents to execute multiple independent tasks in parallel without blocking the main conversation context. It unlocks agent-to-agent orchestration patterns where an orchestrator can delegate to multiple workers simultaneously.

## Problem Statement

**Prior Limitation**: Trinity agents could only process one request at a time due to:
1. Platform-level execution queue (Redis) that serializes requests
2. Container-level execution lock (`asyncio.Lock`)
3. Use of `--continue` flag which requires sequential execution to maintain conversation context

**Solution**: New parallel task mode that bypasses queue and lock, runs stateless (no --continue).

## Two Execution Modes

| Mode | Endpoint | Context | Parallel | Use Case |
|------|----------|---------|----------|----------|
| **Sequential Chat** | `POST /api/agents/{name}/chat` | Maintains conversation | No | Interactive chat, multi-turn reasoning |
| **Parallel Task** | `POST /api/agents/{name}/task` | Stateless | Yes | Agent delegation, batch processing |

## Architecture Diagram

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

## Data Flow

### 1. MCP Tool Call (Orchestrator Agent)

```
Orchestrator Agent
       │
       ▼
┌─────────────────────────────────────────────┐
│ chat_with_agent(                            │
│   agent_name: "worker-1",                   │
│   message: "Process file X",                │
│   parallel: true,                           │
│   timeout_seconds: 300                      │
│ )                                           │
└─────────────────────────────────────────────┘
       │
       ▼
   MCP Server (src/mcp-server)
       │
       ▼
   client.task(name, message, options)
       │
       ▼
   POST /api/agents/{name}/task
```

### 2. Backend Processing

```
POST /api/agents/{name}/task
       │
       ▼
┌─────────────────────────────────────────────┐
│ 1. Validate agent exists and is running     │
│ 2. Check access permissions                 │
│ 3. Track activity (ActivityType.CHAT_START  │
│    with parallel_mode: true)                │
│ 4. Build payload with options               │
│ 5. Proxy to agent container                 │
│    (NO EXECUTION QUEUE)                     │
└─────────────────────────────────────────────┘
       │
       ▼
   POST http://agent-{name}:8000/api/task
```

### 3. Agent Container Execution

```
POST /api/task
       │
       ▼
┌─────────────────────────────────────────────┐
│ execute_headless_task()                     │
│                                             │
│ - NO execution lock acquired                │
│ - Build command:                            │
│   claude -p --output-format stream-json     │
│   --verbose --dangerously-skip-permissions  │
│   [--model X] [--allowedTools Y]            │
│   [--append-system-prompt Z]                │
│   [--mcp-config ~/.mcp.json]                │
│                                             │
│ - NO --continue flag (stateless)            │
│ - Parse streaming JSON output               │
│ - Return response with session_id           │
└─────────────────────────────────────────────┘
```

## Key Files

### Agent Server (docker/base-image/agent_server/)

| File | Purpose |
|------|---------|
| `models.py` | ParallelTaskRequest, ParallelTaskResponse models |
| `services/claude_code.py` | execute_headless_task() function |
| `routers/chat.py` | POST /api/task endpoint |

### Backend (src/backend/)

| File | Purpose |
|------|---------|
| `models.py` | ParallelTaskRequest model |
| `routers/chat.py` | POST /api/agents/{name}/task endpoint |

### MCP Server (src/mcp-server/)

| File | Purpose |
|------|---------|
| `src/client.ts` | task() method for API calls |
| `src/tools/chat.ts` | chat_with_agent tool with parallel parameter |

## API Specifications

### Agent Server: POST /api/task

**Request**:
```json
{
  "message": "string",           // Required: Task to execute
  "model": "sonnet|opus|haiku",  // Optional: Model override
  "allowed_tools": ["Read"],     // Optional: Tool restrictions
  "system_prompt": "string",     // Optional: Additional instructions
  "timeout_seconds": 300         // Optional: Timeout (default 5 min)
}
```

**Response**:
```json
{
  "response": "string",          // Claude's response
  "execution_log": [...],        // Tool calls and results
  "metadata": {
    "cost_usd": 0.01,
    "duration_ms": 5000,
    "input_tokens": 1000,
    "output_tokens": 500,
    "tool_count": 3
  },
  "session_id": "uuid",          // Unique per task
  "timestamp": "2025-12-22T..."
}
```

### Backend: POST /api/agents/{name}/task

Same request/response as agent server, with additional:
- Authentication via JWT token
- Access control validation
- Activity tracking
- Audit logging

### MCP Tool: chat_with_agent

**Parameters**:
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| agent_name | string | required | Target agent |
| message | string | required | Task to execute |
| parallel | boolean | false | Enable parallel mode |
| model | string | null | Model override (parallel only) |
| allowed_tools | string[] | null | Tool restrictions (parallel only) |
| system_prompt | string | null | Additional instructions (parallel only) |
| timeout_seconds | number | 300 | Timeout in seconds (parallel only) |

## Key Differences: Chat vs Task

| Aspect | Chat (Sequential) | Task (Parallel) |
|--------|-------------------|-----------------|
| Endpoint | POST /api/chat | POST /api/task |
| Execution Queue | Yes (Redis) | No |
| Execution Lock | Yes (asyncio.Lock) | No |
| --continue flag | Yes | No |
| Conversation context | Maintained | Stateless |
| Session updates | Yes | No |
| Concurrent requests | 1 per agent | N per agent |
| Use case | Interactive chat | Batch processing |

## Testing

Tests are in `tests/test_parallel_task.py`:

| Test Class | Purpose |
|------------|---------|
| TestParallelTaskEndpoint | Endpoint existence, 404/503 handling |
| TestParallelTaskResponse | Response format validation |
| TestParallelTaskOptions | Model, timeout parameter handling |
| TestParallelTaskStateless | Verifies no context pollution |
| TestParallelExecution | Multiple concurrent tasks |

Run tests:
```bash
cd tests && pytest test_parallel_task.py -v
```

## Use Cases

### 1. Orchestrator-Worker Pattern
```
Orchestrator spawns 5 parallel workers:
  ├─► Worker 1: "Process file A" (parallel=true)
  ├─► Worker 2: "Process file B" (parallel=true)
  ├─► Worker 3: "Process file C" (parallel=true)
  ├─► Worker 4: "Process file D" (parallel=true)
  └─► Worker 5: "Process file E" (parallel=true)

All 5 execute concurrently, no queue blocking.
```

### 2. Batch Processing
```
System Agent runs nightly batch:
  for each agent in fleet:
    chat_with_agent(agent, "Run health check", parallel=true)
```

### 3. Research Tasks
```
Orchestrator gathers info from multiple sources:
  ├─► Research Agent 1: "Find info about X" (parallel=true)
  ├─► Research Agent 2: "Find info about Y" (parallel=true)
  └─► Research Agent 3: "Find info about Z" (parallel=true)

Combine results after all complete.
```

## Security Considerations

1. **Access Control**: Same rules as chat - owner, shared, or admin
2. **Rate Limiting**: Consider implementing concurrency limits
3. **Audit Trail**: All parallel tasks logged with user attribution
4. **Tool Restrictions**: `allowed_tools` parameter for sandboxing
5. **Timeout**: Hard limit prevents runaway tasks

## Future Enhancements

1. **Concurrency Limits**: Agent-level (default 5), platform-level (default 50)
2. **Activity Type**: New `parallel_task` activity type (currently reuses CHAT_START)
3. **Session Persistence**: Optional session persistence with TTL for resume
4. **Batch API**: `POST /api/agents/{name}/batch` for multiple tasks
5. **Priority Queue**: Priority parameter for urgent tasks
