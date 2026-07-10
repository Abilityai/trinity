# Feature: Model Selection

## Overview

Model selection allows users to choose which Claude model to use across three contexts:
1. **Terminal**: Select model for interactive terminal sessions (CFG-005/CFG-006, original)
2. **Tasks**: Select model for manual task execution via the Tasks tab (MODEL-001)
3. **Schedules**: Configure a per-schedule model for automated cron executions (MODEL-001)

The `model_used` field is recorded on every execution for audit and cost tracking.

## User Stories

| ID | Story |
|----|-------|
| CFG-005 | As a user, I want to view the current model so that I know which LLM is being used |
| CFG-006 | As a user, I want to change the model so that I can use different LLM variants |
| MODEL-001 | As a user, I want to select which model to use when running a task so that I can control cost and capability |
| MODEL-001 | As a user, I want to set a model per schedule so that different schedules can use different models |

---

## Entry Points

### Terminal Model (original)
- **UI**: `src/frontend/src/views/AgentDetail.vue:107-108` - Model passed to TerminalPanelContent
- **API (GET)**: `GET /api/agents/{name}/model`
- **API (PUT)**: `PUT /api/agents/{name}/model`
- **WebSocket**: Model passed as query parameter when connecting to terminal

### Task Model (MODEL-001)
- **UI**: `src/frontend/src/components/TasksPanel.vue:86` - ModelSelector above task input
- **API**: `POST /api/agents/{name}/task` - `model` field in request body

### Schedule Model (MODEL-001)
- **UI**: `src/frontend/src/components/SchedulesPanel.vue:85` - ModelSelector in create/edit form
- **API (Create)**: `POST /api/agents/{name}/schedules` - `model` field in body
- **API (Update)**: `PUT /api/agents/{name}/schedules/{id}` - `model` field in body

---

## Shared Component: ModelSelector

**File**: `src/frontend/src/components/ModelSelector.vue` (189 lines)

A reusable combobox component that provides preset model options with free-text input support.

### Props

| Prop | Type | Default | Purpose |
|------|------|---------|---------|
| `modelValue` | String | `''` | v-model binding for selected model |
| `label` | String | `null` | Optional label above input |
| `placeholder` | String | `'Select or type a model...'` | Input placeholder |
| `compact` | Boolean | `false` | Smaller input styling for inline use |

### Preset Models

