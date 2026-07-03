# Feature Flow: Dashboard Timeline View

> **Last Updated**: 2026-03-10 (Avatar display in Timeline Tiles)
> **Status**: Implemented
> **Requirements Doc**: `docs/requirements/DASHBOARD_TIMELINE_VIEW.md`, `docs/requirements/TIMELINE_ALL_EXECUTIONS.md`, `docs/requirements/TIMELINE_SCHEDULE_MARKERS.md`

## Overview

The Dashboard offers two views for monitoring the agent fleet:
- **Graph View**: Node-based visualization showing agents as nodes with collaboration edges (VueFlow)
- **Timeline View**: Waterfall-style activity timeline with rich agent tiles and live event streaming

**Key Feature**: Views are mutually exclusive - Graph is hidden when Timeline is active.

**All Executions**: Timeline shows ALL execution types - scheduled tasks, manual tasks, and agent-triggered tasks. Collaboration events only create arrows (not boxes).

## User Journey

### Switching Views

1. User navigates to Dashboard (`/`)
2. **Timeline is the default view** for new users (no localStorage preference)
3. Toggle buttons in header: `[Graph] [Timeline]`
4. Click "Graph" to switch to Graph view if desired
5. **Graph canvas is hidden** - only Timeline shows when active
6. View preference persisted in localStorage (`trinity-dashboard-view`)

### Timeline View Features

1. **Default Zoom: Last 2 Hours**
   - On page load, zoom defaults to `timeRangeHours / 2`
   - For 24h range, zoom is 12x (shows ~2 hours of activity)
   - Auto-scrolls to "Now" position

2. **Rich Agent Tiles** (left column, 328px):
   - Layout: `flex` row with three siblings -- avatar, content column, and CapacityMeter
   - Avatar (`flex-shrink-0`, vertically centered): `AgentAvatar` at `lg` size (48px) with `border-2` ring (indigo for regular agents, purple for system agents), `shadow-sm`
   - Content column (`flex-col justify-between flex-1 min-w-0`):
     - Row 1: Agent name, system badge (SYS), status dot with active pulse, autonomy toggle
     - Row 2: Success rate bar with percentage (24h primary, 7d fallback, dash if no data)
     - Row 3: Execution stats (tasks, success rate, cost) or "No tasks", memory limit
   - CapacityMeter (`ml-1.5 flex-shrink-0 self-stretch`): vertical slot indicator, height=52 width=6, always shown, defaults to active=0/max=3 when no slot data

3. **Live Event Streaming**
   - WebSocket remains connected (unlike old Replay mode)
   - **WebSocket Heartbeat (REFRESH-001)**: Ping/pong every 30s prevents silent disconnection
   - New collaboration events appear at right edge
   - "Now" marker updates every second
   - Auto-scroll keeps latest events visible
   - **Fallback Polling (REFRESH-001)**: Activities polled every 60s as backup

4. **Timeline Grid** (right area)
   - 15-minute interval time ticks
   - Activity bars showing execution duration
   - Communication arrows between agents (only when both have boxes)
   - "Now" vertical line (green, updates continuously)

5. **Controls**
   - Zoom: +/- buttons and slider (0.5x to 20x)
   - "Active only" toggle: Hide agents with no activity
   - "Jump to Now" button: Re-enables auto-scroll when scrolled away

8. **Schedule Markers (TSM-001)**
   - Purple triangles show when enabled schedules will next run
   - Position: At `next_run_at` timestamp for each enabled schedule
   - Hover: Shows schedule name, next run time, cron expression, message preview
   - Click: Navigates to AgentDetail page with Schedules tab open

6. **NOW Marker Positioning**
   - NOW marker positioned at 90% of viewport width (not at far right edge)
   - 10% empty space to the right of NOW for visual breathing room
   - Users cannot scroll past the NOW position into the future
   - Auto-scroll positions NOW at the 90% mark

7. **Click to View Execution Details**
   - Hover over execution bar shows tooltip with type, status, duration
   - Click on execution bar opens Execution Detail page in a new tab
   - Navigation uses **Database Execution ID** (from `related_execution_id` field), not Queue ID
   - Fallback to Tasks tab if no execution ID available

## Data Flow

```
+-------------------+    +------------------+    +-------------------+
|   Dashboard.vue   |----| network.js store |----| ReplayTimeline.vue|
|   (Parent)        |    |                  |    | (Component)       |
+-------------------+    +------------------+    +-------------------+
        |                        |                        |
        |                        |                        v
        |                        |              +-------------------+
        |                        |              | Compact Agent     |
        |                        |              | Tiles (240px)     |
        |                        |              +-------------------+
        |                        v
        |              +------------------+
        |              | WebSocket        |
        |              | (Live events)    |
        |              +------------------+
        |                        |
        v                        v
+-------------------+    +------------------+
| setViewMode()     |    | contextStats     |
| - graph           |    | executionStats   |
| - timeline        |    | slotStats        |
+-------------------+    | (5s polling)     |
                         +------------------+
```

### Execution ID Handling

**Two ID Systems** (documented 2026-01-11):

| ID Type | Format | Source | Purpose |
|---------|--------|--------|---------|
| Queue Execution ID | UUID v4 | Redis queue | Internal queue management, 10-min TTL |
| Database Execution ID | `token_urlsafe(16)` | `schedule_executions.id` | API access, UI navigation, permanent |

**Timeline uses Database Execution ID** for navigation:
- Activities have `related_execution_id` field linking to `schedule_executions.id`
- Click on execution bar navigates to `/agents/{name}/executions/{database_id}`
- The `details.execution_id` in WebSocket events also contains the Database ID

### Props Passed to ReplayTimeline

