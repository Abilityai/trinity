# Feature: Execution Detail Page

## Overview

A dedicated page for viewing comprehensive execution details including metadata, timing, cost, context usage, task input, response, and the full execution transcript. This provides a better experience than the modal for exploring execution history in depth.

## User Story

As an agent operator, I want to open execution details in a dedicated page so that I can thoroughly analyze the execution without modal constraints, share the URL with colleagues, and have more screen space for long transcripts.

## Entry Points

| Entry Point | File | Line | Description |
|-------------|------|------|-------------|
| TasksPanel (Live) | `src/frontend/src/components/TasksPanel.vue` | 213-232 | **Running tasks**: Green "Live" button with pulsing dot |
| TasksPanel (Details) | `src/frontend/src/components/TasksPanel.vue` | 213-232 | **Completed tasks**: External link icon |
| ReplayTimeline | `src/frontend/src/components/ReplayTimeline.vue` | 767-785 | Click on execution bar |
| Direct URL | N/A | N/A | Navigate to `/agents/{name}/executions/{id}` |

### Live Button for Running Tasks (2026-01-13)

For running tasks, the TasksPanel shows a green "Live" button with an animated pulsing dot, enabling users to navigate to the Execution Detail page to monitor real-time progress.

**Visibility Condition** (line 214):
```vue
v-if="!task.id.startsWith('local-') || task.execution_id"
```

- **Server executions**: `task.id` is the database UUID (e.g., `abc123XYZ789...`) - always shown
- **Local pending tasks**: `task.id` is `local-xxx` - shown only after `execution_id` is obtained from process registry (~2 seconds after task starts)

**Route Parameter Logic** (line 215):
```vue
:to="{ name: 'ExecutionDetail', params: {
  name: agentName,
  executionId: task.id.startsWith('local-') ? task.execution_id : task.id
}}"
```

**Visual Styling** (lines 216-221, 224-228):
- Running tasks: Green background (`bg-green-50 dark:bg-green-900/20`), green text, "Live" label with pulsing dot
- Completed tasks: Gray text with indigo hover state, external link icon only

---

## Route Configuration

| Property | Value |
|----------|-------|
| **Path** | `/agents/:name/executions/:executionId` |
| **Name** | `ExecutionDetail` |
| **Component** | `src/frontend/src/views/ExecutionDetail.vue` |
| **Auth Required** | Yes (`requiresAuth: true`) |
| **File** | `src/frontend/src/router/index.js:41-46` |

```javascript
{
  path: '/agents/:name/executions/:executionId',
  name: 'ExecutionDetail',
  component: () => import('../views/ExecutionDetail.vue'),
  meta: { requiresAuth: true }
}
```

### Execution ID Format

The `:executionId` parameter uses the **Database Execution ID** format:
- Format: `token_urlsafe(16)` - 22 characters, URL-safe base64
- Storage: SQLite `schedule_executions.id` column
- Example: `abc123XYZ789abcd12`

**Important**: This is different from the Queue Execution ID (UUID format) used internally for Redis queue management. The chat API response includes both:
```json
{
  "execution": {
    "id": "uuid-format-queue-id",           // Queue ID (transient)
    "task_execution_id": "abc123XYZ789..."  // Database ID (use this for navigation)
  }
}
```

---

## Frontend Layer

### Component: ExecutionDetail.vue

**File**: `src/frontend/src/views/ExecutionDetail.vue` (496 lines)

#### Page Structure

| Section | Lines | Description |
|---------|-------|-------------|
| Header | 3-57 | Back button, agent name breadcrumb, execution ID (truncated), status badge, copy ID button |
| Loading State | 59-62 | Spinner while fetching data |
| Error State | 64-79 | Error panel with retry button |
| Metadata Cards | 83-148 | 4-column grid: Duration, Cost, Context, Trigger |
| Timestamps Row | 150-170 | Started, Completed, full Execution ID |
| Task Input Panel | 172-180 | Message sent to the agent |
| Error Panel | 182-195 | Conditional, shown if execution.error exists |
| Response Summary | 197-205 | Final response text |
| Execution Transcript | 207-299 | Parsed log entries with formatted bubbles |

