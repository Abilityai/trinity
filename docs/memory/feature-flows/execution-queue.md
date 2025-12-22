# Feature: Execution Queue System

## Overview
The Execution Queue System prevents parallel execution on agents by serializing all execution requests through a Redis-backed queue. This ensures only one execution runs per agent at a time, protecting Claude Code's conversation state from corruption.

## User Story
As a Trinity platform operator, I want all agent execution requests (user chat, scheduled tasks, agent-to-agent calls) to be serialized so that Claude Code's conversation context remains consistent and uncorrupted.

## Entry Points
- **User Chat**: `POST /api/agents/{name}/chat` - User messages from UI
- **Scheduler**: `scheduler_service._execute_schedule()` - Cron-triggered executions
- **MCP Server**: `chat_with_agent` tool - Agent-to-agent communication
- **Queue Management**: `GET/POST /api/agents/{name}/queue/*` - Status, clear, release

---

## Problem Statement

Claude Code maintains conversation state in memory. When multiple execution requests arrive simultaneously, they can corrupt the conversation state and produce unpredictable results.

### Failure Scenarios Before Queue

1. **User + Schedule Collision**: User sends message while schedule fires -> both execute -> corrupted context
2. **Agent-to-Agent + User**: Orchestrator calls agent while user is chatting -> race condition
3. **Multiple Schedules**: Two cron jobs trigger at same minute -> chaos

### Queue Bypass: Parallel Task Execution (Added 2025-12-22)

For stateless, parallel workloads, the queue can be bypassed using `POST /api/agents/{name}/task`:
- **No queue serialization** - Requests execute immediately
- **No --continue flag** - Each task runs in isolation (no conversation context)
- **Multiple concurrent tasks** - N tasks can run simultaneously per agent

See [Parallel Headless Execution](parallel-headless-execution.md) for details.

**When to use /task vs /chat**:
| Use `/chat` | Use `/task` |
|-------------|-------------|
| Multi-turn conversations | One-shot tasks |
| Context continuity needed | Stateless operations |
| User interactive chat | Agent-to-agent delegation |
| Sequential reasoning | Batch processing |

---

## Solution Architecture

```
+-------------------------------------------------------------+
|                    TRINITY BACKEND                           |
+-------------------------------------------------------------+
|                                                              |
|  +-----------+   +-----------+   +-----------+              |
|  | User Chat |   | Scheduler |   | MCP Server|              |
|  +-----+-----+   +-----+-----+   +-----+-----+              |
|        |               |               |                     |
|        v               v               v                     |
|  +-----------------------------------------------------+    |
|  |              EXECUTION QUEUE (Redis)                 |    |
|  |  +------------------------------------------------+ |    |
|  |  | Per-Agent Queue                                | |    |
|  |  |                                                | |    |
|  |  |  Running Key: agent:running:{name}             | |    |
|  |  |  Queue Key:   agent:queue:{name} (List)        | |    |
|  |  |                                                | |    |
|  |  |  +---------+  +---------+  +---------+        | |    |
|  |  |  |Running  |  |Queued 1 |  |Queued 2 |  ...   | |    |
|  |  |  +---------+  +---------+  +---------+        | |    |
|  |  +------------------------------------------------+ |    |
|  +-----------------------------------------------------+    |
|                          |                                   |
|                          v                                   |
|  +-----------------------------------------------------+    |
|  |              AGENT CONTAINER                         |    |
|  |  +-----------------------------------------------+  |    |
|  |  | asyncio.Lock (defense-in-depth)               |  |    |
|  |  | ThreadPoolExecutor(max_workers=1)             |  |    |
|  |  +-----------------------------------------------+  |    |
|  +-----------------------------------------------------+    |
|                                                              |
+-------------------------------------------------------------+
```

---

## Data Models (`src/backend/models.py:163-210`)

### ExecutionSource (Enum)
```python
class ExecutionSource(str, Enum):
    """Source of an execution request."""
    USER = "user"       # User chat via UI
    SCHEDULE = "schedule"  # Scheduled task
    AGENT = "agent"     # Agent-to-agent via MCP
```

### ExecutionStatus (Enum)
```python
class ExecutionStatus(str, Enum):
    """Status of an execution request."""
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
```

### Execution (Model)
```python
class Execution(BaseModel):
    """Represents an execution request in the agent queue."""
    id: str                                    # UUID
    agent_name: str
    source: ExecutionSource
    source_agent: Optional[str] = None         # If source == AGENT
    source_user_id: Optional[str] = None       # User who triggered
    source_user_email: Optional[str] = None    # User email for tracking
    message: str                               # The chat message
    queued_at: datetime
    started_at: Optional[datetime] = None
    status: ExecutionStatus = ExecutionStatus.QUEUED
```

