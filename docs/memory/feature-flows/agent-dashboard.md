# Feature: Agent Dashboard

> **Last Updated**: 2026-03-15 - Added stale dashboard cache (shows last valid dashboard when YAML breaks) and Update Dashboard button.

## Overview

Agent-defined dashboard system that replaces the static Metrics tab. Agents define a `dashboard.yaml` file with declarative widget configuration, and Trinity renders it in the Dashboard tab. The Dashboard tab is **conditionally visible** - it only appears when the agent has a `dashboard.yaml` file or a cached valid dashboard. If the YAML breaks, the last valid dashboard is displayed with a warning banner instead of hiding the page.

## User Story

As an agent developer, I want to define a custom dashboard in YAML so that I can expose agent-specific metrics, status, and data without modifying Trinity platform code.

## Entry Points

- **UI**: `src/frontend/src/views/AgentDetail.vue:88-90` - Dashboard tab renders `DashboardPanel` component
- **Tab Visibility**: `src/frontend/src/views/AgentDetail.vue:442-444` - Dashboard tab only shown if `hasDashboard.value === true`
- **API**: `GET /api/agent-dashboard/{name}` - Fetches dashboard configuration from agent

## Dashboard Resilience - Stale Cache (2026-03-15)

When the dashboard YAML becomes invalid (parse error, validation error, HTTP failure, timeout), the backend serves the **last known valid dashboard** from an in-memory cache with a `stale` flag. This prevents the dashboard tab from disappearing when an agent's YAML is temporarily broken.

### Backend Cache (`services/agent_service/dashboard.py`)

```python
# Module-level in-memory cache
_last_valid_dashboard: Dict[str, dict] = {}
```

**Cache lifecycle:**
1. On every successful dashboard fetch (`has_dashboard: true`), the config is deep-copied into `_last_valid_dashboard[agent_name]`
2. On error (YAML parse, validation, HTTP error, timeout), check cache first
3. If cache hit: return cached config with `stale: true` and `stale_reason: "<error message>"`
4. If cache miss: return original error response (no cache available yet)

**Stale response fields added to normal response:**
- `stale: true` - indicates this is a cached version
- `stale_reason: string` - the actual error that triggered cache usage

History enrichment and platform metrics are still applied to stale dashboards.

### Frontend Warning Banner (`DashboardPanel.vue`)

When `dashboardData.stale` is true, a yellow warning banner is shown above the dashboard:
- "Showing cached dashboard" title
- The `stale_reason` error message as detail text

### Tab Visibility

`checkDashboardExists()` (`AgentDetail.vue`) considers both `has_dashboard === true` and `stale === true` as valid, keeping the Dashboard tab visible even when the YAML is broken.

```javascript
hasDashboard.value = response?.has_dashboard === true || response?.stale === true
```

## Update Dashboard Button (2026-03-15)

An "Update Dashboard" button appears in the dashboard header when the agent has an `update-dashboard` playbook (skill). Clicking it triggers the playbook execution on the agent.

### Frontend (`DashboardPanel.vue`)

1. **Playbook detection**: On mount/agent change, `checkUpdateDashboardPlaybook()` calls `GET /api/agents/{name}/playbooks` and checks for a skill named `update-dashboard`
2. **Button**: Shown next to the refresh button when `hasUpdateDashboardPlaybook` is true
3. **Trigger**: `triggerUpdateDashboard()` sends `POST /api/agents/{name}/task` with `{ message: "/update-dashboard" }`
4. **Feedback**: Button shows spinner during execution, auto-refreshes dashboard after 5 seconds

## Dashboard Tab Conditional Visibility (2026-02-12)

The Dashboard tab is **conditionally hidden** when an agent doesn't have a `dashboard.yaml` file (and no cached version). This provides a cleaner UI for agents that don't define custom dashboards.

## Architecture

