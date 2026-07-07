<template>
  <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
    <!-- Compact Header (always visible) -->
    <div class="px-4 py-3 bg-gray-50 dark:bg-gray-700 border-b border-gray-200 dark:border-gray-600">
      <div class="flex items-center justify-between">
        <div class="flex items-center space-x-3">
          <span class="text-sm font-medium text-gray-700 dark:text-gray-200">Session Activity</span>

          <!-- Status indicator + current tool -->
          <div class="flex items-center space-x-2">
            <span
              :class="[
                'w-2 h-2 rounded-full',
                activity.status === 'running' ? 'bg-status-info-500 animate-pulse' : 'bg-gray-400 dark:bg-gray-500'
              ]"
            ></span>
            <span class="text-sm text-gray-600 dark:text-gray-300">
              <template v-if="activity.active_tool">
                {{ activity.active_tool.name }}: {{ truncate(activity.active_tool.input_summary, 30) }}
              </template>
              <template v-else>
                Idle
              </template>
            </span>
          </div>
        </div>

        <div class="flex items-center space-x-4">
          <!-- Stats -->
          <div class="flex items-center space-x-3 text-xs text-gray-500 dark:text-gray-400">
            <span v-if="activity.totals?.calls > 0">
              {{ activity.totals.calls }} call{{ activity.totals.calls !== 1 ? 's' : '' }}
            </span>
            <span v-if="sessionCost > 0">{{ formatCost(sessionCost) }}</span>
            <span v-if="elapsedTime">{{ elapsedTime }}</span>
          </div>

          <!-- Expand button -->
          <button
            @click="expanded = !expanded"
            class="text-gray-400 hover:text-gray-600 dark:text-gray-500 dark:hover:text-gray-300 transition-colors"
            :title="expanded ? 'Collapse timeline' : 'View timeline'"
          >
            <svg
              class="w-4 h-4 transition-transform duration-200"
              :class="{ 'rotate-180': expanded }"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
            </svg>
          </button>
        </div>
      </div>

      <!-- Tool chips (always visible when there are tools) -->
      <div v-if="Object.keys(activity.tool_counts || {}).length > 0" class="mt-2 flex flex-wrap gap-1.5">
        <span
          v-for="(count, tool) in sortedToolCounts"
          :key="tool"
          class="inline-flex items-center px-2 py-0.5 rounded text-xs font-mono bg-gray-100 dark:bg-gray-600 text-gray-600 dark:text-gray-300"
        >
          {{ tool }}
          <span class="ml-1 text-gray-400 dark:text-gray-500">x{{ count }}</span>
        </span>
      </div>
    </div>

    <!-- Expanded Timeline -->
    <div
      v-show="expanded"
      class="max-h-80 overflow-y-auto divide-y divide-gray-100 dark:divide-gray-700"
    >
      <div v-if="!activity.timeline?.length" class="px-4 py-6 text-center text-gray-500 dark:text-gray-400 text-sm">
        No activity yet
      </div>

      <div
        v-for="entry in activity.timeline"
        :key="entry.id"
        class="px-4 py-2 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors cursor-pointer"
        @click="selectEntry(entry)"
      >
        <div class="flex items-center justify-between">
          <div class="flex items-center space-x-3">
            <!-- Status indicator -->
            <span
              :class="[
                'w-2 h-2 rounded-full flex-shrink-0',
                entry.status === 'running' ? 'bg-status-info-500 animate-pulse' :
                entry.success ? 'bg-status-success-500' : 'bg-status-danger-500'
              ]"
            ></span>

            <!-- Timestamp -->
            <span class="text-xs text-gray-400 dark:text-gray-500 font-mono w-16 flex-shrink-0">
              {{ formatTime(entry.started_at) }}
            </span>

            <!-- Tool name -->
            <span class="text-sm font-medium text-gray-700 dark:text-gray-200">
              {{ entry.tool }}
            </span>

            <!-- Input summary -->
            <span class="text-sm text-gray-500 dark:text-gray-400 truncate max-w-xs">
              {{ entry.input_summary }}
            </span>
          </div>

          <div class="flex items-center space-x-2 text-xs text-gray-400 dark:text-gray-500">
            <span v-if="entry.duration_ms !== null">
              {{ formatDuration(entry.duration_ms) }}
            </span>
            <span v-else-if="entry.status === 'running'">
              running...
            </span>

            <!-- Expand icon -->
            <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
            </svg>
          </div>
        </div>
      </div>

      <!-- Load more button (for pagination) -->
      <div v-if="activity.timeline?.length >= 20" class="px-4 py-2 text-center">
        <button class="text-xs text-action-primary-600 dark:text-action-primary-400 hover:text-action-primary-800 dark:hover:text-action-primary-300">
          Load more...
        </button>
      </div>
    </div>

    <!-- Detail Modal -->
    <div
      v-if="selectedEntry"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-50"
      @click.self="selectedEntry = null"
    >
      <div class="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-2xl w-full mx-4 max-h-[80vh] overflow-hidden flex flex-col">
        <!-- Modal Header -->
        <div class="px-4 py-3 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between bg-gray-50 dark:bg-gray-700">
          <div class="flex items-center space-x-3">
            <span
              :class="[
                'w-2 h-2 rounded-full',
                selectedEntry.status === 'running' ? 'bg-status-info-500 animate-pulse' :
                selectedEntry.success ? 'bg-status-success-500' : 'bg-status-danger-500'
              ]"
            ></span>
            <span class="font-medium text-gray-900 dark:text-white">{{ selectedEntry.tool }}</span>
            <span class="text-sm text-gray-500 dark:text-gray-400">{{ formatTime(selectedEntry.started_at) }}</span>
          </div>
          <button
            @click="selectedEntry = null"
            class="text-gray-400 hover:text-gray-600 dark:text-gray-500 dark:hover:text-gray-300"
          >
            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <!-- Modal Body -->
        <div class="flex-1 overflow-y-auto p-4 space-y-4">
          <!-- Stats row -->
          <div class="flex items-center space-x-4 text-sm text-gray-600 dark:text-gray-300">
            <span v-if="selectedEntry.duration_ms !== null">
              Duration: {{ formatDuration(selectedEntry.duration_ms) }}
            </span>
            <span v-if="selectedEntry.success !== null">
              Status:
              <span :class="selectedEntry.success ? 'text-status-success-600 dark:text-status-success-400' : 'text-status-danger-600 dark:text-status-danger-400'">
                {{ selectedEntry.success ? 'Success' : 'Error' }}
              </span>
            </span>
          </div>

          <!-- Input section -->
          <div>
            <h4 class="text-sm font-medium text-gray-700 dark:text-gray-200 mb-2">Input</h4>
            <pre class="bg-gray-900 text-gray-100 p-3 rounded text-xs overflow-x-auto font-mono">{{ formatJson(selectedEntry.input) }}</pre>
          </div>

          <!-- Output section -->
          <div v-if="detailLoading">
            <h4 class="text-sm font-medium text-gray-700 dark:text-gray-200 mb-2">Output</h4>
            <div class="flex items-center space-x-2 text-gray-500 dark:text-gray-400 text-sm">
              <div class="animate-spin h-4 w-4 border-2 border-gray-400 border-t-transparent rounded-full"></div>
              <span>Loading...</span>
            </div>
          </div>
          <div v-else-if="selectedDetail?.output">
            <h4 class="text-sm font-medium text-gray-700 dark:text-gray-200 mb-2">Output</h4>
            <pre class="bg-gray-900 text-gray-100 p-3 rounded text-xs overflow-x-auto font-mono max-h-64 overflow-y-auto">{{ truncateOutput(selectedDetail.output) }}</pre>
          </div>
          <div v-else-if="selectedEntry.output_summary">
            <h4 class="text-sm font-medium text-gray-700 dark:text-gray-200 mb-2">Output (summary)</h4>
            <pre class="bg-gray-900 text-gray-100 p-3 rounded text-xs overflow-x-auto font-mono">{{ selectedEntry.output_summary }}</pre>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { formatCost } from '../composables/useFormatters'
