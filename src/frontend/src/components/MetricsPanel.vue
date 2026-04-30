<template>
  <div class="space-y-6">
    <!-- Loading State -->
    <div v-if="loading" class="flex items-center justify-center py-8">
      <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-500"></div>
    </div>

    <!-- Agent Not Running State -->
    <div v-else-if="agentStatus !== 'running'" class="text-center py-8">
      <div class="mx-auto w-16 h-16 bg-gray-100 dark:bg-gray-700 rounded-full flex items-center justify-center mb-4">
        <svg class="w-8 h-8 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
        </svg>
      </div>
      <h3 class="text-lg font-medium text-gray-900 dark:text-white">Agent Not Running</h3>
      <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">
        Start the agent to view custom metrics.
      </p>
    </div>

    <!-- No Metrics Defined State -->
    <div v-else-if="!metricsData?.has_metrics" class="text-center py-8">
      <div class="mx-auto w-16 h-16 bg-gray-100 dark:bg-gray-700 rounded-full flex items-center justify-center mb-4">
        <svg class="w-8 h-8 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
        </svg>
      </div>
      <h3 class="text-lg font-medium text-gray-900 dark:text-white">No Metrics Defined</h3>
      <p class="mt-2 text-sm text-gray-500 dark:text-gray-400 max-w-sm mx-auto">
        {{ metricsData?.message || 'This agent does not have custom metrics defined in its template.yaml.' }}
      </p>
      <div class="mt-4 text-xs text-gray-400 dark:text-gray-500">
        To add metrics, include a <code class="bg-gray-100 dark:bg-gray-700 px-1 py-0.5 rounded">metrics:</code> section in template.yaml
      </div>
    </div>

    <!-- Metrics Display -->
    <div v-else>
      <!-- Header with last updated -->
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-sm font-semibold text-gray-900 dark:text-white uppercase tracking-wider flex items-center">
          <svg class="w-4 h-4 mr-2 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
          </svg>
          Custom Metrics
        </h3>
        <div class="flex items-center space-x-3 text-xs text-gray-500 dark:text-gray-400">
          <span v-if="metricsData.last_updated">
            Updated {{ formatRelativeTime(metricsData.last_updated) }}
          </span>
          <button
            @click="loadMetrics"
            :disabled="loading"
            class="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700"
            title="Refresh metrics"
          >
            <svg :class="['w-4 h-4', loading ? 'animate-spin' : '']" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </button>
        </div>
      </div>

      <!-- Metrics Grid -->
      <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
        <div
          v-for="metric in metricsData.definitions"
          :key="metric.name"
          class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 hover:shadow-sm transition-shadow"
          :title="metric.description"
        >
          <!-- Counter Type -->
          <template v-if="metric.type === 'counter'">
            <div class="text-3xl font-bold text-gray-900 dark:text-white">
              {{ formatNumber(getValue(metric.name)) }}
            </div>
            <div class="mt-1 text-sm text-gray-500 dark:text-gray-400">{{ metric.label }}</div>
          </template>

          <!-- Gauge Type -->
          <template v-else-if="metric.type === 'gauge'">
            <div class="flex items-baseline space-x-1">
              <span class="text-3xl font-bold text-gray-900 dark:text-white">
                {{ formatNumber(getValue(metric.name)) }}
              </span>
              <span v-if="metric.unit" class="text-sm text-gray-400 dark:text-gray-500">{{ metric.unit }}</span>
            </div>
            <div class="mt-1 text-sm text-gray-500 dark:text-gray-400">{{ metric.label }}</div>
          </template>

          <!-- Percentage Type -->
          <template v-else-if="metric.type === 'percentage'">
            <div class="flex items-baseline space-x-1">
              <span class="text-3xl font-bold" :class="getPercentageColor(getValue(metric.name), metric)">
                {{ formatNumber(getValue(metric.name)) }}
              </span>
              <span class="text-lg text-gray-400 dark:text-gray-500">%</span>
            </div>
            <div class="mt-2 w-full h-2 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
              <div
                class="h-full rounded-full transition-all duration-500"
                :class="getPercentageBarColor(getValue(metric.name), metric)"
                :style="{ width: `${Math.min(100, getValue(metric.name) || 0)}%` }"
              ></div>
            </div>
            <div class="mt-1 text-sm text-gray-500 dark:text-gray-400">{{ metric.label }}</div>
          </template>

          <!-- Status Type -->
          <template v-else-if="metric.type === 'status'">
            <div class="flex items-center space-x-2">
              <span
                class="px-2 py-1 text-sm font-medium rounded-full"
                :class="getStatusColors(getValue(metric.name), metric)"
              >
                {{ getStatusLabel(getValue(metric.name), metric) }}
              </span>
            </div>
            <div class="mt-2 text-sm text-gray-500 dark:text-gray-400">{{ metric.label }}</div>
          </template>

          <!-- Duration Type -->
          <template v-else-if="metric.type === 'duration'">
            <div class="text-2xl font-bold text-gray-900 dark:text-white">
              {{ formatDuration(getValue(metric.name)) }}
            </div>
            <div class="mt-1 text-sm text-gray-500 dark:text-gray-400">{{ metric.label }}</div>
          </template>

          <!-- Bytes Type -->
          <template v-else-if="metric.type === 'bytes'">
            <div class="text-2xl font-bold text-gray-900 dark:text-white">
              {{ formatBytes(getValue(metric.name)) }}
            </div>
            <div class="mt-1 text-sm text-gray-500 dark:text-gray-400">{{ metric.label }}</div>
          </template>

          <!-- Unknown Type (fallback) -->
          <template v-else>
            <div class="text-2xl font-bold text-gray-900 dark:text-white">
              {{ getValue(metric.name) ?? '—' }}
            </div>
            <div class="mt-1 text-sm text-gray-500 dark:text-gray-400">{{ metric.label }}</div>
          </template>
        </div>
      </div>

      <!-- No Values Yet Message -->
      <div v-if="!hasAnyValues" class="mt-4 p-4 bg-status-warning-50 dark:bg-status-warning-900/30 border border-status-warning-200 dark:border-status-warning-800 rounded-lg">
        <div class="flex items-center space-x-2 text-sm text-status-warning-700 dark:text-status-warning-300">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span>Metrics are defined but no values have been recorded yet. The agent needs to write to <code class="bg-status-warning-100 dark:bg-status-warning-900/50 px-1 py-0.5 rounded">metrics.json</code>.</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useAgentsStore } from '../stores/agents'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  },
  agentStatus: {
    type: String,
    default: 'stopped'
  }
})

