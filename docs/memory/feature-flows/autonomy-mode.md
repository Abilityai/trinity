# Feature: Autonomy Mode

> **Last Updated**: 2026-08-03 (#1945) — the toggle is a **gate, not a bulk edit**: it writes only `agent_ownership.autonomy_enabled` and no longer rewrites per-schedule `enabled`. Per-schedule intent now survives an off→on cycle.
>
> **Previous (2026-02-22)** - Dashboard visual indication: AgentNode now shows "(paused)" text next to schedule count when autonomy is disabled, providing immediate visual feedback that schedules are not executing.

## Overview
Autonomy Mode is the agent-level master gate for proactive work. When autonomy is enabled, the agent's **enabled** schedules run automatically. When disabled, no cron trigger fires for the agent at all.

**Gate, not a bulk edit (#1945).** The toggle writes exactly one row — `agent_ownership.autonomy_enabled` — and never touches a schedule's own `enabled` flag. The two are different concepts:

| Flag | Written by | Means |
|------|-----------|-------|
| `agent_ownership.autonomy_enabled` | the autonomy toggle | may this agent do proactive work *at all* |
| `agent_schedules.enabled` | the owner (Schedules tab / API / template) | should *this* schedule run while the agent is autonomous |

Until #1945 both were written by the toggle: `set_autonomy_status_logic` looped `set_schedule_enabled(id, enabled)` over every schedule, unfiltered and in both directions, so the first toggle destroyed per-schedule intent — a deliberately-disabled schedule was silently re-armed on the next autonomy-on, and autonomy-off was a set-all rather than a pause. With a template able to materialize up to 20 declared schedules at agent creation, one unrelated toggle could arm all of them at once.

Consequences of the gate model:
- An enabled schedule on a paused agent is a **normal, expected state**. The scheduler skips it (cron-only gate), writes no execution row, and advances its `next_run_at` projection so the UI never shows a receding "Next: Nd ago" (#1472). The Schedules tab labels it "Will not fire — autonomy off" and offers a one-click enable-autonomy banner (#1796).
- Re-enabling autonomy restores exactly the per-schedule state the owner left, disabled ones included. **Upgrade note:** an agent whose schedules were already flattened to disabled by a pre-#1945 autonomy-off stays that way — nothing re-arms them, and the toggle response says so ("all N schedule(s) are disabled — nothing will run until you enable one").
- Admin fleet ops (`POST /api/ops/schedules/pause|resume`, `emergency_stop`) still write `enabled` in bulk by design — those are explicit set-all incident tools, not a per-agent gate.

**Scope (autonomy governs proactive work ONLY, #1557):** the toggle deliberately does **not** touch the transport circuit breaker or any inbound path — a paused agent still answers manual chat, Telegram/Slack/public, and webhooks normally. An earlier hook (#631 AC#5) forced the transport breaker `dormant` on autonomy-off; because the `execute_task` gate consults that breaker for every trigger, it fast-failed all inbound chat on a healthy paused agent with "circuit breaker open — agent is unhealthy". That coupling was removed — see [dispatch-circuit-breaker.md](dispatch-circuit-breaker.md). #631's flood protection does not depend on it (the breaker's own failure-driven backoff/dormant path plus the #1464 leader lock and #1121 monitoring-default-off throttle a genuinely-down agent).

## User Story
As an agent owner, I want to toggle autonomous operation for my agent so that I can quickly enable or disable all scheduled tasks without managing each schedule individually.

## Entry Points

### UI Locations (via AutonomyToggle component)
| Location | File | Lines | Notes |
|----------|------|-------|-------|
| Dashboard Graph | `AgentNode.vue` | 78-85 | Same row as RunningStateToggle |
| Dashboard Timeline | `ReplayTimeline.vue` | 155-161 | No label (compact) |
| Agent Detail Header | `AgentHeader.vue` | 120-125 | Medium size, owners only |
| Dashboard List rows | `AgentListPanel.vue` | — | Controls column; rewired to `networkStore.toggleAutonomy` (ent#260 — replaces the retired Agents page) |

### Component
- **Reusable Component**: `src/frontend/src/components/AutonomyToggle.vue` (151 lines)
- See [autonomy-toggle-component.md](autonomy-toggle-component.md) for component documentation

### API
- `GET /api/agents/autonomy-status` - Bulk status for dashboard
- `GET /api/agents/{name}/autonomy` - Per-agent status with schedule counts
- `PUT /api/agents/{name}/autonomy` - Toggle autonomy on/off

---

## Frontend Layer

### Dashboard View (AgentNode.vue)

**File**: `src/frontend/src/components/AgentNode.vue`

#### Toggle Switch (lines 66-100)
The Dashboard agent tiles include an inline toggle switch for quick autonomy control:
```vue
<!-- Autonomy toggle switch with label (not for system agent) -->
<div v-if="!isSystemAgent" class="flex items-center gap-1.5">
  <span
    :class="[
      'text-xs font-medium transition-colors',
      autonomyEnabled
        ? 'text-amber-600 dark:text-amber-400'
        : 'text-gray-400 dark:text-gray-500'
    ]"
  >
    {{ autonomyEnabled ? 'AUTO' : 'Manual' }}
  </span>
  <button
    @click="handleAutonomyToggle"
    :disabled="autonomyLoading"
    :class="[
      'nodrag relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-offset-2',
      autonomyEnabled
        ? 'bg-amber-500 focus:ring-amber-500'
        : 'bg-gray-200 dark:bg-gray-600 focus:ring-gray-400',
      autonomyLoading ? 'opacity-50 cursor-wait' : ''
    ]"
    role="switch"
    :aria-checked="autonomyEnabled"
  >
    <span
      :class="[
        'pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out',
        autonomyEnabled ? 'translate-x-4' : 'translate-x-0'
      ]"
    />
  </button>
</div>
```

**Visual Design**:
- Toggle switch (36x20px) with sliding knob
- Label shows "AUTO" (amber) or "Manual" (gray)
- Disabled state with opacity when API call in progress
- Tooltips explain the current state and action

**Schedule Paused Indicator** (Added 2026-02-22):
When autonomy is disabled and the agent has schedules, a visual "(paused)" indicator appears next to the schedule count:
```vue
<!-- Schedule Stats (compact row) - AgentNode.vue:138-155 -->
<div
  v-if="hasSchedules && !isSystemAgent"
  :class="[
    'flex items-center text-xs gap-x-1.5 mb-2',
    autonomyEnabled ? 'text-gray-500 dark:text-gray-400' : 'text-gray-300 dark:text-gray-600'
  ]"
>
  <svg class="w-3 h-3 flex-shrink-0"><!-- clock icon --></svg>
  <span :class="autonomyEnabled ? 'font-medium text-gray-700 dark:text-gray-300' : ''">
    {{ schedulesEnabled }}/{{ schedulesTotal }}
  </span>
  <span>schedules</span>
  <span v-if="!autonomyEnabled" class="italic">(paused)</span>
</div>
```

This provides immediate visual feedback that schedules exist but are not executing because autonomy is disabled.

#### Toggle Handler (lines 352-365)
```javascript
async function handleAutonomyToggle(event) {
  // Stop propagation to prevent card drag
  event.stopPropagation()

  if (autonomyLoading.value || isSystemAgent.value) return

  autonomyLoading.value = true
  try {
    await networkStore.toggleAutonomy(props.data.label)
  } finally {
    autonomyLoading.value = false
  }
}
```

#### Computed Property (lines 224-227)
```javascript
const autonomyEnabled = computed(() => {
  return props.data.autonomy_enabled === true
})
```

### Network Store (network.js)

**File**: `src/frontend/src/stores/network.js`

#### Toggle Autonomy Action (lines 1172-1209)
```javascript
async function toggleAutonomy(agentName) {
  // Find the node to get current state
  const node = nodes.value.find(n => n.id === agentName)
  if (!node) {
    console.error('[Network] Agent not found:', agentName)
    return { success: false, error: 'Agent not found' }
  }

  const currentState = node.data.autonomy_enabled
  const newState = !currentState

  try {
    const token = localStorage.getItem('token')
    const response = await axios.put(
      `/api/agents/${agentName}/autonomy`,
      { enabled: newState },
      { headers: { Authorization: `Bearer ${token}` } }
    )

    // Update the node data
    node.data.autonomy_enabled = newState

    console.log(`[Network] Autonomy ${newState ? 'enabled' : 'disabled'} for ${agentName}`)

    return {
      success: true,
      enabled: newState,
      // #1945: counts, not "how many we changed" — the toggle changes none
      totalSchedules: response.data.total_schedules,
      enabledSchedules: response.data.enabled_schedules,
      message: response.data.message
    }
  } catch (error) {
    console.error('[Network] Failed to toggle autonomy:', error)
    return {
      success: false,
      error: error.response?.data?.detail || 'Failed to update autonomy mode'
    }
  }
}
```

#### Node Conversion (lines 347-366)
The store passes `autonomy_enabled` from agent data to dashboard nodes:
```javascript
regularAgents.forEach((agent, index) => {
  // ...
  result.push({
    id: agent.name,
    type: 'agent',
    data: {
      // ... other fields
      autonomy_enabled: agent.autonomy_enabled || false,
      // ...
    }
  })
})
```

### Agent Detail View (AgentDetail.vue)

**File**: `src/frontend/src/views/AgentDetail.vue`

The AgentDetail view delegates the autonomy toggle to AgentHeader component.

#### Toggle Handler (lines 322-360)
```javascript
const autonomyLoading = ref(false)

async function toggleAutonomy() {
  if (!agent.value || autonomyLoading.value) return

  autonomyLoading.value = true
  const newState = !agent.value.autonomy_enabled

  try {
    const response = await fetch(`/api/agents/${agent.value.name}/autonomy`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem('token')}`
      },
      body: JSON.stringify({ enabled: newState })
    })

    if (!response.ok) {
      const error = await response.json()
      throw new Error(error.detail || 'Failed to update autonomy mode')
    }

    const result = await response.json()

    // Update local state
    agent.value.autonomy_enabled = newState

    // #1945: the server authors this line — the toggle gates schedules rather
    // than activating them, and the message names the case the raw count hid.
    showNotification(
      result.message || `Autonomy ${newState ? 'enabled' : 'disabled'}.`,
      'success'
    )
  } catch (error) {
    console.error('Failed to toggle autonomy:', error)
    showNotification(error.message || 'Failed to update autonomy mode', 'error')
  } finally {
    autonomyLoading.value = false
  }
}
```

### Agent Header Component (AgentHeader.vue)

**File**: `src/frontend/src/components/AgentHeader.vue`

#### Toggle Button (lines 134-157)
```vue
<template v-if="!agent.is_system && agent.can_share">
  <div class="h-4 w-px bg-gray-300 dark:bg-gray-600 mx-1"></div>
  <button
    @click="$emit('toggle-autonomy')"
    :disabled="autonomyLoading"
    :class="[
      'inline-flex items-center text-sm font-medium py-1.5 px-3 rounded transition-colors',
      agent.autonomy_enabled
        ? 'bg-amber-100 dark:bg-amber-900/50 text-amber-700 dark:text-amber-300 hover:bg-amber-200 dark:hover:bg-amber-900/70'
        : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-600'
    ]"
    :title="agent.autonomy_enabled ? 'Autonomy Mode ON - Scheduled tasks are running' : 'Autonomy Mode OFF - Click to enable scheduled tasks'"
  >
    <svg v-if="autonomyLoading" class="animate-spin -ml-0.5 mr-1.5 h-3.5 w-3.5" ...></svg>
    <svg v-else class="w-3.5 h-3.5 mr-1.5" ...></svg>
    {{ agent.autonomy_enabled ? 'AUTO' : 'Manual' }}
  </button>
