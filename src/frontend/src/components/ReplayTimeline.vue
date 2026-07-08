<template>
  <div class="replay-timeline flex flex-col h-full bg-gray-50 dark:bg-gray-900">
    <!-- Header with controls -->
    <div class="flex items-center justify-between px-4 py-2 border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 flex-shrink-0">
      <!-- Left: Controls -->
      <div class="flex items-center space-x-3">
        <!-- Zoom Controls -->
        <div class="flex items-center space-x-1">
          <button
            @click="zoomOut"
            class="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-500 dark:text-gray-400"
            title="Zoom out"
          >
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M20 12H4" />
            </svg>
          </button>
          <input
            type="range"
            :value="zoomLevel"
            @input="zoomLevel = parseFloat($event.target.value)"
            min="0.5"
            max="20"
            step="0.5"
            class="w-20 h-1 accent-blue-500"
            title="Zoom level"
          />
          <button
            @click="zoomIn"
            class="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-500 dark:text-gray-400"
            title="Zoom in"
          >
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
            </svg>
          </button>
          <span class="text-xs text-gray-400 dark:text-gray-500 ml-1 w-8">{{ Math.round(zoomLevel * 100) }}%</span>
        </div>

        <!-- Hide inactive toggle -->
        <div class="flex items-center space-x-1 border-l border-gray-300 dark:border-gray-600 pl-3">
          <label class="flex items-center space-x-1.5 text-xs text-gray-500 dark:text-gray-400 cursor-pointer">
            <input
              type="checkbox"
              v-model="hideInactiveAgents"
              class="w-3 h-3 rounded border-gray-300 dark:border-gray-600 text-blue-500 focus:ring-blue-500"
            />
            <span>Active only</span>
          </label>
        </div>

        <!-- Jump to Now button (when scrolled away) -->
        <button
          v-if="isLiveMode && !autoScrollEnabled"
          @click="jumpToNow"
          class="flex items-center space-x-1 px-2 py-1 text-xs font-medium text-emerald-600 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-900/30 rounded hover:bg-emerald-100 dark:hover:bg-emerald-900/50 transition-colors"
        >
          <svg class="w-3 h-3" fill="currentColor" viewBox="0 0 20 20">
            <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-12a1 1 0 10-2 0v4a1 1 0 00.293.707l2.828 2.829a1 1 0 101.415-1.415L11 9.586V6z" clip-rule="evenodd" />
          </svg>
          <span>Jump to Now</span>
        </button>
      </div>

      <!-- Right: Legend and Stats -->
      <div class="flex items-center space-x-4 text-xs text-gray-500 dark:text-gray-400">
        <!-- Activity type legend -->
        <div class="hidden sm:flex items-center space-x-3 border-r border-gray-300 dark:border-gray-600 pr-3">
          <span class="flex items-center space-x-1" title="Manual task executions">
            <span class="w-2 h-2 rounded" style="background-color: #22c55e"></span>
            <span>Manual</span>
          </span>
          <span class="flex items-center space-x-1" title="MCP executions (via Claude Code)">
            <span class="w-2 h-2 rounded" style="background-color: #ec4899"></span>
            <span>MCP</span>
          </span>
          <span class="flex items-center space-x-1" title="Scheduled task executions">
            <span class="w-2 h-2 rounded" style="background-color: #8b5cf6"></span>
            <span>Scheduled</span>
          </span>
          <span class="flex items-center space-x-1" title="Agent-triggered executions (called by another agent)">
            <span class="w-2 h-2 rounded" style="background-color: #06b6d4"></span>
            <span>Agent-Triggered</span>
          </span>
          <span class="flex items-center space-x-1" title="Paid executions (via Nevermined payment)">
            <span class="w-2 h-2 rounded" style="background-color: #eab308"></span>
            <span>Paid</span>
          </span>
          <span class="flex items-center space-x-1" title="Public link executions">
            <span class="w-2 h-2 rounded" style="background-color: #0d9488"></span>
            <span>Public</span>
          </span>
          <span class="flex items-center space-x-1" title="Schedule marker (shows next scheduled run time)">
            <span class="text-accent-purple-500 text-xs font-bold">▼</span>
            <span>Next Run</span>
          </span>
        </div>
        <span v-if="isLiveMode" class="flex items-center space-x-1">
          <span class="w-1.5 h-1.5 rounded-full bg-status-success-500 animate-pulse"></span>
          <span>Live</span>
        </span>
        <span>{{ totalEvents }} events</span>
      </div>
    </div>

    <!-- Timeline Grid with scroll -->
    <div
      class="timeline-scroll-container flex-1 overflow-auto"
      ref="timelineContainer"
      @wheel="handleWheel"
      @scroll="handleScroll"
    >
      <div class="relative flex" :style="{ width: timelineWidth + 'px', minHeight: '100%' }">
        <!-- Sticky agent labels (Compact tiles) -->
        <div
          class="sticky left-0 z-10 bg-gray-50 dark:bg-gray-900 flex-shrink-0"
          :style="{ width: labelWidth + 'px' }"
        >
          <!-- Time scale header spacer -->
          <div
            class="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700"
            :style="{ height: headerHeight + 'px' }"
          ></div>

          <!-- Agent tiles (compact) -->
          <div
            v-for="(row, i) in filteredAgentRows"
            :key="'label-' + i"
            :class="[
              'px-2 py-1.5 border-b border-r border-gray-200 dark:border-gray-700',
              'flex',
              row.isSystemAgent
                ? 'bg-accent-purple-50/80 dark:bg-accent-purple-900/20'
                : 'bg-white dark:bg-gray-800'
            ]"
            :style="{ height: rowHeight + 'px' }"
          >
            <!-- Avatar (vertically centered, with border ring) -->
            <div class="flex-shrink-0 flex items-center mr-1.5">
              <div class="rounded-full border-2 overflow-hidden shadow-sm"
                   :class="row.isSystemAgent ? 'border-accent-purple-400 dark:border-accent-purple-500' : 'border-action-primary-400 dark:border-action-primary-500'"
              >
                <AgentAvatar :name="row.name" :avatar-url="row.avatarUrl" size="lg" />
              </div>
            </div>

            <div class="flex flex-col justify-between flex-1 min-w-0">
              <!-- Row 1: Name, badges, status dot -->
              <div class="flex items-center justify-between">
                <div class="flex items-center flex-1 mr-2 min-w-0">
                  <div
                    :class="[
                      'w-2 h-2 rounded-full flex-shrink-0 mr-1.5',
                      row.activityState === 'active' ? 'active-pulse' : ''
                    ]"
                    :style="{ backgroundColor: getStatusDotColor(row) }"
                  ></div>
                  <span
                    class="text-sm font-semibold text-gray-900 dark:text-white truncate cursor-pointer hover:text-blue-600 dark:hover:text-blue-400"
                    @click="navigateToAgent(row.name)"
                    :title="row.name"
                  >
                    {{ row.name }}
                  </span>
                  <span
                    v-if="row.isSystemAgent"
                    class="ml-1.5 px-1 py-0.5 text-[10px] font-semibold rounded bg-accent-purple-100 text-accent-purple-700 dark:bg-accent-purple-900/50 dark:text-accent-purple-300 flex-shrink-0"
                  >
                    SYS
                  </span>
                </div>
                <!-- Autonomy toggle (compact) -->
                <AutonomyToggle
                  v-if="!row.isSystemAgent"
                  :model-value="row.autonomyEnabled"
                  :show-label="false"
                  size="sm"
                  @toggle="toggleAutonomy(row.name)"
                />
              </div>

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
                <span
                  v-if="getRowSuccessPercent(row) > 0"
                  class="text-[10px] font-medium w-7 text-right"
                  :class="getSuccessBarClass(getRowSuccessPercent(row)).replace('bg-', 'text-')"
                >{{ getRowSuccessPercent(row) }}%</span>
                <span v-else class="text-[10px] text-gray-400 dark:text-gray-500 w-7 text-right">&mdash;</span>
              </div>

              <!-- Row 3: Stats (compact) -->
              <div class="flex items-center text-[10px] text-gray-500 dark:text-gray-400 gap-1">
                <template v-if="row.executionStats && row.executionStats.taskCount > 0">
                  <span class="font-medium text-gray-700 dark:text-gray-300">{{ row.executionStats.taskCount }}</span>
                  <span>tasks</span>
                  <span class="text-gray-300 dark:text-gray-600">·</span>
                  <span :class="getSuccessRateClass(row.executionStats.successRate)" class="font-medium">{{ Math.round(row.executionStats.successRate || 0) }}%</span>
                  <template v-if="row.executionStats.totalCost > 0">
                    <span class="text-gray-300 dark:text-gray-600">·</span>
                    <span class="font-medium">{{ formatCostCompact(row.executionStats.totalCost) }}</span>
                  </template>
                </template>
                <template v-else>
                  <span class="text-gray-400 dark:text-gray-500">No tasks</span>
                </template>
                <span class="flex-1"></span>
                <span class="text-gray-400">{{ row.memoryLimit || '4g' }}</span>
              </div>
            </div>

            <!-- Capacity meter — full tile height -->
            <CapacityMeter
              :active="row.slotStats ? row.slotStats.active : 0"
              :max="row.slotStats ? row.slotStats.max : 3"
              :height="52"
              :width="6"
              class="ml-1.5 flex-shrink-0 self-stretch"
            />
          </div>
        </div>

        <!-- Timeline grid area -->
        <div class="flex-1">
          <!-- Time scale header (sticky top) -->
          <div
            class="sticky top-0 bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 z-20"
            :style="{ height: headerHeight + 'px' }"
          >
            <svg :width="actualGridWidth" :height="headerHeight" class="overflow-visible">
              <g v-for="(tick, i) in timeTicks" :key="'tick-' + i">
                <line
                  :x1="tick.x"
                  y1="0"
                  :x2="tick.x"
                  :y2="headerHeight"
                  stroke="#e5e7eb"
                  stroke-width="1"
                  class="dark:stroke-gray-600"
                />
                <text
                  :x="tick.x"
                  :y="headerHeight - 4"
                  text-anchor="middle"
                  class="fill-gray-500 dark:fill-gray-400"
                  font-size="10"
                >
                  {{ tick.label }}
                </text>
              </g>
              <!-- "Now" marker in header -->
              <g v-if="nowX >= 0">
                <line
                  :x1="nowX"
                  y1="0"
                  :x2="nowX"
                  :y2="headerHeight"
                  stroke="#10b981"
                  stroke-width="2"
                />
                <text
                  :x="nowX"
                  :y="10"
                  text-anchor="middle"
                  class="fill-emerald-500 font-medium"
                  font-size="9"
                >
                  NOW
                </text>
              </g>
            </svg>
          </div>

          <!-- Grid content -->
          <svg
            :width="actualGridWidth"
            :height="filteredAgentRows.length * rowHeight"
          >
            <!-- Vertical grid lines -->
            <g v-for="(tick, i) in timeTicks" :key="'grid-' + i">
              <line
                :x1="tick.x"
                y1="0"
                :x2="tick.x"
                :y2="filteredAgentRows.length * rowHeight"
                :stroke="tick.major ? '#d1d5db' : '#e5e7eb'"
                :stroke-width="tick.major ? 1 : 0.5"
                class="dark:stroke-gray-600"
                :stroke-dasharray="tick.major ? '0' : '2,2'"
              />
            </g>

            <!-- Horizontal row separators -->
            <g v-for="(row, i) in filteredAgentRows" :key="'row-line-' + i">
              <line
                x1="0"
                :y1="(i + 1) * rowHeight"
                :x2="actualGridWidth"
                :y2="(i + 1) * rowHeight"
                stroke="#e5e7eb"
                stroke-width="1"
                class="dark:stroke-gray-700"
              />
            </g>

            <!-- Activity bars for each agent -->
            <g v-for="(row, rowIndex) in filteredAgentRows" :key="'activities-' + rowIndex">
              <rect
                v-for="(activity, actIndex) in row.activities"
                :key="'act-' + rowIndex + '-' + actIndex"
                :x="activity.x"
                :y="rowIndex * rowHeight + (rowHeight - barHeight) / 2"
                :width="activity.width"
                :height="barHeight"
                :fill="getBarColor(activity)"
                :opacity="activity.active || activity.hasError ? 1 : 0.7"
                rx="4"
                class="transition-all duration-300 cursor-pointer hover:opacity-90 hover:stroke-white hover:stroke-2"
                @click="navigateToExecution(activity)"
              >
                <title>{{ getBarTooltip(activity) }}
