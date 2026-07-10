<template>
  <div
    :class="[
      'px-5 py-4 rounded-xl border shadow-lg',
      'transition-all duration-200 hover:shadow-xl cursor-move',
      'relative overflow-visible',
      'flex flex-col',
      'backdrop-blur-sm',
      // System agent gets distinct purple styling with transparency
      isSystemAgent
        ? 'bg-accent-purple-50/80 dark:bg-accent-purple-900/30 border-accent-purple-200 dark:border-accent-purple-700/50'
        : 'bg-white/80 dark:bg-gray-800/80 border-gray-200/60 dark:border-gray-700/50'
    ]"
    style="width: 320px; min-height: 180px;"
  >
    <!-- Connection handles - styled for permission edge creation -->
    <Handle
      type="target"
      :position="Position.Top"
      class="!w-4 !h-4 !border-2 !bg-blue-400 !border-white dark:!border-gray-800 hover:!bg-blue-500 hover:!scale-125 !transition-all !duration-150"
    />

    <!-- Avatar on left edge (50% in, 50% out) -->
    <div class="absolute left-0 top-5 z-10 -translate-x-1/2">
      <div class="rounded-full border-2 shadow-md overflow-hidden"
           :class="isSystemAgent ? 'border-accent-purple-400 dark:border-accent-purple-500' : 'border-action-primary-400 dark:border-action-primary-500'"
      >
        <AgentAvatar :name="data.label" :avatar-url="data.avatarUrl" size="xl" />
      </div>
    </div>

    <!-- Agent info -->
    <div class="flex flex-col flex-grow min-w-0">
      <!-- Header with name, runtime badge, system badge, and status dot -->
      <div class="flex items-center justify-between mb-2 pl-5">
        <div class="flex items-center flex-1 mr-2 min-w-0">
          <div
            class="nodrag text-gray-900 dark:text-white font-bold text-base truncate cursor-pointer hover:text-blue-600 dark:hover:text-blue-400 hover:underline transition-colors"
            :title="data.label"
            @click="viewDetails"
          >
            {{ data.label }}
          </div>
          <!-- Runtime badge (Claude/Gemini) -->
          <RuntimeBadge :runtime="data.runtime" :show-label="false" class="ml-2 flex-shrink-0" />
          <!-- System agent badge -->
          <span
            v-if="isSystemAgent"
            class="ml-2 px-1.5 py-0.5 text-xs font-semibold rounded bg-accent-purple-100 text-accent-purple-700 dark:bg-accent-purple-900/50 dark:text-accent-purple-300 flex-shrink-0"
            title="System Agent - Platform Orchestrator"
          >
            SYSTEM
          </span>
          <!-- #526: dispatch circuit breaker open badge (distinct danger styling) -->
          <span
            v-if="circuitOpen"
            data-testid="circuit-open-badge"
            class="ml-2 px-1.5 py-0.5 text-xs font-semibold rounded bg-status-danger-100 text-status-danger-700 dark:bg-status-danger-900/50 dark:text-status-danger-300 flex-shrink-0"
            title="Dispatch circuit breaker OPEN — agent unhealthy; new tasks fast-fail until it recovers"
          >
            ⚡ circuit open
          </span>
        </div>
        <!-- Status indicator dot -->
        <div
          :class="[
            'w-3 h-3 rounded-full flex-shrink-0',
            isActive ? 'active-pulse' : ''
          ]"
          :style="{ backgroundColor: statusDotColor }"
        ></div>
      </div>

      <!-- Running state toggle and activity state -->
      <div class="flex items-center justify-between mb-2 pl-5">
        <!-- Running State Toggle (not for system agent) -->
        <RunningStateToggle
          v-if="!isSystemAgent"
          :model-value="isRunning"
          :loading="runningToggleLoading"
          size="sm"
          class="nodrag"
          @toggle="handleRunningToggle"
        />
        <!-- Activity state label (for system agent, shown on left) -->
        <div
          v-else
          :class="[
            'text-xs font-medium capitalize',
            activityStateColor
          ]"
        >
          {{ activityStateLabel }}
        </div>
        <!-- Autonomy toggle (not for system agent) -->
        <AutonomyToggle
          v-if="!isSystemAgent"
          :model-value="autonomyEnabled"
          :loading="autonomyLoading"
          size="sm"
          class="nodrag"
          @toggle="handleAutonomyToggle"
        />
      </div>

      <!-- GitHub repo or placeholder (always shown for consistent height) -->
      <div class="flex items-center space-x-1 mb-3 h-4 pl-5">
        <template v-if="githubRepo">
          <svg class="w-3 h-3 text-gray-500 dark:text-gray-400 flex-shrink-0" fill="currentColor" viewBox="0 0 24 24">
            <path fill-rule="evenodd" d="M12 2C6.477 2 2 6.477 2 12c0 4.42 2.865 8.17 6.839 9.49.5.092.682-.217.682-.482 0-.237-.008-.866-.013-1.7-2.782.604-3.369-1.34-3.369-1.34-.454-1.156-1.11-1.464-1.11-1.464-.908-.62.069-.608.069-.608 1.003.07 1.531 1.03 1.531 1.03.892 1.529 2.341 1.087 2.91.831.092-.646.35-1.086.636-1.336-2.22-.253-4.555-1.11-4.555-4.943 0-1.091.39-1.984 1.029-2.683-.103-.253-.446-1.27.098-2.647 0 0 .84-.269 2.75 1.025A9.578 9.578 0 0112 6.836c.85.004 1.705.115 2.504.337 1.909-1.294 2.747-1.025 2.747-1.025.546 1.377.203 2.394.1 2.647.64.699 1.028 1.592 1.028 2.683 0 3.842-2.339 4.687-4.566 4.935.359.309.678.919.678 1.852 0 1.336-.012 2.415-.012 2.743 0 .267.18.578.688.48C19.138 20.167 22 16.418 22 12c0-5.523-4.477-10-10-10z" clip-rule="evenodd" />
          </svg>
          <span class="text-xs text-gray-500 dark:text-gray-400 truncate" :title="githubRepo">{{ githubRepoShort }}</span>
        </template>
        <template v-else>
          <svg class="w-3 h-3 text-gray-300 dark:text-gray-600 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14" />
          </svg>
          <span class="text-xs text-gray-300 dark:text-gray-600">Local agent</span>
        </template>
      </div>

      <!-- Success rate bar -->
      <div class="mb-3">
        <div class="flex items-center justify-between mb-1">
          <span class="text-xs text-gray-500 dark:text-gray-400">Success</span>
          <div class="flex items-center gap-1.5">
            <span v-if="hasSuccessData" class="text-xs font-semibold" :class="successBarColorText">{{ successBarPercent }}%</span>
            <span v-if="show7dSecondary" class="text-[10px] text-gray-400 dark:text-gray-500">(7d: {{ successRate7d }}%)</span>
            <span v-if="!hasSuccessData && !has7dOnly" class="text-xs text-gray-400 dark:text-gray-500">&mdash;</span>
            <span v-if="has7dOnly" class="text-xs font-semibold" :class="successBarColorText7d">{{ successRate7d }}%</span>
            <span v-if="has7dOnly" class="text-[10px] text-gray-400 dark:text-gray-500">(7d)</span>
          </div>
        </div>
        <div class="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2 overflow-hidden">
          <div
            v-if="hasSuccessData"
            class="h-full rounded-full transition-all duration-500"
            :class="successBarColor"
            :style="{ width: successBarPercent + '%' }"
          ></div>
          <div
            v-else-if="has7dOnly"
            class="h-full rounded-full transition-all duration-500"
            :class="successBarColor7d"
            :style="{ width: successRate7d + '%' }"
          ></div>
        </div>
      </div>

      <!-- Execution Stats (compact row) -->
      <div v-if="hasExecutionStats" class="flex items-center flex-wrap text-xs text-gray-500 dark:text-gray-400 gap-x-1.5 gap-y-0.5 mb-2">
        <span class="font-medium text-gray-700 dark:text-gray-300">{{ executionStats.taskCount }}</span>
        <span>tasks</span>
        <span class="text-gray-300 dark:text-gray-600">·</span>
        <span :class="successRateColorClass" class="font-medium">{{ executionStats.successRate }}%</span>
        <template v-if="executionStats.totalCost > 0">
          <span class="text-gray-300 dark:text-gray-600">·</span>
          <span class="font-medium text-gray-700 dark:text-gray-300">{{ formatCostCompact(executionStats.totalCost) }}</span>
        </template>
        <template v-if="lastExecutionDisplay">
          <span class="text-gray-300 dark:text-gray-600">·</span>
          <span>{{ lastExecutionDisplay }}</span>
        </template>
      </div>
      <div v-else class="text-xs text-gray-400 dark:text-gray-500 mb-2">
        No tasks (24h)
      </div>

      <!-- Schedule Stats (compact row - always show for consistent height) -->
      <div
        v-if="!isSystemAgent"
        :class="[
          'flex items-center text-xs gap-x-1.5 mb-2',
          hasSchedules && autonomyEnabled ? 'text-gray-500 dark:text-gray-400' : 'text-gray-300 dark:text-gray-600'
        ]"
      >
        <svg class="w-3 h-3 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        <span :class="hasSchedules && autonomyEnabled ? 'font-medium text-gray-700 dark:text-gray-300' : ''">
          {{ schedulesEnabled }}/{{ schedulesTotal }}
        </span>
        <span>schedules</span>
        <span v-if="hasSchedules && !autonomyEnabled" class="italic">(paused)</span>
      </div>

      <!-- Resource indicators (subtle footer) -->
      <div v-if="hasResourceInfo" class="flex items-center gap-4 text-xs text-gray-400 dark:text-gray-500 mb-3 pt-2 border-t border-gray-100 dark:border-gray-700/50">
        <!-- Memory -->
        <div class="flex items-center gap-1.5" :title="'Memory limit: ' + memoryDisplay">
          <svg class="w-3.5 h-3.5 text-gray-300 dark:text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z" />
          </svg>
          <span class="font-medium">{{ memoryDisplay }}</span>
        </div>
        <!-- CPU -->
        <div class="flex items-center gap-1.5" :title="'CPU limit: ' + cpuDisplay + ' cores'">
          <svg class="w-3.5 h-3.5 text-gray-300 dark:text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2z" />
          </svg>
          <span class="font-medium">{{ cpuDisplay }}</span>
          <span class="text-gray-300 dark:text-gray-600">cores</span>
        </div>
      </div>

      <!-- Click-through button (nodrag class prevents drag) - Hidden for system agents -->
      <button
        v-if="!isSystemAgent"
        class="nodrag w-full px-3 py-2 bg-blue-50 dark:bg-blue-900/30 hover:bg-blue-100 dark:hover:bg-blue-900/50 text-blue-700 dark:text-blue-300 rounded-lg text-xs font-semibold transition-all duration-200 border border-blue-200 dark:border-blue-700 hover:border-blue-300 dark:hover:border-blue-600 mt-auto"
        @click="viewDetails"
      >
        View Details
      </button>
      <!-- System agent: Link to System page instead -->
      <router-link
        v-else
        to="/system-agent"
        class="nodrag w-full px-3 py-2 bg-accent-purple-50 dark:bg-accent-purple-900/30 hover:bg-accent-purple-100 dark:hover:bg-accent-purple-900/50 text-accent-purple-700 dark:text-accent-purple-300 rounded-lg text-xs font-semibold transition-all duration-200 border border-accent-purple-200 dark:border-accent-purple-700 hover:border-accent-purple-300 dark:hover:border-accent-purple-600 mt-auto text-center block"
      >
        System Dashboard
      </router-link>
    </div>

    <Handle
      type="source"
      :position="Position.Bottom"
      class="w-3 h-3 border-2 bg-gray-300 dark:bg-gray-600 border-gray-100 dark:border-gray-500"
    />
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { formatCostCompact } from '../composables/useFormatters'
import { useRouter } from 'vue-router'
import { Handle, Position } from '@vue-flow/core'
import AgentAvatar from './AgentAvatar.vue'
import RuntimeBadge from './RuntimeBadge.vue'
import RunningStateToggle from './RunningStateToggle.vue'
import AutonomyToggle from './AutonomyToggle.vue'
import { useNetworkStore } from '../stores/network'