#### State Management

```javascript
// ExecutionDetail.vue:316-319
const loading = ref(true)
const error = ref(null)
const execution = ref(null)  // ExecutionResponse from API
const logData = ref(null)    // Execution log data from API
```

#### Route Parameters

```javascript
// ExecutionDetail.vue:313-314
const agentName = computed(() => route.params.name)
const executionId = computed(() => route.params.executionId)
```

#### API Calls (Parallel Fetch)

```javascript
// ExecutionDetail.vue:353-375
async function loadExecution() {
  loading.value = true
  error.value = null

  try {
    // Fetch execution details and log in parallel
    const [execResponse, logResponse] = await Promise.all([
      axios.get(`/api/agents/${agentName.value}/executions/${executionId.value}`, {
        headers: authStore.authHeader
      }),
      axios.get(`/api/agents/${agentName.value}/executions/${executionId.value}/log`, {
        headers: authStore.authHeader
      })
    ])

    execution.value = execResponse.data
    logData.value = logResponse.data
  } catch (err) {
    error.value = err.response?.data?.detail || err.message || 'Failed to load execution'
  } finally {
    loading.value = false
  }
}
```

#### Log Parsing

```javascript
// ExecutionDetail.vue:402-489
function parseExecutionLog(log) {
  // Handles string (legacy) or JSON array format
  // Returns array of entry objects with type: init | assistant-text | tool-call | tool-result | result
}
```

### Navigation Entry Points

#### TasksPanel.vue (Lines 213-232)

The router-link navigates to ExecutionDetail page with context-aware styling:

```vue
<!-- Open Execution Detail Page (Live for running, Details for completed) -->
<!-- For server executions: task.id IS the database UUID -->
<!-- For local tasks: task.id is 'local-xxx', must use task.execution_id instead -->
<router-link
  v-if="!task.id.startsWith('local-') || task.execution_id"
  :to="{ name: 'ExecutionDetail', params: {
    name: agentName,
    executionId: task.id.startsWith('local-') ? task.execution_id : task.id
  }}"
  :class="[
    'p-1.5 rounded transition-colors flex items-center space-x-1',
    task.status === 'running'
      ? 'text-green-600 dark:text-green-400 hover:text-green-700 dark:hover:text-green-300 bg-green-50 dark:bg-green-900/20'
      : 'text-gray-400 hover:text-indigo-600 dark:hover:text-indigo-400'
  ]"
  :title="task.status === 'running' ? 'View live execution' : 'Open execution details'"
>
  <!-- Live indicator for running tasks -->
  <span v-if="task.status === 'running'" class="flex items-center">
    <span class="w-1.5 h-1.5 bg-green-500 rounded-full animate-pulse mr-1"></span>
    <span class="text-xs font-medium">Live</span>
  </span>
  <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
  </svg>
</router-link>
```

**Key Features:**
- **Running tasks**: Shows green "Live" badge with animated pulsing dot
- **Completed tasks**: Shows gray external link icon
- **Local task handling**: Uses `task.execution_id` (from process registry) instead of `task.id` for local pending tasks

#### ReplayTimeline.vue (Lines 767-785)

Click handler on execution bars (opens in new tab):

```javascript
function navigateToExecution(activity) {
  // Open execution detail page in a new tab
  const agentName = activity.agentName
  if (!agentName) return

  if (activity.executionId) {
    // Open Execution Detail page in new tab
    const route = router.resolve({
      name: 'ExecutionDetail',
      params: { name: agentName, executionId: activity.executionId }
    })
    window.open(route.href, '_blank')
  } else {
    // Fallback: open Tasks tab in new tab
    const route = router.resolve({
      path: `/agents/${agentName}`,
      query: { tab: 'tasks' }
    })
    window.open(route.href, '_blank')
  }
}
```

