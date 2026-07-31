# Feature: Agents Page UI Improvements

> **⚠️ SUPERSEDED (2026-07-31, trinity-enterprise#260)**: the standalone
> Agents page (`views/Agents.vue`) was retired — its list lives on as the
> Dashboard's **List** mode (`components/AgentListPanel.vue`); `/agents`
> redirects to `/?view=list`. Every capability below was migrated or
> consciously re-homed (parity disposition in the ent#260 plan). Current doc:
> [dashboard-list-view.md](dashboard-list-view.md). Kept as historical record.

> **Status**: Superseded by trinity-enterprise#260 (previously: Implemented 2025-12-07, Enhanced 2026-01-09, System Agent Consolidation 2026-01-13, Toggle UX 2026-01-26, Component Standardization 2026-02-12, Horizontal Row Tiles 2026-03-03, Two-Row Tiles + Persistent Filter 2026-03-03, Full Filtering 2026-03-03, Success Rate Bar 2026-03-03, Capacity Meter + Fixed Grid 2026-03-03, Agent Avatars 2026-03-07, Owner Filter 2026-04-08)
> **Tested**: All features verified working
> **Last Updated**: 2026-04-08 - Owner Filter (#69): Added owner dropdown filter to Agents page header bar. Derives distinct owners from full agent list, filters with AND logic alongside name/status/tag filters. Hidden when ≤1 distinct owner. Persisted to localStorage (`trinity-agents-filter-owner`). Supports "Unassigned" option for orphaned agents.
>
> **Previous (2026-03-03)** - Capacity Meter + Fixed Grid Columns: Added CapacityMeter component to desktop and tablet layouts. Desktop: flex sibling alongside two-row content block (height=48, width=6), default fallback active=0/max=3. Tablet: between success bar and stats (height=28, width=10), conditional on slot data. Fixed grid column widths: Activity column 56px, Stats column 200px with `overflow-hidden` to prevent layout shifts from varying stats content.
>
> **Previous (2026-03-03)** - Success Rate Bar (#60): Replaced context usage progress bars with success rate bars across all three breakpoints (desktop, tablet, mobile). Column header "Context" renamed to "Success", grid col 160px to 180px. Sort dropdown "Context Usage" replaced with "Success Rate" (value `success_desc`). Dual-window stats (24h + 7d) fetched via `include_7d=true` param. Color coding: green (>=90%), yellow (50-89%), red (<50%), gray dash for no data. Fallback logic: 24h bar + 7d secondary text, 7d-only bar, or gray dash.
>
> **Previous (2026-03-03)** - Full Filtering (#55): Single-row header with title, search input, status segmented buttons (All/Running/Stopped), tag dropdown, sort dropdown, and Create Agent button all on one line. Filters combine with AND logic. Filter state persists in localStorage (`trinity-agents-filter-name`, `trinity-agents-filter-status`, `trinity-agents-filter-tag-dropdown`). Shows filtered count (X/Y) when filters active. Clear button. Smart empty state with "No matching agents" message. Legacy `trinity-agents-filter-tag` and `trinity-agents-filter-tags` auto-migrate to new dropdown format.
>
> **Previous (2026-03-03)** - Two-Row Tiles: Desktop rows split into top line (fixed grid: checkbox, dot, name+badges, activity, toggles, context, stats, arrow) and bottom line (tag pills, left-aligned under name). Tags column removed from grid. Gap-based spacing (`gap-1.5`) replaces border separators. Agent rows use `rounded-lg`. Tag filter persists in localStorage.
>
> **Previous (2026-03-03)** - Horizontal Row Tile Layout: Complete template rewrite from 3-column card grid to full-width horizontal rows with three responsive breakpoints (desktop/tablet/mobile). Column headers on desktop. System agent styling changed to purple left border accent. Chevron arrow replaces "View Details" button.
>
> **Previous (2026-02-12)** - UI Standardization: AutonomyToggle component used instead of inline implementation. Running and Autonomy toggles now on same row (lines 108-123).
>
> **Previous (2026-01-26)** - UX: Unified Start/Stop Toggle: Replaced separate Start/Stop buttons with `RunningStateToggle.vue` component. Shows "Running" (green) or "Stopped" (gray) state. Uses `toggleAgentRunning()` from agents.js store.
>
> **Previous (2026-01-13)** - System Agent Display: System agent now visible on Agents page for admin users only. Pinned at top with purple ring and "SYSTEM" badge. Uses standard AgentDetail.vue with tab filtering instead of dedicated SystemAgent.vue. Added `systemAgent`, `sortedAgentsWithSystem` getters and `displayAgents` computed to conditionally show system agent.

## Overview

Enhance the Agents list page (`/agents`) with status indicators, success rate bars, execution stats, and sorting capabilities by reusing existing APIs and components from the Collaboration Dashboard.

## Current State Analysis

### Agents Page (Agents.vue) - Current
- Simple list view with server icon
- Shows: agent name, type, port, RuntimeBadge (Claude/Gemini)
- **Running State Toggle** (2026-01-26): `RunningStateToggle.vue` component with "Running/Stopped" label, green/gray styling, loading spinner
- "Shared by X" badge for shared agents
- Activity state indicators, success rate bars, execution stats, sorting (implemented 2025-12-07, context bar replaced with success rate bar 2026-03-03)

### Collaboration Dashboard (AgentNode.vue) - Already Has
- Activity state (Active/Idle/Offline) with pulsing green dot
- Context progress bar with color coding (green → yellow → orange → red)
- GitHub repo display
- Status dot with pulse animation

### Existing APIs to Reuse
| Endpoint | Purpose | Used By |
|----------|---------|---------|
| `GET /api/agents/context-stats` | Context % + activity state for all agents | network.js, agents.js |
| `GET /api/agents/execution-stats?include_7d=true` | Task counts, success rates (24h + 7d), costs | agents.js |
| `GET /api/agents` | Base agent list | agents.js, network.js |

---

## Proposed Improvements

### 1. Activity State Indicator
**Description**: Add a status indicator showing Active/Idle/Offline state for each agent

**Visual Design** (matches AgentNode.vue):
- **Active**: Green pulsing dot (`#10b981`) + "Active" label
- **Idle**: Green static dot (`#10b981`) + "Idle" label
- **Offline**: Gray dot (`#9ca3af`) + "Offline" label

**Implementation**:
- Call `GET /api/agents/context-stats` on mount and poll every 10 seconds
- Parse `activityState` field from response
- Reuse CSS animation from AgentNode.vue:
```css
.active-pulse {
  animation: active-pulse-animation 0.8s ease-in-out infinite;
  box-shadow: 0 0 8px 2px rgba(16, 185, 129, 0.6);
}
```

### 2. Success Rate Bar (replaced Context Progress Bar in Issue #60)
**Description**: Show execution success rate as a progress bar with dual-window (24h + 7d) stats

**Visual Design**:
- Mini progress bar in dedicated "Success" column
- Color coding by success rate percentage:
  - 90-100%: Green (`bg-green-500`)
  - 50-89%: Yellow (`bg-yellow-500`)
  - 0-49%: Red (`bg-red-500`)
  - No data: Gray bar with em-dash
- Fallback logic:
  - **24h data available**: Show 24h bar + percentage, plus 7d rate in parentheses if available
  - **7d only (no 24h)**: Show 7d bar + percentage with "(7d)" label
  - **No data in 7d**: Gray empty bar with em-dash
- Shown for all agents (not just running ones)

**Data Source**: `GET /api/agents/execution-stats?include_7d=true`
- `task_count_24h`: Number (tasks in last 24h)
- `success_rate`: Number (0-100, 24h window)
- `task_count_7d`: Number (tasks in last 7 days)
- `success_rate_7d`: Number (0-100, 7d window)

### 3. Sorting & Filtering
**Description**: Add sort controls to organize agent list

**Sort Options**:
- **Default**: Creation date (newest first) - `agent.created`
- Name (A-Z, Z-A)
- Status (running first, stopped first)
- Success rate (highest first) - sorts by `successRate` from execution stats

**UI**: Dropdown select in header area, next to "Create Agent" button

**Implementation**:
```javascript
const sortedAgents = computed(() => {
  const sorted = [...agents]
  switch (sortBy.value) {
    case 'created_desc':
      return sorted.sort((a, b) => new Date(b.created) - new Date(a.created))
    case 'name_asc':
      return sorted.sort((a, b) => a.name.localeCompare(b.name))
    case 'status':
      return sorted.sort((a, b) => (b.status === 'running' ? 1 : 0) - (a.status === 'running' ? 1 : 0))
    // ... etc
  }
})
```

---

## Technical Implementation

### Option A: Extend agents.js Store (Recommended)
Add context stats fetching to existing agents store:

```javascript
// agents.js - New state
contextStats: {},  // Map of agent name -> stats
contextPollingInterval: null,

// New actions
async fetchContextStats() {
  const response = await axios.get('/api/agents/context-stats', {
    headers: authStore.authHeader
  })
  this.contextStats = {}
  response.data.agents.forEach(stat => {
    this.contextStats[stat.name] = stat
  })
},

startContextPolling() {
  this.fetchContextStats()
  this.contextPollingInterval = setInterval(() => {
    this.fetchContextStats()
  }, 10000)  // Every 10 seconds
},

stopContextPolling() {
  if (this.contextPollingInterval) {
    clearInterval(this.contextPollingInterval)
  }
}
```

### Option B: Reuse network.js Store
Import and use existing store (less code, but adds Vue Flow dependency to simple page):

```javascript
import { useNetworkStore } from '../stores/network'

const networkStore = useNetworkStore()

onMounted(() => {
  // Only start context polling, not full collaboration features
  networkStore.fetchContextStats()
  networkStore.startContextPolling()
})

onUnmounted(() => {
  networkStore.stopContextPolling()
})
```

### Recommended: Option A
Keep agents.js self-contained. Extract shared logic into a composable if needed later.

---

## Updated Agents.vue Component Structure

```vue
<template>
  <div class="min-h-screen bg-gray-100">
    <NavBar />

    <main class="max-w-7xl mx-auto py-6 sm:px-6 lg:px-8">
      <div class="px-4 py-6 sm:px-0">
        <!-- Notification Toast (existing) -->

        <div class="flex justify-between items-center mb-8">
          <h1 class="text-3xl font-bold text-gray-900">Agents</h1>

          <div class="flex items-center space-x-4">
            <!-- NEW: Sort dropdown -->
            <select v-model="sortBy" class="...">
              <option value="created_desc">Newest First</option>
              <option value="created_asc">Oldest First</option>
              <option value="name_asc">Name (A-Z)</option>
              <option value="status">Running First</option>
              <option value="success_desc">Success Rate</option>
            </select>

            <button @click="showCreateModal = true" class="...">
              Create Agent
            </button>
          </div>
        </div>

        <!-- Agents List -->
        <div class="bg-white shadow overflow-hidden sm:rounded-md">
          <ul class="divide-y divide-gray-200">
            <li v-for="agent in sortedAgents" :key="agent.name">
              <div class="px-4 py-4 sm:px-6">
                <div class="flex items-center justify-between">
                  <router-link :to="`/agents/${agent.name}`" class="flex-1">
                    <div class="flex items-center">
                      <!-- Agent Icon -->
                      <div class="flex-shrink-0 relative">
                        <ServerIcon class="h-10 w-10 text-gray-400" />
                        <!-- NEW: Activity state indicator dot -->
                        <div
                          :class="[
                            'absolute -bottom-1 -right-1 w-3 h-3 rounded-full border-2 border-white',
                            getActivityDotClass(agent.name)
                          ]"
                        ></div>
                      </div>

                      <div class="ml-4 flex-1">
                        <div class="flex items-center space-x-2">
                          <p class="text-sm font-medium text-indigo-600">{{ agent.name }}</p>
                          <span v-if="agent.is_shared" class="px-2 py-0.5 text-xs ...">
                            Shared by {{ agent.owner }}
                          </span>
                          <!-- NEW: Activity state label -->
                          <span :class="getActivityLabelClass(agent.name)">
                            {{ getActivityState(agent.name) }}
                          </span>
                        </div>

                        <p class="text-sm text-gray-500">Type: {{ agent.type }}</p>

                        <!-- Success rate bar (replaced context bar in Issue #60) -->
                        <div class="flex items-center gap-2">
                          <template v-if="hasSuccessData(agent.name)">
                            <div class="flex-1 bg-gray-200 dark:bg-gray-700 rounded-full h-1.5 overflow-hidden">
                              <div
                                class="h-full rounded-full transition-all duration-500"
                                :class="getSuccessBarColor(agent.name)"
                                :style="{ width: getSuccessBarPercent(agent.name) + '%' }"
                              ></div>
                            </div>
                            <span :class="getSuccessBarColor(agent.name).replace('bg-', 'text-')">{{ getSuccessBarPercent(agent.name) }}%</span>
                            <span v-if="has7dStats(agent.name)">(7d: {{ get7dSuccessRate(agent.name) }}%)</span>
                          </template>
                          <template v-else-if="has7dOnlyStats(agent.name)">
                            <!-- 7d-only fallback -->
                            <div class="flex-1 bg-gray-200 dark:bg-gray-700 rounded-full h-1.5 overflow-hidden">
                              <div
                                class="h-full rounded-full transition-all duration-500"
                                :class="get7dSuccessBarColor(agent.name)"
                                :style="{ width: get7dSuccessRate(agent.name) + '%' }"
                              ></div>
                            </div>
                            <span :class="get7dSuccessBarColor(agent.name).replace('bg-', 'text-')">{{ get7dSuccessRate(agent.name) }}%</span>
                            <span>(7d)</span>
                          </template>
                          <template v-else>
                            <!-- No data: gray bar + dash -->
                            <div class="flex-1 bg-gray-200 dark:bg-gray-700 rounded-full h-1.5 overflow-hidden"></div>
                            <span>&mdash;</span>
                          </template>
                        </div>
                      </div>
                    </div>
                  </router-link>

                  <!-- Status badge and controls (existing) -->
                  <div class="flex items-center space-x-2">
                    <!-- ... existing status badge and buttons ... -->
                  </div>
                </div>
              </div>
            </li>
          </ul>
        </div>
      </div>
    </main>
  </div>
</template>
```

---

## Helper Functions

```javascript
// Activity state helpers (reuse logic from AgentNode.vue)
const getActivityState = (agentName) => {
  const stats = agentsStore.contextStats[agentName]
  if (!stats) return 'Offline'
  return stats.activityState === 'active' ? 'Active'
       : stats.activityState === 'idle' ? 'Idle'
       : 'Offline'
}

const getActivityDotClass = (agentName) => {
  const state = getActivityState(agentName)
  const baseClasses = 'w-3 h-3 rounded-full'
  if (state === 'Active') return `${baseClasses} bg-green-500 active-pulse`
  if (state === 'Idle') return `${baseClasses} bg-green-500`
  return `${baseClasses} bg-gray-400`
}

const getActivityLabelClass = (agentName) => {
  const state = getActivityState(agentName)
  if (state === 'Active' || state === 'Idle') return 'text-xs text-green-600'
  return 'text-xs text-gray-500'
}

// Success rate bar helpers (replaced context helpers in Issue #60)
const getSuccessBarPercent = (agentName) => {
  const stats = agentsStore.executionStats[agentName]
  return stats ? Math.round(stats.successRate || 0) : 0
}

const getSuccessBarColor = (agentName) => {
  const percent = getSuccessBarPercent(agentName)
  if (percent >= 90) return 'bg-green-500'
  if (percent >= 50) return 'bg-yellow-500'
  return 'bg-red-500'
}

const hasSuccessData = (agentName) => {
  const stats = agentsStore.executionStats[agentName]
  return stats && stats.taskCount > 0
}

const has7dOnlyStats = (agentName) => {
  const stats = agentsStore.executionStats[agentName]
  return stats && stats.taskCount === 0 && stats.taskCount7d > 0
}

const has7dStats = (agentName) => {
  const stats = agentsStore.executionStats[agentName]
  return stats && stats.taskCount7d > 0
}

const get7dSuccessRate = (agentName) => {
  const stats = agentsStore.executionStats[agentName]
  return stats ? Math.round(stats.successRate7d || 0) : 0
}

const get7dSuccessBarColor = (agentName) => {
  const percent = get7dSuccessRate(agentName)
  if (percent >= 90) return 'bg-green-500'
  if (percent >= 50) return 'bg-yellow-500'
  return 'bg-red-500'
}
```

---

## Changes Summary

### Files to Modify
1. **`src/frontend/src/stores/agents.js`**
   - Add `contextStats` state
   - Add `slotStats` state (Map of agent name -> `{ max, active }`)
   - Add `fetchContextStats()`, `fetchSlotStats()` actions
   - Add `startContextPolling()` and `stopContextPolling()` actions

2. **`src/frontend/src/views/Agents.vue`**
   - Add sort dropdown and sorting logic
   - Add activity state indicator (dot + label)
   - Add success rate bar (replaced context bar in Issue #60)
   - Add CapacityMeter to desktop and tablet layouts
   - Import `CapacityMeter` from `../components/CapacityMeter.vue`
   - Import `AgentAvatar` from `../components/AgentAvatar.vue` (line 682)
   - Add `AgentAvatar` with `size="sm"` in all 3 view modes: desktop (line 272), tablet (line 442), mobile (line 585)
   - Add `getSlotStats()` helper reading from `agentsStore.slotStats`
   - Add CSS for pulse animation
   - Call polling on mount/unmount

3. **`src/frontend/src/components/CapacityMeter.vue`**
   - Vertical segmented bar showing active/max parallel slots
   - Props: `active`, `max`, `height`, `width`
   - Color coding: green (<50%), yellow (50-79%), orange (80-99%), red (100%/at capacity with pulse)

4. **`src/backend/routers/agents.py`**
   - Added `include_7d: bool = False` param to GET /execution-stats

5. **`src/backend/db/schedules.py`**
   - Added `get_all_agents_execution_stats_dual()` with single SQL CASE WHEN

6. **`src/backend/database.py`**
   - Added facade method for the dual query

### New Code Reuse
- **API**: `GET /api/agents/context-stats` (already exists, for activity state)
- **API**: `GET /api/agents/execution-stats?include_7d=true` (dual-window stats for success rate bar)
- **API**: `GET /api/agents/slots` (parallel capacity slot stats for CapacityMeter)
- **CSS**: Pulse animation from AgentNode.vue
- **Component**: `CapacityMeter.vue` (reusable vertical bar for slot utilization)
- **Component**: `AgentAvatar.vue` (deterministic gradient avatar with optional image, props: `name`, `avatarUrl`, `size`)
- **Logic**: Success rate bar color coding (green/yellow/red thresholds)
- **Logic**: Activity state detection from network.js

---

## Implementation Order

1. **Phase 1: Sorting**
   - Add sort dropdown
   - Implement sorting computed property
   - Default to newest first

2. **Phase 2: Stats & Success Rate**
   - Add store methods for context and execution stats polling
   - Add activity state indicator (dot + label)
   - Add success rate bar (replaced context progress bar)

3. **Phase 3: Polish**
   - Add loading states
   - Test with various agent states
   - Ensure polling stops on unmount

---

## Testing

### Prerequisites
- Backend running with at least 2 agents
- One agent running, one stopped
- Optionally: agent with active plan

### Test Steps

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Load `/agents` page | Agents sorted by creation date (newest first) |
| 2 | Check running agent | Shows activity state (Active/Idle), context progress bar |
| 3 | Check stopped agent | Shows "Offline" state, no progress bar |
| 4 | Chat with agent | Activity state changes to "Active" within 5s |
| 5 | Wait 60s after chat | Activity state returns to "Idle" |
| 6 | Change sort to "Name (A-Z)" | Agents reorder alphabetically |
| 7 | Navigate away and back | Polling restarts, data fresh |

---

## Future Enhancements

1. **Search/Filter**: Text search by agent name
2. **Grid View**: Toggle between list and card grid
3. **Bulk Actions**: Start/stop multiple agents
4. **Quick Stats**: Summary bar (X running, Y stopped, Z% avg context)

---

## Enhancement: Dashboard Parity (2026-01-09)

### Overview

Major UI overhaul to align Agents page with Dashboard (AgentNode.vue) tiles. Changed from list view to responsive grid layout with card-based design.

### Changes Made

#### Layout
- **Grid Layout**: Changed from vertical list to responsive 3-column grid (`grid-cols-1 md:grid-cols-2 lg:grid-cols-3`)
- **Card Design**: Each agent displayed in a card matching AgentNode.vue styling (320px width concept, shadow, rounded corners)
- **Stopped Agent Styling**: Cards for stopped agents have reduced opacity (`opacity-75`) for visual distinction

#### New Features Added

1. **Autonomy Toggle** (`Agents.vue` lines 98-133)
   - Interactive toggle switch with AUTO/Manual label
   - Amber color (`bg-amber-500`) when enabled, gray (`bg-gray-200`) when disabled
   - Calls `agentsStore.toggleAutonomy()` via `handleAutonomyToggle()` (line 354-364)
   - Loading state with `autonomyLoading` ref during API call
   - ARIA support with `role="switch"` and `aria-checked`

2. **Execution Stats Row** (`Agents.vue` lines 155-172)
   - Task count (24h): `{{ getExecutionStats(agent.name).taskCount }}`
   - Success rate with color coding:
     - Green (`text-green-600`) for ≥80%
     - Yellow (`text-yellow-600`) for 50-79%
     - Red (`text-red-600`) for <50%
   - Total cost in dollars (conditionally shown if > 0)
   - Last execution time (relative display: "just now", "2m ago", "1h ago", "1d ago")
   - Fallback: "No tasks (24h)" when no execution data

3. **Success Rate Bar** (`Agents.vue` -- replaced context progress bar in Issue #60)
   - Shows for all agents (not just running ones)
   - Uses execution stats `successRate` (24h) and `successRate7d` (7d) from `GET /api/agents/execution-stats?include_7d=true`
   - Color coded by success rate percentage:
     - Green (`bg-green-500`) for 90-100%
     - Yellow (`bg-yellow-500`) for 50-89%
     - Red (`bg-red-500`) for 0-49%
     - Gray with em-dash for no data
   - Fallback: 24h bar + 7d secondary text; 7d-only bar; gray dash
   - Smooth transition: `transition-all duration-500`

4. **RuntimeBadge Integration** (`Agents.vue` line 68)
   - Shows Claude/Gemini icon next to agent name
   - Uses existing `RuntimeBadge` component with `show-label="false"`

#### Store Changes (`agents.js`)

1. **New State** (lines 14-16):
   ```javascript
   executionStats: {},  // Map of agent name -> execution stats
   slotStats: {},       // Map of agent name -> { max, active }
   ```

2. **New Actions**:
   - `fetchExecutionStats()` (lines 584-611): Fetches from `GET /api/agents/execution-stats?include_7d=true`
     - Maps response to: `taskCount`, `successCount`, `failedCount`, `runningCount`, `successRate`, `totalCost`, `lastExecutionAt`, `taskCount7d`, `successRate7d`
   - `fetchSlotStats()` (lines 616-638): Fetches from `GET /api/agents/slots`
     - Maps response `agents` map to `{ max, active }` per agent
     - Fails silently on 404 (endpoint may not exist yet)
   - `toggleAutonomy(agentName)` (lines 641-667): Calls `PUT /api/agents/{name}/autonomy`
     - Toggles `agent.autonomy_enabled` locally after successful API call
     - Returns `{ success, enabled, schedulesUpdated }`

3. **Updated Polling** (lines 669-687):
   - `startContextPolling()` calls `fetchContextStats()`, `fetchExecutionStats()`, and `fetchSlotStats()` on mount and every 5 seconds
   - All three fetches run in parallel

#### Helper Functions (`Agents.vue` script section)

| Function | Lines | Purpose |
|----------|-------|---------|
| `getActivityState(agentName)` | 279-286 | Returns 'Active', 'Idle', or 'Offline' |
| `isActive(agentName)` | 288-290 | Returns true if agent is actively processing |
| `getStatusDotColor(agentName)` | 292-297 | Returns hex color for status dot |
| `getActivityLabelClass(agentName)` | 299-303 | Returns Tailwind classes for activity label |
| `getSlotStats(agentName)` | ~908-910 | Returns `{ max, active }` from `agentsStore.slotStats` or null |
| `getSuccessBarPercent(agentName)` | ~848-851 | Returns rounded 24h success rate (0-100) |
| `getSuccessBarColor(agentName)` | ~853-858 | Returns Tailwind bg class (green/yellow/red) based on success rate |
| `hasSuccessData(agentName)` | ~860-863 | Returns true if 24h taskCount > 0 |
| `has7dOnlyStats(agentName)` | ~865-868 | Returns true if 24h=0 but 7d taskCount > 0 |
| `has7dStats(agentName)` | ~870-873 | Returns true if 7d taskCount > 0 |
| `get7dSuccessRate(agentName)` | ~875-878 | Returns rounded 7d success rate (0-100) |
| `get7dSuccessBarColor(agentName)` | ~880-885 | Returns Tailwind bg class for 7d rate |
| `getExecutionStats(agentName)` | 320-322 | Returns execution stats object or null |
| `hasExecutionStats(agentName)` | 324-327 | Returns true if taskCount > 0 |
| `getSuccessRateColorClass(agentName)` | 329-336 | Returns color class for success rate |
| `getLastExecutionDisplay(agentName)` | 338-351 | Returns relative time string |
| `handleAutonomyToggle(agent)` | 354-364 | Handles autonomy toggle with loading state |

### Backend Endpoints

1. **GET /api/agents/execution-stats** (`agents.py` lines 180-246)
   - Returns task counts, success rates, costs for all accessible agents
   - Query params: `hours` (default: 24), `include_7d` (default: false)
   - When `include_7d=true`: calls `db.get_all_agents_execution_stats_dual()` which uses a single SQL query with `CASE WHEN` for both 24h and 7d windows (`db/schedules.py` lines 788-846)
   - Response (with `include_7d=true`): `{ agents: [{ name, task_count_24h, success_count, failed_count, running_count, success_rate, total_cost, last_execution_at, task_count_7d, success_count_7d, failed_count_7d, running_count_7d, success_rate_7d, total_cost_7d, last_execution_at_7d, schedules_total, schedules_enabled }] }`

2. **PUT /api/agents/{name}/autonomy** (`agents.py` lines 775-790)
   - Body: `{ enabled: boolean }`
   - When enabled: activates all schedules for the agent
   - When disabled: pauses all schedules
   - Response: `{ enabled, schedules_updated }`

3. **GET /api/agents/context-stats** (`agents.py` lines 138-141)
   - Returns context window usage and activity state for all agents
   - Response: `{ agents: [{ name, contextPercent, contextUsed, contextMax, activityState, lastActivityTime }] }`

### Visual Comparison

| Feature | Before | After |
|---------|--------|-------|
| Layout | Vertical list (`<ul>` with `<li>`) | 3-column responsive grid (`grid-cols-1 md:grid-cols-2 lg:grid-cols-3`) |
| Autonomy | Not shown | Toggle switch with AUTO/Manual label |
| Execution Stats | Not shown | Tasks · Success Rate · Cost · Last Run row |
| Success Rate Bar | N/A (was context bar) | Success rate bar with 24h + 7d dual windows, color-coded |
| Card Style | Simple table rows | Styled cards with shadow-lg, rounded-xl, hover:shadow-xl |
| Status Indicator | Basic badge | Pulsing dot with activity state label |

### Data Flow

```
User loads /agents page
    └── onMounted() [Agents.vue]
        ├── agentsStore.fetchAgents() → GET /api/agents
        │   └── Backend: get_accessible_agents() [helpers.py:83-153]
        │       ├── list_all_agents_fast() - Docker labels only (no stats)
        │       └── db.get_all_agent_metadata() - Single JOIN query for all metadata
        │           └── Returns: owner, is_system, autonomy, limits, git config, share access
        └── agentsStore.startContextPolling() [agents.js:669-687]
            ├── fetchContextStats() → GET /api/agents/context-stats
            ├── fetchExecutionStats() → GET /api/agents/execution-stats?include_7d=true
            │   └── Backend: db.get_all_agents_execution_stats_dual() [db/schedules.py:788-846]
            │       └── Single SQL with CASE WHEN for 24h + 7d windows
            ├── fetchSlotStats() → GET /api/agents/slots
            │   └── Returns: { agents: { name: { max, active } } }
            │   └── Used by CapacityMeter component on desktop/tablet layouts
            └── setInterval(5000) for continuous updates

User toggles autonomy switch
    └── handleAutonomyToggle(agent) [Agents.vue:354-364]
        └── agentsStore.toggleAutonomy(agentName) [agents.js:550-576]
            └── PUT /api/agents/{name}/autonomy
                └── Updates agent.autonomy_enabled locally
```

### Performance (2026-01-12)

The `/api/agents` endpoint benefits from two optimizations:

1. **Docker Stats Optimization**: `list_all_agents_fast()` (docker_service.py:101-159) extracts data ONLY from container labels, avoiding slow Docker operations (`container.attrs`, `container.stats()`).

2. **Database Batch Queries (N+1 Fix)**: `db.get_all_agent_metadata()` (db/agents.py:467-529) fetches all metadata in a SINGLE JOIN query instead of 8-10 queries per agent.

| Metric | Before | After |
|--------|--------|-------|
| Docker API calls | Full inspect per agent | Labels only |
| Database queries | 160-200 (20 agents) | 2 total |
| Response time | ~2-3 seconds | <50ms |

### Files Modified

1. **`src/frontend/src/stores/agents.js`** (699 lines total):
   - Lines 14-16: Added `executionStats: {}` and `slotStats: {}` state
   - Lines 584-611: Added `fetchExecutionStats()` action
   - Lines 616-638: Added `fetchSlotStats()` action
   - Lines 641-667: Added `toggleAutonomy()` action
   - Lines 669-687: Updated `startContextPolling()` to include execution + slot stats

2. **`src/frontend/src/views/Agents.vue`** (1083 lines total):
   - Line 688: Import `CapacityMeter` from `../components/CapacityMeter.vue`
   - Lines 248-426: Desktop layout with two-row content block + CapacityMeter flex sibling
   - Lines 252: Fixed grid: `grid-cols-[auto_auto_1fr_56px_auto_180px_200px_auto]`
   - Lines 419-425: Desktop CapacityMeter (height=48, width=6, fallback active=0/max=3)
   - Lines 533-539: Tablet CapacityMeter (height=28, width=10, conditional on slot data)
   - Lines 908-910: `getSlotStats()` helper

3. **`src/frontend/src/components/CapacityMeter.vue`** (58 lines total):
   - Vertical segmented bar with `flex-col-reverse gap-px`
   - Props: `active`, `max`, `height`, `width`
   - Color: green (<50%) / yellow (50-79%) / orange (80-99%) / red (100% with capacity-pulse animation)

### Testing Steps

#### Autonomy Toggle
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Load `/agents` page | See AUTO/Manual label on each agent card |
| 2 | Click toggle on agent | Toggle animates, label changes, API called |
| 3 | Verify in DB | Agent's `autonomy_enabled` field updated |
| 4 | Disable autonomy | All schedules for agent are paused |
| 5 | Re-enable | Schedules resume |

#### Execution Stats
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | View agent with tasks | See "X tasks · Y% · $Z · Nm ago" |
| 2 | View agent without tasks | See "No tasks (24h)" |
| 3 | Verify success rate colors | Green ≥80%, yellow 50-79%, red <50% |
| 4 | Wait 10 seconds | Stats auto-refresh |

#### Success Rate Bar
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | View agent with 24h executions | Success bar filled, percentage shown, 7d rate in parentheses if available |
| 2 | View agent with 7d-only executions | Bar shows 7d rate with "(7d)" label |
| 3 | View agent with no recent executions | Gray empty bar with em-dash |
| 4 | Check color changes | Green (>=90%) / yellow (50-89%) / red (<50%) |

### Related Components

- **AgentNode.vue**: Dashboard tiles use same visual design
  - Path: `src/frontend/src/components/AgentNode.vue`
  - Lines 52-97: Autonomy toggle (identical implementation)
  - Lines 130-147: Execution stats row
  - Lines 115-128: Context progress bar

---

## Enhancement: System Agent Display (2026-01-13)

### Overview

System agent (`trinity-system`) is now visible on the Agents page for admin users only. Previously, the system agent had a dedicated `/system-agent` page with `SystemAgent.vue`. This has been consolidated - the system agent now uses the standard agent card on the Agents page and `AgentDetail.vue` for the detail view.

### Changes Made

#### Store Changes (`src/frontend/src/stores/agents.js`)

| Getter/Function | Lines | Purpose |
|-----------------|-------|---------|
| `systemAgent` | 25-27 | Returns the agent with `is_system: true` or null |
| `sortedAgentsWithSystem` | 39-41 | Returns sorted agents with system agent pinned at top |
| `_getSortedAgents(includeSystem)` | 43-76 | Helper that conditionally includes system agent |

**Implementation:**
```javascript
// Line 25-27: systemAgent getter
systemAgent() {
  return this.agents.find(agent => agent.is_system) || null
}

// Line 39-41: sortedAgentsWithSystem getter
sortedAgentsWithSystem() {
  return this._getSortedAgents(true)
}

// Lines 70-74: Pin system agent at top
if (includeSystem && this.systemAgent) {
  return [this.systemAgent, ...sorted]
}
```

#### View Changes (`src/frontend/src/views/Agents.vue`)

1. **Admin Check** (lines 271, 288-305):
   - `isAdmin` ref initialized to `false`
   - On mount, fetches `/api/users/me` to check if `role === 'admin'`
   - Used to conditionally show system agent

2. **Display Agents Computed** (lines 274-279):
   ```javascript
   const displayAgents = computed(() => {
     if (isAdmin.value) {
       return agentsStore.sortedAgentsWithSystem
     }
     return agentsStore.sortedAgents
   })
   ```

3. **System Agent Card Styling** (lines 46-57):
   ```vue
   :class="[
     'bg-white dark:bg-gray-800 rounded-xl border shadow-lg p-5',
     agent.is_system
       ? 'ring-2 ring-purple-500/50 border-purple-300 dark:border-purple-700'
       : agent.status === 'running'
         ? 'border-gray-200/60 dark:border-gray-700/50'
         : 'border-gray-200 dark:border-gray-700 opacity-75'
   ]"
   ```

4. **SYSTEM Badge** (lines 69-75):
   ```vue
   <span
     v-if="agent.is_system"
     class="ml-2 px-1.5 py-0.5 text-xs font-semibold bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-300 rounded flex-shrink-0"
   >
     SYSTEM
   </span>
   ```

#### Router Changes (`src/frontend/src/router/index.js`)

- Lines 77-81: Legacy `/system-agent` redirects to `/agents/trinity-system`
  ```javascript
  {
    path: '/system-agent',
    redirect: '/agents/trinity-system'
  }
  ```

#### NavBar Changes (`src/frontend/src/components/NavBar.vue`)

- "System" link removed from navigation bar
- System agent accessed via Agents page or direct URL `/agents/trinity-system`

### Visual Design

| Aspect | Regular Agent | System Agent |
|--------|---------------|--------------|
| Border | Gray border | Purple ring + purple border |
| Badge | (none) | "SYSTEM" badge (purple) |
| Position | Sorted by sortBy setting | Always pinned at top |
| Visibility | All users | Admin users only |

### Testing Steps

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Login as admin user | See system agent at top of Agents page |
| 2 | Login as regular user | System agent NOT visible |
| 3 | Navigate to `/system-agent` | Redirects to `/agents/trinity-system` |
| 4 | Click system agent card | Opens AgentDetail with filtered tabs |
| 5 | Verify system agent styling | Purple ring, "SYSTEM" badge visible |

---

## Enhancement: Horizontal Row Tile Layout (2026-03-03)

### Overview

Complete template rewrite from 3-column card grid to full-width horizontal row tiles (Issue #54). This is a **template-only + scoped styles change** -- no backend, store, or component changes.

### Layout Change

| Aspect | Before | After |
|--------|--------|-------|
| Container | `grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4` | `flex flex-col` row list |
| Item shape | Card with `shadow-lg`, `rounded-xl`, `p-5` | Flat row with `border-b`, `px-4 py-3` |
| System agent | `ring-2 ring-purple-500/50` purple ring | `border-l-3 border-l-purple-500` left border accent |
| Stopped agents | `opacity-75` (unchanged) | `opacity-75` (unchanged) |
| Navigation | "View Details" button | Chevron arrow (`>`) at row end |
| Type display | "Type: X" line shown | Removed |
| Schedule stats | Separate row | Merged inline with execution stats via clock icon |

### Three Responsive Breakpoints

**Desktop (lg+)** -- `Agents.vue` lines 248-426:
- Outer wrapper: `class="hidden lg:flex px-4 py-3"` containing a `flex-col flex-1 min-w-0` block for the two-row content, plus a CapacityMeter flex sibling
- Row 1 (CSS grid): `grid-cols-[auto_auto_1fr_56px_auto_180px_200px_auto]` with `gap-x-4`
- 8 columns: checkbox | status dot | name+badges | activity label (fixed 56px) | toggles | success rate bar (180px) | stats (fixed 200px, `overflow-hidden`) | chevron
- Column header row: uppercase labels (NAME, STATUS, CONTROLS, SUCCESS, STATS)
- Row 2: tag pills left-aligned under name (`pl-[3.625rem]`)
- CapacityMeter: `height=48`, `width=6`, `class="ml-1 flex-shrink-0 self-stretch"` -- uses `getSlotStats(agent.name)` with fallback `active=0, max=3` when no slot data

**Tablet (md to lg)** -- `Agents.vue` lines 428-556:
- Two-line compact layout using `flex-col`
- Line 1: checkbox, status dot, name, badges, activity label, chevron
- Line 2 (indented `pl-[3.25rem]`): toggles, success rate bar with `w-24` fixed width, CapacityMeter (`height=28`, `width=10`, conditional on `getSlotStats()`), stats summary

**Mobile (<md)** -- `Agents.vue` lines 449-514:
- Compact mini-card using `flex-col`
- Line 1: checkbox, status dot, name, badges, RunningStateToggle only, chevron
- Line 2 (indented): activity state, success rate % (text-only, no bar), task count
- System badge abbreviated to "SYS" (line 476)

### New Scoped CSS (`Agents.vue` lines 905-936)

```css
/* Custom border width for system agent accent */
.border-l-3 {
  border-left-width: 3px;
}

/* Dark hover shade between gray-700 and gray-800 */
@media (prefers-color-scheme: dark) {
  .dark\:hover\:bg-gray-750:hover {
    background-color: rgb(42, 48, 60);
  }
}
```

### Files Changed

- `src/frontend/src/views/Agents.vue` -- template and scoped styles
- `src/frontend/src/components/CapacityMeter.vue` -- new component (added for capacity meter integration)

### Files NOT Changed (for initial row tile rewrite)

- Backend endpoints -- no changes
- Toggle components (RunningStateToggle, ReadOnlyToggle, AutonomyToggle) -- no changes
- Helper functions in script section -- no changes (same functions, same logic)

---

## References

> Line numbers verified 2026-03-07
- **Agents.vue**: `src/frontend/src/views/Agents.vue` (1175 lines total)
  - Template: lines 1-675
  - Script: lines 677-1141
  - Scoped styles: lines 1143-1175
  - Import AgentAvatar: line 682
  - Import CapacityMeter: line 687
  - Desktop AgentAvatar: line 272 (inside name+badges cell)
  - Tablet AgentAvatar: line 442 (between status dot and name)
  - Mobile AgentAvatar: line 585 (between status dot and name)
  - Desktop CapacityMeter: lines 419-425 (height=48, width=6, fallback active=0/max=3)
  - Tablet CapacityMeter: lines 533-539 (height=28, width=10, conditional)
  - Desktop grid template: line 252 (`grid-cols-[auto_auto_1fr_56px_auto_180px_200px_auto]`)
  - `getSlotStats()` helper: lines 908-910
- **AgentAvatar.vue**: `src/frontend/src/components/AgentAvatar.vue` (89 lines)
  - Props: `name` (required), `avatarUrl` (optional), `size` (sm/md/lg/xl/2xl, default md)
  - Size "sm" = 24x24px (`w-6 h-6`), text `text-[10px]`
  - Shows uploaded avatar image, falls back to deterministic gradient with initials
  - Gradient derived from name hash; initials from first two parts split on space/hyphen
- **CapacityMeter.vue**: `src/frontend/src/components/CapacityMeter.vue` (58 lines)
  - Props: `active`, `max`, `height`, `width`
  - Color coding: green/yellow/orange/red by utilization, capacity-pulse animation at 100%
- **agents.js store**: `src/frontend/src/stores/agents.js` (699 lines total)
  - `slotStats` state: line 16
  - `fetchExecutionStats()`: lines 584-611 (calls `/api/agents/execution-stats?include_7d=true`)
  - `fetchSlotStats()`: lines 616-638 (calls `/api/agents/slots`)
  - `startContextPolling()`: lines 669-687 (fetches context + execution + slot stats every 5s)
  - `success_desc` sort case: lines 64-70
- **AgentNode.vue**: `src/frontend/src/components/AgentNode.vue` - Visual design reference
- **Backend agents router**: `src/backend/routers/agents.py`
  - Lines 175-177: GET /context-stats endpoint
  - Lines 180-246: GET /execution-stats endpoint (with `include_7d` param)
  - Lines 775-790: PUT /{name}/autonomy endpoint
- **Backend dual stats query**: `src/backend/db/schedules.py`
  - Lines 788-846: `get_all_agents_execution_stats_dual()` - single SQL with CASE WHEN for 24h + 7d
- **Backend database facade**: `src/backend/database.py`
  - Lines 556-558: `get_all_agents_execution_stats_dual()` facade method
- **Backend helpers**: `src/backend/services/agent_service/helpers.py`
  - Lines 83-153: `get_accessible_agents()` with batch query optimization

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-03-07 | **Agent Avatars**: Added `AgentAvatar` component (`../components/AgentAvatar.vue`, import at line 682) to all three view modes in `Agents.vue`. Desktop: line 272 (inside name+badges grid cell). Tablet: line 442 (between status dot and name link). Mobile: line 585 (between status dot and name link). All use `size="sm"` (24x24px) with `:name="agent.name" :avatar-url="agent.avatar_url"`. Shows uploaded avatar image or deterministic gradient with initials as fallback. |
| 2026-03-03 | **Capacity Meter + Fixed Grid Columns**: Added `CapacityMeter` component (from `../components/CapacityMeter.vue`) to desktop and tablet layouts. Desktop: flex sibling alongside the two-row content block (`ml-1 flex-shrink-0 self-stretch`, height=48, width=6), default fallback active=0/max=3 when no slot data. Tablet: placed between success bar and stats (height=28, width=10), conditional `v-if="getSlotStats(agent.name)"`. Added `getSlotStats()` helper reading `agentsStore.slotStats`. Fixed desktop grid column widths from `grid-cols-[auto_auto_1fr_auto_auto_180px_auto_auto]` to `grid-cols-[auto_auto_1fr_56px_auto_180px_200px_auto]` -- Activity column fixed at 56px, Stats column fixed at 200px with `overflow-hidden` to prevent layout shifts when agents have varying stats content. Store: `slotStats` state (line 16), `fetchSlotStats()` action (lines 616-638) calling `GET /api/agents/slots`, polled every 5s via `startContextPolling()`. |
| 2026-03-03 | **Success Rate Bar (Issue #60)**: Replaced context usage progress bars with success rate bars across desktop, tablet, and mobile layouts. Column header "Context" renamed to "Success", grid col 160px to 180px. Sort dropdown "Context Usage" (`context_desc`) replaced with "Success Rate" (`success_desc`), sorts by `successRate`. Removed helpers: `getContextPercent()`, `getProgressBarColor()`. Added helpers: `getSuccessBarPercent()`, `getSuccessBarColor()`, `hasSuccessData()`, `has7dOnlyStats()`, `has7dStats()`, `get7dSuccessRate()`, `get7dSuccessBarColor()`. `fetchExecutionStats()` now calls `/api/agents/execution-stats?include_7d=true`, stores `taskCount7d` and `successRate7d` per agent. Backend: added `include_7d: bool = False` param to GET /execution-stats, added `get_all_agents_execution_stats_dual()` with single SQL CASE WHEN for both 24h and 7d windows (db/schedules.py), added facade method in database.py. |
| 2026-03-03 | **Horizontal Row Tile Layout (Issue #54)**: Complete template rewrite from 3-column card grid to full-width horizontal rows. Three responsive breakpoints: desktop (9-column CSS grid), tablet (two-line compact), mobile (mini-card). Column header row on desktop. System agent changed from purple ring to `border-l-3` left accent. Chevron arrow replaces "View Details" button. Type display removed. Schedule stats merged inline. New `.border-l-3` and `dark:hover:bg-gray-750` scoped styles. Template-only change -- no store or backend modifications. |
| 2026-02-18 17:50 | **Toggle Size Consistency**: All toggles in Agents.vue now use consistent `size="sm"`: RunningStateToggle (line 245), ReadOnlyToggle (line 252), AutonomyToggle (line 259). ReadOnlyToggle no longer has `:show-label="false"` - it now shows labels like the other toggles. This matches the toggle size standardization across the system. |
| 2026-02-18 | **Read-Only Toggle + Tags Layout Fix**: Added `ReadOnlyToggle` component to toggles row (Agents.vue:248-255) between Running and Autonomy. Fixed tags breaking tile layout by adding fixed height container (`h-6 overflow-hidden`, line 271) and `max-w-20 truncate` on individual tags (line 276). Added `agentReadOnlyStates` state (line 378), `readOnlyLoading` (line 377), `fetchAllReadOnlyStates()` (lines 544-563), `getAgentReadOnlyState()` (lines 540-542), `handleReadOnlyToggle()` (lines 565-594). Import at line 368. Toggle only shown for owned agents (`v-if="!agent.is_system && !agent.is_shared"`). |
| 2026-02-12 | **UI Standardization**: AutonomyToggle now uses reusable `AutonomyToggle.vue` component (151 lines) imported at Agents.vue:367. Running, ReadOnly, and Autonomy toggles positioned on same row (Agents.vue:240-263) for visual consistency with Dashboard. See [autonomy-toggle-component.md](autonomy-toggle-component.md) for component details. |
| 2026-01-26 | **Unified Start/Stop Toggle**: Replaced Start/Stop buttons with `RunningStateToggle.vue` component. |
| 2026-01-13 | **System Agent Display**: System agent now visible on Agents page for admin users only. Added `systemAgent` getter (agents.js:25-27), `sortedAgentsWithSystem` getter (agents.js:39-41), `displayAgents` computed (Agents.vue:391-403), admin check on mount (Agents.vue:427-439). System agent card has purple ring (`ring-2 ring-purple-500/50`) and "SYSTEM" badge (Agents.vue:174-183, 202-208). Legacy `/system-agent` route redirects to `/agents/trinity-system`. Removed "System" link from NavBar. |
| 2026-01-12 | **Polling interval optimization**: Context/execution stats polling changed from 5s to 10s for reduced API load. Updated all polling interval references and test cases. |
| 2026-01-12 | **Database Batch Queries (N+1 Fix)**: Added Performance section documenting `get_accessible_agents()` optimization. Now uses `db.get_all_agent_metadata()` (db/agents.py:467-529) for single JOIN query. Database queries reduced from 160-200 to 2 per request. Combined with Docker stats optimization for <50ms response. Updated Data Flow diagram. |
| 2026-01-09 | **Dashboard Parity**: Major UI overhaul - grid layout, autonomy toggle, execution stats row, context bar always visible. Added `fetchExecutionStats()` and `toggleAutonomy()` to agents.js store. |
| 2025-12-07 | Initial implementation with sorting, activity state indicators, context progress bars. |