```
+------------------+     +-------------------+     +----------------------+
| DashboardPanel   | --> | Backend Router    | --> | Agent Server         |
| (Vue Component)  |     | /api/agent-       |     | /api/dashboard       |
|                  |     | dashboard/{name}  |     |                      |
+------------------+     +-------------------+     +----------------------+
        |                        |                         |
        |                        | httpx                   |
        v                        v                         v
  agents.js store         dashboard.py service      dashboard.yaml file
  getAgentDashboard()     get_agent_dashboard_logic  ~/dashboard.yaml
```

## Frontend Layer

### Components

**AgentDetail.vue:88-89** - Tab integration
```html
<!-- Dashboard Tab Content -->
<div v-if="activeTab === 'dashboard'" class="p-6">
  <DashboardPanel :agent-name="agent.name" :agent-status="agent.status" />
</div>
```

**AgentDetail.vue:226, 417** - Component import and tab definition
```javascript
import DashboardPanel from '../components/DashboardPanel.vue'
// ...
{ id: 'dashboard', label: 'Dashboard' }
```

**DashboardPanel.vue** - Main dashboard renderer (509 lines)
- Line 4-6: Loading state with spinner
- Line 9-19: Agent not running state
- Line 22-32: Error state with error message display
- Line 35-48: No dashboard defined state (prompts user to create `~/dashboard.yaml`)
- Line 51-294: Dashboard display with sections and widgets
- Line 298-312: Props definition (`agentName`, `agentStatus`)
- Line 319-333: `loadDashboard()` - fetches data via store action
- Line 457-465: Auto-refresh based on `config.refresh` (default 30s)
- Line 475-495: Watchers for `agentName` and `agentStatus` changes

### Widget Renderers (DashboardPanel.vue)

| Widget Type | Lines | Description |
|-------------|-------|-------------|
| metric | 195-229 | Numeric value with trend indicator (up/down arrows) |
| status | 232-245 | Colored badge with label |
| progress | 248-285 | Progress bar (0-100%) with color |
| text | 288-299 | Simple text with size/color/align options |
| markdown | 302-308 | Rendered markdown content (uses marked.js) |
| table | 311-345 | Tabular data with columns and rows |
| list | 348-365 | Bullet or numbered list |
| link | 368-383 | Clickable link or button style |
| divider | 386-391 | Horizontal separator (spans full width) |
| spacer | 394-400 | Vertical space (sm/md/lg sizes) |
| image | 403-416 | Image display with optional caption |

