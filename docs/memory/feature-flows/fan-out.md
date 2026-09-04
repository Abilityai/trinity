# Feature: Fan-Out Parallel Task Dispatch (FANOUT-001)

## Overview
Dispatches N independent tasks to an agent in parallel (throttled by an asyncio semaphore) and returns aggregated per-task results. Each subtask follows the standard TaskExecutionService path for full dashboard observability.

**Since #2524 the aggregate is a QUERY over `fan_out_id`, not a dict in the dispatching coroutine.** That is what lets a fan-out run on the durable pull queue (a pull-claimed subtask returns no `TaskExecutionResult` to collect), what makes `async_mode` and the status endpoint possible, and what lets a batch outlive the request that started it.

## Recent Changes
- **#2524**: the batch lives on `schedule_executions`. Every subtask row carries `fan_out_id` plus the caller's own `fan_out_task_id` (a new column — the id used to be a dict key in the service's process, which no async batch or status endpoint could reach), and `build_aggregate()` rebuilds `FanOutResult` from those rows. Adds `async_mode` and `GET /api/agents/{name}/fan-out/{fan_out_id}`. The sync caller waits on `sync_waiter.wait_for_fan_out_batch` — the #1081 Phase 4 "sync edge adapter" — woken by the terminal fan-out once the last row is terminal. `fan_out` joins `pull_pilot.PULL_REACHABLE_TRIGGERS`.
- **Issue #418 (feature/418-inter-agent-timeout)**: `timeout_seconds` is now optional and governs only the outer fan-out-wide deadline. Individual subtasks are always bounded by the target agent's configured `execution_timeout_seconds` (TIMEOUT-001). Previously a hardcoded 600s default capped every subtask regardless of per-agent configuration.

## User Story
As an agent orchestrator, I want to fan out multiple independent tasks to an agent in parallel so that embarrassingly parallel workloads (batch predictions, parallel analysis, ensemble methods) complete faster than sequential execution.

## Entry Points
- **API**: `POST /api/agents/{name}/fan-out` -- authenticated endpoint
- **MCP**: `fan_out` tool registered in MCP server

No frontend UI entry point exists; this is an API/MCP-only feature.

## MCP Layer

### Tool Registration
- `src/mcp-server/src/server.ts:192` -- `server.addTool(chatTools.fanOut)`

