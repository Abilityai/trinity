<script setup>
/**
 * Agent Detail "Overview" tab (#1107) — the default landing tab.
 *
 * Deterministic, DB-sourced glance at the agent over the last few days.
 * Owns "trend over the window"; the persistent AgentHeader owns "now + cost".
 * This panel deliberately does NOT re-render the header's live CPU/MEM
 * gauges, cost cards, git controls, autonomy/read-only/auth chips, or the
 * circuit badge — where it needs one (e.g. circuit open) it links up to the
 * header control.
 *
 * Charts are window-keyed and fetched once per (agent, window) via the
 * executions store cache; nothing here polls.
 */
import { ref, computed, watch, onMounted } from 'vue'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import { useExecutionsStore } from '../stores/executions'
import StackedBarChart from './StackedBarChart.vue'
import TrendLineChart from './TrendLineChart.vue'
import CompatibilityPanel from './CompatibilityPanel.vue'

const props = defineProps({
  agent: { type: Object, required: true },
})
const emit = defineEmits(['navigate-tab', 'open-task'])

const authStore = useAuthStore()
const executionsStore = useExecutionsStore()

const agentName = computed(() => props.agent?.name)
const isRunning = computed(() => props.agent?.status === 'running')

// Shared palette — bucket order must match db `_BUCKET_ORDER` (#1107).
// An *analogous cool* ramp (indigo → violet → blue → sky → cyan → teal →
// emerald) led by the indigo `action-primary`. Deliberately no warm hues:
// mixing amber/rose with green/blue reads as a traffic light. Soft
// 400-level shades keep it calm on the dark theme; the ramp reads as one
// cohesive family rather than a categorical rainbow. One deliberate
// exception (#1150): Loops is a fuchsia accent — it sits stacked between
// Scheduled (cyan) and Agent-to-agent (teal) and the whole point of the
// bucket is distinguishing loop bursts from scheduled work.
const BUCKET_COLORS = {
  'Chat/Tasks': '#6366f1',     // indigo-500  (action-primary, anchor)
  'MCP': '#a78bfa',            // violet-400  (accent-purple)
  'Channels': '#60a5fa',       // blue-400
  'Public': '#38bdf8',         // sky-400
  'Scheduled': '#22d3ee',      // cyan-400
  'Loops': '#e879f9',          // fuchsia-400 (deliberate accent, #1150)
  'Agent-to-agent': '#2dd4bf', // teal-400
  'Voice': '#34d399',          // emerald-400
  'Other': '#94a3b8',          // slate-400
}

// Line-chart colors, also from the design-system semantic hues.
const SUCCESS_COLOR = '#22c55e'  // green-500  (status-success)
const DURATION_COLOR = '#6366f1' // indigo-500 (action-primary)
const CONTEXT_COLOR = '#38bdf8'  // sky-400
const UPTIME_COLOR = '#22c55e'   // green-500  (status-success)
const LATENCY_COLOR = '#fbbf24'  // amber-400  (status-warning family)

// --- window selector ---
const window = ref('7d')
const WINDOWS = [
  { id: '7d', label: '7d' },
  { id: '14d', label: '14d' },
  { id: '30d', label: '30d' },
]

// --- state ---
const analytics = ref(null)
const analyticsLoading = ref(false)
const info = ref(null)
const live = ref(null) // { running_count, queued_count }
const notifCount = ref(0)
const opQueuePending = ref(0)
const syncFailures = ref(0)
const health = ref(null) // AgentHealthDetail
const healthTrend = ref(null) // { dates, uptime, latency }
const schedulesCount = ref(null)
const schedulesPerf = ref(null) // #1115 per-schedule rollups (window-keyed)
const skillsCount = ref(null)
const recent = ref([])

// --- attention badge ---
const attentionCount = computed(
  () => (notifCount.value || 0) + (opQueuePending.value || 0) + (syncFailures.value || 0)
)

// --- formatters ---
function fmtDuration(ms) {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  const m = Math.floor(ms / 60000)
  const s = Math.round((ms % 60000) / 1000)
  return `${m}m ${s}s`
}
function fmtTokens(n) {
  if (n == null) return '—'
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return `${n}`
}
function fmtPct(v) {
  return v == null ? '—' : `${Math.round(v)}%`
}
function fmtDateTime(iso) {
  if (!iso) return '—'
  try { return new Date(iso).toLocaleString() } catch { return iso }
}

