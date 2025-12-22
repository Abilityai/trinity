<template>
  <div
    :class="[
      'px-5 py-4 rounded-xl border-2 shadow-lg',
      'transition-all duration-200 hover:shadow-xl cursor-move',
      'relative',
      'flex flex-col',
      // System agent gets distinct purple styling
      isSystemAgent
        ? 'bg-purple-50 dark:bg-purple-900/20 border-purple-300 dark:border-purple-700'
        : 'bg-white dark:bg-gray-800 border-gray-200 dark:border-gray-700'
    ]"
    style="width: 280px; min-height: 160px;"
  >
    <!-- Connection handles -->
    <Handle
      type="target"
      :position="Position.Top"
      class="w-3 h-3 border-2 bg-gray-300 dark:bg-gray-600 border-gray-100 dark:border-gray-500"
    />

    <!-- Agent info -->
    <div class="flex flex-col flex-grow">
      <!-- Header with name, system badge, and status dot -->
      <div class="flex items-center justify-between mb-2">
        <div class="flex items-center flex-1 mr-2 min-w-0">
          <div class="text-gray-900 dark:text-white font-bold text-base truncate" :title="data.label">
            {{ data.label }}
          </div>
          <!-- System agent badge -->
          <span
            v-if="isSystemAgent"
            class="ml-2 px-1.5 py-0.5 text-xs font-semibold rounded bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-300 flex-shrink-0"
            title="System Agent - Platform Orchestrator"
          >
            SYSTEM
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

      <!-- Activity state label -->
      <div class="flex items-center space-x-2 mb-2">
        <div
          :class="[
            'text-xs font-medium capitalize',
            activityStateColor
          ]"
        >
          {{ activityStateLabel }}
        </div>
      </div>

      <!-- GitHub repo (if from GitHub template) -->
      <div v-if="githubRepo" class="flex items-center space-x-1 mb-3">
        <svg class="w-3 h-3 text-gray-500 dark:text-gray-400 flex-shrink-0" fill="currentColor" viewBox="0 0 24 24">
          <path fill-rule="evenodd" d="M12 2C6.477 2 2 6.477 2 12c0 4.42 2.865 8.17 6.839 9.49.5.092.682-.217.682-.482 0-.237-.008-.866-.013-1.7-2.782.604-3.369-1.34-3.369-1.34-.454-1.156-1.11-1.464-1.11-1.464-.908-.62.069-.608.069-.608 1.003.07 1.531 1.03 1.531 1.03.892 1.529 2.341 1.087 2.91.831.092-.646.35-1.086.636-1.336-2.22-.253-4.555-1.11-4.555-4.943 0-1.091.39-1.984 1.029-2.683-.103-.253-.446-1.27.098-2.647 0 0 .84-.269 2.75 1.025A9.578 9.578 0 0112 6.836c.85.004 1.705.115 2.504.337 1.909-1.294 2.747-1.025 2.747-1.025.546 1.377.203 2.394.1 2.647.64.699 1.028 1.592 1.028 2.683 0 3.842-2.339 4.687-4.566 4.935.359.309.678.919.678 1.852 0 1.336-.012 2.415-.012 2.743 0 .267.18.578.688.48C19.138 20.167 22 16.418 22 12c0-5.523-4.477-10-10-10z" clip-rule="evenodd" />
        </svg>
        <span class="text-xs text-gray-500 dark:text-gray-400 truncate" :title="githubRepo">{{ githubRepoShort }}</span>
      </div>

      <!-- Context progress bar -->
      <div v-if="showProgressBar" class="mb-3">
        <div class="flex items-center justify-between mb-1">
          <span class="text-xs text-gray-500 dark:text-gray-400">Context</span>
          <span class="text-xs font-semibold text-gray-700 dark:text-gray-300">{{ contextPercentDisplay }}%</span>
        </div>
        <div class="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2 overflow-hidden">
          <div
            class="h-full rounded-full transition-all duration-500"
            :class="progressBarColor"
            :style="{ width: contextPercentDisplay + '%' }"
          ></div>
        </div>
      </div>

      <!-- Task DAG Progress (always shown for consistent card height) -->
      <div class="mb-3">
        <!-- Current task (only shown when there is one) -->
        <div v-if="currentTask" class="mb-2">
          <div class="flex items-center space-x-1 mb-1">
            <svg class="w-3 h-3 text-blue-500 flex-shrink-0 animate-pulse" fill="currentColor" viewBox="0 0 20 20">
              <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-8.707l-3-3a1 1 0 00-1.414 0l-3 3a1 1 0 001.414 1.414L9 9.414V13a1 1 0 102 0V9.414l1.293 1.293a1 1 0 001.414-1.414z" clip-rule="evenodd" />
            </svg>
            <span class="text-xs text-gray-500 dark:text-gray-400">Current Task</span>
          </div>
          <div class="text-xs font-medium text-gray-700 dark:text-gray-300 truncate pl-4" :title="currentTask">
            {{ currentTask }}
          </div>
        </div>
        <!-- Task progress bar (always shown) -->
        <div class="flex items-center justify-between mb-1">
          <span class="text-xs text-gray-500 dark:text-gray-400 flex items-center space-x-1">
            <svg class="w-3 h-3 text-purple-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" />
            </svg>
            <span>Tasks</span>
          </span>
          <span class="text-xs font-semibold text-gray-700 dark:text-gray-300">{{ taskProgressDisplay }}</span>
        </div>
        <div class="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2 overflow-hidden">
          <div
            class="h-full rounded-full transition-all duration-500 bg-purple-500"
            :style="{ width: taskProgressPercent + '%' }"
          ></div>
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
        class="nodrag w-full px-3 py-2 bg-purple-50 dark:bg-purple-900/30 hover:bg-purple-100 dark:hover:bg-purple-900/50 text-purple-700 dark:text-purple-300 rounded-lg text-xs font-semibold transition-all duration-200 border border-purple-200 dark:border-purple-700 hover:border-purple-300 dark:hover:border-purple-600 mt-auto text-center block"
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
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { Handle, Position } from '@vue-flow/core'

