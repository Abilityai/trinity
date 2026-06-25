# Agent Scheduling Feature Flow

> Automated task execution for agents using cron-style scheduling

---

## Feature Overview

The Agent Scheduling feature enables users to automate agent tasks by configuring cron-based schedules. The **Dedicated Scheduler Service** (standalone container) executes scheduled tasks by sending messages to agents at specified times.

**Important**: All scheduled executions go through the [Execution Queue System](execution-queue.md) to prevent collisions with user chat or agent-to-agent calls.

**Architecture Change (2026-02-11):** The embedded scheduler (`src/backend/services/scheduler_service.py`) has been **completely removed**. Schedule execution is now handled exclusively by the [Dedicated Scheduler Service](scheduler-service.md).

**Key Capabilities:**
- Cron expression scheduling (5-field format)
- Timezone support (UTC, EST, PST, etc.)
- Execution history tracking
- Enable/disable individual schedules
- Manual trigger for testing (routed through dedicated scheduler)
- WebSocket real-time updates
- Queue-aware execution (429 on queue full)
- **Make Repeatable** - Create schedule from existing task execution (2026-01-12)
- **Configurable Timeout** - Per-schedule timeout (5m, 15m, 30m, 1h, 2h) (2026-02-20)
- **Tool Restrictions** - Per-schedule allowed tools list (null = all tools) (2026-02-20)

**Refactored (2025-12-31):**
- Access control via `AuthorizedAgent` dependency (not inline checks)
- Agent HTTP communication via `AgentClient` service (not raw httpx)

**Fixed (2025-01-02):**
- Scheduler now uses `AgentClient.task()` instead of `AgentClient.chat()` to ensure execution logs are stored in raw Claude Code `stream-json` format for proper display in the [Execution Log Viewer](execution-log-viewer.md)

---

## Architecture

> **Note (2026-02-11)**: Architecture updated to reflect scheduler consolidation. The embedded scheduler has been removed. All schedule execution is handled by the **Dedicated Scheduler Service**.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              Frontend                                    │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  AgentDetail.vue (Schedules Tab)                                 │    │
│  │  └── SchedulesPanel.vue                                          │    │
│  │      ├── Schedule List (enable/disable, trigger, edit, delete)   │    │
│  │      ├── Create/Edit Modal (cron presets, timezone)              │    │
│  │      └── Execution History (expandable, auto-polls every 10s)    │    │
│  └─────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼ HTTP/REST
┌─────────────────────────────────────────────────────────────────────────┐
│                              Backend                                     │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  routers/schedules.py                                            │    │
│  │  ├── CRUD endpoints (create, read, update, delete)               │    │
│  │  ├── Control endpoints (enable, disable, trigger)                │    │
│  │  └── Execution history endpoints                                 │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                    │                                     │
│                 [CRUD only - no direct scheduler calls]                  │
│                                    │                                     │
│                                    ▼                                     │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  database.py (SQLite)                                            │    │
│  │  ├── agent_schedules: Schedule configurations                    │    │
│  │  └── schedule_executions: Execution history                      │    │
│  └─────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
    ┌───────────────────────────────┴───────────────────────────────┐
    │                                                               │
    ▼ [Syncs every 60s]                                             ▼ Manual Trigger
┌─────────────────────────────────────────────────────────────────────────┐
│                    Dedicated Scheduler Service                           │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  src/scheduler/service.py (APScheduler)                          │    │
│  │  ├── Load schedules from database on startup                     │    │
│  │  ├── Sync schedules from database every 60 seconds               │    │
│  │  ├── Execute: POST /api/internal/execute-task (backend)           │    │
│  │  ├── Backend handles: slot, activity, agent call, sanitize       │    │
│  │  └── Broadcast: WebSocket events for execution status            │    │
│  └─────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼ HTTP POST /api/internal/execute-task
┌─────────────────────────────────────────────────────────────────────────┐
│                    Backend (TaskExecutionService)                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  routers/internal.py → services/task_execution_service.py        │    │
│  │  ├── Acquire capacity slot                                       │    │
│  │  ├── Track activity (CHAT_START)                                 │    │
│  │  ├── POST to agent /api/task with retry                          │    │
│  │  ├── Sanitize credentials from response                          │    │
│  │  └── Release slot, complete activity                             │    │
│  └─────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Flow Details

### 1. Create Schedule Flow

**User Action:** Click "New Schedule" → Fill form (incl. model selector) → Click "Create"

> **Note (2026-02-11)**: Backend no longer calls scheduler directly. Schedules are created in the database, and the dedicated scheduler syncs automatically every 60 seconds.
> **Note (2026-03-02, MODEL-001)**: Form now includes a Model selector (`ModelSelector.vue`). The `model` field is stored in `agent_schedules` and forwarded to the agent when the schedule fires.

```
Frontend                          Backend                           Database
--------                          -------                           --------
SchedulesPanel.vue               schedules.py:85                 → db/schedules.py
POST /api/agents/{name}/          create_schedule()                 create_schedule()
schedules                         validate cron expression          INSERT agent_schedules
                                  ↓                                 (next_run_at calculated)
                                  db/schedules.py:99-168
                                  _calculate_next_run_at()
                                  Return 201 Created

                                                                    Dedicated Scheduler
                                                                    ------------------
                                                                    (within 60s sync interval)
                                                                    _sync_schedules()
                                                                    Detects new schedule
                                                                    APScheduler.add_job()
```

**Files:**
- `src/frontend/src/components/SchedulesPanel.vue` - Schedule creation form
- `src/backend/routers/schedules.py:85-113` - create_schedule endpoint
- `src/backend/db/schedules.py:99-168` - create_schedule() with next_run_at calculation
- `src/scheduler/service.py:224-316` - _sync_schedules() auto-detects new schedules

### 1b. Make Repeatable Flow (Create Schedule from Task)

**User Action:** Click calendar icon on completed task in Tasks tab → Fill remaining form fields → Click "Create"

```
Frontend                          Frontend                          Frontend
--------                          --------                          --------
TasksPanel.vue:251-261           AgentDetail.vue:583-594           SchedulesPanel.vue:790-802
User clicks calendar icon        handleCreateSchedule()            watch(initialMessage)
makeRepeatable(task)             - Set schedulePrefillMessage      - Detect new message
emit('create-schedule', msg)     - Switch to 'schedules' tab       - Pre-fill formData.message
                                                                   - Open create form modal
```

**Data Flow:**
1. User clicks calendar icon on a task row in TasksPanel.vue
2. `makeRepeatable(task)` emits `create-schedule` event with `task.message`
3. AgentDetail.vue `handleCreateSchedule(message)` handler:
   - Sets `schedulePrefillMessage` ref to the message
   - Switches `activeTab` to `'schedules'`
   - Clears prefill after 100ms delay (allows reuse)
4. SchedulesPanel.vue receives `initialMessage` prop
5. Watch on `initialMessage` (with `immediate: true`) fires:
   - Pre-fills `formData.message` with the task message
   - Resets other form fields (name, cron_expression, description, timezone)
   - Opens create form (`showCreateForm = true`)
6. User fills in schedule name, cron expression, and submits (uses same Create Schedule Flow above)

**Files:**
- `src/frontend/src/components/TasksPanel.vue:251-261` - Calendar button in action row
- `src/frontend/src/components/TasksPanel.vue:451` - `defineEmits(['create-schedule'])`
- `src/frontend/src/components/TasksPanel.vue:654-657` - `makeRepeatable(task)` function
- `src/frontend/src/views/AgentDetail.vue:84` - `@create-schedule="handleCreateSchedule"` event binding
- `src/frontend/src/views/AgentDetail.vue:148` - `:initial-message="schedulePrefillMessage"` prop
- `src/frontend/src/views/AgentDetail.vue:261` - `schedulePrefillMessage` ref
- `src/frontend/src/views/AgentDetail.vue:583-594` - `handleCreateSchedule(message)` handler
- `src/frontend/src/components/SchedulesPanel.vue:487-490` - `initialMessage` prop definition
- `src/frontend/src/components/SchedulesPanel.vue:790-802` - Watch handler for `initialMessage`

### 2. Schedule Execution Flow (Automatic)

