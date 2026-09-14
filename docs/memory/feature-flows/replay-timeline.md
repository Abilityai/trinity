# Feature: Dashboard Replay Timeline

> **Last Updated**: 2026-02-12 - UI Standardization: AutonomyToggle now uses reusable `AutonomyToggle.vue` component (lines 155-161).
>
> **Previous (2026-01-29)**: Added Timeline Schedule Markers - TSM-001

## Overview

The ReplayTimeline component is a waterfall-style timeline visualization for the Dashboard's replay mode. It displays agent activity and inter-agent communication over time with a horizontal time scale, vertical agent rows, activity bars, communication arrows, and a smooth playback cursor.

## User Story

As a platform operator, I want to visualize historical agent activity on a timeline so that I can analyze communication patterns, identify busy periods, and understand multi-agent workflow sequences across time.

## Entry Points

- **UI Route**: `/` (Dashboard) with Replay mode active
- **Component**: `src/frontend/src/components/ReplayTimeline.vue`
- **Parent Integration**: `src/frontend/src/views/Dashboard.vue:142-158`
- **Store**: `src/frontend/src/stores/network.js` (replay state)

---

## Architecture

### Component Hierarchy

```
Dashboard.vue
  |
  +-- ReplayTimeline.vue (visible only in replay mode)
       |
       +-- Playback Controls (Play/Pause/Stop, Speed selector)
       +-- Zoom Controls (+/- buttons, slider)
       +-- "Active only" Toggle
       +-- Time Scale Header (sticky, 15-min grid)
       +-- Agent Rows (sticky labels, activity bars)
       +-- Communication Arrows (SVG lines between rows)
       +-- Playback Cursor (red vertical line)
       +-- "Now" Marker (green dashed line)
```

### Data Flow

```
Dashboard.vue                     ReplayTimeline.vue
     |                                   |
     | Props:                            |
     | - agents (array)                  |
     | - events (historicalCollaborations) --> agentRows (computed)
     | - timelineStart/End               |     --> filteredAgentRows
     | - currentEventIndex               |     --> communicationArrows
     | - replayElapsedMs                 |     --> timeTicks
     | - replaySpeed                     |     --> currentTimeX (smooth)
     | - isPlaying                       |
     |                                   |
     | Events:                           |
     | <-- @play                         |
     | <-- @pause                        |
     | <-- @stop                         |
     | <-- @speed-change                 |
```

---

## Frontend Layer

### Components

#### ReplayTimeline.vue (`src/frontend/src/components/ReplayTimeline.vue`)

**Props** (Lines 292-303):
```javascript
const props = defineProps({
  agents: { type: Array, default: () => [] },
  events: { type: Array, default: () => [] },
  timelineStart: { type: String, default: null },
  timelineEnd: { type: String, default: null },
  currentEventIndex: { type: Number, default: 0 },
  totalEvents: { type: Number, default: 0 },
  totalDuration: { type: Number, default: 0 },
  replayElapsedMs: { type: Number, default: 0 },
  replaySpeed: { type: Number, default: 10 },
  isPlaying: { type: Boolean, default: false }
})
```

**Emits** (Line 305):
```javascript
defineEmits(['play', 'pause', 'stop', 'speed-change', 'seek'])
```

**Local State** (Lines 307-315):
```javascript
const timelineContainer = ref(null)      // Scroll container ref
const zoomLevel = ref(1)                 // Zoom multiplier (0.5-20)
const hideInactiveAgents = ref(false)    // Filter toggle

// Smooth playback cursor tracking
const localElapsedMs = ref(0)            // Local elapsed time for smooth animation
const playbackStartTime = ref(0)         // Wall-clock time when playback started
const playbackStartElapsed = ref(0)      // replayElapsedMs when playback started
let animationFrame = null                // requestAnimationFrame ID
```

---

### Layout Constants (Lines 355-361)

```javascript
const labelWidth = 150       // Agent label column width (px)
const headerHeight = 24      // Time scale header height (px)
const rowHeight = 32         // Height per agent row (px)
const barHeight = 16         // Activity bar height (px)
const baseGridWidth = 800    // Base timeline width before zoom (px)
```

---

### Key Features

#### 1. Playback Controls (Lines 4-48)

**Play/Pause/Stop Buttons**:
```vue
<button @click="$emit('play')" :disabled="isPlaying || totalEvents === 0">
  <!-- Play SVG icon -->
</button>
<button @click="$emit('pause')" :disabled="!isPlaying">
  <!-- Pause SVG icon -->
</button>
<button @click="$emit('stop')">
  <!-- Stop SVG icon -->
</button>
```

