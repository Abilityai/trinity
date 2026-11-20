<script setup>
/**
 * Agent Detail "Overview" tab — the default landing tab (#1107, rebuilt in
 * trinity-enterprise#1317).
 *
 * A page about the agent: what it is doing now, what it has done, how well,
 * what it reports about its own job, and whether it is healthy. The persistent
 * AgentHeader still owns identity, live CPU/MEM, cost, git controls and the
 * autonomy/auth chips; nothing here re-renders them.
 *
 * This file is layout and wiring only. The reads live in
 * `stores/agentOverview.js` (one source per section, each with its own
 * loaded/failed state), the decisions in `utils/overviewShape.js`, and each
 * section in `components/overview/`. Every section keeps one footprint across
 * loading, empty, failed and ready, so a new agent with nothing to show still
 * gets the whole page.
 */
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useAgentOverviewStore } from '../stores/agentOverview'
import { viewState } from '@/utils/loadingState'
import {
  attentionCount, hasRuns, measureTiles, nextRun, nowState, recentRows, scheduleRows,
} from '@/utils/overviewShape'
import OverviewNow from './overview/OverviewNow.vue'
import OverviewActivity from './overview/OverviewActivity.vue'
import OverviewRecent from './overview/OverviewRecent.vue'
import OverviewMeasureTile from './overview/OverviewMeasureTile.vue'
import OverviewReported from './overview/OverviewReported.vue'
import OverviewSchedules from './overview/OverviewSchedules.vue'
import OverviewHealth from './overview/OverviewHealth.vue'

const props = defineProps({
  agent: { type: Object, required: true },
})
const emit = defineEmits(['navigate-tab', 'open-task'])

const authStore = useAuthStore()
const store = useAgentOverviewStore()
const src = store.sources

const agentName = computed(() => props.agent?.name)
// Only admins can turn the monitoring loop on (#1409).
const isAdmin = computed(() => authStore.role === 'admin')
const canManage = computed(() => !!props.agent?.can_share && !props.agent?.is_system)

// Blank the store for a different agent BEFORE the first render: it outlives
// this component, and one frame of the previous agent's figures is the defect
// #3815 names. The same agent's data stays, so returning to the tab is instant.
if (agentName.value && store.agent !== agentName.value) store.reset(agentName.value)

const now = ref(Date.now())

const view = (key, count = 1) => viewState({ hasLoaded: src[key].hasLoaded, error: src[key].error, count })

// --- activity + measures (one read, one window) ---
const stats = computed(() => src.analytics.data)
const activity = computed(() => view('analytics', hasRuns(stats.value) ? 1 : 0))
// No runs at all, as far as the recent list knows — not merely none in this window.
const never = computed(() => src.recent.hasLoaded && (src.recent.data || []).length === 0)
const tiles = computed(() => measureTiles(stats.value, { never: never.value }))
// Labels name the window the numbers are FROM, which lags the control while a switch is out.
const shownWindow = computed(() => src.analytics.window || store.window)

// --- now ---
const nowFace = computed(() => {
  const known = props.agent?.status !== 'running' || src.recent.hasLoaded || src.live.hasLoaded
  if (!known) return null
  return nowState({
    agentStatus: props.agent?.status,
    recent: src.recent.data || [],
    live: src.live.data,
    stats: src.analytics.hasLoaded ? stats.value : null,
    now: now.value,
  })
})
const next = computed(() => nextRun(src.schedules.data || [], now.value))
const nextState = computed(() => view('schedules', (src.schedules.data || []).length).state)
const attention = computed(() => attentionCount({
  notifications: src.notifications.data,
  asks: src.asks.data,
  // D13: failed syncs count; red divergence alone does not.
  syncFailures: src.sync.data?.consecutive_failures,
}))

// --- lists ---
const recent = computed(() => recentRows(src.recent.data || [], now.value))
const recentView = computed(() => view('recent', recent.value.length))
const schedules = computed(() => scheduleRows(src.summary.data, src.schedules.data || [], { now: now.value, windowLabel: src.summary.window || store.window }))
const schedulesView = computed(() => view('summary', schedules.value.length))

