# Feature Flow: System Agent UI

> **Requirement 11.3** - Dedicated System Agent management interface

## Overview

The System Agent UI (`/system-agent`) is an admin-only dashboard for managing the Trinity platform's internal operations manager (`trinity-system`). It provides fleet oversight, quick actions for common operations, and a direct chat interface for executing ops commands.

## User Story

As a **platform administrator**, I want a dedicated interface for the System Agent so that I can monitor fleet health, execute emergency operations, and manage schedules without navigating to the regular agent detail page.

## Entry Points

| Type | Location | Description |
|------|----------|-------------|
| **NavBar Link** | `src/frontend/src/components/NavBar.vue:46-56` | "System" link with CPU icon (admin-only) |
| **Route** | `src/frontend/src/router/index.js:54-58` | `/system-agent` route definition |
| **View** | `src/frontend/src/views/SystemAgent.vue` | Main page component |

## Frontend Layer

### Route Configuration

```javascript
// src/frontend/src/router/index.js:54-58
{
  path: '/system-agent',
  name: 'SystemAgent',
  component: () => import('../views/SystemAgent.vue'),
  meta: { requiresAuth: true, requiresAdmin: true }
}
```

### NavBar Component

```html
<!-- src/frontend/src/components/NavBar.vue:46-56 -->
<router-link
  v-if="isAdmin"
  to="/system-agent"
  class="..."
>
  <svg class="w-4 h-4 mr-1 text-gray-400" ...>
    <!-- CPU icon -->
  </svg>
  System
</router-link>
```

Admin check: `NavBar.vue:197` fetches user role from `/api/users/me` and sets `isAdmin` computed property.

### SystemAgent.vue Component Structure

| Section | Lines | Description |
|---------|-------|-------------|
| Compact Header | 30-75 | Agent info, SYSTEM badge, status, Start/Restart button (single row) |
| Fleet Stats Bar | 77-112 | Inline horizontal: Fleet count, running, stopped, issues |
| OTel Metrics Grid | 114-238 | 6 metric cards with icons and progress bars |
| Quick Actions | 241-297 | Emergency Stop, Restart All, Pause/Resume Schedules |
| Operations Console | 300-406 | Chat interface with quick command buttons (/ops/costs added) |
| Notification Toast | 409-421 | Success/error feedback |

### State Management

The component uses local reactive state (no Pinia store):

```javascript
// src/frontend/src/views/SystemAgent.vue:443-502
const loading = ref(true)
const systemAgent = ref(null)
const fleetStatus = ref({ total: 0, running: 0, stopped: 0, issues: 0 })
const otelMetrics = ref({
  enabled: false,
  available: false,
  error: null,
  hasData: false,
  metrics: { cost_by_model: {}, tokens_by_model: {}, lines_of_code: {} },
  totals: { total_cost: 0, total_tokens: 0, tokens_by_type: {} }
})
const messages = ref([])
const inputMessage = ref('')
const isTyping = ref(false)
```

### API Calls

| Function | Endpoint | Purpose |
|----------|----------|---------|
| `loadSystemAgent()` | `GET /api/system-agent/status` | Fetch system agent status |
| `refreshFleetStatus()` | `GET /api/ops/fleet/status` | Fetch all agents for overview cards |
| `refreshOtelMetrics()` | `GET /api/observability/metrics` | Fetch OTel cost/token metrics |
| `startSystemAgent()` | `POST /api/system-agent/restart` | Start stopped system agent |
| `restartSystemAgent()` | `POST /api/system-agent/restart` | Restart running system agent |
| `emergencyStop()` | `POST /api/ops/emergency-stop` | Stop all agents, pause schedules |
| `restartAllAgents()` | `POST /api/ops/fleet/restart` | Restart all non-system agents |
| `pauseAllSchedules()` | `POST /api/ops/schedules/pause` | Pause all enabled schedules |
| `resumeAllSchedules()` | `POST /api/ops/schedules/resume` | Resume all paused schedules |
| `sendMessage()` | `POST /api/agents/trinity-system/chat` | Send ops command to system agent |

### Polling

```javascript
// src/frontend/src/views/SystemAgent.vue:802-817
// Fleet status every 10 seconds
pollingInterval = setInterval(() => {
  loadSystemAgent()
  refreshFleetStatus()
}, 10000)

// OTel metrics every 30 seconds
otelPollingInterval = setInterval(() => {
  refreshOtelMetrics()
}, 30000)
```

### OTel Metrics Visualization

The OTel section displays 6 metric cards in a responsive grid:

| Metric | Icon Color | Visualization |
|--------|------------|---------------|
| Total Cost | Green | Progress bar (scaled to $10 max) |
| Tokens | Blue | Stacked bar (input/output/cache breakdown) |
| Sessions | Indigo | Count with "conversations" label |
| Active Time | Orange | Formatted duration (s/m/h) |
| Commits | Pink | Count with "created" label |
| Lines | Cyan | +added / -removed |

States handled:
- **OTel Disabled**: Gray dot, "Set OTEL_ENABLED=1" message
- **OTel Unavailable**: Yellow dot, error message from API
- **No Data Yet**: "Chat with agents to generate data" message
- **Has Data**: Full metrics grid displayed