</template>
```

---

## Backend Layer

### Router Endpoints

**File**: `src/backend/routers/agent_config.py` (per-agent get/put autonomy endpoints; global `/autonomy-status` bulk view remains in `routers/agents.py`)

#### Bulk Status Endpoint (lines 168-173)
```python
@router.get("/autonomy-status")
async def get_all_autonomy_status(
    current_user: User = Depends(get_current_user)
):
    """Get autonomy status for all accessible agents (for dashboard display)."""
    return await get_all_autonomy_status_logic(current_user)
```

#### Per-Agent Status Endpoint (lines 772-778)
```python
@router.get("/{agent_name}/autonomy")
async def get_agent_autonomy_status(
    agent_name: str,
    current_user: User = Depends(get_current_user)
):
    """Get the autonomy status for an agent."""
    return await get_autonomy_status_logic(agent_name, current_user)
```

#### Toggle Endpoint (lines 781-796)
```python
@router.put("/{agent_name}/autonomy")
async def set_agent_autonomy_status(
    agent_name: str,
    body: dict,
    current_user: User = Depends(get_current_user)
):
    """
    Set the autonomy status for an agent.

    Body:
    - enabled: True to enable autonomy, False to disable

    When autonomy is enabled, all schedules for the agent are enabled.
    When disabled, all schedules are paused.
    """
    return await set_autonomy_status_logic(agent_name, body, current_user)