(Click to open in new tab)</title>
              </rect>
            </g>

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

            <!-- Communication arrows -->
            <g v-for="(arrow, i) in communicationArrows" :key="'arrow-' + i">
              <!-- Arrow line (offset outside boxes) -->
              <line
                :x1="arrow.x"
                :y1="arrow.y1Start"
                :x2="arrow.x"
                :y2="arrow.y2End"
                :stroke="arrow.hasError ? '#ef4444' : (arrow.active ? '#06b6d4' : '#67e8f9')"
                :stroke-width="arrow.active ? 1.5 : 1"
                :opacity="arrow.active || arrow.hasError ? 0.8 : 0.5"
                class="transition-all duration-300"
              />
              <!-- Arrowhead (subtle, at end of arrow) -->
              <polygon
                :points="getArrowHead(arrow)"
                :fill="arrow.hasError ? '#ef4444' : (arrow.active ? '#06b6d4' : '#67e8f9')"
                :opacity="arrow.active || arrow.hasError ? 0.8 : 0.5"
              />
            </g>

            <!-- "Now" vertical line -->
            <line
              v-if="nowX >= 0"
              :x1="nowX"
              y1="0"
              :x2="nowX"
              :y2="filteredAgentRows.length * rowHeight"
              stroke="#10b981"
              stroke-width="2"
              opacity="0.8"
            />
          </svg>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch, onMounted, onUnmounted } from 'vue'