The 11 types above are the closed set. The backend D-002 allowlist (`static_checks._WIDGET_TYPES`, a tuple in this same order), the agent-server gate `validate_widget.valid_types` (which strips any other widget before the frontend sees it and lists it in the Dashboard tab's warning banner) and this render chain are kept in lockstep by `tests/unit/test_2110_widget_type_parity.py`, together with the validation spec, the agent guide and the two user-docs pages. There has never been a chart, badge or countdown widget type — trend lines come from the platform's history enrichment (DASH-001) on `metric`/`progress` widgets keyed by a stable `id` (#2110).

### Helper Functions (DashboardPanel.vue)

| Function | Lines | Purpose |
|----------|-------|---------|
| `formatValue()` | 336-342 | Locale-aware number formatting |
| `formatRelativeTime()` | 345-354 | "2m ago" style timestamps |
| `renderMarkdown()` | 357-360 | Convert markdown to HTML |
| `getTrendColor()` | 363-367 | Green for up, red for down |
| `getStatusColors()` | 370-381 | Color map for status badges |
| `getProgressBarColor()` | 384-394 | Progress bar fill colors |
| `getImageUrl()` | 447-454 | Convert `/files/` paths to API URLs |

### State Management

**stores/agents.js:493-499** - Store action
```javascript
async getAgentDashboard(name) {
  const authStore = useAuthStore()
  const response = await axios.get(`/api/agent-dashboard/${name}`, {
    headers: authStore.authHeader
  })
  return response.data
}
```

### API Calls

```javascript
// From DashboardPanel.vue:322
const response = await agentsStore.getAgentDashboard(props.agentName)

// Translates to:
GET /api/agent-dashboard/{name}
```

## Backend Layer

### Router

**src/backend/routers/agent_dashboard.py** (44 lines)

```python
router = APIRouter(prefix="/api/agent-dashboard", tags=["agent-dashboard"])

@router.get("/{name}")
async def get_agent_dashboard(
    name: str,
    current_user: User = Depends(get_current_user)
):
    return await get_agent_dashboard_logic(name, current_user)
```

**src/backend/main.py:49, 265** - Router registration
```python
from routers.agent_dashboard import router as agent_dashboard_router
# ...
app.include_router(agent_dashboard_router)
```

### Service Layer

**src/backend/services/agent_service/dashboard.py** (107 lines)

**get_agent_dashboard_logic()** - Line 18-106
1. **Authorization** (Line 49-50): Check `db.can_user_access_agent()`
2. **Container lookup** (Line 52-54): Get Docker container by name
3. **Status check** (Line 59-67): Return early if agent not running
4. **HTTP call** (Line 71-87): Call agent's internal `/api/dashboard` endpoint
5. **Error handling** (Lines 88-106): Timeout, connection errors

```python
async def get_agent_dashboard_logic(agent_name: str, current_user: User) -> dict:
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # If not running, return basic info
    if container.status != "running":
        return {"agent_name": agent_name, "has_dashboard": False, ...}

    # Fetch from agent's internal API
    agent_url = f"http://agent-{agent_name}:8000/api/dashboard"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(agent_url)
        # ...
```

## Agent Layer

### Agent Server Router

**docker/base-image/agent_server/routers/dashboard.py** (238 lines)

**Router Registration** - Registered in `routers/__init__.py:11, 21`:
```python
from .dashboard import router as dashboard_router
```

**Main Endpoint** - Line 158-237:
```python
@router.get("/api/dashboard")
async def get_dashboard():
    dashboard_path = find_dashboard_yaml()
    if not dashboard_path:
        return {"has_dashboard": False, "error": "No dashboard.yaml found..."}

    content = dashboard_path.read_text()
    config = yaml.safe_load(content)
    errors = validate_dashboard(config)
    # ...
```

### File Location

**get_dashboard_path()** - Line 18-20
```python
def get_dashboard_path() -> Path:
    """Get the fixed path to dashboard.yaml."""
    return Path("/home/developer/dashboard.yaml")
```

> **Note**: The agent home directory is `/home/developer`. All agent files live there directly (no workspace subdirectory).

### Validation

**validate_dashboard()** - Line 130-155
- Validates required `title` field
- Validates required `sections` array
- Validates `refresh` >= 5 seconds (default 30)
- Calls `validate_section()` for each section

**validate_section()** - Line 99-127
- Validates required `widgets` array
- Validates `layout` is 'grid' or 'list' (default 'grid')
- Validates `columns` is 1-4 (default 3)
- Calls `validate_widget()` for each widget

**validate_widget()** - Line 31-96
- Validates required `type` field
- Validates type is one of 11 valid types
- Type-specific required field validation:
  - `metric`: label, value
  - `status`: label, value, color
  - `progress`: label, value
  - `text`: content
  - `markdown`: content
  - `table`: columns, rows
  - `list`: items
  - `link`: label, url
  - `image`: src, alt

## Dashboard YAML Schema

```yaml
title: "Agent Dashboard"      # Required
description: "Optional description"
refresh: 30                   # Auto-refresh interval (seconds, min 5)

sections:
  - title: "Metrics"          # Optional section title
    description: "Key metrics"
    layout: grid              # 'grid' (default) or 'list'
    columns: 3                # 1-4 columns for grid layout
    widgets:
      - type: metric
        label: "Total Users"
        value: 1234
        unit: "users"
        trend: up             # 'up', 'down', or omit
        trend_value: "+12%"
        description: "Optional detail"
        colspan: 2            # Span multiple columns

      - type: status
        label: "System Status"
        value: "Healthy"
        color: green          # green, red, yellow, gray, blue, orange, purple

      - type: progress
        label: "Disk Usage"
        value: 75
        color: yellow

      - type: text
        content: "Plain text content"
        size: md              # xs, sm, md, lg
        color: gray
        align: center         # left, center, right

      - type: markdown
        content: "**Bold** and *italic*"

      - type: table
        title: "Recent Events"
        columns:
          - { key: date, label: Date }
          - { key: event, label: Event }
        rows:
          - { date: "2024-01-01", event: "Started" }
        max_rows: 5

      - type: list
        title: "Tasks"
        items: ["Task 1", "Task 2"]
        style: bullet         # bullet, number, none
        max_items: 10

      - type: link
        label: "Documentation"
        url: "https://example.com"
        external: true        # Opens in new tab
        style: button         # 'button' or omit for text link
        color: blue

      - type: image
        src: "/files/chart.png"   # Will be converted to API path
        alt: "Chart"
        caption: "Weekly metrics"

      - type: divider         # Horizontal line

      - type: spacer
        size: lg              # sm (8px), md (16px), lg (32px)
```

## Response Format

### Success Response
```json
{
  "agent_name": "my-agent",
  "has_dashboard": true,
  "config": {
    "title": "Agent Dashboard",
    "description": "...",
    "refresh": 30,
    "sections": [...]
  },
  "last_modified": "2024-01-15T10:30:00",
  "status": "running",
  "error": null
}
```

### Stale/Cached Response (YAML broken, cache available)
```json
{
  "agent_name": "my-agent",
  "has_dashboard": true,
  "config": { "title": "...", "sections": [...] },
  "last_modified": "2024-01-15T10:30:00",
  "status": "running",
  "error": null,
  "stale": true,
  "stale_reason": "YAML parse error at line 5, column 3: mapping values are not allowed here"
}
```

### Error Responses

**Agent not running:**
```json
{
  "agent_name": "my-agent",
  "has_dashboard": false,
  "config": null,
  "last_modified": null,
  "status": "stopped",
  "error": "Agent must be running to read dashboard"
}
```

**No dashboard file:**
```json
{
  "has_dashboard": false,
  "config": null,
  "last_modified": null,
  "error": "No dashboard.yaml found at /home/developer/dashboard.yaml"
}
```

**Validation error:**
```json
{
  "has_dashboard": false,
  "config": null,
  "last_modified": null,
  "error": "Validation errors: Missing required 'title' field; Section 0, Widget 1 (status): missing required 'color' field"
}
```

## Side Effects

- **No WebSocket broadcasts**: Dashboard is polled, not pushed
- **No Audit Log**: Read-only operation
- **In-memory cache**: Last valid dashboard config cached per agent (lost on backend restart)
- **Update Dashboard button**: Triggers task execution via `POST /api/agents/{name}/task`

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| Not authenticated | 401 | Unauthorized |
| No access to agent | 403 | You don't have permission to access this agent |
| Agent not found | 404 | Agent not found |
| Agent not running | 200 | `error: "Agent must be running to read dashboard"` |
| No dashboard.yaml | 200 | `error: "No dashboard.yaml found..."` |
| YAML parse error (no cache) | 200 | `error: "YAML parse error at line X, column Y: ..."` |
| YAML parse error (cached) | 200 | `stale: true, stale_reason: "YAML parse error..."` |
| Validation error (no cache) | 200 | `error: "Validation errors: ..."` |
| Validation error (cached) | 200 | `stale: true, stale_reason: "Validation errors..."` |
| Agent timeout (no cache) | 200 | `error: "Agent is starting up, please try again"` |
| Agent timeout (cached) | 200 | `stale: true, stale_reason: "Agent is starting up..."` |

## Security Considerations

- **Authorization**: User must have access to agent (owner or shared)
- **File Path**: Dashboard file located at `/home/developer/dashboard.yaml`
- **Image URLs**: `/files/` paths converted to authenticated API paths
- **YAML Parsing**: Uses `yaml.safe_load()` to prevent code execution
- **Markdown Rendering**: Uses `marked` library (client-side)

## Auto-Refresh

The component implements auto-refresh based on the `refresh` value in the dashboard config:

```javascript
// DashboardPanel.vue:457-465
const startRefresh = () => {
  if (refreshInterval) clearInterval(refreshInterval)
  const refreshSeconds = dashboardData.value?.config?.refresh || 30
  refreshInterval = setInterval(() => {
    if (props.agentStatus === 'running') {
      loadDashboard()
    }
  }, refreshSeconds * 1000)
}
```

- Minimum refresh interval: 5 seconds (validated server-side)
- Default refresh interval: 30 seconds
- Stops refreshing when agent is not running
- Restarts when agent status changes to running

## Testing

### Prerequisites
- Agent container running
- User has access to the agent

### Test Steps

1. **No Dashboard File**
   - Action: View Dashboard tab for agent without `dashboard.yaml`
   - Expected: "No Dashboard Defined" message with instructions
   - Verify: Shows `~/dashboard.yaml` path hint

2. **Valid Dashboard**
   - Action: Create `~/dashboard.yaml` with valid widgets, view Dashboard tab
   - Expected: All widgets rendered correctly
   - Verify: Title, sections, and all widget types display

3. **Validation Error**
   - Action: Create `dashboard.yaml` missing required fields
   - Expected: "Dashboard Error" message with specific errors
   - Verify: Error message lists all validation failures

4. **Auto-Refresh**
   - Action: Set `refresh: 10` in config, modify `dashboard.yaml`
   - Expected: Changes appear within 10 seconds
   - Verify: Update timestamp shows recent time

5. **Agent Not Running**
   - Action: Stop agent, view Dashboard tab
   - Expected: "Agent Not Running" message
   - Verify: No loading spinner, clear instructions

6. **Stale Dashboard (YAML broken after valid)**
   - Action: Create valid dashboard, view it, then break YAML syntax
   - Expected: Yellow "Showing cached dashboard" banner with error message, last valid dashboard still displayed
   - Verify: Tab remains visible, widgets still render, error reason shown

7. **Update Dashboard Button**
   - Action: Assign `update-dashboard` skill to agent, view Dashboard tab
   - Expected: "Update Dashboard" button appears next to refresh
   - Verify: Clicking triggers task, spinner shown, dashboard refreshes after ~5s

8. **No Update Dashboard Button (no playbook)**
   - Action: View Dashboard tab for agent without `update-dashboard` skill
   - Expected: No "Update Dashboard" button shown
   - Verify: Only refresh button visible

### Edge Cases
- YAML syntax error (e.g., bad indentation) with no prior valid cache → error state
- YAML syntax error with prior valid cache → stale dashboard shown
- Widget with missing required fields
- Empty dashboard.yaml file
- Image widget with invalid path
- Backend restart clears stale cache → error state until YAML fixed

### Status
- Not Tested (new feature)

## Related Flows

- **Upstream**: Agent Lifecycle (agent must be running)
- **Downstream**: None
- **Similar**: Agent Custom Metrics (replaced by this feature)

## Revision History

| Date | Change |
|------|--------|
| 2026-09-06 | **Widget-type closed set** pinned by a parity test (backend ≡ agent server ≡ renderer ≡ docs); D-002 now names the offending type (#2110). Refreshed stale Widget Renderers line numbers. |
| 2026-03-15 | **Dashboard Resilience**: Added stale cache fallback - when YAML breaks, last valid dashboard shown with warning banner. Added "Update Dashboard" button that triggers `/update-dashboard` playbook if available. Tab visibility now considers stale responses. |
| 2026-02-12 | **Conditional Tab Visibility**: Dashboard tab now hidden when agent doesn't have `dashboard.yaml`. |
| 2026-02-11 | Fixed workspace path references - dashboard.yaml only at `/home/developer/dashboard.yaml` (no workspace fallback) |
| 2026-01-12 | Initial documentation - 11 widget types, YAML-based configuration |