### QueueStatus (Model)
```python
class QueueStatus(BaseModel):
    """Status of an agent's execution queue."""
    agent_name: str
    is_busy: bool
    current_execution: Optional[Execution] = None
    queue_length: int
    queued_executions: List[Execution] = []
```

---

## Execution Queue Service (`src/backend/services/execution_queue.py`)

### Configuration (lines 32-34)
```python
MAX_QUEUE_SIZE = 3           # Max queued requests per agent
EXECUTION_TTL = 600          # 10 minutes max execution time (Redis TTL)
QUEUE_WAIT_TIMEOUT = 120     # 120 seconds max wait in queue
```

### Redis Key Structure
| Key Pattern | Type | Purpose |
|-------------|------|---------|
| `agent:running:{name}` | String | Currently running execution (JSON) |
| `agent:queue:{name}` | List | Waiting executions (FIFO via LPUSH/RPOP) |

### Core Methods

#### create_execution() (lines 82-102)
Creates an execution request object (not yet submitted to queue):
```python
def create_execution(
    self,
    agent_name: str,
    message: str,
    source: ExecutionSource,
    source_agent: Optional[str] = None,
    source_user_id: Optional[str] = None,
    source_user_email: Optional[str] = None
) -> Execution
```

#### submit() (lines 104-153)
Submits execution to queue. Returns status and execution object.
```python
async def submit(
    self,
    execution: Execution,
    wait_if_busy: bool = True
) -> tuple[str, Execution]:
    """
    Returns:
        ("running", execution) - Started immediately
        ("queued:N", execution) - Queued at position N

    Raises:
        QueueFullError - Queue at MAX_QUEUE_SIZE
        AgentBusyError - Agent busy and wait_if_busy=False
    """
```

**Flow:**
1. Check if agent is free (Redis key empty)
2. If free: set key, return "running"
3. If busy and wait_if_busy=True: check queue length
4. If queue < MAX_QUEUE_SIZE: LPUSH to queue, return "queued:N"
5. If queue full: raise QueueFullError

#### complete() (lines 155-189)
Marks current execution done and starts next if queued:
```python
async def complete(self, agent_name: str, success: bool = True) -> Optional[Execution]:
    """
    Returns:
        Next execution that was started (if any), or None
    """
```

**Flow:**
1. Get and log completed execution
2. RPOP next from queue (FIFO)
3. If next exists: update status to RUNNING, set key
4. If queue empty: delete running key (agent is idle)

#### get_status() (lines 191-212)
Returns current queue status:
```python
async def get_status(self, agent_name: str) -> QueueStatus
```

#### is_busy() (lines 214-217)
Quick check if agent is executing:
```python
async def is_busy(self, agent_name: str) -> bool
```

#### clear_queue() (lines 219-230)
Clears all queued executions (not current):
```python
async def clear_queue(self, agent_name: str) -> int
```

#### force_release() (lines 232-244)
Emergency: clears running state for stuck agents:
```python
async def force_release(self, agent_name: str) -> bool
```

### Custom Exceptions

```python
class QueueFullError(Exception):
    """Raised when an agent's queue is full."""
    def __init__(self, agent_name: str, queue_length: int)

class AgentBusyError(Exception):
    """Raised when an agent is busy and caller doesn't want to wait."""
    def __init__(self, agent_name: str, current_execution: Optional[Execution] = None)
```

---

## Integration Points

### 1. User Chat (`src/backend/routers/chat.py:50-293`)

**Entry Point**: `POST /api/agents/{name}/chat`

```python
@router.post("/{name}/chat")
async def chat_with_agent(
    name: str,
    request: ChatMessageRequest,
    current_user: User = Depends(get_current_user),
    x_source_agent: Optional[str] = Header(None)
):
    # Determine source
    source = ExecutionSource.AGENT if x_source_agent else ExecutionSource.USER

    # Create and submit execution request
    queue = get_execution_queue()
    execution = queue.create_execution(
        agent_name=name,
        message=request.message,
        source=source,
        source_agent=x_source_agent,
        source_user_id=str(current_user.id),
        source_user_email=current_user.email or current_user.username
    )

    try:
        queue_result, execution = await queue.submit(execution, wait_if_busy=True)
        logger.info(f"[Chat] Agent '{name}' execution {execution.id}: {queue_result}")
    except QueueFullError as e:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "Agent queue is full",
                "agent": name,
                "queue_length": e.queue_length,
                "retry_after": 30,
                "message": f"Agent '{name}' is busy with {e.queue_length} queued requests."
            }
        )

    # ... execute chat, track activities ...

    finally:
        # ALWAYS release queue slot when done
        await queue.complete(name, success=execution_success)
```