const agentsStore = useAgentsStore()
const metricsData = ref(null)
const loading = ref(true)
let refreshInterval = null

const loadMetrics = async () => {
  loading.value = true
  try {
    const response = await agentsStore.getAgentMetrics(props.agentName)
    metricsData.value = response
  } catch (error) {
    console.error('Failed to load metrics:', error)
    metricsData.value = {
      has_metrics: false,
      message: 'Failed to load metrics'
    }
  } finally {
    loading.value = false
  }
}

// Get value from metrics data
const getValue = (name) => {
  return metricsData.value?.values?.[name]
}

// Check if any metric has a value
const hasAnyValues = computed(() => {
  if (!metricsData.value?.values) return false
  return Object.keys(metricsData.value.values).length > 0
})

// Format number with thousands separator
const formatNumber = (value) => {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'number') {
    // Use locale formatting for thousands separator
    return value.toLocaleString('en-US', { maximumFractionDigits: 2 })
  }
  return value
}

// Format bytes to human-readable
const formatBytes = (bytes) => {
  if (bytes === null || bytes === undefined) return '—'
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i]
}

// Format duration in seconds to human-readable
const formatDuration = (seconds) => {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds === 0) return '0s'

  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const mins = Math.floor((seconds % 3600) / 60)
  const secs = Math.floor(seconds % 60)

  const parts = []
  if (days > 0) parts.push(`${days}d`)
  if (hours > 0) parts.push(`${hours}h`)
  if (mins > 0) parts.push(`${mins}m`)
  if (secs > 0 && days === 0) parts.push(`${secs}s`)

  return parts.join(' ') || '0s'
}