## Backend Layer

### System Agent Router

**File**: `src/backend/routers/system_agent.py`

| Endpoint | Method | Line | Handler | Description |
|----------|--------|------|---------|-------------|
| `/api/system-agent/status` | GET | 42 | `get_system_agent_status()` | Get agent status and health |
| `/api/system-agent/reinitialize` | POST | 105 | `reinitialize_system_agent()` | Reset to clean state (admin) |
| `/api/system-agent/restart` | POST | 215 | `restart_system_agent()` | Simple stop/start (admin) |

### Fleet Operations Router

**File**: `src/backend/routers/ops.py`

| Endpoint | Method | Line | Handler | Description |
|----------|--------|------|---------|-------------|
| `/api/ops/fleet/status` | GET | 40 | `get_fleet_status()` | All agents with status/context |
| `/api/ops/fleet/health` | GET | 137 | `get_fleet_health()` | Health summary with issues |
| `/api/ops/fleet/restart` | POST | 260 | `restart_fleet()` | Restart all/filtered agents |
| `/api/ops/fleet/stop` | POST | 378 | `stop_fleet()` | Stop all/filtered agents |
| `/api/ops/schedules/pause` | POST | 488 | `pause_all_schedules()` | Pause all schedules |
| `/api/ops/schedules/resume` | POST | 538 | `resume_all_schedules()` | Resume all schedules |
| `/api/ops/emergency-stop` | POST | 598 | `emergency_stop()` | Halt all executions |

### Chat Endpoint

**File**: `src/backend/routers/chat.py:50`

```python
@router.post("/{name}/chat")
async def chat_with_agent(name: str, request: ChatMessageRequest, ...):
    """Proxy chat messages to agent's internal web server."""
```

The Operations Console sends messages to `POST /api/agents/trinity-system/chat`.

### System Agent Service

**File**: `src/backend/services/system_agent_service.py`

| Method | Line | Description |
|--------|------|-------------|
| `is_deployed()` | 40 | Check if container exists |
| `is_running()` | 45 | Check if container is running |
| `ensure_deployed()` | 58 | Main entry point on startup |
| `_create_system_agent()` | 120 | Create container from template |

### Auto-Deployment on Startup

**File**: `src/backend/main.py:168-175`

```python
# Auto-deploy system agent (Phase 11.1)
try:
    result = await system_agent_service.ensure_deployed()
    print(f"System agent: {result['action']} - {result['message']}")
except Exception as e:
    print(f"Error deploying system agent: {e}")
```

## Data Layer

### Constants

```python
# src/backend/db/agents.py:16
SYSTEM_AGENT_NAME = "trinity-system"
```

### Database Tables

| Table | Column | Purpose |
|-------|--------|---------|
| `agent_ownership` | `is_system` | Marks system agents (deletion-protected) |
| `mcp_api_keys` | `scope` | "system" scope bypasses permissions |

### Template Files

| File | Purpose |
|------|---------|
| `config/agent-templates/trinity-system/template.yaml` | Agent configuration |
| `config/agent-templates/trinity-system/CLAUDE.md` | Operations instructions |

## UI Layout

```
+-----------------------------------------------------------------------+
|  [CPU Icon]  System Agent                       [running] [Restart]   |
|              Platform Operations Manager          SYSTEM              |
+-----------------------------------------------------------------------+
|                                                                       |
|  +----------+  +----------+  +----------+  +----------+               |
|  |  Total   |  | Running  |  | Stopped  |  |  Issues  |               |
|  |    12    |  |     8    |  |     4    |  |     1    |               |
|  +----------+  +----------+  +----------+  +----------+               |
|                                                                       |
|  +------------------+  +------------------------------------------+   |
|  | Quick Actions    |  | Operations Console                       |   |
|  |                  |  |  [/ops/status] [/ops/health] [/ops/sch] |   |
|  | [Emergency Stop] |  |                                          |   |
|  | [Restart All]    |  |  User: /ops/status                       |   |
|  | [Pause Scheds]   |  |  Agent: Fleet status report...           |   |
|  | [Resume Scheds]  |  |                                          |   |
|  | [Refresh Status] |  |  [Enter command...           ] [Send]    |   |
|  +------------------+  +------------------------------------------+   |
+-----------------------------------------------------------------------+
```

## Data Flow

### 1. Page Load

```
User navigates to /system-agent
        |
        v
+------------------------+
| Route guard checks     |
| requiresAdmin: true    |
+------------------------+
        |
        v (admin)
+------------------------+
| SystemAgent.vue        |
| onMounted()            |
+------------------------+
        |
        +---> GET /api/system-agent/status
        |           |
        |           v
        |    systemAgent = { status, name, health }
        |
        +---> GET /api/ops/fleet/status
                    |
                    v
             fleetStatus = { total, running, stopped, issues }
```

### 2. Quick Action: Emergency Stop