```

### Service Layer

**File**: `src/backend/services/agent_service/autonomy.py`

#### Get Status Logic (lines 21-50)
```python
async def get_autonomy_status_logic(
    agent_name: str,
    current_user: User
) -> dict:
    """Get the autonomy status for an agent."""
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="You don't have permission to access this agent")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    autonomy_enabled = db.get_autonomy_enabled(agent_name)

    # Get schedule counts
    schedules = db.list_agent_schedules(agent_name)
    total_schedules = len(schedules)
    enabled_schedules = sum(1 for s in schedules if s.enabled)

    return {
        "agent_name": agent_name,
        "autonomy_enabled": autonomy_enabled,
        "total_schedules": total_schedules,
        "enabled_schedules": enabled_schedules,
        "status": container.status
    }
```

#### Set Status Logic (lines 53-117)
```python
async def set_autonomy_status_logic(
    agent_name: str,
    body: dict,
    current_user: User
) -> dict:
    """Set the autonomy status for an agent."""
    # Only owner can modify autonomy
    if not db.can_user_share_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Only the owner can modify autonomy settings")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Don't allow autonomy for system agent
    if db.is_system_agent(agent_name):
        raise HTTPException(status_code=403, detail="Cannot modify autonomy for system agent")

    enabled = body.get("enabled")
    if enabled is None:
        raise HTTPException(status_code=400, detail="enabled is required")

    enabled = bool(enabled)

    # The ONLY write (#1945). Do NOT re-add a per-schedule fan-out here — the
    # per-schedule `enabled` flag is owner intent and must survive a toggle.
    db.set_autonomy_enabled(agent_name, enabled)

    # Report-only: what the agent's schedules will do under the new gate.
    schedules = db.list_agent_schedules(agent_name)
    total_schedules = len(schedules)
    enabled_schedules = sum(1 for s in schedules if s.enabled)
    # message names the case: no schedules / all disabled / N of M will run
    ...

    return {
        "status": "updated",
        "agent_name": agent_name,
        "autonomy_enabled": enabled,
        "total_schedules": total_schedules,
        "enabled_schedules": enabled_schedules,
        "message": message,
    }