**Key Points:**
- Creates execution with source tracking (USER/AGENT)
- Handles 429 on queue full
- Always releases queue in `finally` block
- Adds execution metadata to response

### 2. Scheduler (`src/backend/services/scheduler_service.py:169-399`)

**Entry Point**: APScheduler cron trigger -> `_execute_schedule()`

```python
async def _execute_schedule(self, schedule_id: str):
    # Create queue execution request
    queue = get_execution_queue()
    queue_execution = queue.create_execution(
        agent_name=schedule.agent_name,
        message=schedule.message,
        source=ExecutionSource.SCHEDULE,
        source_user_id=str(schedule.owner_id) if schedule.owner_id else None
    )

    try:
        queue_result, queue_execution = await queue.submit(queue_execution, wait_if_busy=True)
        logger.info(f"[Schedule] Agent '{schedule.agent_name}' execution {queue_execution.id}: {queue_result}")
    except QueueFullError as e:
        error_msg = f"Agent queue full ({e.queue_length} waiting), skipping scheduled execution"
        db.update_execution_status(execution_id=execution.id, status="failed", error=error_msg)
        return

    # ... execute via httpx to agent, track in database ...

    finally:
        # Always release the queue slot when done
        await queue.complete(schedule.agent_name, success=execution_success)
```