---

## Backend Layer

### API Endpoints

| Method | Path | Handler | Lines | Description |
|--------|------|---------|-------|-------------|
| GET | `/api/agents/{name}/executions/{id}` | `get_execution()` | schedules.py:319-332 | Execution metadata |
| GET | `/api/agents/{name}/executions/{id}/log` | `get_execution_log()` | schedules.py:335-375 | Full transcript |

### Endpoint: Get Execution Details

**File**: `src/backend/routers/schedules.py:319-332`

```python
@router.get("/{name}/executions/{execution_id}", response_model=ExecutionResponse)
async def get_execution(
    name: AuthorizedAgent,
    execution_id: str
):
    """Get details of a specific execution."""
    execution = db.get_execution(execution_id)
    if not execution or execution.agent_name != name:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Execution not found"
        )

    return ExecutionResponse(**execution.model_dump())
```

**trinity-enterprise#754**: the handler (now `routers/schedules.py:920-938`) also takes `current_user` and returns `gate_self_approved` / `gate_self_approved_by_viewer` (`models.py:4178-4179`), read through `skill_gate_map_service.self_approved_flags` from the run's `self_approved` record in `skill_gate_requests` — see [skills-tab.md](skills-tab.md).

### Endpoint: Get Execution Log

**File**: `src/backend/routers/schedules.py:335-375`

```python
@router.get("/{name}/executions/{execution_id}/log")
async def get_execution_log(
    name: AuthorizedAgent,
    execution_id: str
):
    """Get the full execution log for a specific execution."""
    execution = db.get_execution(execution_id)
    if not execution or execution.agent_name != name:
        raise HTTPException(status_code=404, detail="Execution not found")

    if not execution.execution_log:
        return {"execution_id": execution_id, "has_log": False, "log": None}

    # Parse JSON log for structured response
    try:
        log_data = json.loads(execution.execution_log)
    except json.JSONDecodeError:
        log_data = execution.execution_log

    return {
        "execution_id": execution_id,
        "agent_name": name,
        "has_log": True,
        "log": log_data,
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "completed_at": execution.completed_at.isoformat() if execution.completed_at else None,
        "status": execution.status
    }
```

### Response Model: ExecutionResponse

**File**: `src/backend/routers/schedules.py:78-105`

```python
class ExecutionResponse(BaseModel):
    """Full response model for execution data - includes all fields."""
    id: str
    schedule_id: str
    agent_name: str
    status: str  # pending, running, success, failed
    started_at: datetime
    completed_at: Optional[datetime]
    duration_ms: Optional[int]
    message: str
    response: Optional[str]           # Full response text
    error: Optional[str]              # Full error text
    triggered_by: str  # schedule, manual, user
    # Observability fields
    context_used: Optional[int] = None
    context_max: Optional[int] = None
    cost: Optional[float] = None
    tool_calls: Optional[str] = None
    execution_log: Optional[str] = None  # Full Claude Code transcript (JSON)
```

**Note (PERF-001)**: The list endpoint (`GET /api/agents/{name}/executions`) returns `ExecutionSummary` which excludes `response`, `error`, `tool_calls`, and `execution_log`. The detail endpoint (`GET /api/agents/{name}/executions/{id}`) returns the full `ExecutionResponse`.

### Authorization

The `AuthorizedAgent` dependency (`src/backend/deps.py`) validates:
1. User is authenticated (valid JWT token)
2. User has access to the specified agent (owner or shared)

---

## Data Layer

### Database Operations

**File**: `src/backend/db/schedules.py`

| Method | Lines | Description |
|--------|-------|-------------|
| `get_execution()` | 447-453 | Fetch single execution by ID |
| `get_agent_executions()` | 435-445 | Fetch all executions for an agent |

```python
# db/schedules.py:447-453
def get_execution(self, execution_id: str) -> Optional[ScheduleExecution]:
    """Get a specific execution by ID."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM schedule_executions WHERE id = ?", (execution_id,))
        row = cursor.fetchone()
        return self._row_to_schedule_execution(row) if row else None
```