import { formatCostCompact } from '../composables/useFormatters'
import { useRouter } from 'vue-router'
import { parseUTC, getTimestampMs, formatLocalTime } from '@/utils/timestamps'
import AutonomyToggle from './AutonomyToggle.vue'
import CapacityMeter from './CapacityMeter.vue'
import AgentAvatar from './AgentAvatar.vue'

const router = useRouter()

const props = defineProps({
  agents: { type: Array, default: () => [] },
  nodes: { type: Array, default: () => [] },
  events: { type: Array, default: () => [] },
  timelineStart: { type: String, default: null },
  timelineEnd: { type: String, default: null },
  currentEventIndex: { type: Number, default: 0 },
  totalEvents: { type: Number, default: 0 },
  totalDuration: { type: Number, default: 0 },
  replayElapsedMs: { type: Number, default: 0 },
  replaySpeed: { type: Number, default: 10 },
  isPlaying: { type: Boolean, default: false },
  contextStats: { type: Object, default: () => ({}) },
  executionStats: { type: Object, default: () => ({}) },
  isLiveMode: { type: Boolean, default: false },
  timeRangeHours: { type: Number, default: 24 },
  schedules: { type: Array, default: () => [] },
  slotStats: { type: Object, default: () => ({}) }
})

const emit = defineEmits(['play', 'pause', 'stop', 'speed-change', 'seek', 'toggle-autonomy'])

const timelineContainer = ref(null)
const hideInactiveAgents = ref(false)
const autoScrollEnabled = ref(true)
const currentNow = ref(Date.now())