const props = defineProps({
  id: String,
  data: {
    type: Object,
    required: true
  }
})

const router = useRouter()

// Check if this is a system agent
const isSystemAgent = computed(() => {
  return props.data.is_system === true
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
  if (state === 'active') return 'text-green-600 dark:text-green-400'
  if (state === 'idle') return 'text-green-600 dark:text-green-400'
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

// Context progress bar
const contextPercentDisplay = computed(() => {
  const percent = props.data.contextPercent || 0
  return Math.round(percent)
})

const showProgressBar = computed(() => {
  // Always show progress bar for consistent card height
  return true
})

const progressBarColor = computed(() => {
  const percent = contextPercentDisplay.value
  if (percent >= 90) return 'bg-red-500'
  if (percent >= 75) return 'bg-orange-500'
  if (percent >= 50) return 'bg-yellow-500'
  return 'bg-green-500'
})

// Task DAG progress computed properties
const hasActivePlan = computed(() => {
  return props.data.activePlan || props.data.totalTasks > 0
})

const currentTask = computed(() => {
  return props.data.currentTask || null
})

const completedTasks = computed(() => {
  return props.data.completedTasks || 0
})

const totalTasks = computed(() => {
  return props.data.totalTasks || 0
})

const taskProgressPercent = computed(() => {
  if (totalTasks.value === 0) return 0
  return Math.round((completedTasks.value / totalTasks.value) * 100)
})

const taskProgressDisplay = computed(() => {
  if (totalTasks.value === 0) return '—'
  return `${completedTasks.value}/${totalTasks.value}`
})

function viewDetails() {
  router.push(`/agents/${props.data.label}`)
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
