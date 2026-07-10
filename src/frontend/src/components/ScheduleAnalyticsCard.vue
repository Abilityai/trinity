<template>
  <div class="schedule-analytics-card mt-3 border-t border-gray-100 dark:border-gray-700 pt-3">
    <div class="flex items-center justify-between mb-3">
      <h4 class="text-xs font-medium text-gray-700 dark:text-gray-300 uppercase tracking-wide">
        Analytics
      </h4>
      <div class="flex items-center gap-1">
        <button
          v-for="opt in windowOptions"
          :key="opt.hours"
          @click="setWindow(opt.hours)"
          :class="[
            'text-xs px-2 py-0.5 rounded transition-colors',
            windowHours === opt.hours
              ? 'bg-action-primary-100 dark:bg-action-primary-900/40 text-action-primary-700 dark:text-action-primary-300 font-medium'
              : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-600'
          ]"
        >{{ opt.label }}</button>
      </div>
    </div>

    <div v-if="loading" class="py-6 flex justify-center">
      <div class="animate-spin rounded-full h-5 w-5 border-b-2 border-action-primary-500"></div>
    </div>

    <div
      v-else-if="error"
      class="text-xs p-2 rounded bg-status-danger-50 dark:bg-status-danger-900/20 text-status-danger-700 dark:text-status-danger-300"
    >{{ error }}</div>

    <div
      v-else-if="!data || data.total_executions === 0"
      class="text-center py-6 text-xs text-gray-400 dark:text-gray-500"
    >No executions in selected window</div>

    <div v-else class="space-y-3">
      <!-- Stat tiles -->
      <div class="grid grid-cols-2 sm:grid-cols-5 gap-2">
        <div class="px-3 py-2 rounded bg-gray-50 dark:bg-gray-800/50">
          <div class="text-xs text-gray-500 dark:text-gray-400">Runs</div>
          <div class="text-sm font-semibold text-gray-900 dark:text-white">
            {{ data.total_executions }}
          </div>
        </div>
        <div class="px-3 py-2 rounded bg-gray-50 dark:bg-gray-800/50">
          <div class="text-xs text-gray-500 dark:text-gray-400">Success</div>
          <div :class="['text-sm font-semibold', successRateColor]">
            {{ successRatePercent }}
          </div>
        </div>
        <div class="px-3 py-2 rounded bg-gray-50 dark:bg-gray-800/50">
          <div class="text-xs text-gray-500 dark:text-gray-400">p50 / p95 / p99</div>
          <div class="text-sm font-semibold text-gray-900 dark:text-white">
            {{ formatPercentileTriple }}
          </div>
        </div>
        <div class="px-3 py-2 rounded bg-gray-50 dark:bg-gray-800/50">
          <div class="text-xs text-gray-500 dark:text-gray-400">Cost</div>
          <div class="text-sm font-semibold text-gray-900 dark:text-white">
            {{ formatCostCompact(data.cost.total) }}
          </div>
        </div>
        <div class="px-3 py-2 rounded bg-gray-50 dark:bg-gray-800/50">
          <div class="text-xs text-gray-500 dark:text-gray-400">Tool calls</div>
          <div class="text-sm font-semibold text-gray-900 dark:text-white">
            {{ data.tool_calls.total_calls }}
          </div>
        </div>
      </div>

      <!-- Tool top-5 -->
      <div v-if="data.tool_calls.top.length > 0">
        <div class="text-xs text-gray-500 dark:text-gray-400 mb-1">
          Top tools by total wall time
        </div>
        <div class="space-y-1">
          <div
            v-for="tool in data.tool_calls.top"
            :key="tool.name"
            class="flex items-center gap-2"
          >
            <span class="text-xs font-mono text-gray-700 dark:text-gray-300 w-24 truncate">
              {{ tool.name }}
            </span>
            <div class="flex-1 h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
              <div
                class="h-full bg-action-primary-500 dark:bg-action-primary-400 rounded-full"
                :style="{ width: toolBarWidth(tool.total_duration_ms) + '%' }"
              ></div>
            </div>
            <span class="text-xs text-gray-500 dark:text-gray-400 w-16 text-right">
              {{ formatDuration(tool.total_duration_ms) }}
            </span>
          </div>
        </div>
      </div>

      <!-- Daily timeline -->
      <div>
        <div class="flex items-center justify-between mb-1">
          <div class="text-xs text-gray-500 dark:text-gray-400">
            Daily activity (UTC)
          </div>
          <div class="flex items-center gap-3 text-xs">
            <span class="flex items-center gap-1">
              <span class="w-2 h-2 rounded-sm bg-status-success-500"></span>
              <span class="text-gray-500 dark:text-gray-400">success</span>
            </span>
            <span class="flex items-center gap-1">
              <span class="w-2 h-2 rounded-sm bg-status-danger-500"></span>
              <span class="text-gray-500 dark:text-gray-400">failed</span>
            </span>
            <span class="flex items-center gap-1">
              <span class="w-2 h-2 rounded-sm bg-status-info-500"></span>
              <span class="text-gray-500 dark:text-gray-400">cost</span>
            </span>
          </div>
        </div>
        <div class="relative h-24 flex items-end gap-0.5">
          <div
            v-for="bucket in data.timeline"
            :key="bucket.date"
            class="flex-1 flex flex-col items-stretch group relative min-w-[2px]"
            :title="`${bucket.date} • ${bucket.success} ok / ${bucket.failed} fail • ${formatCost(bucket.cost)}`"
          >
            <!-- Tooltip -->
            <div
              class="absolute bottom-full mb-1 left-1/2 -translate-x-1/2 px-2 py-1 bg-gray-900 dark:bg-gray-700 text-white text-xs rounded opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap z-10 pointer-events-none"
            >
              <div class="font-medium">{{ bucket.date }}</div>
              <div>
                <span class="text-status-success-400">{{ bucket.success }}</span>
                /
                <span class="text-status-danger-400">{{ bucket.failed }}</span>
                · {{ formatCost(bucket.cost) }}
              </div>
            </div>

            <div class="flex-1 flex flex-col-reverse">
              <div
                v-if="bucket.success > 0"
                class="bg-status-success-500 dark:bg-status-success-400"
                :style="{ height: barHeight(bucket.success) + '%' }"
              ></div>
              <div
                v-if="bucket.failed > 0"
                class="bg-status-danger-500 dark:bg-status-danger-400"
                :style="{ height: barHeight(bucket.failed) + '%' }"
              ></div>
            </div>
            <!-- Cost overlay tick at top -->
            <div
              v-if="bucket.cost > 0"
              class="absolute left-0 right-0 h-0.5 bg-status-info-500 dark:bg-status-info-400"
              :style="{ bottom: costMarkerOffset(bucket.cost) + '%' }"
            ></div>
          </div>
        </div>
      </div>

      <!-- Sampled badge -->
      <div
        v-if="data.sampled"
        class="text-xs text-status-warning-700 dark:text-status-warning-300 bg-status-warning-50 dark:bg-status-warning-900/20 px-2 py-1 rounded"
      >
        Showing latest {{ data.sample_size.toLocaleString() }} successful runs — older runs excluded from distribution.
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { formatCost, formatCostCompact } from '../composables/useFormatters'
import api from '../api'
import { useFormatters } from '../composables/useFormatters'