// --- chart data ---
const dates = computed(() => (analytics.value?.timeline || []).map((p) => p.date))

const successSeries = computed(() => [{
  label: 'Success rate',
  color: SUCCESS_COLOR,
  fill: true,
  data: (analytics.value?.timeline || []).map((p) =>
    p.success_rate == null ? null : Math.round(p.success_rate * 100)
  ),
}])

const durationSeries = computed(() => [{
  label: 'Avg duration',
  color: DURATION_COLOR,
  fill: true,
  data: (analytics.value?.timeline || []).map((p) => p.duration_avg_ms ?? null),
}])

const contextSeries = computed(() => [{
  label: 'Avg context',
  color: CONTEXT_COLOR,
  fill: true,
  data: (analytics.value?.timeline || []).map((p) => p.context_avg ?? null),
}])

const hasExecutions = computed(() => (analytics.value?.total_executions || 0) > 0)
const hasContext = computed(() =>
  (analytics.value?.timeline || []).some((p) => p.context_avg != null)
)

// health trend (clamped to ≤7d by retention — labeled in the UI)
const hasHealthTrend = computed(
  () => healthTrend.value && healthTrend.value.dates.length > 0
)
const uptimeSeries = computed(() => [{
  label: 'Uptime', color: UPTIME_COLOR, fill: true,
  data: healthTrend.value?.uptime || [],
}])
const latencySeries = computed(() => [{
  label: 'Latency', color: LATENCY_COLOR, fill: true,
  data: healthTrend.value?.latency || [],
}])

const statusColor = {
  success: 'bg-status-success-500', completed: 'bg-status-success-500',
  failed: 'bg-status-danger-500', error: 'bg-status-danger-500',
  running: 'bg-action-primary-500', queued: 'bg-status-warning-500',
  cancelled: 'bg-gray-400', skipped: 'bg-gray-400',
}
function dotColor(s) { return statusColor[s] || 'bg-gray-400' }

const healthBadge = computed(() => {
  const s = (health.value?.aggregate_status || '').toLowerCase()
  if (s === 'healthy') return { label: 'Healthy', cls: 'bg-status-success-100 dark:bg-status-success-900/50 text-status-success-700 dark:text-status-success-300' }
  if (s === 'degraded') return { label: 'Degraded', cls: 'bg-status-warning-100 dark:bg-status-warning-900/50 text-status-warning-700 dark:text-status-warning-300' }
  if (s === 'unhealthy') return { label: 'Unhealthy', cls: 'bg-status-danger-100 dark:bg-status-danger-900/50 text-status-danger-700 dark:text-status-danger-300' }
  return { label: 'Unknown', cls: 'bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400' }
})

// --- fetching ---
async function loadAnalytics() {
  if (!agentName.value) return
  analyticsLoading.value = true
  try {
    const [a, s] = await Promise.allSettled([
      executionsStore.fetchAgentAnalytics(agentName.value, window.value),
      executionsStore.fetchSchedulesSummary(agentName.value, window.value),
    ])
    analytics.value = a.status === 'fulfilled' ? a.value : null
    schedulesPerf.value = s.status === 'fulfilled' ? s.value : null
  } finally {
    analyticsLoading.value = false
  }
}

// #1115 per-schedule scorecard formatters (shared style with the Schedules tab).
function fmtSuccessRate(rate) {
  return rate == null ? '—' : `${Math.round(rate * 100)}%`
}
function successRateClass(rate) {
  if (rate == null) return 'text-gray-400 dark:text-gray-500'
  if (rate >= 0.9) return 'text-status-success-600 dark:text-status-success-400'
  if (rate >= 0.5) return 'text-status-warning-600 dark:text-status-warning-400'
  return 'text-status-danger-600 dark:text-status-danger-400'
}
// Reuses the existing `fmtDuration` defined for the Duration chart below.