const props = defineProps({
  id: String,
  data: {
    type: Object,
    required: true
  }
})

const router = useRouter()
const networkStore = useNetworkStore()

// Autonomy toggle loading state
const autonomyLoading = ref(false)

// Running toggle loading state
const runningToggleLoading = ref(false)

// Check if agent is running
const isRunning = computed(() => {
  return props.data.status === 'running'
})

// Check if this is a system agent
const isSystemAgent = computed(() => {
  return props.data.is_system === true
})

// Check if autonomy mode is enabled
const autonomyEnabled = computed(() => {
  return props.data.autonomy_enabled === true
})

// #526: dispatch circuit breaker open — agent is auth-dead, dispatches fast-fail
const circuitOpen = computed(() => {
  return props.data.circuitBreaker?.state === 'open'
})

// Compute activity state (active, idle, offline)
const activityState = computed(() => {
  return props.data.activityState || 'offline'
})

const isActive = computed(() => {
  return activityState.value === 'active'
})

const activityStateLabel = computed(() => {
  const state = activityState.value
  if (state === 'active') return 'Active'
  if (state === 'idle') return 'Idle'
  return 'Offline'
})

const activityStateColor = computed(() => {
  const state = activityState.value
  if (state === 'active') return 'text-status-success-600 dark:text-status-success-400'
  if (state === 'idle') return 'text-status-success-600 dark:text-status-success-400'
  return 'text-gray-500 dark:text-gray-400'
})

