# Feature: Execution Termination

> **Updated 2026-04-26 (#428):** Backend-side capacity cleanup after a successful terminate now calls [`CapacityManager.force_release(agent_name)`](capacity-management.md), which clears both the slot counter and the in-memory queue in one call. Replaces the prior two-step `execution_queue.force_release` + `slot_service.release_slot`.

## Overview

Execution Termination allows users to stop running Claude Code executions mid-flight by sending graceful signals (SIGINT) to the subprocess, with fallback to force kill (SIGKILL) if needed. This feature enables users to cancel long-running or stuck tasks without waiting for them to complete.

## User Story

As an agent operator, I want to stop a running task execution so that I can cancel stuck or unnecessary operations and reclaim agent resources.

## Entry Points

- **UI (Stop)**: `src/frontend/src/components/TasksPanel.vue:243-258` - Stop button for running tasks
- **UI (Live)**: `src/frontend/src/components/TasksPanel.vue:213-232` - Live button navigates to Execution Detail page (see [execution-detail-page.md](execution-detail-page.md))
- **API (Backend)**: `POST /api/agents/{name}/executions/{execution_id}/terminate?task_execution_id={id}`
- **API (Agent)**: `POST /api/executions/{execution_id}/terminate`
- **API (Agent)**: `GET /api/executions/running`

---

## Architecture

```
+---------------------------+      +---------------------------+      +----------------------------+
|       TasksPanel.vue      |      |     Backend chat.py       |      |   Agent agent_server/      |
|---------------------------|      |---------------------------|      |----------------------------|
| Stop Button (running task)|----->| POST /{name}/executions/  |----->| POST /api/executions/      |
|                           |      | {execution_id}/terminate  |      | {execution_id}/terminate   |
|---------------------------|      |---------------------------|      |----------------------------|
| terminateTask()           |      | Proxy to agent container  |      | ProcessRegistry.terminate()|
| loadRunningExecutions()   |      | Update DB: cancelled      |      | SIGINT -> wait 5s -> SIGKILL
+---------------------------+      | Track activity            |      +----------------------------+
                                   +---------------------------+
```

---

## Unified Execution ID Flow

The execution ID is now unified across the entire system. The backend generates the database `execution_id` and passes it to the agent container, which uses it for process registry. This ensures the same ID is used for:

1. **Database tracking**: `task_executions.id` column
2. **Process registry**: Key for subprocess lookup and termination
3. **API termination**: URL parameter matches both database and registry

### Flow Diagram

```
Backend (chat.py)                       Agent Container (claude_code.py)
+-------------------------------+       +----------------------------------+
| 1. Create task_execution      |       |                                  |
|    execution = db.create_     |       |                                  |
|      task_execution(...)      |       |                                  |
|    execution_id = execution.id|       |                                  |
|                               |       |                                  |
| 2. Pass to agent in payload   |------>| 3. Use execution_id for registry |
|    payload = {                |       |    task_session_id = execution_id|
|      "execution_id": exec_id  |       |      or str(uuid.uuid4())        |
|    }                          |       |                                  |
|                               |       | 4. Register subprocess           |
|                               |       |    registry.register(            |
|                               |       |      task_session_id, process)   |
+-------------------------------+       +----------------------------------+
```

---

## Agent Layer

### Process Registry (`docker/base-image/agent_server/services/process_registry.py`)

Thread-safe registry for tracking running subprocess handles.

**Class**: `ProcessRegistry` (lines 18-163)

| Method | Description |
|--------|-------------|
| `register(execution_id, process, metadata)` | Register a running subprocess with metadata |
| `unregister(execution_id)` | Remove a completed process from registry |
| `terminate(execution_id, graceful_timeout=5)` | Graceful SIGINT -> SIGKILL termination |
| `get_status(execution_id)` | Get status of a registered process |
| `list_running()` | List all currently running executions |
| `cleanup_finished()` | Remove entries for finished processes |

**Termination Flow**:
```python
def terminate(self, execution_id: str, graceful_timeout: int = 5) -> dict:
    # 1. Check if process exists
    if not entry:
        return {"success": False, "reason": "not_found"}

    # 2. Check if already finished
    if process.poll() is not None:
        return {"success": False, "reason": "already_finished", "returncode": ...}

    # 3. Graceful termination (SIGINT = Ctrl+C) — signal the whole
    #    process group via captured pgid so hook grandchildren go too
    #    (Issue #407). Falls back to signaling just the parent if pgid
    #    is unknown.
    pgid = (entry.get("metadata") or {}).get("pgid")
    _signal_process_tree(process, signal.SIGINT, pgid=pgid)

    try:
        process.wait(timeout=graceful_timeout)  # Wait 5 seconds
    except subprocess.TimeoutExpired:
        # 4. Force kill the group if graceful didn't work
        _signal_process_tree(process, signal.SIGKILL, pgid=pgid)
        process.wait(timeout=2)

    return {"success": True, "returncode": process.returncode}
```

### Process Registration in Claude Code (`docker/base-image/agent_server/services/claude_code.py`)

Both chat and headless task paths use the same pattern: use the backend-provided `execution_id` if available, otherwise generate a new UUID. This ensures the process registry key matches the database execution ID, enabling termination from the UI.

**Conversational Chat** (lines 468-536):
```python
# Use provided execution_id if available (enables termination tracking from backend)
execution_id = execution_id or str(uuid.uuid4())

# Register process after Popen
registry = get_process_registry()
registry.register(execution_id, process, metadata={
    "type": "chat",
    "message_preview": prompt[:100]
})

try:
    # ... execute and parse response ...
    metadata.execution_id = execution_id  # Include in response metadata
finally:
    # Always unregister when done
    registry.unregister(execution_id)
```

**Headless Task** (lines 625-655):
```python
# Use provided execution_id if available (from backend database)
task_session_id = execution_id or str(uuid.uuid4())

registry = get_process_registry()
registry.register(task_session_id, process, metadata={
    "type": "task",
    "message_preview": prompt[:100]
})

try:
    # ... execute and parse response ...
    metadata.execution_id = task_session_id  # Include in response metadata
finally:
    registry.unregister(task_session_id)
```

### Agent Server Endpoints (`docker/base-image/agent_server/routers/chat.py`)

**POST /api/chat** (lines 24-51):
```python
@router.post("/api/chat")
async def chat(request: ChatRequest):
    # Execute via runtime adapter — passes execution_id for process registry
    runtime = get_runtime()
    response_text, execution_log, metadata, raw_messages = await runtime.execute(
        prompt=request.message,
        model=effective_model,
        execution_id=request.execution_id  # Use provided ID for process registry
    )
```

**POST /api/task** (lines 94-135):
```python
@router.post("/api/task")
async def execute_task(request: ParallelTaskRequest):
    # Execute via runtime adapter in headless mode
    # Pass execution_id from request (provided by backend)
    runtime = get_runtime()
    response_text, raw_messages, metadata, session_id = await runtime.execute_headless(
        prompt=request.message,
        model=request.model,
        execution_id=request.execution_id  # Use provided ID for process registry
    )
```

**POST /api/executions/{execution_id}/terminate** (lines 242-261):
```python
@router.post("/api/executions/{execution_id}/terminate")
async def terminate_execution(execution_id: str):
    registry = get_process_registry()
    result = registry.terminate(execution_id)

    if result["success"]:
        return {"status": "terminated", "execution_id": execution_id}
    elif result["reason"] == "not_found":
        raise HTTPException(status_code=404, detail="Execution not found")
    elif result["reason"] == "already_finished":
        return {"status": "already_finished", "execution_id": execution_id, "returncode": ...}
    else:
        raise HTTPException(status_code=500, detail=result.get("error", "Termination failed"))
```

**GET /api/executions/running** (lines 264-272):
```python
@router.get("/api/executions/running")
async def list_running_executions():
    registry = get_process_registry()
    return {"executions": registry.list_running()}
```

---

## Backend Layer

### Proxy Endpoints (`src/backend/routers/chat.py`)

**Passing Execution ID to Agent** — Both `/api/chat` and `/api/task` paths pass the database execution ID to the agent container so the process registry uses the same ID:

```python
# /api/chat path (lines 236-243):
payload = {"message": request.message, "stream": False}
payload["system_prompt"] = get_platform_system_prompt()
if task_execution_id:
    payload["execution_id"] = task_execution_id  # Enables termination tracking

# /api/task path (lines 462-470):
payload = {
    "message": request.message,
    "execution_id": execution_id  # Database execution ID for process registry
}
```

**Status Preservation on Termination** (lines 567-616):

The backend error handlers check if an execution was already cancelled before overwriting the status. This prevents the "cancelled" status from being replaced with "failed" when the HTTP request naturally fails after termination.

```python
# Timeout handler (lines 567-578)
except httpx.TimeoutException:
    if execution_id:
        # Check if already cancelled - don't overwrite with timeout
        existing = db.get_execution(execution_id)
        if existing and existing.status == "cancelled":
            logger.info(f"Execution {execution_id} already cancelled, not overwriting with timeout status")
        else:
            db.update_execution_status(execution_id, status="failed", error="...")

# HTTP error handler (lines 591-616)
except httpx.HTTPError as e:
    if execution_id:
        # Check if execution was already cancelled (terminated by user)
        existing = db.get_execution(execution_id)
        if existing and existing.status == "cancelled":
            logger.info(f"Execution {execution_id} already cancelled, not overwriting with failed status")
        else:
            db.update_execution_status(execution_id, status="failed", error=error_msg)
```

**POST /api/agents/{name}/executions/{execution_id}/terminate** (lines 1363-1443):
```python
@router.post("/{name}/executions/{execution_id}/terminate")
async def terminate_agent_execution(
    name: str,
    execution_id: str,
    task_execution_id: Optional[str] = None,  # Defaults to execution_id
    current_user: User = Depends(get_current_user)
):
    # execution_id IS the database execution ID (unified with process registry)
    if not task_execution_id:
        task_execution_id = execution_id

    # 1. Validate agent exists and is running
    # 2. Proxy termination request to agent container
    # 3. Clear queue state + release capacity slot
    # 4. Update database execution record to "cancelled"
    # 4b. #1332: close the open dispatch activity as ActivityState.CANCELLED
    #     (get_open_activity_id_for_execution + complete_activity). The terminate
    #     path writes the CANCELLED row first and never reaches apply_result, so
    #     without this the dispatch activity is left `started` until the ~30-min
    #     stale-activity reaper flips it to FAILED — collapsing a user-cancel into
    #     a failure in activity-derived views (timeline/replay). Best-effort:
    #     a close failure is logged, never fails the terminate request.
    #     The activity state is GATED on the CANCELLED row-write CAS bool: if the
    #     write LOST (row already terminal via a concurrent path), the activity is
    #     closed in the row's REAL state via activity_state_for_terminal(existing)
    #     so the activity never disagrees with the row.
    # 5. Track activity
```

---

## Frontend Layer

### TasksPanel.vue (`src/frontend/src/components/TasksPanel.vue`)

**Stop Button UI** (lines 242-258):
```vue
<!-- Stop Button (for running tasks) -->
<button
  v-if="task.status === 'running' && task.execution_id"
  @click="terminateTask(task)"
  :disabled="terminatingTaskId === task.id"
  class="p-1.5 text-gray-400 hover:text-red-600 ..."
  title="Stop execution"
>
  <!-- Spinner when terminating, stop icon otherwise -->
</button>
```

**Cancelled Status Badge** (lines 138-158):
```vue
<!-- Status badge with cancelled (orange) styling -->
<span
  :class="[
    'inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium',
    task.status === 'success' ? 'bg-green-100 ... text-green-800 ...' :
    task.status === 'failed' ? 'bg-red-100 ... text-red-800 ...' :
    task.status === 'cancelled' ? 'bg-orange-100 ... text-orange-800 ...' :
    task.status === 'running' ? 'bg-yellow-100 ... text-yellow-800 ...' :
    'bg-gray-100 ...'
  ]"
>
```

**State** (lines 485, 494):
```javascript
const terminatingTaskId = ref(null)        // ID of task being terminated
const runningExecutions = ref([])           // Running executions from agent
```

**Load Running Executions with Improved Polling** (lines 598-623):
```javascript
async function loadQueueStatus() {
  // ... load queue status ...

  // Fetch running executions for termination support if:
  // 1. Queue is busy (scheduled/queued tasks), OR
  // 2. We have local running tasks (manual tasks via /task endpoint bypass queue)
  const hasLocalRunningTasks = pendingTasks.value.some(t => t.status === 'running')
  if (response.data.is_busy || hasLocalRunningTasks) {
    await loadRunningExecutions()
  } else {
    runningExecutions.value = []
  }
}
```

**Execution ID Matching** (lines 513-533):
```javascript
// Enhance running tasks with execution_id from runningExecutions
const enhanceWithExecutionId = (task) => {
  if (task.status !== 'running' || task.execution_id) {
    return task
  }
  // Match by message preview (first 100 chars)
  const messagePreview = task.message.substring(0, 100)
  const match = runningExecutions.value.find(e =>
    e.metadata?.message_preview === messagePreview
  )
  if (match) {
    return { ...task, execution_id: match.execution_id }
  }
  // If only one running execution, assume it's this task
  if (runningExecutions.value.length === 1) {
    return { ...task, execution_id: runningExecutions.value[0].execution_id }
  }
  return task
}
```

**Terminate Task with Unified ID** (lines 754-789):
```javascript
async function terminateTask(task) {
  if (!task.execution_id) {
    console.error('No execution_id available for termination')
    return
  }

  terminatingTaskId.value = task.id

  try {
    // Pass task_execution_id as query param so backend can update database record
    // With unified ID, execution_id and task_execution_id are the same
    await axios.post(
      `/api/agents/${props.agentName}/executions/${task.execution_id}/terminate?task_execution_id=${task.execution_id}`,
      {},
      { headers: authStore.authHeader }
    )

    // Update task status locally to 'cancelled' while we wait for refresh
    const idx = pendingTasks.value.findIndex(t => t.id === task.id)
    if (idx !== -1) {
      pendingTasks.value[idx].status = 'cancelled'
      pendingTasks.value[idx].error = 'Execution terminated by user'
    }

    // Refresh data to get updated status
    await loadAllData()
  } catch (error) {
    console.error('Failed to terminate task:', error)
  } finally {
    terminatingTaskId.value = null
  }
}
```

---

## Data Models

### ChatRequest (`docker/base-image/agent_server/models.py:19-24`)
```python
class ChatRequest(BaseModel):
    message: str
    stream: bool = False
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    execution_id: Optional[str] = None  # Database execution ID (enables termination tracking)
```

### ParallelTaskRequest (`docker/base-image/agent_server/models.py:198-207`)
```python
class ParallelTaskRequest(BaseModel):
    message: str
    model: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    system_prompt: Optional[str] = None
    timeout_seconds: Optional[int] = 900
    max_turns: Optional[int] = None
    execution_id: Optional[str] = None  # Database execution ID (used for process registry if provided)
```

### ExecutionMetadata (`docker/base-image/agent_server/models.py:75-89`)
```python
class ExecutionMetadata(BaseModel):
    # ... other fields ...
    execution_id: Optional[str] = None  # Unique ID for process registry (termination)
```

### ActivityType (`src/backend/models.py:135`)
```python
class ActivityType(str, Enum):
    # ... other types ...
    EXECUTION_CANCELLED = "execution_cancelled"
```

---

## Side Effects

### Activity Tracking

When a task is terminated, an activity record is created:
```python
await activity_service.track_activity(
    agent_name=name,
    activity_type=ActivityType.EXECUTION_CANCELLED,
    user_id=current_user.id,
    triggered_by="user",
    related_execution_id=task_execution_id,
    details={
        "execution_id": execution_id,
        "task_execution_id": task_execution_id,
        "status": "terminated" | "already_finished",
        "returncode": int  # Process exit code
    }
)
```

### Queue State

The execution queue is force-released after successful termination to allow new executions:
```python
queue = get_execution_queue()
await queue.force_release(name)
```

### Database Status Preservation

When termination succeeds:
1. Backend updates `task_executions.status` to "cancelled"
2. Error handlers in `/task` endpoint check existing status before overwriting
3. If already "cancelled", the "failed" status is not written

---

## Signal Handling

| Signal | Purpose | Scope | Timeout |
|--------|---------|-------|---------|
| `SIGINT` | Graceful termination (Ctrl+C) | Full process group | 5 seconds |
| `SIGKILL` | Force kill (after SIGINT timeout) | Full process group | 2 seconds |

Claude Code handles SIGINT gracefully, finishing its current operation before exiting. This allows tools like `Write` or `Bash` to complete their current action.

**Process-group semantics (Issue #407)**: The claude subprocess is launched with `start_new_session=True` so it leads its own process group. Hook subprocesses (bash-guardrail, file-guardrail, output-scanner, …) and any grandchildren they fork are in the same group. Termination sends signals to the group via `os.killpg(pgid, …)` so hook grandchildren don't outlive claude and keep our stdout/stderr pipes open (which previously caused `readline()` to wedge, leaving a `<defunct>` zombie and pinning agent-server CPU until the container was restarted).

The pgid is captured at spawn time (before `process.wait()` reaps the parent — after that, `os.getpgid(pid)` raises) and stored in the registry entry metadata so termination can signal the full tree even after the parent has exited. See `docker/base-image/agent_server/utils/subprocess_pgroup.py` for the helper functions (`capture_pgid`, `terminate_process_group`, `signal_process_tree`, `drain_reader_threads`).

### Slow-path drain observability (#586)

`drain_reader_threads` emits a structured `[METRIC] drain_outcome` log line once per call **only on the slow path** — when reader threads are still alive after the initial grace window and the post-kill grace is engaged. The fast path (reader exits during initial grace) emits **no** metric: silence is the success signal, so any emission is operationally meaningful.

Three emission sites in `subprocess_pgroup.py` map to three `outcome=` values:

| `outcome=` | Site | Meaning |
|---|---|---|
| `natural` | Site B | Reader joined during the post-kill grace window — `terminate_process_group` reaped the claude pgid; the `kill_cgroup_orphans()` sweep in `finally` mops up any grandchildren that escaped via `setsid()` (e.g. `ssh` from `git push`) before the kernel EOFs the pipe. |
| `force_close` | Site C | Drain budget exceeded, pipes force-closed; reader thread did exit cleanly afterwards (no daemon leak). |
| `leaked` | Site C | Drain budget exceeded **and** daemon reader threads survived force-close. Bug-class regression signal — operators should alert on a non-zero rate. |

Fields surfaced for operator queries: `pid`, `pgid`, `outcome`, `drain_elapsed_ms`, `stuck_initial`, `orphan_kill_count` (vestigial since the #817 follow-up cgroup-sweep refactor — always `0`; the actual orphan-kill count is logged separately as `Cgroup sweep killed N orphan(s) after drain` and can be correlated by `pid`), and optional `leaked_count` (present only on `outcome=leaked`).

**Fleet audit**: `scripts/586-fleet-check.sh` scans Vector-aggregated agent logs (`/data/logs/agents-*.json`) across the configurable lookback window (default 7d), prints a per-container summary of `[METRIC] drain_outcome` and related slow-path events, and exits non-zero if any residual `still stuck after Ns` or `no result message after` events are found — gating the Issue #586 close-out.

**Authoring escape hatch**: Stop hooks that release the inherited stdout FD before any blocking I/O never enter the slow path at all. See the `TRINITY_COMPATIBLE_AGENT_GUIDE.md` cross-reference below for bash/python/node patterns.

For Stop-hook authoring guidance to avoid the orphan-killer slow path, see [`TRINITY_COMPATIBLE_AGENT_GUIDE.md` → "Stop hook authoring — release inherited stdout"](../../TRINITY_COMPATIBLE_AGENT_GUIDE.md#stop-hook-authoring--release-inherited-stdout).

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Agent not found | 404 | Agent not found |
| Agent not running | 503 | Agent is not running |
| Execution not found | 404 | Execution not found |
| Already finished | 200 | status: "already_finished" |
| Connection error | 502 | Failed to connect to agent |
| Timeout | 504 | Timeout connecting to agent |
| Termination error | 500 | Termination failed |

---

## Security Considerations

1. **Authentication**: All termination endpoints require valid JWT token via `get_current_user`
2. **Authorization**: Only users with agent access can terminate executions (implicit via agent container access)
3. **Queue Protection**: Queue is force-released after termination to prevent stuck state
4. **Audit Trail**: All terminations are tracked in activity log with user ID

---

## Related Flows

- **Upstream**: [execution-queue.md](execution-queue.md) - Queue management and execution tracking
- **Upstream**: [parallel-headless-execution.md](parallel-headless-execution.md) - Task execution that generates execution_id
- **Related**: [tasks-tab.md](tasks-tab.md) - UI component displaying stop button
- **Related**: [activity-stream.md](activity-stream.md) - Activity tracking for cancellations

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-03-22 | Fix: `/api/chat` path now passes `execution_id` to agent (was only `/api/task`). Added `execution_id` to `ChatRequest` model, `execute()` runtime interface, and backend chat payload. Terminate endpoint defaults `task_execution_id` to `execution_id` for DB update. |
| 2026-01-13 | Added "Live" button entry point (lines 213-232) - green badge with pulsing dot for running tasks, navigates to Execution Detail page for real-time monitoring |
| 2026-01-13 | Updated: Unified execution ID flow (backend passes to agent), status preservation in error handlers, frontend polling improvements, cancelled status styling |
| 2026-01-12 | Initial documentation - Execution Termination feature |