// Calculate default zoom to show last 2 hours
// If timeRangeHours is 24, we want zoom to show ~2 hours, so zoom = 24/2 = 12
const defaultZoom = computed(() => {
  return Math.max(1, props.timeRangeHours / 2)
})

const zoomLevel = ref(1)

// Set initial zoom on mount
onMounted(() => {
  zoomLevel.value = defaultZoom.value

  if (props.isLiveMode) {
    startNowUpdates()
    // Auto-scroll to "now" on mount
    setTimeout(() => {
      scrollToNow()
    }, 100)
  }
})

let nowUpdateInterval = null

function startNowUpdates() {
  if (nowUpdateInterval) clearInterval(nowUpdateInterval)
  nowUpdateInterval = setInterval(() => {
    currentNow.value = Date.now()
  }, 1000)
}

function stopNowUpdates() {
  if (nowUpdateInterval) {
    clearInterval(nowUpdateInterval)
    nowUpdateInterval = null
  }
}

onUnmounted(() => {
  stopNowUpdates()
})

// Watch for new events and auto-scroll if enabled
watch(() => props.events.length, () => {
  if (props.isLiveMode && autoScrollEnabled.value) {
    scrollToNow()
  }
})

function scrollToNow() {
  if (!timelineContainer.value) return
  const container = timelineContainer.value
  // Position NOW at 90% of the visible viewport width (10% space on right)
  const viewportWidth = container.clientWidth - labelWidth
  const nowPosition = labelWidth + nowX.value
  const targetScroll = nowPosition - (viewportWidth * 0.9)
  container.scrollLeft = Math.max(0, targetScroll)
}

function jumpToNow() {
  autoScrollEnabled.value = true
  scrollToNow()
}

function handleScroll() {
  if (!timelineContainer.value || !props.isLiveMode) return
  const container = timelineContainer.value

  // Calculate the "now" scroll position (for auto-scroll toggle detection)
  const viewportWidth = container.clientWidth - labelWidth
  const nowPosition = labelWidth + nowX.value
  const nowScrollPosition = Math.max(0, nowPosition - (viewportWidth * 0.9))

  // Allow scrolling into future only if there are nearby schedules (within 2 hours)
  // Otherwise clamp to prevent going past NOW
  const maxFutureExtensionMs = 2 * 60 * 60 * 1000
  const futureLimit = currentNow.value + maxFutureExtensionMs
  const hasNearbySchedules = props.schedules && props.schedules.some(s => {
    if (!s.next_run_at || !s.enabled) return false
    const nextRunMs = getTimestampMs(s.next_run_at)
    return nextRunMs > currentNow.value && nextRunMs <= futureLimit
  })

  if (!hasNearbySchedules) {
    // No nearby schedules - clamp scroll to prevent going past NOW
    if (container.scrollLeft > nowScrollPosition + 10) {
      container.scrollLeft = nowScrollPosition
    }
  }

  // Check if we're near the "now" position to enable/disable auto-scroll
  if (Math.abs(container.scrollLeft - nowScrollPosition) > 50) {
    autoScrollEnabled.value = false
  } else {
    autoScrollEnabled.value = true
  }
}

// Layout constants - compact version
const labelWidth = 328
const headerHeight = 24
const rowHeight = 72
const barHeight = 24  // Taller bars for better visibility
const baseGridWidth = 800

const actualGridWidth = computed(() => baseGridWidth * zoomLevel.value)
// In live mode, add ~11% padding after NOW so it appears at ~90% of viewport
const futurePadding = computed(() => props.isLiveMode ? Math.max(150, actualGridWidth.value * 0.11) : 0)
const timelineWidth = computed(() => labelWidth + actualGridWidth.value + futurePadding.value)

// Zoom controls
function zoomIn() {
  zoomLevel.value = Math.min(20, zoomLevel.value + 0.5)
}

function zoomOut() {
  zoomLevel.value = Math.max(0.5, zoomLevel.value - 0.5)
}

function handleWheel(event) {
  if (event.ctrlKey || event.metaKey) {
    event.preventDefault()
    if (event.deltaY < 0) {
      zoomIn()
    } else {
      zoomOut()
    }
  }
}

// Parse timeline bounds - extend to "now" for live mode
// Use getTimestampMs to correctly parse UTC timestamps from backend
const startTime = computed(() => props.timelineStart ? getTimestampMs(props.timelineStart) : 0)
const endTime = computed(() => {
  const eventEnd = props.timelineEnd ? getTimestampMs(props.timelineEnd) : 0
  let maxEnd = eventEnd

  if (props.isLiveMode) {
    maxEnd = Math.max(maxEnd, currentNow.value)
  }

  // Extend timeline into future for schedule markers, but with a reasonable limit
  // Max 2 hours into the future to avoid breaking the scale for far-off schedules
  if (props.schedules && props.schedules.length > 0 && props.isLiveMode) {
    const maxFutureExtensionMs = 2 * 60 * 60 * 1000  // 2 hours max
    const futureLimit = currentNow.value + maxFutureExtensionMs

    const nearbyScheduleTimes = props.schedules
      .filter(s => s.next_run_at && s.enabled)
      .map(s => getTimestampMs(s.next_run_at))
      .filter(t => t <= futureLimit)  // Only consider schedules within 2 hours

    if (nearbyScheduleTimes.length > 0) {
      const furthestNearbySchedule = Math.max(...nearbyScheduleTimes)
      // Add small padding (5 min) for visual clarity
      const futurePaddingMs = 5 * 60 * 1000
      maxEnd = Math.max(maxEnd, furthestNearbySchedule + futurePaddingMs)
    }
  }

  return maxEnd
})
const duration = computed(() => endTime.value - startTime.value)

