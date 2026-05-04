<template>
  <div
    class="px-5 py-4 rounded-xl border-2 shadow-lg bg-gradient-to-br from-white to-accent-purple-50/50 dark:from-gray-800 dark:to-accent-purple-900/20 border-accent-purple-300 dark:border-accent-purple-600/50 relative flex flex-col"
    style="width: 400px; min-height: 130px;"
  >
    <!-- Connection handles -->
    <Handle
      type="target"
      :position="Position.Top"
      class="!w-3 !h-3 !border-2 !bg-accent-purple-400 !border-white dark:!border-gray-800"
    />

    <!-- Content -->
    <div class="relative z-10 flex flex-col flex-grow">
      <!-- Header row -->
      <div class="flex items-center justify-between mb-3">
        <div class="flex items-center space-x-3">
          <!-- Trinity icon -->
          <div class="w-9 h-9 rounded-lg bg-accent-purple-500 dark:bg-accent-purple-600 flex items-center justify-center shadow">
            <svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z" />
            </svg>
          </div>
          <div>
            <div class="text-base font-semibold text-gray-800 dark:text-gray-100">Trinity System Agent</div>
            <div class="text-xs text-accent-purple-500 dark:text-accent-purple-400">Platform Orchestrator</div>
          </div>
        </div>

        <!-- Status indicator -->
        <div class="flex items-center space-x-1.5">
          <span
            :class="[
              'px-2 py-0.5 text-xs font-medium rounded-full',
              isRunning
                ? 'bg-status-success-100 text-status-success-600 dark:bg-status-success-900/40 dark:text-status-success-400'
                : 'bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400'
            ]"
          >
            {{ isRunning ? 'Online' : 'Offline' }}
          </span>
          <div
            :class="[
              'w-2 h-2 rounded-full',
              isRunning ? 'bg-status-success-500 active-pulse' : 'bg-gray-400'
            ]"
          ></div>
        </div>
      </div>

      <!-- Stats row -->
      <div class="flex items-center justify-between bg-white/50 dark:bg-gray-800/50 rounded-lg px-4 py-2 mb-3">
        <!-- Success rate -->
        <div class="flex items-center space-x-2">
          <span class="text-xs text-gray-500 dark:text-gray-400">Success</span>
          <div class="w-20 bg-gray-200 dark:bg-gray-700 rounded-full h-2 overflow-hidden">
            <div
              v-if="hasExecutionStats"
              class="h-full rounded-full transition-all duration-500"
              :class="successBarColor"
              :style="{ width: successBarPercent + '%' }"
            ></div>
          </div>
          <span v-if="hasExecutionStats" class="text-xs font-semibold" :class="successBarColorText">{{ successBarPercent }}%</span>
          <span v-else class="text-xs text-gray-400 dark:text-gray-500">&mdash;</span>
        </div>

        <!-- Execution stats -->
        <div v-if="hasExecutionStats" class="flex items-center space-x-2 text-xs text-gray-500 dark:text-gray-400">
          <span class="font-medium text-gray-700 dark:text-gray-300">{{ executionStats.taskCount }}</span>
          <span>tasks</span>
          <span class="text-gray-300 dark:text-gray-600">·</span>
          <span :class="successRateColorClass" class="font-medium">{{ executionStats.successRate }}%</span>
        </div>
      </div>

      <!-- Action button -->
      <router-link
        to="/system-agent"
        class="nodrag w-full px-3 py-2 bg-accent-purple-50 dark:bg-accent-purple-900/30 hover:bg-accent-purple-100 dark:hover:bg-accent-purple-900/50 text-accent-purple-700 dark:text-accent-purple-300 rounded-lg text-xs font-semibold transition-all duration-200 border border-accent-purple-200 dark:border-accent-purple-700 hover:border-accent-purple-300 dark:hover:border-accent-purple-600 text-center block"
      >
        System Dashboard
      </router-link>
    </div>

    <Handle
      type="source"
      :position="Position.Bottom"
      class="!w-3 !h-3 !border-2 !bg-accent-purple-400 !border-white dark:!border-gray-800"
    />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { Handle, Position } from '@vue-flow/core'

const props = defineProps({
  id: String,
  data: {
    type: Object,
    required: true
  }
})

const isRunning = computed(() => {
  return props.data.status === 'running'
})

// Execution stats
const executionStats = computed(() => {
  return props.data.executionStats || null
})

const hasExecutionStats = computed(() => {
  return executionStats.value && executionStats.value.taskCount > 0
})

const successRateColorClass = computed(() => {
  if (!executionStats.value) return 'text-gray-500 dark:text-gray-400'
  const rate = executionStats.value.successRate
  if (rate >= 80) return 'text-status-success-600 dark:text-status-success-400'
  if (rate >= 50) return 'text-status-warning-600 dark:text-status-warning-400'
  return 'text-status-danger-600 dark:text-status-danger-400'
})

// Success rate bar
const successBarPercent = computed(() => {
  if (!executionStats.value) return 0
  return Math.round(executionStats.value.successRate || 0)
})

const successBarColor = computed(() => {
  const percent = successBarPercent.value
  if (percent >= 90) return 'bg-status-success-500'
  if (percent >= 50) return 'bg-status-warning-500'
  return 'bg-status-danger-500'
})

const successBarColorText = computed(() => {
  const percent = successBarPercent.value
  if (percent >= 90) return 'text-status-success-600 dark:text-status-success-400'
  if (percent >= 50) return 'text-status-warning-600 dark:text-status-warning-400'
  return 'text-status-danger-600 dark:text-status-danger-400'
})
</script>

<style scoped>
.active-pulse {
  animation: active-pulse-animation 2s ease-in-out infinite;
  box-shadow: 0 0 4px 1px rgba(16, 185, 129, 0.4);
}

@keyframes active-pulse-animation {
  0%, 100% {
    opacity: 1;
  }
  50% {
    opacity: 0.6;
  }
}
</style>