### Database Table: schedule_executions

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT | Primary key (UUID) |
| schedule_id | TEXT | FK to agent_schedules (or `__manual__` for manual tasks) |
| agent_name | TEXT | Agent that executed |
| status | TEXT | pending / running / success / failed |
| started_at | TEXT | ISO timestamp |
| completed_at | TEXT | ISO timestamp (NULL if running) |
| duration_ms | INTEGER | Execution duration in milliseconds |
| message | TEXT | Task input (prompt) |
| response | TEXT | Final response summary |
| error | TEXT | Error message if failed |
| triggered_by | TEXT | schedule / manual / user / agent |
| context_used | INTEGER | Input tokens consumed |
| context_max | INTEGER | Max context window size |
| cost | REAL | Cost in USD |
| tool_calls | TEXT | JSON array of tool call names |
| execution_log | TEXT | Full Claude Code transcript (JSON array) |

---

## UI Components

### Metadata Cards (Lines 83-148)

| Card | Icon | Background | Data Source | Format |
|------|------|------------|-------------|--------|
| Duration | Clock | Blue | `execution.duration_ms` | `formatDuration()` - "1.5s", "2m 30s" |
| Cost | Dollar | Green | `execution.cost` | `$0.0000` |
| Context | Chart | Purple | `execution.context_used/max` | `"X.XK / Y.YK"` |
| Trigger | Calendar/Click/Lightning | Purple/Amber/Cyan | `execution.triggered_by` | "schedule", "manual", "user" |

### Execution Origin Card (`views/ExecutionDetail.vue:177-211`)

The AUDIT-001 origin card (User / Source Agent / MCP Key). Since trinity-enterprise#754 it also renders `ExecutionGateMarker` (`variant="line"`, `:205-210`) when `execution.gate_self_approved` is true: "Ran without approval: you are the approver." for the requester (`gate_self_approved_by_viewer`), else "Ran without approval: its approver started it.". The card shows when the run has origin data **or** that flag (`:179`).

### Status Badge Colors (Lines 322-329)

| Status | Light Mode | Dark Mode |
|--------|------------|-----------|
| success | `bg-green-100 text-green-800` | `bg-green-900/50 text-green-300` |
| failed | `bg-red-100 text-red-800` | `bg-red-900/50 text-red-300` |
| running | `bg-blue-100 text-blue-800` | `bg-blue-900/50 text-blue-300` |
| pending | `bg-gray-100 text-gray-800` | `bg-gray-700 text-gray-300` |

### Trigger Icon Colors (Lines 331-345)

| Trigger | Icon | Background | Icon Color |
|---------|------|------------|------------|
| schedule | Calendar | Purple | Purple |
| manual | Click/Cursor | Amber | Amber |
| paid | Lightning | Yellow | Yellow |
| public | Lightning | Teal | Teal |
| user/other | Lightning | Cyan | Cyan |

### Execution Transcript Entry Types (Lines 226-296)

| Entry Type | Icon | Background | Description |
|------------|------|------------|-------------|
| init | - | Gray | Session started with model, tool count, MCP servers |
| assistant-text | Computer | Indigo | Claude's thinking/response text |
| tool-call | Gear | Amber | Tool invocation with name and input JSON |
| tool-result | Checkmark | Green | Tool execution result (truncated at 5000 chars) |
| result | - | Gray + top border | Completion summary: turns, duration, cost |

---

## Error Handling

| Error Case | HTTP Status | UI Display |
|------------|-------------|------------|
| Execution not found | 404 | Error panel with "Try Again" button |
| Agent not accessible | 403 | Error panel with permission message |
| Network error | - | Error panel with retry option |
| No log available | 200 | "No execution transcript available" message in transcript section |

---

## Security Considerations

