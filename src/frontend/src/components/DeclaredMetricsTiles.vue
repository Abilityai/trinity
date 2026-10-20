<template>
  <section class="space-y-4" data-testid="declared-metrics">
    <!-- ent#253: a failed refresh keeps the tiles on screen and says so. It
         never replaces a rendered number with "no points yet" — that is a
         claim about the agent produced by a failed request. -->
    <InlineError
      v-if="view.stale"
      :message="staleMessage"
      :detail="loadError"
      retryable
      :retry-label="loading ? 'Retrying…' : 'Try again'"
      @retry="load"
      @dismiss="loadError = ''"
    />

    <div class="flex items-center justify-between gap-3">
      <div>
        <h3 class="text-sm font-semibold uppercase tracking-wider text-gray-900 dark:text-white">
          Declared Metrics
        </h3>
        <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
          From <code class="rounded bg-gray-100 px-1 py-0.5 dark:bg-gray-700">template.yaml</code>
          <span class="mx-1">·</span>
          <span :title="STALE_RULE_TITLE">stale = no point within 2× cadence</span>
        </p>
      </div>
      <div class="flex items-center gap-2">
        <BaseSelect
          v-model="selectedWindow"
          variant="ghost"
          aria-label="Metric window"
          data-testid="metric-window"
        >
          <option v-for="opt in WINDOW_OPTIONS" :key="opt.value" :value="opt.value">
            {{ opt.label }}
          </option>
        </BaseSelect>
        <button
          type="button"
          class="rounded p-1.5 text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-700"
          title="Refresh metrics"
          :disabled="loading"
          data-testid="declared-metrics-refresh"
          @click="load"
        >
          <svg :class="['w-4 h-4', loading ? 'animate-spin motion-reduce:animate-none' : '']" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
        </button>
      </div>
    </div>

    <!-- First load: a skeleton with the tiles' own footprint (p4, p12) — a
         panel, not a chart, so no scanline. -->
    <div
      v-if="view.state === 'loading'"
      class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3"
      aria-busy="true"
      data-testid="declared-metrics-skeleton"
    >
      <span class="sr-only">Loading declared metrics…</span>
      <div
        v-for="n in 3"
        :key="n"
        class="h-28 animate-pulse rounded-lg border border-gray-200 bg-gray-100 motion-reduce:animate-none dark:border-gray-750 dark:bg-gray-800"
      ></div>
    </div>

    <LoadFailed
      v-else-if="view.state === 'failed'"
      title="Couldn't load the declared metrics"
      message="The metric store did not answer. This is not the same as an agent with no metrics."
      :detail="loadError"
      :retrying="loading"
      @retry="load"
    />

    <!-- A fetch that SUCCEEDED and returned zero declarations (p15/p16). -->
    <div
      v-else-if="metrics.length === 0"
      class="rounded-lg border border-dashed border-gray-300 p-6 text-center dark:border-gray-700"
      data-testid="declared-metrics-empty"
    >
      <p class="text-sm text-gray-600 dark:text-gray-300">
        {{ payload?.message || 'No metrics are declared for this agent.' }}
      </p>
    </div>

    <template v-else>
      <!-- The superseded-`metrics.json` finding (D-010), echoed by the read.
           Never silent: an agent still writing that file has numbers nobody
           serves, and the tile grid alone would not explain why. -->
      <div
        v-for="finding in findings"
        :key="finding.code"
        class="rounded-md border border-state-autonomous-200 bg-state-autonomous-50 p-3 dark:border-state-autonomous-800 dark:bg-state-autonomous-900/20"
        data-testid="declared-metrics-finding"
      >
        <p class="text-sm font-medium text-state-autonomous-800 dark:text-state-autonomous-200">
          {{ finding.message }}
        </p>
        <p
          v-if="findingDetail(finding)"
          class="mt-1 text-xs text-state-autonomous-700 dark:text-state-autonomous-300"
          data-testid="declared-metrics-finding-detail"
        >
          {{ findingDetail(finding) }}
        </p>
      </div>

      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <div
          v-for="metric in metrics"
          :key="metric.name"
          class="rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-750 dark:bg-gray-800"
          data-testid="metric-tile"
          :data-metric="metric.name"
        >
          <!-- Value: a status metric reads as its declared badge, everything
               else as a number in its declared type's units. -->
          <div class="flex items-start justify-between gap-2">
            <div class="min-w-0">
              <span
                v-if="metric.type === 'status' && metric.latest"
                class="inline-flex items-center rounded-full px-2.5 py-1 text-sm font-medium"
                :class="statusBadgeClasses(statusColor(metric))"
                data-testid="metric-value"
              >{{ metric.latest.value }}</span>
              <div
                v-else
                class="text-2xl font-bold tabular-nums"
                :class="valueClasses(metric)"
                data-testid="metric-value"
              >
                {{ formatMetricValue(metric.latest?.value, metric.type) }}<span
                  v-if="unitSuffix(metric)"
                  class="ml-1 text-base text-gray-400 dark:text-gray-500"
                >{{ unitSuffix(metric) }}</span>
              </div>
            </div>
            <div
              v-if="metric.stats?.trend && metric.stats.trend !== 'stable'"
              class="flex shrink-0 items-center text-sm"
              :class="trendClasses(metric.stats.trend, metric.direction)"
              data-testid="metric-trend"
            >
              <svg v-if="metric.stats.trend === 'up'" class="h-4 w-4" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M5.293 9.707a1 1 0 010-1.414l4-4a1 1 0 011.414 0l4 4a1 1 0 01-1.414 1.414L11 7.414V15a1 1 0 11-2 0V7.414L6.707 9.707a1 1 0 01-1.414 0z" clip-rule="evenodd" />
              </svg>
              <svg v-else class="h-4 w-4" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M14.707 10.293a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 111.414-1.414L9 12.586V5a1 1 0 012 0v7.586l2.293-2.293a1 1 0 011.414 0z" clip-rule="evenodd" />
              </svg>
              <span v-if="metric.stats.trend_percent" class="ml-1 tabular-nums">
                {{ metric.stats.trend_percent > 0 ? '+' : '' }}{{ metric.stats.trend_percent }}%
              </span>
            </div>
          </div>

          <div class="mt-1 text-sm text-gray-700 dark:text-gray-300">{{ metric.label || metric.name }}</div>
          <div v-if="metric.description" class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            {{ metric.description }}
          </div>

          <SparklineChart
            v-if="sparkline(metric).length > 1"
            :data="sparkline(metric)"
            :color="sparklineColor(metric.stats?.trend, metric.direction)"
            :y-max="sparklineMax(sparkline(metric), metric.stats)"
            :width="140"
            :height="26"
            class="mt-2"
          />

          <!-- Freshness. EVERY tile states when its point was recorded — a
               number with no time is a claim about now, and the whole reason
               this surface exists is that it often is not. -->
          <div class="mt-3 flex flex-wrap items-center gap-2 text-xs">
            <span
              v-if="metric.last_point_at"
              class="text-gray-500 dark:text-gray-400"
              :title="absoluteTime(metric.last_point_at)"
              data-testid="metric-point-time"
            >{{ relativeTime(metric.last_point_at) }}</span>
            <span
              v-if="chip(metric)"
              class="inline-flex items-center rounded-full px-2 py-0.5 font-medium"
              :class="chip(metric).tone"
              :title="chip(metric).title"
              data-testid="metric-freshness-chip"
            >{{ chip(metric).label }}</span>
            <span
              v-if="metric.series_count > 1"
              class="inline-flex items-center rounded-full bg-gray-100 px-2 py-0.5 text-gray-600 dark:bg-gray-700 dark:text-gray-300"
              :title="seriesTitle(metric)"
              data-testid="metric-series-chip"
            >{{ metric.series_count }} series · {{ metric.aggregation || 'last' }}</span>
            <!-- The chart and the number must describe the same thing. When
                 the aggregation has no cross-series fold the chart is ONE
                 series, and this says which rather than letting the trend
                 arrow be read as the whole metric's. -->
            <span
              v-if="chartNote(metric)"
              class="inline-flex items-center rounded-full bg-gray-100 px-2 py-0.5 text-gray-600 dark:bg-gray-700 dark:text-gray-300"
              :title="chartNote(metric).title"
              data-testid="metric-chart-basis"
            >{{ chartNote(metric).label }}</span>
            <span
              v-if="metric.status === 'retired'"
              class="inline-flex items-center rounded-full bg-gray-100 px-2 py-0.5 text-gray-600 dark:bg-gray-700 dark:text-gray-300"
            >Retired</span>
          </div>

          <!-- Declared, never recorded. The copy names the next action, and
               which action depends on whether this agent actually has the
               playbook that would do it. -->
          <p
            v-if="metric.message"
            class="mt-2 text-xs text-gray-500 dark:text-gray-400"
            data-testid="metric-empty-message"
          >{{ metric.message }}</p>
        </div>
      </div>
    </template>
  </section>