```
User clicks [Emergency Stop]
        |
        v
+------------------------+
| confirm() dialog       |
+------------------------+
        |
        v (confirmed)
+------------------------+
| POST /api/ops/         |
| emergency-stop         |
+------------------------+
        |
        v
+------------------------+
| Backend:               |
| 1. Pause all schedules |
| 2. Stop all agents     |
|    (except trinity-    |
|     system)            |
| 3. Audit log           |
+------------------------+
        |
        v
+------------------------+
| Response:              |
| { schedules_paused: N, |
|   agents_stopped: N }  |
+------------------------+
        |
        v
+------------------------+
| showNotification()     |
| refreshFleetStatus()   |
+------------------------+
```

### 3. Operations Console Chat

```
User enters "/ops/status" and clicks Send
        |
        v
+------------------------+
| messages.push({        |
|   role: 'user',        |
|   content: message     |
| })                     |
+------------------------+
        |
        v
+------------------------+
| POST /api/agents/      |
| trinity-system/chat    |
| { message: "/ops/..." }|
+------------------------+
        |
        v
+------------------------+
| chat.py:               |
| - Execution queue      |
| - Proxy to agent       |
| - Stream response      |
+------------------------+
        |
        v
+------------------------+
| messages.push({        |
|   role: 'assistant',   |
|   content: response    |
| })                     |
+------------------------+
```

## Side Effects

| Action | Side Effect | Location |
|--------|-------------|----------|
| Status check | Audit log: `system_agent.status` | `system_agent.py:93` |
| Reinitialize | Audit log: `system_agent.reinitialize` | `system_agent.py:176` |
| Restart | Audit log: `system_agent.restart` | `system_agent.py:253` |
| Emergency stop | Audit log: `ops.emergency_stop` (warning severity) | `ops.py:648` |
| Fleet restart | Audit log: `ops.fleet_restart` | `ops.py:353` |
| Pause schedules | Audit log: `ops.schedules_pause` | `ops.py:521` |
| Resume schedules | Audit log: `ops.schedules_resume` | `ops.py:581` |

## Error Handling

| Error Case | HTTP Status | UI Handling |
|------------|-------------|-------------|
| System agent not found | - | Shows "Not Found" state with warning |
| Agent not running | 503 | Disables chat input, shows message |
| Not admin | 403 | Route protected, NavBar hides link |
| Emergency stop failure | 500 | Shows error notification |
| Chat error | 4xx/5xx | Shows error in chat as assistant message |

## Security Considerations

1. **Admin-Only Access**: Route has `requiresAdmin: true` meta
2. **NavBar Protection**: Link only visible when `isAdmin` is true
3. **Backend Validation**: All `/api/ops/*` endpoints call `require_admin()`
4. **Audit Logging**: All operations logged with user ID and IP
5. **Deletion Protection**: System agent cannot be deleted via API

## Testing

### Prerequisites

1. Backend running (`docker-compose up -d backend`)
2. System agent auto-deployed (check backend logs)
3. Admin user logged in

### Test Steps

| # | Action | Expected | Verify |
|---|--------|----------|--------|
| 1 | Login as admin | Dashboard loads | See "System" in navbar |
| 2 | Login as non-admin | Dashboard loads | NO "System" in navbar |
| 3 | Click "System" link | Navigate to /system-agent | Page loads, shows agent status |
| 4 | View fleet cards | Shows Total/Running/Stopped/Issues | Numbers match agent list |
| 5 | Click "Refresh Status" | Cards update | Spinning icon during refresh |
| 6 | Click /ops/status button | Message appears in chat | Response shows fleet status |
| 7 | Click /ops/health button | Message appears in chat | Response shows health report |
| 8 | Type "hello" and send | Response in chat | System agent responds |
| 9 | Click "Restart All" | Confirm dialog | All non-system agents restart |
| 10 | Click "Emergency Stop" | Confirm dialog | All agents stop, schedules pause |
| 11 | Click "Pause Schedules" | Notification | Schedules paused count shown |
| 12 | Click "Resume Schedules" | Notification | Schedules resumed count shown |
| 13 | Stop system agent | UI updates | Start button appears instead of Restart |
| 14 | Click "Start" | Agent starts | Status changes to "running" |

### Edge Cases

| Scenario | Expected Behavior |
|----------|-------------------|
| System agent not deployed | Yellow "Not Found" warning with instructions |
| System agent stopped | Chat input disabled, "Start" button visible |
| Empty fleet | Cards show 0, no issues |
| API error | Error notification toast |
| Admin check fails | Hidden navbar link, 403 if direct URL |

### Cleanup

1. Resume any paused schedules: Click "Resume Schedules"
2. Restart any stopped agents: Use /ops/restart-all in console
3. Clear chat: Click trash icon in Operations Console

### Status

**Current**: Working (tested 2025-12-20)

## Related Documents

- [Internal System Agent](internal-system-agent.md) - Backend/service layer details
- [Agent Lifecycle](agent-lifecycle.md) - Start/stop/restart operations
- [Agent Chat](agent-chat.md) - Chat proxy implementation
- [Execution Queue](execution-queue.md) - Request serialization

## Changelog

| Date | Change |
|------|--------|
| 2025-12-21 | Compact header redesign + OTel metrics visualization |
| 2025-12-20 | Initial document created |