1. **Route Protection**: `requiresAuth: true` in router config blocks unauthenticated access
2. **Agent Authorization**: `AuthorizedAgent` dependency checks user has access to agent
3. **Execution Ownership**: Backend verifies `execution.agent_name == name` parameter
4. **No Cross-Agent Access**: Cannot view executions from agents user doesn't have access to

---

## Live SSE Streaming

For running executions, the Execution Detail page supports real-time log streaming via Server-Sent Events (SSE).

### How It Works

1. **Page detects running status**: If `execution.status === 'running'`, starts SSE stream
2. **Connects to stream endpoint**: `GET /api/agents/{name}/executions/{id}/stream`
3. **Parses SSE events**: Each `data: {...}` line is parsed and added to the transcript
4. **Auto-scrolls**: Keeps the latest log entries in view
5. **Completes when done**: On `stream_end` event, fetches final execution state

### Frontend Implementation

**File**: `src/frontend/src/views/ExecutionDetail.vue:446-519`

The frontend uses `fetch` with `ReadableStream` (not `EventSource`) because `EventSource` doesn't support custom Authorization headers:

```javascript
fetch(url, {
  headers: {
    'Authorization': `Bearer ${authStore.token}`,
    'Accept': 'text/event-stream'
  }
}).then(response => {
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  function processStream() {
    reader.read().then(({ done, value }) => {
      if (done) { handleStreamEnd(); return }
      buffer += decoder.decode(value, { stream: true })
      // Process SSE lines...
    })
  }
  processStream()
})
```

### nginx Configuration (Required for Production)

SSE streaming requires nginx proxy buffering to be disabled. Without this configuration, nginx buffers the SSE response and events don't stream in real-time.

**File**: `src/frontend/nginx.conf`

```nginx
location /api/ {
    # ... standard proxy settings ...

    # SSE (Server-Sent Events) support - REQUIRED for live streaming
    proxy_buffering off;
    proxy_cache off;
    chunked_transfer_encoding on;
}
```

| Directive | Purpose |
|-----------|---------|
| `proxy_buffering off` | Disable response buffering so events stream immediately |
| `proxy_cache off` | Prevent caching of SSE stream |
| `chunked_transfer_encoding on` | Enable chunked transfer for streaming |

**Note**: Without these directives, the page loads but no log entries appear for running executions (bug fixed 2026-02-05).

### UI Indicators

| Element | Location | Description |
|---------|----------|-------------|
| "Live" badge | Header status area | Green badge shown when streaming |
| Entry counter | Transcript header | Shows "X entries" updating in real-time |
| Auto-scroll toggle | Transcript header | Button to enable/disable auto-scroll |
| Pulsing status | Header | Status badge pulses when streaming |

---

## Related Flows

| Direction | Flow | Description |
|-----------|------|-------------|
| **Upstream** | [tasks-tab.md](tasks-tab.md) | Lists executions with link to detail page |
| **Upstream** | [dashboard-timeline-view.md](dashboard-timeline-view.md) | Timeline bars link to detail page |
| **Related** | [execution-log-viewer.md](execution-log-viewer.md) | Modal viewer (still available for quick view) |
| **Related** | [parallel-headless-execution.md](parallel-headless-execution.md) | How executions are created and stored, SSE streaming details |
| **Related** | [scheduling.md](scheduling.md) | Scheduled execution creation |
| **Downstream** | [continue-execution-as-chat.md](continue-execution-as-chat.md) | "Continue as Chat" button navigates to Chat tab with resume context (EXEC-023) |

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-10-08 | **trinity-enterprise#754**: the origin card says when the run went through without approval because its requester is the approver (`ExecutionGateMarker`, from `gate_self_approved` / `gate_self_approved_by_viewer` on the detail read) — [skills-tab.md](skills-tab.md). |
| 2026-03-04 | **Paid/Public trigger types**: Added `paid` (yellow bg/icon) and `public` (teal bg/icon) to Trigger Icon Colors table. |
| 2026-02-21 | **PERF-001**: Added note distinguishing `ExecutionResponse` (full, used here) from `ExecutionSummary` (lightweight, used by list endpoint). Execution Detail page continues to fetch full data via `GET /api/agents/{name}/executions/{id}`. |
| 2026-02-20 | Added "Continue as Chat" button (EXEC-023) - visible when `claude_session_id` exists and status is not "running". Navigates to Chat tab with `resumeSessionId` and `executionId` query params. See [continue-execution-as-chat.md](continue-execution-as-chat.md) for full flow. |
| 2026-02-05 | Added "Live SSE Streaming" section documenting real-time log streaming. Documented nginx configuration requirements (`proxy_buffering off`, `proxy_cache off`, `chunked_transfer_encoding on`) needed for production streaming. Added frontend fetch/ReadableStream implementation details. |
| 2026-01-13 | Added "Live" button for running tasks in TasksPanel (lines 213-232). Green badge with animated pulsing dot navigates to Execution Detail for live monitoring. Explicit ID logic: server executions use `task.id`, local pending tasks use `task.execution_id` from process registry. |
| 2026-01-11 | Clarified Database Execution ID format (`token_urlsafe(16)`) vs Queue Execution ID (UUID). Navigation uses Database ID from `task_execution_id` in chat response. |
| 2026-01-10 | Initial implementation - dedicated execution detail page with metadata cards, timestamps, task input, error panel, response summary, and full transcript |