### Tool Definition
- `src/mcp-server/src/tools/chat.ts:351-457` -- `fan_out` tool
- Parameters: `agent_name`, `tasks[]`, `timeout_seconds` (optional; no default — when omitted, no outer deadline is applied and each sub-task is bounded by the target agent's configured `execution_timeout_seconds`), `max_concurrency`, `model`, `system_prompt`, `allowed_tools`
- Access control: calls `checkAgentAccess()` (same rules as `chat_with_agent`)
- Delegates to `TrinityClient.fanOut()`

### Client Method
- `src/mcp-server/src/client.ts:610-704` -- `fanOut()` method
- Sets headers: `Authorization`, `X-Via-MCP`, `X-Source-Agent`, `X-MCP-Key-ID`, `X-MCP-Key-Name`
- Builds request body with `tasks`, `agent`, `max_concurrency`, `policy`, `model`, `system_prompt`, `allowed_tools`; `timeout_seconds` is conditionally spread in only when the caller provided it, so the backend sees `None` on omission and falls back to per-agent `execution_timeout_seconds`
- HTTP ceiling = `(options?.timeout_seconds ?? 7200) + 60` seconds — covers the platform max per-agent timeout (7200s) + 60s buffer so the HTTP fetch doesn't abort before the backend finishes (#418)
- Calls `POST /api/agents/{name}/fan-out`

## Backend Layer

### Router
- `src/backend/routers/fan_out.py` -- registered in `main.py:41,450`
- Prefix: `/api/agents`, tag: `fan-out`

### Request Validation (Pydantic)
```python
class FanOutRequest(BaseModel):
    tasks: List[FanOutTask]                    # 1-50 tasks, unique IDs
    agent: str = "self"                        # v1: self-only
    timeout_seconds: Optional[int] = None      # 10-3600 when set; None = per-agent default (#418)
    max_concurrency: int = 3                   # 1-10
    policy: str = "best-effort"                # only value supported
    model: Optional[str]
    system_prompt: Optional[str]
    allowed_tools: Optional[List[str]]
```

- Task IDs: regex `^[a-zA-Z0-9_-]{1,64}$`, must be unique
- Max tasks: 50 (`MAX_TASKS`)
- Max concurrency: 10 (`MAX_CONCURRENCY`)
- Timeout range: 10-3600 seconds (or `None` for per-agent default, #418). The validator short-circuits and returns `None` when the field is omitted.
- Policy: only `"best-effort"` supported in v1
- Cross-agent fan-out (`agent != "self"` and `agent != name`): returns 400

### Endpoint Handler
- `src/backend/routers/fan_out.py:126` -- `fan_out()`
- Auth: `get_current_user` + `get_authorized_agent`
- Origin tracking headers: `X-Source-Agent`, `X-Via-MCP`, `X-MCP-Key-ID`, `X-MCP-Key-Name`

### Business Logic Flow
1. Validate `request.agent` is `"self"` or matches path `name` (v1 restriction)
2. Convert `FanOutTask` list to `FanOutTaskInput` dataclasses
3. Determine `source_agent` from header or path name
4. Call `FanOutService.execute()` with all parameters + origin tracking fields
5. Map `FanOutResult` to `FanOutResponse` Pydantic model

### FanOutService
- `src/backend/services/fan_out_service.py:67` -- `FanOutService` class
- Singleton via `get_fan_out_service()` (module-level `_fan_out_service`)

#### `execute()` method (#2524 shape)
1. Generate `fan_out_id` = `fo_{secrets.token_urlsafe(12)}`
2. **Create every execution row first**, each with `fan_out_id` + the caller's `fan_out_task_id`. Ordering is load-bearing: if a subtask could reach a terminal before the rest of the batch had rows, the join would count an incomplete batch and wake the caller early with a partial aggregate.
3. **Spawn** `_dispatch_all` — the batch is not owned by the request. Inside it, `asyncio.Semaphore(max_concurrency)` wraps each `task_service.execute_task()` call, with `triggered_by="fan_out"`, the pre-created `execution_id`, and **`timeout_seconds=None`** so TaskExecutionService resolves the agent's `execution_timeout_seconds` (TIMEOUT-001, #418).
4. `async_mode=True` → return `{fan_out_id, status="accepted", total}` now. Otherwise `await sync_waiter.wait_for_fan_out_batch(...)`.
5. Build the aggregate with `build_aggregate(fan_out_id, order=[t.id ...])` — read from the rows, ordered by the caller's input list.

**`max_concurrency` keeps its meaning, and needed no branch.** The semaphore still wraps the `execute_task` call. On push that call spans the whole turn, so it paces dispatch exactly as before — deleting it would fire N concurrent dispatches at an agent whose `max_parallel_tasks` is 3 and turn the excess into `CapacityFull` failures. Under pull the same call returns in milliseconds (the row is queued, not run), so the semaphore self-releases and real concurrency becomes the agent's worker pool — #1081 Phase 5's "capacity becomes physical", arrived at by construction.

⚠️ **The outer deadline bounds the WAIT, not the work (#2524, contract change).** It used to wrap the `gather` in `asyncio.timeout`, cancelling in-flight subtasks and reporting them `failed`/`timeout`. A queued or claimed row is not the backend's to cancel, and on push that cancellation was always half-illusory — it abandoned the HTTP call while the agent kept running (and billing for) the turn. A still-open subtask now reports **`status="running"`**; the batch still reports `deadline_exceeded`. A caller that branches on the batch status is unaffected; one that treats every non-`completed` subtask as failed sees a third value. **After a deadline the status endpoint is the source of truth**, not the returned aggregate.

#### The join
- `join_fan_out_on_terminal(execution_id)` — called from `event_dispatch_service.spawn_task_terminal_event`, the wrapper every CAS-won terminal writer already goes through (push applier, pull sink, lease reaper, cleanup), beside #2523's loop advance. Deliberately NOT inside `emit_task_terminal_event`, which returns early when no event subscription matches — the common case.
- One PK read on every terminal in the fleet to ask "does this row carry a `fan_out_id`?"; a batch COUNT only when it does.
- Idempotent by construction: it only *signals*, and signalling an absent or already-resolved waiter is a no-op.
- `_dispatch_all` also calls it directly on a non-QUEUED return, because `execute_task`'s fast-fail paths (capacity, circuit-open, ephemeral budget) write a FAILED row without reaching a CAS-won terminal writer, so no terminal event fires for them.

Log line format: `[FanOut] Starting {fan_out_id}: {N} tasks on '{agent}' (concurrency={max_concurrency}, deadline={deadline_desc})` where `deadline_desc` is either `"{N}s"` or `"per-agent"`.

### Data Models
```python
@dataclass
class FanOutTaskInput:
    id: str
    message: str

@dataclass
class FanOutTaskResult:
    id: str
    status: str           # "completed" | "failed"
    response: Optional[str]
    error: Optional[str]
    error_code: Optional[str]
    execution_id: Optional[str]
    cost: Optional[float]
    context_used: Optional[int]
    duration_ms: Optional[int]

@dataclass
class FanOutResult:
    fan_out_id: str
    status: str           # "completed" | "deadline_exceeded" | "accepted" | "running"
    total: int
    completed: int
    failed: int
    results: List[FanOutTaskResult]
```

## Data Layer

### Database Migration
- `_migrate_execution_fan_out_id()` — migration #30, adds `fan_out_id TEXT` to `schedule_executions` + index `idx_executions_fan_out`.
- **#2524, dual-track (Invariant #9)**: `_migrate_execution_fan_out_task_id()` in `db/migrations.py` **and** Alembic `0051_execution_fan_out_task_id` (chained after `0050_agent_loops_terminal_driven`). Adds `fan_out_task_id TEXT` plus a composite `idx_executions_fan_out_status ON schedule_executions(fan_out_id, status)` — the join COUNTs non-terminal rows for one batch on every fan-out terminal, which the single-column index cannot serve without reading the whole batch.

### Model
- `src/backend/db_models.py:170` -- `fan_out_id: Optional[str]` on `ScheduleExecution` dataclass

### Execution Record Creation
- `src/backend/db/schedules.py:451` -- `create_task_execution()` accepts `fan_out_id` parameter
- `src/backend/db/schedules.py:476` -- INSERT includes `fan_out_id` column
- `src/backend/db/schedules.py:128` -- row mapper reads `fan_out_id` from result set

### TaskExecutionService Integration
- `src/backend/services/task_execution_service.py:134` -- `triggered_by="fan_out"` (new trigger type)
- `src/backend/services/task_execution_service.py:146` -- `fan_out_id` parameter passed through to `db.create_task_execution()`
- Each subtask gets its own execution record, capacity slot, and activity tracking via the standard path

## Side Effects
- **Execution Records**: Each subtask creates a `schedule_executions` row with `triggered_by="fan_out"` and shared `fan_out_id`
- **Capacity Slots**: Each subtask acquires/releases a parallel execution slot via `SlotService`
- **Activity Tracking**: Standard activity tracking from `TaskExecutionService` applies per subtask
- **WebSocket**: Standard execution status broadcasts from `TaskExecutionService` apply per subtask
- **No dedicated fan-out WebSocket event**: The fan-out itself does not broadcast; individual subtask events flow through existing channels

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| No tasks provided | 422 | "At least one task is required" |
| Too many tasks (>50) | 422 | "Maximum 50 tasks per fan-out" |
| Duplicate task IDs | 422 | "Duplicate task IDs: {dupes}" |
| Invalid task ID format | 422 | "Task ID must be 1-64 alphanumeric..." |
| Concurrency out of range | 422 | "max_concurrency must be between 1 and 10" |
| Timeout out of range | 422 | "timeout_seconds must be between 10 and 3600" (only validated when field is set; `None`/omitted is accepted) |
| Unsupported policy | 422 | "Only 'best-effort' policy is supported" |
| Cross-agent target | 400 | "Fan-out target must be 'self' or '{name}'" |
| Agent not found | 404 | From `get_authorized_agent` dependency |
| Auth failure | 401 | From `get_current_user` dependency |
| Overall deadline exceeded | 200 | `status: "deadline_exceeded"`, unfinished tasks get `error_code: "timeout"` (only reachable when `timeout_seconds` was explicitly set) |
| Per-subtask timeout (per-agent config) | 200 | Per-task `status: "failed"` with `error_code: "timeout"` from TaskExecutionService; other subtasks continue |
| Individual subtask failure | 200 | Per-task `status: "failed"` with `error` and `error_code` |

## Request/Response Example

### Request
```json
POST /api/agents/my-agent/fan-out
{
  "tasks": [
    {"id": "task-1", "message": "Analyze Q1 revenue"},
    {"id": "task-2", "message": "Analyze Q2 revenue"},
    {"id": "task-3", "message": "Analyze Q3 revenue"}
  ],
  "max_concurrency": 3,
  "timeout_seconds": 300,
  "model": "sonnet"
}
```

### Response
```json
{
  "fan_out_id": "fo_abc123def456",
  "status": "completed",
  "total": 3,
  "completed": 3,
  "failed": 0,
  "results": [
    {
      "id": "task-1",
      "status": "completed",
      "response": "Q1 revenue was...",
      "execution_id": "exec_xyz",
      "cost": 0.05,
      "context_used": 12000,
      "duration_ms": 8500
    },
    ...
  ]
}
```

## Testing

### Prerequisites
- Backend running at `http://localhost:8000`
- At least one running agent

### Test Steps
1. **Action**: Send fan-out request with 3 tasks
   **Expected**: All 3 tasks complete, `status: "completed"`
   **Verify**: `GET /api/agents/{name}/executions` shows 3 records with same `fan_out_id`

2. **Action**: Send fan-out with `max_concurrency: 1`
   **Expected**: Tasks execute sequentially (only 1 at a time)
   **Verify**: Execution timestamps show sequential pattern

3. **Action**: Send fan-out with very short `timeout_seconds: 10` and complex tasks
   **Expected**: `status: "deadline_exceeded"`, unfinished tasks have `error_code: "timeout"`

4. **Action**: Send fan-out with `agent: "other-agent"`
   **Expected**: 400 error "Cross-agent fan-out is not yet supported"

5. **Action**: Send fan-out with duplicate task IDs
   **Expected**: 422 validation error

## Architecture Notes
- Concurrency is managed by `asyncio.Semaphore` -- safe because asyncio is single-threaded (no preemption between awaits)
- `asyncio.gather(return_exceptions=True)` ensures all coroutines complete even if one raises
- `asyncio.timeout()` wraps the entire gather for the overall deadline **only when `timeout_seconds` is set**; otherwise the gather runs unwrapped and each subtask is bounded by per-agent `execution_timeout_seconds` (#418)
- Results dict is safe for concurrent writes in asyncio's cooperative model
- v1 is self-only (agent fans out to itself); cross-agent fan-out is a future extension

## Related Flows
- [task-execution-service.md](task-execution-service.md) -- Each subtask uses the standard execution path
- [parallel-capacity.md](parallel-capacity.md) -- Subtasks consume parallel execution slots
- [parallel-headless-execution.md](parallel-headless-execution.md) -- Similar stateless execution model
- [mcp-orchestration.md](mcp-orchestration.md) -- MCP tool registration
- [AUDIT-001-execution-origin-tracking.md](AUDIT-001-execution-origin-tracking.md) -- Origin tracking headers