**Speed Selector**:
```vue
<select :value="replaySpeed" @change="$emit('speed-change', $event)">
  <option :value="1">1x</option>
  <option :value="2">2x</option>
  <option :value="5">5x</option>
  <option :value="10">10x</option>
  <option :value="20">20x</option>
  <option :value="50">50x</option>
</select>
```

#### 2. Zoom Controls (Lines 50-81)

**Zoom Range**: 50% (0.5) to 2000% (20)

```javascript
const actualGridWidth = computed(() => baseGridWidth * zoomLevel.value)
// 800px * 1 = 800px (100%)
// 800px * 0.5 = 400px (50%)
// 800px * 20 = 16000px (2000%)

function zoomIn() {
  zoomLevel.value = Math.min(20, zoomLevel.value + 0.5)
}

function zoomOut() {
  zoomLevel.value = Math.max(0.5, zoomLevel.value - 0.5)
}

function handleWheel(event) {
  if (event.ctrlKey || event.metaKey) {
    event.preventDefault()
    event.deltaY < 0 ? zoomIn() : zoomOut()
  }
}
```

#### 3. Hide Inactive Agents Toggle (Lines 83-93)

```vue
<label class="flex items-center space-x-1.5 text-xs cursor-pointer">
  <input type="checkbox" v-model="hideInactiveAgents" />
  <span>Active only</span>
</label>
```

**Filtering Logic** (Lines 503-509):
```javascript
const filteredAgentRows = computed(() => {
  if (!hideInactiveAgents.value) {
    return agentRows.value
  }
  return agentRows.value.filter(row => row.hasActivity)
})
```

#### 4. Time Scale Header (Lines 110-161)

**15-Minute Grid Ticks** (Lines 410-438):
```javascript
const timeTicks = computed(() => {
  if (!duration.value) return []

  const ticks = []
  const intervalMs = 15 * 60 * 1000 // 15 minutes
  const start = startTime.value
  const end = endTime.value

  // Round start to nearest 15 min
  const firstTick = Math.ceil(start / intervalMs) * intervalMs

  for (let time = firstTick; time <= end; time += intervalMs) {
    const x = ((time - start) / duration.value) * actualGridWidth.value
    const date = new Date(time)
    const hours = date.getHours()
    const minutes = date.getMinutes()
    const major = minutes === 0 // Hour marks are major ticks

    ticks.push({
      x,
      time,
      label: `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}`,
      major
    })
  }

  return ticks
})
```

**Visual Distinction**:
- Major ticks (hour marks): Solid line, bolder stroke
- Minor ticks (15/30/45 min): Dashed line, lighter stroke

#### 5. Agent Rows with Activity Bars (Lines 165-236)

**Agent Row Computation** (Lines 441-501):
```javascript
const agentRows = computed(() => {
  if (!props.agents.length) return []

  // Group events by agent
  const agentActivityMap = new Map()

  // NOTE: Use getTimestampMs() for timezone-aware parsing (handles missing 'Z' suffix)
  // See docs/TIMEZONE_HANDLING.md for details
  props.events.forEach((event, index) => {
    // Source agent activity
    if (!agentActivityMap.has(event.source_agent)) {
      agentActivityMap.set(event.source_agent, [])
    }
    agentActivityMap.get(event.source_agent).push({
      time: getTimestampMs(event.timestamp),  // from @/utils/timestamps
      type: 'send',
      eventIndex: index
    })

    // Target agent activity
    if (!agentActivityMap.has(event.target_agent)) {
      agentActivityMap.set(event.target_agent, [])
    }
    agentActivityMap.get(event.target_agent).push({
      time: getTimestampMs(event.timestamp),  // from @/utils/timestamps
      type: 'receive',
      eventIndex: index
    })
  })

  return props.agents.map(agent => {
    const activities = agentActivityMap.get(agent.name) || []

    // Convert activities to bars
    const bars = activities.map(act => {
      const x = ((act.time - startTime.value) / duration.value) * actualGridWidth.value
      // Check if active (at or before current playback position)
      const chronoIndex = props.events.length - 1 - act.eventIndex
      const active = chronoIndex < props.currentEventIndex

      return {
        x: Math.max(0, x - 2),
        width: 8,
        active,
        type: act.type
      }
    })

    return {
      name: agent.name,
      status: agent.status,
      activities: bars,
      hasActivity: bars.length > 0,
      isExecuting: bars.some(bar => bar.active) && props.isPlaying
    }
  })
})
```