**Key Points:**
- Uses `ExecutionSource.SCHEDULE`
- Logs queue full as failure (doesn't retry)
- Always releases queue in `finally`

### 3. MCP Server (`src/mcp-server/src/tools/chat.ts:137-189`)

**Entry Point**: `chat_with_agent` MCP tool

```typescript
execute: async ({ agent_name, message }, context) => {
    const sourceAgent = authContext?.scope === "agent" ? authContext.agentName : undefined;
    const response = await apiClient.chat(agent_name, message, sourceAgent);

    // Check if response is a queue status (agent busy)
    if ('queue_status' in response) {
        console.log(`[Queue Full] Agent '${agent_name}' is busy, queue is full`);
        return JSON.stringify({
            status: "agent_busy",
            agent: agent_name,
            queue_status: response.queue_status,
            retry_after_seconds: response.retry_after,
            message: `Agent '${agent_name}' is currently busy. The execution queue is full. ` +
                `Please wait ${response.retry_after} seconds before retrying, or try a different agent.`,
            details: response.details,
        }, null, 2);
    }

    return JSON.stringify(response, null, 2);
}
```

**Trinity Client 429 Handling** (`src/mcp-server/src/client.ts:288-319`):
```typescript
async chat(name: string, message: string, sourceAgent?: string) {
    const response = await fetch(`${this.baseUrl}/api/agents/${name}/chat`, { ... });

    // Handle 429 Too Many Requests (agent queue full)
    if (response.status === 429) {
        let details = {};
        try { details = await response.json(); } catch {}
        return {
            error: "Agent is busy",
            queue_status: "queue_full",
            retry_after: (details.retry_after as number) || 30,
            agent: name,
            details,
        };
    }
}
```

**Key Points:**
- Passes `sourceAgent` as `X-Source-Agent` header
- Gracefully handles 429 with retry guidance
- Returns structured busy response to calling agent

### 4. Agent Container Defense-in-Depth

> **Architecture Change (2025-12-06)**: The agent-server has been refactored from a monolithic file into a modular package structure at `docker/base-image/agent_server/`.

**Files**:
- `docker/base-image/agent_server/services/claude_code.py:23-30` - Execution lock and ThreadPoolExecutor
- `docker/base-image/agent_server/routers/chat.py:18-80` - Chat endpoint with lock

**Defense Layer**: asyncio.Lock + ThreadPoolExecutor(max_workers=1)

```python
# agent_server/services/claude_code.py:23-30
# Thread pool for running blocking subprocess operations
# max_workers=1 ensures only one execution at a time within this container
_executor = ThreadPoolExecutor(max_workers=1)

# Asyncio lock for execution serialization (safety net for parallel request prevention)
_execution_lock = asyncio.Lock()

# agent_server/routers/chat.py:18-80
@router.post("/api/chat")
async def chat(request: ChatRequest):
    """
    This endpoint uses an asyncio lock to ensure only one execution happens
    at a time. This is a safety net - the platform-level execution queue
    should prevent parallel requests, but this provides defense-in-depth.
    """
    async with get_execution_lock():
        logger.info(f"[Chat] Execution lock acquired for message: {request.message[:50]}...")

        # Execute Claude Code
        response_text, execution_log, metadata = await execute_claude_code(
            request.message,
            stream=request.stream,
            model=request.model
        )

        logger.info(f"[Chat] Execution lock releasing after completion")

        return { ... }
```

**Key Points:**
- `asyncio.Lock` - single execution at a time within container
- `ThreadPoolExecutor(1)` - subprocess execution is serialized
- Defense-in-depth: Platform queue is primary protection, this is backup

---

## API Endpoints (`src/backend/routers/agents.py:1152-1261`)

### GET /api/agents/{name}/queue (lines 1152-1178)
Returns current queue status for an agent.

**Response:**
```json
{
  "agent_name": "my-agent",
  "is_busy": true,
  "current_execution": {
    "id": "uuid-123",
    "source": "user",
    "source_user_email": "user@example.com",
    "message": "Hello agent...",
    "queued_at": "2025-12-06T10:00:00Z",
    "started_at": "2025-12-06T10:00:01Z",
    "status": "running"
  },
  "queue_length": 2,
  "queued_executions": [
    {
      "id": "uuid-456",
      "source": "schedule",
      "message": "Daily report...",
      "queued_at": "2025-12-06T10:00:05Z",
      "status": "queued"
    }
  ]
}
```

### POST /api/agents/{name}/queue/clear (lines 1181-1218)
Clear all queued executions (does not stop running execution).

**Response:**
```json
{
  "status": "cleared",
  "agent": "my-agent",
  "cleared_count": 2
}
```

**Audit Log**: `event_type="agent_queue", action="clear_queue"`

### POST /api/agents/{name}/queue/release (lines 1221-1261)
Emergency: force release agent from running state.

**Response:**
```json
{
  "status": "released",
  "agent": "my-agent",
  "was_running": true,
  "warning": "Agent queue state has been reset. Any in-progress execution may still be running."
}
```

**Audit Log**: `event_type="agent_queue", action="force_release", severity="warning"`

---

## Error Handling

### HTTP 429 Too Many Requests (Queue Full)
Returned by `/api/agents/{name}/chat` when queue is full:

```json
{
  "error": "Agent queue is full",
  "agent": "my-agent",
  "queue_length": 3,
  "retry_after": 30,
  "message": "Agent 'my-agent' is busy with 3 queued requests. Please try again later."
}
```

### MCP Response for Busy Agent
Returned by `chat_with_agent` MCP tool:

```json
{
  "status": "agent_busy",
  "agent": "my-agent",
  "queue_status": "queue_full",
  "retry_after_seconds": 30,
  "message": "Agent 'my-agent' is currently busy. The execution queue is full. Please wait 30 seconds before retrying, or try a different agent.",
  "details": { ... }
}
```

### Schedule Execution Failed (Queue Full)
Logged in `schedule_executions` table:
```json
{
  "status": "failed",
  "error": "Agent queue full (3 waiting), skipping scheduled execution"
}
```

---

## Configuration

| Parameter | Default | Location | Description |
|-----------|---------|----------|-------------|
| `MAX_QUEUE_SIZE` | 3 | `execution_queue.py:32` | Maximum queued requests per agent |
| `EXECUTION_TTL` | 600 | `execution_queue.py:33` | Redis key TTL (10 min) - auto-cleanup for stuck executions |
| `QUEUE_WAIT_TIMEOUT` | 120 | `execution_queue.py:34` | Max wait in queue (not yet enforced) |
| `REDIS_URL` | `redis://redis:6379` | env var | Redis connection URL |

---

## Side Effects

### Activity Tracking
Each chat execution creates activity records:
```python
# Track chat start with queue status
chat_activity_id = await activity_service.track_activity(
    agent_name=name,
    details={
        "execution_id": execution.id,
        "queue_status": queue_result  # "running" or "queued:N"
    }
)
```

### Execution Metadata in Response
Chat responses include execution metadata:
```json
{
  "response": "...",
  "execution": {
    "id": "uuid-123",
    "queue_status": "running",
    "was_queued": false
  }
}
```

### Audit Logging
Queue management operations are audited:
- `agent_queue` / `clear_queue` - Queue cleared
- `agent_queue` / `force_release` - Agent force released (severity=warning)

---

## Testing

### Prerequisites
- [ ] Backend running with Redis
- [ ] At least one agent running
- [ ] Frontend at http://localhost:3000

### Test Steps

#### 1. Verify Queue Status Endpoint
```bash
# Check queue status for an agent
curl http://localhost:8000/api/agents/my-agent/queue \
  -H "Authorization: Bearer $TOKEN"
```

**Expected**: Returns `is_busy: false` when idle

#### 2. Test Single Execution
```bash
# Send chat message
curl -X POST http://localhost:8000/api/agents/my-agent/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello"}'
```

**Expected**:
- Response includes `execution.queue_status: "running"`
- Queue status shows `is_busy: true` during execution

#### 3. Test Queue Behavior
**Action**: Open two browser tabs to same agent, send messages simultaneously

**Expected**:
- First message executes immediately
- Second message shows "Queued at position 1" (if UI implements)
- Both complete successfully in order

#### 4. Test Queue Full (429)
**Action**: Send 4+ rapid requests to trigger queue full

```bash
# Rapid fire 5 requests
for i in {1..5}; do
  curl -X POST http://localhost:8000/api/agents/my-agent/chat \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"message": "Request '$i'"}' &
done
```

**Expected**: One or more requests return 429 with `retry_after: 30`

#### 5. Test Queue Clear
```bash
# Clear queue
curl -X POST http://localhost:8000/api/agents/my-agent/queue/clear \
  -H "Authorization: Bearer $TOKEN"
```

**Expected**: Returns `cleared_count` of pending requests

#### 6. Test Force Release
```bash
# Force release stuck agent
curl -X POST http://localhost:8000/api/agents/my-agent/queue/release \
  -H "Authorization: Bearer $TOKEN"
```

**Expected**: Returns `was_running: true/false`, warning message

### Edge Cases
- [ ] Agent stopped mid-execution: Queue releases on error
- [ ] Redis unavailable: Service degradation handling
- [ ] TTL expiration: Stuck execution auto-clears after 10 min

**Status**: Ready for testing
**Last Updated**: 2025-12-19

---

## Future Improvements

1. **Frontend Integration**
   - Show queue status indicator on agent cards
   - Display "Queued at position N" when request is queued
   - Show currently running execution details

2. **Queue Wait Timeout**
   - Implement 120s timeout for queued requests
   - Auto-cleanup stale queued items

3. **Priority Queue**
   - Allow priority levels (user > schedule > agent-to-agent?)
   - Emergency escalation for critical operations

4. **Metrics**
   - Queue depth per agent (Prometheus metrics)
   - Wait time distribution
   - Queue full rejection rate
   - P95 wait times

5. **WebSocket Updates**
   - Broadcast queue position changes
   - Notify when execution starts from queue

---

## Related Flows

- **Upstream**:
  - Agent Lifecycle (`agent-lifecycle.md`) - Agent must be running
  - Auth0 Authentication (`auth0-authentication.md`) - User authorization

- **Integrates With**:
  - Agent Chat (`agent-chat.md`) - Queue enforced on all chat requests
  - Agent Scheduling (`scheduling.md`) - Scheduled executions use queue
  - MCP Orchestration (`mcp-orchestration.md`) - Agent-to-agent calls use queue (unless `parallel: true`)
  - Activity Stream (`activity-stream.md`) - Queue status tracked in activities

- **Bypassed By**:
  - Parallel Headless Execution (`parallel-headless-execution.md`) - `POST /api/agents/{name}/task` bypasses queue entirely (Added 2025-12-22)

- **Downstream**:
  - Activity Monitoring (`activity-monitoring.md`) - Tool execution tracking

---

## Revision History

| Date | Changes |
|------|---------|
| 2025-12-22 | Added Queue Bypass section for Parallel Task Execution (/api/task endpoint) |
| 2025-12-19 | Updated line numbers for all source files based on current codebase |
| 2025-12-19 | Updated models.py reference to lines 163-210 |
| 2025-12-19 | Updated agents.py queue endpoints to lines 1152-1261 |
| 2025-12-19 | Updated scheduler_service.py reference to lines 169-399 |
| 2025-12-19 | Updated MCP chat.ts reference to lines 137-189 |
| 2025-12-19 | Updated MCP client.ts reference to lines 288-319 |
| 2025-12-19 | Updated agent_server claude_code.py reference to lines 23-30 |
| 2025-12-19 | Updated agent_server chat.py reference to lines 18-80 |
| 2025-12-06 | Updated agent-server references to new modular structure (`agent_server/` package) |
| 2025-12-06 | Initial documentation |
