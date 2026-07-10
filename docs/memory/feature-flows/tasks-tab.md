# Feature: Tasks Tab

## Overview

The Tasks tab provides a unified view of all agent executions within the Agent Detail page. It consolidates all execution types — chat, manual tasks, scheduled, agent-to-agent, MCP, paid, and public — into a single interface with the ability to trigger new tasks directly, monitor running tasks and queue status in real-time, and re-run historical tasks.

**Performance Optimization (2026-02-21)**: The list endpoint now returns lightweight `ExecutionSummary` objects (excluding large `response`, `error`, `tool_calls`, `execution_log` fields). Task details are fetched on-demand when users expand a task row. This reduces data transfer by 50-100x for agents with many executions.

**Log Format Standardization (2025-01-02)**: All execution types (manual tasks, scheduled executions, manual triggers) now use the `/api/task` endpoint which returns raw Claude Code `stream-json` format. This ensures the [Execution Log Viewer](execution-log-viewer.md) can properly render all execution transcripts.

## User Story

As an agent operator, I want to view all agent executions (including chats) from a single location so that I can track total workload, costs, and history without querying multiple tables.

## Entry Points

- **UI**: `src/frontend/src/views/AgentDetail.vue:201-204` - Tasks tab button
- **UI**: `src/frontend/src/components/TasksPanel.vue` - Main component
- **API**: `POST /api/agents/{name}/task` - Execute a parallel task (creates execution record)
- **API**: `POST /api/agents/{name}/chat` - All chat calls create execution records: user chat (`triggered_by=chat`), MCP (`triggered_by=mcp`), agent-to-agent (`triggered_by=agent`) *(unified #96)*
- **API**: `GET /api/agents/{name}/executions` - Get execution summaries (lightweight, excludes large fields)
- **API**: `GET /api/agents/{name}/executions/{id}` - Get full execution details (on-demand)
- **API**: `GET /api/agents/{name}/executions/{execution_id}/log` - Get full execution log *(added 2025-12-31)*
- **API**: `GET /api/agents/{name}/queue` - Get queue status
- **API**: `POST /api/agents/{name}/queue/clear` - Clear queued tasks
- **API**: `POST /api/agents/{name}/queue/release` - Force release queue

---

## Frontend Layer

### Components

**AgentDetail.vue:97-100** - Tab content rendering (#1500 — fullscreen flex-fill):
```vue
<!-- Tasks Tab Content (#1500: fullscreen flex-fill like Chat; padding
     lives on TasksPanel's scroll root so content scrolls under it) -->
<div v-if="activeTab === 'tasks'" class="flex-1 min-h-0 flex flex-col overflow-hidden">
  <TasksPanel :agent-name="agent.name" :agent-status="agent.status" ... />
</div>
```

**Fullscreen fill layout (#1500)**: `tasks` is a member of `FULLSCREEN_TABS`
(`AgentDetail.vue`, alongside `chat`), so the page enters the `h-screen
overflow-hidden` fullscreen layout and the tab card flex-fills the viewport.
Inside it, `TasksPanel.vue`'s root (`p-6 flex-1 flex flex-col gap-6
overflow-y-auto`) is both the fill column and the **short-viewport fallback
scroller** — the load-bearing piece: without it, fixed chrome (header + stats +
composer) plus the list's min-height would hard-clip the composer, since the
page itself cannot scroll in fullscreen mode. The task-history card is `flex-1
min-h-96 flex flex-col` — the old `max-h-96` list **cap** is retained as the
new **floor** (never fewer visible rows than the pre-#1500 layout at any
viewport height); the list inside is the `flex-1 min-h-0 overflow-y-auto`
scroll region, and loading/empty states center vertically in the filled card.
Anchors `data-testid="task-history-card"` / `"task-list"` exist for e2e.
Regression spec: `src/frontend/e2e/agent-detail-tasks-fill-height.spec.js`
(floor ≥ old cap, growth-with-viewport, page-scroll containment, #954-class
width parity, short-viewport composer reachability).

**TasksPanel.vue** - Main tasks component with the following sections:
- **Header (lines 4-61)**: Title, trigger type filter dropdown, queue status indicator, refresh button
- **Summary Stats (lines 63-83)**: Total tasks, success rate, total cost, avg duration - Compact layout with smaller padding (`px-3 py-2`) and text (`text-base`, `text-[10px]`)
- **New Task Input (lines 85-134)**: Model + Timeout selectors + textarea for task message, Run button - Uses `items-stretch` so Run button height matches textarea
  - **Model Selector (line 90, MODEL-001)**: `ModelSelector` component, persisted per-agent in localStorage
  - **Timeout Selector (lines 93-104)**: Dropdown for `timeout_seconds` (5min/15min/30min/1hr/2hr), persisted per-agent in localStorage (default: 15min/900s)
  - **Keyboard shortcuts**: Enter to submit, Shift+Enter for newline, Cmd/Ctrl+Enter also works
- **Task History**: Scrollable list of all tasks with expand/collapse — fills remaining viewport height (`flex-1 min-h-96`, #1500; formerly capped at `max-h-96`)
  - Row highlight: `highlightExecutionId` prop causes ring highlight and auto-scroll
  - **Action Buttons per Task** (lines 264-365):
    - Open Execution Detail (lines 265-287): External link / Live button for running tasks
    - View Log (lines 288-298): Modal for execution transcript
    - Copy Input (lines 299-308): Copy task message to clipboard
    - **Stop Execution** (lines 309-325): Terminate running task
    - Re-run (lines 326-337): Repeat the same task
    - **Make Repeatable** (lines 338-348): Create schedule from task message (calendar icon)
    - Expand/Collapse (lines 349-365): Show/hide task details
- **Execution Log Modal (lines 371-487)**: Modal for viewing execution transcript
- **Queue Management (lines 489-507)**: Force release and clear queue buttons

### Events Emitted

**TasksPanel.vue:539** - Component events:
```javascript
const emit = defineEmits(['create-schedule'])
```

- `create-schedule`: Emitted when user clicks "Make Repeatable" button, passes task message

### State Management

**TasksPanel.vue:520-580** - Local reactive state:
```javascript
const props = defineProps({
  agentName: { type: String, required: true },
  agentStatus: { type: String, default: 'stopped' },
  highlightExecutionId: { type: String, default: null },
  initialMessage: { type: String, default: '' }
})

// State
const executions = ref([])           // Server-persisted executions (ExecutionSummary - lightweight)
const pendingTasks = ref([])         // Local tasks awaiting server response
const queueStatus = ref(null)        // Current queue status
const loading = ref(true)
const newTaskMessage = ref('')       // New task input
const taskLoading = ref(false)       // Submit in progress
const expandedTaskId = ref(null)     // Currently expanded task
const expandLoadingTaskId = ref(null)  // PERF-001: Track which task is loading details
const taskDetailsCache = ref({})     // PERF-001: Cache for task details (response/error)
const terminatingTaskId = ref(null)  // Task being terminated
const triggerFilter = ref('all')     // Filter by triggered_by type
const runningExecutions = ref([])    // Running executions from agent (for termination)

// Model selection (MODEL-001) - persisted per-agent in localStorage
const taskModelKey = computed(() => `trinity-task-model-${props.agentName}`)
const selectedModel = ref(localStorage.getItem(`trinity-task-model-${props.agentName}`) || 'claude-opus-4-5-20251101')
watch(selectedModel, (val) => { localStorage.setItem(taskModelKey.value, val) })

// Timeout selection - persisted per-agent in localStorage (default: 900s / 15min)
const taskTimeoutKey = computed(() => `trinity-task-timeout-${props.agentName}`)
const taskTimeout = ref(parseInt(localStorage.getItem(`trinity-task-timeout-${props.agentName}`)) || 900)
watch(taskTimeout, (val) => { localStorage.setItem(taskTimeoutKey.value, val) })
```

**Computed Properties (lines 590-643)**:
```javascript
// Combine local pending tasks with server executions for seamless UX
const allTasks = computed(() => {
  const pending = pendingTasks.value.filter(p => {
    return !executions.value.some(e => e.message === p.message && e.started_at === p.started_at)
  })

  // enhanceWithExecutionId: matches running tasks to process registry entries for Stop button
  // Falls back to single-running-execution assumption when message preview matching fails
  const enhanceWithExecutionId = (task) => { /* ... */ }

  let tasks = [...pending.map(enhanceWithExecutionId), ...executions.value.map(enhanceWithExecutionId)]

  // Apply trigger type filter
  if (triggerFilter.value !== 'all') {
    tasks = tasks.filter(t => t.triggered_by === triggerFilter.value)
  }

  return tasks
})

const successRate = computed(() => { /* ... */ })
const totalCost = computed(() => { /* ... */ })
const avgDuration = computed(() => { /* ... */ })
```

### API Calls

**Load Executions (lines 656-670)** - Returns lightweight `ExecutionSummary`:
```javascript
async function loadExecutions() {
  // PERF-001: Returns ExecutionSummary (excludes response, error, tool_calls, execution_log)
  const response = await axios.get(`/api/agents/${props.agentName}/executions?limit=100`, {
    headers: authStore.authHeader
  })
  executions.value = response.data

  // If highlightExecutionId is set, auto-expand and scroll to that task
  if (props.highlightExecutionId) {
    scrollToHighlightedTask()
  }
}
```

**Fetch Task Details on Expand (lines 843-881)** - PERF-001 on-demand loading:
```javascript
// PERF-001: Fetch task details when user expands a task row
async function fetchTaskDetails(taskId) {
  // Skip local tasks (no server record yet)
  if (taskId.startsWith('local-')) {
    return
  }

  // Check cache first
  if (taskDetailsCache.value[taskId] !== undefined) {
    return
  }

  // Fetch full details from server
  expandLoadingTaskId.value = taskId
  try {
    const response = await axios.get(`/api/agents/${props.agentName}/executions/${taskId}`, {
      headers: authStore.authHeader
    })
    // Cache response and error fields
    taskDetailsCache.value[taskId] = {
      response: response.data.response,
      error: response.data.error
    }
  } catch (error) {
    console.error('Failed to load task details:', error)
    taskDetailsCache.value[taskId] = { response: null, error: null }
  } finally {
    expandLoadingTaskId.value = null
  }
}
```

**Load Queue Status (lines 690-715)**:
```javascript
async function loadQueueStatus() {
  if (props.agentStatus !== 'running') {
    queueStatus.value = null
    runningExecutions.value = []
    return
  }
  const response = await axios.get(`/api/agents/${props.agentName}/queue`, {
    headers: authStore.authHeader
  })
  queueStatus.value = response.data

  // Also fetch running executions when queue is busy OR when local tasks are running
  // (manual tasks bypass the queue, so is_busy may be false even with active tasks)
  const hasLocalRunningTasks = pendingTasks.value.some(t => t.status === 'running')
  if (response.data.is_busy || hasLocalRunningTasks) {
    await loadRunningExecutions()
  } else {
    runningExecutions.value = []
  }
}
```

**Run New Task (lines 734-818)**:
```javascript
async function runNewTask() {
  // Create local pending task for immediate UI feedback
  const localTask = {
    id: 'local-' + Date.now(),
    message: taskMessage,
    status: 'running',
    triggered_by: 'manual',
    started_at: new Date().toISOString(),
    // ... other fields
  }
  pendingTasks.value.unshift(localTask)

  // Poll for running executions at 500ms and 2s after start (enables Stop button)
  setTimeout(() => loadQueueStatus(), 500)
  setTimeout(() => loadQueueStatus(), 2000)

  // Submit to server (MODEL-001: include selected model; timeout_seconds from selector)
  const payload = { message: taskMessage, timeout_seconds: taskTimeout.value }
  if (selectedModel.value) {
    payload.model = selectedModel.value
  }
  const response = await axios.post(`/api/agents/${props.agentName}/task`, payload, {
    headers: authStore.authHeader })

  // Update local task with response data
  // Refresh server executions
  await loadExecutions()
  loadQueueStatus()
}
```

**Make Repeatable (lines 827-830)**:
```javascript
// Create schedule from task (make repeatable)
function makeRepeatable(task) {
  emit('create-schedule', task.message)
}
```

This emits the `create-schedule` event with the task message, which is handled by AgentDetail.vue to switch to the Schedules tab with the message pre-filled.

**Queue Management (lines 1055-1083)**:
```javascript
async function forceReleaseQueue() {
  await axios.post(`/api/agents/${props.agentName}/queue/release`, {}, {
    headers: authStore.authHeader
  })
  await loadQueueStatus()
}

async function clearQueue() {
  await axios.post(`/api/agents/${props.agentName}/queue/clear`, {}, {
    headers: authStore.authHeader
  })
  await loadQueueStatus()
}
```

### Execution Termination (Added 2026-01-12)

Running tasks can be stopped via the Stop button, which terminates the Claude Code subprocess gracefully.

**Load Running Executions** (lines 717-732):
```javascript
async function loadRunningExecutions() {
  if (props.agentStatus !== 'running') {
    runningExecutions.value = []
    return
  }
  const response = await axios.get(`/api/agents/${props.agentName}/executions/running`, {
    headers: authStore.authHeader
  })
  runningExecutions.value = response.data.executions || []
}
```

**Execution ID Matching** (inside `allTasks` computed, lines 598-616):
```javascript
// Enhance running tasks with execution_id from runningExecutions
const enhanceWithExecutionId = (task) => {
  if (task.status !== 'running' || task.execution_id) {
    return task
  }
  // Match by message preview (first 100 chars)
  const messagePreview = task.message.substring(0, 100)
  const match = runningExecutions.value.find(e =>
    e.metadata?.message_preview === messagePreview ||
    e.metadata?.message_preview?.startsWith(messagePreview.substring(0, 50))
  )
  if (match) return { ...task, execution_id: match.execution_id }
  // Fallback: if only one running execution exists, assume it's this task
  if (runningExecutions.value.length === 1) {
    return { ...task, execution_id: runningExecutions.value[0].execution_id }
  }
  return task
}
```

**Terminate Task** (lines 888-924):
```javascript
async function terminateTask(task) {
  if (!task.execution_id) return

  terminatingTaskId.value = task.id
  try {
    // task_execution_id query param lets backend update the database record
    await axios.post(
      `/api/agents/${props.agentName}/executions/${task.execution_id}/terminate?task_execution_id=${task.execution_id}`,
      {},
      { headers: authStore.authHeader }
    )
    // Update local pending task status immediately
    const idx = pendingTasks.value.findIndex(t => t.id === task.id)
    if (idx !== -1) pendingTasks.value[idx].status = 'cancelled'
    // Refresh data to get updated status
    await loadAllData()
  } finally {
    terminatingTaskId.value = null
  }
}
```

See [execution-termination.md](execution-termination.md) for full feature documentation.

### Polling

Queue status is polled every 5 seconds when agent is running (watch handler on agentStatus prop).

---

## Backend Layer

### Endpoints

#### POST /api/agents/{name}/task

**File**: `src/backend/routers/chat.py:358-569`

Execute a stateless task in parallel mode (no conversation context).

**Request Model** (`src/backend/models.py`):
```python
class ParallelTaskRequest(BaseModel):
    message: str  # The task to execute
    model: Optional[str] = None  # Model override: sonnet, opus, haiku
    allowed_tools: Optional[List[str]] = None  # Tool restrictions
    system_prompt: Optional[str] = None  # Additional instructions
    timeout_seconds: Optional[int] = 300  # Execution timeout
```

**Business Logic (lines 358-569)**:
1. Validate agent container exists and is running
2. Determine execution source (user or agent-to-agent via `X-Source-Agent` header)
3. **Create task execution record in database** via `db.create_task_execution()`
4. Track activity via `activity_service.track_activity()`
5. If agent-to-agent, broadcast collaboration event via WebSocket
6. Forward request to agent's internal `/api/task` endpoint
7. Update execution record with result (success/failure, cost, duration)
8. Log audit event

**Key difference from /chat**: This endpoint does NOT use the execution queue - tasks run in parallel.

#### GET /api/agents/{name}/executions

**File**: `src/backend/routers/schedules.py:435-459`

Get execution summaries for an agent - optimized for list views.

**PERF-001 (2026-02-21)**: Returns lightweight `ExecutionSummary` objects that exclude large text fields (`response`, `error`, `tool_calls`, `execution_log`). This reduces data transfer by 50-100x for agents with many executions.

```python
@router.get("/{name}/executions", response_model=List[ExecutionSummary])
async def get_agent_executions(
    name: AuthorizedAgent,
    limit: int = 50
):
    """Get execution summaries for an agent - optimized for list views.

    Returns lightweight ExecutionSummary objects that exclude large text fields:
    - response, error, tool_calls, execution_log

    For full execution details including response/error, use:
    GET /api/agents/{name}/executions/{id}
    """
    executions = db.get_agent_executions_summary(name, limit=limit)
    return executions
```

#### GET /api/agents/{name}/executions/{id}

**File**: `src/backend/routers/schedules.py:462-476`

Get full execution details including response, error, and execution_log. Used for on-demand loading when user expands a task row.

```python
@router.get("/{name}/executions/{execution_id}", response_model=ExecutionResponse)
async def get_execution(
    name: AuthorizedAgent,
    execution_id: str
):
    """Get full details of a specific execution."""
    execution = db.get_execution(execution_id)
    if not execution or execution.agent_name != name:
        raise HTTPException(status_code=404, detail="Execution not found")
    return ExecutionResponse(**execution.model_dump())
```

#### GET /api/agents/{name}/queue

**File**: `src/backend/routers/agents.py:445-451`
**Service**: `src/backend/services/agent_service/queue.py:18-43`

Get execution queue status for an agent.

```python
@router.get("/{agent_name}/queue")
async def get_agent_queue_status(agent_name: str, current_user: User = Depends(get_current_user)):
    return await get_agent_queue_status_logic(agent_name, current_user)
```

Returns `QueueStatus` model with:
- `is_busy`: Whether agent is currently executing
- `current_execution`: Details of running execution
- `queue_length`: Number of waiting executions
- `queued_executions`: List of queued execution details

#### GET /api/agents/{name}/executions/{execution_id}/log

**File**: `src/backend/routers/schedules.py:426-473`

Get the full execution log for a specific execution. *(Added 2025-12-31)*

```python
@router.get("/{name}/executions/{execution_id}/log")
async def get_execution_log(
    name: str,
    execution_id: str,
    current_user: User = Depends(get_current_user)
):
    """Get the full execution log for a specific execution."""
```

**Response** (log available):
```json
{
  "execution_id": "abc123",
  "agent_name": "my-agent",
  "has_log": true,
  "log": [
    {"type": "assistant", "message": {...}},
    {"type": "tool_use", "name": "Read", "input": {...}},
    {"type": "tool_result", "content": [...]}
  ],
  "started_at": "2025-12-31T10:00:00Z",
  "completed_at": "2025-12-31T10:01:00Z",
  "status": "success"
}
```

**Response** (no log):
```json
{
  "execution_id": "abc123",
  "has_log": false,
  "log": null,
  "message": "No execution log available for this execution"
}
```

#### POST /api/agents/{name}/queue/clear

**File**: `src/backend/routers/agents.py:454-460`
**Service**: `src/backend/services/agent_service/queue.py:46-82`

Clear all queued executions for an agent (does not stop running execution).

#### POST /api/agents/{name}/queue/release

**File**: `src/backend/routers/agents.py:463-469`
**Service**: `src/backend/services/agent_service/queue.py:85-124`

Force release an agent from its running state (emergency use for stuck executions).

---

## Agent Layer

### Agent Server Endpoint

**File**: `docker/base-image/agent_server/routers/chat.py:83-119`

```python
@router.post("/api/task")
async def execute_task(request: ParallelTaskRequest):
    """
    Execute a stateless task in parallel mode (no conversation context).

    Unlike /api/chat, this endpoint:
    - Does NOT acquire execution lock (parallel allowed)
    - Does NOT use --continue flag (stateless)
    - Each call is independent and can run concurrently
    """
    response_text, execution_log, metadata, session_id = await execute_headless_task(
        prompt=request.message,
        model=request.model,
        allowed_tools=request.allowed_tools,
        system_prompt=request.system_prompt,
        timeout_seconds=request.timeout_seconds or 300
    )

    return {
        "response": response_text,
        "execution_log": [entry.model_dump() for entry in execution_log],
        "metadata": metadata.model_dump(),
        "session_id": session_id,
        "timestamp": datetime.now().isoformat()
    }
```

### Claude Code Execution

The `execute_headless_task()` function in `agent_server/services/claude_code.py` runs Claude Code without conversation context:

```bash
claude --print --output-format stream-json --model {model} "prompt"
```

**Key differences from conversational chat**:
- No `--continue` flag (fresh context each time)
- No conversation history updates
- No execution lock (parallel allowed)
- Unique session ID generated per execution

---

## Data Layer

### Database Operations

**File**: `src/backend/db/schedules.py:298-328`

**Create Task Execution**:
```python
def create_task_execution(self, agent_name: str, message: str, triggered_by: str = "manual") -> Optional[ScheduleExecution]:
    """Create a new execution record for a manual/API-triggered task (no schedule)."""
    execution_id = self._generate_id()
    now = datetime.utcnow().isoformat()

    cursor.execute("""
        INSERT INTO schedule_executions (
            id, schedule_id, agent_name, status, started_at, message, triggered_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        execution_id,
        "__manual__",  # Special marker for manual/API-triggered tasks
        agent_name,
        "running",
        now,
        message,
        triggered_by
    ))
```

**Update Execution Status** (`src/backend/db/schedules.py:362-405`):
```python
def update_execution_status(
    self,
    execution_id: str,
    status: str,
    response: str = None,
    error: str = None,
    context_used: int = None,
    context_max: int = None,
    cost: float = None,
    tool_calls: str = None
) -> bool:
    # Calculate duration from started_at
    duration_ms = int((completed_at - started_at).total_seconds() * 1000)
    # Update record
```

**Get Agent Executions Summary** (`src/backend/db/schedules.py:655-682`) - PERF-001:
```python
def get_agent_executions_summary(self, agent_name: str, limit: int = 50) -> List[dict]:
    """Get execution summaries for list view - excludes large text fields.

    Returns lightweight dicts without: response, error, tool_calls, execution_log
    """
    cursor.execute("""
        SELECT
            id, schedule_id, agent_name, status, started_at, completed_at,
            duration_ms, message, triggered_by, context_used, context_max, cost,
            source_user_id, source_user_email, source_agent_name,
            source_mcp_key_id, source_mcp_key_name, claude_session_id
        FROM schedule_executions
        WHERE agent_name = ?
        ORDER BY started_at DESC
        LIMIT ?
    """, (agent_name, limit))
    return [dict(row) for row in cursor.fetchall()]
```

**Get Full Execution** (`src/backend/db/schedules.py:447-453`):
```python
def get_execution(self, execution_id: str) -> Optional[ScheduleExecution]:
    """Get a specific execution by ID with all fields."""
    cursor.execute("SELECT * FROM schedule_executions WHERE id = ?", (execution_id,))
    row = cursor.fetchone()
    return self._row_to_schedule_execution(row) if row else None
```

**Note**: The `create_task_execution` method was added 2025-12-28 specifically for manual task persistence.

### Database Schema

**Table**: `schedule_executions`

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT | Unique execution ID |
| `schedule_id` | TEXT | Schedule ID or `__manual__` for tasks |
| `agent_name` | TEXT | Agent name |
| `status` | TEXT | pending, running, success, failed |
| `started_at` | TEXT | ISO timestamp |
| `completed_at` | TEXT | ISO timestamp (nullable) |
| `duration_ms` | INTEGER | Execution duration in ms |
| `message` | TEXT | Task message |
| `response` | TEXT | Claude's response (nullable) |
| `error` | TEXT | Error message if failed (nullable) |
| `triggered_by` | TEXT | chat, manual, schedule, agent, mcp, paid, public |
| `context_used` | INTEGER | Tokens used (nullable) |
| `context_max` | INTEGER | Context window size (nullable) |
| `cost` | REAL | Cost in USD (nullable) |
| `tool_calls` | TEXT | JSON array of tool calls (nullable) |
| `execution_log` | TEXT | Full Claude Code execution transcript (JSON, nullable) *(added 2025-12-31)* |
| `model_used` | TEXT | Model used for this execution (nullable) *(added 2026-03-02, MODEL-001)* |

**Index** (PERF-001):
```sql
CREATE INDEX IF NOT EXISTS idx_executions_agent_started
ON schedule_executions(agent_name, started_at DESC)
```

This composite index optimizes the `WHERE agent_name = ? ORDER BY started_at DESC` query pattern used by the list endpoint.

---

## Execution Queue (Redis)

**File**: `src/backend/services/execution_queue.py`

The execution queue ensures only one execution runs per agent at a time for the `/chat` endpoint. **Note: The `/task` endpoint bypasses this queue entirely for parallel execution.**

### Queue Configuration

```python
MAX_QUEUE_SIZE = 3           # Max queued requests per agent
EXECUTION_TTL = 600          # 10 minutes max execution time
QUEUE_WAIT_TIMEOUT = 120     # 120 seconds max wait in queue
```

### Redis Keys

- `agent:running:{agent_name}` - Currently running execution (JSON)
- `agent:queue:{agent_name}` - Waiting queue (Redis list)

### QueueStatus Model

```python
class QueueStatus(BaseModel):
    agent_name: str
    is_busy: bool
    current_execution: Optional[Execution] = None
    queue_length: int
    queued_executions: List[Execution] = []
```

---

## Side Effects

### WebSocket Broadcasts

**Agent Collaboration Event** (when X-Source-Agent header present):
```python
{
    "type": "agent_collaboration",
    "source_agent": "orchestrator",
    "target_agent": "worker",
    "action": "parallel_task",
    "timestamp": "2025-12-28T10:00:00Z"
}
```

### Audit Log

**File**: `src/backend/routers/chat.py:430-443`

```python
await log_audit_event(
    event_type="agent_interaction",
    action="parallel_task",
    user_id=current_user.username,
    agent_name=name,
    resource=f"agent-{name}",
    result="success",
    details={
        "source": source.value,
        "source_agent": x_source_agent,
        "session_id": response_data.get("session_id"),
        "execution_id": execution_id
    }
)
```

### Activity Tracking

Tasks are tracked in the `agent_activities` table via `activity_service.track_activity()`:
- Activity type: `CHAT_START` (reused for tasks)
- Details include: parallel_mode=True, model, timeout_seconds, execution_id

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Agent not found | 404 | Agent not found |
| Agent not running | 503 | Agent is not running |
| Task timeout | 504 | Task execution timed out after N seconds |
| HTTP error to agent | 503 | Failed to execute task. The agent may be unavailable. |
| Access denied | 403 | Access denied |

---

## UI States

### Queue Status Indicator

**Idle State** (green):
```
[Green dot] Idle
```

**Busy State** (yellow pulsing):
```
[Yellow pulsing dot] Busy | 2 queued
```

### Task Status Badges

| Status | Badge Color | Indicator |
|--------|-------------|-----------|
| running | Yellow | Pulsing dot |
| success | Green | Solid dot |
| failed | Red | Solid dot |
| cancelled | Orange | Solid dot |
| skipped | Purple | Solid dot |
| queued | Gray | Solid dot |

### Trigger Badges

| Trigger | Color |
|---------|-------|
| chat | Sky blue |
| manual | Blue |
| schedule | Purple |
| paid | Yellow |
| public | Teal |
| mcp | Gray |
| agent | Gray |

---

## Security Considerations

1. **Authorization**: All endpoints require authentication via `get_current_user`
2. **Agent Access**: `db.can_user_access_agent()` check before any operation
3. **No Credential Exposure**: Task messages may contain sensitive data but are stored in database (not logged)
4. **Queue Protection**: Queue clear/release endpoints require agent access permissions

---

## Related Flows

- **Upstream**: [parallel-headless-execution.md](parallel-headless-execution.md) - Core `/task` endpoint implementation. **2026-02-20**: Now supports `save_to_session` parameter for Chat tab persistence
- **Upstream**: [execution-queue.md](execution-queue.md) - Queue system (bypassed by tasks)
- **Related**: [scheduling.md](scheduling.md) - Scheduled executions share the same database table (now use `/api/task` for log format); **Make Repeatable** feature creates schedules from task messages (2026-02-20: schedules now support per-schedule timeout and allowed_tools)
- **Related**: [execution-log-viewer.md](execution-log-viewer.md) - Log viewer that renders execution transcripts
- **Related**: [execution-termination.md](execution-termination.md) - Stop button, process registry, graceful termination
- **Related**: [authenticated-chat-tab.md](authenticated-chat-tab.md) - Chat tab uses same `/task` endpoint for Dashboard tracking. **2026-02-20**: Session persistence via `save_to_session=true` parameter
- **Related**: [persistent-chat-tracking.md](persistent-chat-tracking.md) - Chat session tables (`chat_sessions`, `chat_messages`) used by Chat tab via `/task` endpoint
- **Related**: [model-selection.md](model-selection.md) - Model selector in task input, `model_used` field in execution records (MODEL-001)
- **Downstream**: [activity-monitoring.md](activity-monitoring.md) - Activity tracking for tasks

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-03-18 | **Timeout selector**: Added `taskTimeout` state with a Timeout dropdown in the New Task Input section (5min/15min/30min/1hr/2hr, default 15min). Value persisted per-agent in localStorage (`trinity-task-timeout-{name}`). Sent as `timeout_seconds` in POST /task payload. Added `cancelled` (orange) and `skipped` (purple) task status badges. Updated default model to `claude-opus-4-5-20251101`. Enhanced execution ID matching: fallback to single running execution when preview matching fails. `loadQueueStatus` now conditionally fetches running executions for local running tasks (manual tasks bypass queue). Terminate URL now includes `?task_execution_id=` query param so backend can update DB record. |
| 2026-03-15 | **Unified chat execution tracking (#96)**: All `/api/chat` calls now create execution records (previously only MCP/agent-to-agent did). User chats use `triggered_by=chat`. Added "Chat" filter option (sky blue badge) to TasksPanel. Subtitle updated to "All executions". |
| 2026-03-04 | **Paid/Public trigger types**: Added `paid` (yellow) and `public` (teal) to Trigger Badges table and filter dropdown. |
| 2026-03-02 | **MODEL-001 Model Selection**: Added `ModelSelector.vue` component above task textarea (line 86). Model persisted per-agent in localStorage. Model sent in POST /task payload. `model_used` displayed in task history rows (line 205). New `model_used TEXT` column in `schedule_executions` table. |
| 2026-02-21 | **PERF-001 Performance Optimization**: List endpoint now returns `ExecutionSummary` (excludes `response`, `error`, `tool_calls`, `execution_log`). Frontend loads details on-demand when user expands task row via `GET /api/agents/{name}/executions/{id}`. Added `taskDetailsCache` and `fetchTaskDetails()` function. New composite index `idx_executions_agent_started`. Data transfer reduced 50-100x. |
| 2026-02-20 | **Make Repeatable enhancement**: Updated test step and Related Flows to note that schedules created via "Make Repeatable" now support per-schedule timeout and allowed_tools configuration. |
| 2026-02-18 | **UI Redesign (UI-001)**: Reordered sections - Stats (49-69) now first, then Task Input (71-101), then Task History (103-315). Stats section more compact with smaller padding. Run button height now matches textarea. Updated line numbers throughout. |
| 2026-01-12 | **Execution Termination**: Added Stop button (lines 255-271) for running tasks, `terminateTask()` function, `loadRunningExecutions()`, execution_id matching via `enhanceWithExecutionId()`. See [execution-termination.md](execution-termination.md). |
| 2026-01-12 | Added "Make Repeatable" feature - calendar icon button to create schedule from task, emits `create-schedule` event to parent |
| 2025-01-02 | Added note about log format standardization - all execution types now use `/api/task` for raw Claude Code format |
| 2025-12-31 | Initial documentation - execution log viewer added |

---

## Testing

### Prerequisites
- Trinity platform running (`./scripts/deploy/start.sh`)
- At least one agent created and running

### Test Steps

1. **Navigate to Tasks Tab**
   - Go to Agent Detail page for any agent
   - Click "Tasks" tab
   - **Expected**: Tasks panel loads with empty state or existing executions

2. **Submit New Task (Agent Running)**
   - Enter task message: "What is 2+2?"
   - Click "Run" button
   - **Expected**:
     - Task appears immediately with "running" status (local optimistic update)
     - After completion, status changes to "success"
     - Response can be expanded by clicking task row

3. **Submit Task (Agent Stopped)**
   - Stop the agent
   - Attempt to submit task
   - **Expected**:
     - Run button is disabled
     - Warning message: "Agent must be running to execute tasks"

4. **Re-run Historical Task**
   - Click re-run icon on any completed task
   - **Expected**: Task message populates input and submits automatically

5. **Make Repeatable (Create Schedule from Task)**
   - Find a completed task in the history
   - Click the calendar icon ("Create schedule from this task")
   - **Expected**:
     - UI switches to Schedules tab automatically
     - Create schedule form opens with message field pre-filled with task message
     - User can add schedule name, cron expression, timeout, allowed tools, and click Create

6. **Queue Status (with /chat endpoint)**
   - Open terminal tab and start a long-running task
   - Switch to Tasks tab
   - **Expected**: Queue status shows "Busy" indicator

7. **Force Release Queue**
   - When queue shows "Busy"
   - Click "Force Release Queue"
   - **Expected**: Queue indicator returns to "Idle"

### Edge Cases
- Submit empty task (should be disabled)
- Submit very long task message (should work, message truncated in display)
- Multiple rapid task submissions (all should complete independently)

### Status
Verified 2025-12-31 - Execution log storage and retrieval endpoint added