```

> **Note (2026-08-03, #1945)**: the pre-#1945 body looped `db.set_schedule_enabled(id, enabled)` over every schedule here and returned `schedules_updated`. Both are gone — the loop was the defect (it erased per-schedule intent), and the count described a write that no longer happens. The response now carries `total_schedules` + `enabled_schedules` and a server-authored `message` the UI renders verbatim. The dedicated scheduler (`src/scheduler/`) picks up genuine per-schedule changes on its 60s sync; the autonomy gate itself is read live at fire time, so a toggle takes effect immediately.

#### Bulk Status Logic (lines 120-143)
```python
async def get_all_autonomy_status_logic(
    current_user: User
) -> Dict[str, dict]:
    """Get autonomy status for all agents accessible to the user."""
    all_status = db.get_all_agents_autonomy_status()

    result = {}
    for agent_name, autonomy_enabled in all_status.items():
        if db.can_user_access_agent(current_user.username, agent_name):
            # Skip system agent
            if db.is_system_agent(agent_name):
                continue
            result[agent_name] = {
                "autonomy_enabled": autonomy_enabled
            }

    return result
```

---

## Data Layer

### Database Schema

**File**: `src/backend/database.py`

#### agent_ownership Table (lines 328-338)
```sql
CREATE TABLE agent_ownership (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT UNIQUE NOT NULL,
    owner_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    is_system INTEGER DEFAULT 0,
    use_platform_api_key INTEGER DEFAULT 1,
    autonomy_enabled INTEGER DEFAULT 0,
    memory_limit TEXT,
    cpu_limit TEXT,
    FOREIGN KEY (owner_id) REFERENCES users(id)
)
```

#### Migration (lines 219-227)
```python
def _migrate_agent_ownership_autonomy(cursor, conn):
    """Add autonomy_enabled column to agent_ownership table for autonomous scheduling control."""
    cursor.execute("PRAGMA table_info(agent_ownership)")
    columns = {row[1] for row in cursor.fetchall()}

    if "autonomy_enabled" not in columns:
        print("Adding autonomy_enabled column to agent_ownership for autonomous scheduling...")
        cursor.execute("ALTER TABLE agent_ownership ADD COLUMN autonomy_enabled INTEGER DEFAULT 0")
        conn.commit()
