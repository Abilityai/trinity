# Feature: Agent Network

> **Last Updated**: 2026-04-17 - Bulk permissions endpoint (#359): `fetchPermissionEdges()` now calls `GET /api/agents/permissions-edges` (single bulk query) instead of N per-agent calls. Backend filters edges at SQL level to accessible agents only. Reduces dashboard load from ~15s to ~3-5s with 12+ agents.
>
> **Previous (2026-04-08)** - Owner filter (#69): Added owner dropdown to Dashboard header controls. Network store gains `filterOwner` ref (persisted to localStorage) and `setFilterOwner()` action. On change, agents are filtered client-side before `convertAgentsToNodes()` — graph shows only selected owner's agents. Dropdown hidden when ≤1 distinct owner.
>
> **Previous (2026-03-20)** - Canonical edge deduplication: bidirectional agent connections now render as ONE line with arrowheads on appropriate ends, instead of TWO overlapping lines. New `getCanonicalEdgePair(x, y)` helper (line 128) returns alphabetically sorted pair. `fetchPermissionEdges()`, `createHistoricalEdges()`, `animateEdge()`, `clearEdgeAnimation()`, and `resetAllEdges()` all use canonical IDs and track `hasForward`/`hasReverse` in edge data for direction-aware `markerStart`/`markerEnd` arrowheads.
>
> **Previous (2026-03-18)** - Graph edges simplified to permission-only: `edges` computed now returns `permissionEdges.value` directly (old merge logic combining collaborationEdges + permissionEdges removed). `collaborationEdges` still exists in state and is populated by WebSocket events for live animation purposes, but is NOT rendered in the graph.
>
> **Previous (2026-03-14)** - Dashboard header compacted: agents display shortened to "N/N agents" format (running/total), removed redundant "active" collaboration indicator, added whitespace-nowrap to tag selector to prevent two-row wrapping.
>
> **Previous (2026-03-07)** - Agent Avatars in Network Graph: AgentNode now displays an `AgentAvatar` component in the node header (line 28), showing the agent's avatar image or initials-based fallback. `avatarUrl` field added to node data in `convertAgentsToNodes()` for both system agents (line 426) and regular agents (line 460) in `network.js`.
>
> **Previous (2026-03-03)** - Replace Context Bar with Success Rate Bar (Issue #60): AgentNode and SystemAgentNode now display a success rate progress bar instead of context usage bar. Dual-window stats (24h primary + 7d secondary). Backend `GET /api/agents/execution-stats` extended with `include_7d` parameter and new `get_all_agents_execution_stats_dual()` DB method.
>
> **Previous (2026-03-02)** - Dashboard Filter Persistence (FILTER-001): Time range and quick tags now persist to localStorage. See "Dashboard Filter Persistence" section in [dashboard-timeline-view.md](dashboard-timeline-view.md).
>
> **Previous (2026-02-27)** - Dashboard Timeline Refresh Fix (REFRESH-001): Added WebSocket heartbeat (ping/pong every 30s), activity status change handler, and fallback activity polling (60s). See "WebSocket Heartbeat" and "Timeline Refresh" sections.
>
> **Previous (2026-02-22)** - Dashboard schedule stats display: AgentNode now shows "X/Y schedules" with "(paused)" indicator when autonomy disabled. Backend `/api/agents/execution-stats` extended with `schedules_total` and `schedules_enabled` fields.
>
> **Previous (2026-02-12)** - UI Standardization: AutonomyToggle now uses reusable `AutonomyToggle.vue` component. Running and Autonomy toggles on same row in AgentNode.vue (lines 57-86).
>
> **Previous (2026-01-26)** - UX: Added `RunningStateToggle` to AgentNode.vue for Dashboard start/stop control. Added `toggleAgentRunning()` to network.js store.
>
> **Previous (2026-01-15)** - Timezone-aware timestamps: All timestamps now use UTC with 'Z' suffix. See [Timezone Handling Guide](/docs/TIMEZONE_HANDLING.md).

## Overview
Real-time visual dashboard that displays all agents as interactive, draggable nodes connected by animated edges that light up when agents communicate with each other. Built with Vue Flow for graph visualization. **This is the main landing page after login.**

## User Story
As a platform user, I want to see a live network graph of all agents and their communications so that I can monitor interaction patterns, identify communication hotspots, and understand the multi-agent ecosystem at a glance.

## Entry Points
- **UI**: `/` route (Dashboard) - Main landing page after authentication
- **Legacy**: `/network` route redirects to `/`
- **WebSocket**: `ws://[host]/ws` - Real-time communication events
- **API**: `GET /api/agents` - Fetch all agents for initial graph rendering

## Architecture Note (2025-12-07)

The Agent Network view was previously a separate page at `/network` (`AgentNetwork.vue`). It has been **merged into Dashboard.vue** as the primary view:

- `AgentNetwork.vue` was **deleted**
- `Dashboard.vue` now contains the complete Agent Network visualization
- `/network` route redirects to `/` (Dashboard)
- NavBar no longer has a separate "Network" link (Dashboard serves this purpose)
- "Communications" terminology changed to "messages" throughout

## Frontend Layer

### Components

#### Dashboard.vue (`src/frontend/src/views/Dashboard.vue`)
Main dashboard view component with integrated Agent Network visualization.

**Key Features** (Updated line references for Dashboard.vue - 2025-12-30):
- Lines 274-335: Vue Flow canvas with Background, Controls, and MiniMap
- Lines 97-104: Connection status indicator (green/red dot with tooltip)
- Lines 84-95: **Time Range Filter** dropdown (1h, 6h, 24h, 3d, 7d)
- Lines 112-122: Refresh button to reload agents and historical data
- Lines 124-133: Reset Layout button to clear saved positions
- Lines 8-55: **Compact Header** with inline stats (N/N agents, messages, OTel cost/tokens, host telemetry)
- Lines 63-82: **Live/Replay Mode Toggle** buttons
- Lines 337-414: **Message History Panel** (collapsible) with "Live Feed" and "Historical" sections
- Lines 138-244: **Replay Controls Panel** (visible in replay mode) with playback controls and timeline scrubber
- Lines 416-418: **ObservabilityPanel** component for OTel metrics visualization

**Dark Mode Support** (Added 2025-12-14):
- Line 2: Root container uses `dark:bg-gray-900`
- Line 8: Header uses `dark:bg-gray-800 dark:border-gray-700`
- Lines 63-82: Mode toggle buttons use dark-aware classes
- Lines 247, 283: Vue Flow canvas uses `dark:from-gray-800 dark:to-gray-900`
- Lines 353-355: History panel uses `dark:bg-gray-800 dark:border-gray-700`
- Lines 608-664: Scoped styles include `:root.dark` selectors for minimap and controls

**Lifecycle**:
- Lines 486-509: `onMounted()` - Fetches agents, loads historical communications, connects WebSocket, starts polling, initializes observability, fits view
- Lines 511-515: `onUnmounted()` - Disconnects WebSocket, stops polling on cleanup

**Event Handlers**:
- Lines 517-523: `refreshAll()` - Reloads agents and historical data, refits view
- Lines 525-528: `onTimeRangeChange()` - Updates time filter and reloads data
- Lines 530-535: `resetLayout()` - Clears localStorage positions and refits view
- Lines 537-539: `onNodeDragStop()` - Saves node positions to localStorage
- Lines 541-553: `getNodeColor()` - Maps agent status to minimap colors
- Lines 555-563: `formatTime()` - Human-readable relative timestamps (uses `parseUTC()` from `@/utils/timestamps`)
- Lines 565-605: Replay mode handlers (toggleMode, handlePlay, handlePause, handleStop, etc.)

#### AgentNode.vue (`src/frontend/src/components/AgentNode.vue`)
Custom node component for each agent (updated 2026-03-07).

**Layout** (320px wide, min 180px height):
- Lines 2-14: Clean white card with rounded corners, border, shadow, **flex flex-col** for layout. System agents get distinct purple styling.
- Lines 17-21: Top connection handle for incoming edges
- Lines 207-211: Bottom connection handle for outgoing edges

**Imports** (Lines 215-223):
- `AgentAvatar` from `./AgentAvatar.vue` (line 219) - Avatar display component
- `RuntimeBadge`, `RunningStateToggle`, `AutonomyToggle` - Other node sub-components

**Dark Mode Support** (Added 2025-12-14, updated 2026-03-03):
- Lines 10-12: Card uses `dark:bg-gray-800/80`, `dark:border-gray-700/50` (system agents: `dark:bg-purple-900/30 dark:border-purple-700/50`)
- Line 20: Handle uses `dark:!border-gray-800`
- Line 29: Agent name uses `dark:text-white`
- Lines 68-76: Activity state labels use dark-aware classes
- Lines 107-114: Success bar label uses `dark:text-gray-400`, value uses color-coded text classes
- Line 116: Progress bar background uses `dark:bg-gray-700`
- Line 191: View Details button uses `dark:bg-blue-900/30 dark:hover:bg-blue-900/50 dark:text-blue-300`

**Header Section**:
- Lines 25-55: Agent name (truncated) with AgentAvatar, RuntimeBadge, SYSTEM badge (if applicable), and status indicator dot
- Line 28: `AgentAvatar` component (`:name="data.label" :avatar-url="data.avatarUrl" size="md"`) displays agent avatar image or initials-based gradient fallback before the agent name
- Lines 47-54: Status dot with activity-based color (green for active/idle, gray for offline)
- Line 51: Pulsing animation for active agents (`active-pulse` class)

**Status Display**:
- Lines 57-66: **Running State Toggle** - for non-system agents:
  - `RunningStateToggle` component (size: sm, nodrag class)
  - Shows "Running" (green) or "Stopped" (gray)
  - Clicking calls `handleRunningToggle()` -> `networkStore.toggleAgentRunning()`
  - Loading spinner during API call
- Lines 77-85: **Autonomy Toggle** (Updated 2026-02-12) - Uses reusable `AutonomyToggle.vue` component:
  - `AutonomyToggle` component (size: sm, nodrag class)
  - Shows "AUTO" (amber) or "Manual" (gray) label
  - Clicking calls `handleAutonomyToggle()` -> `networkStore.toggleAutonomy()`
  - See [autonomy-toggle-component.md](autonomy-toggle-component.md) for component details
- **Same Row Layout**: Running and Autonomy toggles positioned on same row (lines 57-86) for visual consistency with Agents page
- Lines 88-102: GitHub repo display (if from GitHub template)

**Success Rate Bar** (Replaced Context Bar - Issue #60, 2026-03-03):
- Lines 104-130: Success rate progress bar with dual-window display (24h primary, 7d secondary)
- **Display Logic**:
  - **24h data available** (taskCount > 0): Shows 24h success rate bar + optional 7d secondary text
  - **7d-only data** (taskCount=0, taskCount7d > 0): Shows 7d success rate bar with "(7d)" label
  - **No data** (no 7d executions): Shows gray dash (mdash) placeholder
- **Color Coding**:
  - Green (`bg-green-500`): >= 90% success rate
  - Yellow (`bg-yellow-500`): 50-89% success rate
  - Red (`bg-red-500`): < 50% success rate

**Execution Stats Display** (Lines 119-136):
- Lines 120-133: Compact stats row for agents with task history:
  ```
  12 tasks · 92% · $0.45 · 2m ago
  ```
- Lines 134-136: "No tasks (24h)" placeholder for agents without recent executions

**Schedule Stats Display** (Lines 138-155):
- Lines 139-154: Schedule stats row (only shown for agents with schedules, not system agent):
  ```
  [clock icon] 2/3 schedules
  ```
- When autonomy is disabled, shows grayed "(paused)" text after schedule count
- Color changes based on autonomy state: normal gray when enabled, light gray when disabled

**Stats Row Format** (Execution Stats - Lines 132-149):
| Element | Source | Styling |
|---------|--------|---------|
| Task count | `executionStats.taskCount` | Bold gray |
| Success rate | `executionStats.successRate` | Color-coded: green (>=80%), yellow (50-80%), red (<50%) |
| Total cost | `executionStats.totalCost` | Bold gray, `$X.XX` format |
| Last run | `executionStats.lastExecutionAt` | Relative time ("2m ago") |

**Success Rate Bar Data** (used by success bar section):
| Field | Source | Description |
|-------|--------|-------------|
| `taskCount` | `executionStats.taskCount` | 24h task count |
| `successRate` | `executionStats.successRate` | 24h success rate |
| `taskCount7d` | `executionStats.taskCount7d` | 7d task count (NEW) |
| `successRate7d` | `executionStats.successRate7d` | 7d success rate (NEW) |

**Schedule Stats Row Format**:
| Element | Source | Styling |
|---------|--------|---------|
| Clock icon | Static SVG | Gray, 12x12px |
| Enabled count | `executionStats.schedulesEnabled` | Bold when autonomy on, gray when off |
| Total count | `executionStats.schedulesTotal` | Normal gray |
| Paused indicator | Shown when `!autonomyEnabled` | Italic gray "(paused)" |

**Interaction**:
- Lines 105-120: "View Details" button (for regular agents) or "System Dashboard" link (for system agents) with `nodrag` class and **mt-auto** for bottom alignment
- Lines 252-254: `viewDetails()` - Navigates to `/agents/:name` on button click

**Computed Properties** (Lines 240-398):
- `isSystemAgent` (246-248): checks if agent is a system agent
- `autonomyEnabled` (251-253): whether autonomy mode is enabled
- `activityState` (256-258): active, idle, or offline based on `data.activityState`
- `statusDotColor` (278-283): Green (#10b981) for active/idle, gray (#9ca3af) for offline
- `successBarPercent` (306-309): Rounded 24h success rate percentage
- `hasSuccessData` (311-313): True if 24h taskCount > 0
- `has7dOnly` (315-317): True if no 24h tasks but 7d tasks exist (fallback display)
- `show7dSecondary` (319-321): True if both 24h and 7d data available (shows secondary text)
- `successRate7d` (323-326): Rounded 7d success rate percentage
- `successBarColor` (328-333): Green/Yellow/Red bg class based on 24h success rate
- `successBarColorText` (335-340): Green/Yellow/Red text class for 24h percentage display
- `successBarColor7d` (342-347): Green/Yellow/Red bg class based on 7d success rate
- `successBarColorText7d` (349-354): Green/Yellow/Red text class for 7d percentage display
- `executionStats` (357-359): Execution stats object from node data
- `hasExecutionStats` (361-363): True if taskCount > 0
- `successRateColorClass` (365-371): Color class based on success rate threshold
- `lastExecutionDisplay` (373-385): Relative time string for last execution
- `schedulesTotal` (388-390): Total schedule count from `executionStats.schedulesTotal`
- `schedulesEnabled` (392-394): Enabled schedule count from `executionStats.schedulesEnabled`
- `hasSchedules` (396-398): True if `schedulesTotal > 0`

#### SystemAgentNode.vue (`src/frontend/src/components/SystemAgentNode.vue`)
Custom node component for the Trinity System Agent (updated 2026-03-03 - Issue #60).

**Layout** (400px wide, min 130px height):
- Purple gradient card with distinct styling
- Trinity icon with "Trinity System Agent / Platform Orchestrator" header
- Online/Offline status badge with pulsing dot
- **Stats row** (Lines 52-75): Success rate bar + execution stats in compact layout
- "System Dashboard" link to `/system-agent`

**Stats Row** (Lines 52-75, updated Issue #60):
- "Success" label with inline progress bar (w-20) showing `successBarPercent`
- Color-coded bar: Green (>=90%), Yellow (50-89%), Red (<50%)
- Execution stats: task count and success rate percentage

**Computed Properties** (Lines 106-145):
- `isRunning` (106-108): checks if `data.status === 'running'`
- `executionStats` (111-113): Execution stats from node data
- `hasExecutionStats` (115-117): True if taskCount > 0
- `successRateColorClass` (119-125): Color class for success rate text
- `successBarPercent` (128-131): Rounded success rate for bar width
- `successBarColor` (133-138): Green/Yellow/Red bg class based on rate
- `successBarColorText` (140-145): Green/Yellow/Red text class for percentage

**Note**: SystemAgentNode uses 24h-only success rate (no 7d secondary display), unlike AgentNode which shows dual-window data.

### State Management

#### network.js (`src/frontend/src/stores/network.js`)
Pinia store managing graph state and WebSocket communication. **Note**: Previously named `collaborations.js`, renamed to `network.js` (2025-12-07).

**State** (Lines 6-59):
- `agents` (8) - Raw agent data from API
- `nodes` (9) - Vue Flow node objects
- `collaborationEdges` (10) - Edges from collaboration history (populated by WebSocket events for animation tracking; NOT rendered in the graph)
- `permissionEdges` (11) - Edges from agent-to-agent permissions (the sole source for graph edges)
- `edges` (15) - Computed property returning `permissionEdges.value` directly; no merge logic
- `collaborationHistory` (37) - Last 100 real-time communication events (in-memory)
- `lastEventTime` (38) - Timestamp of most recent event
- `activeCollaborations` (39) - Count of currently animated edges
- `websocket` (40) - WebSocket connection instance
- `isConnected` (41) - Connection status boolean
- `nodePositions` (42) - localStorage cache
- `historicalCollaborations` (43) - Persistent data from Activity Stream API
- `totalCollaborationCount` (44) - Database count from selected time range
- `timeRangeHours` (45) - Selected filter (1, 6, 24, 72, 168)
- `isLoadingHistory` (46) - Loading indicator for historical data queries
- `contextStats` (47) - Map of agent name -> context stats
- `executionStats` (50) - Map of agent name -> execution stats (task count, success rate, cost, last run, schedule counts, **taskCount7d**, **successRate7d**)
- `contextPollingInterval` (49) - Interval ID for context polling
- `agentRefreshInterval` (50) - Interval ID for agent list refresh
- **Replay State** (52-59): `isReplayMode`, `isPlaying`, `replaySpeed`, `currentEventIndex`, `replayInterval`, `replayStartTime`, `replayElapsedMs`

**Computed** (Lines 60-105):
- `activeCollaborationCount` (61-63) - Number of animated edges (filters `edge.animated === true`)
- `lastEventTimeFormatted` (65-74) - Human-readable relative time (e.g., "2m ago")
- **Replay Computed** (76-105): `totalEvents`, `totalDuration`, `playbackPosition`, `timelineStart`, `timelineEnd`, `currentTime`

**Actions**:

##### fetchAgents() (Lines 112-125)
```javascript
await axios.get('/api/agents')
// -> convertAgentsToNodes()
// -> fetchPermissionEdges()  (called at end, after nodes are built)
```
Fetches all agents, converts to Vue Flow nodes with grid layout, then populates permission edges. Also called by `setFilterTags()` when the tag filter changes.

##### getCanonicalEdgePair() (Line 128)
```javascript
function getCanonicalEdgePair(x, y) {
  return x < y ? [x, y] : [y, x]
}
```
Returns alphabetically sorted `[a, b]` pair. Used by `fetchPermissionEdges()`, `createHistoricalEdges()`, and `animateEdge()` to ensure bidirectional connections between two agents always map to the same canonical edge ID, eliminating duplicate overlapping edges.

##### fetchPermissionEdges() (Lines 152-200)
```javascript
// Single bulk API call instead of N per-agent calls (#359)
const response = await axios.get('/api/agents/permissions-edges')
const edges = response.data.edges || []

// Collect canonical pairs via pairMap to deduplicate bidirectional permissions
edges.forEach(({ source, target }) => {
  // Filter to edges where both nodes exist in current view
  const [a, b] = getCanonicalEdgePair(source, target)
  // Track hasForward (a->b) and hasReverse (b->a) for direction arrowheads
})
permissionEdges.value = newEdges
```
Fetches all agent-to-agent permission edges in a single `GET /api/agents/permissions-edges` call. Backend filters to accessible agents at SQL level. Uses `getCanonicalEdgePair()` and a `pairMap` to collapse bidirectional permissions into a single edge with direction-appropriate arrowheads (`markerStart`/`markerEnd`).

**Edge format**:
```javascript
{
  id: `perm-${a}-${b}`,             // canonical: alphabetically sorted, e.g. "perm-alice-bob"
  source: a,                         // alphabetically first
  target: b,                         // alphabetically second
  type: 'smoothstep',
  animated: false,
  style: {
    stroke: '#94a3b8',              // Tailwind slate-400
    strokeWidth: 1.5,
    strokeDasharray: '5,3',         // Dashed pattern
    opacity: 0.7
  },
  markerEnd: pair.hasForward ? { type: 'arrowclosed', color: '#94a3b8', width: 12, height: 12 } : undefined,
  markerStart: pair.hasReverse ? { type: 'arrowclosed', color: '#94a3b8', width: 12, height: 12 } : undefined,
  data: { type: 'permission', hasForward: true, hasReverse: true }  // example: both directions permitted
}
```

**Key behaviors**:
- Only non-system agents are queried (system agents have no permission entries).
- `Promise.allSettled` means 403s (inaccessible agents) are silently skipped — only `fulfilled` results produce edges.
- Both source and target node IDs must exist in `nodes.value` before an edge is added; stale agent references are discarded.
- **Canonical edge deduplication**: Bidirectional permissions (A can call B AND B can call A) produce ONE edge with `markerEnd` (forward arrowhead) and `markerStart` (reverse arrowhead). The edge ID is `perm-{a}-{b}` where `a < b` alphabetically.
- One-way permissions produce a single edge with only `markerEnd` or `markerStart` (one arrowhead).
- `edge.data.hasForward` / `edge.data.hasReverse` track which directions are permitted, used by `clearEdgeAnimation()` and `resetAllEdges()` to restore correct arrowheads after animation.
- Re-runs on every `fetchAgents()` call (including tag-filter changes and periodic refreshes), replacing `permissionEdges.value` in full.

**`edges` computed** (Line 15):
```javascript
const edges = computed(() => permissionEdges.value)
```
The graph renders only permission-based edges. There is no merge with `collaborationEdges`. `collaborationEdges` remains in state because WebSocket `agent_collaboration` events still populate it (used for tracking `activeCollaborationCount` and the animated-edge indicator in the header), but those edges are never passed to Vue Flow's `<VueFlow :edges="...">` binding.

##### fetchHistoricalCollaborations() (Lines 202-293)
```javascript
import { getTimestampMs } from '@/utils/timestamps'

async function fetchHistoricalCollaborations(hours = null) {
  const hoursToQuery = hours || timeRangeHours.value

  // Calculate start time (X hours ago)
  const startTime = new Date()
  startTime.setHours(startTime.getHours() - hoursToQuery)

  const params = {
    activity_types: 'agent_collaboration',
    start_time: startTime.toISOString(),
    limit: 500
  }

  const response = await axios.get('/api/activities/timeline', { params })

  // Parse activities into communication events
  // Note: Timestamps from backend include 'Z' suffix for UTC
  const collaborations = response.data.activities
    .filter(activity => activity.details)
    .map(activity => ({
      source_agent: details.source_agent,
      target_agent: details.target_agent,
      timestamp: activity.started_at,  // UTC with 'Z' suffix
      activity_id: activity.id,
      status: activity.activity_state,
      duration_ms: activity.duration_ms
    }))

  historicalCollaborations.value = collaborations
  totalCollaborationCount.value = collaborations.length

  // Create initial inactive edges from historical data
  createHistoricalEdges(collaborations)
}
```

Queries Activity Stream API for communication history in selected time range. Creates gray inactive edges on graph with count labels.

##### createHistoricalEdges() (Lines 295-368)
Groups communications by canonical pair (using `getCanonicalEdgePair()`) and creates inactive edges. Bidirectional communications collapse into a single edge with direction-aware arrowheads:
```javascript
// Edge style for historical data
{
  id: `e-${a}-${b}`,          // canonical: alphabetically sorted
  source: a,
  target: b,
  animated: false,
  className: 'collaboration-edge-inactive',
  style: {
    stroke: '#cbd5e1',      // Gray
    strokeWidth: 2,
    opacity: 0.5
  },
  label: count > 1 ? `${count}x` : undefined,  // Show count
  markerEnd: hasForward ? { type: 'arrowclosed', color: '#cbd5e1', width: 15, height: 15 } : undefined,
  markerStart: hasReverse ? { type: 'arrowclosed', color: '#cbd5e1', width: 15, height: 15 } : undefined,
  data: {
    collaborationCount: count,
    lastTimestamp: lastTimestamp,
    timestamps: [...],
    hasForward: true,        // a->b communications observed
    hasReverse: true         // b->a communications observed
  }
}
```

##### convertAgentsToNodes() (Lines 370-529)
- Separates system agent from regular agents; groups regular agents by primary tag for layout
- Loads saved positions from localStorage or uses default grid
- Creates node objects with `type: 'agent'`, draggable, and status data
- **Avatar Data**: Includes `avatarUrl: agent.avatar_url || null` in node data for both system agents (line 485) and regular agents (line 518), sourced from the `GET /api/agents` response
- **Bug Fix (2025-12-09)**: Sets initial `activityState` based on agent status:
  ```javascript
  activityState: agent.status === 'running' ? 'idle' : 'offline'
  ```
  This prevents running agents from briefly showing "Offline" before the first context-stats poll completes.

##### connectWebSocket() (Lines 529-600)
WebSocket connection with auto-reconnect:
- Line 530: Constructs WebSocket URL from window.location
- Lines 535-538: `onopen` - Sets `isConnected = true`, **starts heartbeat (REFRESH-001)**
- Lines 540-554: `onmessage` - Routes events to handlers:
  - `agent_collaboration` -> `handleCollaborationEvent()`
  - `agent_status` -> `handleAgentStatusChange()`
  - `agent_deleted` -> `handleAgentDeleted()`
  - `agent_activity` -> `handleActivityStatusChange()` **(REFRESH-001)**
  - `pong` -> heartbeat response **(REFRESH-001)**
- Lines 321-332: `onclose` - Reconnects after 5 seconds, **stops heartbeat (REFRESH-001)**

##### WebSocket Heartbeat (REFRESH-001 - Added 2026-02-27)

Prevents WebSocket from silently disconnecting after 60-120s of inactivity.

**State**:
- `websocketHeartbeatInterval` (line ~53) - Interval ID for cleanup

**Functions**:
- `startWebSocketHeartbeat()` - Sends "ping" every 30 seconds
- `stopWebSocketHeartbeat()` - Clears interval on disconnect

**Backend Handler** (`src/backend/main.py:362-376`):
```python
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
```

**Flow**:
1. WebSocket opens -> `startWebSocketHeartbeat()` called
2. Every 30s: Frontend sends `"ping"`, backend responds `"pong"`
3. If pong not received, browser detects dead connection and triggers `onclose`
4. `onclose` handler stops heartbeat and schedules reconnect after 5s

##### handleCollaborationEvent() (Lines 601-627)
```javascript
// Event format: {type, source_agent, target_agent, action, timestamp}
1. Add to in-memory history (max 100 events, for real-time feed)
2. Add to historicalCollaborations (persistent database-backed list)
3. Increment totalCollaborationCount
4. Update lastEventTime
5. animateEdge(source_agent, target_agent) - converts gray edge to blue
```

**Data Merging**: Real-time WebSocket events are merged with database-backed historical data. When a new communication occurs:
- Real-time feed shows event immediately
- Edge animates blue (or updates existing gray edge)
- Event is persisted to database via activity_service
- After 6 seconds, edge fades back to gray (extended from 3s)
- Communication count increments (e.g., "5x" -> "6x")

##### animateEdge() (Lines 741-845)
Creates or updates edge with animation. Uses canonical edge IDs via `getCanonicalEdgePair()` and sets direction-appropriate arrowheads (cyan for active direction):
```javascript
const [a, b] = getCanonicalEdgePair(sourceId, targetId)
const edgeId = `e-${a}-${b}`
const isForward = sourceId === a  // a->b direction

// New edge:
{
  id: edgeId,
  source: a,
  target: b,
  type: 'smoothstep',
  animated: true,
  style: {
    stroke: 'url(#collaboration-gradient)',
    strokeWidth: 3,
    filter: 'drop-shadow(0 0 4px rgba(6, 182, 212, 0.5))'
  },
  // Only the active direction gets the cyan arrowhead:
  markerEnd: isForward ? { type: 'arrowclosed', color: '#06b6d4', width: 18, height: 18 } : undefined,
  markerStart: !isForward ? { type: 'arrowclosed', color: '#06b6d4', width: 18, height: 18 } : undefined,
  data: { hasForward: isForward, hasReverse: !isForward, ... }
}
```
- When updating an existing edge, merges direction flags (`hasForward`/`hasReverse`) and sets the active-direction arrowhead to cyan.
- Line 831: Increments `activeCollaborations`
- Lines 841-844: Sets 6-second (or 8-second extended) timeout to fade edge

##### fadeEdgeAnimation() / clearEdgeAnimation() (Lines 847-899)
Fades edge back to inactive state with direction-aware arrowhead restoration:
```javascript
{
  animated: false,
  style: { stroke: '#cbd5e1', strokeWidth: 2, opacity: 0.5 },
  // Restore arrowheads based on tracked directions (gray inactive color)
  markerEnd: edge.data?.hasForward ? { type: 'arrowclosed', color: '#cbd5e1', width: 15, height: 15 } : undefined,
  markerStart: edge.data?.hasReverse ? { type: 'arrowclosed', color: '#cbd5e1', width: 15, height: 15 } : undefined
}
```

##### saveNodePositions() / loadNodePositions() (Lines 901-920)
- Stores node positions in `localStorage` key: `trinity-collaboration-node-positions`
- Saves on every drag stop event
- Loads on initial render

##### handleAgentStatusChange() (Lines 628-641)
Updates node color when agent starts/stops.

##### handleAgentDeleted() (Lines 642-660)
Removes node and all connected edges.

##### fetchContextStats() (Lines 944-983)
- Fetches context stats from `/api/agents/context-stats`
- Updates node data with context percentage and activity state

##### fetchExecutionStats() (Lines 984-1027)
```javascript
async function fetchExecutionStats() {
  const response = await axios.get('/api/agents/execution-stats', {
    params: { include_7d: true }
  })
  const agentStats = response.data.agents

  // Update execution stats map
  const newStats = {}
  agentStats.forEach(stat => {
    newStats[stat.name] = {
      taskCount: stat.task_count_24h,
      successCount: stat.success_count,
      failedCount: stat.failed_count,
      runningCount: stat.running_count,
      successRate: stat.success_rate,
      totalCost: stat.total_cost,
      lastExecutionAt: stat.last_execution_at,
      schedulesTotal: stat.schedules_total || 0,
      schedulesEnabled: stat.schedules_enabled || 0,
      taskCount7d: stat.task_count_7d || 0,       // NEW: 7d task count
      successRate7d: stat.success_rate_7d || 0     // NEW: 7d success rate
    }
  })
  executionStats.value = newStats

  // Update node data with execution stats
  nodes.value.forEach(node => {
    const stats = newStats[node.id]
    if (stats) {
      node.data = { ...node.data, executionStats: stats }
    }
  })
}
```

**Note**: The `include_7d: true` parameter triggers the backend to use `get_all_agents_execution_stats_dual()` which returns both 24h and 7d stats in a single SQL query.

##### startContextPolling() / stopContextPolling() (Lines 1061-1090)
- Polls every 10 seconds for context stats AND execution stats
- Calls both `fetchContextStats()` and `fetchExecutionStats()` on each poll
- Automatically starts on dashboard mount, stops on unmount

##### startAgentRefresh() / stopAgentRefresh() (Lines 1091-1137)
- Polls every 15 seconds for agent list changes
- Detects new/deleted agents and updates graph

##### Timeline Activity Refresh (REFRESH-001 - Added 2026-02-27)

Fallback polling mechanism for Timeline view when WebSocket events are missed.

**State**:
- `activityRefreshInterval` (line ~54) - Interval ID for cleanup

**Functions**:
- `startActivityRefresh()` - Polls activities every 60 seconds as fallback
- `stopActivityRefresh()` - Clears interval when leaving timeline mode
- `handleActivityStatusChange()` - Handles `agent_activity` WebSocket events

**Integration with setViewMode()**:
```javascript
function setViewMode(mode) {
  viewMode.value = mode
  localStorage.setItem('trinity-dashboard-view', mode)

  if (mode === 'timeline') {
    startActivityRefresh()  // Start polling when entering timeline
  } else {
    stopActivityRefresh()   // Stop polling when leaving timeline
  }
}
```

**Dashboard Lifecycle** (`Dashboard.vue`):
- `onMounted()` - starts activity refresh if already in timeline mode
- `onUnmounted()` - stops activity refresh

**Activity Event Handler**:
```javascript
function handleActivityStatusChange(event) {
  // Refresh timeline when activity completes
  if (event.activity_state === 'completed') {
    fetchHistoricalCollaborations()
  }
}
```

##### toggleAutonomy() (Lines 1498-1536)
```javascript
async function toggleAutonomy(agentName) {
  const node = nodes.value.find(n => n.id === agentName)
  const newState = !node.data.autonomy_enabled

  const response = await axios.put(`/api/agents/${agentName}/autonomy`, { enabled: newState })
  node.data.autonomy_enabled = newState  // Update reactively

  return { success: true, enabled: newState, schedulesUpdated: response.data.schedules_updated }
}
```
Toggles autonomy mode for an agent and updates the node data reactively.

##### toggleAgentRunning() (Lines 1551-1595) - NEW 2026-01-26
```javascript
async function toggleAgentRunning(agentName) {
  const node = nodes.value.find(n => n.id === agentName)
  if (!node) return { success: false, error: 'Agent not found' }

  const isRunning = node.data.status === 'running'
  runningToggleLoading.value[agentName] = true

  try {
    if (isRunning) {
      await axios.post(`/api/agents/${agentName}/stop`, {}, { headers: ... })
      node.data.status = 'stopped'
      node.data.activityState = 'offline'
    } else {
      await axios.post(`/api/agents/${agentName}/start`, {}, { headers: ... })
      node.data.status = 'running'
      node.data.activityState = 'idle'
    }
    return { success: true, status: node.data.status }
  } finally {
    runningToggleLoading.value[agentName] = false
  }
}
```
Toggles running state (start/stop) for an agent from the Dashboard. Updates node status and activity state reactively.

##### isTogglingRunning() (Lines 1596-1598) - NEW 2026-01-26
```javascript
function isTogglingRunning(agentName) {
  return runningToggleLoading.value[agentName] || false
}
```
Helper to check if an agent is currently toggling running state (for loading UI).

##### Replay Mode Functions (Lines 1188-1497)
- `setReplayMode()` (1188-1191): Toggle between live and replay mode
- `startReplay()` (1192-1219): Begin playback from current position
- `pauseReplay()` (1220-1228): Pause at current event
- `stopReplay()` (1229-1261): Reset to beginning
- `setReplaySpeed()` (1262-1275): Change playback speed (1x-50x)
- `scheduleNextEvent()` (1353-1395): Schedule next event with time-compressed delay
- `jumpToTime()` / `jumpToEvent()` (1396-1435): Seek to specific point
- `resetAllEdges()` (1437-1468): Set all edges to inactive state with **direction-aware arrowhead restoration** (restores `markerEnd`/`markerStart` based on `edge.data.hasForward`/`hasReverse`)
- `getEventPosition()` / `handleTimelineClick()` (1470-1497): Timeline scrubber support

### Dependencies

**Vue Flow Packages** (`src/frontend/package.json`):
- `@vue-flow/core`: ^1.48.0 - Main graph library
- `@vue-flow/background`: ^1.3.2 - Dot grid background
- `@vue-flow/controls`: ^1.1.3 - Zoom/pan controls
- `@vue-flow/minimap`: ^1.5.4 - Mini overview map

**Imported Styles** (`Dashboard.vue:439-442`):
```javascript
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'
import '@vue-flow/minimap/dist/style.css'
```

## Backend Layer

### WebSocket Broadcasting

#### Endpoint Configuration
**File**: `src/backend/main.py`

##### ConnectionManager Class (Lines 61-80)
```python
class ConnectionManager:
    """WebSocket connection manager for broadcasting events."""

    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except:
                pass
```

##### WebSocket Endpoint (Lines 240-268)
```python
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = None):
    """WebSocket endpoint for real-time updates."""
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
```

##### Manager Injection (Lines 86-98)
WebSocket manager injected into routers:
```python
set_agents_ws_manager(manager)
set_sharing_ws_manager(manager)
set_chat_ws_manager(manager)
set_public_links_ws_manager(manager)

# Inject trinity meta-prompt function into system agent router
set_inject_trinity_meta_prompt(inject_trinity_meta_prompt)

# Subscribe to scheduler events from Redis (dedicated scheduler publishes to scheduler:events channel)
# Backend relays events to WebSocket manager

# Set up activity service WebSocket manager
activity_service.set_websocket_manager(manager)
```

### Communication Event Detection

**File**: `src/backend/routers/chat.py`

#### set_websocket_manager() (Lines 85-88)
```python
def set_websocket_manager(manager):
    """Set WebSocket manager for broadcasting collaboration events."""
    global _websocket_manager
    _websocket_manager = manager
```

#### broadcast_collaboration_event() (Lines 91-103)
```python
from utils.helpers import utc_now_iso

async def broadcast_collaboration_event(source_agent: str, target_agent: str, action: str = "chat"):
    """Broadcast agent collaboration event to all WebSocket clients."""
    if _websocket_manager:
        event = {
            "type": "agent_collaboration",
            "source_agent": source_agent,
            "target_agent": target_agent,
            "action": action,
            "timestamp": utc_now_iso()  # UTC with 'Z' suffix
        }
        await _websocket_manager.broadcast(json.dumps(event))
```

> **Timezone Note (2026-01-15)**: All WebSocket event timestamps use `utc_now_iso()` which includes 'Z' suffix. Frontend uses `parseUTC()` from `@/utils/timestamps` to correctly parse these. See [Timezone Handling Guide](/docs/TIMEZONE_HANDLING.md).

#### Agent-to-Agent Detection (Lines 106-142)
```python
@router.post("/{name}/chat")
async def chat_with_agent(
    name: str,
    request: ChatMessageRequest,
    current_user: User = Depends(get_current_user),
    x_source_agent: Optional[str] = Header(None)  # Key detection header
):
    # Lines 127-131: Determine execution source
    if x_source_agent:
        source = ExecutionSource.AGENT
    else:
        source = ExecutionSource.USER
    # Execution queue handles the request
```

**How It Works**:
1. Agent A calls Trinity MCP tool `trinity_chat_with_agent(target_agent="B", message="...")`
2. MCP server adds `X-Source-Agent: A` header to request
3. Backend detects header presence in `/api/agents/{name}/chat` endpoint
4. Broadcasts WebSocket event to all connected clients
5. Frontend dashboard animates edge from A to B

### Context Stats Endpoint

**File**: `src/backend/routers/agents.py`

#### GET /api/agents/context-stats (Lines 134-137)
```python
@router.get("/context-stats")
async def get_agents_context_stats(current_user: User = Depends(get_current_user)):
    """Get context window stats and activity state for all accessible agents."""
    return await get_agents_context_stats_logic(current_user)
```

**Note**: Business logic moved to `src/backend/services/agent_service/stats.py` for cleaner separation.

#### GET /api/agents/execution-stats (Lines 180-246)
```python
@router.get("/execution-stats")
async def get_agents_execution_stats(
    hours: int = 24,
    include_7d: bool = False,       # NEW: dual-window stats (Issue #60)
    current_user: User = Depends(get_current_user)
):
    """Get execution statistics for all accessible agents.

    Args:
        hours: Time window in hours (default: 24)
        include_7d: If true, include 7-day stats alongside 24h stats
    """
    # Get all stats from database
    if include_7d:
        all_stats = db.get_all_agents_execution_stats_dual()
    else:
        all_stats = db.get_all_agents_execution_stats(hours=hours)

    # Get schedule counts for all agents
    schedule_counts = db.get_all_agents_schedule_counts()

    # Filter to only agents the user can access
    accessible_agents = {a['name'] for a in get_accessible_agents(current_user)}

    filtered_stats = []
    for stat in all_stats:
        if stat["name"] in accessible_agents:
            agent_schedules = schedule_counts.get(stat["name"], {"total": 0, "enabled": 0})
            stat["schedules_total"] = agent_schedules["total"]
            stat["schedules_enabled"] = agent_schedules["enabled"]
            filtered_stats.append(stat)

    # Also include agents with schedules but no executions
    stats_agents = {s["name"] for s in filtered_stats}
    for agent_name in accessible_agents:
        if agent_name not in stats_agents:
            agent_schedules = schedule_counts.get(agent_name, {"total": 0, "enabled": 0})
            if agent_schedules["total"] > 0:
                empty_stat = {
                    "name": agent_name,
                    "task_count_24h": 0, "success_count": 0, "failed_count": 0,
                    "running_count": 0, "success_rate": 0, "total_cost": 0,
                    "last_execution_at": None,
                    "schedules_total": agent_schedules["total"],
                    "schedules_enabled": agent_schedules["enabled"]
                }
                if include_7d:
                    empty_stat.update({
                        "task_count_7d": 0, "success_count_7d": 0, "failed_count_7d": 0,
                        "running_count_7d": 0, "success_rate_7d": 0, "total_cost_7d": 0,
                        "last_execution_at_7d": None
                    })
                filtered_stats.append(empty_stat)

    return {"agents": filtered_stats}
```

**Query Parameters**:
- `hours`: Time window in hours (default: 24)
- `include_7d`: If true, include 7-day stats alongside 24h stats (default: false) **(NEW - Issue #60)**

**Response Format** (with `include_7d=true`):
```json
{
  "agents": [
    {
      "name": "agent-name",
      "task_count_24h": 12,
      "success_count": 11,
      "failed_count": 1,
      "running_count": 0,
      "success_rate": 91.7,
      "total_cost": 0.45,
      "last_execution_at": "2025-12-31T22:45:30.123456",
      "schedules_total": 3,
      "schedules_enabled": 2,
      "task_count_7d": 85,
      "success_count_7d": 78,
      "failed_count_7d": 7,
      "running_count_7d": 0,
      "success_rate_7d": 91.8,
      "total_cost_7d": 3.25,
      "last_execution_at_7d": "2025-12-31T22:45:30.123456"
    }
  ]
}
```

**Database Layer - Single-Window**: `src/backend/db/schedules.py:673-717`
```python
def get_all_agents_execution_stats(self, hours: int = 24) -> List[Dict]:
    """Get execution statistics for all agents."""
    cursor.execute("""
        SELECT agent_name,
            COUNT(*) as task_count,
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count,
            SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running_count,
            SUM(COALESCE(cost, 0)) as total_cost,
            MAX(started_at) as last_execution_at
        FROM schedule_executions
        WHERE started_at > datetime('now', ? || ' hours')
        GROUP BY agent_name
    """, (f"-{hours}",))
```

**Database Layer - Dual-Window (NEW - Issue #60)**: `src/backend/db/schedules.py:788-846`
```python
def get_all_agents_execution_stats_dual(self) -> List[Dict]:
    """Get execution statistics for all agents with both 24h and 7d windows.

    Single SQL query using CASE WHEN to compute both time windows efficiently.
    """
    cursor.execute("""
        SELECT agent_name,
            SUM(CASE WHEN started_at > datetime('now', '-24 hours') THEN 1 ELSE 0 END) as task_count_24h,
            SUM(CASE WHEN started_at > datetime('now', '-24 hours') AND status = 'success' THEN 1 ELSE 0 END) as success_count_24h,
            SUM(CASE WHEN started_at > datetime('now', '-24 hours') AND status = 'failed' THEN 1 ELSE 0 END) as failed_count_24h,
            SUM(CASE WHEN started_at > datetime('now', '-24 hours') AND status = 'running' THEN 1 ELSE 0 END) as running_count_24h,
            SUM(CASE WHEN started_at > datetime('now', '-24 hours') THEN COALESCE(cost, 0) ELSE 0 END) as total_cost_24h,
            MAX(CASE WHEN started_at > datetime('now', '-24 hours') THEN started_at ELSE NULL END) as last_execution_at_24h,
            COUNT(*) as task_count_7d,
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count_7d,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count_7d,
            SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running_count_7d,
            SUM(COALESCE(cost, 0)) as total_cost_7d,
            MAX(started_at) as last_execution_at_7d
        FROM schedule_executions
        WHERE started_at > datetime('now', '-168 hours')
        GROUP BY agent_name
    """)
    # Returns both 24h and 7d stats per agent in single query
```

**Schedule Counts Method**: `src/backend/db/schedules.py:719-743`
```python
def get_all_agents_schedule_counts(self) -> Dict[str, Dict[str, int]]:
    """Get schedule counts (total and enabled) for all agents."""
    cursor.execute("""
        SELECT agent_name,
            COUNT(*) as total,
            SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) as enabled
        FROM agent_schedules
        GROUP BY agent_name
    """)
```

**Delegate Methods**: `src/backend/database.py`
```python
def get_all_agents_execution_stats(self, hours: int = 24):
    """Get execution statistics for all agents."""
    return self._schedule_ops.get_all_agents_execution_stats(hours)

def get_all_agents_execution_stats_dual(self):
    """Get execution statistics for all agents with both 24h and 7d windows."""
    return self._schedule_ops.get_all_agents_execution_stats_dual()

def get_all_agents_schedule_counts(self):
    """Get schedule counts (total and enabled) for all agents."""
    return self._schedule_ops.get_all_agents_schedule_counts()
```

Returns JSON:
```json
{
  "agents": [
    {
      "name": "agent-name",
      "status": "running",
      "activityState": "active",
      "contextPercent": 45.2,
      "contextUsed": 90400,
      "contextMax": 200000,
      "lastActivityTime": "2025-12-02T20:45:30.123456"
    }
  ]
}
```

#### GET /api/agents/permissions-edges (Lines 273-291) — NEW #359
```python
@router.get("/permissions-edges")
async def get_all_permission_edges(
    current_user: User = Depends(get_current_user)
):
    """Get all permission edges for dashboard graph visualization (bulk endpoint)."""
    # Get accessible agents once (not N times)
    accessible = {a['name'] for a in get_accessible_agents(current_user)}
    # Single DB query filtered at SQL level
    edges = db.get_all_permission_edges(accessible)
    return {"edges": edges}
```

Replaces N per-agent `GET /api/agents/{name}/permissions` calls with a single bulk query. Backend filters edges to accessible agents at SQL level (security: no info leak).

**Database Layer**: `src/backend/db/permissions.py:53-88`
```python
def get_all_permission_edges(self, accessible_agents: Optional[set] = None) -> List[dict]:
    if accessible_agents:
        placeholders = ",".join("?" for _ in accessible_agents)
        cursor.execute(f"""
            SELECT source_agent, target_agent FROM agent_permissions
            WHERE source_agent IN ({placeholders}) AND target_agent IN ({placeholders})
        """, agent_list + agent_list)
    return [{"source": row["source_agent"], "target": row["target_agent"]} for row in cursor.fetchall()]
```

**Response Format**:
```json
{
  "edges": [
    {"source": "agent-a", "target": "agent-b"},
    {"source": "agent-b", "target": "agent-c"}
  ]
}
```

### Business Logic

**Communication Event Flow**:
1. Validate agent exists and is running (Lines 64-69)
2. Check for `X-Source-Agent` header (Line 55)
3. Submit to execution queue (Lines 77-86)
4. If agent source, activity is tracked for collaboration visualization
5. Persist chat message to database via execution queue
6. Log audit event

## Data Layer

### Agent List Optimization (2026-01-12)

The Dashboard loads agents via `GET /api/agents` which uses `get_accessible_agents()` from `helpers.py:83-153`.

**Performance Optimizations Applied**:

1. **Docker Stats Optimization**: Uses `list_all_agents_fast()` (docker_service.py:101-159) which extracts data ONLY from container labels, avoiding slow Docker operations.

2. **Database Batch Queries (N+1 Fix)**: Uses `db.get_all_agent_metadata(user_email)` (db/agents.py:467-529) - single JOIN query across all related tables instead of 8-10 queries per agent.

**Before/After**:
| Metric | Before | After |
|--------|--------|-------|
| Docker API calls | Full `container.attrs` per agent | Labels only |
| Database queries | 160-200 (for 20 agents) | 2 total |
| Response time | ~2-3 seconds | <50ms |

**Batch Query Returns** (per agent):
- `owner_id`, `owner_username`, `owner_email`
- `is_system`, `autonomy_enabled`, `use_platform_api_key`
- `memory_limit`, `cpu_limit`
- `github_repo`, `github_branch`
- `is_shared_with_user` (boolean)

**Note**: Orphaned agents (exist in Docker but not in DB) are only visible to admin users.

### LocalStorage Persistence

All Dashboard state that persists across page reloads:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `trinity-dashboard-time-range` | number | `24` | Time range in hours (1, 6, 24, 72, 168) |
| `trinity-dashboard-quick-tags` | JSON array | `[]` | Selected quick tag filters |
| `trinity-dashboard-view` | string | `timeline` | View mode: `graph` or `timeline` |
| `trinity-show-tag-clouds` | boolean | `true` | Tag clouds visibility in graph view |
| `trinity-network-node-positions` | JSON object | `{}` | Saved node positions (see below) |

**Node Positions Schema**:
```json
{
  "agent-name-1": { "x": 100, "y": 100 },
  "agent-name-2": { "x": 350, "y": 100 },
  "agent-name-3": { "x": 100, "y": 350 }
}
```

**Operations**:
- **Save**: On node drag stop (`network.js:234-240`)
- **Load**: On initial render (`network.js:242-250`)
- **Clear**: On "Reset Layout" button (`network.js:252-255`)

**System View Interaction**: Quick tags (`trinity-dashboard-quick-tags`) are cleared when a System View is selected, as the System View's tags take precedence. See [dashboard-timeline-view.md](dashboard-timeline-view.md) for details.

### Database Operations

#### Activity Stream Integration (2025-12-02)

**Status**: **IMPLEMENTED** - Full database-backed communication history.

**Table**: `agent_activities` (see `src/backend/database.py`)

**Schema**:
```sql
CREATE TABLE agent_activities (
    id TEXT PRIMARY KEY,                -- UUID
    agent_name TEXT NOT NULL,
    activity_type TEXT NOT NULL,        -- 'agent_collaboration'
    activity_state TEXT DEFAULT 'started',
    parent_activity_id TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms INTEGER,
    user_id INTEGER,
    triggered_by TEXT DEFAULT 'user',   -- 'agent' for communications
    details TEXT,                       -- JSON: {source_agent, target_agent, action}
    created_at TEXT NOT NULL
);
```

**API Endpoint**: `GET /api/activities/timeline`

**Query Parameters**:
- `activity_types`: "agent_collaboration"
- `start_time`: ISO 8601 timestamp (e.g., last 24h)
- `end_time`: Optional end boundary
- `limit`: Max results (default 100)

**Access Control**: Filters activities by user's accessible agents (owner, shared, admin).

**See**: [Activity Stream Communication Tracking](activity-stream-collaboration-tracking.md) for complete flow documentation.

## Side Effects

### WebSocket Broadcasts

**Event Type**: `agent_collaboration`

**Payload**:
```json
{
  "type": "agent_collaboration",
  "source_agent": "research-agent",
  "target_agent": "writing-agent",
  "action": "chat",
  "timestamp": "2025-12-01T15:30:45.123456"
}
```

**Broadcast Trigger**: Any agent-to-agent chat request with `X-Source-Agent` header

**Subscribers**: All connected WebSocket clients (all users viewing Agent Network)

### Other WebSocket Events Handled

**Event Type**: `agent_status`
```json
{
  "type": "agent_status",
  "agent_name": "my-agent",
  "status": "running"
}
```
Updates node color in real-time when agents start/stop.

**Event Type**: `agent_deleted`
```json
{
  "type": "agent_deleted",
  "agent_name": "my-agent"
}
```
Removes node and edges from graph.

### Audit Logging

**Not directly logged** - Communication events are ephemeral visualization. The underlying chat messages are logged via:
- `src/backend/routers/chat.py:124-131` - Audit log for agent chat interactions
- `event_type="agent_interaction"`, `action="chat"`

## Error Handling

| Error Case | Handling | User Feedback |
|------------|----------|---------------|
| WebSocket disconnected | Auto-reconnect after 5s | Red status indicator, "Disconnected" |
| WebSocket parse error | Log to console, skip event | Silent (continues listening) |
| Agent fetch failure | Log error, empty graph | "No agents" empty state with icon |
| localStorage quota exceeded | Catch error, continue with defaults | Silent degradation, positions not saved |
| Invalid communication event | Try/catch in onmessage handler | Silent, logged to console |
| Missing agent in graph | Edge creation skipped | Silent (waits for agent refresh) |

**Error Recovery**:
- WebSocket: Automatic reconnection with exponential backoff (5s initial delay)
- Failed broadcasts: Non-blocking, other clients still receive events
- Position persistence: Falls back to grid layout if localStorage unavailable

## Security Considerations

### Authentication
- **Route Protection**: Lines 12-15 in `src/frontend/src/router/index.js` - Dashboard route has `meta: { requiresAuth: true }`
- **Legacy Redirect**: Lines 53-57 - `/network` redirects to `/`
- **Auth Guard**: Lines 71-97 check `authStore.isAuthenticated` before access
- **JWT Required**: All API calls include JWT token from email/admin auth

### Authorization
- **Agent Visibility**: Dashboard shows all agents user has access to (via `GET /api/agents`)
- **No Direct Agent Control**: Nodes link to detail pages where proper access control enforced
- **WebSocket Events**: Broadcast to all authenticated users (no filtering by agent ownership)

### Data Privacy
- **No Sensitive Data**: Communication events only contain agent names and timestamps
- **Message Content**: Not included in communication events (only that communication occurred)
- **LocalStorage**: Only stores node positions (x, y coordinates), no credentials or personal data

### Rate Limiting
- **Not Implemented**: WebSocket broadcasts are lightweight, no throttling needed
- **Future Enhancement**: Could implement event debouncing if communication volume becomes excessive

## Performance Considerations

### Optimization Strategies
1. **Edge Cleanup**: Fades edges after 3 seconds to prevent graph clutter
2. **History Limit**: Max 100 events in `communicationHistory` array
3. **LocalStorage**: Minimal JSON payload (only node positions)
4. **Grid Layout**: O(n) calculation on agent refresh
5. **WebSocket**: Single connection per client, multiplexed events

### Scalability Limits
- **Tested**: Up to 50 agents (grid layout performs well)
- **Performance Degradation**: 100+ agents may require virtual scrolling or clustering
- **Edge Limit**: No limit, but graph becomes visually cluttered with 200+ edges

## Testing

### Prerequisites
1. **Running Services**:
   ```bash
   ./scripts/deploy/start.sh
   ```
2. **Authentication**: Valid session (email login or admin login)
3. **Test Agents**: At least 2 running agents for communication testing
4. **Browser**: Chrome/Firefox with WebSocket support

### Test Steps

#### Test Case 1: Dashboard Load and Initial Rendering
**Objective**: Verify dashboard loads with all agents displayed in grid layout

| Step | Action | Expected Result | Verification |
|------|--------|-----------------|--------------|
| 1 | Navigate to `/` (or `/network` which redirects) | Dashboard loads | NavBar shows "Dashboard" as active |
| 2 | Check compact header stats | Shows agent count, running count, messages | Inline stats visible in header |
| 3 | Verify grid layout | All agents positioned in grid | Nodes spaced 350px apart |
| 4 | Check connection status | WebSocket connected | Green dot (not pulsing), no "Disconnected" label |
| 5 | Verify minimap | Minimap shows all nodes | Bottom-right corner shows overview |
| 6 | Check Live/Replay toggle | "Live" button is active (blue) | Default mode is Live |

**Status**: Working (2025-12-07)

#### Test Case 2: Real-Time Communication Detection
**Objective**: Verify edges animate when agents communicate

| Step | Action | Expected Result | Verification |
|------|--------|-----------------|--------------|
| 1 | Open Agent A detail page | Agent A chat interface visible | URL: `/agents/agent-a` |
| 2 | Send message to Agent B via Trinity MCP | Chat executes successfully | Response received in chat |
| 3 | Return to Agent Network | Edge from A->B is blue and animated | Flowing dots visible |
| 4 | Check communication stats | "1 active" communication shown | Header updates immediately |
| 5 | Wait 3 seconds | Edge fades to gray | Animation stops, color changes |
| 6 | Check stats again | "0 active" communications | Counter decrements |

**Status**: Working (2025-12-01)

**Test Command** (from Agent A's MCP tools):
```json
{
  "tool": "trinity_chat_with_agent",
  "arguments": {
    "target_agent": "agent-b",
    "message": "Test communication from Agent A"
  }
}
```

#### Test Case 3: Node Drag and Position Persistence
**Objective**: Verify user can reposition nodes and layout is saved

| Step | Action | Expected Result | Verification |
|------|--------|-----------------|--------------|
| 1 | Drag agent node to new position | Node moves smoothly | Mouse cursor changes to move |
| 2 | Release mouse button | Node stays in new position | Position saved |
| 3 | Check localStorage | Position stored | DevTools -> Application -> localStorage |
| 4 | Refresh page | Node remains in new position | Layout persists across reloads |
| 5 | Click "Reset Layout" | Nodes return to grid | Grid spacing restored |
| 6 | Check localStorage | Positions cleared | Key removed from localStorage |

**Status**: Working (2025-12-01)

**localStorage Key**: `trinity-network-node-positions`

#### Test Case 4: Agent Status Updates
**Objective**: Verify node colors update when agent status changes

| Step | Action | Expected Result | Verification |
|------|--------|-----------------|--------------|
| 1 | Stop a running agent | Node color changes to gray | Gradient: gray-400 to gray-500 |
| 2 | Start the stopped agent | Node color changes to green | Gradient: green-500 to emerald-600 |
| 3 | Check minimap colors | Minimap node color updates | Matches main canvas |
| 4 | Verify status badge | Badge shows "running" / "stopped" | Text updates in node |

**Status**: Working (2025-12-01)

**Status Color Mapping**:
- Running: Green (`#10b981`)
- Stopped: Gray (`#9ca3af`)
- Starting: Yellow (`#f59e0b`)
- Error: Red (`#ef4444`)
- Exited: Dark Gray (`#6b7280`)

#### Test Case 5: WebSocket Reconnection
**Objective**: Verify auto-reconnect when WebSocket drops

| Step | Action | Expected Result | Verification |
|------|--------|-----------------|--------------|
| 1 | Stop backend server | Status changes to "Disconnected" | Red dot, no pulse |
| 2 | Wait 5 seconds | Reconnection attempt logged | Console: "Attempting to reconnect..." |
| 3 | Restart backend server | Connection reestablished | Green pulsing dot returns |
| 4 | Trigger communication event | Edge animates normally | WebSocket functional |

**Status**: Working (2025-12-01)

#### Test Case 6: Multiple Communications
**Objective**: Verify dashboard handles multiple simultaneous communications

| Step | Action | Expected Result | Verification |
|------|--------|-----------------|--------------|
| 1 | Agent A -> Agent B | Edge A->B animates | Blue flowing dots |
| 2 | Agent B -> Agent C | Edge B->C animates | Two edges active |
| 3 | Agent C -> Agent A | Edge C->A animates | Three edges active |
| 4 | Check active count | Shows "3 active" | Header updates |
| 5 | Check history panel | Shows 3 recent events | Bottom-right panel |
| 6 | Wait 3 seconds | All edges fade | "0 active" |

**Status**: Working (2025-12-01)

#### Test Case 7: Agent Deletion
**Objective**: Verify node and edges removed when agent deleted

| Step | Action | Expected Result | Verification |
|------|--------|-----------------|--------------|
| 1 | Create edge A->B via communication | Edge visible | Blue edge exists |
| 2 | Delete Agent B from detail page | Node B disappears | Removed from graph |
| 3 | Check edge A->B | Edge removed | No dangling edge |
| 4 | Check agent count | Decrements by 1 | Header shows new count |

**Status**: Working (2025-12-01)

#### Test Case 8: Click-Through to Agent Detail
**Objective**: Verify "View Details" button navigates correctly

| Step | Action | Expected Result | Verification |
|------|--------|-----------------|--------------|
| 1 | Click "View Details" on node | Navigates to agent page | URL: `/agents/{name}` |
| 2 | Check page load | Agent detail page opens | Tabs visible |
| 3 | Return to dashboard | Dashboard state preserved | Node positions unchanged |

**Status**: Working (2025-12-01)

**Note**: Button has `nodrag` class to prevent drag interference.

### Edge Cases

#### Empty State
- **Scenario**: No agents exist
- **Expected**: Empty state with icon and "Create an agent to get started"
- **Location**: `AgentNetwork.vue:56-67`

#### Single Agent
- **Scenario**: Only one agent exists
- **Expected**: Single node displayed, no edges possible
- **Behavior**: Works correctly (grid with 1 node)

#### Large Graph (50+ Agents)
- **Scenario**: Many agents in grid
- **Expected**: Grid layout may become crowded
- **Mitigation**: Zoom out, pan, use minimap for navigation

#### Rapid Communications
- **Scenario**: Multiple communications within 3-second window
- **Expected**: Edges remain blue until 3s after last event
- **Behavior**: Each edge has independent 3s timer

#### Test Case 9: Historical Data Load and Time Range Filter - **NEW**
**Objective**: Verify Activity Stream integration and time range filtering

| Step | Action | Expected Result | Verification |
|------|--------|-----------------|--------------|
| 1 | Create 10 communications (A->B) | 10 activity records in database | Check `agent_activities` table |
| 2 | Open Agent Network | Gray edge with "10x" label | Historical data loaded |
| 3 | Check header | Shows "10 total (24h)" | Default 24h range |
| 4 | Change filter to "Last Hour" | Counter updates to 0 (if old) | Query with 1h start_time |
| 5 | Trigger new communication | Edge animates blue, count updates | Real-time + historical merge |
| 6 | Refresh page | Gray edge persists with "11x" | Database persistence works |
| 7 | Check history panel | Shows "Live Feed" and "Historical" sections | Dual data sources |

**Status**: Working (2025-12-02)

#### Test Case 10: Access Control for Historical Data - **NEW**
**Objective**: Verify timeline API filters by user access

| Step | Action | Expected Result | Verification |
|------|--------|-----------------|--------------|
| 1 | User A views dashboard | Shows only owned/shared agents | Access control enforced |
| 2 | User B (no shared agents) views dashboard | Empty graph or limited view | Filtered results |
| 3 | Admin views dashboard | Shows all communications | Admin bypass |

**Status**: Working (2025-12-02)

### Known Issues

1. **No Pagination UI**: Timeline API supports pagination but frontend doesn't implement next/prev
2. **No Export**: Historical data can't be exported (CSV, JSON)
3. **No Message Filtering**: Can't filter by specific source/target agent in UI
4. **Grid Layout Only**: No auto-layout algorithms (force-directed, hierarchical)
5. **No Analytics**: No aggregation charts or heatmaps (future enhancement)

### Cleanup
- **WebSocket**: Automatically disconnected on component unmount
- **localStorage**: Preserved unless "Reset Layout" clicked
- **No Backend State**: Dashboard is stateless, no cleanup needed

## Related Flows

### Upstream Flows
- **[Agent Lifecycle](agent-lifecycle.md)**: Agents must exist and be running to appear in graph
- **[Agent-to-Agent Communication](agent-to-agent-collaboration.md)**: Trinity MCP `trinity_chat_with_agent` tool triggers communication events
- **[Email Authentication](email-authentication.md)**: User must be authenticated to access dashboard
- **[Activity Stream Communication Tracking](activity-stream-collaboration-tracking.md)**: **NEW** - Complete flow from MCP to database to visualization

### Downstream Flows
- **[Agent Detail](agent-info-display.md)**: "View Details" button navigates to agent page
- **Future**: Analytics dashboard could aggregate communication data for insights

## Future Enhancements

### Planned (REQ-9.6 Extensions)
1. **Communication Analytics**: Heatmaps, most-connected agents, communication patterns
2. **Persistent History**: Database-backed communication logs with search/filter
3. **Edge Weights**: Thickness based on message count or frequency
4. **Agent Grouping**: Cluster nodes by type or project
5. **Auto-Layout**: Force-directed layout option for complex graphs
6. **Export Graph**: PNG/SVG export for documentation
7. **Communication Metrics**: Average response time, message volume charts

### Technical Debt
1. **No Unit Tests**: Add Vitest tests for store actions
2. **No E2E Tests**: Add Playwright tests for dashboard interactions
3. **Hard-coded Timeouts**: Make 3s fade timeout configurable
4. **No Error Boundaries**: Add Vue error handling for component crashes
5. **Accessibility**: Add ARIA labels and keyboard navigation

## Implementation Timeline

### Phase 1: Real-Time Visualization (2025-12-01)
- **2025-12-01 10:00**: Initial implementation started
- **2025-12-01 12:30**: Vue Flow integration complete
- **2025-12-01 14:15**: WebSocket communication detection added
- **2025-12-01 15:45**: Edge animation and fade-out working
- **2025-12-01 16:20**: Position persistence and reset layout implemented
- **2025-12-01 17:00**: Feature complete and tested
- **2025-12-01 21:35**: Bug fixes for User model authentication (added id and email fields)

### Phase 2: Activity Stream Integration (2025-12-02)
- **2025-12-02 08:00**: Activity Stream integration planning
- **2025-12-02 10:30**: Backend activity tracking added to chat router
- **2025-12-02 12:15**: Activities API router created (`/api/activities/timeline`)
- **2025-12-02 14:45**: Frontend historical data loading implemented
- **2025-12-02 15:30**: Time range filter and UI updates complete
- **2025-12-02 16:20**: Data merging (real-time + historical) working
- **2025-12-02 17:30**: Testing, documentation, and feature complete

## Bug Fixes (2025-12-01)

### Issue: User Model Missing Fields
**Problem**: Chat endpoint failed with `AttributeError: 'User' object has no attribute 'id'`

**Root Cause**:
- `src/backend/models.py` User model only had `username` and `role` fields
- Chat persistence code expected `id` and `email` fields from User object
- JWT authentication in `dependencies.py` wasn't populating these fields

**Fix Applied**:
1. Updated `src/backend/models.py` User model:
   ```python
   class User(BaseModel):
       id: int
       username: str
       email: Optional[str] = None
       role: str = "user"
   ```

2. Updated `src/backend/dependencies.py` get_current_user():
   ```python
   return User(
       id=user["id"],
       username=user["username"],
       email=user.get("email"),
       role=user["role"]
   )
   ```

3. Updated all references in `src/backend/routers/chat.py`:
   - Session creation uses `current_user.id` and `current_user.email`
   - Message persistence uses `current_user.id`
   - Access control checks use `current_user.id`

**Files Modified**:
- `src/backend/models.py:42-47`
- `src/backend/dependencies.py:79-84`
- `src/backend/routers/chat.py:67-81, 110-122, 425-429, 463-464, 505-506, 536-537`

**Testing**: Successfully demonstrated with 8 agent-to-agent communications, all WebSocket events broadcast correctly

## UI/UX Enhancements (2025-12-02)

### Context Stats Polling & Success Rate Display

**Objective**: Detect agent activity state (active/idle/offline) via context stats polling, and display success rate bars for each agent.

**Note (2026-03-03 - Issue #60)**: The visual context usage progress bar has been replaced with a success rate bar. Context stats polling (`GET /api/agents/context-stats`) still runs every 10 seconds but is now used **only for activity state detection** (active/idle/offline). The visible progress bar on AgentNode now shows execution success rate sourced from `GET /api/agents/execution-stats` with `include_7d=true`.

**Backend Implementation**:

#### Endpoint: `GET /api/agents/context-stats`
**File**: `src/backend/routers/agents.py:208-288`

```python
@router.get("/context-stats")
async def get_agents_context_stats(current_user: User = Depends(get_current_user)):
    """
    Get context window stats and activity state for all accessible agents.

    Returns: List of agent stats with context usage and active/idle/offline state
    """
```

**Note**: This endpoint was moved to line 208 (before the `/{agent_name}` catch-all route) to fix a routing bug where `/context-stats` was being matched as an agent name.

**Logic**:
1. Get all accessible agents for current user
2. For each running agent:
   - Fetch context stats from agent's internal API: `http://{container.name}:8000/api/chat/session`
   - Parse response: `context_percent`, `context_tokens`, `context_window`
   - Query recent activity from `agent_activities` table (last 60 seconds)
   - Determine activity state:
     - **Active**: Activity in last 60s with state="started"
     - **Idle**: Running but no recent activity or activity completed
     - **Offline**: Agent not running
3. Return array of stats for all agents

**Response Format**:
```json
{
  "agents": [
    {
      "name": "agent-a",
      "status": "running",
      "activityState": "active",
      "contextPercent": 45.2,
      "contextUsed": 90400,
      "contextMax": 200000,
      "lastActivityTime": "2025-12-02T20:45:30.123456"
    }
  ]
}
```

**Frontend Implementation**:

#### Context Polling Store
**File**: `src/frontend/src/stores/network.js`

**State** (Lines 20-23):
- `contextStats` (20): Map of agent name -> context stats object
- `contextPollingInterval` (21): Interval ID for cleanup
- `agentRefreshInterval` (22): Interval ID for agent list refresh

**Functions** (Lines 563-619):
```javascript
fetchContextStats()        // Fetch stats from backend, update node data (563-591)
startContextPolling()      // Start 10-second interval polling (593-607)
stopContextPolling()       // Clear interval on unmount (609-619)
```

**Polling Strategy**:
- Starts on dashboard mount
- Polls every 10 seconds for context stats
- Polls every 15 seconds for agent list refresh
- Updates node data reactively
- Stops on dashboard unmount

#### Agent Node Component
**File**: `src/frontend/src/components/AgentNode.vue`

**Visual Design** (320px wide, min 180px height):
- Clean white card with subtle shadow and backdrop blur (dark mode: gray-800)
- **AgentAvatar** (32x32px, `size="md"`) before agent name - shows avatar image or initials-based gradient fallback
- Agent name in bold gray text (clickable, links to detail page)
- RuntimeBadge + SYSTEM badge (if applicable)
- Running/Autonomy toggle row
- GitHub repo display
- **Success rate bar** (replaced context bar - Issue #60)
- Execution stats row (task count, success rate, cost, last run)
- Schedule stats row
- Resource indicators (memory, CPU)
- Status indicator dot (pulses when active)
- "View Details" button at bottom

**Success Rate Bar Color Coding** (Lines 328-333, 342-347):
```javascript
>= 90%:  Green   (bg-green-500)
50-89%:  Yellow  (bg-yellow-500)
< 50%:   Red     (bg-red-500)
No data: Gray dash (no bar rendered)
```

**Activity States**:
| State | Label | Dot Color | Pulsing |
|-------|-------|-----------|---------|
| Active | "Active" | Green (#10b981) | Yes |
| Idle | "Idle" | Green (#10b981) | No |
| Offline | "Offline" | Gray (#9ca3af) | No |

**Template Structure** (Lines 2-212):
```vue
<div class="px-5 py-4 rounded-xl border shadow-lg bg-white/80 dark:bg-gray-800/80 flex flex-col"
     style="width: 320px; min-height: 180px;">
  <!-- Agent avatar + name + RuntimeBadge + SYSTEM badge + status dot -->
  <div class="flex items-center justify-between mb-2">
    <AgentAvatar :name="data.label" :avatar-url="data.avatarUrl" size="md" class="mr-2 flex-shrink-0" />
    <div class="text-gray-900 dark:text-white font-bold truncate">{{ data.label }}</div>
    <div :style="{ backgroundColor: statusDotColor }" :class="isActive ? 'active-pulse' : ''"></div>
  </div>

  <!-- Running + Autonomy toggles row -->
  <div class="flex items-center justify-between mb-2">
    <RunningStateToggle v-if="!isSystemAgent" ... />
    <AutonomyToggle v-if="!isSystemAgent" ... />
  </div>

  <!-- GitHub repo display -->
  <div class="flex items-center space-x-1 mb-3 h-4">...</div>

  <!-- Success rate bar (replaced context bar - Issue #60) -->
  <div class="mb-3">
    <div class="flex items-center justify-between mb-1">
      <span class="text-xs text-gray-500">Success</span>
      <div class="flex items-center gap-1.5">
        <!-- 24h data: show percentage + optional 7d secondary -->
        <span v-if="hasSuccessData" :class="successBarColorText">{{ successBarPercent }}%</span>
        <span v-if="show7dSecondary" class="text-[10px]">(7d: {{ successRate7d }}%)</span>
        <!-- No data at all: show dash -->
        <span v-if="!hasSuccessData && !has7dOnly">&mdash;</span>
        <!-- 7d-only fallback -->
        <span v-if="has7dOnly" :class="successBarColorText7d">{{ successRate7d }}%</span>
        <span v-if="has7dOnly" class="text-[10px]">(7d)</span>
      </div>
    </div>
    <div class="bg-gray-200 dark:bg-gray-700 rounded-full h-2">
      <div v-if="hasSuccessData" :class="successBarColor" :style="{ width: successBarPercent + '%' }"></div>
      <div v-else-if="has7dOnly" :class="successBarColor7d" :style="{ width: successRate7d + '%' }"></div>
    </div>
  </div>

  <!-- Execution stats row: "12 tasks . 92% . $0.45 . 2m ago" -->
  <!-- Schedule stats row: "[clock] 2/3 schedules (paused)" -->
  <!-- Resource indicators: Memory / CPU -->

  <!-- View Details button -->
  <button class="nodrag ... mt-auto" @click="viewDetails">View Details</button>
</div>
```

### Activity State Detection

**Data Source**: `agent_activities` table

**Detection Logic** (backend):
```python
# Look for activity in last 60 seconds
cutoff_time = (datetime.utcnow() - timedelta(seconds=60)).isoformat()
recent_activities = db.get_agent_activities(agent_name=agent_name, limit=1)

if recent_activities:
    last_activity = recent_activities[0]
    activity_time = last_activity.get("created_at")

    if activity_time > cutoff_time:
        if last_activity.get("activity_state") == "started":
            return "active"   # Currently executing
        else:
            return "idle"     # Recently completed
    else:
        return "idle"         # No recent activity
else:
    return "idle"             # No activity recorded
```

**Activity Types Considered**:
- `chat_start` - Agent processing a chat message
- `tool_call` - Agent executing a tool
- `schedule_start` - Agent running scheduled task
- `agent_collaboration` - Agent communicating with another agent

### Dashboard Lifecycle Integration

**File**: `src/frontend/src/views/Dashboard.vue`

**Mounting** (Lines 462-482):
```javascript
onMounted(async () => {
  // Fetch agents first
  await networkStore.fetchAgents()

  // Fetch historical communication data from Activity Stream
  await networkStore.fetchHistoricalCommunications()

  // Connect WebSocket for real-time updates
  networkStore.connectWebSocket()

  // Start polling for context stats
  networkStore.startContextPolling()

  // Start polling for agent list updates
  networkStore.startAgentRefresh()

  // Fit view after initial load
  setTimeout(() => {
    fitView({ padding: 0.2, duration: 800 })
  }, 100)
})
```

**Unmounting** (Lines 484-488):
```javascript
onUnmounted(() => {
  networkStore.disconnectWebSocket()
  networkStore.stopContextPolling()
  networkStore.stopAgentRefresh()
})
```

### Performance Considerations

**Polling Frequency**: 10 seconds (context/execution stats), 15 seconds (agent list)
- Balance between freshness and API load
- 6 context requests/minute + 4 agent refresh requests/minute
- Async/await prevents request queuing

**Context Fetch Optimization**:
- 2-second timeout per agent API call
- Parallel fetches using httpx.AsyncClient
- Graceful degradation on timeout (shows 0%)
- No blocking of other endpoints

**Frontend Reactivity**:
- Vue Flow auto-updates when node data changes
- Progress bar width transitions smoothly (500ms)
- No full re-render, only affected nodes update

### Testing

**Test Case**: Success Rate Bar Display (Updated 2026-03-03 - Issue #60)
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | View agent with 24h executions | Success bar shows 24h rate with color (green/yellow/red) |
| 2 | Agent with both 24h and 7d data | Shows 24h bar + "(7d: X%)" secondary text |
| 3 | Agent with only 7d data (no 24h) | Shows 7d rate bar with "(7d)" label |
| 4 | Agent with no executions at all | Shows gray dash, no bar rendered |
| 5 | Agent with 100% success rate | Green bar, full width |
| 6 | Agent with 50-89% success rate | Yellow bar |
| 7 | Agent with < 50% success rate | Red bar |

**Test Case**: Activity State Transitions
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Agent idle | Shows "Idle" with green dot (not pulsing) |
| 2 | Send chat message | Within 5s, shows "Active" with pulsing green dot |
| 3 | Wait 60 seconds after chat completes | Returns to "Idle" |
| 4 | Stop agent | Shows "Offline" with gray dot |

**Status**: Working (2025-12-02)

**Test Case**: Execution Stats Display (Added 2026-01-01)
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | View Dashboard with agents | Agent cards show execution stats row |
| 2 | Trigger a task on an agent | Within 5s, task count increments |
| 3 | Task completes successfully | Success rate recalculates, last run updates |
| 4 | Trigger a failing task | Success rate decreases, red color if <50% |
| 5 | Check cost accumulation | Total cost sums all task costs |
| 6 | Agent with no tasks | Shows "No tasks (24h)" placeholder |

**Test Case**: Execution Stats Success Rate Colors (in stats row text)
| Success Rate | Expected Color |
|--------------|----------------|
| >= 80% | Green (`text-green-600`) |
| 50-79% | Yellow (`text-yellow-600`) |
| < 50% | Red (`text-red-600`) |

**Test Case**: Success Rate Bar Colors (in progress bar)
| Success Rate | Bar Color | Text Color |
|--------------|-----------|------------|
| >= 90% | Green (`bg-green-500`) | `text-green-600` |
| 50-89% | Yellow (`bg-yellow-500`) | `text-yellow-600` |
| < 50% | Red (`bg-red-500`) | `text-red-600` |

**Status**: Working (2026-03-03)

---

## Bug Fixes & UX Improvements

### 2025-12-02: Agent Network UX Fixes

#### Issue #1: History Panel Always Visible
**Problem**: Communication history panel permanently visible, cluttering main graph view

**Solution**: Made panel collapsible with toggle button
- Added floating button (clipboard icon) in bottom-right corner
- Panel starts closed by default
- Smooth slide-in/slide-out animations
- Button repositions when panel opens to avoid overlap
- State stored in `isHistoryPanelOpen` ref

**Files Modified**:
- `AgentNetwork.vue:295-307` - Toggle button HTML
- `AgentNetwork.vue:310-312` - Conditional panel display with `isHistoryPanelOpen`
- `AgentNetwork.vue:423` - State variable initialization

#### Issue #2: Agents Showing as Offline (Critical Bug)
**Problem**: All agents displayed "Offline" status despite being running. Context progress bars and activity state not showing.

**Root Cause**: FastAPI route ordering bug
- `/context-stats` endpoint was defined after `/{agent_name}` catch-all pattern
- FastAPI matches routes in order - `/{agent_name}` is a catch-all pattern
- Request to `/api/agents/context-stats` matched as `agent_name="context-stats"`
- Returned 404 instead of agent stats

**Solution**: Moved `/context-stats` endpoint before catch-all route
- New position: Line 208 (before `/{agent_name}` at line 289)
- Backend restarted to apply fix
- Endpoint now responds correctly with agent status, context percentage, activity state

**Files Modified**:
- `src/backend/routers/agents.py` - Moved entire endpoint

**Backend Logs Evidence**:
```
INFO: 172.28.0.6:57436 - "GET /api/agents/context-stats HTTP/1.1" 404 Not Found  # BEFORE
INFO: 172.28.0.6:57454 - "GET /api/agents/context-stats HTTP/1.1" 200 OK         # AFTER
```

**Impact**:
- Agent nodes now correctly show Active/Idle/Offline status
- Pulsing green dots for active agents
- Context progress bars display with color coding (green/yellow/orange/red)
- Activity state updates every 10 seconds via polling

#### Issue #3: Time Range Selector Purpose Unclear
**Problem**: "Time Range:" label ambiguous - users unclear what it controls

**Solution**: Improved labeling and added context
- Changed label: "Time Range:" -> "History:" with clock icon
- Added tooltip: "How far back to load communication history"
- Clarified that it filters initial historical data load (both Live and Replay modes)

**Files Modified**:
- `AgentNetwork.vue:43-62` - Updated label, added icon, added tooltip

**User Guidance**:
- "History:" clearly indicates it controls historical data scope
- Clock icon provides visual hint
- Tooltip explains exact behavior on hover
- Useful in both Live (initial load) and Replay (timeline scope) modes

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-03-07 | **Agent Avatars in Network Graph**: AgentNode.vue now displays `AgentAvatar` component (line 28) in the node header, showing agent avatar image or initials-based gradient fallback. Props: `:name="data.label" :avatar-url="data.avatarUrl" size="md"`. Import added at line 219. `avatarUrl` field added to node data in `convertAgentsToNodes()` in network.js for system agents (line 426) and regular agents (line 460), sourced from `agent.avatar_url`. |
| 2026-03-03 | **Replace Context Bar with Success Rate Bar (Issue #60)**: AgentNode.vue (lines 104-130) now displays a success rate progress bar instead of context usage bar. Dual-window display: 24h primary bar + 7d secondary text when both available; 7d-only bar when no 24h data; gray dash when no data. Color coding: Green (>=90%), Yellow (50-89%), Red (<50%). SystemAgentNode.vue (lines 52-75) updated similarly with 24h-only success bar in stats row. Removed computed properties: `contextPercentDisplay`, `showProgressBar`, `progressBarColor`. Added computed properties: `successBarPercent`, `hasSuccessData`, `has7dOnly`, `show7dSecondary`, `successRate7d`, `successBarColor`, `successBarColorText`, `successBarColor7d`, `successBarColorText7d`. network.js `fetchExecutionStats()` now calls `/api/agents/execution-stats` with `{ params: { include_7d: true } }` and stores `taskCount7d`/`successRate7d`. Backend: Added `include_7d: bool = False` parameter to `/api/agents/execution-stats` (agents.py:180-246). New `get_all_agents_execution_stats_dual()` method (db/schedules.py:788-846) computes both 24h and 7d stats in single SQL query. Facade method added to database.py:556-558. Context polling still runs for activity state detection. |
| 2026-03-02 | **Dashboard Filter Persistence (FILTER-001)**: Time range and quick tags now persist to localStorage. Added `trinity-dashboard-time-range` (number) and `trinity-dashboard-quick-tags` (JSON array) keys. Quick tags are cleared when a System View is selected. Restored on mount via `Dashboard.vue:583-593`. Updated LocalStorage Persistence table with all persisted keys. See [dashboard-timeline-view.md](dashboard-timeline-view.md) for full details. |
| 2026-02-27 | **Dashboard Timeline Refresh Fix (REFRESH-001)**: Added WebSocket heartbeat (ping/pong every 30s) to prevent silent disconnection after 60-120s idle. Backend: `main.py:362-376` ping handler. Frontend: `startWebSocketHeartbeat()`, `stopWebSocketHeartbeat()` in network.js. Added `handleActivityStatusChange()` for `agent_activity` events. Added fallback activity polling (60s) via `startActivityRefresh()`, `stopActivityRefresh()`. Dashboard.vue lifecycle updated to start/stop activity refresh. |
| 2026-02-22 | **Dashboard Schedule Stats Display**: Added schedule stats row to AgentNode.vue (lines 138-155) showing "X/Y schedules" with clock icon. Shows grayed "(paused)" text when autonomy is disabled. Backend `GET /api/agents/execution-stats` (agents.py:176-223) now includes `schedules_total` and `schedules_enabled` fields via new `db.get_all_agents_schedule_counts()` method (db/schedules.py:719-743). Frontend `fetchExecutionStats()` (network.js:747-784) extended to include schedule counts. |
| 2026-02-12 | **UI Standardization**: AutonomyToggle now uses reusable `AutonomyToggle.vue` component (imported at AgentNode.vue:189). Running and Autonomy toggles positioned on same row (AgentNode.vue:57-86) for visual consistency with Agents page. See [autonomy-toggle-component.md](autonomy-toggle-component.md). |
| 2026-01-26 | **UX: Running State Toggle on Dashboard**: Added `RunningStateToggle.vue` component to AgentNode.vue (lines 57-65) for start/stop control from Dashboard. Added `toggleAgentRunning()` (lines 1211-1250), `isTogglingRunning()` (lines 1252-1254), and `runningToggleLoading` ref to network.js store. Users can now start/stop agents directly from the Dashboard without navigating to detail page. |
| 2026-01-15 | **Timezone-aware timestamps**: All timestamps now use UTC with 'Z' suffix. Backend uses `utc_now_iso()` from `utils/helpers.py`. Frontend uses `parseUTC()` and `getTimestampMs()` from `@/utils/timestamps.js`. Added timezone notes to WebSocket events and timestamp parsing sections. See [Timezone Handling Guide](/docs/TIMEZONE_HANDLING.md). |
| 2026-01-12 | **Polling interval optimization**: Context/execution stats polling changed from 5s to 10s. Agent list refresh changed from 10s to 15s. Updated polling strategy documentation and performance considerations. |
| 2026-01-12 | **Database Batch Queries (N+1 Fix)**: `get_accessible_agents()` (helpers.py:83-153) now uses `db.get_all_agent_metadata()` (db/agents.py:467-529) - single JOIN query instead of 8-10 queries per agent. Database queries reduced from 160-200 to 2 per request. Added Agent List Optimization section to Data Layer. Orphaned agents (Docker-only) only visible to admin. |
| 2026-01-12 | **Docker Stats Optimization**: Backend agent listing now uses `list_all_agents_fast()` (docker_service.py:101-159) which extracts data ONLY from container labels, avoiding slow Docker operations. Performance: `/api/agents` reduced from ~2-3s to <50ms. Helpers.py `get_accessible_agents()` updated at line 92. |
| 2026-01-03 | **Autonomy Toggle Switch**: Added interactive toggle switch to AgentNode cards (lines 62-96). Replaces static "AUTO" badge with clickable toggle showing "AUTO/Manual" label. Toggle calls `networkStore.toggleAutonomy()` (lines 993-1030) to enable/disable all agent schedules. Amber styling when enabled, gray when disabled. |
| 2026-01-01 | **Execution Stats Display**: Added task execution metrics to AgentNode cards. New `GET /api/agents/execution-stats` endpoint (agents.py:140-161). Database aggregation in `db/schedules.py:445-489`. Frontend: `fetchExecutionStats()` in network.js:622-658, polled every 5s with context stats. AgentNode shows compact row: "12 tasks - 92% - $0.45 - 2m ago" with color-coded success rate. |
| 2025-12-19 | **Documentation Update**: Updated all line number references for Dashboard.vue, AgentNode.vue, network.js, agents.py, chat.py, and main.py. Added dark mode styling documentation. Verified store rename (collaborations.js to network.js). Updated file path references. |
| 2025-12-23 | **Workplan removal**: Removed Task DAG progress display from AgentNode.vue. Plan stats removed from network.js store. |
| 2025-12-09 | **Bug Fix - Agent Status Display**: Added initial `activityState` to node data in `convertAgentsToNodes()`. Running agents now show "Idle" immediately instead of "Offline" until first context-stats poll. |
| 2025-12-07 | **AgentNode.vue fixes**: Added `flex flex-col` to card container for consistent layout. Added `mt-auto` to "View Details" button to align at bottom. |
| 2025-12-07 | **Major refactor**: Merged AgentNetwork.vue into Dashboard.vue. Dashboard is now the main landing page at `/`. Deleted AgentNetwork.vue. Updated NavBar (removed Network link). Renamed "communications" to "messages". Consolidated header into compact single row. Renamed store from collaborations.js to network.js. |
| 2025-12-07 | Terminology refactor: Collaboration Dashboard -> Agent Network, collaborations -> communications |
| 2025-12-02 | Activity Stream integration, context monitoring, UX fixes |
| 2025-12-01 | Initial implementation with real-time visualization |

---

## References

### Code Files
- `src/frontend/src/views/Dashboard.vue` - Main view with Agent Network visualization
- `src/frontend/src/components/AgentNode.vue` - Custom Vue Flow node with AgentAvatar (line 28), success rate bar (lines 105-131), and execution stats (lines 133-150)
- `src/frontend/src/components/AgentAvatar.vue` - Reusable avatar component (avatar image with initials-based gradient fallback)
- `src/frontend/src/components/SystemAgentNode.vue` - System agent node with success rate bar in stats row (lines 52-75)
- `src/frontend/src/stores/network.js` - Pinia store for graph state, `fetchExecutionStats()` with `include_7d: true` (lines 922-963)
- `src/frontend/src/router/index.js` - Routes (/ for Dashboard, /network redirects to /)
- `src/frontend/src/components/NavBar.vue` - Navigation (Dashboard link)
- `src/backend/routers/chat.py` - WebSocket broadcast for messages
- `src/backend/routers/agents.py` - Context stats (line 175) and execution stats with `include_7d` param (line 180) endpoints
- `src/backend/db/schedules.py` - Database operations: `get_all_agents_execution_stats()` (line 673), `get_all_agents_execution_stats_dual()` (line 788)
- `src/backend/database.py` - Delegate methods for execution stats (line 552) and dual stats (line 556)
- `src/backend/main.py` - WebSocket endpoint (line 211)

### Deleted/Renamed Files
- `src/frontend/src/views/AgentNetwork.vue` - Merged into Dashboard.vue (2025-12-07)
- `src/frontend/src/stores/collaborations.js` - Renamed to network.js (2025-12-07)
- `src/frontend/src/views/AgentCollaboration.vue` - Renamed to Dashboard.vue (2025-12-07)

### Documentation
- **Requirements**: `docs/memory/requirements/core-agent.md` (§9 Agent Collaboration, REQ-9.6)
- **Vue Flow Docs**: https://vueflow.dev/
- **WebSocket API**: MDN WebSocket Documentation

### Related MCP Tools
- `trinity_chat_with_agent` - Triggers communication events
- `trinity_list_agents` - Fetches agent list for graph
- `trinity_get_agent_info` - Agent metadata for nodes
