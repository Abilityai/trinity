<template>
  <div class="h-screen flex flex-col bg-gray-100 dark:bg-gray-900 overflow-hidden">
    <NavBar />

    <main class="flex-1 flex overflow-hidden">
      <!-- System Views Sidebar -->
      <SystemViewsSidebar
        @create="openCreateModal"
        @edit="openEditModal"
      />

      <div class="flex flex-col flex-1 overflow-hidden">
        <!-- Compact Header -->
        <div class="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 px-4 py-2">
          <div class="flex items-center justify-between">
            <!-- Left: Stats -->
            <div class="flex items-center min-w-0 overflow-hidden">
              <div class="flex items-center space-x-3 text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap">
                <span class="flex items-center space-x-1">
                  <span class="w-1.5 h-1.5 rounded-full bg-status-success-500"></span>
                  <span class="font-medium text-status-success-600 dark:text-status-success-400">{{ runningCount }}/{{ agents.length }}</span>
                  <span>agents</span>
                </span>
                <!-- Working-now count (trinity-enterprise#47) -->
                <span class="text-gray-300 dark:text-gray-600">·</span>
                <span class="flex items-center space-x-1">
                  <span class="font-medium text-status-info-600 dark:text-status-info-400">{{ workingNowCount }}</span>
                  <span>working now</span>
                </span>
                <span class="text-gray-300 dark:text-gray-600">·</span>
                <span class="flex items-center space-x-1">
                  <span class="font-medium text-status-info-600 dark:text-status-info-400">{{ totalCollaborationCount }}</span>
                  <span>messages ({{ timeRangeHours }}h)</span>
                </span>
                <!-- Host Telemetry (inline) -->
                <span class="text-gray-300 dark:text-gray-600">·</span>
                <HostTelemetry />
              </div>
            </div>

            <!-- Right: Controls -->
            <div class="flex items-center space-x-2 flex-shrink-0">
              <!-- Quick Tag Filter Dropdown -->
              <div v-if="availableTags.length > 0" class="relative">
                <button
                  @click="showTagDropdown = !showTagDropdown"
                  :class="[
                    'flex items-center space-x-1 px-2 py-0.5 rounded text-xs font-medium transition-all whitespace-nowrap',
                    selectedQuickTags.length > 0
                      ? 'bg-blue-600 text-white'
                      : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
                  ]"
                >
                  <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A2 2 0 013 12V7a4 4 0 014-4z"/>
                  </svg>
                  <span class="whitespace-nowrap">{{ selectedQuickTags.length > 0 ? selectedQuickTags.length + ' tag' + (selectedQuickTags.length > 1 ? 's' : '') : 'Tags' }}</span>
                  <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                  </svg>
                </button>
                <div
                  v-if="showTagDropdown"
                  class="absolute right-0 top-full mt-1 bg-white dark:bg-gray-800 rounded-lg shadow-lg border border-gray-200 dark:border-gray-700 py-1 z-50 max-h-48 overflow-y-auto min-w-36"
                >
                  <button
                    v-if="selectedQuickTags.length > 0"
                    @click="clearQuickTags(); showTagDropdown = false"
                    class="w-full px-3 py-1.5 text-left text-xs text-status-danger-600 dark:text-status-danger-400 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors border-b border-gray-200 dark:border-gray-700"
                  >
                    Clear all
                  </button>
                  <button
                    v-for="tagInfo in availableTags"
                    :key="tagInfo.tag"
                    @click="toggleQuickTag(tagInfo.tag)"
                    :class="[
                      'w-full px-3 py-1.5 text-left text-xs hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors flex items-center justify-between',
                      selectedQuickTags.includes(tagInfo.tag) ? 'text-blue-600 dark:text-blue-400 font-medium' : 'text-gray-700 dark:text-gray-300'
                    ]"
                  >
                    <span>#{{ tagInfo.tag }}</span>
                    <span class="text-gray-400 dark:text-gray-500 text-[10px]">{{ tagInfo.count }}</span>
                  </button>
                </div>
              </div>

              <!-- Owner filter dropdown -->
              <select
                v-if="availableOwners.length > 1"
                v-model="selectedOwner"
                @change="onOwnerFilterChange"
                class="text-xs border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-700 dark:text-gray-200 focus:outline-none focus:ring-1 focus:ring-blue-500"
              >
                <option value="">All Owners</option>
                <option v-for="owner in availableOwners" :key="owner.name || '__unassigned__'" :value="owner.name || '__unassigned__'">
                  {{ owner.name || 'Unassigned' }} ({{ owner.count }})
                </option>
              </select>

              <span v-if="availableTags.length > 0 || availableOwners.length > 1" class="text-gray-300 dark:text-gray-600">|</span>

              <!-- Mode Toggle (Grid / Timeline — trinity-enterprise#47; Graph decommissioned #1689) -->
              <div class="flex rounded-md border border-gray-300 dark:border-gray-600 p-0.5 bg-gray-50 dark:bg-gray-700">
                <button
                  v-for="mode in ['grid', 'timeline']"
                  :key="mode"
                  @click="toggleMode(mode)"
                  :class="[
                    'px-2 py-1 rounded text-xs font-medium transition-all capitalize',
                    viewMode === mode ? 'bg-blue-600 text-white' : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'
                  ]"
                >
                  {{ mode }}
                </button>
              </div>

              <!-- Grid-mode controls (trinity-enterprise#47) -->
              <template v-if="viewMode === 'grid'">
                <button
                  @click="fleetGridRef?.tidyUp()"
                  class="text-xs border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-gray-100 transition-colors"
                  title="Compact tiles row-by-row, preserving reading order"
                >
                  Tidy up
                </button>
                <button
                  @click="fleetGridRef?.resetToDefault()"
                  class="text-xs border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-gray-100 transition-colors"
                  title="Restore the default tile layout"
                >
                  Reset
                </button>
              </template>

              <!-- Time Range -->
              <select
                v-model="selectedTimeRange"
                @change="onTimeRangeChange"
                class="text-xs border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-700 dark:text-gray-200 focus:outline-none focus:ring-1 focus:ring-blue-500"
              >
                <option :value="1">1h</option>
                <option :value="6">6h</option>
                <option :value="24">24h</option>
                <option :value="72">3d</option>
                <option :value="168">7d</option>
              </select>

              <!-- Connection indicator -->
              <div
                :class="[
                  'w-2 h-2 rounded-full',
                  isConnected ? 'bg-status-success-500' : 'bg-status-danger-500'
                ]"
                :title="isConnected ? 'Connected' : 'Disconnected'"
              ></div>

              <!-- Loading -->
              <svg v-if="isLoadingHistory" class="animate-spin h-4 w-4 text-blue-600 dark:text-blue-400" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
              </svg>

              <!-- Refresh -->
              <button
                @click="refreshAll"
                :disabled="isLoadingHistory"
                class="p-1.5 text-gray-500 dark:text-gray-400 hover:text-blue-600 dark:hover:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/30 rounded transition-colors disabled:opacity-50"
                title="Refresh"
              >
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
              </button>
            </div>
          </div>
        </div>

    <!-- Timeline View (only visible in timeline mode) -->
    <template v-if="isTimelineMode">
      <!-- Loading skeleton (#1266): immediate feedback while fleet/timeline data loads -->
      <div
        v-if="isFleetLoading && agents.length === 0"
        class="flex-1 min-h-0 overflow-hidden bg-white dark:bg-gray-800 px-4 py-4"
      >
        <SkeletonLoader variant="rows" :count="8" height="2.5rem" gap="0.5rem" />
      </div>
      <!-- Error state (#1266): distinct from an empty timeline / infinite skeleton -->
      <div
        v-else-if="fleetLoadError && agents.length === 0"
        class="flex-1 min-h-0 flex flex-col items-center justify-center text-center px-4"
      >
        <p class="text-sm text-gray-600 dark:text-gray-300">Couldn't load timeline data.</p>
        <button
          @click="refreshAll"
          class="mt-2 text-sm font-medium text-blue-600 dark:text-blue-400 hover:underline"
        >Retry</button>
      </div>
      <ReplayTimeline
        v-else
        :agents="agents"
        :nodes="nodes"
        :events="historicalCollaborations"
        :timeline-start="timelineStart"
      :timeline-end="timelineEnd"
      :current-event-index="currentEventIndex"
      :total-events="totalEvents"
      :total-duration="totalDuration"
      :replay-elapsed-ms="replayElapsedMs"
      :replay-speed="replaySpeed"
      :is-playing="isPlaying"
      :context-stats="contextStats"
      :execution-stats="executionStats"
      :is-live-mode="true"
      :time-range-hours="selectedTimeRange"
      :schedules="schedules"
      :slot-stats="slotStats"
      @play="handlePlay"
      @pause="handlePause"
      @stop="handleStop"
      @speed-change="handleSpeedChange"
      @toggle-autonomy="handleToggleAutonomy"
      />
    </template>

    <!-- Grid View (trinity-enterprise#47) — magnetic tile canvas. v-if so its
         polling and timers tear down whenever the mode is not active. -->
    <div v-if="viewMode === 'grid'" class="relative bg-white dark:bg-gray-800 shadow-sm dark:shadow-gray-900 flex-1 min-h-0">
      <!-- Loading skeleton: immediate feedback while the fleet list loads -->
      <div
        v-if="isFleetLoading && agents.length === 0"
        class="absolute inset-0 flex items-center justify-center"
      >
        <SkeletonLoader variant="nodes" :count="5" />
      </div>
      <!-- Error state -->
      <div
        v-else-if="fleetLoadError && agents.length === 0"
        class="absolute inset-0 flex items-center justify-center"
      >
        <div class="text-center">
          <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">Couldn't load agents</h3>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Something went wrong fetching the fleet.</p>
          <button
            @click="refreshAll"
            class="mt-4 inline-flex items-center px-4 py-2 border border-transparent shadow-sm text-sm font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700"
          >Retry</button>
        </div>
      </div>
      <!-- Empty state -->
      <div
        v-else-if="gridAgents.length === 0"
        class="absolute inset-0 flex items-center justify-center"
      >
        <div class="text-center">
          <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">No agents yet</h3>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Launch your first agent in a couple of clicks.</p>
          <button
            @click="openOnboarding"
            class="mt-4 inline-flex items-center px-4 py-2 border border-transparent shadow-sm text-sm font-medium rounded-md text-white bg-blue-600 hover:bg-blue-700"
          >
            Get started
          </button>
        </div>
      </div>
      <FleetGrid v-else ref="fleetGridRef" :agents="gridAgents" />
    </div>

      </div>
    </main>

    <!-- System View Editor Modal -->
    <SystemViewEditor
      :is-open="isEditorOpen"
      :editing-view="editingView"
      @close="closeEditor"
      @saved="onViewSaved"
    />

    <!-- First-run onboarding wizard (trinity-enterprise#52) -->
    <OnboardingWizard
      v-if="showOnboarding"
      :claude-auth-configured="sessionsStore.claudeAuthConfigured"
      @close="closeOnboarding"
      @deployed="onAgentDeployed"
    />
  </div>
</template>

<script setup>
import NavBar from '@/components/NavBar.vue'
import HostTelemetry from '@/components/HostTelemetry.vue'
import ReplayTimeline from '@/components/ReplayTimeline.vue'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import SystemViewsSidebar from '@/components/SystemViewsSidebar.vue'
import SystemViewEditor from '@/components/SystemViewEditor.vue'
import OnboardingWizard from '@/components/OnboardingWizard.vue'
import { useSessionsStore } from '@/stores/sessions'
import axios from 'axios'
import { ref, onMounted, onUnmounted, computed, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useNetworkStore } from '@/stores/network'
import { useSystemViewsStore } from '@/stores/systemViews'
import { storeToRefs } from 'pinia'
import FleetGrid from '@/components/FleetGrid.vue'
import { isOrgTag } from '@/utils/gridOrg'
import { useNotification } from '@/composables/useNotification'

const networkStore = useNetworkStore()
const systemViewsStore = useSystemViewsStore()
const sessionsStore = useSessionsStore()
const route = useRoute()

// First-run onboarding (trinity-enterprise#52). Auto-opens once for a fresh
// install with zero agents; dismissal is remembered so it never nags.
const ONBOARDING_DISMISSED_KEY = 'trinity_onboarding_dismissed_v1'
const showOnboarding = ref(false)
function openOnboarding() {
  showOnboarding.value = true
}
function closeOnboarding() {
  showOnboarding.value = false
  try { localStorage.setItem(ONBOARDING_DISMISSED_KEY, '1') } catch { /* ignore */ }
}
function onAgentDeployed() {
  // Agent was created via the wizard's real create modal. Keep the wizard open
  // (it advances to the credential step on its own) and refresh the fleet so
  // the new agent appears on the graph without a manual page reload — the
  // WebSocket agent_created event can lag while the container spins up.
  networkStore.fetchAgents()
}
function maybeAutoOpenOnboarding() {
  if (showOnboarding.value) return
  // Explicit ?onboarding=1 re-opens the wizard any time (re-run / QA preview),
  // regardless of fleet size or prior dismissal.
  if (route.query.onboarding === '1') {
    showOnboarding.value = true
    return
  }
  if (isFleetLoading.value) return
  // Count only user-created agents — `trinity-system` exists on every install,
  // so counting it would mean a fresh fleet is never "empty" and auto-open
  // would never fire.
  if (agents.value.filter(a => !a.is_system).length > 0) return
  if (localStorage.getItem(ONBOARDING_DISMISSED_KEY) === '1') return
  showOnboarding.value = true
}

// System View Editor Modal State
const isEditorOpen = ref(false)
const editingView = ref(null)

function openCreateModal() {
  editingView.value = null
  isEditorOpen.value = true
}

function openEditModal(view) {
  editingView.value = view
  isEditorOpen.value = true
}

function closeEditor() {
  isEditorOpen.value = false
  editingView.value = null
}

async function onViewSaved() {
  // Refresh views after save
  await systemViewsStore.fetchViews()
}

const {
  agents,
  nodes,
  edges,
  collaborationHistory,
  isConnected,
  activeCollaborationCount,
  lastEventTimeFormatted,
  historicalCollaborations,
  totalCollaborationCount,
  timeRangeHours,
  isLoadingHistory,
  loading: isFleetLoading,
  loadError: fleetLoadError,
  contextStats,
  executionStats,
  slotStats,
  workingState,
  schedules,
  // Timeline/Replay state
  viewMode,
  isTimelineMode,
  isPlaying,
  replaySpeed,
  currentEventIndex,
  replayElapsedMs,
  totalEvents,
  totalDuration,
  playbackPosition,
  timelineStart,
  timelineEnd,
  currentTime
} = storeToRefs(networkStore)

// Persisted state: Time range (default 24h, persisted to localStorage)
const savedTimeRange = localStorage.getItem('trinity-dashboard-time-range')
const selectedTimeRange = ref(savedTimeRange ? parseInt(savedTimeRange) : 24)


// Quick Tag Filter state (persisted to localStorage when not using System View)
const availableTags = ref([])
const savedQuickTags = localStorage.getItem('trinity-dashboard-quick-tags')
const selectedQuickTags = ref(savedQuickTags ? JSON.parse(savedQuickTags) : [])
const showTagDropdown = ref(false)

// Owner filter state (persisted to localStorage, synced with network store)
const selectedOwner = ref(networkStore.filterOwner || '')

// Derive distinct owners from the full (unfiltered) agent list
const availableOwners = computed(() => {
  const allAgents = agents.value
  const counts = {}
  for (const agent of allAgents) {
    const owner = agent.owner || null
    counts[owner] = (counts[owner] || 0) + 1
  }
  return Object.entries(counts)
    .map(([name, count]) => ({ name: name === 'null' ? null : name, count }))
    .sort((a, b) => {
      if (a.name === null) return 1
      if (b.name === null) return -1
      return a.name.localeCompare(b.name)
    })
})

function onOwnerFilterChange() {
  networkStore.setFilterOwner(selectedOwner.value)
}

// Computed: First 5 tags for inline display
const displayedTags = computed(() => availableTags.value.slice(0, 5))

// Watch for active view changes and update network filter
const { activeFilterTags, activeViewId } = storeToRefs(systemViewsStore)
watch(activeFilterTags, (tags) => {
  networkStore.setFilterTags(tags)
  // Sync quick tags with system view selection
  if (activeViewId.value) {
    selectedQuickTags.value = [...tags]
    // Clear persisted quick tags when using a system view
    localStorage.removeItem('trinity-dashboard-quick-tags')
  }
}, { immediate: true })


// Computed stats
const runningCount = computed(() => {
  return agents.value.filter(a => a.status?.toLowerCase() === 'running').length
})

// Grid view (trinity-enterprise#47)
const fleetGridRef = ref(null)

// Agents shown on the grid: same owner filter the graph applies to nodes
// (the tag filter is already applied server-side by fetchAgents).
const gridAgents = computed(() => {
  if (!selectedOwner.value) return agents.value
  const owner = selectedOwner.value === '__unassigned__' ? null : selectedOwner.value
  return agents.value.filter(a => (a.owner || null) === owner)
})

// Agents executing right now: WS-observed in-flight work unioned with the
// polled context-stats activity state.
const workingNowCount = computed(() => {
  const working = new Set(Object.keys(workingState.value))
  for (const [name, s] of Object.entries(contextStats.value)) {
    if (s.activityState === 'active') working.add(name)
  }
  const names = new Set(agents.value.map(a => a.name))
  return [...working].filter(n => names.has(n)).length
})

const stoppedCount = computed(() => {
  return agents.value.filter(a => a.status?.toLowerCase() !== 'running').length
})

onMounted(async () => {
  // Initialize system views store (restores persisted view selection)
  systemViewsStore.initialize()

  // Apply persisted time range to network store
  networkStore.timeRangeHours = selectedTimeRange.value

  // Apply persisted quick tags filter (only if no system view is active)
  if (!systemViewsStore.activeViewId && selectedQuickTags.value.length > 0) {
    networkStore.setFilterTags([...selectedQuickTags.value])
  }

  // PERF-269: Parallelize independent mount calls
  await Promise.allSettled([
    systemViewsStore.fetchViews(),
    networkStore.fetchAgents(),
    networkStore.fetchHistoricalCommunications(),
    networkStore.fetchSchedules(),
    fetchAvailableTags()
  ])

  // First-run onboarding: load the Claude-auth flag (for the wizard's setup
  // hint) and auto-open the wizard if this is a fresh, empty install.
  sessionsStore.loadFeatureFlags().catch(() => {})
  maybeAutoOpenOnboarding()

  // Connect WebSocket for real-time updates
  networkStore.connectWebSocket()

  // Start polling (PERF-269: reduced frequencies, visibility-aware)
  networkStore.startContextPolling()
  networkStore.startAgentRefresh()

  // Start activity refresh polling if in timeline mode (fallback for WebSocket gaps)
  if (networkStore.isTimelineMode) {
    networkStore.startActivityRefresh()
  }

  // Add click outside listener for tag dropdown
  document.addEventListener('click', handleClickOutside)
})

onUnmounted(() => {
  networkStore.disconnectWebSocket()
  networkStore.stopContextPolling()
  networkStore.stopAgentRefresh()
  networkStore.stopActivityRefresh()
  document.removeEventListener('click', handleClickOutside)
})

async function refreshAll() {
  await networkStore.fetchAgents()
  await networkStore.fetchHistoricalCommunications()
  if (networkStore.viewMode === 'grid') {
    // Grid mode: re-pull chip batch data + re-hydrate visible tiles.
    fleetGridRef.value?.refresh()
  }
  // Timeline mode needs nothing extra — the two fetches above feed it.
}

async function onTimeRangeChange() {
  networkStore.timeRangeHours = selectedTimeRange.value
  // Persist time range to localStorage
  localStorage.setItem('trinity-dashboard-time-range', selectedTimeRange.value)
  await networkStore.fetchHistoricalCommunications()
}


function getNodeColor(node) {
  // System agent gets purple color
  if (node.data?.is_system) {
    return '#a855f7' // purple-500
  }

  const status = node.data?.status?.toLowerCase() || 'stopped'

  const colors = {
    running: '#06b6d4', // cyan-500
    stopped: '#94a3b8', // slate-400
    starting: '#f59e0b', // amber-500
    error: '#ef4444', // red-500
    exited: '#6b7280' // gray-500
  }

  return colors[status] || colors.stopped
}

function formatTime(timestamp) {
  const date = new Date(timestamp)
  const now = new Date()
  const diff = now - date

  if (diff < 60000) return `${Math.floor(diff / 1000)}s`
  if (diff < 3600000) return `${Math.floor(diff / 60000)}m`
  return date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
}

// View Mode Functions
function toggleMode(mode) {
  networkStore.setViewMode(mode)
}

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

async function handleToggleAutonomy(agentName) {
  await networkStore.toggleAutonomy(agentName)
}

function handleTimelineClick(event) {
  const rect = event.currentTarget.getBoundingClientRect()
  const clickX = event.clientX - rect.left
  const timelineWidth = rect.width
  networkStore.handleTimelineClick(clickX, timelineWidth)
}

function formatDuration(ms) {
  if (!ms) return '00:00'
  const minutes = Math.floor(ms / 60000)
  const seconds = Math.floor((ms % 60000) / 1000)
  return `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`
}

function formatTimestamp(timestamp) {
  if (!timestamp) return '--:--'
  const date = new Date(timestamp)
  return date.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
}

// Quick Tag Filter functions
async function fetchAvailableTags() {
  try {
    const response = await axios.get('/api/tags')
    // Org-overlay namespaces (dept-*/reports-to-*) are structural facts, not
    // browse filters — hidden here; the Grid renders them as zones/lines and
    // the AgentDetail tag editor still shows them (trinity-enterprise#305).
    availableTags.value = (response.data.tags || []).filter((t) => !isOrgTag(t.tag))
  } catch (err) {
    console.error('Failed to fetch tags:', err)
    availableTags.value = []
  }
}

function toggleQuickTag(tag) {
  const index = selectedQuickTags.value.indexOf(tag)
  if (index === -1) {
    selectedQuickTags.value.push(tag)
  } else {
    selectedQuickTags.value.splice(index, 1)
  }
  // Clear system view selection when using quick tags
  systemViewsStore.clearSelection()
  // Apply filter to network store
  networkStore.setFilterTags([...selectedQuickTags.value])
  // Persist quick tags to localStorage
  localStorage.setItem('trinity-dashboard-quick-tags', JSON.stringify(selectedQuickTags.value))
}

function clearQuickTags() {
  selectedQuickTags.value = []
  networkStore.setFilterTags([])
  // Clear persisted quick tags
  localStorage.removeItem('trinity-dashboard-quick-tags')
}

// Close dropdown when clicking outside
function handleClickOutside(event) {
  if (showTagDropdown.value && !event.target.closest('.relative')) {
    showTagDropdown.value = false
  }
}
</script>

<style scoped>

/* Replay Mode Styles */
.mode-toggle {
  transition: all 0.2s ease;
}

.mode-toggle button {
  transition: all 0.2s ease;
}

/* Timeline Scrubber Styles */
.timeline-scrubber {
  user-select: none;
}

.timeline-track {
  position: relative;
  transition: background-color 0.2s ease;
}

.timeline-track:hover {
  background-color: #e5e7eb;
}

.event-marker {
  transition: all 0.15s ease;
  z-index: 10;
}

.event-marker:hover {
  z-index: 20;
  transform: translate(-50%, -50%) scale(1.3);
}

.playback-marker {
  z-index: 30;
  pointer-events: none;
  transition: left 0.3s ease-out;
}

.playback-marker > div {
  pointer-events: all;
  transition: all 0.2s ease;
}

.playback-marker > div:hover {
  transform: translate(-50%, -50%) scale(1.2);
}

/* Replay controls hover effects */
button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

button:not(:disabled):hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
}

button:not(:disabled):active {
  transform: translateY(0);
}

/* Loading animation for timeline */
@keyframes timeline-pulse {
  0%, 100% {
    opacity: 1;
  }
  50% {
    opacity: 0.5;
  }
}

.timeline-loading {
  animation: timeline-pulse 2s ease-in-out infinite;
}
</style>