---

## Testing

### Prerequisites
- Trinity platform running (`./scripts/deploy/start.sh`)
- At least one agent with completed executions (manual or scheduled)

### Test Steps

1. **Navigate from TasksPanel**
   - Go to Agent Detail page -> Tasks tab
   - Find a completed execution in the list
   - Click the external link icon (opens in new view, first action icon)
   - **Expected**: Opens `/agents/{name}/executions/{id}` page
   - **Verify**: URL contains agent name and execution UUID

2. **Navigate from Dashboard Timeline**
   - Go to Dashboard -> Switch to Timeline view (toggle in header)
   - Click on any execution bar (colored rectangle)
   - **Expected**: Opens Execution Detail page in a new browser tab
   - **Verify**: New tab opens with URL `/agents/{name}/executions/{id}`, original Dashboard tab unchanged

3. **Verify Metadata Display**
   - View any execution detail page
   - **Expected**:
     - Duration card shows formatted time (e.g., "2.5s", "1m 30s")
     - Cost card shows USD format (e.g., "$0.0234")
     - Context card shows token ratio (e.g., "12.5K / 200K")
     - Trigger card shows source (schedule/manual/user)

4. **Verify Timestamps**
   - Check timestamps row below metadata cards
   - **Expected**: Started and Completed times in local timezone, full execution ID

5. **Verify Task Input**
   - Check "Task Input" panel
   - **Expected**: Shows the original message/prompt sent to agent

6. **Verify Execution Transcript**
   - Scroll to "Execution Transcript" section
   - **Expected**:
     - Shows "Session Started" init entry with model name
     - Claude messages in indigo bubbles
     - Tool calls in amber bubbles with JSON input
     - Tool results in green bubbles
     - Completion summary at bottom

7. **Error Execution Display**
   - Navigate to a failed execution (red status in Tasks list)
   - **Expected**: Red error panel appears between Task Input and Response Summary

8. **Invalid Execution ID**
   - Navigate to `/agents/{name}/executions/invalid-uuid`
   - **Expected**: Error panel with "Failed to load execution" and "Try Again" button

9. **Back Navigation**
   - Click back arrow in header
   - **Expected**: Returns to Agent Detail page with Tasks tab selected

10. **Copy Execution ID**
    - Click copy button in header (clipboard icon)
    - **Expected**: Full execution UUID copied to clipboard

### Edge Cases

- **Running Execution**: Duration shows "-", Completed shows "In progress..."
- **No Log Available**: Transcript section shows "No execution transcript available" message
- **Long Transcript**: Tool results truncated at 5000 characters with "... (truncated)" suffix

### Status
Implemented 2026-01-10