**Activity Bar Rendering** (Lines 222-236):
```vue
<g v-for="(row, rowIndex) in filteredAgentRows" :key="'activities-' + rowIndex">
  <rect
    v-for="(activity, actIndex) in row.activities"
    :key="'act-' + rowIndex + '-' + actIndex"
    :x="activity.x"
    :y="rowIndex * rowHeight + (rowHeight - barHeight) / 2"
    :width="Math.max(activity.width, 4)"
    :height="barHeight"
    :fill="activity.active ? '#3b82f6' : '#93c5fd'"
    :opacity="activity.active ? 1 : 0.7"
    rx="2"
  />
</g>
```

**Colors**:
- Active bars: `#3b82f6` (blue-500) at 100% opacity
- Inactive bars: `#93c5fd` (blue-300) at 70% opacity

#### 5a. Schedule Markers (Added 2026-01-29, TSM-001)

Visual markers showing when agent schedules are configured to run. Purple triangles appear at the `next_run_at` position for each enabled schedule.

**Requirements Doc**: `docs/requirements/TIMELINE_SCHEDULE_MARKERS.md`

**Props** (Line 386):
```javascript
schedules: { type: Array, default: () => [] }
```

**Computed Property** (`ReplayTimeline.vue:799-834`):
```javascript
const scheduleMarkers = computed(() => {
  if (!props.schedules.length || !startTime.value || !duration.value) return []

  const markers = []

  props.schedules.forEach(schedule => {
    if (!schedule.next_run_at || !schedule.enabled) return

    // Find the agent row index
    const rowIndex = filteredAgentRows.value.findIndex(row => row.name === schedule.agent_name)
    if (rowIndex === -1) return

    // Parse the next_run_at timestamp
    const nextRunMs = getTimestampMs(schedule.next_run_at)

    // Only show markers within the visible timeline range
    if (nextRunMs < startTime.value || nextRunMs > endTime.value) return

    // Calculate x position
    const x = ((nextRunMs - startTime.value) / duration.value) * actualGridWidth.value

    markers.push({
      x,
      rowIndex,
      id: schedule.id,
      name: schedule.name,
      agentName: schedule.agent_name,
      nextRun: schedule.next_run_at,
      cronExpression: schedule.cron_expression,
      message: schedule.message,
      enabled: schedule.enabled
    })
  })

  return markers
})
```

**SVG Rendering** (`ReplayTimeline.vue:312-322`):
```vue
<!-- Schedule markers (show when schedules are set to run) -->
<g v-for="(marker, i) in scheduleMarkers" :key="'sched-' + i">
  <polygon
    :points="getScheduleMarkerPoints(marker)"
    fill="#8b5cf6"
    opacity="0.8"
    class="cursor-pointer hover:opacity-100 transition-opacity"
    @click="navigateToSchedule(marker)"
  >
    <title>{{ getScheduleTooltip(marker) }}</title>
  </polygon>
</g>
```

**Helper Functions** (`ReplayTimeline.vue:1014-1034`):
```javascript
function getScheduleMarkerPoints(marker) {
  const x = marker.x
  const y = marker.rowIndex * rowHeight + 4
  // Downward-pointing triangle
  return `${x},${y} ${x - 4},${y + 8} ${x + 4},${y + 8}`
}

function getScheduleTooltip(marker) {
  const nextRun = formatLocalTime(marker.nextRun)
  const messagePreview = marker.message && marker.message.length > 50
    ? marker.message.substring(0, 50) + '...'
    : (marker.message || 'No message')
  return `Schedule: ${marker.name}\nNext Run: ${nextRun}\nCron: ${marker.cronExpression}\nMessage: ${messagePreview}`
}

function navigateToSchedule(marker) {
  router.push({
    path: `/agents/${marker.agentName}`,
    query: { tab: 'schedules' }
  })
}
```

**Visual Design**:
- Shape: Downward-pointing triangle (8px wide x 8px tall)
- Color: Purple (`#8b5cf6`) to match scheduled execution bars
- Position: Top of agent row (y = rowIndex * rowHeight + 4)
- Interaction: Hover shows tooltip, click navigates to Schedules tab

**Timeline Extension Limit** (Updated 2026-01-29):
- Timeline extends into the future **max 2 hours** for schedule markers
- Far-off schedules (beyond 2 hours) don't extend the timeline to prevent scale distortion
- Markers only appear if `next_run_at` falls within the visible time range