// Format relative time
const formatRelativeTime = (isoString) => {
  if (!isoString) return ''
  const date = new Date(isoString)
  const now = new Date()
  const diffSeconds = Math.floor((now - date) / 1000)

  if (diffSeconds < 60) return 'just now'
  if (diffSeconds < 3600) return `${Math.floor(diffSeconds / 60)}m ago`
  if (diffSeconds < 86400) return `${Math.floor(diffSeconds / 3600)}h ago`
  return `${Math.floor(diffSeconds / 86400)}d ago`
}

// Get percentage text color based on value and thresholds
const getPercentageColor = (value, metric) => {
  if (value === null || value === undefined) return 'text-gray-400'
  const critical = metric.critical_threshold ?? 0
  const warning = metric.warning_threshold ?? 0

  if (critical > 0 && value < critical) return 'text-status-danger-600'
  if (warning > 0 && value < warning) return 'text-status-warning-600'
  return 'text-status-success-600'
}

// Get percentage bar color
const getPercentageBarColor = (value, metric) => {
  if (value === null || value === undefined) return 'bg-gray-300'
  const critical = metric.critical_threshold ?? 0
  const warning = metric.warning_threshold ?? 0

  if (critical > 0 && value < critical) return 'bg-status-danger-500'
  if (warning > 0 && value < warning) return 'bg-status-warning-500'
  return 'bg-status-success-500'
}

// Get status badge colors
const getStatusColors = (value, metric) => {
  if (!value || !metric.values) return 'bg-gray-100 text-gray-600'

  const statusDef = metric.values.find(v => v.value === value)
  if (!statusDef) return 'bg-gray-100 text-gray-600'

  const colorMap = {
    green: 'bg-status-success-100 text-status-success-800',
    red: 'bg-status-danger-100 text-status-danger-800',
    yellow: 'bg-status-warning-100 text-status-warning-800',
    gray: 'bg-gray-100 text-gray-600',
    blue: 'bg-status-info-100 text-status-info-800',
    orange: 'bg-status-urgent-100 text-status-urgent-800'
  }

  return colorMap[statusDef.color] || 'bg-gray-100 text-gray-600'
}

// Get status label
const getStatusLabel = (value, metric) => {
  if (!value) return '—'
  if (!metric.values) return value

  const statusDef = metric.values.find(v => v.value === value)
  return statusDef?.label || value
}

// Start auto-refresh when component is visible
const startRefresh = () => {
  if (refreshInterval) clearInterval(refreshInterval)
  refreshInterval = setInterval(() => {
    if (props.agentStatus === 'running') {
      loadMetrics()
    }
  }, 30000) // Refresh every 30 seconds
}

const stopRefresh = () => {
  if (refreshInterval) {
    clearInterval(refreshInterval)
    refreshInterval = null
  }
}

// Reload when agent name changes (navigating between agents)
watch(() => props.agentName, (newName, oldName) => {
  if (newName && newName !== oldName) {
    // Reset state for new agent
    metricsData.value = null
    stopRefresh()
    if (props.agentStatus === 'running') {
      loadMetrics()
      startRefresh()
    } else {
      loading.value = false
    }
  }
})

// Reload when agent status changes to running
watch(() => props.agentStatus, (newStatus) => {
  if (newStatus === 'running') {
    loadMetrics()
    startRefresh()
  } else {
    stopRefresh()
  }
})

onMounted(() => {
  if (props.agentStatus === 'running') {
    loadMetrics()
    startRefresh()
  } else {
    loading.value = false
  }
})

onUnmounted(() => {
  stopRefresh()
})
</script>