const statusDotColor = computed(() => {
  const state = activityState.value
  if (state === 'active') return '#10b981' // green-500
  if (state === 'idle') return '#10b981' // green-500
  return '#9ca3af' // gray-400
})

// GitHub repo display
const githubRepo = computed(() => {
  return props.data.githubRepo || null
})

const githubRepoShort = computed(() => {
  if (!githubRepo.value) return ''
  // Extract owner/repo from full URL or github:owner/repo format
  const repo = githubRepo.value
  // Handle formats like "github:owner/repo", "https://github.com/owner/repo", or "owner/repo"
  if (repo.startsWith('github:')) {
    return repo.substring(7)
  }
  if (repo.includes('github.com/')) {
    const parts = repo.split('github.com/')[1]
    return parts.replace(/\.git$/, '')
  }
  return repo
})

// Success rate bar
const successBarPercent = computed(() => {
  if (!executionStats.value) return 0
  return Math.round(executionStats.value.successRate || 0)
})

const hasSuccessData = computed(() => {
  return executionStats.value && executionStats.value.taskCount > 0
})

const has7dOnly = computed(() => {
  return executionStats.value && executionStats.value.taskCount === 0 && (executionStats.value.taskCount7d || 0) > 0
})

const show7dSecondary = computed(() => {
  return hasSuccessData.value && (executionStats.value?.taskCount7d || 0) > 0
})