```

### Database Operations

**File**: `src/backend/db/agents.py`

#### get_autonomy_enabled (lines 330-341)
```python
def get_autonomy_enabled(self, agent_name: str) -> bool:
    """Check if autonomy mode is enabled for agent (scheduled tasks run automatically)."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(autonomy_enabled, 0) as autonomy_enabled
            FROM agent_ownership WHERE agent_name = ?
        """, (agent_name,))
        row = cursor.fetchone()
        if row:
            return bool(row["autonomy_enabled"])
        return False  # Default to disabled
```

#### set_autonomy_enabled (lines 343-352)
```python
def set_autonomy_enabled(self, agent_name: str, enabled: bool) -> bool:
    """Set whether autonomy mode is enabled for agent."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE agent_ownership SET autonomy_enabled = ?
            WHERE agent_name = ?
        """, (1 if enabled else 0, agent_name))
        conn.commit()
        return cursor.rowcount > 0
```

#### get_all_agents_autonomy_status (lines 354-362)
```python
def get_all_agents_autonomy_status(self) -> Dict[str, bool]:
    """Get autonomy status for all agents (for dashboard display)."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT agent_name, COALESCE(autonomy_enabled, 0) as autonomy_enabled
            FROM agent_ownership
        """)
        return {row["agent_name"]: bool(row["autonomy_enabled"]) for row in cursor.fetchall()}
```

---

## Side Effects

### Schedule Toggling — none (#1945)
Toggling autonomy writes **no** schedule row. The single side effect is the
`agent_ownership.autonomy_enabled` flag; every schedule keeps its own `enabled`,
`next_run_at` and `updated_at` untouched (a test pins the unchanged
`updated_at`/`next_run_at` pair, since `set_schedule_enabled` would bump both).

```python
db.set_autonomy_enabled(agent_name, enabled)   # the only write
schedules = db.list_agent_schedules(agent_name)  # read-only, for the response counts
```

Because nothing changes per schedule, the scheduler's 60s sync has nothing to
pick up — the gate is read live on every cron fire, so the pause/resume is
immediate.

### Scheduler Enforcement
The **dedicated scheduler service** double-checks autonomy before executing any schedule:

**File**: `src/scheduler/service.py` (lines 237-242)

```python
async def _execute_schedule_with_lock(self, schedule_id: str):
    schedule = self.db.get_schedule(schedule_id)
    if not schedule:
        logger.error(f"Schedule {schedule_id} not found")
        return

    if not schedule.enabled:
        logger.info(f"Schedule {schedule_id} is disabled, skipping")
        return

    # Check if agent has autonomy enabled (master switch for all schedules)
    if not self.db.get_autonomy_enabled(schedule.agent_name):
        logger.info(f"Schedule {schedule_id} skipped: agent {schedule.agent_name} autonomy is disabled")
        return

    # ... execute schedule ...
```

This provides defense-in-depth: even if a schedule is somehow enabled in the database, it won't execute unless the agent's autonomy is also enabled.

### Database Schedule State

**File**: `src/backend/db/schedules.py` (lines 290-315)

```python
def set_schedule_enabled(schedule_id: str, enabled: bool):
    """Enable or disable a schedule with next_run_at recalculation."""
    if enabled:
        # Calculate next_run_at when enabling
        next_run = _calculate_next_run_at(cron_expression, timezone)
        cursor.execute("""
            UPDATE agent_schedules
            SET enabled = 1, next_run_at = ?, updated_at = ?
            WHERE id = ?
        """, (next_run, utc_now_iso(), schedule_id))
    else:
        # Clear next_run_at when disabling
        cursor.execute("""
            UPDATE agent_schedules
            SET enabled = 0, next_run_at = NULL, updated_at = ?
            WHERE id = ?
        """, (utc_now_iso(), schedule_id))
