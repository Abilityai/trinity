<template>
  <!--
    "Now" for this agent — live CPU and memory against its limits, and what it
    has cost (trinity-enterprise#887). These were the header's second and third
    rows; the decision on trinity#966 keeps the header to identity, state, the
    autonomy chip and the doors, and spend is read on Overview. Nothing here is
    a control: memory and CPU limits are set in Settings › General, funding too.

    Same footprint in every state: a running agent with no sample yet shows a
    skeleton line where the numbers will be, a stopped one its allocation.
  -->
  <div
    class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-750 rounded-lg shadow-sm px-4 py-3 space-y-2 text-[12.5px]"
    data-testid="agent-live-strip"
  >
    <div class="flex flex-wrap items-center gap-x-5 gap-y-2 min-h-[20px]">
      <template v-if="agent.status === 'running' && agentStats">
        <div class="flex items-center gap-1.5">
          <span class="text-gray-600 dark:text-gray-400">CPU</span>
          <SparklineChart :data="cpuHistory" :color="CPU_COLOR" :y-max="100" :width="40" :height="16" />
          <span class="font-mono tabular-nums w-10 text-right" :class="loadClass(agentStats.cpu_percent)">{{ agentStats.cpu_percent }}%</span>
          <span class="font-mono text-gray-600 dark:text-gray-400">/ {{ resourceLimits.current_cpu || '2' }} cores</span>
        </div>
        <div class="flex items-center gap-1.5">
          <span class="text-gray-600 dark:text-gray-400">Memory</span>
          <SparklineChart :data="memoryHistory" :color="MEMORY_COLOR" :y-max="100" :width="40" :height="16" />
          <span class="font-mono tabular-nums w-14 text-right" :class="loadClass(agentStats.memory_percent)">{{ formatBytes(agentStats.memory_used_bytes) }}</span>
          <span class="font-mono text-gray-600 dark:text-gray-400">/ {{ (resourceLimits.current_memory || '4g').toUpperCase() }}</span>
        </div>
        <span class="font-mono tabular-nums text-gray-600 dark:text-gray-400" title="Uptime">up {{ formatUptime(agentStats.uptime_seconds) }}</span>
      </template>
      <span
        v-else-if="agent.status === 'running'"
        class="block h-3 w-64 rounded bg-gray-200 dark:bg-gray-750 animate-pulse motion-reduce:animate-none"
        aria-busy="true"
      ><span class="sr-only">Loading live usage…</span></span>
      <template v-else>
        <span v-if="agent.created" class="text-gray-600 dark:text-gray-400" :title="agent.created">Created {{ formatRelativeTime(agent.created) }}</span>
        <span class="text-gray-600 dark:text-gray-400">Stopped · allocated</span>
        <span class="font-mono text-gray-700 dark:text-gray-300">{{ resourceLimits.current_cpu || '2' }} CPU</span>
        <span class="font-mono text-gray-700 dark:text-gray-300">{{ (resourceLimits.current_memory || '4g').toUpperCase() }}</span>
      </template>
    </div>

    <div
      v-if="tokenStats && tokenStats.lifetime_executions > 0"
      class="flex flex-wrap items-center gap-x-5 gap-y-1 border-t border-gray-200 dark:border-gray-750 pt-2"
      data-testid="agent-live-cost"
    >
      <!-- #471 Tier 0: subscription usage is shown as API-price equivalents,
           never presented as a bill. -->
      <BaseBadge
        v-if="isSubscriptionFunded"
        variant="purple"
        size="sm"
        :title="`Figures are API-price equivalents of subscription usage — not a bill. Funded by subscription: ${authStatus?.subscription_name || 'unknown'}`"
      >≈ API-equiv</BaseBadge>
      <div class="flex items-center gap-1.5">
        <span class="text-gray-600 dark:text-gray-400">7d</span>
        <SparklineChart :data="costSparkline" :color="COST_COLOR" :y-max="costSparklineMax" :width="56" :height="16" />
      </div>
      <div class="flex items-center gap-1">
        <span class="text-gray-600 dark:text-gray-400">Today</span>
        <span class="font-mono tabular-nums text-gray-800 dark:text-gray-200" :title="costUnreported ? UNREPORTED : null">{{ costUnreported ? '—' : costPrefix + formatCost(tokenStats.cost_24h) }}</span>
      </div>
      <span
        v-if="tokenStats.avg_daily_cost > 0"
        class="font-mono tabular-nums"
        :class="trendClass"
        :title="`7d avg: ${formatCost(tokenStats.avg_daily_cost)}/day`"
      >{{ trendArrow }} {{ formatTrendPct(tokenStats.trend_cost_pct) }} vs avg</span>
      <div class="sm:ml-auto flex flex-wrap items-center gap-1 text-gray-600 dark:text-gray-400">
        <span>Lifetime</span>
        <span class="font-mono tabular-nums text-gray-800 dark:text-gray-200" :title="costUnreported ? UNREPORTED : null">{{ costUnreported ? '—' : costPrefix + formatCost(tokenStats.lifetime_cost) }}</span>
        <span aria-hidden="true">·</span>
        <span class="font-mono tabular-nums">{{ tokenStats.lifetime_executions }} runs</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import SparklineChart from './SparklineChart.vue'
import BaseBadge from './base/BaseBadge.vue'
import { useFormatters } from '../composables'
import { formatRelativeTime } from '../utils/timestamps'
import { CANVAS_PALETTE } from '../utils/canvasPalette'

const props = defineProps({
  agent: { type: Object, required: true },
  agentStats: { type: Object, default: null },
  cpuHistory: { type: Array, default: () => [] },
  memoryHistory: { type: Array, default: () => [] },
  resourceLimits: { type: Object, default: () => ({}) },
  tokenStats: { type: Object, default: null },
  authStatus: { type: Object, default: null },
})

// uPlot strokes take a colour value, not a class; the palette module states
// why hex is unavoidable there (ent#536). Action-primary, accent-purple and
// state-autonomous — the same three families the header drew these in.
const CPU_COLOR = CANVAS_PALETTE[0]
const MEMORY_COLOR = CANVAS_PALETTE[7]
const COST_COLOR = CANVAS_PALETTE[2]
const UNREPORTED = 'Cost is not reported under this auth — token-based tracking only'

const { formatBytes, formatUptime, formatCost } = useFormatters()

function loadClass(pct) {
  if (pct > 80) return 'text-status-danger-700 dark:text-status-danger-400'
  if (pct > 50) return 'text-status-warning-700 dark:text-status-warning-400'
  return 'text-status-success-700 dark:text-status-success-400'
}

const isSubscriptionFunded = computed(() => props.authStatus?.auth_mode === 'subscription')
const costPrefix = computed(() => (isSubscriptionFunded.value ? '≈' : ''))
// A subscription that never reports cost would render `≈$0.00` — a meaningless
// number — so an honest dash instead (the header's Step-0 fork, unchanged).
const costUnreported = computed(() =>
  isSubscriptionFunded.value
  && (props.tokenStats?.lifetime_executions || 0) > 0
  && !(props.tokenStats?.lifetime_cost > 0)
)
const costSparkline = computed(() => (props.tokenStats?.daily_breakdown || []).map((d) => d.cost))
const costSparklineMax = computed(() => Math.max(...costSparkline.value, 0.0001))

function formatTrendPct(pct) {
  if (!pct) return '—'
  const abs = Math.abs(pct)
  return `${abs >= 1 ? Math.round(abs) : abs.toFixed(1)}%`
}
const trendArrow = computed(() => {
  const pct = props.tokenStats?.trend_cost_pct ?? 0
  return pct > 5 ? '↑' : pct < -5 ? '↓' : '→'
})
const trendClass = computed(() => {
  const pct = props.tokenStats?.trend_cost_pct ?? 0
  if (pct > 5) return 'text-status-warning-700 dark:text-status-warning-400'
  if (pct < -5) return 'text-status-success-700 dark:text-status-success-400'
  return 'text-gray-600 dark:text-gray-400'
})
</script>