const props = defineProps({
  agentName: { type: String, required: true },
  scheduleId: { type: String, required: true },
})

const { formatDuration } = useFormatters()

const windowOptions = [
  { hours: 24, label: '24h' },
  { hours: 168, label: '7d' },
  { hours: 720, label: '30d' },
]

const storageKey = computed(() => `trinity:868:window:${props.scheduleId}`)

function loadInitialWindow() {
  try {
    const raw = localStorage.getItem(storageKey.value)
    const parsed = parseInt(raw, 10)
    if ([24, 168, 720].includes(parsed)) return parsed
  } catch {
    // localStorage may be unavailable; fall through
  }
  return 168
}

const windowHours = ref(loadInitialWindow())
const data = ref(null)
const loading = ref(false)
const error = ref('')

async function fetchAnalytics() {
  loading.value = true
  error.value = ''
  try {
    const res = await api.get(
      `/api/agents/${props.agentName}/schedules/${props.scheduleId}/analytics`,
      { params: { window_hours: windowHours.value } },
    )
    data.value = res.data
  } catch (err) {
    error.value = err.response?.data?.detail || 'Failed to load analytics'
    data.value = null
  } finally {
    loading.value = false
  }
}

function setWindow(hours) {
  if (hours === windowHours.value) return
  windowHours.value = hours
  try {
    localStorage.setItem(storageKey.value, String(hours))
  } catch {
    // ignore — non-essential persistence
  }
}

watch(windowHours, fetchAnalytics)
onMounted(fetchAnalytics)

const successRatePercent = computed(() => {
  if (!data.value) return '—'
  return `${(data.value.success_rate * 100).toFixed(1)}%`
})

const successRateColor = computed(() => {
  if (!data.value) return 'text-gray-900 dark:text-white'
  const rate = data.value.success_rate
  if (rate >= 0.95) return 'text-status-success-600 dark:text-status-success-400'
  if (rate >= 0.80) return 'text-status-warning-600 dark:text-status-warning-400'
  return 'text-status-danger-600 dark:text-status-danger-400'
})

const formatPercentileTriple = computed(() => {
  if (!data.value) return '—'
  const { p50, p95, p99 } = data.value.duration_ms
  if (p50 == null) return '—'
  return `${formatDuration(p50)} / ${formatDuration(p95)} / ${formatDuration(p99)}`
})

const maxBucketCount = computed(() => {
  if (!data.value) return 1
  let max = 0
  for (const b of data.value.timeline) {
    const total = b.success + b.failed
    if (total > max) max = total
  }
  return Math.max(max, 1)
})

function barHeight(value) {
  return Math.max((value / maxBucketCount.value) * 100, 0)
}

const maxBucketCost = computed(() => {
  if (!data.value) return 0
  let max = 0
  for (const b of data.value.timeline) {
    if (b.cost > max) max = b.cost
  }
  return max
})

function costMarkerOffset(cost) {
  if (maxBucketCost.value <= 0) return 0
  // Place the cost tick on the bar as a relative marker (0% = bottom,
  // 100% = top). Tied to the same axis as bars; not strictly accurate
  // (bars are count-units, cost is dollars) but conveys relative
  // distribution day-to-day.
  return (cost / maxBucketCost.value) * 95
}

const maxToolDuration = computed(() => {
  if (!data.value) return 1
  let max = 0
  for (const t of data.value.tool_calls.top) {
    if (t.total_duration_ms > max) max = t.total_duration_ms
  }
  return Math.max(max, 1)
})

function toolBarWidth(ms) {
  return Math.max((ms / maxToolDuration.value) * 100, 2)
}
</script>