// Position of "now" on the timeline
const nowX = computed(() => {
  if (!duration.value || !startTime.value) return -1
  const now = currentNow.value
  if (now < startTime.value || now > endTime.value) return -1
  return ((now - startTime.value) / duration.value) * actualGridWidth.value
})

// Generate time ticks (15 min intervals)
const timeTicks = computed(() => {
  if (!duration.value || !startTime.value || !endTime.value) return []

  const ticks = []
  const intervalMs = 15 * 60 * 1000
  const start = startTime.value
  const end = endTime.value
  const maxTicks = 700
  let tickCount = 0
  const firstTick = Math.ceil(start / intervalMs) * intervalMs

  for (let time = firstTick; time <= end && tickCount < maxTicks; time += intervalMs) {
    const x = ((time - start) / duration.value) * actualGridWidth.value
    const date = new Date(time)
    const hours = date.getHours()
    const minutes = date.getMinutes()
    const major = minutes === 0

    ticks.push({
      x,
      time,
      label: `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}`,
      major
    })
    tickCount++
  }

  return ticks
})

// Create agent rows with full tile data matching AgentNode.vue
const agentRows = computed(() => {
  if (!props.agents.length || !startTime.value || !duration.value) return []

  // Build activity map from events
  // Only create boxes for EXECUTION events (chat_start, schedule_start)
  // Collaboration events (agent_collaboration) are only used for arrows, not boxes
  const agentActivityMap = new Map()
  props.events.forEach((event, index) => {
    // Skip collaboration events for boxes - they only create arrows
    // The target agent will have its own chat_start event with triggered_by='agent'
    if (event.activity_type === 'agent_collaboration') {
      return
    }

    // #1332: a user-cancelled terminal is not a failure. It carries
    // event.error="Execution terminated by user", which would otherwise make
    // hasError truthy and render the bar red — so detect it first and exclude
    // it from hasError, then carry isCancelled onto the bar for a distinct color.
    const isCancelled = event.status === 'cancelled'
    // Check if event has error status
    const hasError = !isCancelled && (
      event.status === 'error' || event.status === 'failed' || event.error || event.hasError
    )
    // Get duration from event (in milliseconds)
    // If null, use a default of 30 seconds for visibility (typical execution time)
    const durationMs = event.duration_ms || 30000

    if (!agentActivityMap.has(event.source_agent)) {
      agentActivityMap.set(event.source_agent, [])
    }
    const isInProgress = event.status === 'started'
    const isEstimated = !event.duration_ms

    // Add activity box for the executing agent
    // Use getTimestampMs to correctly parse UTC timestamp from backend
    const startTimestamp = getTimestampMs(event.timestamp)
    agentActivityMap.get(event.source_agent).push({
      time: startTimestamp,
      startTimestamp, // Store for dynamic duration calculation
      type: 'execution',
      eventIndex: index,
      hasError,
      isCancelled,
      durationMs,
      isInProgress,
      isEstimated,
      activityType: event.activity_type,
      triggeredBy: event.triggered_by,
      scheduleName: event.schedule_name,
      // For navigation to execution details
      executionId: event.execution_id,
      agentName: event.source_agent,
      // #514: previews + error text for hover tooltip
      commandPreview: event.command_preview || '',
      responsePreview: event.response_preview || '',
      errorText: event.error || ''
    })
  })

  // Build a map from agent name to node data for full tile info
  const nodeMap = new Map()
  props.nodes.forEach(node => {
    if (node.data && node.data.label) {
      nodeMap.set(node.data.label, node.data)
    }
  })

  return props.agents.map(agent => {
    const activities = agentActivityMap.get(agent.name) || []
    const nodeData = nodeMap.get(agent.name) || {}
    const ctxStats = props.contextStats[agent.name] || {}
    const execStats = props.executionStats[agent.name] || null
    const slotData = props.slotStats[agent.name] || null

    const bars = activities.map(act => {
      const x = ((act.time - startTime.value) / duration.value) * actualGridWidth.value
      const chronoIndex = props.events.length - 1 - act.eventIndex
      const active = chronoIndex < props.currentEventIndex

      // Calculate effective duration - for in-progress tasks, use elapsed time
      let effectiveDuration = act.durationMs
      if (act.isInProgress && act.startTimestamp) {
        // For in-progress tasks, calculate elapsed time from start
        // This will update every second as currentNow updates
        effectiveDuration = Math.max(1000, currentNow.value - act.startTimestamp)
      }

      // Calculate bar width based on duration
      // duration.value is total timeline span in ms, actualGridWidth is pixels
      const minBarWidth = 12  // Minimum width for visibility
      let barWidth = minBarWidth
      if (effectiveDuration > 0) {
        // Convert duration to pixels: (durationMs / totalMs) * gridWidth
        barWidth = Math.max(minBarWidth, (effectiveDuration / duration.value) * actualGridWidth.value)
      }

      return {
        x: Math.max(0, x),
        width: barWidth,
        active,
        type: act.type,
        hasError: act.hasError,
        // #1332: carry the cancelled flag onto the FINAL bar object — getBarColor/
        // getBarTooltip read this rendered object, not the intermediate `act`, so
        // dropping it here left the slate color + (Cancelled) tooltip dead.
        isCancelled: act.isCancelled,
        durationMs: effectiveDuration, // Use effective duration for tooltips
        isInProgress: act.isInProgress,
        isEstimated: act.isEstimated && !act.isInProgress, // Not estimated if we're calculating live
        // Activity type info for color coding
        activityType: act.activityType,
        triggeredBy: act.triggeredBy,
        scheduleName: act.scheduleName,
        // For navigation to execution details
        executionId: act.executionId,
        agentName: act.agentName,
        // #514: previews carried through to getBarTooltip
        commandPreview: act.commandPreview,
        responsePreview: act.responsePreview,
        errorText: act.errorText
      }
    })

    const hasActivity = bars.length > 0

    return {
      name: agent.name,
      status: agent.status,
      activities: bars,
      hasActivity,
      // From node data
      runtime: nodeData.runtime || null,
      isSystemAgent: nodeData.is_system === true,
      autonomyEnabled: nodeData.autonomy_enabled === true,
      githubRepo: nodeData.githubRepo || null,
      memoryLimit: nodeData.memoryLimit || '4g',
      cpuLimit: nodeData.cpuLimit || '2',
      avatarUrl: nodeData.avatarUrl || null,
      // From context/execution stats
      contextPercent: ctxStats.contextPercent || 0,
      activityState: ctxStats.activityState || (agent.status === 'running' ? 'idle' : 'offline'),
      executionStats: execStats,
      slotStats: slotData
    }
  })
})