```

### Logging
Server-side logging when autonomy state changes:
```python
logger.info(
    f"Autonomy {'enabled' if enabled else 'disabled'} for agent {agent_name} "
    f"by {current_user.username}. Updated {updated_count} schedules."
)
```

---

## Error Handling

| Error Case | HTTP Status | Message |
|------------|-------------|---------|
| No access to agent | 403 | "You don't have permission to access this agent" |
| Not owner (for toggle) | 403 | "Only the owner can modify autonomy settings" |
| Agent not found | 404 | "Agent not found" |
| System agent toggle | 403 | "Cannot modify autonomy for system agent" |
| Missing `enabled` field | 400 | "enabled is required" |

---

## Security Considerations

1. **Access Control**: Only users with access to an agent can view its autonomy status
2. **Owner-Only Toggle**: Only the agent owner (or admin) can modify autonomy settings (uses `can_user_share_agent` permission)
3. **System Agent Protection**: System agents are excluded from autonomy controls entirely
4. **Dashboard Filtering**: Bulk status endpoint only returns agents the user can access, excluding system agents

---

## API Response Examples

### GET /api/agents/autonomy-status
```json
{
  "my-agent": { "autonomy_enabled": true },
  "another-agent": { "autonomy_enabled": false }
}
```

### GET /api/agents/{name}/autonomy
```json
{
  "agent_name": "my-agent",
  "autonomy_enabled": true,
  "total_schedules": 3,
  "enabled_schedules": 3,
  "status": "running"
}
```

### PUT /api/agents/{name}/autonomy
Request:
```json
{ "enabled": true }
```

Response:
```json
{
  "status": "updated",
  "agent_name": "my-agent",
  "autonomy_enabled": true,
  "total_schedules": 3,
  "enabled_schedules": 2,
  "message": "Autonomy enabled. 2 of 3 schedule(s) will run; per-schedule settings unchanged."
}
```

---

## Testing

### Prerequisites
- Trinity platform running (`./scripts/deploy/start.sh`)
- At least one agent created
- At least one schedule configured for the agent

### Test Steps

1. **Dashboard Toggle Switch**
   - Action: Open Dashboard (/)
   - Expected: Agent cards show toggle switch with "Manual" label (gray)
   - Verify: Click toggle - switch slides right, label changes to "AUTO" (amber)
   - Verify: Click again - switch slides left, returns to "Manual"

2. **Per-schedule intent survives a toggle (#1945)** — the regression scenario
   - Action: on an agent with two enabled schedules, disable ONE from the Schedules tab, then toggle autonomy off and back on
   - Expected: the disabled schedule is still disabled; the other is still enabled
   - Verify: Schedules tab shows one Active + one Disabled; `sqlite3 ~/trinity-data/trinity.db "SELECT id, enabled FROM agent_schedules WHERE agent_name='<agent>'"` matches
   - Automated: `tests/unit/test_1945_autonomy_preserves_schedule_intent.py`

3. **Toggle from Agent Detail**
   - Action: Open agent detail page, click "Manual" button
   - Expected: Button changes to "AUTO", notification names the case ("N of M schedule(s) will run", or "all N are disabled — nothing will run")
   - Verify: Refresh page - state persists

4. **Disable Autonomy**
   - Action: Click "AUTO" button on an agent with autonomy enabled
   - Expected: Button changes to "Manual", "N schedule(s) paused; per-schedule settings preserved"
   - Verify: Schedules tab still shows each schedule's own Active/Disabled state (unchanged), with the "Will not fire — autonomy off" warning on the enabled ones; nothing fires

5. **System Agent Exclusion**
   - Action: Navigate to trinity-system agent
   - Expected: No autonomy toggle button visible
   - Verify: API call returns 403 for system agent

6. **Non-Owner Access**
   - Action: As non-owner, try to toggle autonomy on shared agent
   - Expected: Toggle button not visible (only visible if can_share)
   - Verify: Direct API call returns 403

### Status
- Working (2026-01-23)

---

## Related Flows

- **Upstream**: [Scheduling](scheduling.md) - Autonomy gates whether an enabled schedule may fire (it does not change the schedule's own `enabled`, #1945)
- **Related**: [Scheduler Service](scheduler-service.md) - Dedicated scheduler enforces autonomy check before execution
- **Related**: [Agent Lifecycle](agent-lifecycle.md) - Agent must exist for autonomy to apply
- **Related**: [Agent Sharing](agent-sharing.md) - Shares `can_share` permission check for toggle access

---

## Revision History

| Date | Change |
|------|--------|
| 2026-08-03 | **Gate, not a bulk edit (#1945)**: `set_autonomy_status_logic` no longer loops `set_schedule_enabled` over every schedule — it writes only `agent_ownership.autonomy_enabled`, and the scheduler's existing cron-fire gate (`src/scheduler/service.py::_execute_schedule_with_lock`) does the rest. Per-schedule `enabled` is now purely owner intent and survives an autonomy off→on cycle in both directions; a template-authored `enabled: false` is likewise no longer erased. Response drops `schedules_updated` (a count of a write that no longer happens) for `total_schedules` + `enabled_schedules` + a server-authored `message`; `AgentDetail.vue` renders the message verbatim. No schema change and no migration — the fix is the removal of a write. Upgrade note: an agent already flattened to all-disabled by a pre-#1945 toggle stays that way (the intent is unrecoverable), and the response says so. |
| 2026-07-10 | **Decoupled from the circuit breaker (#1557)**: removed the #631 AC#5 hook that forced the transport circuit breaker `dormant` on autonomy-off (and reset it on autonomy-on). Pausing autonomy no longer blocks inbound chat — it acts only via `set_schedule_enabled`. The misleading "agent is unhealthy" fast-fail message was also split to name the real cause (transport-unreachable vs dispatch-auth-dead). See `services/agent_service/autonomy.py` and `services/task_execution_service.py::_circuit_breaker_error`. |
| 2026-02-22 | **Dashboard Visual Indication**: AgentNode.vue (lines 138-155) now shows "(paused)" text in italics next to schedule count when autonomy is disabled. Schedule stats row grayed out (text-gray-300) when autonomy off vs normal gray (text-gray-500) when on. Schedule count fetched via `/api/agents/execution-stats` which now includes `schedules_total` and `schedules_enabled` fields. |
| 2026-02-12 | **UI Standardization**: Extracted `AutonomyToggle.vue` reusable component (151 lines) used in 4 locations: AgentNode.vue, ReplayTimeline.vue, AgentHeader.vue, Agents.vue. Running and Autonomy toggles now on same row in Dashboard Graph (AgentNode.vue:57-86) and Agents page (Agents.vue:108-123). Created dedicated [autonomy-toggle-component.md](autonomy-toggle-component.md) for component documentation. |
| 2026-02-11 | **Scheduler Consolidation**: Updated to reflect removal of embedded scheduler. Schedule toggling now uses database only; dedicated scheduler syncs changes within 60s. Scheduler enforcement section updated to reference `src/scheduler/service.py`. |
| 2026-01-23 | **Line Number Update**: Verified and updated all line numbers against current codebase. AgentNode.vue toggle at lines 66-100 (handler 352-365). AgentHeader.vue toggle at lines 134-157. network.js toggleAutonomy at lines 1172-1209. AgentDetail.vue handler at lines 322-360. Router endpoints at lines 168, 772, 781. autonomy.py logic unchanged. db/agents.py operations at lines 330-362. |
| 2026-01-03 | **Dashboard Toggle Switch**: Replaced static "AUTO" badge with interactive toggle switch. Users can now enable/disable autonomy directly from Dashboard agent tiles. Added `toggleAutonomy()` action to network.js store. Toggle includes "AUTO/Manual" label with amber/gray styling. |
| 2026-01-03 | Added scheduler enforcement section - scheduler now double-checks autonomy before executing |
| 2026-01-01 | Initial documentation of Autonomy Mode feature |