// --- reported ---
const metrics = computed(() => src.metrics.data || [])
const reportedView = computed(() => view('metrics', metrics.value.filter((m) => m?.status !== 'retired').length))

// --- health ---
const healthView = computed(() => view('health'))

// --- loading and liveness ---
function load() {
  if (agentName.value) store.load(agentName.value)
}

// AgentDetail is KeepAlive'd, so timers are paired with activation, not only
// with mount: a cached page must not keep ticking or polling.
let tick = null
let poll = null
function startTimers() {
  stopTimers()
  tick = setInterval(() => { now.value = Date.now() }, 1000)
  poll = setInterval(() => {
    if (typeof document !== 'undefined' && document.hidden) return
    store.refreshLive()
  }, 30000)
}
function stopTimers() {
  if (tick) clearInterval(tick)
  if (poll) clearInterval(poll)
  tick = null
  poll = null
}

onMounted(() => {
  load()
  startTimers()
})
// Coming back to a cached page. A fresh mount already has its timers (whether
// or not Vue also fires this hook for it), so there is nothing to restart then.
onActivated(() => {
  if (tick) return
  now.value = Date.now()
  store.refreshLive()
  startTimers()
})
onDeactivated(stopTimers)
onBeforeUnmount(stopTimers)

watch(agentName, (name, previous) => {
  if (name && name !== previous) load()
})

function retry(...keys) {
  for (const key of keys) store.retry(key)
}
</script>

<template>
  <div class="space-y-4" data-testid="overview-panel">
    <OverviewNow
      :now="nowFace"
      :next="next"
      :next-state="nextState"
      :attention="attention"
      @new-task="emit('navigate-tab', 'tasks')"
      @navigate="emit('navigate-tab', $event)"
      @open-task="emit('open-task', $event)"
    />

    <div class="grid grid-cols-1 gap-4 lg:grid-cols-3">
      <OverviewActivity
        class="min-w-0 lg:col-span-2"
        :state="activity.state"
        :stale="activity.stale"
        :stats="stats"
        :window="store.window"
        :never="never"
        @update:window="store.setWindow($event)"
        @retry="retry('analytics', 'summary')"
      />
      <OverviewRecent
        class="min-w-0"
        :state="recentView.state"
        :rows="recent"
        @open-task="emit('open-task', $event)"
        @navigate="emit('navigate-tab', $event)"
        @retry="retry('recent')"
      />
    </div>

    <div class="grid grid-cols-1 gap-4 sm:grid-cols-3">
      <OverviewMeasureTile
        v-for="tile in tiles"
        :key="tile.key"
        class="min-w-0"
        :state="activity.state"
        :label="tile.label"
        :window-label="shownWindow"
        :value="tile.value"
        :sub="tile.sub"
        :series="tile.series"
        :hi="tile.hi"
        :guide="tile.guide"
        :aria="tile.aria"
      />
    </div>

    <OverviewReported
      :state="reportedView.state"
      :metrics="metrics"
      :can-open-dashboard="canManage"
      @navigate="emit('navigate-tab', $event)"
      @retry="retry('metrics')"
    />

    <div class="grid grid-cols-1 gap-4 lg:grid-cols-3">
      <OverviewSchedules
        class="min-w-0 lg:col-span-2"
        :state="schedulesView.state"
        :rows="schedules"
        @navigate="emit('navigate-tab', $event)"
        @retry="retry('summary', 'schedules')"
      />
      <OverviewHealth
        class="min-w-0"
        :state="healthView.state"
        :health="src.health.data"
        :history-loaded="src.history.hasLoaded"
        :history-failed="!!src.history.error && !src.history.hasLoaded"
        :checks="src.history.data || []"
        :sync="src.sync.data"
        :is-admin="isAdmin"
        :now="now"
        @retry="retry('health', 'history')"
      />
    </div>
  </div>
</template>