import { useAgentsStore } from '../stores/agents'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  },
  activity: {
    type: Object,
    default: () => ({
      status: 'idle',
      active_tool: null,
      tool_counts: {},
      timeline: [],
      totals: { calls: 0, duration_ms: 0, started_at: null }
    })
  },
  sessionCost: {
    type: Number,
    default: 0
  }
})

const agentsStore = useAgentsStore()

const expanded = ref(false)
const selectedEntry = ref(null)
const selectedDetail = ref(null)
const detailLoading = ref(false)

// Sort tool counts by frequency (most used first)
const sortedToolCounts = computed(() => {
  const counts = props.activity.tool_counts || {}
  return Object.fromEntries(
    Object.entries(counts).sort((a, b) => b[1] - a[1])
  )
})

// Format elapsed time from totals
const elapsedTime = computed(() => {
  const ms = props.activity.totals?.duration_ms
  if (!ms) return null
  return formatDuration(ms)
})

// Load detail when entry is selected
watch(selectedEntry, async (entry) => {
  if (entry && entry.status === 'completed') {
    detailLoading.value = true
    try {
      selectedDetail.value = await agentsStore.getActivityDetail(props.agentName, entry.id)
    } catch (err) {
      console.error('Failed to load activity detail:', err)
      selectedDetail.value = null
    } finally {
      detailLoading.value = false
    }
  } else {
    selectedDetail.value = null
  }
})

function selectEntry(entry) {
  selectedEntry.value = entry
}

function formatTime(isoString) {
  if (!isoString) return ''
  const date = new Date(isoString)
  return date.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function formatDuration(ms) {
  if (ms === null || ms === undefined) return ''
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60000)}m ${Math.floor((ms % 60000) / 1000)}s`
}

function formatJson(obj) {
  if (!obj) return '{}'
  try {
    return JSON.stringify(obj, null, 2)
  } catch {
    return String(obj)
  }
}

function truncate(str, maxLen) {
  if (!str) return ''
  if (str.length <= maxLen) return str
  return str.substring(0, maxLen) + '...'
}

function truncateOutput(output) {
  if (!output) return ''
  const maxLen = 5000
  if (output.length <= maxLen) return output
  return output.substring(0, maxLen) + '\n\n... (truncated)'
}
</script>
