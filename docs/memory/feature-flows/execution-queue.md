# Feature: Execution Queue System

> **⚠️ SUPERSEDED 2026-04-26 (#428):** `ExecutionQueue` has been deleted. Its responsibilities — N=1 serial gating and the in-memory FIFO overflow store — collapsed into the unified [`CapacityManager`](capacity-management.md) facade with `overflow_policy="queue_in_memory"`. The Redis LIST key (`agent:queue:{name}`) and the depth bound (3) are preserved for wire-format compatibility. New work should reference [`capacity-management.md`](capacity-management.md). The notes below are kept for historical context on the original race-condition fixes and observability behaviour.
>
> **Updated**: 2026-03-21 - **Unified Capacity Tracking (#98)**: Chat executions (`/api/chat`) now acquire a capacity slot via `SlotService` in addition to using `ExecutionQueue`. This makes `SlotService` the single source of truth for agent load — the capacity meter reflects ALL execution types. The queue still enforces serial chat; the slot tracks resource usage. `force_release_agent_logic()` now also clears capacity slots. Termination also releases slots.
>
> **Previous (2026-03-18)** - **Execution Records for All /api/chat Calls (#96)**: Every call to `POST /api/agents/{name}/chat` now creates a `task_execution` DB record regardless of call source. `triggered_by` values: `"chat"` (UI), `"mcp"` (user via MCP), `"agent"` (agent-to-agent). The response always includes `execution.task_execution_id`. New headers accepted: `X-Via-MCP`, `X-MCP-Key-ID`, `X-MCP-Key-Name` for MCP attribution. **Default model changed** to `"sonnet"` in the Chat tab frontend (`AgentDetail.vue:511`) for subscription compatibility (#138).
>
> **Previous (2026-02-16)** - **Security Fix (Credential Leakage Prevention)**: Execution logs, tool calls, and responses are now sanitized before database persistence to prevent credential leakage. Backend uses `sanitize_execution_log()` and `sanitize_response()` from `src/backend/utils/credential_sanitizer.py` in `routers/chat.py` for both `/chat` and `/task` endpoints.
>
> **Previous (2026-01-14)** - **Bug Fix (Race Conditions)**: Atomic Redis operations now prevent race conditions:
> - `submit()` uses `SET NX EX` for atomic slot acquisition (prevents concurrent requests acquiring the same slot)
> - `complete()` uses Lua script for atomic pop-and-set (prevents queue entries from being lost or processed twice)
> - `get_all_busy_agents()` uses `SCAN` instead of `KEYS` to avoid blocking Redis on large datasets
>
> **Previous (2025-01-02)**: Scheduler now uses `AgentClient.task()` instead of `AgentClient.chat()` to ensure execution logs are stored in raw Claude Code `stream-json` format for proper display in the log viewer.

## Overview
The Execution Queue System prevents parallel execution on agents by serializing all execution requests through a Redis-backed queue. This ensures only one execution runs per agent at a time, protecting Claude Code's conversation state from corruption.

## User Story
As a Trinity platform operator, I want all agent execution requests (user chat, scheduled tasks, agent-to-agent calls) to be serialized so that Claude Code's conversation context remains consistent and uncorrupted.

## Entry Points
- **User Chat**: `POST /api/agents/{name}/chat` - User messages from UI
- **Dedicated Scheduler**: `src/scheduler/service.py:_execute_schedule()` - Cron-triggered executions
- **MCP Server**: `chat_with_agent` tool - Agent-to-agent communication
- **MCP Server**: `trigger_agent_schedule` tool - Manual schedule trigger via MCP (Added 2026-01-29)
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

### Queue Bypass: Web Terminal (Added 2025-12-25)

The Web Terminal (`WS /api/agents/{name}/terminal`) also bypasses the queue:
- **Direct PTY access** - User has interactive Claude Code session
- **No queue serialization** - Terminal sessions are independent of chat queue
- See [agent-terminal.md](agent-terminal.md) for details

**Note on /task endpoint (Parallel Headless Execution)**:
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

## Data Models (`src/backend/models.py:189-236`)

### ExecutionSource (Enum)
```python
class ExecutionSource(str, Enum):
    """Source of an execution request."""
    USER = "user"       # User chat via UI
    SCHEDULE = "schedule"  # Scheduled task
    AGENT = "agent"     # Agent-to-agent via MCP
```

### QueueItemStatus (Enum)
```python
class QueueItemStatus(str, Enum):
    """Status of an execution request in the in-memory/Redis execution queue."""
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
```

> **⚠️ Status Enum Disambiguation**: There are three separate status systems in Trinity:
>
> | Enum | Scope | Values | Storage |
> |------|-------|--------|---------|
> | `QueueItemStatus` | Execution queue (this feature) | `queued`, `running`, `completed`, `failed`, `timeout` | Redis/in-memory |
> | `TaskExecutionStatus` | Task/schedule executions | `running`, `success`, `failed`, `cancelled`, `skipped` | SQLite (`schedule_executions`, `task_executions`) |
> | `ExecutionStatus` (process engine) | Process step executions | `pending`, `running`, `completed`, `failed`, `cancelled` | SQLite (`process_executions`) |
>
> Note: `QueueItemStatus.COMPLETED` ≠ `TaskExecutionStatus` — the queue uses `completed` while the DB uses `success`. Renamed from `ExecutionStatus` to `QueueItemStatus` (#92) to reduce confusion.

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
    queued_at: datetime                        # UTC timestamp
    started_at: Optional[datetime] = None      # UTC timestamp
    status: QueueItemStatus = QueueItemStatus.QUEUED
```

> **Timezone Note (2026-01-15)**: All timestamps are stored and transmitted as UTC with 'Z' suffix for consistent cross-timezone behavior. Backend uses `utc_now_iso()` from `utils/helpers.py`. See [Timezone Handling Guide](/docs/TIMEZONE_HANDLING.md).

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

### Thread Safety (2026-01-14 Bug Fix)

The queue uses atomic Redis operations to prevent race conditions:

| Operation | Pattern | Security Benefit |
|-----------|---------|------------------|
| `submit()` | `SET NX EX` | Atomic slot acquisition - prevents two requests from acquiring the same slot |
| `complete()` | Lua script | Atomic pop-and-set - prevents queue entries from being lost or processed twice |
| `get_all_busy_agents()` | `SCAN` (not `KEYS`) | Non-blocking iteration - avoids Redis blocking on large datasets |

**Lua Script for `complete()`** (lines 67-84):
```lua
-- Atomically pops next from queue and sets as running, or clears if empty
local next_item = redis.call('RPOP', queue_key)
if next_item then
    redis.call('SET', running_key, next_item, 'EX', ttl)
    return next_item
else
    redis.call('DEL', running_key)
    return nil
end
```

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

#### create_execution() (lines 109-129)
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

#### submit() (lines 131-196)
Submits execution to queue using atomic `SET NX EX` for slot acquisition.
```python
async def submit(
    self,
    execution: Execution,
    wait_if_busy: bool = True
) -> tuple[str, Execution]:
    """
    Uses atomic SET NX EX to prevent race conditions where two concurrent
    requests could both acquire the execution slot.

    Returns:
        ("running", execution) - Started immediately
        ("queued:N", execution) - Queued at position N

    Raises:
        QueueFullError - Queue at MAX_QUEUE_SIZE
        AgentBusyError - Agent busy and wait_if_busy=False
    """
```

**Flow:**
1. **Atomic acquire**: `SET key value NX EX ttl` - returns True only if key was set
2. If acquired: return "running"
3. If busy (atomic acquire failed) and wait_if_busy=True: check queue length
4. If queue < MAX_QUEUE_SIZE: LPUSH to queue, return "queued:N"
5. If queue full: raise QueueFullError

#### complete() (lines 198-238)
Marks current execution done and atomically starts next if queued:
```python
async def complete(self, agent_name: str, success: bool = True) -> Optional[Execution]:
    """
    Uses a Lua script for atomic pop-and-set to prevent race conditions
    where queue entries could be lost or processed twice.

    Returns:
        Next execution that was started (if any), or None
    """
```

**Flow (Atomic via Lua Script):**
1. Log completed execution (before atomic operation)
2. Execute Lua script that atomically:
   - RPOP next from queue
   - If next exists: SET as running with TTL
   - If queue empty: DEL running key
3. Return next execution (or None if queue empty)

#### get_status() (lines 240-261)
Returns current queue status:
```python
async def get_status(self, agent_name: str) -> QueueStatus
```

#### is_busy() (lines 263-266)
Quick check if agent is executing:
```python
async def is_busy(self, agent_name: str) -> bool
```

#### clear_queue() (lines 268-279)
Clears all queued executions (not current):
```python
async def clear_queue(self, agent_name: str) -> int
```

#### force_release() (lines 281-293)
Emergency: clears running state for stuck agents:
```python
async def force_release(self, agent_name: str) -> bool
```

#### get_all_busy_agents() (lines 295-308)
Lists all agents currently executing using `SCAN` (not `KEYS`):
```python
async def get_all_busy_agents(self) -> List[str]:
    """
    Uses SCAN instead of KEYS to avoid blocking Redis on large datasets.
    """
```

**Note**: Previous implementation used `KEYS` which blocks Redis during iteration. The `SCAN` approach iterates incrementally with cursor, returning batches of 100 keys at a time.

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

### 1. User Chat (`src/backend/routers/chat.py:59-380`)

**Entry Point**: `POST /api/agents/{name}/chat`

```python
@router.post("/{name}/chat")
async def chat_with_agent(
    request: ChatMessageRequest,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user),
    x_source_agent: Optional[str] = Header(None),
    x_via_mcp: Optional[str] = Header(None),       # Set for all MCP calls
    x_mcp_key_id: Optional[str] = Header(None),    # MCP key ID for attribution
    x_mcp_key_name: Optional[str] = Header(None)   # MCP key name for attribution
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
    except QueueFullError as e:
        raise HTTPException(status_code=429, detail={...})

    # Issue #98: Acquire capacity slot so SlotService tracks ALL execution types
    slot_service = get_slot_service()
    chat_slot_acquired = await slot_service.acquire_slot(
        agent_name=name, execution_id=execution.id,
        max_parallel_tasks=db.get_max_parallel_tasks(name),
        message_preview=request.message[:100],
        timeout_seconds=db.get_execution_timeout(name),
    )

    # Create execution record for ALL chat calls (#96)
    # triggered_by: "agent" | "mcp" | "chat"
    task_execution = db.create_task_execution(
        agent_name=name, message=request.message,
        triggered_by=triggered_by, ...
    )

    # ... execute chat, track activities, update execution record on success/failure ...

    finally:
        # ALWAYS release queue slot when done
        await queue.complete(name, success=execution_success)
        # Issue #98: Release capacity slot
        if chat_slot_acquired:
            await slot_service.release_slot(name, execution.id)
```

**Response includes execution metadata:**
```json
{
  "execution": {
    "id": "<queue-uuid>",
    "task_execution_id": "<db-uuid>",
    "queue_status": "running",
    "was_queued": false
  }
}
```

**Key Points:**
- Accepts three new headers: `X-Via-MCP`, `X-MCP-Key-ID`, `X-MCP-Key-Name` for MCP call attribution
- Creates execution with source tracking (USER/AGENT)
- **Always** creates a `task_execution` DB record for every call (user, MCP, agent-to-agent) — #96
- **Acquires a capacity slot** via `SlotService` so the capacity meter reflects chat load — #98
- `triggered_by` values: `"chat"` (UI), `"mcp"` (user via MCP key), `"agent"` (agent-to-agent)
- `task_execution_id` is always non-null in the response `execution` block
- On success: calls `db.update_execution_status(status=SUCCESS, response, context_used, cost, tool_calls, execution_log)`
- On failure: calls `db.update_execution_status(status=FAILED, error=error_msg)`
- Handles 429 on queue full; always releases queue in `finally` block

### 2. Dedicated Scheduler (`src/scheduler/service.py`)

> **Note (2026-02-11)**: The embedded scheduler (`src/backend/services/scheduler_service.py`) has been **removed**. Schedule execution is now handled by the Dedicated Scheduler Service. See [scheduler-service.md](scheduler-service.md) for complete documentation.

**Entry Point**: APScheduler cron trigger -> `_execute_schedule()` in dedicated scheduler container

```python
# src/scheduler/service.py
async def _execute_schedule(self, schedule_id: str):
    # Try to acquire distributed lock
    lock = self.lock_manager.try_acquire_schedule_lock(schedule_id)
    if not lock:
        logger.info(f"Schedule {schedule_id} already being executed by another instance")
        return

    try:
        await self._execute_schedule_with_lock(schedule_id)
    finally:
        lock.release()

async def _execute_schedule_with_lock(self, schedule_id: str):
    # Check autonomy enabled
    if not self.db.get_autonomy_enabled(schedule.agent_name):
        logger.info(f"Schedule {schedule_id} skipped: agent autonomy disabled")
        return

    # Create execution record
    execution = self.db.create_execution(...)

    # Track activity via internal API
    await self._track_activity_start(schedule, execution)

    # Execute using AgentClient.task()
    client = get_agent_client(schedule.agent_name)
    task_response = await client.task(schedule.message, execution_id=execution.id)

    # Update execution with parsed response metrics
    self.db.update_execution_status(...)

    # Complete activity via internal API
    await self._complete_activity(...)
```

**Key Points:**
- Runs in **dedicated scheduler container** (not backend)
- Uses Redis distributed locks to prevent duplicate executions
- Checks autonomy before execution
- Activity tracking via internal API (`POST /api/internal/activities/track`)
- Uses `AgentClient.task()` for stateless execution with raw log format
- Execution log stored in raw Claude Code `stream-json` format for log viewer compatibility

#### AgentClient Service (Scheduler Version)

The dedicated scheduler has its own `AgentClient` (`src/scheduler/agent_client.py`) for agent communication:

```python
from scheduler.agent_client import get_agent_client, AgentNotReachableError

# Create client for agent
client = get_agent_client(schedule.agent_name)

# Send task message - returns AgentTaskResponse with raw Claude Code log
task_response = await client.task(schedule.message, execution_id=execution.id)

# Access parsed metrics
task_response.response_text          # The agent's response (truncated if > 10000 chars)
task_response.metrics.context_used   # Tokens used
task_response.metrics.context_max    # Context window size
task_response.metrics.context_percent # Usage percentage
task_response.metrics.cost_usd       # Cost in USD
task_response.metrics.tool_calls_json     # JSON string of tool calls
task_response.metrics.execution_log_json  # Raw Claude Code stream-json format
task_response.raw_response           # Original response dict
```

**Note**: The `task()` method was added on 2025-01-02 to fix execution log viewer compatibility. It calls `/api/task` which returns raw Claude Code `stream-json` format, unlike `chat()` which calls `/api/chat` and returns a simplified format.

#### Response Data Classes (`src/backend/services/agent_client.py:528-544`)

```python
@dataclass
class AgentChatMetrics:
    """Observability data extracted from agent chat response."""
    context_used: int
    context_max: int
    context_percent: float
    cost_usd: Optional[float]
    tool_calls_json: Optional[str]
    execution_log_json: Optional[str]


@dataclass
class AgentChatResponse:
    """Parsed response from agent chat endpoint."""
    response_text: str
    metrics: AgentChatMetrics
    raw_response: Dict[str, Any]
```

#### Response Parsing Logic (`src/backend/services/agent_client.py:907-958`)

The `_parse_chat_response()` method centralizes response parsing:

```python
def _parse_chat_response(self, result: Dict[str, Any]) -> AgentChatResponse:
    # Extract response text (truncated if > 10000 chars)
    response_text = result.get("response", str(result))
    if len(response_text) > 10000:
        response_text = response_text[:10000] + "... (truncated)"

    # Extract observability data
    session_data = result.get("session", {})
    metadata = result.get("metadata", {})
    execution_log = result.get("execution_log")

    # Context usage - NOTE: cache tokens are SUBSETS, not additional
    context_used = session_data.get("context_tokens") or metadata.get("input_tokens", 0)
    context_max = session_data.get("context_window") or metadata.get("context_window", 200000)
    context_percent = round(context_used / max(context_max, 1) * 100, 1)

    # Cost
    cost = metadata.get("cost_usd") or session_data.get("total_cost_usd")

    # Tool calls / execution log
    # Note: Check is not None, not truthiness - empty list [] is valid log
    tool_calls_json = None
    execution_log_json = None
    if execution_log is not None:
        execution_log_json = json.dumps(execution_log)
        tool_calls_json = execution_log_json  # Backwards compatibility
```

**Context Token Bug Fix**: The parsing logic correctly handles cache tokens:
- `cache_creation_tokens` and `cache_read_tokens` are **billing breakdowns** (subsets) of `input_tokens`, not additional tokens
- Do NOT sum them - use `input_tokens` directly or `session_data.context_tokens`

### 3. MCP Server (`src/mcp-server/src/tools/chat.ts:186-270`)

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
- `docker/base-image/agent_server/services/claude_code.py:26-33` - Execution lock and ThreadPoolExecutor
- `docker/base-image/agent_server/routers/chat.py:21-87` - Chat endpoint with lock

**Defense Layer**: asyncio.Lock + ThreadPoolExecutor(max_workers=1)

```python
# agent_server/services/claude_code.py:26-33
# Thread pool for running blocking subprocess operations
# max_workers=1 ensures only one execution at a time within this container
_executor = ThreadPoolExecutor(max_workers=1)

# Asyncio lock for execution serialization (safety net for parallel request prevention)
_execution_lock = asyncio.Lock()

# agent_server/routers/chat.py:21-87
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

## Frontend: Chat Tab Model Default

**File**: `src/frontend/src/views/AgentDetail.vue:505-511`

The Chat tab computes a default model based on agent runtime (#138):

```javascript
const defaultModel = computed(() => {
  const runtime = agent.value?.runtime || 'claude-code'
  if (runtime === 'gemini-cli' || runtime === 'gemini') {
    return 'gemini-2.5-flash'
  }
  return 'sonnet' // Claude default
})
```

The `selectedModel` in `ChatPanel.vue:218` is initialized from `localStorage.getItem('trinity_chat_model') || ''`. When the agent loads and no model is stored, `AgentDetail.vue:890` sets `currentModel.value = defaultModel.value`, which resolves to `'sonnet'` for Claude-runtime agents. This ensures subscription-compatible model selection out of the box.

---

## API Endpoints

### Architecture (Post-Refactoring)

The queue management endpoints use a **thin router + service layer** architecture:

| Layer | File | Purpose |
|-------|------|---------|
| Router | `src/backend/routers/agents.py` | Endpoint definitions |
| Service | `src/backend/services/agent_service/queue.py` (125 lines) | Queue management logic |

### Key Files Summary

| File | Lines | Purpose |
|------|-------|---------|
| `src/backend/services/execution_queue.py` | 244 | Redis-backed queue implementation |
| `src/backend/services/agent_client.py` | 1130 | Centralized agent HTTP client (Redis-backed circuit breaker, retry, connection pool, per-base_url drop-grace — RELIABILITY-001 / #631 / #474) |
| `src/scheduler/service.py` | - | Dedicated scheduler (APScheduler, activity tracking) |
| `src/scheduler/agent_client.py` | - | Scheduler's agent HTTP client |
| `src/backend/routers/chat.py` | 1004 | Chat endpoint (uses raw httpx with retry) |
| `src/backend/models.py:189-236` | 47 | ExecutionSource, ExecutionStatus, Execution, QueueStatus |
| `docker/base-image/agent_server/services/claude_code.py` | - | Defense-in-depth lock |

### GET /api/agents/{name}/queue

**Router**: `src/backend/routers/agents.py`
**Service**: `src/backend/services/agent_service/queue.py:18-43`

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

### POST /api/agents/{name}/queue/clear

**Router**: `src/backend/routers/agents.py`
**Service**: `src/backend/services/agent_service/queue.py:46-82`

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

### POST /api/agents/{name}/queue/release

**Router**: `src/backend/routers/agents.py`
**Service**: `src/backend/services/agent_service/queue.py:85-125`

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
Each chat execution creates activity records with `related_execution_id` for structured queries:
```python
# Track chat start with queue status AND execution linkage
# related_execution_id is a top-level field for SQL JOINs
chat_activity_id = await activity_service.track_activity(
    agent_name=name,
    activity_type=ActivityType.CHAT_START,
    related_execution_id=task_execution_id,  # Database ID for structured queries
    details={
        "execution_id": task_execution_id,  # Also in details for WebSocket events
        "queue_status": queue_result  # "running" or "queued:N"
    }
)
```

### Two ID Systems

**Important**: The system maintains two distinct execution ID types:

| ID Type | Format | Storage | TTL | Purpose |
|---------|--------|---------|-----|---------|
| **Queue Execution ID** | UUID v4 | Redis | 10 min | Internal queue management, transient |
| **Database Execution ID** | `token_urlsafe(16)` | SQLite `schedule_executions` | Permanent | API access, UI navigation, activity linkage |

**Usage**:
- `related_execution_id` in `agent_activities` table links to `schedule_executions.id` (Database ID)
- `execution.id` in queue operations is the Redis Queue ID (transient)
- Navigation URLs use Database Execution ID: `/agents/{name}/executions/{database_id}`

### Execution Metadata in Response
Chat responses include both IDs for clarity:
```json
{
  "response": "...",
  "execution": {
    "id": "uuid-123",               // Queue ID (transient, 10-min TTL)
    "task_execution_id": "abc123...", // Database ID (permanent) - use for navigation
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

#### 7. Verify Execution Record Created for Chat (#96)
```bash
# Send a chat message
curl -X POST http://localhost:8000/api/agents/my-agent/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "Execution tracking test"}'
```

**Expected**:
- Response contains `execution.task_execution_id` (non-null UUID)
- `GET /api/agents/my-agent/executions` shows a new record with `triggered_by: "chat"`

**Automated tests**: `tests/test_agent_chat.py::TestChatExecutionTracking`
- `test_chat_creates_execution_record` — verifies execution count increases
- `test_chat_execution_has_chat_trigger` — verifies `triggered_by == "chat"`
- `test_chat_response_includes_task_execution_id` — verifies response field is non-null

### Edge Cases
- [ ] Agent stopped mid-execution: Queue releases on error
- [ ] Redis unavailable: Service degradation handling
- [ ] TTL expiration: Stuck execution auto-clears after 10 min

**Status**: Ready for testing
**Last Updated**: 2026-03-18

---

## Chat API Implementation Details

This section documents the internal implementation details of the Chat API (`POST /api/agents/{name}/chat`) that uses the execution queue. For the deprecated Chat tab UI, users now interact via the Web Terminal - see [agent-terminal.md](agent-terminal.md).

### Claude Code CLI Execution (`agent_server/services/claude_code.py:400-520`)

The agent server executes Claude Code as a subprocess with specific flags:

```python
async def execute_claude_code(prompt: str, stream: bool = False, model: Optional[str] = None):
    # Build command
    cmd = ["claude", "--print", "--output-format", "stream-json",
           "--verbose", "--dangerously-skip-permissions"]

    # Add MCP config if .mcp.json exists
    mcp_config_path = Path.home() / ".mcp.json"
    if mcp_config_path.exists():
        cmd.extend(["--mcp-config", str(mcp_config_path)])

    # Add model selection if set
    if agent_state.current_model:
        cmd.extend(["--model", agent_state.current_model])

    # Use --continue flag for subsequent messages (maintains conversation context)
    if agent_state.session_started:
        cmd.append("--continue")

    # Use Popen for real-time streaming
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, ...)

    # Read output in thread pool (allows activity polling during execution)
    loop = asyncio.get_event_loop()
    stderr_output, return_code = await loop.run_in_executor(_executor, read_subprocess_output)
```

### Stream-JSON Parsing (`agent_server/services/claude_code.py:50-200`)

Claude Code outputs one JSON object per line:

```json
{"type": "init", "session_id": "abc123"}
{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "...", "name": "Read", "input": {...}}]}}
{"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "...", "content": [...]}]}}
{"type": "result", "total_cost_usd": 0.003, "duration_ms": 1234, "usage": {...}}
```

### Session State Management (`agent_server/state.py:15-91`)

The agent maintains session state for conversation continuity:

```python
class AgentState:
    def __init__(self):
        self.conversation_history: List[ChatMessage] = []
        self.session_started = False
        self.session_total_cost: float = 0.0
        self.session_context_tokens: int = 0
        self.session_context_window: int = 200000
        self.current_model: Optional[str] = os.getenv("CLAUDE_MODEL", None)
        self.session_activity = {...}  # Real-time tool tracking

    def reset_session(self):
        self.conversation_history = []
        self.session_started = False
        self.session_total_cost = 0.0
        self.session_total_output_tokens = 0
        self.session_context_tokens = 0
        # Note: current_model is NOT reset - it persists until explicitly changed
        self.session_activity = self._create_empty_activity()
        self.tool_outputs = {}
```

### Context Window Tracking (`agent_server/routers/chat.py:53-70`)

Token tracking logic with monotonic growth guarantee:

```python
agent_state.session_total_cost += metadata.cost_usd
agent_state.session_total_output_tokens += metadata.output_tokens
# Context window usage: metadata.input_tokens contains the complete total
# (from modelUsage.inputTokens which includes all turns and cached tokens)
# Fix: Context should monotonically increase during a session, so keep the max
if metadata.input_tokens > agent_state.session_context_tokens:
    agent_state.session_context_tokens = metadata.input_tokens
elif metadata.input_tokens > 0 and metadata.input_tokens < agent_state.session_context_tokens:
    # Claude reported fewer tokens than before - likely only new input, not cumulative
    # Keep the previous (higher) value as context should only grow
    logger.warning(...)
agent_state.session_context_window = metadata.context_window
```

**Bug Fix (2025-12-11)**: Previously, context tracking incorrectly summed `input_tokens + cache_creation_tokens + cache_read_tokens`, causing context percentages >100% (e.g., 130%, 289%). The issue: `cache_creation_tokens` and `cache_read_tokens` are billing **breakdowns** (subsets) of `input_tokens`, not additional tokens. The fix uses `metadata.input_tokens` directly, which already contains the authoritative total from Claude's `modelUsage.inputTokens`.

**Bug Fix (2025-12-19)**: Context was resetting to ~4 tokens on subsequent messages when using `--continue` flag. Root cause: Claude Code may report only new input tokens (not cumulative) for continued conversations. Fix: Context tokens now use monotonic growth - only update if new value is greater than previous.

### Chat Endpoint Session Flow

The chat endpoint (`routers/chat.py:106-400`) integrates with the execution queue and maintains session state:

1. **Queue submission** - Create execution, submit to queue (wait if busy)
2. **Session lookup** - `db.get_or_create_chat_session()` for persistent tracking
3. **Activity tracking** - Create `chat_start` activity linked to execution
4. **Message logging** - Store user message in `chat_messages` table
5. **Agent proxy** - `httpx.post(f"http://agent-{name}:8000/api/chat")` via Docker network
6. **Response logging** - Store assistant response with observability data (cost, context, tool_calls)
7. **Tool tracking** - Create `tool_call` activities for each tool execution
8. **Activity completion** - Mark chat activity complete with duration and metadata
9. **Queue release** - Always release queue slot in `finally` block

---

## Execution Termination (Added 2026-01-12)

Running executions can be terminated mid-flight via the Stop button in the Tasks panel. This uses a process registry to track subprocess handles and send graceful termination signals.

### Process Registry

**File**: `docker/base-image/agent_server/services/process_registry.py`

Thread-safe registry for tracking running subprocess handles:
- `register(execution_id, process, metadata)` - Register subprocess when started
- `unregister(execution_id)` - Remove when completed
- `terminate(execution_id)` - Graceful SIGINT -> SIGKILL termination
- `list_running()` - Get all running executions

### Termination Flow

1. User clicks Stop button in Tasks panel (`TasksPanel.vue:239-255`)
2. Frontend calls `POST /api/agents/{name}/executions/{execution_id}/terminate`
3. Backend proxies to agent container (`chat.py:1059-1131`)
4. Agent server terminates subprocess via registry (`routers/chat.py:241-260`)
5. Backend clears queue state on success
6. Activity tracked with type `EXECUTION_CANCELLED`

### Signal Handling

| Signal | Purpose | Timeout |
|--------|---------|---------|
| `SIGINT` | Graceful termination | 5 seconds |
| `SIGKILL` | Force kill fallback | 2 seconds |

Claude Code handles SIGINT gracefully, finishing its current tool operation before exiting.

### Endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /api/agents/{name}/executions/{execution_id}/terminate` | Backend proxy |
| `GET /api/agents/{name}/executions/running` | List running for termination |
| `POST /api/executions/{execution_id}/terminate` | Agent container endpoint |
| `GET /api/executions/running` | Agent container endpoint |

See [execution-termination.md](execution-termination.md) for full documentation.

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
  - ~~Agent Chat~~ (`agent-chat.md` - DEPRECATED) - Chat API still uses queue; user now uses Terminal
  - Agent Scheduling (`scheduling.md`) - Scheduled executions use queue; MCP `trigger_agent_schedule` also uses queue
  - MCP Orchestration (`mcp-orchestration.md`) - Agent-to-agent calls use queue (unless `parallel: true`); schedule tools go through queue
  - Activity Stream (`activity-stream.md`) - Queue status tracked in activities

- **Bypassed By**:
  - Agent Terminal (`agent-terminal.md`) - WebSocket PTY bypasses queue entirely (Added 2025-12-25)
  - Parallel Headless Execution (`parallel-headless-execution.md`) - `POST /api/agents/{name}/task` bypasses queue entirely (Added 2025-12-22)

- **Downstream**:
  - Activity Monitoring (`activity-monitoring.md`) - Tool execution tracking

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-05-13 | **Circuit Breaker Sibling-Collapse Fix (#474)**: `AgentClient._request()` in `services/agent_client.py` rewritten to neutralise the burst-collapse where one mid-flight transport drop in a concurrent fan-out tripped the breaker. Added per-base_url `_recent_drops: Dict[str, float]` map with `_DROP_GRACE_SEC = 2.0` window (lines 442-473), helpers `_stamp_drop()` / `_is_within_drop_grace()` / `_build_http_client()` / `_acquire_client() -> (client, is_pooled)` (lines 462-514), and new exception `AgentConnectionDroppedError(AgentNotReachableError)` (lines 570-578). The rewritten exception handler in `_request()` (lines 671-727) now: (a) on `ConnectError` / `TimeoutException` within the drop-grace window, raises `AgentConnectionDroppedError` instead of `AgentNotReachableError` and skips `record_failure()`; (b) on `ReadError` / `WriteError` / `RemoteProtocolError` / `BrokenPipeError` / `ConnectionResetError` mid-flight, stamps the drop, evicts the pooled client only if its identity matches the one this worker acquired (`evicted is client` guard, prevents siblings double-closing), and raises `AgentConnectionDroppedError` — again no `record_failure()`. Non-pooled (in-grace) clients are single-use and `aclose()`'d in `finally` to prevent connection-handle leaks during sustained bursts. The grace map and pool are process-local (per uvicorn worker); the Redis-backed `CircuitState` from #631 remains the fleet-wide source of truth — transport drops never call `record_failure()` in any worker. `AgentConnectionDroppedError` inherits from `AgentNotReachableError` so tenacity `retry_if_exception_type` decorators on `get_session()` / `health_check()` continue to handle it. Solves the 10-concurrent-call scenario where one drop evicts the shared pool and sibling fresh-client retries die with `ConnectError`, previously tripping 9-of-10 sibling failures into the breaker. Tests: `tests/integration/test_circuit_breaker.py` (cross-worker burst scenarios), `tests/unit/test_circuit_breaker.py` (drop-grace state machine, pool-eviction identity check, `AgentConnectionDroppedError` raising). |
| 2026-02-21 | **Bug Fix (EXEC-023)**: Fixed `DatabaseManager.update_execution_status()` wrapper in `src/backend/database.py:1295-1299` - was missing `claude_session_id` parameter. The wrapper now correctly forwards the parameter to `db/schedules.py:update_execution_status()` (lines 559-610). This affected all execution status updates (chat, task, scheduled) that tried to store Claude Code session IDs for the "Continue Execution as Chat" feature. |
| 2026-03-15 | **Unified chat execution tracking (#96)**: All `/api/chat` calls now create `schedule_executions` records, not just MCP/agent-to-agent. User chats use `triggered_by=chat`. Execution records are updated on success/failure for all chat types. |
| 2026-02-16 | **Security Fix (Credential Leakage Prevention)**: Backend now sanitizes execution logs, tool calls, and responses before database persistence. Uses `sanitize_execution_log()` and `sanitize_response()` from `src/backend/utils/credential_sanitizer.py`. Both `/chat` (lines 283-286) and `/task` (lines 468-470, 699-712) endpoints sanitize data before calling `db.update_execution_status()`. This is a defense-in-depth layer that catches any credentials that may have bypassed agent-side sanitization. |
| 2026-02-15 | **Claude Max subscription support**: Documented that Claude Code now uses whatever authentication is available (OAuth session from `/login` or `ANTHROPIC_API_KEY`). The mandatory API key check was removed from `execute_claude_code()` and `execute_headless_task()`. This allows headless executions to use Claude Max subscription billing if user logged in via web terminal. |
| 2026-02-12 | **Test fix**: `test_parallel_task_does_not_show_in_queue` now uses `async_mode: True` to return immediately instead of waiting for task completion (was timing out after 30s). |
| 2026-05-12 | **Circuit-breaker failure classification narrowed (#474)**: `AgentClient._request()` now classifies exceptions via the shared `is_circuit_failure()` helper in `services/agent_client.py`. Only `CIRCUIT_FAILURE_EXCEPTIONS` (`httpx.ConnectError`, `httpx.ConnectTimeout`) increment the 3-failure threshold. `TRANSIENT_TRANSPORT_EXCEPTIONS` (`ReadTimeout`, `WriteTimeout`, `PoolTimeout`, `WriteError`, `ReadError`, `RemoteProtocolError`) still raise `AgentNotReachableError` (preserves the typed-error contract for `except AgentClientError` blocks) but no longer poison the circuit. Raw `OSError` subclasses (`BrokenPipeError`, `ConnectionResetError`) propagate uncaught — local resource bugs, not agent unhealth. `asyncio.CancelledError` is re-raised explicitly so it can't be shadowed by a broader catch. Same rule is mirrored in `services/monitoring_service.py:check_network_health()` so the `/health` probe and inline `/api/*` requests agree on what "unreachable" means. |
| 2026-04-11 | **RELIABILITY-001**: `AgentClient` now includes per-agent circuit breaker (3 failures → 30s cooldown), tenacity retry on idempotent calls (health, session), and persistent connection pooling. New `AgentCircuitOpenError` exception (subclass of `AgentClientError`). Updated line numbers for response data classes and parsing logic. |
| 2026-02-11 | **Scheduler Consolidation**: Updated Section 2 to reflect removal of embedded scheduler. Schedule execution now handled by dedicated scheduler (`src/scheduler/`). Updated Key Files Summary table. |
| 2026-01-29 | **MCP Schedule Management (MCP-SCHED-001)**: Added `trigger_agent_schedule` MCP tool to Entry Points. Updated Related Flows to note that MCP schedule tools go through the queue. |
| 2026-01-14 | **Race Condition Bug Fixes (HIGH)**: Fixed three race conditions in execution queue. (1) `submit()` now uses atomic `SET NX EX` instead of separate EXISTS/SET - prevents two concurrent requests from acquiring the same execution slot. (2) `complete()` now uses Lua script for atomic pop-and-set - prevents queue entries from being lost or processed twice. (3) `get_all_busy_agents()` replaced `KEYS` with `SCAN` - avoids blocking Redis on large datasets. Added Thread Safety section with Lua script documentation. Updated all method line numbers. |
| 2026-01-12 | **Execution Termination**: Added section documenting process registry, termination flow, signal handling (SIGINT -> SIGKILL), and new endpoints. See [execution-termination.md](execution-termination.md) for full feature documentation. |
| 2026-01-11 | **Execution ID tracking**: Documented two ID systems (Queue vs Database). Chat response now includes both `id` (queue) and `task_execution_id` (database). Activity tracking uses `related_execution_id` as top-level field for structured SQL queries. |
| 2025-01-02 | **Scheduler log fix**: Changed scheduler from `AgentClient.chat()` to `AgentClient.task()`. The `task()` method calls `/api/task` endpoint which returns raw Claude Code `stream-json` format. This fixes execution log viewer which could not parse the simplified format from `/api/chat`. Added `task()` method and `_parse_task_response()` to `AgentClient`. |
| 2025-12-31 | **AgentClient service**: Scheduler now uses centralized `AgentClient` from `services/agent_client.py` instead of raw `httpx` calls. Response parsing logic moved to `AgentClient._parse_chat_response()`. Added `AgentChatResponse` and `AgentChatMetrics` dataclasses. Context token bug fix documented (cache tokens are subsets, not additional). |
| 2025-12-31 | **Execution log storage**: All execution records now store full Claude Code execution transcript in `execution_log` column. New endpoint `GET /api/agents/{name}/executions/{execution_id}/log` retrieves full execution log for debugging and audit purposes. |
| 2025-12-30 | **Agent-to-agent chat tracking**: `/chat` endpoint now creates `schedule_executions` record when `X-Source-Agent` header is present, ensuring agent-to-agent MCP calls appear in Tasks tab alongside scheduled and manual tasks. |
| 2025-12-30 | **Merged agent-chat.md content**: Added "Chat API Implementation Details" section covering Claude Code CLI execution, stream-JSON parsing, session state management, context window tracking (with bug fixes from 2025-12-11 and 2025-12-19), and chat endpoint session flow. |
| 2025-12-30 | **Line number verification**: Updated all line numbers to match current codebase state |
| 2025-12-29 | Updated chat.py line numbers (106-356) to reflect current codebase after retry helper addition |
| 2025-12-27 | **Service layer refactoring**: Queue endpoint handlers moved to `services/agent_service/queue.py`. Router reduced to thin endpoint definitions. |
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