// Filtered agent rows - system agents first
const filteredAgentRows = computed(() => {
  let rows = agentRows.value
  if (hideInactiveAgents.value) {
    rows = rows.filter(row => row.hasActivity)
  }
  // Sort: system agents first, then alphabetically
  return [...rows].sort((a, b) => {
    if (a.isSystemAgent && !b.isSystemAgent) return -1
    if (!a.isSystemAgent && b.isSystemAgent) return 1
    return a.name.localeCompare(b.name)
  })
})

// Communication arrows - only draw when BOTH source and target agents have execution boxes
const communicationArrows = computed(() => {
  if (!props.events.length || !filteredAgentRows.value.length || !startTime.value || !duration.value) return []

  const agentIndexMap = new Map()
  filteredAgentRows.value.forEach((row, i) => {
    agentIndexMap.set(row.name, i)
  })

  // Build a map of which agents have activity boxes and their time ranges
  // This helps us verify both ends of an arrow have actual boxes
  const agentActivityTimeRanges = new Map()
  filteredAgentRows.value.forEach(row => {
    if (row.activities && row.activities.length > 0) {
      const ranges = row.activities.map(act => ({
        startX: act.x,
        endX: act.x + act.width,
        startTime: startTime.value + (act.x / actualGridWidth.value) * duration.value,
        endTime: startTime.value + ((act.x + act.width) / actualGridWidth.value) * duration.value
      }))
      agentActivityTimeRanges.set(row.name, ranges)
    }
  })

  const chronoEvents = [...props.events].reverse()
  const arrowPadding = 4  // Padding outside the activity bars

  return chronoEvents.map((event, chronoIndex) => {
    // Only process collaboration events (those with target_agent)
    if (!event.target_agent) return null

    const sourceIndex = agentIndexMap.get(event.source_agent)
    const targetIndex = agentIndexMap.get(event.target_agent)

    if (sourceIndex === undefined || targetIndex === undefined) return null

    // Use getTimestampMs to correctly parse UTC timestamp from backend
    const time = getTimestampMs(event.timestamp)

    // Check if target agent has an activity box near this time
    // The target agent should have a triggered execution
    const targetRanges = agentActivityTimeRanges.get(event.target_agent)
    if (!targetRanges || targetRanges.length === 0) {
      // Target has no activity boxes at all - don't draw arrow
      return null
    }

    // Find if there's a box on target agent that could be the triggered execution
    // Look for boxes that start within ~30 seconds of the collaboration event
    const toleranceMs = 30000  // 30 second window
    const hasMatchingTargetBox = targetRanges.some(range => {
      return Math.abs(range.startTime - time) < toleranceMs
    })

    if (!hasMatchingTargetBox) {
      // No matching target box - don't draw a floating arrow
      return null
    }

    const x = ((time - startTime.value) / duration.value) * actualGridWidth.value

    // Calculate center Y for each row
    const sourceY = sourceIndex * rowHeight + rowHeight / 2
    const targetY = targetIndex * rowHeight + rowHeight / 2

    // Determine direction and calculate start/end positions outside boxes
    const goingDown = targetIndex > sourceIndex
    const barHalf = barHeight / 2

    // Start position: edge of source agent's activity bar
    const y1Start = goingDown
      ? sourceY + barHalf + arrowPadding
      : sourceY - barHalf - arrowPadding

    // End position: edge of target agent's activity bar
    const y2End = goingDown
      ? targetY - barHalf - arrowPadding
      : targetY + barHalf + arrowPadding

    const active = chronoIndex < props.currentEventIndex
    const hasError = event.status === 'error' || event.error || event.hasError

    return {
      x,
      y1: sourceY,
      y2: targetY,
      y1Start,
      y2End,
      active,
      hasError,
      direction: goingDown ? 'down' : 'up'
    }
  }).filter(a => a !== null)
})