**Scheduler Sync (FIXED 2026-01-29)**:
> New schedules now work without container restart.
>
> **Fix Details**:
> 1. **Database layer** (`src/backend/db/schedules.py`): `create_schedule()`, `update_schedule()`, and `set_schedule_enabled()` now calculate `next_run_at` using croniter before INSERT/UPDATE
> 2. **Dedicated scheduler** (`src/scheduler/service.py`): Added periodic sync (`_sync_schedules()`) every 60 seconds that detects new, updated, and deleted schedules
>
> See `scheduling.md` and `scheduler-service.md` for full details.

**Legend Entry** (`ReplayTimeline.vue:85-88`):
```html
<span class="flex items-center space-x-1" title="Schedule marker (shows next scheduled run time)">
  <span class="text-purple-500 text-xs font-bold">▼</span>
  <span>Next Run</span>
</span>
```

#### 5b. Real-Time In-Progress Bar Extension (Added 2026-01-13)

In-progress task bars now grow in real-time as the task executes, providing live visual feedback.

**Implementation** (`ReplayTimeline.vue:568-612`):

1. **Store start timestamp**: Activities store `startTimestamp` for dynamic duration calculation:
```javascript
import { getTimestampMs } from '@/utils/timestamps'

const startTimestamp = getTimestampMs(event.timestamp)  // Timezone-aware
agentActivityMap.get(event.source_agent).push({
  time: startTimestamp,
  startTimestamp, // Store for dynamic duration calculation
  isInProgress: event.status === 'started',
  // ...
})
```

2. **Dynamic duration calculation**: For in-progress tasks, elapsed time updates every second:
```javascript
// ReplayTimeline.vue:606-612
let effectiveDuration = act.durationMs
if (act.isInProgress && act.startTimestamp) {
  // For in-progress tasks, calculate elapsed time from start
  // This will update every second as currentNow updates
  effectiveDuration = Math.max(1000, currentNow.value - act.startTimestamp)
}
```

3. **Reactive timer**: A `currentNow` ref updates every second (`ReplayTimeline.vue:371, 399`):
```javascript
const currentNow = ref(Date.now())
// Updated every second in setInterval
currentNow.value = Date.now()
```

4. **Bar properties**: Updated to use effective duration (`ReplayTimeline.vue:629, 631`):
```javascript
return {
  durationMs: effectiveDuration, // Live elapsed time for tooltips
  isEstimated: act.isEstimated && !act.isInProgress, // Not estimated while live
}
```