const successRate7d = computed(() => {
  if (!executionStats.value) return 0
  return Math.round(executionStats.value.successRate7d || 0)
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

const successBarColor7d = computed(() => {
  const percent = successRate7d.value
  if (percent >= 90) return 'bg-status-success-500'
  if (percent >= 50) return 'bg-status-warning-500'
  return 'bg-status-danger-500'
})

const successBarColorText7d = computed(() => {
  const percent = successRate7d.value
  if (percent >= 90) return 'text-status-success-600 dark:text-status-success-400'
  if (percent >= 50) return 'text-status-warning-600 dark:text-status-warning-400'
  return 'text-status-danger-600 dark:text-status-danger-400'
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

const lastExecutionDisplay = computed(() => {
  if (!executionStats.value?.lastExecutionAt) return null
  const lastTime = new Date(executionStats.value.lastExecutionAt)
  const now = new Date()
  const diffMs = now - lastTime
  const diffMins = Math.floor(diffMs / 60000)
  const diffHours = Math.floor(diffMs / 3600000)

  if (diffMins < 1) return 'just now'
  if (diffMins < 60) return `${diffMins}m ago`
  if (diffHours < 24) return `${diffHours}h ago`
  return `${Math.floor(diffHours / 24)}d ago`
})

// Schedule stats
const schedulesTotal = computed(() => {
  return executionStats.value?.schedulesTotal || 0
})

const schedulesEnabled = computed(() => {
  return executionStats.value?.schedulesEnabled || 0
})

const hasSchedules = computed(() => {
  return schedulesTotal.value > 0
})

// Resource indicators - always show for consistent card layout
const hasResourceInfo = computed(() => true)

const memoryDisplay = computed(() => {
  const mem = props.data.memoryLimit
  if (!mem) return '4g'
  return mem
})

const cpuDisplay = computed(() => {
  const cpu = props.data.cpuLimit
  if (!cpu) return '2'
  return cpu
})

function viewDetails() {
  if (isSystemAgent.value) {
    router.push('/system-agent')
  } else {
    router.push(`/agents/${props.data.label}`)
  }
}

// Handle autonomy toggle
async function handleAutonomyToggle() {
  if (autonomyLoading.value || isSystemAgent.value) return

  autonomyLoading.value = true
  try {
    await networkStore.toggleAutonomy(props.data.label)
  } finally {
    autonomyLoading.value = false
  }
}

// Handle running state toggle (start/stop)
async function handleRunningToggle() {
  if (runningToggleLoading.value || isSystemAgent.value) return

  runningToggleLoading.value = true
  try {
    await networkStore.toggleAgentRunning(props.data.label)
  } finally {
    runningToggleLoading.value = false
  }
}
</script>

<style scoped>
/* Ensure handles are visible and clickable */
:deep(.vue-flow__handle) {
  cursor: crosshair;
}

:deep(.vue-flow__handle-top) {
  top: -6px;
}

:deep(.vue-flow__handle-bottom) {
  bottom: -6px;
}

/* More pronounced and faster pulsing for active agents */
.active-pulse {
  animation: active-pulse-animation 0.8s ease-in-out infinite;
  box-shadow: 0 0 8px 2px rgba(16, 185, 129, 0.6);
}

@keyframes active-pulse-animation {
  0%, 100% {
    transform: scale(1);
    opacity: 1;
    box-shadow: 0 0 8px 2px rgba(16, 185, 129, 0.6);
  }
  50% {
    transform: scale(1.3);
    opacity: 0.8;
    box-shadow: 0 0 16px 4px rgba(16, 185, 129, 0.9);
  }
}
</style>
