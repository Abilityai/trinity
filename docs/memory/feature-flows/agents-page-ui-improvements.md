# Feature: Agents Page UI Improvements

> **Status**: ✅ Implemented (2025-12-07)
> **Tested**: ✅ All features verified working
> **Last Updated**: 2025-12-19

## Overview

Enhance the Agents list page (`/agents`) with status indicators, context stats, and sorting capabilities by reusing existing APIs and components from the Collaboration Dashboard.

## Current State Analysis

### Agents Page (Agents.vue) - Current
- Simple list view with server icon
- Shows: agent name, type, port, status badge (running/stopped)
- Start/Stop buttons with loading spinners
- "Shared by X" badge for shared agents
- **Missing**: Activity state indicators, context stats, sorting

### Collaboration Dashboard (AgentNode.vue) - Already Has
- Activity state (Active/Idle/Offline) with pulsing green dot
- Context progress bar with color coding (green → yellow → orange → red)
- GitHub repo display
- Status dot with pulse animation

### Existing APIs to Reuse
| Endpoint | Purpose | Used By |
|----------|---------|---------|
| `GET /api/agents/context-stats` | Context % + activity state for all agents | network.js, agents.js |
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
- Call `GET /api/agents/context-stats` on mount and poll every 5 seconds
- Parse `activityState` field from response
- Reuse CSS animation from AgentNode.vue:
```css
.active-pulse {
  animation: active-pulse-animation 0.8s ease-in-out infinite;
  box-shadow: 0 0 8px 2px rgba(16, 185, 129, 0.6);
}
```

### 2. Context Progress Bar
**Description**: Show context window usage as a progress bar with color coding

**Visual Design**:
- Mini progress bar under agent name
- Color coding by percentage:
  - 0-49%: Green (`bg-green-500`)
  - 50-74%: Yellow (`bg-yellow-500`)
  - 75-89%: Orange (`bg-orange-500`)
  - 90-100%: Red (`bg-red-500`)
- Text: "Context: XX%"
- Only shown for running agents (hide for stopped/offline)

**Data Source**: `GET /api/agents/context-stats`
- `contextPercent`: Number (0-100)
- `contextUsed`: Number (tokens used)
- `contextMax`: Number (max tokens)

### 3. Sorting & Filtering
**Description**: Add sort controls to organize agent list

**Sort Options**:
- **Default**: Creation date (newest first) - `agent.created`
- Name (A-Z, Z-A)
- Status (running first, stopped first)
- Context usage (highest first) - needs context stats

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
  }, 5000)
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
              <option value="context_desc">Context Usage</option>
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

                        <!-- NEW: Context progress bar (only for running) -->
                        <div v-if="agent.status === 'running'" class="mt-2 max-w-xs">
                          <div class="flex items-center justify-between text-xs text-gray-500 mb-1">
                            <span>Context</span>
                            <span>{{ getContextPercent(agent.name) }}%</span>
                          </div>
                          <div class="w-full bg-gray-200 rounded-full h-1.5">
                            <div
                              :class="getProgressBarColor(agent.name)"
                              :style="{ width: getContextPercent(agent.name) + '%' }"
                              class="h-full rounded-full transition-all duration-500"
                            ></div>
                          </div>
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

// Context helpers
const getContextPercent = (agentName) => {
  const stats = agentsStore.contextStats[agentName]
  return stats ? Math.round(stats.contextPercent || 0) : 0
}

const getProgressBarColor = (agentName) => {
  const percent = getContextPercent(agentName)
  if (percent >= 90) return 'bg-red-500'
  if (percent >= 75) return 'bg-orange-500'
  if (percent >= 50) return 'bg-yellow-500'
  return 'bg-green-500'
}
```

---

## Changes Summary

### Files to Modify
1. **`src/frontend/src/stores/agents.js`**
   - Add `contextStats` state
   - Add `fetchContextStats()` actions
   - Add `startContextPolling()` and `stopContextPolling()` actions

2. **`src/frontend/src/views/Agents.vue`**
   - Add sort dropdown and sorting logic
   - Add activity state indicator (dot + label)
   - Add context progress bar for running agents
   - Add CSS for pulse animation
   - Call polling on mount/unmount

### New Code Reuse
- **API**: `GET /api/agents/context-stats` (already exists)
- **CSS**: Pulse animation from AgentNode.vue
- **Logic**: Progress bar color coding from AgentNode.vue
- **Logic**: Activity state detection from network.js

---

## Implementation Order

1. **Phase 1: Sorting**
   - Add sort dropdown
   - Implement sorting computed property
   - Default to newest first

2. **Phase 2: Context Stats**
   - Add store methods for context stats polling
   - Add activity state indicator (dot + label)
   - Add context progress bar

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

## References

- **AgentNode.vue**: `/src/frontend/src/components/AgentNode.vue` - Visual design reference
- **network.js**: `/src/frontend/src/stores/network.js:564-601` - API polling reference (fetchContextStats)
- **agents.js**: `/src/frontend/src/stores/agents.js` - Context stats fetching + polling
- **Backend endpoint**: `/src/backend/routers/agents.py:208-290` - context-stats endpoint
- **Agents.vue**: `/src/frontend/src/views/Agents.vue:1-295` - Full component with dark mode support