// Schedule markers - show where enabled schedules will next run
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

// Helper functions
function navigateToAgent(agentName) {
  router.push(`/agents/${agentName}`)
}

function navigateToExecution(activity) {
  // Open execution detail page in a new tab
  const agentName = activity.agentName
  if (!agentName) return

  if (activity.executionId) {
    // Open Execution Detail page in new tab
    const route = router.resolve({
      name: 'ExecutionDetail',
      params: { name: agentName, executionId: activity.executionId }
    })
    window.open(route.href, '_blank')
  } else {
    // Fallback: open Tasks tab in new tab
    const route = router.resolve({
      path: `/agents/${agentName}`,
      query: { tab: 'tasks' }
    })
    window.open(route.href, '_blank')
  }
}

function toggleAutonomy(agentName) {
  emit('toggle-autonomy', agentName)
}

function getStatusDotColor(row) {
  if (row.activityState === 'active') return '#10b981'
  if (row.activityState === 'idle') return '#10b981'
  return '#9ca3af'
}

function getActivityStateLabel(row) {
  if (row.activityState === 'active') return 'Active'
  if (row.activityState === 'idle') return 'Idle'
  return 'Offline'
}

function getActivityStateColor(row) {
  if (row.activityState === 'active') return 'text-status-success-600 dark:text-status-success-400'
  if (row.activityState === 'idle') return 'text-status-success-600 dark:text-status-success-400'
  return 'text-gray-500 dark:text-gray-400'
}

function getRowSuccessPercent(row) {
  if (row.executionStats && row.executionStats.taskCount > 0) {
    return Math.round(row.executionStats.successRate || 0)
  }
  if (row.executionStats && (row.executionStats.taskCount7d || 0) > 0) {
    return Math.round(row.executionStats.successRate7d || 0)
  }
  return 0
}

function getSuccessBarClass(percent) {
  if (percent >= 90) return 'bg-status-success-500'
  if (percent >= 50) return 'bg-status-warning-500'
  return 'bg-status-danger-500'
}

function getSuccessRateClass(rate) {
  if (rate >= 80) return 'text-status-success-600 dark:text-status-success-400'
  if (rate >= 50) return 'text-status-warning-600 dark:text-status-warning-400'
  return 'text-status-danger-600 dark:text-status-danger-400'
}

