<template>
  <div class="trend-chart">
    <!-- Chart Header -->
    <div class="flex items-center justify-between mb-4">
      <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ title }}</h3>
      <div class="text-xs text-gray-500 dark:text-gray-400">
        Last {{ days }} days
      </div>
    </div>

    <!-- Chart Container -->
    <div class="relative h-48">
      <!-- Y-axis labels -->
      <div class="absolute left-0 top-0 bottom-6 w-8 flex flex-col justify-between text-xs text-gray-400">
        <span>{{ maxValue }}</span>
        <span>{{ Math.round(maxValue / 2) }}</span>
        <span>0</span>
      </div>

      <!-- Chart area -->
      <div class="ml-10 h-full flex items-end gap-1">
        <div
          v-for="(item, index) in chartData"
          :key="index"
          class="flex-1 flex flex-col items-center group relative"
        >
          <!-- Tooltip -->
          <div
            class="absolute bottom-full mb-2 px-2 py-1 bg-gray-900 dark:bg-gray-700 text-white text-xs rounded opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap z-10 pointer-events-none"
          >
            <div class="font-medium">{{ formatDate(item.date) }}</div>
            <div v-if="chartType === 'executions'">
              <span class="text-status-success-400">{{ item.completed_count }}</span> /
              <span class="text-status-danger-400">{{ item.failed_count }}</span>
            </div>
            <div v-else-if="chartType === 'cost'">
              {{ formatCost(item.total_cost) }}
            </div>
            <div v-else>
              {{ item.success_rate.toFixed(1) }}%
            </div>
          </div>

          <!-- Bar -->
          <div class="w-full flex flex-col-reverse gap-0.5" :style="{ height: 'calc(100% - 20px)' }">
            <!-- Success/Completed bar (green) -->
            <div
              v-if="chartType === 'executions'"
              class="w-full bg-status-success-500 dark:bg-status-success-400 rounded-t transition-all duration-300"
              :style="{ height: getBarHeight(item.completed_count) + '%' }"
            ></div>
            <!-- Failed bar (red) - stacked on top -->
            <div
              v-if="chartType === 'executions' && item.failed_count > 0"
              class="w-full bg-status-danger-500 dark:bg-status-danger-400 rounded-t transition-all duration-300"
              :style="{ height: getBarHeight(item.failed_count) + '%' }"
            ></div>
            <!-- Cost bar (blue) -->
            <div
              v-if="chartType === 'cost'"
              class="w-full bg-status-info-500 dark:bg-status-info-400 rounded-t transition-all duration-300"
              :style="{ height: getCostBarHeight(item.total_cost) + '%' }"
            ></div>
            <!-- Success rate bar (gradient) -->
            <div
              v-if="chartType === 'success_rate'"
              class="w-full rounded-t transition-all duration-300"
              :class="getSuccessRateColor(item.success_rate)"
              :style="{ height: item.success_rate + '%' }"
            ></div>
          </div>

          <!-- X-axis label -->
          <div class="text-xs text-gray-400 mt-1 truncate w-full text-center">
            {{ formatDateShort(item.date) }}
          </div>
        </div>
      </div>

      <!-- Empty state -->
      <div
        v-if="chartData.length === 0"
        class="absolute inset-0 flex items-center justify-center text-gray-400 dark:text-gray-500"
      >
        No data available
      </div>
    </div>

    <!-- Legend -->
    <div v-if="chartType === 'executions'" class="flex items-center justify-center gap-4 mt-4 text-xs">
      <div class="flex items-center gap-1">
        <div class="w-3 h-3 bg-status-success-500 rounded"></div>
        <span class="text-gray-600 dark:text-gray-400">Completed</span>
      </div>
      <div class="flex items-center gap-1">
        <div class="w-3 h-3 bg-status-danger-500 rounded"></div>
        <span class="text-gray-600 dark:text-gray-400">Failed</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { formatCost } from '../../composables/useFormatters'

const props = defineProps({
  title: {
    type: String,
    default: 'Execution Trend',
  },
  data: {
    type: Array,
    default: () => [],
  },
  days: {
    type: Number,
    default: 30,
  },
  chartType: {
    type: String,
    default: 'executions', // 'executions' | 'cost' | 'success_rate'
    validator: (v) => ['executions', 'cost', 'success_rate'].includes(v),
  },
})

// Computed
const chartData = computed(() => {
  // Only show last N days based on props.days
  return props.data.slice(-props.days)
})

const maxValue = computed(() => {
  if (props.chartType === 'executions') {
    const max = Math.max(...chartData.value.map(d => d.execution_count || 0))
    return Math.max(max, 10) // Minimum of 10 for scale
  } else if (props.chartType === 'cost') {
    const max = Math.max(...chartData.value.map(d => d.total_cost || 0))
    return Math.max(Math.ceil(max), 1)
  }
  return 100 // For success rate
})

const maxCost = computed(() => {
  return Math.max(...chartData.value.map(d => d.total_cost || 0), 1)
})

// Methods
function getBarHeight(value) {
  if (maxValue.value === 0) return 0
  return Math.max((value / maxValue.value) * 100, 0)
}

function getCostBarHeight(value) {
  if (maxCost.value === 0) return 0
  return Math.max((value / maxCost.value) * 100, 0)
}

function getSuccessRateColor(rate) {
  if (rate >= 80) return 'bg-status-success-500 dark:bg-status-success-400'
  if (rate >= 50) return 'bg-status-warning-500 dark:bg-status-warning-400'
  return 'bg-status-danger-500 dark:bg-status-danger-400'
}

function formatDate(dateStr) {
  if (!dateStr) return ''
  const date = new Date(dateStr)
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

function formatDateShort(dateStr) {
  if (!dateStr) return ''
  const date = new Date(dateStr)
  return date.getDate().toString()
}
</script>

<style scoped>
.trend-chart {
  min-height: 200px;
}
</style>