**Visual Behavior**:
- Task starts: Amber bar with minimum width (12px)
- Every second: Bar grows as `effectiveDuration` increases
- Tooltip: Shows live time like "In Progress - 45.3s"
- Task completes: Bar snaps to final size from actual `duration_ms`
- Task closed by a watchdog sweep (#2434): `duration_ms` is **NULL** — the sweep
  invented the end time and no longer records a number for it — so the bar takes
  the existing estimate path (`event.duration_ms || 30000`) and is flagged
  `isEstimated`. Previously it snapped to a fabricated `now − started_at`, which
  rendered a 15-minute run as a ~120-minute bar

#### 6. Blinking Execution Indicator (Lines 178-186)

```vue
<span
  class="w-2 h-2 rounded-full mr-2 flex-shrink-0"
  :class="[
    row.isExecuting ? 'bg-green-500 animate-pulse' :
      (row.status === 'running' ? 'bg-green-500' : 'bg-gray-400')
  ]"
></span>
```

**Logic**: Agent label shows pulsing green dot when:
- Agent has activity at or before current playback position
- Replay is currently playing

#### 7. Communication Arrows (Lines 238-256)

**Arrow Computation** (Lines 511-547):
```javascript
const communicationArrows = computed(() => {
  if (!props.events.length || !filteredAgentRows.value.length) return []

  // Build index map from filtered rows
  const agentIndexMap = new Map()
  filteredAgentRows.value.forEach((row, i) => {
    agentIndexMap.set(row.name, i)
  })

  // Reverse to get chronological order
  const chronoEvents = [...props.events].reverse()

  return chronoEvents.map((event, chronoIndex) => {
    const sourceIndex = agentIndexMap.get(event.source_agent)
    const targetIndex = agentIndexMap.get(event.target_agent)

    if (sourceIndex === undefined || targetIndex === undefined) return null

    const time = getTimestampMs(event.timestamp)  // Timezone-aware parsing
    const x = ((time - startTime.value) / duration.value) * actualGridWidth.value

    const y1 = sourceIndex * rowHeight + rowHeight / 2
    const y2 = targetIndex * rowHeight + rowHeight / 2

    const active = chronoIndex < props.currentEventIndex

    return {
      x,
      y1,
      y2,
      active,
      direction: targetIndex > sourceIndex ? 'down' : 'up'
    }
  }).filter(a => a !== null)
})
```

**Arrow Rendering** (Lines 238-256):
```vue
<g v-for="(arrow, i) in communicationArrows" :key="'arrow-' + i">
  <line
    :x1="arrow.x"
    :y1="arrow.y1"
    :x2="arrow.x"
    :y2="arrow.y2"
    :stroke="arrow.active ? '#06b6d4' : '#67e8f9'"
    :stroke-width="arrow.active ? 1.5 : 1"
    :opacity="arrow.active ? 1 : 0.6"
  />
  <polygon
    :points="getArrowHead(arrow)"
    :fill="arrow.active ? '#06b6d4' : '#67e8f9'"
    :opacity="arrow.active ? 1 : 0.6"
  />
</g>
```

**Arrow Head** (Lines 568-578):
```javascript
function getArrowHead(arrow) {
  const size = 2
  const x = arrow.x
  const y = arrow.y2

  if (arrow.direction === 'down') {
    return `${x},${y} ${x-size},${y-size*2} ${x+size},${y-size*2}`
  } else {
    return `${x},${y} ${x-size},${y+size*2} ${x+size},${y+size*2}`
  }
}
```

**Colors**:
- Active arrows: `#06b6d4` (cyan-500)
- Inactive arrows: `#67e8f9` (cyan-300)

#### 8. Smooth Playback Cursor (Lines 258-281, 311-353)

**requestAnimationFrame Loop** (Lines 317-323):
```javascript
function updateCursor() {
  if (props.isPlaying && playbackStartTime.value > 0) {
    const wallClockElapsed = Date.now() - playbackStartTime.value
    localElapsedMs.value = playbackStartElapsed.value + wallClockElapsed
    animationFrame = requestAnimationFrame(updateCursor)
  }
}
```

**Watcher for isPlaying** (Lines 325-340):
```javascript
watch(() => props.isPlaying, (playing) => {
  if (playing) {
    // Store when we started and the elapsed value at that time
    playbackStartTime.value = Date.now()
    playbackStartElapsed.value = props.replayElapsedMs
    localElapsedMs.value = props.replayElapsedMs
    updateCursor()
  } else {
    if (animationFrame) {
      cancelAnimationFrame(animationFrame)
      animationFrame = null
    }
    // Sync to actual elapsed when paused
    localElapsedMs.value = props.replayElapsedMs
  }
})
```

**Cursor Position Calculation** (Lines 549-565):
```javascript
const currentTimeX = computed(() => {
  if (!props.events.length || !duration.value) return -1

  // Get the first event time as the replay start reference
  const chronoEvents = [...props.events].reverse()
  const firstEventTime = new Date(chronoEvents[0].timestamp).getTime()

  // Calculate current simulated time
  const simulatedElapsedMs = localElapsedMs.value * props.replaySpeed
  const currentSimulatedTime = firstEventTime + simulatedElapsedMs

  // Clamp to timeline bounds
  if (currentSimulatedTime < startTime.value) return -1

  return ((currentSimulatedTime - startTime.value) / duration.value) * actualGridWidth.value
})
```

**Rendering** (Lines 272-281):
```vue
<line
  v-if="currentTimeX >= 0"
  :x1="currentTimeX"
  y1="0"
  :x2="currentTimeX"
  :y2="filteredAgentRows.length * rowHeight"
  stroke="#ef4444"
  stroke-width="2"
  class="transition-all duration-100"
/>
```

**Color**: `#ef4444` (red-500) with 2px stroke width

#### 9. "Now" Marker (Lines 141-160, 258-269)

**Position Calculation** (Lines 402-408):
```javascript
const nowX = computed(() => {
  if (!duration.value || !startTime.value) return -1
  const now = Date.now()
  if (now < startTime.value || now > endTime.value) return -1
  return ((now - startTime.value) / duration.value) * actualGridWidth.value
})
```

**Timeline Extension** (Lines 393-399):
```javascript
const endTime = computed(() => {
  const eventEnd = props.timelineEnd ? new Date(props.timelineEnd).getTime() : 0
  const now = Date.now()
  // Use whichever is later: last event or now
  return Math.max(eventEnd, now)
})
```

**Rendering**:
```vue
<line
  v-if="nowX >= 0"
  :x1="nowX"
  :y2="filteredAgentRows.length * rowHeight"
  stroke="#10b981"
  stroke-width="1"
  stroke-dasharray="4,4"
  opacity="0.6"
/>
```

**Color**: `#10b981` (emerald-500) with dashed line

#### 10. Sticky Headers (Lines 110-163, 166-188)

**Time Scale Header** (sticky top):
```vue
<div
  class="sticky top-0 bg-gray-50 dark:bg-gray-800 border-b z-20"
  :style="{ height: headerHeight + 'px' }"
>
  <!-- Time scale SVG -->
</div>
```

**Agent Labels** (sticky left):
```vue
<div
  class="sticky left-0 z-10 bg-white dark:bg-gray-800"
  :style="{ width: labelWidth + 'px', flexShrink: 0 }"
>
  <!-- Agent label rows -->
</div>
```

---

### Dashboard.vue Integration (Lines 142-158)

```vue
<ReplayTimeline
  v-if="isReplayMode"
  :agents="agents"
  :events="historicalCollaborations"
  :timeline-start="timelineStart"
  :timeline-end="timelineEnd"
  :current-event-index="currentEventIndex"
  :total-events="totalEvents"
  :total-duration="totalDuration"
  :replay-elapsed-ms="replayElapsedMs"
  :replay-speed="replaySpeed"
  :is-playing="isPlaying"
  @play="handlePlay"
  @pause="handlePause"
  @stop="handleStop"
  @speed-change="handleSpeedChange"
/>
```

**Event Handlers** (Lines 495-510):
```javascript
function handlePlay() {
  networkStore.startReplay()
}

function handlePause() {
  networkStore.pauseReplay()
}

function handleStop() {
  networkStore.stopReplay()
}

function handleSpeedChange(event) {
  const speed = parseInt(event.target.value)
  networkStore.setReplaySpeed(speed)
}
```

---

### State Management

#### network.js (`src/frontend/src/stores/network.js`)

**Replay State** (Lines 53-60):
```javascript
const isReplayMode = ref(false)
const isPlaying = ref(false)
const replaySpeed = ref(10)
const currentEventIndex = ref(0)
const replayInterval = ref(null)
const replayStartTime = ref(null)
const replayElapsedMs = ref(0)
```

**Computed Properties** (Lines 79-107):
```javascript
const totalEvents = computed(() => historicalCollaborations.value.length)

const totalDuration = computed(() => {
  if (historicalCollaborations.value.length < 2) return 0
  const first = new Date(historicalCollaborations.value[historicalCollaborations.value.length - 1].timestamp)
  const last = new Date(historicalCollaborations.value[0].timestamp)
  return last - first
})

const playbackPosition = computed(() => {
  if (totalEvents.value === 0) return 0
  return (currentEventIndex.value / totalEvents.value) * 100
})

const timelineStart = computed(() => {
  if (historicalCollaborations.value.length === 0) return null
  return historicalCollaborations.value[historicalCollaborations.value.length - 1].timestamp
})

const timelineEnd = computed(() => {
  if (historicalCollaborations.value.length === 0) return null
  return historicalCollaborations.value[0].timestamp
})
```

**Replay Actions** (Lines 797-1020):
- `setReplayMode(mode)` - Toggle replay/live mode
- `startReplay()` - Begin or resume playback
- `pauseReplay()` - Pause at current position
- `stopReplay()` - Stop and reset to beginning
- `setReplaySpeed(speed)` - Change playback speed
- `scheduleNextEvent()` - Time-compressed event scheduling
- `simulateAgentActivity(source, target)` - Activity state simulation

---

## Styling

### CSS (Lines 588-663)

**Container Constraints**:
```css
.replay-timeline {
  max-height: 320px;
}

.timeline-scroll-container {
  max-height: 280px;
}
```

**Custom Scrollbars**:
```css
.timeline-scroll-container::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}

.timeline-scroll-container::-webkit-scrollbar-thumb {
  background: #cbd5e1;
  border-radius: 4px;
}
```

**Zoom Slider**:
```css
input[type="range"]::-webkit-slider-thumb {
  background-color: #3b82f6;
  height: 12px;
  width: 12px;
  border-radius: 50%;
}
```

---

## Performance Considerations

### requestAnimationFrame for Smooth Cursor

The playback cursor uses `requestAnimationFrame` instead of CSS transitions for smoother animation during playback:

```javascript
function updateCursor() {
  if (props.isPlaying && playbackStartTime.value > 0) {
    const wallClockElapsed = Date.now() - playbackStartTime.value
    localElapsedMs.value = playbackStartElapsed.value + wallClockElapsed
    animationFrame = requestAnimationFrame(updateCursor)
  }
}
```

**Benefits**:
- 60fps smooth animation
- Accurate wall-clock tracking
- No CSS transition jank
- Proper cleanup on unmount

### Cleanup on Unmount (Lines 349-353)

```javascript
onUnmounted(() => {
  if (animationFrame) {
    cancelAnimationFrame(animationFrame)
  }
})
```

### SVG Performance

- Activity bars and arrows rendered as SVG primitives
- Uses `key` attributes for efficient Vue diffing
- Minimal DOM updates during playback

---

## Error Handling

| Error Case | Handling |
|------------|----------|
| No events | Returns -1 for cursor position, empty arrays for rows/arrows |
| No agents | Returns empty agentRows array |
| Invalid timestamps | Falls back to 0 or -1 positions |
| Browser tab backgrounded | requestAnimationFrame pauses automatically |

---

## Testing

### Prerequisites

1. Running services: `./scripts/deploy/start.sh`
2. At least 2 agents with historical collaboration data
3. Authenticated user session

### Test Cases

#### Test Case 1: Basic Timeline Rendering
| Step | Action | Expected |
|------|--------|----------|
| 1 | Navigate to Dashboard | Live mode active |
| 2 | Click "Replay" toggle | ReplayTimeline appears |
| 3 | Check timeline header | 15-min grid lines visible |
| 4 | Check agent rows | All agents listed with status dots |
| 5 | Check activity bars | Blue bars at event timestamps |

#### Test Case 2: Zoom Controls
| Step | Action | Expected |
|------|--------|----------|
| 1 | Note initial zoom | Shows "100%" |
| 2 | Click + button | Zoom increases to 150% |
| 3 | Drag slider to max | Zoom shows "2000%" |
| 4 | Click - button | Zoom decreases by 50% |
| 5 | Ctrl+Scroll up | Zoom increases |

#### Test Case 3: Hide Inactive Toggle
| Step | Action | Expected |
|------|--------|----------|
| 1 | Note agent count | All agents visible |
| 2 | Check "Active only" | Only agents with activity remain |
| 3 | Uncheck toggle | All agents reappear |
| 4 | Check arrows | Arrows update to filtered positions |

#### Test Case 4: Playback Controls
| Step | Action | Expected |
|------|--------|----------|
| 1 | Click Play | Red cursor starts moving |
| 2 | Watch activity | Bars turn active blue progressively |
| 3 | Watch arrows | Arrows turn cyan when reached |
| 4 | Click Pause | Cursor stops, state preserved |
| 5 | Click Play again | Resumes from paused position |
| 6 | Click Stop | Resets to start, all bars/arrows inactive |

#### Test Case 5: Speed Selection
| Step | Action | Expected |
|------|--------|----------|
| 1 | Start at 10x | Moderate cursor speed |
| 2 | Change to 50x | Cursor moves much faster |
| 3 | Change to 1x | Cursor moves very slowly |

#### Test Case 6: Now Marker
| Step | Action | Expected |
|------|--------|----------|
| 1 | Check timeline | Green dashed "now" line visible |
| 2 | Timeline extends | End bound includes current time |

#### Test Case 7: Blinking Indicator
| Step | Action | Expected |
|------|--------|----------|
| 1 | Start playback | Agent label dots pulse green when active |
| 2 | Pause playback | Pulsing stops |
| 3 | Stop playback | All dots return to static state |

#### Test Case 8: In-Progress Bar Real-Time Extension (Added 2026-01-13)
| Step | Action | Expected |
|------|--------|----------|
| 1 | Start a long-running task | Amber bar appears with minimum width |
| 2 | Wait 5-10 seconds | Bar visibly grows wider each second |
| 3 | Hover over bar | Tooltip shows live elapsed time (e.g., "In Progress - 8.2s") |
| 4 | Wait for task completion | Bar snaps to final width, color changes based on result |
| 5 | Hover after completion | Tooltip shows actual duration (no tilde prefix) |

---

## Related Flows

### Upstream
- **[Agent Network](agent-network.md)** - Parent Dashboard view
- **[Agent Network Replay Mode](agent-network-replay-mode.md)** - Replay mode orchestration
- **[Activity Stream](activity-stream.md)** - Historical event data source

### Downstream
- None (read-only visualization)

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-03-04 | **Paid/Public trigger types**: Added `paid` (yellow `#eab308`) and `public` (teal `#0d9488`) bar colors, legend entries, and tooltip labels to `getBarColor()` and `getBarTooltip()`. |
| 2026-02-12 | **UI Standardization**: AutonomyToggle now uses reusable `AutonomyToggle.vue` component (lines 155-161, imported at line 356). No label shown (`showLabel="false"`) for compact timeline rows. See [autonomy-toggle-component.md](autonomy-toggle-component.md). |
| 2026-01-29 | **Fix**: Scheduler sync bug resolved - `next_run_at` now calculated in database layer; dedicated scheduler syncs every 60s. Schedule markers appear immediately after schedule creation without container restart. |
| 2026-01-29 | **Fix (TSM-001)**: Timeline scale issue - far-off schedules (days away) no longer extend timeline excessively. Added 2-hour max limit for future extension. Markers only visible if `next_run_at` within 2 hours of NOW |
| 2026-01-29 | **Feature (TSM-001)**: Timeline Schedule Markers - purple triangles at `next_run_at` position for enabled schedules; hover tooltip shows schedule details; click navigates to Schedules tab. Data from `GET /api/ops/schedules?enabled_only=true` via `network.js:fetchSchedules()` |
| 2026-01-15 | **Timezone-aware timestamps**: Code examples updated to use `getTimestampMs()` from `@/utils/timestamps`. Ensures events display at correct positions regardless of server/user timezone. See `docs/TIMEZONE_HANDLING.md` |
| 2026-01-15 | **Feature**: Added pink color (#ec4899) for MCP executions (`triggered_by='mcp'`); updated Visual Elements Summary with trigger-based color scheme |
| 2026-01-13 | **Feature**: In-progress bars now extend in real-time - added `startTimestamp` storage, dynamic `effectiveDuration` calculation, and 1-second reactive updates |
| 2026-01-04 | Initial documentation of ReplayTimeline component |

---

## References

### Code Files
- `src/frontend/src/components/ReplayTimeline.vue` - Timeline component (~1060 lines)
  - Props: Line 370-387 (includes `schedules` prop at line 386)
  - Schedule markers computed: Lines 799-834
  - Schedule marker SVG: Lines 312-322
  - Schedule helper functions: Lines 1014-1034
- `src/frontend/src/views/Dashboard.vue` - Parent integration
  - ReplayTimeline props: Lines 142-165 (includes `:schedules="schedules"` at line 159)
  - onMounted with `fetchSchedules()`: Lines 416-434 (line 424)
- `src/frontend/src/stores/network.js` - Network state
  - `schedules` ref: Line 54
  - `fetchSchedules()` action: Lines 1214-1225
  - Export: Line 1350

### Visual Elements Summary

| Element | Color | Purpose |
|---------|-------|---------|
| Activity bars (manual) | `#22c55e` (green-500) | Manual task executions |
| Activity bars (MCP) | `#ec4899` (pink-500) | MCP executions via Claude Code |
| Activity bars (scheduled) | `#8b5cf6` (purple-500) | Scheduled task executions |
| Activity bars (agent-triggered) | `#06b6d4` (cyan-500) | Agent-triggered executions |
| Activity bars (paid) | `#eab308` (yellow-500) | Paid executions (Nevermined payment) |
| Activity bars (public) | `#0d9488` (teal-500) | Public link executions |
| Activity bars (in-progress) | `#f59e0b` (amber-500) | Currently running tasks (grows in real-time) |
| Activity bars (error) | `#ef4444` (red-500) | Failed executions |
| Activity bars (inactive) | Lighter variant | Events after cursor (30% opacity reduction) |
| **Schedule markers** | `#8b5cf6` (purple-500) | Shows `next_run_at` for enabled schedules (TSM-001) |
| Communication arrows (active) | `#06b6d4` (cyan-500) | Active connections |
| Communication arrows (inactive) | `#67e8f9` (cyan-300) | Future connections |
| Playback cursor | `#ef4444` (red-500) | Current playback position |
| "Now" marker | `#10b981` (emerald-500) | Current real time |
| Major grid lines | `#d1d5db` (gray-300) | Hour marks |
| Minor grid lines | `#e5e7eb` (gray-200) | 15-min marks |
| Executing indicator | `bg-green-500 animate-pulse` | Active agent during playback |