**Trigger:** APScheduler cron trigger fires in **Dedicated Scheduler Service**

> **Note (2026-02-11)**: Execution now handled entirely by the dedicated scheduler service (`src/scheduler/`), not the backend. Activity tracking is done via internal API endpoints.

```
Dedicated Scheduler               Backend                              Agent
-------------------               -------                              -----
APScheduler fires
service.py:_execute_schedule()
  |
  v
Check autonomy_enabled
Check lock (Redis)
  |
  v
Create execution record
db.create_execution(
  model_used=schedule.model)
  |
  v
POST /api/internal/execute-task -> routers/internal.py
  {async_mode: true,               |
   execution_id: "...",             v
   timeout_seconds: ...}          Spawn background task
  |                               (asyncio.create_task)
  v                                |
Receive {"status": "accepted"}     v
  |                               TaskExecutionService.execute_task()
  v                                 ├── Acquire capacity slot
Poll DB every 10s                   ├── Track activity (CHAT_START)
(SCHED-ASYNC-001)                   ├── POST to agent /api/task -------> POST /api/task
  |                                 │                                    {model: "opus"}
  v                                 │                                    Claude Code exec
Status != "running"?                ├── Sanitize response
  |                                 ├── Update execution record
  v                                 ├── Complete activity
Update scheduler's                  └── Release slot (finally)
  execution record
  |
  v
Publish WebSocket event
Release lock
```

> **Architecture change (2026-03-09, EXEC-024)**: Scheduler no longer calls agent containers directly or tracks activities via separate internal API endpoints. Instead, it dispatches to `POST /api/internal/execute-task` with `async_mode=True`. The backend's `TaskExecutionService` handles the full lifecycle (slot management, activity tracking, agent HTTP call with retry, credential sanitization). The scheduler polls the DB for completion.

**Authentication** (Updated 2026-02-15):
Claude Code uses whatever authentication is available in the agent container:
1. **OAuth session** (Claude Pro/Max subscription): If user ran `/login` in web terminal, session stored in `~/.claude.json` is used
2. **API key**: If `ANTHROPIC_API_KEY` environment variable is set (platform key or user-injected)

This enables scheduled tasks to use Claude Max subscription billing instead of API billing. The mandatory `ANTHROPIC_API_KEY` check was removed from `execute_headless_task()` in `docker/base-image/agent_server/services/claude_code.py` to support this flow.

**Capacity Handling**: If the agent's slots are full (`max_parallel_tasks` reached), `TaskExecutionService` returns a failed result and the scheduler records the execution as failed.

**Files:**
- `src/scheduler/service.py:237-345` - _execute_schedule_with_lock() execution logic
- `src/scheduler/service.py:760+` - _call_backend_execute_task() dispatches to backend
- `src/scheduler/database.py:167-250` - create_execution(), update_execution_status()
- `src/backend/routers/internal.py:186-270` - POST /api/internal/execute-task endpoint
- `src/backend/services/task_execution_service.py` - TaskExecutionService (slot, activity, agent call, sanitization)

**Key Implementation Detail (SCHED-ASYNC-001):**
The scheduler uses async fire-and-forget dispatch: the HTTP call to the backend returns immediately with `{"status": "accepted"}`, and the scheduler polls the SQLite DB every `POLL_INTERVAL` seconds (default 10s) until the execution status changes from `"running"` to `"success"` or `"failed"`. This prevents TCP connection drops on long-running tasks.

### 3. Manual Trigger Flow

**User Action:** Click play button on schedule

> **Note (2026-02-11)**: Manual triggers are now routed through the dedicated scheduler service to ensure consistent activity tracking.

```
Frontend                          Backend                           Dedicated Scheduler
--------                          -------                           -------------------
SchedulesPanel.vue               schedules.py:trigger_schedule()   POST /api/schedules/{id}/trigger
POST .../trigger                  |
                                  v
                                  httpx.post(SCHEDULER_URL +        main.py:_trigger_handler()
                                    "/api/schedules/{id}/trigger")  |
                                  |                                 v
                                  v                                 Validate schedule exists
                                  Return 200 OK                     create_task(_execute_manual_trigger)
                                  {status: "triggered"}             Return immediately
                                                                    |
                                                                    v (background)
                                                                    _execute_manual_trigger()
                                                                    - acquire lock
                                                                    - _execute_schedule_with_lock()
                                                                    - track activity via internal API
                                                                    - send task to agent
                                                                    - complete activity
```

**Note**: Manual triggers now have full activity tracking with `related_execution_id` linkage (added 2026-01-11), matching the automatic schedule execution flow.

**Files:**
- `src/frontend/src/components/SchedulesPanel.vue` - triggerSchedule() method
- `src/backend/routers/schedules.py` - trigger_schedule endpoint (proxies to scheduler)
- `src/scheduler/main.py:_trigger_handler()` - Manual trigger endpoint in scheduler
- `src/scheduler/service.py:_execute_manual_trigger()` - Execution with activity tracking

### 4. Enable/Disable Flow

**User Action:** Toggle enable/disable button

> **Note (2026-02-11)**: Backend only updates the database. The dedicated scheduler syncs changes automatically within 60 seconds.

```
Frontend                          Backend                           Dedicated Scheduler
--------                          -------                           -------------------
SchedulesPanel.vue               schedules.py:200/218              (within 60s)
POST .../enable or disable        enable_schedule()                 _sync_schedules()
                                  disable_schedule()                Detects enabled change
                                  ↓                                 APScheduler add/remove job
                                  db/schedules.py:set_schedule_enabled()
                                  (recalculates next_run_at)
```

**Access Control:** Uses `AuthorizedAgent` dependency - only users with agent access can toggle.

**Files:**
- `src/frontend/src/components/SchedulesPanel.vue` - toggleSchedule() method
- `src/backend/routers/schedules.py:200-233` - enable/disable endpoints with AuthorizedAgent
- `src/backend/db/schedules.py:290-315` - set_schedule_enabled() with next_run_at recalculation
- `src/scheduler/service.py:224-316` - _sync_schedules() detects enabled state changes

### 5. Delete Schedule Flow

**User Action:** Click delete → Confirm

> **Note (2026-02-11)**: Backend only deletes from database. The dedicated scheduler removes the job on next sync.

```
Frontend                          Backend                           Dedicated Scheduler
--------                          -------                           -------------------
SchedulesPanel.vue               schedules.py:174                  (within 60s)
DELETE .../schedules/{id}         delete_schedule()                 _sync_schedules()
                                  ↓                                 Detects deleted schedule
                                  db.delete_schedule()              APScheduler remove_job()
                                  DELETE executions
                                  DELETE schedule
```

**Access Control:** Uses `CurrentUser` dependency with manual ownership check via `db.delete_schedule()`.

**Files:**
- `src/frontend/src/components/SchedulesPanel.vue` - deleteSchedule() method
- `src/backend/routers/schedules.py:174-196` - delete_schedule endpoint
- `src/backend/db/schedules.py` - db.delete_schedule()
- `src/scheduler/service.py:224-316` - _sync_schedules() detects deleted schedules

### 6. View All Executions (Tasks Tab)

**User Action:** Click "Tasks" tab on AgentDetail.vue

> **PERF-001 (2026-02-21)**: List endpoint now returns `ExecutionSummary` objects that exclude large text fields (`response`, `error`, `tool_calls`, `execution_log`). Full details fetched on-demand when user expands a task row.

```
Frontend                          Backend                           Database
--------                          -------                           --------
TasksPanel.vue                   schedules.py:435              →    db/schedules.py:655
GET /api/agents/{name}/           get_agent_executions()            get_agent_executions_summary()
executions?limit=100              AuthorizedAgent dependency         SELECT (specific columns)
                                  ↓                                  FROM schedule_executions
                                  return ExecutionSummary[]          WHERE agent_name = ?
                                                                     ORDER BY started_at DESC
                                                                     (uses idx_executions_agent_started)
```

**Access Control:** Uses `AuthorizedAgent` dependency - `name: AuthorizedAgent` validates access automatically.

