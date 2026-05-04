<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import SparklineChart from './SparklineChart.vue'

const API_BASE = import.meta.env.VITE_API_BASE || ''

// History configuration: 60 samples at 5s intervals = 5 minutes
const MAX_POINTS = 60

// History data
const cpuHistory = ref([])
const memHistory = ref([])

// Current stats
const hostStats = ref(null)
const loading = ref(true)
const error = ref(null)

let pollInterval = null

// Initialize with empty data (nulls)
function initHistory() {
  cpuHistory.value = Array(MAX_POINTS).fill(null)
  memHistory.value = Array(MAX_POINTS).fill(null)
}

async function fetchStats() {
  try {
    const token = localStorage.getItem('token')
    if (!token) return

    const headers = { Authorization: `Bearer ${token}` }

    // Fetch host stats
    const hostRes = await fetch(`${API_BASE}/api/telemetry/host`, {
      headers,
      signal: AbortSignal.timeout(3000)
    }).catch(() => null)

    if (hostRes?.ok) {
      hostStats.value = await hostRes.json()

      // Push new values and maintain rolling window
      cpuHistory.value.push(hostStats.value.cpu?.percent ?? null)
      memHistory.value.push(hostStats.value.memory?.percent ?? null)

      // Trim to max points
      if (cpuHistory.value.length > MAX_POINTS) {
        cpuHistory.value.shift()
      }
      if (memHistory.value.length > MAX_POINTS) {
        memHistory.value.shift()
      }
    }

    loading.value = false
    error.value = null
  } catch (e) {
    loading.value = false
    error.value = e.message
  }
}

function formatPercent(pct) {
  return pct?.toFixed(0) || '0'
}

function formatMemory(usedGb, totalGb) {
  if (!usedGb || !totalGb) return '0/0G'
  return `${usedGb.toFixed(1)}/${totalGb.toFixed(0)}G`
}

function getColorClass(percent) {
  if (percent === null || percent === undefined) return 'text-gray-400'
  if (percent < 50) return 'text-status-success-500 dark:text-status-success-400'
  if (percent < 75) return 'text-status-warning-500 dark:text-status-warning-400'
  if (percent < 90) return 'text-status-urgent-500 dark:text-status-urgent-400'
  return 'text-status-danger-500 dark:text-status-danger-400'
}

onMounted(async () => {
  initHistory()
  await fetchStats()
  pollInterval = setInterval(fetchStats, 5000)
})

onUnmounted(() => {
  if (pollInterval) clearInterval(pollInterval)
})
</script>

<template>
  <div class="host-telemetry">
    <template v-if="!loading">
      <!-- CPU -->
      <span class="stat-item">
        <span class="dot bg-blue-500"></span>
        <span class="stat-label">CPU</span>
        <SparklineChart
          :data="cpuHistory"
          color="#3b82f6"
          :y-max="100"
          :width="60"
          :height="20"
        />
        <span class="stat-value" :class="getColorClass(hostStats?.cpu?.percent)">{{ formatPercent(hostStats?.cpu?.percent) }}%</span>
      </span>

      <span class="text-gray-300 dark:text-gray-600">·</span>

      <!-- Memory -->
      <span class="stat-item">
        <span class="dot bg-accent-purple-500"></span>
        <span class="stat-label">Mem</span>
        <SparklineChart
          :data="memHistory"
          color="#a855f7"
          :y-max="100"
          :width="60"
          :height="20"
        />
        <span class="stat-value" :class="getColorClass(hostStats?.memory?.percent)">{{ formatMemory(hostStats?.memory?.used_gb, hostStats?.memory?.total_gb) }}</span>
      </span>

      <span class="text-gray-300 dark:text-gray-600">·</span>

      <!-- Disk -->
      <span class="stat-item">
        <span class="dot bg-status-success-500"></span>
        <span class="stat-label">Disk</span>
        <span class="disk-bar">
          <span
            class="disk-fill"
            :style="{ width: `${hostStats?.disk?.percent || 0}%` }"
            :class="hostStats?.disk?.percent > 90 ? 'bg-status-danger-500' : hostStats?.disk?.percent > 75 ? 'bg-status-urgent-500' : 'bg-status-success-500'"
          ></span>
        </span>
        <span class="stat-value" :class="getColorClass(hostStats?.disk?.percent)">{{ formatPercent(hostStats?.disk?.percent) }}%</span>
      </span>
    </template>
  </div>
</template>

<style scoped>
.host-telemetry {
  display: inline-flex;
  align-items: center;
  gap: 12px;
  font-size: 12px;
}

.stat-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.stat-label {
  color: #6b7280;
}

.dark .stat-label {
  color: #9ca3af;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}

.stat-value {
  font-weight: 600;
  font-family: ui-monospace, monospace;
  font-size: 12px;
}

.disk-bar {
  display: inline-block;
  width: 50px;
  height: 6px;
  background: rgba(107, 114, 128, 0.2);
  border-radius: 3px;
  overflow: hidden;
  vertical-align: middle;
}

.disk-fill {
  display: block;
  height: 100%;
  transition: width 0.3s ease;
}

/* Dark mode adjustments */
.dark .disk-bar {
  background: rgba(55, 65, 81, 0.5);
}
</style>