function formatLastExecution(timestamp) {
  if (!timestamp) return ''
  // Use parseUTC to correctly parse UTC timestamp from backend
  const date = parseUTC(timestamp)
  const now = new Date()
  const diff = now - date

  if (diff < 60000) return `${Math.floor(diff / 1000)}s ago`
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`
  return `${Math.floor(diff / 86400000)}d ago`
}

function formatGithubRepo(repo) {
  if (!repo) return ''
  if (repo.startsWith('github:')) {
    return repo.substring(7)
  }
  if (repo.includes('github.com/')) {
    const parts = repo.split('github.com/')[1]
    return parts.replace(/\.git$/, '')
  }
  return repo
}

function getArrowHead(arrow) {
  const size = 4  // Subtle arrowhead
  const x = arrow.x
  const y = arrow.y2End  // Use the end position (outside box)

  if (arrow.direction === 'down') {
    // Arrow pointing down
    return `${x},${y} ${x-size},${y-size*1.5} ${x+size},${y-size*1.5}`
  } else {
    // Arrow pointing up
    return `${x},${y} ${x-size},${y+size*1.5} ${x+size},${y+size*1.5}`
  }
}

function formatDuration(ms) {
  if (!ms || ms <= 0) return 'Unknown duration'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  if (ms < 3600000) return `${Math.floor(ms / 60000)}m ${Math.floor((ms % 60000) / 1000)}s`
  return `${Math.floor(ms / 3600000)}h ${Math.floor((ms % 3600000) / 60000)}m`
}

function getBarColor(activity) {
  // #1332: user-cancelled terminal — neutral slate, distinct from the red of a
  // genuine failure. Checked before hasError/isInProgress/trigger branches.
  if (activity.isCancelled) return '#94a3b8'  // Slate for cancelled
  if (activity.hasError) return '#ef4444'  // Red for errors
  if (activity.isInProgress) return '#f59e0b'  // Amber for in-progress

  // Color by trigger type (not activity type)
  // Executions are colored by what triggered them
  const triggeredBy = activity.triggeredBy
  const activityType = activity.activityType

  // Scheduled executions → Purple
  if (activityType?.startsWith('schedule_') || triggeredBy === 'schedule') {
    return activity.active ? '#8b5cf6' : '#c4b5fd'
  }

  // Agent-triggered executions (called by another agent) → Cyan
  if (triggeredBy === 'agent') {
    return activity.active ? '#06b6d4' : '#67e8f9'
  }

  // MCP executions (user via Claude Code MCP client) → Pink/Rose
  if (triggeredBy === 'mcp') {
    return activity.active ? '#ec4899' : '#f9a8d4'
  }

  // Manual/user executions → Green
  if (triggeredBy === 'manual' || triggeredBy === 'user') {
    return activity.active ? '#22c55e' : '#86efac'
  }

  // Paid executions (Nevermined payment) → Yellow/Gold
  if (triggeredBy === 'paid') {
    return activity.active ? '#eab308' : '#fde047'
  }

  // Public link executions → Teal
  if (triggeredBy === 'public') {
    return activity.active ? '#0d9488' : '#2dd4bf'
  }

  // Default blue for unknown types
  if (activity.active) return '#3b82f6'
  return '#93c5fd'
}

function getBarTooltip(activity) {
  let prefix = ''
  let status = ''

  // Determine execution type prefix based on trigger
  const activityType = activity.activityType
  const triggeredBy = activity.triggeredBy

  if (activityType?.startsWith('schedule_') || triggeredBy === 'schedule') {
    prefix = activity.scheduleName ? `Scheduled: ${activity.scheduleName}` : 'Scheduled Task'
  } else if (triggeredBy === 'agent') {
    prefix = 'Agent-Triggered Task'
  } else if (triggeredBy === 'mcp') {
    prefix = 'MCP Task'
  } else if (triggeredBy === 'manual') {
    prefix = 'Manual Task'
  } else if (triggeredBy === 'paid') {
    prefix = 'Paid Task'
  } else if (triggeredBy === 'public') {
    prefix = 'Public Task'
  } else if (triggeredBy === 'user') {
    prefix = 'Task'
  } else {
    prefix = 'Execution'
  }

  // Determine status
  if (activity.isCancelled) status = '(Cancelled)'  // #1332
  else if (activity.hasError) status = '(Error)'
  else if (activity.isInProgress) status = '(In Progress)'
  else status = ''

  const duration = activity.isEstimated
    ? `~${formatDuration(activity.durationMs)}`
    : formatDuration(activity.durationMs)

  // Header line — same as before.
  const header = `${prefix} ${status} - ${duration}`.trim().replace('  ', ' ')

  // #514: optional command + response/error preview lines.
  const lines = [header]
  const cmd = previewLine(activity.commandPreview)
  if (cmd) lines.push(`Cmd: ${cmd}`)
  if (activity.hasError && activity.errorText) {
    lines.push(`Error: ${previewLine(activity.errorText)}`)
  } else {
    const resp = previewLine(activity.responsePreview)
    if (resp) lines.push(`Out: ${resp}`)
  }
  return lines.join('\n')
}

// #514: collapse newlines, trim, truncate to 80 chars with an ellipsis.
// Empty/whitespace-only input returns empty so the tooltip line is skipped.
function previewLine(text) {
  if (!text) return ''
  const collapsed = String(text).replace(/\s+/g, ' ').trim()
  if (!collapsed) return ''
  return collapsed.length > 80 ? collapsed.slice(0, 80) + '…' : collapsed
}

// Schedule marker helper functions
function getScheduleMarkerPoints(marker) {
  const x = marker.x
  const y = marker.rowIndex * rowHeight + 4
  // Downward-pointing triangle (▼)
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
</script>

<style scoped>
.replay-timeline {
  min-height: 400px;
}

.timeline-scroll-container::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}

.timeline-scroll-container::-webkit-scrollbar-track {
  background: #f1f5f9;
}

.dark .timeline-scroll-container::-webkit-scrollbar-track {
  background: #374151;
}

.timeline-scroll-container::-webkit-scrollbar-thumb {
  background: #cbd5e1;
  border-radius: 4px;
}

.dark .timeline-scroll-container::-webkit-scrollbar-thumb {
  background: #4b5563;
}

.timeline-scroll-container::-webkit-scrollbar-thumb:hover {
  background: #94a3b8;
}

.dark .timeline-scroll-container::-webkit-scrollbar-thumb:hover {
  background: #6b7280;
}

.timeline-scroll-container::-webkit-scrollbar-corner {
  background: #f1f5f9;
}

.dark .timeline-scroll-container::-webkit-scrollbar-corner {
  background: #374151;
}

input[type="range"] {
  -webkit-appearance: none;
  appearance: none;
  background: transparent;
  cursor: pointer;
}

input[type="range"]::-webkit-slider-runnable-track {
  background: #e5e7eb;
  height: 4px;
  border-radius: 2px;
}

.dark input[type="range"]::-webkit-slider-runnable-track {
  background: #4b5563;
}

input[type="range"]::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  margin-top: -4px;
  background-color: #3b82f6;
  height: 12px;
  width: 12px;
  border-radius: 50%;
}

/* Active pulse animation for status dot */
.active-pulse {
  animation: active-pulse-animation 0.8s ease-in-out infinite;
  box-shadow: 0 0 8px 2px rgba(16, 185, 129, 0.6);
}

@keyframes active-pulse-animation {
  0%, 100% {
    transform: scale(1);
    opacity: 1;
    box-shadow: 0 0 8px 2px rgba(16, 185, 129, 0.6);
  }
  50% {
    transform: scale(1.3);
    opacity: 0.8;
    box-shadow: 0 0 16px 4px rgba(16, 185, 129, 0.9);
  }
}
</style>