**Files:**
- `src/frontend/src/components/ExecutionsPanel.vue` - loadExecutions() method
- `src/backend/routers/schedules.py:283-290` - get_agent_executions endpoint with AuthorizedAgent
- `src/backend/database.py:1462-1492` - db.get_agent_executions()

**Frontend Computed Stats (calculated client-side):**
- `successRate` - % of executions with status="success"
- `totalCost` - Sum of all execution costs
- `avgDuration` - Average duration_ms of completed executions

---

## API Endpoints

| Method | Path | Description | File:Line | Access |
|--------|------|-------------|-----------|--------|
| GET | `/api/agents/{name}/schedules` | List schedules | schedules.py:78 | AuthorizedAgent |
| POST | `/api/agents/{name}/schedules` | Create schedule | schedules.py:85 | CurrentUser |
| GET | `/api/agents/{name}/schedules/{id}` | Get schedule | schedules.py:116 | AuthorizedAgent |
| PUT | `/api/agents/{name}/schedules/{id}` | Update schedule | schedules.py:132 | CurrentUser |
| DELETE | `/api/agents/{name}/schedules/{id}` | Delete schedule | schedules.py:174 | CurrentUser |
| POST | `/api/agents/{name}/schedules/{id}/enable` | Enable | schedules.py:200 | AuthorizedAgent |
| POST | `/api/agents/{name}/schedules/{id}/disable` | Disable | schedules.py:218 | AuthorizedAgent |
| POST | `/api/agents/{name}/schedules/{id}/trigger` | Manual trigger | schedules.py:236 | AuthorizedAgent |
| GET | `/api/agents/{name}/schedules/{id}/executions` | Execution history | schedules.py:265 | AuthorizedAgent |
| GET | `/api/agents/{name}/schedules/{id}/analytics` | Per-schedule analytics (#868) — counts, success rate, duration p50/p95/p99, cost total, tool-call top-5 by total duration, daily timeline; `?window_hours=` ∈ {24, 168, 720}, default 168 | schedules.py:255 | AuthorizedAgent |
| GET | `/api/agents/{name}/executions` | All agent executions (summary) | schedules.py:435 | AuthorizedAgent |
| GET | `/api/agents/{name}/executions/{id}` | Get specific execution (full) | schedules.py:462 | AuthorizedAgent |
| GET | `/api/agents/{name}/executions/{id}/log` | Get execution log | schedules.py:309 | AuthorizedAgent |
| GET | `/api/agents/scheduler/status` | Scheduler status (admin) | schedules.py:354 | Admin only |
| POST | `/api/agents/{name}/schedules/{id}/webhook` | Generate/rotate webhook token | schedules.py:474 | CurrentUser |
| GET | `/api/agents/{name}/schedules/{id}/webhook` | Get webhook status and URL | schedules.py:511 | AuthorizedAgent |
| DELETE | `/api/agents/{name}/schedules/{id}/webhook` | Revoke webhook token | schedules.py:537 | CurrentUser |

### Access Control Pattern

Endpoints use FastAPI dependencies from `src/backend/dependencies.py`:

```python
# Type aliases used in schedules router
from dependencies import AuthorizedAgent, CurrentUser

# AuthorizedAgent dependency (schedules.py:78)
@router.get("/{name}/schedules")
async def list_agent_schedules(name: AuthorizedAgent):
    # name is validated and returned by get_authorized_agent()
    # Raises 403 if user cannot access agent
    ...

# CurrentUser dependency (schedules.py:85)
@router.post("/{name}/schedules")
async def create_schedule(
    name: str,
    schedule_data: ScheduleCreate,
    current_user: User = Depends(get_current_user)
):
    # Manual check via db.create_schedule() which validates access
    ...
```

**Dependency definitions** (`src/backend/dependencies.py:168-259`):

| Dependency | Purpose | Check |
|------------|---------|-------|
| `AuthorizedAgent` | Read access to agent | `db.can_user_access_agent()` |
| `OwnedAgent` | Owner access (delete, share) | `db.can_user_share_agent()` |
| `CurrentUser` | Authenticated user | JWT/MCP key validation |

---

## Webhook Triggers (WEBHOOK-001, #291)

Each schedule can optionally expose a public webhook URL. Calling the URL triggers the schedule exactly once — same execution path as a manual trigger but with `triggered_by="webhook"` in the execution record. No JWT or API key is required; the opaque token embedded in the URL is the credential.

See [webhook-triggers.md](webhook-triggers.md) for the complete vertical slice.

### Token Lifecycle

```
POST .../webhook  →  32-byte token stored in agent_schedules.webhook_token
                      webhook_enabled = 1
                      Returns full URL: {base}/api/webhooks/{token}

GET .../webhook   →  Returns current status (has_token, webhook_enabled, webhook_url)

DELETE .../webhook →  token = NULL, webhook_enabled = 0 — old URL returns 404 immediately
```

Calling `POST .../webhook` again rotates the token, instantly invalidating the previous URL.

### Public Trigger Endpoint

**File**: `src/backend/routers/webhooks.py` — router prefix `/api/webhooks`, **no JWT auth**

```
POST /api/webhooks/{webhook_token}
Optional body: {"context": "...", "metadata": {...}}
Returns: 202 Accepted
```

Flow: token format check → DB lookup via partial index → rate limit (10/60s, Redis, fail-open) → append context to message → forward to scheduler → audit log.

### Schema Additions (migration `agent_schedules_webhook`)

**File**: `src/backend/db/migrations.py:1631-1648`

```sql
ALTER TABLE agent_schedules ADD COLUMN webhook_token TEXT;          -- 43-char urlsafe token, nullable
ALTER TABLE agent_schedules ADD COLUMN webhook_enabled INTEGER DEFAULT 0;

-- O(1) lookup; partial so only non-NULL rows are indexed
CREATE UNIQUE INDEX idx_schedules_webhook_token
    ON agent_schedules(webhook_token)
    WHERE webhook_token IS NOT NULL;
```

### DB Methods

**File**: `src/backend/db/schedules.py:515-594`

| Method | Purpose |
|--------|---------|
| `generate_webhook_token(schedule_id)` | `secrets.token_urlsafe(32)` → UPDATE + set `webhook_enabled=1` |
| `get_schedule_by_webhook_token(token)` | O(1) SELECT via partial index |
| `set_webhook_enabled(schedule_id, enabled)` | Enable/disable without revoking token |
| `revoke_webhook_token(schedule_id)` | NULL token, set `webhook_enabled=0` |
| `get_webhook_status(schedule_id)` | Returns `{webhook_token, webhook_enabled, has_token}` |

### Scheduler Integration

**File**: `src/scheduler/main.py:136-221`

`_trigger_handler` reads `triggered_by` from the JSON body. Accepts `"manual"` or `"webhook"`; defaults to `"manual"`. Passes value through to `_execute_schedule_with_lock`, which stamps it on the `schedule_executions` row. Execution flow is otherwise identical to a manual trigger.

### Security

- Token entropy: `secrets.token_urlsafe(32)` = 43 chars ≈ 256 bits
- Format guard: regex `^[A-Za-z0-9_\-]{20,60}$` before DB lookup (avoids injection)
- Rate limit: 10 calls / 60s per token; fail-open on Redis unavailability
- Prompt injection: caller `context` wrapped in labeled separator, capped at 4000 chars
- Audit: every trigger writes to `audit_log` with caller IP; token truncated to 8 chars in endpoint field

---

## Database Schema

### agent_schedules
**Location**: `src/backend/database.py:457-477`

```sql
CREATE TABLE agent_schedules (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    name TEXT NOT NULL,
    cron_expression TEXT NOT NULL,      -- "0 9 * * *" format
    message TEXT NOT NULL,               -- Task message to send
    enabled INTEGER DEFAULT 1,
    timezone TEXT DEFAULT 'UTC',
    description TEXT,
    owner_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_run_at TEXT,                    -- Last execution time
    next_run_at TEXT,                    -- Calculated next run
    timeout_seconds INTEGER DEFAULT 900, -- Execution timeout (default 15 min, max 2h)
    allowed_tools TEXT,                  -- JSON array of tool names, NULL = all tools allowed
    model TEXT,                          -- Model override (MODEL-001), NULL = agent default
    webhook_token TEXT,                  -- WEBHOOK-001: opaque 43-char urlsafe token, nullable
    webhook_enabled INTEGER DEFAULT 0,  -- WEBHOOK-001: 0 = disabled, 1 = active
    FOREIGN KEY (owner_id) REFERENCES users(id)
);
```

**New Fields (2026-02-20, 2026-03-02):**
| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `timeout_seconds` | INTEGER | 900 | Per-schedule execution timeout (5m-2h) |
| `allowed_tools` | TEXT | NULL | JSON array of allowed Claude Code tools, NULL = all tools |
| `model` | TEXT | NULL | Model override for this schedule (MODEL-001). NULL = agent default |

### schedule_executions
**Location**: `src/backend/database.py:273-295`

```sql
CREATE TABLE schedule_executions (
    id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    status TEXT NOT NULL,                -- pending, running, success, failed
    started_at TEXT NOT NULL,            -- UTC timestamp with 'Z' suffix
    completed_at TEXT,                   -- UTC timestamp with 'Z' suffix
    duration_ms INTEGER,
    message TEXT NOT NULL,               -- The message that was sent
    response TEXT,                       -- Agent's response (truncated to 10KB)
    error TEXT,                          -- Error message if failed
    triggered_by TEXT NOT NULL,          -- "schedule" or "manual"
    -- Observability fields (added 2025-11-29)
    context_used INTEGER,                -- Tokens used in context window
    context_max INTEGER,                 -- Max context window size
    cost REAL,                           -- Execution cost in USD
    tool_calls TEXT,                     -- JSON array of tool calls
    execution_log TEXT,                  -- Full Claude Code execution transcript (JSON)
    model_used TEXT,                     -- Model used for this execution (MODEL-001)
    FOREIGN KEY (schedule_id) REFERENCES agent_schedules(id)
);
```

> **Timezone Note (2026-01-15)**: All timestamps (`started_at`, `completed_at`, `last_run_at`, `next_run_at`) are stored as UTC with 'Z' suffix. Backend uses `utc_now_iso()` from `utils/helpers.py`. See [Timezone Handling Guide](/docs/TIMEZONE_HANDLING.md).

---

## WebSocket Events

Broadcast via dedicated scheduler -> Redis pub/sub -> backend WebSocket relay for real-time UI updates:

### schedule_execution_started
**Source**: Dedicated scheduler (`src/scheduler/service.py`) via Redis `scheduler:events` channel

```json
{
  "type": "schedule_execution_started",
  "agent": "agent-name",
  "schedule_id": "abc123",
  "execution_id": "def456",
  "schedule_name": "Daily Report"
}
```

### schedule_execution_completed
**Source**: Dedicated scheduler (`src/scheduler/service.py`) via Redis `scheduler:events` channel

```json
{
  "type": "schedule_execution_completed",
  "agent": "agent-name",
  "schedule_id": "abc123",
  "execution_id": "def456",
  "status": "success",          // or "failed"
  "error": null                 // error message if failed
}
```

---

## Cron Expression Format

5-field cron format: `minute hour day month day_of_week`

| Field | Values | Example |
|-------|--------|---------|
| Minute | 0-59 | `*/15` (every 15 min) |
| Hour | 0-23 | `9` (9 AM) |
| Day | 1-31 | `*` (every day) |
| Month | 1-12 | `*` (every month) |
| Day of Week | 0-6 (Sun=0) | `1` (Monday) |

**Common Presets:**
- `0 9 * * *` - Daily at 9 AM
- `0 9 * * 1` - Weekly on Monday at 9 AM
- `0 */6 * * *` - Every 6 hours
- `*/30 * * * *` - Every 30 minutes

---

## Dependencies

**Python packages** (docker/backend/Dockerfile):
- `apscheduler==3.10.4` - Async job scheduler
- `croniter==2.0.1` - Cron expression parsing
- `pytz==2024.1` - Timezone support
- `httpx` - Async HTTP client (used by AgentClient)

---

## Agent HTTP Client Service

**Added**: 2025-12-31 (Plan 03 refactoring)
**Updated**: 2026-02-11 - Dedicated scheduler has its own `AgentClient` at `src/scheduler/agent_client.py`

The dedicated scheduler uses `AgentClient` from `src/scheduler/agent_client.py` for all HTTP communication with agent containers. This centralizes URL construction, timeout handling, and response parsing.

### Usage in Dedicated Scheduler

**Location**: `src/scheduler/agent_client.py`

```python
from scheduler.agent_client import get_agent_client, AgentNotReachableError

# Send task to agent using AgentClient.task() for raw log format
client = get_agent_client(schedule.agent_name)
task_response = await client.task(schedule.message, execution_id=execution.id)

# Update execution status with parsed response
self.db.update_execution_status(
    execution_id=execution.id,
    status="success",
    response=task_response.response_text,
    context_used=task_response.metrics.context_used,
    context_max=task_response.metrics.context_max,
    cost=task_response.metrics.cost_usd,
    tool_calls=task_response.metrics.tool_calls_json,
    execution_log=task_response.metrics.execution_log_json  # Raw Claude Code format
)
```

### AgentClient API

**Location**: `src/backend/services/agent_client.py`

| Method | Purpose | Timeout | Retry | Circuit Breaker |
|--------|---------|---------|-------|-----------------|
| `task(message)` | Execute stateless task (raw log format) | 900s (15 min) | No | Yes |
| `chat(message, stream)` | Send chat message (simplified log format) | 900s (15 min) | No | Yes |
| `get_session()` | Get context info | 5s | Yes (3x) | Yes |
| `health_check()` | Check agent health | 5s | Yes (3x) | Yes |

**Important**: The scheduler uses `task()` not `chat()`. The `task()` method calls `/api/task` which returns raw Claude Code `stream-json` format required by the log viewer. The `chat()` method calls `/api/chat` which returns a simplified format that is not compatible with `parseExecutionLog()`.

**Reliability (RELIABILITY-001, refined by #474)**: All calls go through a per-agent circuit breaker. After 3 consecutive `ConnectError`/`ConnectTimeout` failures (the `CIRCUIT_FAILURE_EXCEPTIONS` tuple in `services/agent_client.py`), the circuit opens and subsequent calls fail immediately with `AgentCircuitOpenError` (30s cooldown). Read/write timeouts, pool exhaustion, mid-write broken-pipe/reset, and garbled HTTP framing (the `TRANSIENT_TRANSPORT_EXCEPTIONS` tuple) still surface as `AgentNotReachableError` but do **not** count toward the threshold — those are client-side / mid-request noise, not agent unhealth. Only idempotent calls (`health_check`, `get_session`) have automatic retries (3 attempts, exponential backoff). `task()` and `chat()` are NOT retried to prevent double-execution. Connection pooling reuses persistent `httpx.AsyncClient` instances per agent.

### Response Models

**AgentChatResponse** (`agent_client.py:138-142`):
```python
@dataclass
class AgentChatResponse:
    response_text: str           # Agent's text response (truncated to 10KB)
    metrics: AgentChatMetrics    # Parsed observability data
    raw_response: Dict[str, Any] # Original JSON from agent
```

**AgentChatMetrics** (`agent_client.py:127-134`):
```python
@dataclass
class AgentChatMetrics:
    context_used: int            # Tokens in context window
    context_max: int             # Max context window size
    context_percent: float       # Usage percentage
    cost_usd: Optional[float]    # Execution cost
    tool_calls_json: Optional[str]      # JSON array of tool calls
    execution_log_json: Optional[str]   # Full execution transcript
```

### Error Handling

**Exceptions** (`agent_client.py:158-177`):

| Exception | Meaning | HTTP Equivalent |
|-----------|---------|-----------------|
| `AgentNotReachableError` | Connection failed or timeout | 503 Service Unavailable |
| `AgentCircuitOpenError` | Circuit breaker open (agent known unhealthy) | 503 fast-fail |
| `AgentRequestError` | Agent returned error status | 4xx/5xx from agent |
| `AgentClientError` | Base exception | General failure |

**Scheduler error handling** (dedicated scheduler `src/scheduler/service.py:295-313`):
```python
try:
    client = get_agent_client(schedule.agent_name)
    task_response = await client.task(schedule.message, execution_id=execution.id)
    # ... success handling
except AgentNotReachableError as e:
    error_msg = f"Agent not reachable: {str(e)}"
    # ... failure handling with specific message
except Exception as e:
    error_msg = str(e)
    # ... generic failure handling
```

---

## Related Files

> **Note (2026-02-11)**: The embedded scheduler (`src/backend/services/scheduler_service.py`) has been **removed**. Schedule execution is now handled by the dedicated scheduler service.

| Category | File | Purpose |
|----------|------|---------|
| **Scheduler** | `src/scheduler/service.py` | APScheduler service (execution, sync) |
| **Scheduler** | `src/scheduler/main.py` | Service entry point, health endpoints, manual trigger |
| **Scheduler** | `src/scheduler/database.py` | Schedule/execution DB operations |
| **Scheduler** | `src/scheduler/agent_client.py` | Agent HTTP client for scheduler |
| **Scheduler** | `src/scheduler/locking.py` | Redis distributed locks |
| Backend | `src/backend/routers/schedules.py` | REST API endpoints (CRUD, proxies trigger) |
| Backend | `src/backend/routers/internal.py` | Internal API for activity tracking |
| Backend | `src/backend/db/schedules.py` | Schedule DB operations with next_run_at calculation |
| Backend | `src/backend/dependencies.py` | Access control dependencies |
| Frontend | `src/frontend/src/components/SchedulesPanel.vue` | Schedule management UI |
| Frontend | `src/frontend/src/components/ExecutionsPanel.vue` | Execution history UI |
| Frontend | `src/frontend/src/views/AgentDetail.vue` | Tab integration |
| Docker | `docker/scheduler/Dockerfile` | Scheduler container |

---

## Execution Observability

Each execution now tracks detailed observability data extracted from the agent's response:

### Fields Captured

| Field | Type | Source | Description |
|-------|------|--------|-------------|
| `context_used` | INTEGER | session.context_tokens | Tokens used in context window |
| `context_max` | INTEGER | session.context_window | Maximum context window size |
| `cost` | REAL | metadata.cost_usd | Cost in USD for this execution |
| `tool_calls` | TEXT | execution_log (JSON) | Array of tool calls made |

### UI Features

1. **Progress Bar**: Visual context usage indicator (green < 50%, yellow < 75%, orange < 90%, red >= 90%)
2. **Cost Display**: Formatted as `$0.0123` in execution rows
3. **Execution Detail Modal**: Click any execution to see full details:
   - Message sent
   - Response received
   - Tool calls with duration and success status
   - Error details if failed
4. **Executions Tab**: Dedicated tab on AgentDetail.vue showing:
   - All executions across all schedules
   - Summary stats (total, success rate, total cost, avg duration)
   - Sortable table view with full observability data

---

## Scheduler Service Implementation

> **Note (2026-02-11)**: This section documents the **Dedicated Scheduler Service** (`src/scheduler/`), not the removed embedded scheduler.

For complete scheduler service documentation, see [scheduler-service.md](scheduler-service.md).

### Key Implementation Details

**Initialization** (`src/scheduler/service.py:71-98`):
- Creates APScheduler with MemoryJobStore
- Loads all enabled schedules from database on startup
- Builds snapshot for change detection during sync

**Periodic Sync** (`src/scheduler/service.py:224-316`):
- Every 60 seconds, compares database state with in-memory jobs
- Detects new, deleted, and updated schedules
- Adds/removes APScheduler jobs accordingly

**Cron Parsing** (`src/scheduler/service.py:100-120`):
```python
def _parse_cron(self, cron_expression: str) -> Dict:
    """
    Parse a cron expression into APScheduler CronTrigger kwargs.

    Format: minute hour day month day_of_week
    Examples:
      - "0 9 * * *" = Every day at 9:00 AM
      - "*/15 * * * *" = Every 15 minutes
      - "0 0 * * 0" = Every Sunday at midnight
    """
    parts = cron_expression.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron expression: {cron_expression}. Expected 5 parts.")

    return {
        'minute': parts[0],
        'hour': parts[1],
        'day': parts[2],
        'month': parts[3],
        'day_of_week': parts[4]
    }
```

---

## Activity Stream Integration (NEW: Req 9.7)

**Implemented**: 2025-12-02
**Updated**: 2026-01-11 - `related_execution_id` now a top-level field for structured SQL queries
**Updated**: 2026-02-11 - Activity tracking moved to internal API for dedicated scheduler

Schedule executions now create persistent activity records in the unified activity stream for cross-platform observability. The dedicated scheduler tracks activities via internal API endpoints.

### Activity Tracking Flow

**Internal API Endpoints** (used by dedicated scheduler):
- `POST /api/internal/activities/track` - Start tracking an activity
- `POST /api/internal/activities/{id}/complete` - Mark activity as completed/failed

**Schedule Start** (dedicated scheduler calls internal API):
```python
# Dedicated scheduler makes HTTP call to backend
response = await httpx.post(
    f"{BACKEND_URL}/api/internal/activities/track",
    json={
        "agent_name": schedule.agent_name,
        "activity_type": "schedule_start",
        "user_id": schedule.owner_id,
        "triggered_by": "schedule",  # or "manual"
        "related_execution_id": execution.id,
        "details": {
            "schedule_id": schedule.id,
            "schedule_name": schedule.name,
            "cron_expression": schedule.cron_expression
        }
    }
)
activity_id = response.json()["activity_id"]
```

**Schedule Completion** (dedicated scheduler calls internal API):
```python
# Dedicated scheduler makes HTTP call to backend
await httpx.post(
    f"{BACKEND_URL}/api/internal/activities/{activity_id}/complete",
    json={
        "status": "completed",
        "details": {
            "context_used": task_response.metrics.context_used,
            "context_max": task_response.metrics.context_max,
            "cost_usd": task_response.metrics.cost_usd,
            "tool_count": len(execution_log) if execution_log else 0
        }
    }
)
```

**Schedule Failure** (dedicated scheduler calls internal API):
```python
await httpx.post(
    f"{BACKEND_URL}/api/internal/activities/{activity_id}/complete",
    json={
        "status": "failed",
        "error": error_msg
    }
)
```

### Database Records

Each schedule execution creates:
1. **schedule_executions** record (existing): Full execution details, response, error, cost, tool_calls
   - ID format: `token_urlsafe(16)` - permanent, used for API and UI navigation
2. **agent_activities** record (NEW): Unified activity stream with:
   - `activity_type`: "schedule_start"
   - `related_execution_id`: Links to `schedule_executions.id` - enables structured SQL queries
   - `duration_ms`: Calculated on completion
   - Parent-child relationships with tool calls (future)

**Activity Linkage Pattern** (2026-01-11):
```sql
-- Find all activities for a given execution
SELECT * FROM agent_activities
WHERE related_execution_id = ?
ORDER BY started_at;

-- Join activities with execution details
SELECT a.*, e.status, e.cost, e.duration_ms
FROM agent_activities a
JOIN schedule_executions e ON a.related_execution_id = e.id
WHERE e.agent_name = ?;
```

### WebSocket Events

Activity events broadcast in addition to existing schedule events:
```json
{
  "type": "agent_activity",
  "agent_name": "my-agent",
  "activity_id": "uuid-123",
  "activity_type": "schedule_start",
  "activity_state": "started",
  "action": "Running: Daily Report",
  "timestamp": "2025-12-02T09:00:00.000Z",
  "details": {
    "schedule_name": "Daily Report",
    "schedule_id": "sched-456"
  }
}
```

### Query Examples

**Get all schedule activities for an agent**:
```bash
curl "http://localhost:8000/api/agents/my-agent/activities?activity_type=schedule_start&limit=20"
```

**Get schedule activity with execution details**:
```sql
SELECT a.*, se.response, se.error, se.cost, se.tool_calls
FROM agent_activities a
JOIN schedule_executions se ON a.related_execution_id = se.id
WHERE a.activity_type = 'schedule_start'
  AND a.agent_name = 'my-agent'
ORDER BY a.started_at DESC
```

**Cross-agent schedule timeline**:
```bash
curl "http://localhost:8000/api/activities/timeline?activity_types=schedule_start&start_time=2025-12-02T00:00:00"
```

### Benefits

- Unified observability across chat and schedules
- Parent-child relationships (future: tool calls within schedules)
- Cross-agent timeline queries
- Real-time WebSocket monitoring
- Full audit trail with user attribution

See: `activity-stream.md` for complete details

---

## MCP Integration (Added 2026-01-29)

Schedule management is now available via MCP tools, enabling programmatic control from Claude Code or other MCP clients.

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `list_agent_schedules` | List all schedules for an agent |
| `create_agent_schedule` | Create a new cron-based schedule |
| `get_agent_schedule` | Get schedule details |
| `update_agent_schedule` | Update schedule configuration |
| `delete_agent_schedule` | Delete a schedule |
| `toggle_agent_schedule` | Enable/disable a schedule |
| `trigger_agent_schedule` | Manually trigger execution |
| `get_schedule_executions` | Get execution history |

### MCP Tool Parameters

Both `create_agent_schedule` and `update_agent_schedule` support the full schedule configuration:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `timeout_seconds` | number | 900 | Execution timeout in seconds |
| `allowed_tools` | string[] | null (all) | Restrict tools available during execution |
| `model` | string | null (agent default) | Model override for executions |

**Files**: `src/mcp-server/src/tools/schedules.ts` (Zod schemas), `src/mcp-server/src/types.ts` (TypeScript interfaces)

### Example: Orchestrator Creating Worker Schedule

```javascript
// Head agent (Claude Code) creating a schedule on a worker
await mcp.call("create_agent_schedule", {
  agent_name: "report-generator",
  name: "Daily Sales Report",
  cron_expression: "0 9 * * 1-5",  // Weekdays at 9am
  message: "Generate the daily sales report and email to team",
  timezone: "America/New_York",
  enabled: true,
  timeout_seconds: 1800,                       // 30 min timeout
  allowed_tools: ["Read", "Glob", "Grep", "WebFetch"],  // Read-only + web
  model: "claude-sonnet-4-6"                    // Use Sonnet for cost efficiency
});
```

### Access Control

| Key Scope | Own Schedules | Other Agents' Schedules |
|-----------|---------------|-------------------------|
| User | Full CRUD | Full CRUD (if agent owner) |
| Agent | Full CRUD | Read/toggle/trigger only (if permitted) |
| System | Full CRUD | Full CRUD (all agents) |

See `mcp-orchestration.md` for full MCP authentication and access control details.

---

## Known Issues

None currently. Previous issues have been resolved.

### FIXED: New schedules don't execute until scheduler restart

**Fixed**: 2026-01-29

**Original Symptoms**:
- `next_run_at` field was `None` for newly created schedules
- Schedule never executed until scheduler container was restarted
- Timeline markers didn't show (they depend on `next_run_at`)

**Root Cause**: The backend created schedules without calculating `next_run_at`, and the dedicated scheduler only loaded schedules on startup with no sync mechanism.

**Solution** (two parts):

1. **Calculate `next_run_at` in database layer** (`src/backend/db/schedules.py`):
   - `create_schedule()` now calculates `next_run_at` using croniter before INSERT
   - `update_schedule()` recalculates when cron_expression, timezone, or enabled status changes
   - `set_schedule_enabled()` recalculates when enabling, clears when disabling

2. **Periodic schedule sync in dedicated scheduler** (`src/scheduler/service.py`):
   - Added `_sync_schedules()` method that compares database state with in-memory APScheduler jobs
   - Detects new, updated, and deleted schedules
   - Called every 60 seconds (configurable via `SCHEDULE_RELOAD_INTERVAL` env var)
   - Automatically adds/removes/updates jobs without restart

**Data Flow (Fixed)**:
```
API Request                  Backend                    Scheduler Container
-----------                  -------                    -------------------
POST /schedules  ------>  db/schedules.py:create_schedule()
                          - Calculate next_run_at via croniter
                          - INSERT with next_run_at populated
                          - Return 201 Created

                                                        (within 60s sync interval)
                                                        service.py:_sync_schedules()
                                                        - Detect new schedule
                                                        - Add APScheduler job
                                                        - Log: "Adding new schedule X"
```

**Related Files**:
- `src/backend/db/schedules.py:26-50` - `_calculate_next_run_at()` helper
- `src/backend/db/schedules.py:99-168` - `create_schedule()` with next_run_at
- `src/backend/db/schedules.py:199-261` - `update_schedule()` with recalculation
- `src/backend/db/schedules.py:290-315` - `set_schedule_enabled()` with recalculation
- `src/scheduler/service.py:224-316` - `_sync_schedules()` and `_sync_agent_schedules()`
- `src/scheduler/config.py:46-48` - `schedule_reload_interval` setting

---

## Status
**Updated 2026-03-09** - **MCP Tool Parity (#85)**: Added `timeout_seconds`, `allowed_tools`, and `model` parameters to MCP `create_agent_schedule` and `update_agent_schedule` tools. TypeScript interfaces updated in `src/mcp-server/src/types.ts`, Zod schemas updated in `src/mcp-server/src/tools/schedules.ts`.
**Updated 2026-03-02** - **MODEL-001 Model Selection**: Per-schedule model override. New `model` column on `agent_schedules`, `model_used` column on `schedule_executions`. ModelSelector.vue component in create/edit form. Model forwarded through scheduler service to agent container. model_used recorded on every execution for audit. See [model-selection.md](model-selection.md).
**Updated 2026-02-22** - **Dashboard Schedule Stats**: Added `get_all_agents_schedule_counts()` method to `db/schedules.py:719-743`. Returns schedule counts (total and enabled) per agent. Used by `GET /api/agents/execution-stats` endpoint to populate `schedules_total` and `schedules_enabled` fields. Dashboard AgentNode now displays "X/Y schedules" with "(paused)" indicator when autonomy disabled.

**Updated 2026-02-21** - **Bug Fix (EXEC-023)**: Fixed scheduled executions missing `claude_session_id`. The dedicated scheduler service (`src/scheduler/`) had its own code path for task execution that was not capturing `session_id` from agent responses. Four files were updated:
- `src/scheduler/models.py:85` - Added `session_id` field to `AgentTaskMetrics` dataclass
- `src/scheduler/agent_client.py:176-224` - `_parse_task_response()` now extracts `session_id` from agent response
- `src/scheduler/database.py:233-284` - `update_execution_status()` accepts and stores `claude_session_id` parameter
- `src/scheduler/service.py:483-493` - Passes `task_response.metrics.session_id` to database update as `claude_session_id`

This enables "Continue Execution as Chat" for scheduled executions (previously only worked for manual task executions via backend `/task` endpoint).

**Updated 2026-02-21** - **Bug Fix (EXEC-023)**: Fixed `DatabaseManager.update_execution_status()` wrapper in `src/backend/database.py:1295-1299` - was missing `claude_session_id` parameter. This affected all code paths that update execution status (scheduled tasks, manual tasks, chat executions). The underlying `db/schedules.py:update_execution_status()` (lines 559-610) already supported the parameter for the "Continue Execution as Chat" feature.
**Updated 2026-02-21** - **PERF-001 Performance Optimization**: List endpoint now returns `ExecutionSummary` (excludes large text fields). Task details fetched on-demand. See [tasks-tab.md](tasks-tab.md) for full implementation details.
**Updated 2026-02-20** - **Configurable Timeout and Allowed Tools**: Per-schedule execution configuration - custom timeout (5m-2h) and tool restrictions. See "Per-Schedule Execution Configuration" section below.
**Updated 2026-02-15** - **Claude Max Subscription Support**: Scheduled executions now work with Claude Max subscription authentication. If agent has OAuth session from `/login` stored in `~/.claude.json`, scheduled tasks use the subscription instead of requiring `ANTHROPIC_API_KEY`. See "Authentication" section in Schedule Execution Flow.
**Updated 2026-02-11** - **SCHEDULER CONSOLIDATION**: Embedded scheduler removed. All references updated to dedicated scheduler (`src/scheduler/`). Manual triggers routed through scheduler. Activity tracking via internal API.
**Updated 2026-01-29** - FIXED: Scheduler sync bug - new schedules now work without restart (next_run_at calculated in DB layer + periodic sync)
**Updated 2026-01-29** - Added Dashboard Timeline Integration section (TSM-001) with full data flow, frontend implementation, and visual design details
**Updated 2026-01-29** - Added MCP Integration section with 8 schedule management tools
**Updated 2026-01-15** - Added timezone handling note for UTC timestamps with 'Z' suffix
**Updated 2026-01-12** - Added "Make Repeatable" flow documentation - create schedule from existing task via calendar icon in Tasks tab
**Updated 2026-01-11** - Execution record created BEFORE activity tracking for proper `related_execution_id` linkage. Manual trigger now has full activity tracking with queue integration.
**Updated 2025-01-02** - Scheduler now uses `AgentClient.task()` instead of `AgentClient.chat()` for raw log format compatibility with execution log viewer
**Updated 2025-12-31** - Documented access control dependencies and AgentClient service (Plan 02/03 refactoring)
**Updated 2025-12-29** - Fixed API endpoint line numbers to match current schedules.py (post-refactoring)
**Updated 2025-12-06** - Added Execution Queue integration documentation

---

## Dashboard Timeline Integration (TSM-001)

**Added**: 2026-01-29

Enabled schedules are visualized on the Dashboard Timeline as purple triangle markers at their `next_run_at` positions.

### Data Flow

```
Dashboard.vue                    network.js                     Backend
     |                               |                              |
     | onMounted()                   |                              |
     +----> fetchSchedules() -----> GET /api/ops/schedules -----> ops.py:427
     |                               |   ?enabled_only=true         |
     |                               |                              |
     |      schedules.value <------ [{id, agent_name, name, --------|
     |                               |   cron_expression,           |
     |                               |   next_run_at, enabled}]     |
     |                               |                              |
     | :schedules="schedules"        |                              |
     +----> ReplayTimeline.vue       |                              |
                |                    |                              |
                | scheduleMarkers    |                              |
                | (computed)         |                              |
                v                    |                              |
          Purple triangles at        |                              |
          next_run_at positions      |                              |
```

### Frontend Implementation

**Store** (`network.js:54, 1214-1225`):
```javascript
const schedules = ref([])

async function fetchSchedules() {
  const response = await axios.get('/api/ops/schedules', {
    params: { enabled_only: true }
  })
  schedules.value = response.data.schedules || []
}
```

**Dashboard** (`Dashboard.vue:159, 424`):
```javascript
// In onMounted
await networkStore.fetchSchedules()

// Pass to ReplayTimeline
<ReplayTimeline :schedules="schedules" ... />
```

**ReplayTimeline** (`ReplayTimeline.vue:799-834, 312-322, 1014-1034`):
- `scheduleMarkers` computed property filters and positions markers
- SVG `<polygon>` renders purple downward triangles
- Click navigates to `/agents/{agent_name}?tab=schedules`
- Hover shows tooltip with schedule name, next run time, cron expression, message preview

### Timeline Extension for Future Schedules

The timeline now extends into the future to show upcoming schedule markers (`ReplayTimeline.vue:534-545`):

```javascript
// Extend timeline into future to show upcoming schedule markers
if (props.schedules && props.schedules.length > 0) {
  const futureScheduleTimes = props.schedules
    .filter(s => s.next_run_at && s.enabled)
    .map(s => getTimestampMs(s.next_run_at))

  if (futureScheduleTimes.length > 0) {
    const furthestSchedule = Math.max(...futureScheduleTimes)
    maxEnd = Math.max(maxEnd, furthestSchedule + futurePaddingMs)
  }
}
```

### Visual Design

- **Shape**: Downward-pointing triangle (8px wide x 8px tall)
- **Color**: Purple (`#8b5cf6`) - matches scheduled execution bar color
- **Position**: Top of agent row at `next_run_at` timestamp
- **Interaction**: Hover shows tooltip, click navigates to Schedules tab

### See Also

- `docs/requirements/TIMELINE_SCHEDULE_MARKERS.md` - Requirements document
- `dashboard-timeline-view.md` - Full timeline documentation with Test Case 9
- `replay-timeline.md` - ReplayTimeline component with Section 5a

---

## Trinity Connect Integration (Added 2026-02-05)

Schedule execution events are now broadcast to Trinity Connect clients via the filtered WebSocket endpoint.

### Event Publishing Pattern

> **Note (2026-02-11)**: The dedicated scheduler publishes events to Redis pub/sub channel `scheduler:events`. The backend subscribes and relays to WebSocket managers.

**Location**: `src/scheduler/service.py:387-401`

```python
async def _publish_event(self, event: dict):
    """Publish an event to Redis for WebSocket compatibility."""
    if not config.publish_events:
        return
    try:
        event_json = json.dumps(event)
        self.redis.publish("scheduler:events", event_json)
        logger.debug(f"Published event: {event['type']}")
    except Exception as e:
        logger.error(f"Failed to publish event: {e}")
```

### Events Broadcast to Trinity Connect

| Event Type | Trigger | Agent Name Field |
|------------|---------|------------------|
| `schedule_execution_started` | Schedule begins execution | `agent` |
| `schedule_execution_completed` | Schedule completes (success or failure) | `agent` |

### Backend Event Relay

**Location**: `src/backend/main.py` (Redis subscriber)

The backend subscribes to the `scheduler:events` Redis channel and relays events to both WebSocket managers:
- Main WebSocket Manager: All UI clients
- Filtered WebSocket Manager: Trinity Connect clients (server-side agent filtering)

### Use Case: Event-Driven Coordination

External Claude Code instances can listen for schedule completion events to coordinate work:

```bash
# Wait for my-agent's scheduled task to complete
./trinity-listen.sh my-agent completed
# On event: Claude Code receives JSON, can fetch results, continue work
```

### Related Documentation

- **Trinity Connect**: [trinity-connect.md](trinity-connect.md) - Full feature flow for `/ws/events` endpoint
- **Scheduler Service**: [scheduler-service.md](scheduler-service.md) - Standalone scheduler implementation

---

## Per-Schedule Execution Configuration (Added 2026-02-20)

Each schedule can now specify custom timeout and tool restrictions for its execution.

### Configuration Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `timeout_seconds` | INTEGER | 900 | Execution timeout in seconds (5m-2h range) |
| `allowed_tools` | TEXT (JSON) | NULL | JSON array of tool names, NULL = all tools allowed |

### Timeout Options

The frontend provides preset timeout options via a dropdown (`SchedulesPanel.vue:100-111`):

| Option | Value (seconds) | Use Case |
|--------|-----------------|----------|
| 5 minutes | 300 | Quick status checks, simple queries |
| 15 minutes | 900 | Standard tasks (default) |
| 30 minutes | 1800 | Complex analysis, multi-step operations |
| 1 hour | 3600 | Large codebase operations, research tasks |
| 2 hours | 7200 | Long-running analysis, heavy processing |

### Allowed Tools

When `allowed_tools` is set, only the specified tools can be used during execution. The frontend provides a categorized multi-select UI (`SchedulesPanel.vue:114-152`).

**Tool Categories** (`SchedulesPanel.vue:611-647`):

| Category | Tools | Description |
|----------|-------|-------------|
| Files | Read, Write, Edit, NotebookEdit | File system operations |
| Search | Glob, Grep | File and content search |
| System | Bash | Command execution |
| Web | WebFetch, WebSearch | Internet access |
| Advanced | Task (Agents) | Sub-agent delegation |

**Common Configurations:**
- Read-only operations: `["Read", "Glob", "Grep"]`
- No shell access: `["Read", "Write", "Edit", "Glob", "Grep", "WebFetch"]`
- Research only: `["Read", "Glob", "Grep", "WebFetch", "WebSearch"]`
- Unrestricted (default): `null` (all tools allowed)

### Data Flow

```
Frontend                          Backend                           Scheduler                         Agent
--------                          -------                           ---------                         -----
SchedulesPanel.vue               routers/schedules.py              service.py                        agent-server.py

POST /api/agents/{name}/         create_schedule()
schedules                        |
{                                v
  name: "...",                   db/schedules.py:141-214
  timeout_seconds: 1800,         create_schedule()
  allowed_tools: ["Read",...]    - Validate cron
  model: "claude-opus-4-6"       - JSON.dumps(allowed_tools)
}                                - INSERT with timeout_seconds,
                                   allowed_tools, model columns
                                 |
                                 v
                                 201 Created

                                                                   (within 60s sync)
                                                                   _sync_schedules()
                                                                   Detects new schedule
                                                                   APScheduler.add_job()

                                                                   (cron fires)
                                                                   _execute_schedule_with_lock()
                                                                   |
                                                                   v
                                                                   db.create_execution(
                                                                     model_used=schedule.model)
                                                                   |
                                                                   v
                                                                   client.task(
                                                                     message,
                                                                     timeout=schedule.timeout_seconds,
                                                                     allowed_tools=schedule.allowed_tools,
                                                                     model=schedule.model
                                                                   )
                                                                   |
                                                                   v
                                                                   agent_client.py:101-153
                                                                   POST /api/task                ──> Receives:
                                                                   {                                 - message
                                                                     message,                        - timeout_seconds
                                                                     timeout_seconds,                - allowed_tools
                                                                     allowed_tools,                  - model
                                                                     model,
                                                                     execution_id                    Claude Code CLI:
                                                                   }                                 --max-turns
                                                                                                     --allowedTools
                                                                                                     --model
```

### Backend Models

**ScheduleCreate** (`src/backend/db_models.py:107-118`):
```python
class ScheduleCreate(BaseModel):
    name: str
    cron_expression: str
    message: str
    enabled: bool = True
    timezone: str = "UTC"
    description: Optional[str] = None
    timeout_seconds: int = 900  # Default 15 minutes
    allowed_tools: Optional[List[str]] = None  # None = all tools allowed
    model: Optional[str] = None  # Model override (MODEL-001). None = agent default
```

**Schedule** (`src/backend/db_models.py:120-138`):
```python
class Schedule(BaseModel):
    # ... existing fields ...
    timeout_seconds: int = 900
    allowed_tools: Optional[List[str]] = None
    model: Optional[str] = None  # Model override (MODEL-001). None = agent default
```

**ScheduleUpdateRequest** (`src/backend/models.py:1574` — centralized per Invariant #14, #654):
```python
class ScheduleUpdateRequest(BaseModel):
    # ... existing fields ...
    timeout_seconds: Optional[int] = None
    allowed_tools: Optional[List[str]] = None
    model: Optional[str] = None  # Model override (MODEL-001)
```

### Scheduler Models

**Schedule dataclass** (`src/scheduler/models.py:29-48`):
```python
@dataclass
class Schedule:
    # ... existing fields ...
    timeout_seconds: int = 900
    allowed_tools: Optional[List[str]] = None
    model: Optional[str] = None  # Model override (MODEL-001). None = agent default
```

### Database Operations

**JSON Serialization** (`src/backend/db/schedules.py:165-168`, `289-291`):
```python
# On create/update:
allowed_tools_json = None
if schedule_data.allowed_tools is not None:
    allowed_tools_json = json.dumps(schedule_data.allowed_tools)

# On read (_row_to_schedule):
allowed_tools = None
if "allowed_tools" in row_keys and row["allowed_tools"]:
    allowed_tools = json.loads(row["allowed_tools"])
```

### Agent Client

**task() method** (`src/scheduler/agent_client.py:101-153`):
```python
async def task(
    self,
    message: str,
    timeout: float = None,
    execution_id: Optional[str] = None,
    allowed_tools: Optional[list] = None,
    model: Optional[str] = None          # MODEL-001
) -> AgentTaskResponse:
    timeout = timeout or self.timeout

    payload = {"message": message, "timeout_seconds": int(timeout)}
    if execution_id:
        payload["execution_id"] = execution_id
    if allowed_tools is not None:
        payload["allowed_tools"] = allowed_tools
    if model:
        payload["model"] = model           # MODEL-001

    response = await self._request(
        "POST", "/api/task",
        json=payload,
        timeout=timeout + 10  # Add buffer to agent timeout
    )
```

### Scheduler Service

**Execution with config** (`src/scheduler/service.py:591-598`):
```python
# Send task to agent with schedule-specific timeout, allowed_tools, and model
client = get_agent_client(schedule.agent_name)
task_response = await client.task(
    schedule.message,
    timeout=schedule.timeout_seconds,
    execution_id=execution.id,
    allowed_tools=schedule.allowed_tools,
    model=schedule.model                    # MODEL-001
)
```

### Frontend UI

**Timeout Dropdown** (`SchedulesPanel.vue:99-112`):
```html
<div>
  <label>Timeout</label>
  <select v-model="formData.timeout_seconds">
    <option :value="300">5 minutes</option>
    <option :value="900">15 minutes (default)</option>
    <option :value="1800">30 minutes</option>
    <option :value="3600">1 hour</option>
    <option :value="7200">2 hours</option>
  </select>
</div>
```

**Allowed Tools Multi-Select** (`SchedulesPanel.vue:114-152`):
- Toggle button: "All Tools (Unrestricted)" vs "Enable All"
- Categorized tool checkboxes (Files, Search, System, Web, Advanced)
- Visual indication: selected tools highlighted in indigo

**Schedule Card Display** (`SchedulesPanel.vue:244-255`):
```html
<!-- Shows timeout in schedule card -->
<span>{{ formatTimeout(schedule.timeout_seconds) }}</span>

<!-- Shows tool count if restricted -->
<span v-if="schedule.allowed_tools">
  {{ schedule.allowed_tools.length }} tools
</span>
```

### Files Changed

| File | Line Numbers | Changes |
|------|--------------|---------|
| `src/backend/database.py` | Migration | Added `timeout_seconds`, `allowed_tools` columns |
| `src/backend/db_models.py` | 106-135 | `ScheduleCreate`, `Schedule` model updates |
| `src/backend/routers/schedules.py` | 76-86, 88-103 | `ScheduleUpdateRequest`, `ScheduleResponse` |
| `src/backend/db/schedules.py` | 66-91, 165-168, 283, 289-291 | JSON serialization, row mapping |
| `src/scheduler/models.py` | 45-46 | `Schedule` dataclass fields |
| `src/scheduler/database.py` | Row mapping | Parse `timeout_seconds`, `allowed_tools` |
| `src/scheduler/agent_client.py` | 101-149 | Accept/forward `allowed_tools` param |
| `src/scheduler/service.py` | 473-480 | Pass config to `client.task()` |
| `src/frontend/src/components/SchedulesPanel.vue` | 99-152, 244-255, 599-672, 875-880 | UI components, helpers |

### Security Considerations

1. **Tool Restrictions**: Allows limiting agent capabilities per-schedule (e.g., read-only schedules)
2. **Timeout Limits**: Prevents runaway executions (max 2 hours)
3. **JSON Validation**: Backend validates `allowed_tools` is a list before serialization
4. **Null Semantics**: `null` means unrestricted (all tools), `[]` means no tools allowed

---

## Related Flows

- **Integrates With**:
  - Execution Queue (`execution-queue.md`) - All scheduled executions go through queue (Added 2025-12-06)
  - Activity Stream (`activity-stream.md`) - Schedule activities tracked persistently
  - **Dashboard Timeline** (`dashboard-timeline-view.md`, `replay-timeline.md`) - Schedule markers show `next_run_at` on timeline (TSM-001, Added 2026-01-29)
  - **Trinity Connect** (`trinity-connect.md`) - Filtered event broadcast for external listeners (Added 2026-02-05)
- **Upstream**:
  - Tasks Tab (`tasks-tab.md`) - "Make Repeatable" button emits `create-schedule` event to pre-fill schedule creation form (Added 2026-01-12)
- **Related**:
  - Model Selection (`model-selection.md`) - Per-schedule model override (MODEL-001, Added 2026-03-02)
  - ~~Agent Chat~~ (`agent-chat.md` - DEPRECATED) - Chat API still uses queue; user now uses Terminal ([agent-terminal.md](agent-terminal.md))
  - MCP Orchestration (`mcp-orchestration.md`) - All three sources share the queue