Current-generation models use undated aliases (resolve to the latest snapshot); legacy models use full snapshot-dated IDs. **Synced 2026-07-08 (#1521)** against the [Anthropic models overview](https://platform.claude.com/docs/en/about-claude/models/overview):

```javascript
const PRESET_MODELS = [
  // Claude 5 family (latest generation, native 1M context)
  { value: 'claude-fable-5', label: 'Claude Fable 5', note: 'Most capable — longest tasks (latest)' },
  { value: 'claude-sonnet-5', label: 'Claude Sonnet 5', note: 'Fast + smart, 1M context (latest)' },
  // Current generation
  { value: 'claude-opus-4-8', label: 'Claude Opus 4.8', note: 'Most capable Opus' },
  { value: 'claude-opus-4-7', label: 'Claude Opus 4.7', note: 'Current' },
  { value: 'claude-opus-4-6', label: 'Claude Opus 4.6', note: 'Current' },
  { value: 'claude-sonnet-4-6', label: 'Claude Sonnet 4.6', note: 'Fast + smart' },
  { value: 'claude-haiku-4-5-20251001', label: 'Claude Haiku 4.5', note: 'Fastest, cheapest' },
  // Legacy (still active)
  { value: 'claude-opus-4-5-20251101', label: 'Claude Opus 4.5', note: 'Legacy' },
  { value: 'claude-sonnet-4-5-20250929', label: 'Claude Sonnet 4.5', note: 'Legacy' },
]
```

**Context-window denominator (#1521).** The `% context used` gauge is model-specific, not a flat 200K. The runtime-reported `modelUsage.contextWindow` is the primary value; the fallback is `services/model_context.py::resolve_context_window` (`[1m]`→1M, Gemini→1M, Codex→272K, bare Claude→200K *safe floor*, unknown→200K + warning), vendored byte-identically into the agent server (Invariant #5). See the Agent Runtimes → Context-window catalog note in `architecture.md`.

> **Removed models retire safely (#1080).** `claude-opus-4-20250514` and `claude-sonnet-4-20250514` retire **2026-06-15** and are removed from the presets. Removing a model from the picker only removes the *suggestion* — the field is free-text, so a schedule/session pinned to a removed ID stays valid as free-text until Anthropic retires it on the API. After that the next execution fails with a clear error in `schedule_executions.error` (surfaced in the execution history), not silently. To recover, re-point the schedule's Model field (or the session/task model) to a current model. No default resolves to a removed model — the platform default (`claude-sonnet-4-6`), agent-base default (`claude-sonnet-4-6`), and the summarization model (`claude-haiku-4-5-20251001`) are all current.

### Dropdown Filtering with `isTyping` Flag (lines 98, 107-114, 116-121)

The dropdown filters presets **only when the user is actively typing** (free-text input), not when opening via the chevron button. This is controlled by the `isTyping` ref:

```javascript
const isTyping = ref(false)

const filteredModels = computed(() => {
  if (!isTyping.value || !props.modelValue) return PRESET_MODELS
  const query = props.modelValue.toLowerCase()
  const filtered = PRESET_MODELS.filter(m =>
    m.value.toLowerCase().includes(query) || m.label.toLowerCase().includes(query)
  )
  return filtered.length > 0 ? filtered : PRESET_MODELS
})

function onInput(event) {
  isTyping.value = true   // only set when user types
  emit('update:modelValue', event.target.value)
  showDropdown.value = true
  highlightedIndex.value = -1
}
```

`isTyping` is reset to `false` on: `selectModel()` (line 141), `handleClickOutside()` (line 175), and `Escape` keydown (line 11).

### Features
- Dropdown with preset models, filtered by text input only when typing
- Chevron button opens full unfiltered dropdown
- Keyboard navigation (Up/Down/Enter/Escape)
- Free-text input for any model string (not limited to presets)
- Click-outside to close dropdown and reset typing state
- Checkmark indicator on currently selected model

---

## Flow 1: Task Model Selection (MODEL-001)

### Frontend

**File**: `src/frontend/src/components/TasksPanel.vue`

| Element | Line | Purpose |
|---------|------|---------|
| ModelSelector component | 86 | Compact combobox above task textarea |
| Import | 494 | `import ModelSelector from './ModelSelector.vue'` |
| State | 538-542 | `selectedModel` ref with localStorage persistence |
| Payload | 744-745 | Adds `model` to POST body if set |
| Display | 205 | Shows `model_used` badge in task history rows |

**localStorage Persistence** (lines 538-542):
```javascript
const taskModelKey = computed(() => `trinity-task-model-${props.agentName}`)
const selectedModel = ref(localStorage.getItem(`trinity-task-model-${props.agentName}`) || 'claude-sonnet-4-6')
watch(selectedModel, (val) => {
  localStorage.setItem(taskModelKey.value, val)
})
```

**Task Submission** (lines 743-748):
```javascript
const payload = { message: taskMessage }
if (selectedModel.value) {
  payload.model = selectedModel.value
}
const response = await axios.post(`/api/agents/${props.agentName}/task`, payload, {
  headers: authStore.authHeader
})
```

### Backend

**File**: `src/backend/routers/chat.py`

1. `POST /api/agents/{name}/task` (line 527+) receives `model` in `ParallelTaskRequest`
2. `model` is forwarded to agent container in payload (line 260, 700):
   ```python
   if request.model:
       payload["model"] = request.model
   ```
3. Execution record stores `model_used=request.model` (line 593):
   ```python
   execution = db.create_task_execution(
       agent_name=name,
       message=request.message,
       model_used=request.model
   )
   ```

### Agent Server

**File**: `docker/base-image/agent_server/routers/chat.py:83-119`

The `/api/task` endpoint receives `model` in `ParallelTaskRequest` and passes it to `execute_headless_task()`:
```python
response_text, execution_log, metadata, session_id = await execute_headless_task(
    prompt=request.message,
    model=request.model,
    ...
)
```

**File**: `docker/base-image/agent_server/services/claude_code.py:434-436`
```python
if agent_state.current_model:
    cmd.extend(["--model", agent_state.current_model])
```

### Data Flow

```
TasksPanel.vue                Backend chat.py               Agent Container
--------------                ---------------               ---------------
selectedModel = "opus"
  |
  v
POST /api/agents/{name}/task  execute_parallel_task()
{ message: "...",             |
  model: "opus" }             v
                              db.create_task_execution(      POST /api/task
                                model_used="opus")           { message: "...",
                              |                                model: "opus" }
                              v                              |
                              Forward to agent container      v
                                                             execute_headless_task(
                                                               model="opus")
                                                             |
                                                             v
                                                             claude --model opus -p "..."
```

---

## Flow 2: Schedule Model Selection (MODEL-001)

### Frontend

**File**: `src/frontend/src/components/SchedulesPanel.vue`

| Element | Line | Purpose |
|---------|------|---------|
| ModelSelector in form | 85 | `<ModelSelector v-model="formData.model" />` |
| Import | 563 | `import ModelSelector from './ModelSelector.vue'` |
| formData default | 625 | `model: 'claude-opus-4-6'` |
| Reset form | 751 | `model: 'claude-opus-4-6'` |
| Edit form | 767 | `model: schedule.model \|\| 'claude-opus-4-6'` |
| Badge in list | 261-265 | Shows model icon+name if `schedule.model` is set |
| Execution detail | 391 | Shows `exec.model_used` in execution history |
| Execution modal | 480 | Shows `selectedExecution.model_used \|\| 'default'` |

**Form Data** (line 616-626):
```javascript
const formData = ref({
  name: '',
  cron_expression: '',
  message: '',
  description: '',
  timezone: 'UTC',
  enabled: true,
  timeout_seconds: 900,
  allowed_tools: null,
  model: 'claude-opus-4-6'  // MODEL-001
})
```

**Schedule List Badge** (lines 261-265):
```html
<span v-if="schedule.model" class="flex items-center" :title="`Model: ${schedule.model}`">
  <svg ...><!-- computer icon --></svg>
  {{ schedule.model }}
</span>
```

### Backend

**File**: `src/backend/routers/schedules.py`

| Model | Line | `model` field |
|-------|------|---------------|
| `ScheduleUpdateRequest` | 86 | `model: Optional[str] = None` |
| `ScheduleResponse` | 105 | `model: Optional[str] = None` |
| `ExecutionSummary` | 139 | `model_used: Optional[str] = None` |
| `ExecutionResponse` | 182 | `model_used: Optional[str] = None` |

**File**: `src/backend/db_models.py`

| Model | Line | Field |
|-------|------|-------|
| `ScheduleCreate` | 117 | `model: Optional[str] = None` |
| `Schedule` | 137 | `model: Optional[str] = None` |
| `ScheduleExecution` | 168 | `model_used: Optional[str] = None` |

### Database

**File**: `src/backend/db/schema.py:146` - `agent_schedules.model TEXT`
**File**: `src/backend/db/schema.py:169` - `schedule_executions.model_used TEXT`

**Migration #22** (`src/backend/db/migrations.py:575-597`):
```python
def _migrate_schedule_model_selection(cursor, conn):
    # Add model to agent_schedules
    cursor.execute("ALTER TABLE agent_schedules ADD COLUMN model TEXT")
    # Add model_used to schedule_executions
    cursor.execute("ALTER TABLE schedule_executions ADD COLUMN model_used TEXT")
```

### CRUD Operations

**File**: `src/backend/db/schedules.py`

| Operation | Lines | Model handling |
|-----------|-------|----------------|
| `_row_to_schedule()` | 91 | `model=row["model"] if "model" in row_keys else None` |
| `_row_to_schedule_execution()` | 125 | `model_used=row["model_used"] if "model_used" in row_keys else None` |
| `create_schedule()` | 188, 205, 224 | Writes `schedule_data.model` to INSERT and returns it |
| `update_schedule()` | 296 | `"model"` in `allowed_fields` for dynamic SET |
| `create_task_execution()` | 437, 471, 486, 503 | Accepts and writes `model_used` parameter |
| `create_schedule_execution()` | 506, 534, 549, 566 | Accepts and writes `model_used` parameter |
| `get_agent_executions_summary()` | 669 | Includes `model_used` in SELECT columns |

### DatabaseManager Proxy

**File**: `src/backend/database.py:486-507`

`DatabaseManager.create_task_execution()` is a facade method that delegates to `ScheduleOps.create_task_execution()`. The proxy accepts `model_used: str = None` (line 496) and forwards it to the underlying implementation:

```python
def create_task_execution(
    self,
    agent_name: str,
    message: str,
    triggered_by: str = "manual",
    source_user_id: int = None,
    source_user_email: str = None,
    source_agent_name: str = None,
    source_mcp_key_id: str = None,
    source_mcp_key_name: str = None,
    model_used: str = None,           # MODEL-001 fix: was missing, added
):
    return self._schedule_ops.create_task_execution(
        agent_name, message, triggered_by,
        ...,
        model_used=model_used,        # Now forwarded correctly
    )
```

**Bug fixed**: Previously the proxy signature lacked `model_used`, so callers passing `model_used=request.model` would crash with `TypeError: got an unexpected keyword argument 'model_used'` on every task submission that specified a model.

### Scheduler Service

**File**: `src/scheduler/models.py:48`
```python
@dataclass
class Schedule:
    # ... existing fields ...
    model: Optional[str] = None  # Model override (MODEL-001). None = agent default
```

**File**: `src/scheduler/database.py:78`
```python
# _row_to_schedule()
model=row["model"] if "model" in row_keys else None
```

**File**: `src/scheduler/database.py:196-223`
```python
def create_execution(self, ..., model_used: str = None):
    # INSERT INTO schedule_executions (..., model_used) VALUES (?, ..., ?)
```

**File**: `src/scheduler/agent_client.py:107, 138-139`
```python
async def task(self, message, ..., model: Optional[str] = None):
    if model:
        payload["model"] = model
```

**File**: `src/scheduler/service.py:564, 591-598`
```python
# Create execution record with model
execution = self.db.create_execution(
    ...,
    model_used=schedule.model
)

# Send task to agent with model
task_response = await client.task(
    schedule.message,
    timeout=schedule.timeout_seconds,
    execution_id=execution.id,
    allowed_tools=schedule.allowed_tools,
    model=schedule.model
)
```

### Schedule Execution Data Flow

```
SchedulesPanel.vue           Backend                      Scheduler Service           Agent
------------------           -------                      -----------------           -----
POST /schedules              schedules.py                 (sync within 60s)
{ name: "...",               create_schedule()            _sync_schedules()
  model: "opus" }            INSERT model="opus"          Detects new schedule
                                                          |
                                                          v (cron fires)
                                                          _execute_schedule_with_lock()
                                                          |
                                                          v
                                                          db.create_execution(         POST /api/task
                                                            model_used="opus")         { message: "...",
                                                          |                              model: "opus" }
                                                          v                            |
                                                          client.task(                 v
                                                            model="opus")              claude --model opus
```

---

## Flow 3: Terminal Model Selection (original CFG-005/CFG-006)

This is the original model selection flow for the interactive terminal. See the detailed documentation below for completeness; this flow is unchanged by MODEL-001.

### Components

| Component | File | Lines | Purpose |
|-----------|------|-------|---------|
| AgentDetail | `src/frontend/src/views/AgentDetail.vue` | 107-108 | Passes `currentModel` to TerminalPanelContent |
| TerminalPanelContent | `src/frontend/src/components/TerminalPanelContent.vue` | 33, 119-122 | Forwards model prop to AgentTerminal |
| AgentTerminal | `src/frontend/src/components/AgentTerminal.vue` | 123-127, 331-332 | Includes model in WebSocket connection URL |

### Composable

**File**: `src/frontend/src/composables/useAgentSettings.js`

| Method | Lines | Purpose |
|--------|-------|---------|
| `currentModel` | 13 | Reactive ref storing selected model |
| `loadModelInfo()` | 52-60 | Fetches current model from agent |
| `changeModel()` | 62-76 | Updates model via API |

### Store Actions

**File**: `src/frontend/src/stores/agents.js`

| Action | Lines | Purpose |
|--------|-------|---------|
| `getAgentModel(name)` | 292-298 | `GET /api/agents/{name}/model` |
| `setAgentModel(name, model)` | 300-307 | `PUT /api/agents/{name}/model` |

### Backend Endpoints

**File**: `src/backend/routers/chat.py`

| Endpoint | Lines | Purpose |
|----------|-------|---------|
| `GET /api/agents/{name}/model` | 1158-1187 | Proxy to agent-server `GET /api/model` |
| `PUT /api/agents/{name}/model` | 1191-1223 | Proxy to agent-server `PUT /api/model` |

### Agent Server Model State

**File**: `docker/base-image/agent_server/state.py:36`
```python
self.current_model: Optional[str] = os.getenv("AGENT_RUNTIME_MODEL", None) or os.getenv("CLAUDE_MODEL", None)
```

Note: `current_model` persists across session resets.

---

## Database Schema Changes (MODEL-001)

### agent_schedules - new column

```sql
ALTER TABLE agent_schedules ADD COLUMN model TEXT;
```

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `model` | TEXT | NULL | Model to use when schedule fires. NULL = agent default |

### schedule_executions - new column

```sql
ALTER TABLE schedule_executions ADD COLUMN model_used TEXT;
```

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `model_used` | TEXT | NULL | Model that was actually used for this execution |

---

## Available Models

### Claude Code Runtime

| Model ID | Label | Notes |
|-----------|-------|-------|
| `claude-fable-5` | Claude Fable 5 | Claude 5 family — most capable, longest tasks (latest), 1M context |
| `claude-sonnet-5` | Claude Sonnet 5 | Claude 5 family — fast + smart (latest), 1M context |
| `claude-opus-4-8` | Claude Opus 4.8 | Most capable Opus, 1M context |
| `claude-opus-4-7` | Claude Opus 4.7 | Current, 1M context |
| `claude-opus-4-6` | Claude Opus 4.6 | Current |
| `claude-sonnet-4-6` | Claude Sonnet 4.6 | Fast + smart (current) |
| `claude-haiku-4-5-20251001` | Claude Haiku 4.5 | Fastest, cheapest (current) |
| `claude-opus-4-5-20251101` | Claude Opus 4.5 | Legacy (still active) |
| `claude-sonnet-4-5-20250929` | Claude Sonnet 4.5 | Legacy (still active) |

Current-generation models (`claude-opus-4-8`, `claude-opus-4-7`, `claude-opus-4-6`, `claude-sonnet-4-6`) use undated aliases that resolve to the latest snapshot. Legacy models use full snapshot-dated IDs (e.g., `claude-opus-4-5-20251101`) for deterministic version pinning. The two models that retired from the presets on 2026-06-15 (`claude-opus-4-20250514`, `claude-sonnet-4-20250514`) are no longer offered — see the graceful-degradation note under Preset Models above.

Also accepts model aliases (`sonnet`, `opus`, `haiku`) and 1M context variants (`sonnet[1m]`, etc.).

### Gemini Runtime

| Model | Notes |
|-------|-------|
| `gemini-2.5-pro` | Latest pro model, 1M context |
| `gemini-2.5-flash` | Default, fast and capable |
| `gemini-2.0-flash` | Previous generation |

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Agent not found | 404 | "Agent not found" |
| Agent not running | 400 | "Agent is not running" |
| Invalid Claude model | 400 | "Invalid Claude model: {model}. Use aliases (sonnet, opus, haiku) or full model names." |
| Invalid Gemini model | 400 | "Invalid Gemini model: {model}. Use: gemini-2.5-pro, gemini-2.5-flash, etc." |
| Agent server unreachable | 503 | "Failed to get/set model" |

### Known Issues (Resolved)

| Issue | Root Cause | Fix |
|-------|-----------|-----|
| Task submission crashes with `TypeError: got an unexpected keyword argument 'model_used'` | `DatabaseManager.create_task_execution()` proxy in `database.py` was missing `model_used` parameter, while the underlying `ScheduleOps.create_task_execution()` in `db/schedules.py` already had it | Added `model_used: str = None` to proxy signature and forwarded it (line 496, 506) |
| Dropdown shows filtered results when opening via chevron button | `filteredModels` computed was always filtering based on input value, even when user clicked the chevron | Added `isTyping` ref; filtering only applies when `isTyping.value` is true (set by `onInput`, cleared by `selectModel`/`handleClickOutside`/Escape) |
| Preset model IDs did not match Anthropic API (e.g., `claude-opus-4-5` vs `claude-opus-4-5-20251101`) | Short IDs used for previous-gen models are not valid API identifiers | Updated to full snapshot-dated IDs for all models except current-gen (`claude-opus-4-6`, `claude-sonnet-4-6`) |

---

## Testing

### Prerequisites
- Agent must be running
- User must have access to the agent

### Test Steps

1. **Task Model - Select and Run**
   - Navigate to agent Tasks tab
   - Select "Claude Sonnet 4.6" from ModelSelector dropdown
   - Enter task message and click Run
   - **Expected**: Task executes with selected model
   - **Verify**: `model_used` shows "claude-sonnet-4-6" in task history row

2. **Task Model - localStorage Persistence**
   - Select a model in Tasks tab
   - Navigate away and return
   - **Expected**: Previously selected model is restored from localStorage

3. **Schedule - Create with Model**
   - Click New Schedule, fill form
   - Select "Claude Opus 4.6" in Model field
   - Click Create
   - **Expected**: Schedule card shows model badge "claude-opus-4-6"
   - **Verify**: `GET /api/agents/{name}/schedules` returns `model: "claude-opus-4-6"`

4. **Schedule - Edit Model**
   - Edit existing schedule
   - Change model to different value
   - Save
   - **Expected**: Schedule card badge updates
   - **Verify**: Next execution uses new model

5. **Schedule Execution - model_used Recorded**
   - Trigger schedule manually
   - View execution in history
   - **Expected**: `model_used` field is populated in execution detail

6. **Execution Summary - model_used in List**
   - View Tasks tab with prior executions
   - **Expected**: `model_used` badge visible on each task row

7. **Terminal Model (original)**
   - `PUT /api/agents/{name}/model` with `{"model": "opus"}`
   - **Expected**: 200 response with success status
   - **Verify**: Terminal reconnects with new model

### Edge Cases
- Free-text model name in ModelSelector (not a preset) - should pass through
- NULL model on schedule (uses agent default)
- Model field in execution summary for lightweight list endpoint

---

## Related Flows

- **Terminal Model**: [agent-terminal.md](agent-terminal.md) - Terminal uses model for CLI commands
- **Task Execution**: [tasks-tab.md](tasks-tab.md) - Tasks tab UI with model selection
- **Scheduling**: [scheduling.md](scheduling.md) - Schedule CRUD with model field
- **Scheduler Service**: [scheduler-service.md](scheduler-service.md) - Passes model to agent client
- **Parallel Execution**: [parallel-headless-execution.md](parallel-headless-execution.md) - `/api/task` model parameter
- **Agent Lifecycle**: [agent-lifecycle.md](agent-lifecycle.md) - Model persists while agent runs

---

## Revision History

| Date | Change |
|------|--------|
| 2026-06-06 | **#1080 — model list refresh**: Added **Claude Opus 4.8** (`claude-opus-4-8`) as the flagship/latest across `ModelSelector.vue` and the admin platform-default dropdown (`Settings.vue`). Removed the two models retiring 2026-06-15 (`claude-opus-4-20250514`, `claude-sonnet-4-20250514`) from the presets; re-tiered current vs. legacy. Bumped the `TasksPanel.vue` localStorage fallback off legacy `claude-opus-4-5-20251101` → current `claude-sonnet-4-6`. Refreshed MCP `model` param examples (`chat.ts`, `schedules.ts`, `loops.ts`) off the EOL example ID. Backend/agent-server defaults were already current (`claude-sonnet-4-6`, `claude-haiku-4-5-20251001`) — unchanged, no base-image rebuild. Documented graceful degradation: removed presets remain valid free-text until Anthropic's retirement date, then fail with a clear execution error rather than silently. |
| 2026-03-02 | **MODEL-001 bug fixes**: (1) ModelSelector `PRESET_MODELS` updated to correct Anthropic API IDs with snapshot dates (`claude-opus-4-5-20251101`, `claude-sonnet-4-5-20250929`, `claude-haiku-4-5-20251001`) plus legacy models. (2) Added `isTyping` flag so dropdown filtering only applies during free-text input, not when opening via chevron. (3) Fixed `DatabaseManager.create_task_execution()` proxy missing `model_used` parameter -- was crashing all task submissions with model selection. (4) Default model changed from `claude-opus-4-5` to `claude-opus-4-6` in TasksPanel.vue and SchedulesPanel.vue. |
| 2026-03-02 | **MODEL-001**: Added model selection for Tasks and Schedules. New ModelSelector.vue component, database migration #22 (`model` on agent_schedules, `model_used` on schedule_executions), full-stack integration through backend, scheduler service, and agent server. |
| 2026-01-13 | Initial documentation created (terminal model only: CFG-005/CFG-006) |