| Prop | Source | Purpose |
|------|--------|---------|
| `agents` | `networkStore.agents` | Agent list with status |
| `nodes` | VueFlow nodes array | Full node data for tiles |
| `events` | `networkStore.historicalCollaborations` | Timeline events (executions + collaborations) |
| `contextStats` | `networkStore.contextStats` | Activity state detection per agent (not displayed as bar) |
| `executionStats` | `networkStore.executionStats` | Task stats per agent (includes 24h + 7d dual-window stats) |
| `isLiveMode` | Hardcoded `true` | Enables live features |
| `timeRangeHours` | `selectedTimeRange` | For default zoom calculation |
| `schedules` | `networkStore.schedules` | Enabled schedules for markers (TSM-001) |
| `slotStats` | `networkStore.slotStats` | Per-agent slot capacity `{ agentName: { max, active } }` for CapacityMeter |

### Events Emitted by ReplayTimeline

| Event | Payload | Purpose |
|-------|---------|---------|
| `toggle-autonomy` | `agentName` | Toggle autonomy mode for agent |

## Key Implementation Details

### 1. Graph Hidden in Timeline Mode

```vue
<!-- Dashboard.vue:142-167 -->
<ReplayTimeline v-if="isTimelineMode" ... />
<div v-if="!isTimelineMode" class="...">
  <!-- VueFlow Graph -->
</div>
```

### 2. Execution Events vs Collaboration Events

**Critical Distinction**: Only execution events create boxes on the timeline. Collaboration events only create arrows.

```javascript
// ReplayTimeline.vue:543-579
props.events.forEach((event, index) => {
  // Skip collaboration events for boxes - they only create arrows
  // The target agent will have its own chat_start event with triggered_by='agent'
  if (event.activity_type === 'agent_collaboration') {
    return
  }

  // Add activity box for the executing agent
  // NOTE: Use getTimestampMs() for timezone-aware parsing (handles missing 'Z' suffix)
  agentActivityMap.get(event.source_agent).push({
    time: getTimestampMs(event.timestamp),  // from @/utils/timestamps
    type: 'execution',
    activityType: event.activity_type,
    triggeredBy: event.triggered_by,
    scheduleName: event.schedule_name
  })
})
```

### 3. Source Agent Mapping (network.js)

**Key Fix**: The `source_agent` field is mapped differently for execution vs collaboration events.

```javascript
// network.js:161-171
const isCollaboration = activity.activity_type === 'agent_collaboration'

return {
  // For execution events (chat_start, schedule_start): use agent_name (the executor)
  // For collaboration events: use details.source_agent (the caller) for arrows
  source_agent: isCollaboration ? (details.source_agent || activity.agent_name) : activity.agent_name,
  target_agent: details.target_agent || null,  // null for non-collaboration events
  // ...
}
```

**Why This Matters**:
- When Agent A calls Agent B:
  - Agent A's execution shows on A's row (source_agent = A)
  - The collaboration arrow goes from A to B (source_agent = A, target_agent = B)
  - Agent B's triggered execution shows on B's row (source_agent = B, triggered_by = 'agent')

### 4. Trigger-Based Color Coding

Activity bars are colored by what triggered them, not the activity type:

| Trigger | Color | Active | Inactive | Description |
|---------|-------|--------|----------|-------------|
| Cancelled | Slate | `#94a3b8` | - | User-cancelled execution — distinct from a failure (#1332) |
| Error | Red | `#ef4444` | - | Any failed execution |
| In Progress | Amber | `#f59e0b` | - | Currently running |
| `schedule` | Purple | `#8b5cf6` | `#c4b5fd` | Scheduled executions |
| `agent` | Cyan | `#06b6d4` | `#67e8f9` | Agent-triggered (called by another agent) |
| `mcp` | Pink | `#ec4899` | `#f9a8d4` | MCP executions (via Claude Code) |
| `manual`/`user` | Green | `#22c55e` | `#86efac` | Manual task executions |
| Default | Blue | `#3b82f6` | `#93c5fd` | Unknown trigger type |

```javascript
// ReplayTimeline.vue:880-916
function getBarColor(activity) {
  if (activity.isCancelled) return '#94a3b8'  // Slate for cancelled (#1332)
  if (activity.hasError) return '#ef4444'  // Red for errors
  if (activity.isInProgress) return '#f59e0b'  // Amber for in-progress

  // Color by trigger type (not activity type)
  const triggeredBy = activity.triggeredBy
  const activityType = activity.activityType

  // Scheduled executions -> Purple
  if (activityType?.startsWith('schedule_') || triggeredBy === 'schedule') {
    return activity.active ? '#8b5cf6' : '#c4b5fd'
  }

  // Agent-triggered executions (called by another agent) -> Cyan
  if (triggeredBy === 'agent') {
    return activity.active ? '#06b6d4' : '#67e8f9'
  }

  // MCP executions (user via Claude Code MCP client) -> Pink
  if (triggeredBy === 'mcp') {
    return activity.active ? '#ec4899' : '#f9a8d4'
  }

  // Manual/user executions -> Green
  if (triggeredBy === 'manual' || triggeredBy === 'user') {
    return activity.active ? '#22c55e' : '#86efac'
  }

  // Default blue for unknown types
  return activity.active ? '#3b82f6' : '#93c5fd'
}
```

### 5. Arrow Drawing with Box Validation

Arrows are only drawn when the target agent has an execution box within 30 seconds of the collaboration event. This prevents "floating arrows" that point to nothing.

```javascript
// ReplayTimeline.vue:661-753
const communicationArrows = computed(() => {
  // Build a map of which agents have activity boxes and their time ranges
  const agentActivityTimeRanges = new Map()
  filteredAgentRows.value.forEach(row => {
    if (row.activities && row.activities.length > 0) {
      const ranges = row.activities.map(act => ({
        startTime: startTime.value + (act.x / actualGridWidth.value) * duration.value,
        endTime: startTime.value + ((act.x + act.width) / actualGridWidth.value) * duration.value
      }))
      agentActivityTimeRanges.set(row.name, ranges)
    }
  })

  return chronoEvents.map((event, chronoIndex) => {
    // Only process collaboration events (those with target_agent)
    if (!event.target_agent) return null

    const time = getTimestampMs(event.timestamp)  // Timezone-aware parsing

    // Check if target agent has an activity box near this time
    const targetRanges = agentActivityTimeRanges.get(event.target_agent)
    if (!targetRanges || targetRanges.length === 0) {
      return null  // Target has no activity boxes at all
    }

    // Find if there's a box on target agent that could be the triggered execution
    // Look for boxes that start within ~30 seconds of the collaboration event
    const toleranceMs = 30000  // 30 second window
    const hasMatchingTargetBox = targetRanges.some(range => {
      return Math.abs(range.startTime - time) < toleranceMs
    })

    if (!hasMatchingTargetBox) {
      return null  // No matching target box - don't draw a floating arrow
    }

    // ... create arrow with validated endpoints
  }).filter(a => a !== null)
})
```

### 6. Legend Display

The legend shows the four execution trigger types:

```html
<!-- ReplayTimeline.vue:68-85 -->
<div class="hidden sm:flex items-center space-x-3">
  <span title="Manual task executions">
    <span class="w-2 h-2 rounded" style="background-color: #22c55e"></span>
    <span>Manual</span>
  </span>
  <span title="MCP executions (via Claude Code)">
    <span class="w-2 h-2 rounded" style="background-color: #ec4899"></span>
    <span>MCP</span>
  </span>
  <span title="Scheduled task executions">
    <span class="w-2 h-2 rounded" style="background-color: #8b5cf6"></span>
    <span>Scheduled</span>
  </span>
  <span title="Agent-triggered executions (called by another agent)">
    <span class="w-2 h-2 rounded" style="background-color: #06b6d4"></span>
    <span>Agent-Triggered</span>
  </span>
</div>
```

### 7. Fetching All Execution Types

The timeline fetches ALL execution types from the backend:

```javascript
// network.js:127-135
const params = {
  activity_types: 'agent_collaboration,schedule_start,schedule_end,chat_start,chat_end',
  start_time: startTime.toISOString(),
  limit: 500
}
```

**Filtering Logic**:
- Keep: `agent_collaboration` - all (for arrows only)
- Keep: `schedule_start` / `schedule_end` - all
- Keep: `chat_start` / `chat_end` where `triggered_by != 'user'` (agent-initiated)
- Keep: `chat_start` / `chat_end` where `details.parallel_mode == true` (manual tasks)
- Skip: `chat_start` / `chat_end` where `triggered_by == 'user'` AND NOT `parallel_mode` (regular user chats)

### 8. Duration-Based Activity Bar Widths

Activity bars show width proportional to execution duration:

```javascript
// ReplayTimeline.vue:614-621
const minBarWidth = 12  // Minimum width for visibility
let barWidth = minBarWidth
if (effectiveDuration > 0) {
  // Convert duration to pixels: (durationMs / totalMs) * gridWidth
  barWidth = Math.max(minBarWidth, (effectiveDuration / duration.value) * actualGridWidth.value)
}
```

### 8a. Real-Time In-Progress Bar Extension

**Added 2026-01-13**: In-progress task bars now grow in real-time as the task executes.

**Implementation** (`ReplayTimeline.vue:568-612`):

1. **Store start timestamp**: Each activity stores `startTimestamp` for dynamic calculation:
```javascript
import { getTimestampMs } from '@/utils/timestamps'

const startTimestamp = getTimestampMs(event.timestamp)  // Timezone-aware
agentActivityMap.get(event.source_agent).push({
  time: startTimestamp,
  startTimestamp, // Store for dynamic duration calculation
  // ...
})
```

2. **Dynamic duration calculation**: For in-progress tasks, elapsed time is calculated from start:
```javascript
// ReplayTimeline.vue:606-612
let effectiveDuration = act.durationMs
if (act.isInProgress && act.startTimestamp) {
  // For in-progress tasks, calculate elapsed time from start
  // This will update every second as currentNow updates
  effectiveDuration = Math.max(1000, currentNow.value - act.startTimestamp)
}
```

3. **Reactive timer**: A 1-second interval updates `currentNow` ref (`ReplayTimeline.vue:371, 399`):
```javascript
const currentNow = ref(Date.now())
// Updated every second in the timer interval
currentNow.value = Date.now()
```

4. **Bar properties update** (`ReplayTimeline.vue:629, 631`):
```javascript
return {
  // ...
  durationMs: effectiveDuration, // Use effective duration for tooltips
  isEstimated: act.isEstimated && !act.isInProgress, // Not estimated if calculating live
  // ...
}
```

**Behavior**:
- On task start: Amber bar appears with minimum width (12px)
- Every second: Bar grows as `effectiveDuration` increases
- Tooltip shows live elapsed time like "In Progress - 45.3s"
- On task complete: Bar snaps to final size from actual `duration_ms`

### 9. NOW Marker at 90% Viewport Position

```javascript
// ReplayTimeline.vue:420-428
function scrollToNow() {
  const container = timelineContainer.value
  const viewportWidth = container.clientWidth - labelWidth
  const nowPosition = labelWidth + nowX.value
  const targetScroll = nowPosition - (viewportWidth * 0.9)
  container.scrollLeft = Math.max(0, targetScroll)
}
```

### 10. Future Scroll Prevention

```javascript
// ReplayTimeline.vue:435-455
function handleScroll() {
  const container = timelineContainer.value
  const viewportWidth = container.clientWidth - labelWidth
  const nowPosition = labelWidth + nowX.value
  const maxAllowedScroll = Math.max(0, nowPosition - (viewportWidth * 0.9))

  // Clamp scroll to prevent scrolling into the future
  if (container.scrollLeft > maxAllowedScroll + 10) {
    container.scrollLeft = maxAllowedScroll
  }
}
```

### 11. Future Padding for 90% Positioning

```javascript
// ReplayTimeline.vue:464-467
const actualGridWidth = computed(() => baseGridWidth * zoomLevel.value)
// In live mode, add ~11% padding after NOW so it appears at ~90% of viewport
const futurePadding = computed(() => props.isLiveMode ? Math.max(150, actualGridWidth.value * 0.11) : 0)
const timelineWidth = computed(() => labelWidth + actualGridWidth.value + futurePadding.value)
```

### 12. Enhanced Tooltips

```javascript
// ReplayTimeline.vue:870-900
function getBarTooltip(activity) {
  let prefix = ''
  const activityType = activity.activityType
  const triggeredBy = activity.triggeredBy

  if (activityType?.startsWith('schedule_') || triggeredBy === 'schedule') {
    prefix = activity.scheduleName ? `Scheduled: ${activity.scheduleName}` : 'Scheduled Task'
  } else if (triggeredBy === 'agent') {
    prefix = 'Agent-Triggered Task'
  } else if (triggeredBy === 'manual') {
    prefix = 'Manual Task'
  } else if (triggeredBy === 'user' || activityType?.startsWith('chat_')) {
    prefix = 'Task'
  } else {
    prefix = 'Execution'
  }

  // Status suffix
  let status = ''
  if (activity.hasError) status = '(Error)'
  else if (activity.isInProgress) status = '(In Progress)'

  const duration = activity.isEstimated
    ? `~${formatDuration(activity.durationMs)}`
    : formatDuration(activity.durationMs)

  return `${prefix} ${status} - ${duration}`
}
```

### 13. Success Rate Bar in Agent Tiles (Issue #60, replaces Context Bar)

Row 2 in timeline agent tiles displays a **success rate bar** instead of the previous context progress bar. The bar shows 24h success rate as primary, falling back to 7d if no 24h data exists, and a dash if no data at all.

**Template** (`ReplayTimeline.vue:164-180`):
```html
<!-- Row 2: Success rate bar (inline) -->
<div class="flex items-center gap-2">
  <div class="flex-1 bg-gray-200 dark:bg-gray-700 rounded-full h-1.5 overflow-hidden">
    <div
      v-if="getRowSuccessPercent(row) > 0"
      class="h-full rounded-full transition-all duration-500"
      :class="getSuccessBarClass(getRowSuccessPercent(row))"
      :style="{ width: getRowSuccessPercent(row) + '%' }"
    ></div>
  </div>
  <span v-if="getRowSuccessPercent(row) > 0" ...>{{ getRowSuccessPercent(row) }}%</span>
  <span v-else ...>&mdash;</span>
</div>
```

**Helpers** (`ReplayTimeline.vue:915-929`):
```javascript
// Returns 24h success rate if available, falls back to 7d, returns 0 if no data
function getRowSuccessPercent(row) {
  if (row.executionStats && row.executionStats.taskCount > 0) {
    return Math.round(row.executionStats.successRate || 0)
  }
  if (row.executionStats && (row.executionStats.taskCount7d || 0) > 0) {
    return Math.round(row.executionStats.successRate7d || 0)
  }
  return 0
}

// Color thresholds: green >= 90%, yellow 50-89%, red < 50%
function getSuccessBarClass(percent) {
  if (percent >= 90) return 'bg-green-500'
  if (percent >= 50) return 'bg-yellow-500'
  return 'bg-red-500'
}
```

**Dual-Window Execution Stats** (`network.js:922-963`):
```javascript
// fetchExecutionStats() now requests 7d stats as well
const response = await axios.get('/api/agents/execution-stats', {
  params: { include_7d: true }
})

// Stored fields include both windows:
newStats[stat.name] = {
  taskCount: stat.task_count_24h,     // 24h task count
  successRate: stat.success_rate,      // 24h success rate
  taskCount7d: stat.task_count_7d,     // 7d task count (fallback)
  successRate7d: stat.success_rate_7d, // 7d success rate (fallback)
  // ... other fields
}
```

**Note**: The `contextStats` prop is still passed to `ReplayTimeline` and used for `activityState` detection (active/idle/offline status dot), but it no longer drives a visible bar in the tile layout.

### 14. Capacity Meter in Timeline Tiles (CAPACITY-001 Phase 2)

Each agent tile includes a vertical `CapacityMeter` showing active vs max parallel execution slots. The tile div changed from `flex-col` to `flex` (horizontal row): the 3-row content sits in a `flex-col justify-between flex-1 min-w-0` wrapper, and CapacityMeter is a sibling.

**Tile Layout** (`ReplayTimeline.vue:125-220`):
```html
<!-- Outer tile: flex row -->
<div class="px-2 py-1.5 ... flex" :style="{ height: rowHeight + 'px' }">
  <!-- Left: Avatar with border ring -->
  <div class="flex-shrink-0 flex items-center mr-1.5">
    <div class="rounded-full border-2 overflow-hidden shadow-sm border-indigo-400">
      <AgentAvatar :name="row.name" :avatar-url="row.avatarUrl" size="lg" />
    </div>
  </div>

  <!-- Middle: 3-row content column -->
  <div class="flex flex-col justify-between flex-1 min-w-0">
    <!-- Row 1: Name, badges, status dot, autonomy toggle -->
    <!-- Row 2: Success rate bar -->
    <!-- Row 3: Stats -->
  </div>

  <!-- Right: Capacity meter -->
  <CapacityMeter
    :active="row.slotStats ? row.slotStats.active : 0"
    :max="row.slotStats ? row.slotStats.max : 3"
    :height="52"
    :width="6"
    class="ml-1.5 flex-shrink-0 self-stretch"
  />
</div>
```

**CapacityMeter Component** (`src/frontend/src/components/CapacityMeter.vue`):
- Renders `max` stacked cells in `flex-col-reverse` (bottom-up fill)
- Active slots filled with color based on utilization: green (<50%), yellow (50-80%), orange (80-100%), red (100%)
- At capacity (100%): cells pulse with `capacity-pulse` animation
- Tooltip: `"active/max slots"`

**Data Flow**:
1. `stores/network.js:fetchSlotStats()` calls `GET /api/agents/slots` (line ~967)
2. Response: `{ agents: { agentName: { max, active } }, timestamp }` (from `BulkSlotState` model)
3. Stored in `slotStats` ref (line ~51), also threaded onto `nodes[].data.slotStats` for Graph view
4. `fetchSlotStats()` runs on 5-second context polling interval alongside `fetchContextStats()` and `fetchExecutionStats()` (line ~1008-1014)
5. `Dashboard.vue` destructures `slotStats` from network store (line ~523) and passes as prop to `<ReplayTimeline>`  (line ~231)
6. `ReplayTimeline.vue` accepts `slotStats` prop (line ~394), maps `slotStats[agent.name]` onto each `agentRows` row as `row.slotStats` (line ~667-728)
7. Each tile reads `row.slotStats` to render CapacityMeter (line ~204-210)

**Backend Endpoint** (`src/backend/routers/agents.py:257-282`):
```python
@router.get("/slots")
async def get_all_agent_slots(current_user: User = Depends(get_current_user)):
    agent_capacities = db.get_all_agents_parallel_capacity()
    slot_service = get_slot_service()
    slot_states = await slot_service.get_all_slot_states(agent_capacities)
    return BulkSlotState(agents=slot_states, timestamp=datetime.utcnow().isoformat() + "Z")
```

**Default Behavior**: When `slotStats[agent.name]` is absent (e.g., endpoint not yet available), the meter defaults to `active=0`, `max=3`. The meter is always rendered (no `v-if`).

## Testing

### Prerequisites
- [x] Backend running at http://localhost:8000
- [x] Frontend running at http://localhost
- [x] At least 2 agents created
- [x] Some execution history (scheduled, manual, or agent-triggered)

### Test Steps

#### 1. Toggle Between Views
**Action**: Click "Graph" and "Timeline" buttons
**Expected**:
- Graph view shows VueFlow node visualization
- Timeline view shows waterfall timeline **without Graph overlay**
- Toggle state reflects current view

**Verify**:
- [x] Button highlighting updates correctly
- [x] Graph canvas completely hidden in Timeline mode
- [x] localStorage key `trinity-dashboard-view` updates

#### 2. Execution Box Color Coding
**Action**: Trigger different execution types and view in Timeline
**Expected**:
- Manual tasks show as **green** boxes
- MCP tasks show as **pink** boxes
- Scheduled tasks show as **purple** boxes
- Agent-triggered tasks show as **cyan** boxes
- Errors show as **red** boxes
- In-progress show as **amber** boxes

**Verify**:
- [x] Colors match the legend in header
- [x] Legend shows "Manual", "MCP", "Scheduled", "Agent-Triggered"
- [x] Tooltips show correct execution type (e.g., "MCP Task")

#### 3. Collaboration Arrows
**Action**: Have Agent A call Agent B via MCP
**Expected**:
- Agent A's execution appears as a box on A's row
- Cyan arrow drawn from A to B
- Agent B's triggered execution appears as cyan box on B's row
- Arrow connects the two boxes

**Verify**:
- [x] Arrow only appears if both agents have execution boxes
- [x] No "floating arrows" pointing to empty space
- [x] Arrow timestamp aligns with collaboration event

#### 4. No Floating Arrows
**Action**: Create collaboration event where target has no execution
**Expected**:
- Arrow should NOT be drawn
- Only collaboration events with matching target execution boxes get arrows

**Verify**:
- [x] 30-second tolerance window for matching
- [x] Arrows filtered out when target has no boxes
- [x] Console shows filtered collaboration count

#### 5. Source Agent Attribution
**Action**: Agent A triggers Agent B, verify both rows
**Expected**:
- A's execution box on A's row (green or purple depending on trigger)
- Arrow from A to B
- B's execution box on B's row (cyan = agent-triggered)

**Verify**:
- [x] Execution events use `activity.agent_name` as source
- [x] Collaboration events use `details.source_agent` for arrow origin
- [x] B's box shows `triggered_by: 'agent'`

#### 6. Live Event Streaming
**Action**: Trigger new executions while viewing Timeline
**Expected**:
- New execution boxes appear at right edge
- Collaboration arrows appear when applicable
- Timeline auto-scrolls to show new events

**Verify**:
- [x] WebSocket connection indicator stays green
- [x] New events appear without page refresh
- [x] Colors applied correctly to new events

#### 6a. In-Progress Bar Real-Time Extension (Added 2026-01-13)
**Action**: Start a long-running task and observe the timeline
**Expected**:
- Amber bar appears at task start with minimum width
- Bar grows every second as task continues
- Tooltip updates to show live elapsed time (e.g., "In Progress - 45.3s")
- When task completes, bar snaps to final width based on actual duration

**Verify**:
- [x] Bar width increases visibly every second
- [x] Tooltip shows accurate live elapsed time
- [x] Bar not marked as "estimated" (tilde removed) while in progress
- [x] Final size uses actual `duration_ms` from completion event

#### 7. Now Marker Positioning
**Action**: Wait and observe "Now" marker
**Expected**:
- "NOW" label positioned at 90% of viewport width
- 10% empty space visible to the right
- Green vertical line updates every second

**Verify**:
- [x] Cannot scroll past NOW into the future
- [x] "Jump to Now" button appears when scrolled away
- [x] Auto-scroll keeps NOW at 90% position

#### 8. Click to View Execution Details
**Action**: Click on an execution bar in the timeline
**Expected**:
- Opens Execution Detail page in a new browser tab
- URL is `/agents/{agent_name}/executions/{execution_id}`
- Full execution metadata, transcript, and response visible

**Verify**:
- [x] Hover shows tooltip with "(Click to open in new tab)"
- [x] Click opens new tab (not same-tab navigation)
- [x] Execution Detail page loads with correct data
- [x] For executions without ID, opens Tasks tab in new tab

#### 9. Schedule Markers (TSM-001, Added 2026-01-29)
**Action**: View timeline for agents with enabled schedules
**Expected**:
- Purple triangle markers appear at `next_run_at` positions
- Markers only visible for enabled schedules within time range
- Hover shows schedule details (name, next run, cron, message preview)
- Click navigates to AgentDetail with Schedules tab open

**Verify**:
- [ ] Markers positioned at correct `next_run_at` timestamp
- [ ] Only enabled schedules show markers
- [ ] Markers respect zoom level and time range
- [ ] Markers hidden for agents filtered by "Active only" toggle
- [ ] Tooltip shows: "Schedule: {name}\nNext Run: {time}\nCron: {expr}\nMessage: {preview}"
- [ ] Click opens `/agents/{name}?tab=schedules`

### Edge Cases
- [x] 0 agents: Empty timeline with just "Now" marker
- [x] 0 events: Timeline grid visible with all agent rows, ready for live events (fixed 2026-01-15)
- [x] Agent with no executions: Row visible but no boxes
- [x] Collaboration without target execution: Arrow not drawn
- [x] Multiple rapid collaborations: Each gets own arrow if box exists
- [x] Agent calls itself: Arrow from row to same row (if box exists)
- [ ] Agent with no schedules: No markers shown on row (TSM-001)
- [ ] Schedule with `next_run_at` outside time range: Marker not shown (TSM-001)
- [ ] Disabled schedule: Marker not shown (filtered by `enabled_only=true`) (TSM-001)

**Last Tested**: 2026-01-29
**Status**: Fully implemented and tested (TSM-001 added)

## Backend Bug Fixes

### Collaboration Activity Completion on Error (2026-01-10)

**Issue**: Collaboration activities were stuck in "started" state when chat requests failed with HTTP errors.

**Root Cause**: The HTTP error handler in `routers/chat.py` was missing the `complete_activity()` call for `collaboration_activity_id`.

**Fix Location**: `src/backend/routers/chat.py:386-392`

```python
# Complete collaboration activity on failure
if collaboration_activity_id:
    await activity_service.complete_activity(
        activity_id=collaboration_activity_id,
        status="failed",
        error=error_msg
    )
```

**Impact**: Collaboration activities now properly show "failed" status with `duration_ms` calculated.

## Timeline Refresh Mechanisms (REFRESH-001)

**Added 2026-02-27**: Three-layer approach to keep the Timeline view fresh when left idle.

### Problem Statement

The Dashboard timeline stopped refreshing when left idle because:
1. WebSocket connection silently died after 60-120s of inactivity (no heartbeat)
2. Activity completion events (`agent_activity` with `activity_state: completed`) were not handled by the frontend
3. No fallback mechanism when WebSocket was unavailable

### Solution Architecture

```
+-------------------+     +---------------------+     +-------------------+
| WebSocket         |     | Activity Status     |     | Fallback Polling  |
| Heartbeat (30s)   |     | Handler             |     | (60s)             |
+-------------------+     +---------------------+     +-------------------+
        |                         |                         |
        v                         v                         v
  Keeps connection          Updates timeline           Refreshes data
  alive via ping/pong       on activity events         even if WS fails
```

### 1. WebSocket Heartbeat (Ping/Pong)

**Backend** (`src/backend/main.py:362-376`):
```python
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # Handle ping/pong to keep connection alive
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
```

**Frontend** (`src/frontend/src/stores/network.js`):
- `websocketHeartbeatInterval` ref (line ~53)
- `startWebSocketHeartbeat()` - sends "ping" every 30 seconds
- `stopWebSocketHeartbeat()` - clears interval on disconnect

**Connection Flow**:
1. WebSocket opens -> `startWebSocketHeartbeat()` called
2. Every 30s, frontend sends `"ping"`
3. Backend responds with `"pong"`
4. If pong not received, browser detects dead connection and triggers reconnect

### 2. Activity Status Change Handler

**Frontend** (`src/frontend/src/stores/network.js`):
- `handleActivityStatusChange()` - handles `agent_activity` WebSocket events
- Triggered when backend broadcasts activity completion

**WebSocket Event Format**:
```json
{
  "type": "agent_activity",
  "agent_name": "my-agent",
  "activity_type": "chat_start",
  "activity_state": "completed",
  "details": {...}
}
```

**Handler Logic**:
1. Receive `agent_activity` event with `activity_state: completed`
2. Refresh historical collaborations to update timeline
3. Timeline re-renders with updated execution data

### 3. Fallback Activity Polling

**Frontend** (`src/frontend/src/stores/network.js`):
- `activityRefreshInterval` ref (line ~54)
- `startActivityRefresh()` - polls activities every 60 seconds
- `stopActivityRefresh()` - clears interval when leaving timeline mode

**Lifecycle Integration** (`src/frontend/src/views/Dashboard.vue`):
- `onMounted()` - starts activity refresh if in timeline mode
- `onUnmounted()` - stops activity refresh
- `setViewMode()` - starts/stops based on view mode change

**Polling Logic**:
1. Every 60 seconds while in timeline mode
2. Calls `fetchHistoricalCollaborations()` from network store
3. Timeline re-renders with fresh data from database
4. Acts as safety net if WebSocket events are missed

### Implementation Details

**Files Changed**:
| File | Changes |
|------|---------|
| `src/backend/main.py:362-376` | Added ping/pong handler to `/ws` endpoint |
| `src/frontend/src/stores/network.js:53-54` | Added `activityRefreshInterval` and `websocketHeartbeatInterval` refs |
| `src/frontend/src/stores/network.js` | Added `startWebSocketHeartbeat()`, `stopWebSocketHeartbeat()` functions |
| `src/frontend/src/stores/network.js` | Added `handleActivityStatusChange()` for `agent_activity` events |
| `src/frontend/src/stores/network.js` | Added `startActivityRefresh()`, `stopActivityRefresh()` functions |
| `src/frontend/src/stores/network.js` | Updated `connectWebSocket()` to start heartbeat on open |
| `src/frontend/src/stores/network.js` | Updated WebSocket message handler for `agent_activity` and `pong` events |
| `src/frontend/src/stores/network.js` | Updated `setViewMode()` to start/stop activity refresh based on mode |
| `src/frontend/src/stores/network.js` | Updated `disconnectWebSocket()` to stop heartbeat |
| `src/frontend/src/views/Dashboard.vue` | Updated `onMounted()` to start activity refresh if in timeline mode |
| `src/frontend/src/views/Dashboard.vue` | Updated `onUnmounted()` to stop activity refresh |

### Testing

**Test Case: WebSocket Heartbeat**
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Open Dashboard in timeline mode | WebSocket connected, heartbeat starts |
| 2 | Wait 2+ minutes | Connection stays alive (no reconnect) |
| 3 | Check browser Network tab | Ping/pong messages every 30s |

**Test Case: Activity Status Handler**
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Open Dashboard in timeline mode | Timeline displays current activities |
| 2 | Trigger execution on agent | Execution bar appears |
| 3 | Wait for completion | Bar updates to completed state without refresh |

**Test Case: Fallback Polling**
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Open Dashboard in timeline mode | Timeline displays |
| 2 | Disconnect network briefly | WebSocket disconnects |
| 3 | Wait 60 seconds | Timeline refreshes via polling |
| 4 | Reconnect network | WebSocket reconnects, heartbeat resumes |

## Dashboard Filter Persistence (FILTER-001 - Added 2026-03-02)

Dashboard filters are now persisted to localStorage and restored on page reload.

### Persisted State

| Setting | localStorage Key | Default | Description |
|---------|------------------|---------|-------------|
| Time Range | `trinity-dashboard-time-range` | `24` (hours) | Dropdown: 1h, 6h, 24h, 3d, 7d |
| Quick Tags | `trinity-dashboard-quick-tags` | `[]` (empty array) | Tag filter dropdown selection |
| View Mode | `trinity-dashboard-view` | `timeline` | Graph or Timeline toggle |
| Tag Clouds | `trinity-show-tag-clouds` | `true` | Clouds visibility toggle |
| Node Positions | `trinity-network-node-positions` | `{}` (empty object) | Dragged node positions |

**Note**: System View selection is persisted via `systemViewsStore.initialize()` and has its own localStorage key.

### Implementation Details

**File**: `src/frontend/src/views/Dashboard.vue`

**Time Range Persistence** (Lines 537-539, 649-652):
```javascript
// Load on component definition
const savedTimeRange = localStorage.getItem('trinity-dashboard-time-range')
const selectedTimeRange = ref(savedTimeRange ? parseInt(savedTimeRange) : 24)

// Save on change
async function onTimeRangeChange() {
  networkStore.timeRangeHours = selectedTimeRange.value
  localStorage.setItem('trinity-dashboard-time-range', selectedTimeRange.value)
  await networkStore.fetchHistoricalCommunications()
}
```

**Quick Tags Persistence** (Lines 543-546, 557-561, 764-765, 771-772):
```javascript
// Load on component definition
const savedQuickTags = localStorage.getItem('trinity-dashboard-quick-tags')
const selectedQuickTags = ref(savedQuickTags ? JSON.parse(savedQuickTags) : [])

// Save on toggle
function toggleQuickTag(tag) {
  // ... toggle logic ...
  systemViewsStore.clearSelection()  // Clear system view when using quick tags
  networkStore.setFilterTags([...selectedQuickTags.value])
  localStorage.setItem('trinity-dashboard-quick-tags', JSON.stringify(selectedQuickTags.value))
}

// Clear persistence
function clearQuickTags() {
  selectedQuickTags.value = []
  networkStore.setFilterTags([])
  localStorage.removeItem('trinity-dashboard-quick-tags')
}
```

**Restore on Mount** (Lines 583-593):
```javascript
onMounted(async () => {
  systemViewsStore.initialize()  // Restores persisted system view selection
  await systemViewsStore.fetchViews()

  // Apply persisted time range to network store
  networkStore.timeRangeHours = selectedTimeRange.value

  // Apply persisted quick tags filter (only if no system view is active)
  if (!systemViewsStore.activeViewId && selectedQuickTags.value.length > 0) {
    networkStore.setFilterTags([...selectedQuickTags.value])
  }
  // ... rest of mount logic
})
```

### System View vs Quick Tags Interaction

When a System View is selected, quick tags are synced to match the view's tags but NOT persisted:

```javascript
// Dashboard.vue:552-561
watch(activeFilterTags, (tags) => {
  networkStore.setFilterTags(tags)
  if (activeViewId.value) {
    selectedQuickTags.value = [...tags]
    // Clear persisted quick tags when using a system view
    localStorage.removeItem('trinity-dashboard-quick-tags')
  }
}, { immediate: true })
```

This ensures:
- Quick tags persist when user manually selects tags
- Quick tags are cleared when user selects a System View
- System View selection takes precedence over persisted quick tags on reload

### Testing

**Test Case: Filter Persistence**
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Select "7d" from time range dropdown | Data reloads for 7 days |
| 2 | Select 2 tags from quick tag dropdown | Graph filters to those tags |
| 3 | Refresh the page (F5 or Cmd+R) | Time range shows "7d", tags still selected |
| 4 | Check localStorage | `trinity-dashboard-time-range` = "168", `trinity-dashboard-quick-tags` = `["tag1","tag2"]` |

**Test Case: System View Clears Quick Tags**
| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Select 2 tags from quick tag dropdown | Tags persisted to localStorage |
| 2 | Select a System View from sidebar | Quick tags sync to view's tags |
| 3 | Refresh the page | System View is restored, quick tags not persisted |
| 4 | Check localStorage | `trinity-dashboard-quick-tags` key removed |

## Revision History

| Date | Change |
|------|--------|
| 2026-03-03 | **UX: Capacity Meter in Timeline Tiles (CAPACITY-001 Phase 2)** - CapacityMeter component added to every agent tile as a sibling to the content column. Tile outer div changed from `flex-col` to `flex` row. `slotStats` prop added: Dashboard destructures from network store, passes to ReplayTimeline, threaded through `agentRows` computed. Data from `GET /api/agents/slots` via `fetchSlotStats()` on 5s polling. Defaults to active=0/max=3 when no data. |
| 2026-03-03 | **UX: Success Rate Bar (Issue #60)** - Row 2 in timeline agent tiles now shows a success rate bar instead of context progress bar. Uses 24h success rate (primary) with 7d fallback. Helpers: `getRowSuccessPercent()`, `getSuccessBarClass()`. Removed: `getProgressBarClass()`. `contextStats` prop still passed for activity state detection but no longer displayed as a bar. `fetchExecutionStats()` now requests `include_7d: true` for dual-window stats (`taskCount7d`, `successRate7d`). Tile layout condensed from 5 rows to 3 rows (autonomy toggle moved to Row 1). |
| 2026-03-02 | **UX: Filter Persistence** - Dashboard now preserves filter state across page reload. Persisted: time range (`trinity-dashboard-time-range`), quick tags (`trinity-dashboard-quick-tags`). Previously persisted: view mode, tag clouds toggle, system view selection, node positions. Quick tags cleared when system view is selected. |
| 2026-02-27 | **Fix (REFRESH-001)**: Dashboard Timeline Refresh - Added WebSocket ping/pong heartbeat (30s), activity status change handler for `agent_activity` events, and fallback activity polling (60s). Prevents timeline from going stale when idle. Backend: `main.py:362-376`. Frontend: `network.js` (4 new functions), `Dashboard.vue` (lifecycle hooks). |
| 2026-01-29 | **Fix**: Scheduler sync bug resolved - `next_run_at` now calculated in database layer (`db/schedules.py`); dedicated scheduler syncs every 60s (`scheduler/service.py`). Schedule markers appear immediately after schedule creation without container restart. See `scheduling.md` and `scheduler-service.md` for details. |
| 2026-01-29 | **Feature (TSM-001)**: Timeline Schedule Markers - purple triangles at `next_run_at` positions for enabled schedules; hover tooltip with details; click navigates to Schedules tab. Data fetched via `fetchSchedules()` from `GET /api/ops/schedules?enabled_only=true` |
| 2026-01-15 | **Critical Fix**: Timezone-aware timestamp handling - All timestamps now use UTC with 'Z' suffix. Frontend uses `parseUTC()` and `getTimestampMs()` utilities. Events display at correct positions regardless of server/user timezone difference. See `docs/TIMEZONE_HANDLING.md` |
| 2026-01-15 | **Fix**: MCP executions now appear on Timeline - Fixed API field mismatch (`timeline` → `activities`) and activity `triggered_by` (`user` → `mcp` for MCP calls) |
| 2026-01-15 | **Fix**: Timeline now visible even with no events - `timelineStart`/`timelineEnd` always provide valid time range based on `timeRangeHours`, enabling live event streaming |
| 2026-01-13 | **UX**: Timeline is now the default view for new users; header logo is clickable and navigates to Dashboard |
| 2026-01-13 | **Feature**: In-progress bars now extend in real-time - bars grow every second as tasks execute, tooltips show live elapsed time |
| 2026-01-11 | **Fix**: Frontend network.js now correctly reads `related_execution_id` from top-level activity field (was only checking details) |
| 2026-01-11 | **Docs**: Clarified execution ID handling - navigation uses Database ID (from `related_execution_id`), not Queue ID |
| 2026-01-11 | **UX**: Click opens Execution Detail page in new tab instead of same-tab navigation |
| 2026-01-10 | **Feature**: Click-to-navigate - Click execution bar to view details |
| 2026-01-10 | **Major**: Execution-only boxes - collaboration events only create arrows, not boxes |
| 2026-01-15 | **Feature**: Added pink color (#ec4899) for MCP executions (`triggered_by='mcp'`) with "MCP Task" tooltip prefix; legend now shows 4 execution types |
| 2026-01-10 | **Major**: Trigger-based color coding - Green=Manual, Purple=Scheduled, Cyan=Agent-Triggered |
| 2026-01-10 | **Major**: Arrow validation - 30-second tolerance window, prevents floating arrows |
| 2026-01-10 | **Fix**: Source agent mapping - execution events use `agent_name`, collaboration events use `details.source_agent` |
| 2026-01-10 | Updated legend to show "Manual", "Scheduled", "Agent-Triggered" |
| 2026-01-10 | NOW marker at 90% viewport: positioned at 90% width with 10% padding on right |
| 2026-01-10 | Future scroll prevention: users cannot scroll past NOW into the future |
| 2026-01-10 | Added `futurePadding` computed property (~11% padding in live mode) |
| 2026-01-10 | All Executions: Timeline now shows scheduled, manual, and collaboration events |
| 2026-01-10 | Added duration-based bar widths, enhanced tooltips, visual improvements |
| 2026-01-10 | Fixed collaboration activity completion bug in HTTP error handler |
| 2026-01-10 | Initial documentation