</template>

<script setup>
/**
 * The agent's declared metrics as tiles (ent#479 C8).
 *
 * A SIBLING of `DashboardPanel.vue`, never an arm inside it. The panel's
 * terminal states are "Agent Not Running" and "No Dashboard Defined"; this
 * surface is store-backed and must render for a stopped agent that has no
 * `dashboard.yaml` at all — ent#439's sensible default, and exactly the agent
 * the panel refuses to render anything for.
 *
 * It owns its own fetch, its own interval and its own `viewState`, so a failed
 * metrics read cannot take the dashboard down with it and vice versa.
 *
 * The stale rule is NOT implemented here. `stale` / `freshness` /
 * `stale_after` arrive decided by `services/metric_read_service.freshness` —
 * the platform's single definition, which the health block, the bound widgets
 * and the role card all read too. A browser-side recomputation would be a
 * second rule the first time one of them is edited.
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useAgentsStore } from '../stores/agents'
import SparklineChart from './SparklineChart.vue'
import LoadFailed from './LoadFailed.vue'
import InlineError from './InlineError.vue'
import BaseSelect from './base/BaseSelect.vue'
import { viewState, staleBannerMessage } from '../utils/loadingState'
import { formatRelativeTime, formatLocalDateTime } from '../utils/timestamps'
import {
  chartBasisNote,
  formatMetricValue,
  freshnessChip,
  metricUnitSuffix,
  sparklineColor,
  sparklineMax,
  sparklinePoints,
  statusBadgeClasses,
  thresholdClasses,
  trendClasses,
} from '../utils/metricFormat'

const props = defineProps({
  agentName: { type: String, required: true },
  // Poll cadence, matched to the dashboard panel's default so the two surfaces
  // on one tab do not tick against each other.
  refreshSeconds: { type: Number, default: 30 },
})

const WINDOW_OPTIONS = [
  { value: 'auto', label: 'Auto' },
  { value: '24h', label: '24 hours' },
  { value: '7d', label: '7 days' },
  { value: '30d', label: '30 days' },
  { value: '90d', label: '90 days' },
]
const STALE_RULE_TITLE =
  'A metric is stale when no point has arrived within twice its declared cadence. '
  + 'A metric with no declared cadence is never stale — there is nothing to be late against.'

const agentsStore = useAgentsStore()
const payload = ref(null)
const loading = ref(true)
const loadError = ref('')
const lastLoadedAt = ref(null)
const selectedWindow = ref('auto')
let refreshInterval = null

const metrics = computed(() => payload.value?.metrics || [])
const findings = computed(() => payload.value?.findings || [])

// `loading` stays "fetch in flight" for the retry label only; the template
// gates on "no data yet" (ent#253 / p14), so a scheduled refresh swaps values
// in place — same DOM nodes, no scroll reset, no selection reset.
const view = computed(() => viewState({
  loading: loading.value,
  hasLoaded: payload.value !== null,
  error: loadError.value,
  count: metrics.value.length,
}))
const staleMessage = computed(() => staleBannerMessage('the declared metrics', lastLoadedAt.value))

async function load() {
  loading.value = true
  try {
    payload.value = await agentsStore.getAgentMetrics(props.agentName, {
      window: selectedWindow.value,
    })
    lastLoadedAt.value = new Date()
    loadError.value = ''
  } catch (error) {
    // Keep the last numbers on screen; the banner above says the reading is
    // stale and offers the retry. Never overwrite with an empty payload.
    loadError.value = detailOf(error)
  } finally {
    loading.value = false
  }
}

function detailOf(error) {
  const detail = error?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail?.message) return detail.message
  return error?.message || 'Request failed'
}

// ---- per-tile helpers (all delegate to utils/metricFormat.js) -------------

const unitSuffix = (m) => metricUnitSuffix(m.latest?.value, m.type, m.unit)
const chip = (m) => freshnessChip(m)
const sparkline = (m) => sparklinePoints(m)
const chartNote = (m) => chartBasisNote(m)
const relativeTime = (ts) => formatRelativeTime(ts)
const absoluteTime = (ts) => formatLocalDateTime(ts)

function valueClasses(metric) {
  const threshold = thresholdClasses(metric, metric.latest?.value)
  return threshold || 'text-gray-900 dark:text-white'
}

/** A status metric's colour comes from its own declared `values[]` entry. */
function statusColor(metric) {
  const value = metric.latest?.value
  const match = (metric.values || []).find((v) => v?.value === value)
  return match?.color || 'gray'
}