async function loadSidecars() {
  const name = agentName.value
  if (!name) return
  const h = authStore.authHeader
  const get = (url, params) => axios.get(url, { params, headers: h })

  const results = await Promise.allSettled([
    get('/api/executions/stats', { agent: name }),
    get(`/api/agents/${name}/notifications/count`),
    get(`/api/operator-queue/agents/${name}`, { status: 'pending', limit: 100 }),
    get(`/api/agents/${name}/git/sync-state`),
    get(`/api/monitoring/agents/${name}`),
    get(`/api/monitoring/agents/${name}/history`, { check_type: 'network', hours: 168, limit: 1000 }),
    get(`/api/agents/${name}/schedules`),
    get(`/api/agents/${name}/skills`),
    get('/api/executions', { agent: name, limit: 5 }),
    get(`/api/agents/${name}/info`),
  ])

  const [stats, notif, opq, sync, hDetail, hHist, scheds, skills, recents, agentInfo] = results

  if (agentInfo.status === 'fulfilled') info.value = agentInfo.value.data
  if (stats.status === 'fulfilled') live.value = stats.value.data
  if (notif.status === 'fulfilled') notifCount.value = notif.value.data?.pending_count || 0
  if (opq.status === 'fulfilled') opQueuePending.value = opq.value.data?.count || 0
  if (sync.status === 'fulfilled') syncFailures.value = sync.value.data?.consecutive_failures || 0
  if (hDetail.status === 'fulfilled') health.value = hDetail.value.data
  if (hHist.status === 'fulfilled') healthTrend.value = bucketHealth(hHist.value.data?.checks || [])
  if (scheds.status === 'fulfilled') schedulesCount.value = (scheds.value.data || []).length
  if (skills.status === 'fulfilled') {
    const d = skills.value.data
    skillsCount.value = Array.isArray(d) ? d.length : (d?.skills?.length ?? null)
  }
  if (recents.status === 'fulfilled') recent.value = recents.value.data || []
}

// Bucket network health checks into per-UTC-day uptime% + avg latency.
function bucketHealth(checks) {
  if (!checks.length) return { dates: [], uptime: [], latency: [] }
  const byDay = {}
  for (const c of checks) {
    const day = (c.checked_at || '').slice(0, 10)
    if (!day) continue
    if (!byDay[day]) byDay[day] = { reach: 0, n: 0, lat: 0, latN: 0 }
    byDay[day].n += 1
    if (c.reachable) byDay[day].reach += 1
    if (c.latency_ms != null) { byDay[day].lat += c.latency_ms; byDay[day].latN += 1 }
  }
  const days = Object.keys(byDay).sort()
  return {
    dates: days,
    uptime: days.map((d) => Math.round((byDay[d].reach / byDay[d].n) * 100)),
    latency: days.map((d) => (byDay[d].latN ? Math.round(byDay[d].lat / byDay[d].latN) : null)),
  }
}

watch(window, loadAnalytics)
watch(() => agentName.value, () => { loadAnalytics(); loadSidecars() })

onMounted(() => {
  loadAnalytics()
  loadSidecars()
})
</script>

