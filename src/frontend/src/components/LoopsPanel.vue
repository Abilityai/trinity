<template>
  <div class="p-6">
    <!-- Header -->
    <div class="flex items-center justify-between mb-4">
      <div>
        <h3 class="text-lg font-medium text-gray-900 dark:text-white">Loops</h3>
        <p class="text-sm text-gray-500 dark:text-gray-400">
          Run a task repeatedly — fixed count or until a stop signal. Each iteration runs sequentially.
        </p>
      </div>
      <button
        type="button"
        @click="showForm = !showForm"
        :disabled="agentStatus !== 'running'"
        :title="agentStatus !== 'running' ? 'Agent must be running to start a loop' : ''"
        class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-action-primary-600 hover:bg-action-primary-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-action-primary-500 disabled:bg-gray-400 disabled:cursor-not-allowed"
      >
        {{ showForm ? 'Cancel' : 'Run Loop' }}
      </button>
    </div>

    <!-- Start form -->
    <div v-if="showForm" class="mb-6 p-4 border border-gray-200 dark:border-gray-700 rounded-lg bg-gray-50 dark:bg-gray-800/50">
      <form @submit.prevent="submit" class="space-y-4">
        <!-- Message template -->
        <div>
          <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Message template <span class="text-status-danger-500">*</span></label>
          <textarea
            v-model="form.message"
            rows="4"
            required
            :placeholder="messagePlaceholder"
            class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white rounded-md focus:outline-none focus:ring-2 focus:ring-action-primary-500"
          ></textarea>
          <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">
            Use <code class="px-1 bg-gray-200 dark:bg-gray-700 rounded">{{ RUN_VAR }}</code> for the 1-indexed run number and
            <code class="px-1 bg-gray-200 dark:bg-gray-700 rounded">{{ PREV_VAR }}</code> for the previous iteration's output.
          </p>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <!-- Max runs -->
          <div>
            <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Max runs <span class="text-status-danger-500">*</span></label>
            <input
              v-model.number="form.max_runs"
              type="number"
              min="1"
              max="100"
              required
              class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white rounded-md focus:outline-none focus:ring-2 focus:ring-action-primary-500"
            />
            <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">1–100</p>
          </div>

          <!-- Stop signal -->
          <div>
            <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Stop signal</label>
            <input
              v-model="form.stop_signal"
              type="text"
              placeholder="optional — substring that ends the loop"
              class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white rounded-md focus:outline-none focus:ring-2 focus:ring-action-primary-500"
            />
            <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">If a response contains this text, the loop stops early.</p>
          </div>

          <!-- Delay -->
          <div>
            <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Delay between runs (seconds)</label>
            <input
              v-model.number="form.delay_seconds"
              type="number"
              min="0"
              max="3600"
              class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white rounded-md focus:outline-none focus:ring-2 focus:ring-action-primary-500"
            />
          </div>

          <!-- Timeout per run -->
          <div>
            <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Timeout per run (seconds)</label>
            <input
              v-model.number="form.timeout_per_run"
              type="number"
              min="10"
              max="7200"
              placeholder="agent default"
              class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white rounded-md focus:outline-none focus:ring-2 focus:ring-action-primary-500"
            />
          </div>

          <!-- Max duration (wall-clock deadline) -->
          <div>
            <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Max duration (seconds)</label>
            <input
              v-model.number="form.max_duration_seconds"
              type="number"
              min="1"
              max="604800"
              placeholder="no deadline"
              class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white rounded-md focus:outline-none focus:ring-2 focus:ring-action-primary-500"
            />
            <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">Total wall-clock limit; checked between runs. Must be ≥ the per-run timeout.</p>
          </div>

          <!-- Max cost (USD budget) #1155 -->
          <div>
            <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Max cost (USD)</label>
            <input
              v-model.number="form.max_cost_usd"
              type="number"
              min="0.01"
              step="0.01"
              placeholder="no budget"
              class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white rounded-md focus:outline-none focus:ring-2 focus:ring-action-primary-500"
            />
            <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">Hard USD budget; checked between runs (the current run always finishes).</p>
          </div>

          <!-- No-progress threshold (doom-loop detection) #1157 -->
          <div>
            <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">No-progress threshold</label>
            <input
              v-model.number="form.no_progress_threshold"
              type="number"
              min="0"
              placeholder="3"
              class="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white rounded-md focus:outline-none focus:ring-2 focus:ring-action-primary-500"
            />
            <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">Stops after this many identical responses in a row (default 3). Set 0 to disable.</p>
          </div>
        </div>

        <!-- Model -->
        <div>
          <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Model</label>
          <ModelSelector v-model="form.model" placeholder="Agent default" />
        </div>

        <!-- Allowed tools -->
        <div>
          <div class="flex items-center justify-between mb-2">
            <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">Allowed tools</label>
            <button
              type="button"
              @click="toggleAllTools"
              class="text-xs px-2 py-1 rounded"
              :class="form.allowed_tools === null ? 'bg-action-primary-100 dark:bg-action-primary-900/30 text-action-primary-700 dark:text-action-primary-300' : 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'"
            >
              {{ form.allowed_tools === null ? 'All Tools (Unrestricted)' : 'Enable All' }}
            </button>
          </div>
          <div v-if="form.allowed_tools !== null" class="space-y-3">
            <div v-for="category in toolCategories" :key="category.name">
              <p class="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">{{ category.name }}</p>
              <div class="flex flex-wrap gap-2">
                <label
                  v-for="tool in category.tools"
                  :key="tool.value"
                  class="inline-flex items-center px-2 py-1 rounded text-xs cursor-pointer transition-colors"
                  :class="isToolSelected(tool.value) ? 'bg-action-primary-100 dark:bg-action-primary-900/30 text-action-primary-700 dark:text-action-primary-300' : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-600'"
                >
                  <input
                    type="checkbox"
                    :value="tool.value"
                    :checked="isToolSelected(tool.value)"
                    @change="toggleTool(tool.value)"
                    class="sr-only"
                  />
                  {{ tool.label }}
                </label>
              </div>
            </div>
          </div>
          <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">
            {{ form.allowed_tools === null ? 'Agent can use any tool' : `${form.allowed_tools.length} tool(s) selected` }}
          </p>
        </div>

        <div v-if="store.error" class="p-3 bg-status-danger-50 dark:bg-status-danger-900/30 text-status-danger-700 dark:text-status-danger-300 text-sm rounded-md">
          {{ store.error }}
        </div>

        <div class="flex justify-end gap-2">
          <button
            type="button"
            @click="resetForm"
            class="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-700 border border-gray-300 dark:border-gray-600 rounded-md hover:bg-gray-50 dark:hover:bg-gray-600"
          >
            Reset
          </button>
          <button
            type="submit"
            :disabled="store.starting || !form.message"
            class="px-4 py-2 text-sm font-medium text-white bg-action-primary-600 border border-transparent rounded-md hover:bg-action-primary-700 disabled:bg-gray-400 disabled:cursor-not-allowed"
          >
            {{ store.starting ? 'Starting…' : 'Start Loop' }}
          </button>
        </div>
      </form>
    </div>

    <!-- Loading -->
    <div v-if="store.loading && !store.loops.length" class="text-center py-8 text-gray-500 dark:text-gray-400">
      Loading loops…
    </div>

    <!-- Empty -->
    <div v-else-if="!store.loops.length" class="text-center py-8 text-gray-500 dark:text-gray-400">
      No loops yet. Start one with “Run Loop”.
    </div>

    <!-- Loop list -->
    <div v-else class="space-y-3">
      <div
        v-for="loop in store.loops"
        :key="loop.loop_id"
        class="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden"
      >
        <!-- Row header -->
        <div
          class="flex items-center justify-between p-4 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/50"
          @click="store.toggleExpanded(loop.loop_id)"
        >
          <div class="flex items-center gap-3 min-w-0">
            <span
              class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium"
              :class="statusBadgeClass(loop.status)"
            >
              <span v-if="loop.status === 'running'" class="w-1.5 h-1.5 rounded-full bg-current mr-1 animate-pulse"></span>
              {{ loop.status }}
            </span>
            <span class="text-sm font-medium text-gray-900 dark:text-white">
              Run {{ loop.runs_completed }} / {{ loop.max_runs }}
            </span>
            <span v-if="loop.stop_reason" class="text-xs text-gray-500 dark:text-gray-400 truncate">
              · {{ formatStopReason(loop.stop_reason) }}
            </span>
          </div>
          <div class="flex items-center gap-2 flex-shrink-0">
            <span class="text-xs text-gray-400 dark:text-gray-500 hidden sm:inline">{{ formatDate(loop.created_at) }}</span>
            <button
              v-if="isActive(loop)"
              type="button"
              @click.stop="store.stopLoop(loop.loop_id)"
              :disabled="store.stoppingIds.includes(loop.loop_id)"
              class="px-2.5 py-1 text-xs font-medium rounded-md text-status-danger-700 dark:text-status-danger-300 bg-status-danger-50 dark:bg-status-danger-900/30 hover:bg-status-danger-100 dark:hover:bg-status-danger-900/50 disabled:opacity-60 disabled:cursor-not-allowed"
            >
              {{ store.stoppingIds.includes(loop.loop_id) ? 'Stopping…' : 'Stop' }}
            </button>
            <svg
              class="w-4 h-4 text-gray-400 transition-transform"
              :class="store.expandedLoopId === loop.loop_id ? 'rotate-180' : ''"
              fill="none" stroke="currentColor" viewBox="0 0 24 24"
            >
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
            </svg>
          </div>
        </div>

        <!-- Expanded detail -->
        <div v-if="store.expandedLoopId === loop.loop_id" class="border-t border-gray-200 dark:border-gray-700 p-4 space-y-4 bg-gray-50 dark:bg-gray-800/30">
          <div v-if="loop.error" class="p-3 bg-status-danger-50 dark:bg-status-danger-900/30 text-status-danger-700 dark:text-status-danger-300 text-sm rounded-md">
            {{ loop.error }}
          </div>

          <!-- Deadline (#1156) -->
          <div v-if="loop.max_duration_seconds" class="text-xs text-gray-600 dark:text-gray-400">
            <span class="font-medium text-gray-500 dark:text-gray-400">Deadline:</span>
            {{ formatSeconds(loop.elapsed_seconds) }} / {{ formatSeconds(loop.max_duration_seconds) }} elapsed
          </div>

          <!-- Cost budget (#1155) -->
          <div v-if="loop.max_cost_usd" class="text-xs text-gray-600 dark:text-gray-400">
            <span class="font-medium text-gray-500 dark:text-gray-400">Budget:</span>
            {{ formatCost(loop.total_cost) }} / {{ formatCost(loop.max_cost_usd) }} spent
          </div>

          <!-- Per-run table -->
          <div>
            <p class="text-xs font-medium text-gray-500 dark:text-gray-400 mb-2">Runs</p>
            <div v-if="!loop.runs || !loop.runs.length" class="text-sm text-gray-500 dark:text-gray-400">
              No runs yet.
            </div>
            <div v-else class="overflow-x-auto">
              <table class="min-w-full text-sm">
                <thead>
                  <tr class="text-left text-xs text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700">
                    <th class="py-1 pr-4 font-medium">#</th>
                    <th class="py-1 pr-4 font-medium">Status</th>
                    <th class="py-1 pr-4 font-medium">Cost</th>
                    <th class="py-1 pr-4 font-medium">Duration</th>
                    <th class="py-1 font-medium">Response</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="run in loop.runs" :key="run.run_number" class="border-b border-gray-100 dark:border-gray-800 align-top">
                    <td class="py-1.5 pr-4 text-gray-700 dark:text-gray-300">{{ run.run_number }}</td>
                    <td class="py-1.5 pr-4">
                      <span :class="runStatusClass(run.status)">{{ run.status }}</span>
                    </td>
                    <td class="py-1.5 pr-4 text-gray-600 dark:text-gray-400">{{ run.cost != null ? formatCost(run.cost) : '—' }}</td>
                    <td class="py-1.5 pr-4 text-gray-600 dark:text-gray-400">{{ formatDuration(run.duration_ms) }}</td>
                    <td class="py-1.5 text-gray-600 dark:text-gray-400">
                      <span v-if="run.error" class="text-status-danger-600 dark:text-status-danger-400">{{ run.error }}</span>
                      <span v-else class="line-clamp-2">{{ run.response_preview || '—' }}</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <!-- Last full response -->
          <div v-if="loop.last_response">
            <p class="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Last response</p>
            <div
              class="prose prose-sm dark:prose-invert max-w-none max-h-80 overflow-y-auto p-3 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md"
              v-html="renderMarkdown(loop.last_response)"
            ></div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted, watch } from 'vue'
import { useLoopsStore } from '../stores/loops'
import { renderMarkdown } from '../utils/markdown'
import { formatCost } from '../composables/useFormatters'
import ModelSelector from './ModelSelector.vue'

const props = defineProps({
  agentName: { type: String, required: true },
  agentStatus: { type: String, default: '' },
})

const store = useLoopsStore()
const showForm = ref(false)

// Literal template placeholders — held as constants because Vue's template
// parser can't tokenize `{{run}}` written inline in markup.
const RUN_VAR = '{{run}}'
const PREV_VAR = '{{previous_response}}'
const messagePlaceholder = `e.g. Process item ${RUN_VAR}. Previous result: ${PREV_VAR}`

const ACTIVE_STATUSES = ['queued', 'running']

const defaultForm = () => ({
  message: '',
  max_runs: 5,
  stop_signal: '',
  delay_seconds: 0,
  timeout_per_run: null,
  max_duration_seconds: null,
  max_cost_usd: null,
  no_progress_threshold: 3,
  model: '',
  allowed_tools: null,
})
const form = reactive(defaultForm())

const toolCategories = [
  { name: 'File', tools: [
    { value: 'Read', label: 'Read' },
    { value: 'Write', label: 'Write' },
    { value: 'Edit', label: 'Edit' },
  ] },
  { name: 'Search', tools: [
    { value: 'Glob', label: 'Glob' },
    { value: 'Grep', label: 'Grep' },
  ] },
  { name: 'Execution', tools: [
    { value: 'Bash', label: 'Bash' },
  ] },
  { name: 'Web', tools: [
    { value: 'WebFetch', label: 'WebFetch' },
    { value: 'WebSearch', label: 'WebSearch' },
  ] },
]

function isToolSelected(tool) {
  return form.allowed_tools !== null && form.allowed_tools.includes(tool)
}
function toggleTool(tool) {
  if (form.allowed_tools === null) {
    form.allowed_tools = [tool]
  } else if (form.allowed_tools.includes(tool)) {
    form.allowed_tools = form.allowed_tools.filter((t) => t !== tool)
  } else {
    form.allowed_tools = [...form.allowed_tools, tool]
  }
}
function toggleAllTools() {
  form.allowed_tools = form.allowed_tools === null ? [] : null
}

function resetForm() {
  Object.assign(form, defaultForm())
  store.error = null
}

async function submit() {
  const payload = {
    message: form.message,
    max_runs: form.max_runs,
  }
  if (form.stop_signal && form.stop_signal.trim()) payload.stop_signal = form.stop_signal.trim()
  if (form.delay_seconds) payload.delay_seconds = form.delay_seconds
  if (form.timeout_per_run) payload.timeout_per_run = form.timeout_per_run
  if (form.max_duration_seconds) payload.max_duration_seconds = form.max_duration_seconds
  if (form.max_cost_usd) payload.max_cost_usd = form.max_cost_usd
  // 0 is the disable sentinel — a truthy guard would drop it, so send explicitly.
  if (form.no_progress_threshold !== null && form.no_progress_threshold !== undefined && form.no_progress_threshold !== '') {
    payload.no_progress_threshold = form.no_progress_threshold
  }
  if (form.model) payload.model = form.model
  if (form.allowed_tools !== null) payload.allowed_tools = form.allowed_tools

  const ok = await store.startLoop(payload)
  if (ok) {
    resetForm()
    showForm.value = false
  }
}

// --- formatting / styling helpers ---
function isActive(loop) {
  return ACTIVE_STATUSES.includes(loop.status)
}

function statusBadgeClass(status) {
  switch (status) {
    case 'running':
    case 'queued':
      return 'bg-status-warning-100 dark:bg-status-warning-900/30 text-status-warning-800 dark:text-status-warning-300'
    case 'completed':
      return 'bg-status-success-100 dark:bg-status-success-900/30 text-status-success-800 dark:text-status-success-300'
    case 'failed':
      return 'bg-status-danger-100 dark:bg-status-danger-900/30 text-status-danger-800 dark:text-status-danger-300'
    case 'stopped':
    case 'interrupted':
      return 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300'
    default:
      return 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400'
  }
}

function runStatusClass(status) {
  if (status === 'completed') return 'text-status-success-600 dark:text-status-success-400'
  if (status === 'failed') return 'text-status-danger-600 dark:text-status-danger-400'
  return 'text-status-warning-600 dark:text-status-warning-400'
}

function formatStopReason(reason) {
  const map = {
    max_runs_reached: 'reached max runs',
    stop_signal_matched: 'stop signal matched',
    user_stopped: 'stopped by user',
    deadline_exceeded: 'deadline exceeded',
    budget_exhausted: 'budget exhausted',
    no_progress: 'no progress — identical responses',
    error: 'error',
    interrupted: 'interrupted',
  }
  return map[reason] || reason
}

function formatSeconds(secs) {
  if (secs === null || secs === undefined) return '—'
  if (secs < 60) return `${secs}s`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${secs % 60}s`
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  return `${h}h ${m}m`
}


function formatDuration(ms) {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

function formatDate(iso) {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

// --- lifecycle: bind the store to this agent and fetch ---
function bind(name) {
  store.setAgent(name)
  store.fetchLoops()
}

onMounted(() => bind(props.agentName))
watch(() => props.agentName, (name) => { if (name) bind(name) })
onUnmounted(() => store.clear())
</script>