/**
 * A finding's `detail` as a sentence.
 *
 * D-010 emits an OBJECT — `{keys, undeclared}` from
 * `compatibility/static_checks.c_d010`, persisted into `checks_json` and
 * echoed verbatim by the route. `{{ finding.detail }}` renders an object
 * through Vue's `toDisplayString`, i.e. pretty-printed JSON braces in the
 * middle of an operator's dashboard, so the two lists are spelled out here
 * instead. A plain string (any other finding, and the shape a future check
 * may use) passes straight through.
 */
function findingDetail(finding) {
  const detail = finding?.detail
  if (!detail) return ''
  if (typeof detail === 'string') return detail
  if (typeof detail !== 'object') return String(detail)

  const parts = []
  if (Array.isArray(detail.keys) && detail.keys.length) {
    parts.push(`keys in the file: ${detail.keys.join(', ')}`)
  }
  if (Array.isArray(detail.undeclared) && detail.undeclared.length) {
    parts.push(`declared nowhere: ${detail.undeclared.join(', ')}`)
  }
  if (parts.length) return parts.join(' · ')

  // An unrecognised object still says something readable rather than nothing:
  // a silent finding is the failure this banner exists to prevent.
  return Object.entries(detail)
    .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(', ') : v}`)
    .join(' · ')
}

function seriesTitle(metric) {
  const dims = (metric.latest_by_series || [])
    .map((s) => Object.entries(s.dims || {}).map(([k, v]) => `${k}=${v}`).join(','))
    .filter(Boolean)
  const agg = metric.aggregation || 'last'
  return `${metric.series_count} dimension series folded with '${agg}'`
    + (dims.length ? `: ${dims.join(' · ')}` : '')
}

// ---- lifecycle ------------------------------------------------------------

function startRefresh() {
  stopRefresh()
  refreshInterval = setInterval(load, Math.max(5, props.refreshSeconds) * 1000)
}

function stopRefresh() {
  if (refreshInterval) {
    clearInterval(refreshInterval)
    refreshInterval = null
  }
}

// A window change is a REFETCH of a surface that already holds data, not a
// first load: keeping `payload` means the grid does not collapse and reflow
// while the new window is on the wire (p5/p6).
watch(selectedWindow, () => { load() })

watch(() => props.agentName, (next, prev) => {
  if (!next || next === prev) return
  payload.value = null
  loadError.value = ''
  lastLoadedAt.value = null
  load()
})

onMounted(() => {
  load()
  startRefresh()
})

onUnmounted(stopRefresh)
</script>