<template>
  <div class="space-y-5">
    <!-- 1. About (lead) -->
    <div class="bg-white dark:bg-gray-800 rounded-lg p-5 border border-gray-200 dark:border-gray-700">
      <div class="flex items-start justify-between gap-4">
        <div class="min-w-0">
          <h2 class="text-lg font-semibold text-gray-900 dark:text-white truncate">
            {{ info?.display_name || info?.name || agent.name }}
          </h2>
          <p v-if="info?.tagline" class="mt-0.5 text-sm text-action-primary-600 dark:text-action-primary-400 font-medium">
            {{ info.tagline }}
          </p>
          <p v-if="info?.description" class="mt-2 text-sm text-gray-600 dark:text-gray-300 line-clamp-3 whitespace-pre-line">
            {{ info.description }}
          </p>
        </div>
        <button
          class="shrink-0 text-xs font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline"
          @click="emit('navigate-tab', 'info')"
        >Full details →</button>
      </div>
      <div class="mt-4">
        <button
          class="inline-flex items-center px-3 py-1.5 text-sm font-medium rounded-md bg-action-primary-600 hover:bg-action-primary-700 text-white"
          @click="emit('navigate-tab', 'tasks')"
        >
          <svg class="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" /></svg>
          New task
        </button>
      </div>
    </div>

    <!-- 2. Needs attention (count + link only; hidden when zero) -->
    <router-link
      v-if="attentionCount > 0"
      :to="{ path: '/operations' }"
      class="flex items-center justify-between px-4 py-3 rounded-lg bg-status-warning-50 dark:bg-status-warning-900/30 border border-status-warning-200 dark:border-status-warning-800 hover:bg-status-warning-100 dark:hover:bg-status-warning-900/50 transition-colors"
    >
      <span class="flex items-center text-sm font-medium text-status-warning-800 dark:text-status-warning-300">
        <svg class="w-5 h-5 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01M5.07 19h13.86a2 2 0 001.74-3L13.74 4a2 2 0 00-3.48 0L3.34 16a2 2 0 001.73 3z" /></svg>
        {{ attentionCount }} {{ attentionCount === 1 ? 'item needs' : 'items need' }} attention
      </span>
      <span class="text-xs text-status-warning-700 dark:text-status-warning-400">View in Operations →</span>
    </router-link>

    <!-- 2b. Deployment compatibility (#668) — count + expandable checklist, auto-fix -->
    <CompatibilityPanel :agent="agent" />

    <!-- 3. Trend charts -->
    <div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
      <div class="flex items-center justify-between px-5 pt-4">
        <h3 class="text-sm font-semibold text-gray-900 dark:text-white uppercase tracking-wider">Activity trends</h3>
        <div class="flex items-center gap-2">
          <span v-if="live" class="text-xs text-gray-500 dark:text-gray-400">
            <span class="font-mono text-action-primary-600 dark:text-action-primary-400">{{ live.running_count }}</span> running ·
            <span class="font-mono text-status-warning-600 dark:text-status-warning-400">{{ live.queued_count }}</span> queued
          </span>
          <div class="inline-flex rounded-md border border-gray-200 dark:border-gray-700 overflow-hidden">
            <button
              v-for="w in WINDOWS" :key="w.id"
              @click="window = w.id"
              :class="['px-2.5 py-1 text-xs font-medium', window === w.id ? 'bg-action-primary-600 text-white' : 'bg-white dark:bg-gray-800 text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-700']"
            >{{ w.label }}</button>
          </div>
        </div>
      </div>

      <div v-if="analyticsLoading && !analytics" class="py-12 text-center">
        <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-action-primary-500 mx-auto"></div>
      </div>

      <div v-else-if="!hasExecutions" class="py-12 text-center text-sm text-gray-500 dark:text-gray-400">
        No executions in the last {{ window }}.
      </div>

      <div v-else class="p-5 grid grid-cols-1 lg:grid-cols-2 gap-6">
        <!-- executions by type -->
        <div class="lg:col-span-2">
          <div class="flex items-baseline justify-between mb-2">
            <h4 class="text-xs font-semibold text-gray-700 dark:text-gray-300">Executions by type</h4>
            <span class="text-xs text-gray-400">{{ analytics.total_executions }} total</span>
          </div>
          <StackedBarChart :data="analytics.timeline" :buckets="analytics.buckets" :colors="BUCKET_COLORS" :height="150" />
        </div>

        <!-- success rate -->
        <div>
          <div class="flex items-baseline justify-between mb-2">
            <h4 class="text-xs font-semibold text-gray-700 dark:text-gray-300">Execution success rate</h4>
            <span class="text-sm font-semibold text-status-success-600 dark:text-status-success-400">{{ Math.round(analytics.success_rate * 100) }}%</span>
          </div>
          <TrendLineChart :dates="dates" :series="successSeries" :y-min="0" :y-max="100" :value-format="(v) => (v == null ? '—' : v + '%')" :axis-format="(v) => v + '%'" />
        </div>

        <!-- duration -->
        <div>
          <div class="flex items-baseline justify-between mb-2">
            <h4 class="text-xs font-semibold text-gray-700 dark:text-gray-300">Duration</h4>
            <span class="text-xs text-gray-500 dark:text-gray-400">avg <span class="font-mono text-gray-700 dark:text-gray-200">{{ fmtDuration(analytics.duration_ms.avg) }}</span> · p95 <span class="font-mono text-gray-700 dark:text-gray-200">{{ fmtDuration(analytics.duration_ms.p95) }}</span></span>
          </div>
          <TrendLineChart :dates="dates" :series="durationSeries" :y-min="0" :value-format="(v) => fmtDuration(v)" :axis-format="(v) => fmtDuration(v)" />
        </div>

        <!-- context -->
        <div v-if="hasContext" class="lg:col-span-2">
          <div class="flex items-baseline justify-between mb-2">
            <h4 class="text-xs font-semibold text-gray-700 dark:text-gray-300">Context consumption</h4>
            <span class="text-xs text-gray-500 dark:text-gray-400">avg <span class="font-mono text-gray-700 dark:text-gray-200">{{ fmtTokens(analytics.context_avg) }}</span> tokens</span>
          </div>
          <TrendLineChart :dates="dates" :series="contextSeries" :y-min="0" :value-format="(v) => fmtTokens(v)" :axis-format="(v) => fmtTokens(v)" />
        </div>
      </div>

      <p v-if="analytics?.sampled" class="px-5 pb-3 -mt-2 text-[11px] text-gray-400">
        p95 sampled over the newest {{ analytics.sample_size }} runs.
      </p>
    </div>

    <!-- 3.5 Schedules performance (#1115) — per-schedule rollups for the
         window. Hidden when the agent has no schedules. Each row deep-links
         to the Schedules tab (the #868 per-schedule deep view). -->
    <div
      v-if="schedulesPerf && schedulesPerf.schedules.length"
      class="bg-white dark:bg-gray-800 rounded-lg p-5 border border-gray-200 dark:border-gray-700"
    >
      <div class="flex items-baseline justify-between mb-3">
        <h3 class="text-sm font-semibold text-gray-900 dark:text-white uppercase tracking-wider">Schedules performance</h3>
        <span class="text-xs text-gray-400">last {{ window }}</span>
      </div>
      <div class="divide-y divide-gray-100 dark:divide-gray-700/60">
        <button
          v-for="s in schedulesPerf.schedules"
          :key="s.schedule_id"
          @click="emit('navigate-tab', 'schedules')"
          class="w-full flex items-center gap-3 py-2.5 text-left hover:bg-gray-50 dark:hover:bg-gray-700/50 rounded px-1 -mx-1"
          :title="`Open ${s.name} in the Schedules tab`"
        >
          <div class="min-w-0 flex-1">
            <p class="text-sm font-medium text-gray-900 dark:text-white truncate">{{ s.command || s.name }}</p>
            <p class="text-xs text-gray-400 truncate font-mono">{{ s.name }} · {{ s.cron_expression }}</p>
          </div>
          <div class="shrink-0 flex items-center gap-4 text-right">
            <div class="w-14">
              <p :class="['text-sm font-semibold', successRateClass(s.success_rate)]">{{ fmtSuccessRate(s.success_rate) }}</p>
              <p class="text-[10px] text-gray-400 uppercase tracking-wide">success</p>
            </div>
            <div class="w-16 hidden sm:block">
              <p class="text-sm font-mono text-gray-700 dark:text-gray-200">{{ fmtDuration(s.avg_duration_ms) }}</p>
              <p class="text-[10px] text-gray-400 uppercase tracking-wide">avg</p>
            </div>
            <div class="w-10">
              <p class="text-sm font-mono text-gray-700 dark:text-gray-200">{{ s.total_executions }}</p>
              <p class="text-[10px] text-gray-400 uppercase tracking-wide">runs</p>
            </div>
            <div class="w-10 hidden sm:block">
              <p class="text-sm font-mono text-gray-700 dark:text-gray-200">{{ s.tool_call_total }}</p>
              <p class="text-[10px] text-gray-400 uppercase tracking-wide">tools</p>
            </div>
            <svg class="w-4 h-4 text-gray-300 dark:text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
            </svg>
          </div>
        </button>
      </div>
      <p v-if="schedulesPerf.tool_calls_sampled" class="mt-2 text-[11px] text-gray-400">
        Tool counts sampled over the newest runs.
      </p>
    </div>

    <!-- 4. Health & reliability -->
    <div class="bg-white dark:bg-gray-800 rounded-lg p-5 border border-gray-200 dark:border-gray-700">
      <h3 class="text-sm font-semibold text-gray-900 dark:text-white uppercase tracking-wider mb-3">Health &amp; reliability</h3>
      <div class="flex flex-wrap items-center gap-2 mb-4">
        <span :class="['px-2.5 py-1 text-xs font-semibold rounded-full', healthBadge.cls]">{{ healthBadge.label }}</span>
        <span v-if="health?.network" class="px-2.5 py-1 text-xs rounded-full bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300">
          {{ health.network.reachable ? 'Reachable' : 'Offline' }}
        </span>
        <span v-if="health?.docker" class="px-2.5 py-1 text-xs rounded-full bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300">
          {{ health.docker.restart_count || 0 }} restarts
        </span>
        <span v-if="health?.docker?.oom_killed" class="px-2.5 py-1 text-xs rounded-full bg-status-danger-100 dark:bg-status-danger-900/50 text-status-danger-700 dark:text-status-danger-300">OOM killed</span>
        <span v-if="health?.uptime_percent_24h != null" class="px-2.5 py-1 text-xs rounded-full bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300">
          {{ fmtPct(health.uptime_percent_24h) }} uptime (24h)
        </span>
        <span v-if="health?.circuit_breaker?.open" class="px-2.5 py-1 text-xs rounded-full bg-status-danger-100 dark:bg-status-danger-900/50 text-status-danger-700 dark:text-status-danger-300">
          Circuit open — see header
        </span>
      </div>

      <div v-if="hasHealthTrend" class="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div>
          <h4 class="text-xs font-semibold text-gray-700 dark:text-gray-300 mb-2">Uptime <span class="font-normal text-gray-400">(last 7 days)</span></h4>
          <TrendLineChart :dates="healthTrend.dates" :series="uptimeSeries" :y-min="0" :y-max="100" :height="120" :value-format="(v) => (v == null ? '—' : v + '%')" :axis-format="(v) => v + '%'" />
        </div>
        <div>
          <h4 class="text-xs font-semibold text-gray-700 dark:text-gray-300 mb-2">Latency <span class="font-normal text-gray-400">(last 7 days)</span></h4>
          <TrendLineChart :dates="healthTrend.dates" :series="latencySeries" :y-min="0" :height="120" :value-format="(v) => (v == null ? '—' : v + 'ms')" :axis-format="(v) => v + 'ms'" />
        </div>
      </div>
      <p v-else class="text-xs text-gray-400 py-1">
        No health data available — the monitoring service may be off.
      </p>
    </div>

    <!-- 5. Recent activity -->
    <div class="bg-white dark:bg-gray-800 rounded-lg p-5 border border-gray-200 dark:border-gray-700">
      <div class="flex items-center justify-between mb-3">
        <h3 class="text-sm font-semibold text-gray-900 dark:text-white uppercase tracking-wider">Recent activity</h3>
        <button class="text-xs font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline" @click="emit('navigate-tab', 'tasks')">View all →</button>
      </div>
      <div v-if="recent.length === 0" class="text-sm text-gray-400 py-2">No recent executions.</div>
      <ul v-else class="divide-y divide-gray-100 dark:divide-gray-700">
        <li v-for="r in recent" :key="r.id">
          <button class="w-full flex items-center gap-3 py-2 text-left hover:bg-gray-50 dark:hover:bg-gray-700/50 rounded px-1 -mx-1" @click="emit('open-task', r.id)">
            <span :class="['w-2 h-2 rounded-full shrink-0', dotColor(r.status)]"></span>
            <span class="flex-1 min-w-0 truncate text-sm text-gray-700 dark:text-gray-200">{{ r.message || '(no message)' }}</span>
            <span class="shrink-0 px-1.5 py-0.5 text-[10px] rounded bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400">{{ r.triggered_by }}</span>
            <span class="shrink-0 text-[11px] text-gray-400">{{ fmtDateTime(r.started_at) }}</span>
          </button>
        </li>
      </ul>
    </div>

    <!-- 6. Footprint (compact, static) -->
    <div class="bg-white dark:bg-gray-800 rounded-lg p-5 border border-gray-200 dark:border-gray-700">
      <h3 class="text-sm font-semibold text-gray-900 dark:text-white uppercase tracking-wider mb-3">Footprint</h3>
      <div class="flex flex-wrap gap-2 text-xs">
        <button class="px-2.5 py-1 rounded-md bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600" @click="emit('navigate-tab', 'schedules')">
          {{ schedulesCount ?? '—' }} schedules
        </button>
        <button class="px-2.5 py-1 rounded-md bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600" @click="emit('navigate-tab', 'skills')">
          {{ skillsCount ?? '—' }} skills
        </button>
        <button v-if="agent.can_share" class="px-2.5 py-1 rounded-md bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600" @click="emit('navigate-tab', 'sharing')">
          {{ (agent.shares && agent.shares.length) || 0 }} shares
        </button>
        <span class="px-2.5 py-1 rounded-md bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300">
          Sync: <span :class="syncFailures > 0 ? 'text-status-danger-600 dark:text-status-danger-400' : 'text-status-success-600 dark:text-status-success-400'">{{ syncFailures > 0 ? `${syncFailures} failing` : 'ok' }}</span>
        </span>
      </div>
    </div>
  </div>
</template>
