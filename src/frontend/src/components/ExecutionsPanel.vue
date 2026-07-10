<template>
  <!--
    Fleet executions panel, embedded as the "Executions" tab of
    Operations.vue (#1109). Extracted from the former views/Executions.vue:
    no <NavBar/> and no min-h-screen/<main> page wrapper. Polling lifecycle
    (onMounted start / onUnmounted stop) is preserved — the parent toggles
    this panel with v-if so the 30s poll is torn down when the tab is left.
    The per-execution detail route (/agents/:name/executions/:executionId)
    is unchanged.
  -->
  <div class="max-w-7xl mx-auto">
    <!-- Toolbar: live status + refresh -->
    <div class="flex items-center justify-end gap-3 mb-6">
      <span class="flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400" :title="wsTooltip">
        <span
          class="inline-block w-2 h-2 rounded-full"
          :class="isConnected ? 'bg-status-success-500' : 'bg-status-warning-500 animate-pulse'"
        ></span>
        {{ isConnected ? 'Live' : 'Polling' }}
      </span>
      <button
        @click="store.refresh()"
        :disabled="store.loading"
        class="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors disabled:opacity-50"
      >
        <svg class="w-3.5 h-3.5" :class="{ 'animate-spin': store.loading }" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
        </svg>
        Refresh
      </button>
    </div>

    <!-- Stat cards -->
    <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
      <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2">
        <p class="text-[10px] uppercase tracking-wide text-gray-500 dark:text-gray-400">Total</p>
        <p class="text-base font-semibold text-gray-900 dark:text-white">{{ store.stats?.total ?? '—' }}</p>
      </div>
      <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2">
        <p class="text-[10px] uppercase tracking-wide text-gray-500 dark:text-gray-400">Success rate</p>
        <p class="text-base font-semibold" :class="successRateClass">
          {{ store.stats ? store.stats.success_rate + '%' : '—' }}
        </p>
      </div>
      <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2">
        <p class="text-[10px] uppercase tracking-wide text-gray-500 dark:text-gray-400">Failed</p>
        <p class="text-base font-semibold" :class="store.stats?.failed_count > 0 ? 'text-status-danger-600 dark:text-status-danger-400' : 'text-gray-900 dark:text-white'">
          {{ store.stats?.failed_count ?? '—' }}
        </p>
      </div>
      <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2">
        <p class="text-[10px] uppercase tracking-wide text-gray-500 dark:text-gray-400">Cost</p>
        <p class="text-base font-semibold text-gray-900 dark:text-white">
          {{ store.stats ? formatCostCompact(store.stats.total_cost) : '—' }}
        </p>
      </div>
    </div>

    <!-- Filter bar -->
    <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg mb-4">
      <div class="flex flex-wrap items-center gap-2 px-4 py-3 border-b border-gray-200 dark:border-gray-700">
        <!-- Agent filter -->
        <div class="relative">
          <select
            :value="store.filters.agent"
            @change="store.setFilter('agent', $event.target.value)"
            class="appearance-none text-sm rounded-lg pl-3 pr-8 py-1.5 bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-200 focus:outline-none focus:ring-2 focus:ring-action-primary-500 cursor-pointer transition-colors"
            :class="store.filters.agent
              ? 'border-2 border-action-primary-500 font-medium'
              : 'border border-gray-200 dark:border-gray-600 hover:border-gray-300 dark:hover:border-gray-500'"
          >
            <option value="">All agents</option>
            <option v-for="name in agentNames" :key="name" :value="name">{{ name }}</option>
          </select>
          <svg class="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M19 9l-7 7-7-7"/></svg>
        </div>

        <!-- Status filter -->
        <div class="relative">
          <select
            :value="store.filters.status"
            @change="store.setFilter('status', $event.target.value)"
            class="appearance-none text-sm rounded-lg pl-3 pr-8 py-1.5 bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-200 focus:outline-none focus:ring-2 focus:ring-action-primary-500 cursor-pointer transition-colors"
            :class="store.filters.status
              ? 'border-2 border-action-primary-500 font-medium'
              : 'border border-gray-200 dark:border-gray-600 hover:border-gray-300 dark:hover:border-gray-500'"
          >
            <option value="">All statuses</option>
            <option value="running">running</option>
            <option value="queued">queued</option>
            <option value="success">success</option>
            <option value="failed">failed</option>
            <option value="error">error</option>
            <option value="cancelled">cancelled</option>
            <option value="skipped">skipped</option>
          </select>
          <svg class="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M19 9l-7 7-7-7"/></svg>
        </div>

        <!-- Trigger filter -->
        <div class="relative">
          <select
            :value="store.filters.triggered_by"
            @change="store.setFilter('triggered_by', $event.target.value)"
            class="appearance-none text-sm rounded-lg pl-3 pr-8 py-1.5 bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-200 focus:outline-none focus:ring-2 focus:ring-action-primary-500 cursor-pointer transition-colors"
            :class="store.filters.triggered_by
              ? 'border-2 border-action-primary-500 font-medium'
              : 'border border-gray-200 dark:border-gray-600 hover:border-gray-300 dark:hover:border-gray-500'"
          >
            <option value="">All triggers</option>
            <option value="schedule">schedule</option>
            <option value="manual">manual</option>
            <option value="chat">chat</option>
            <option value="session">session</option>
            <option value="agent">agent</option>
            <option value="mcp">mcp</option>
            <option value="public">public</option>
            <option value="webhook">webhook</option>
            <option value="fan_out">fan_out</option>
            <option value="loop">loop</option>
          </select>
          <svg class="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M19 9l-7 7-7-7"/></svg>
        </div>

        <!-- Time range filter -->
        <div class="relative">
          <select
            :value="store.filters.hours"
            @change="store.setFilter('hours', Number($event.target.value))"
            class="appearance-none text-sm rounded-lg pl-3 pr-8 py-1.5 bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-200 focus:outline-none focus:ring-2 focus:ring-action-primary-500 cursor-pointer transition-colors"
            :class="store.filters.hours !== 24
              ? 'border-2 border-action-primary-500 font-medium'
              : 'border border-gray-200 dark:border-gray-600 hover:border-gray-300 dark:hover:border-gray-500'"
          >
            <option :value="1">Last 1h</option>
            <option :value="6">Last 6h</option>
            <option :value="24">Last 24h</option>
            <option :value="168">Last 7d</option>
            <option :value="720">Last 30d</option>
            <option :value="0">All time</option>
          </select>
          <svg class="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M19 9l-7 7-7-7"/></svg>
        </div>

        <!-- Search -->
        <input
          type="text"
          :value="store.filters.search"
          @input="onSearchInput"
          placeholder="Search tasks…"
          class="text-sm border border-gray-300 dark:border-gray-600 rounded-md px-2 py-1.5 bg-white dark:bg-gray-700 text-gray-700 dark:text-gray-200 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-action-primary-500 w-44"
        />
      </div>

      <!-- Result count + clear -->
      <div class="flex items-center px-4 py-2 text-xs text-gray-500 dark:text-gray-400 gap-3">
        <span>{{ store.rows.length }} shown</span>
        <button
          v-if="store.hasActiveFilters"
          @click="store.clearFilters()"
          class="text-action-primary-600 dark:text-action-primary-400 hover:underline"
        >
          Clear filters
        </button>
      </div>
    </div>

    <!-- Running now strip -->
    <div
      v-if="store.runningCount > 0"
      class="flex items-center gap-2 px-4 py-2.5 mb-2 bg-status-warning-50 dark:bg-status-warning-900/10 border border-status-warning-200 dark:border-status-warning-800 rounded-lg text-sm text-status-warning-800 dark:text-status-warning-300"
    >
      <span class="w-2 h-2 rounded-full bg-status-warning-500 animate-pulse flex-shrink-0"></span>
      {{ store.runningCount }} running now
    </div>

    <!-- Execution list -->
    <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
      <!-- Loading state -->
      <div v-if="store.loading && store.rows.length === 0" class="text-center py-12">
        <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-action-primary-500 mx-auto"></div>
        <p class="text-sm text-gray-500 dark:text-gray-400 mt-2">Loading…</p>
      </div>

      <!-- Error state -->
      <div v-else-if="store.error && store.rows.length === 0" class="px-4 py-3 text-sm text-status-danger-700 dark:text-status-danger-300 bg-status-danger-50 dark:bg-status-danger-900/20">
        {{ store.error }}
      </div>

      <!-- Empty — no executions ever -->
      <div v-else-if="store.rows.length === 0 && !store.hasActiveFilters" class="text-center py-12">
        <svg class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
        </svg>
        <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">No executions yet</p>
        <p class="text-xs text-gray-400 dark:text-gray-500">Run a task or wait for a schedule to fire.</p>
      </div>

      <!-- Empty — filters returned nothing -->
      <div v-else-if="store.rows.length === 0 && store.hasActiveFilters" class="text-center py-12">
        <svg class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M3 4a1 1 0 011-1h16a1 1 0 011 1v2a1 1 0 01-.293.707L13 13.414V19a1 1 0 01-.553.894l-4 2A1 1 0 017 21v-7.586L3.293 6.707A1 1 0 013 6V4z" />
        </svg>
        <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">No matching executions</p>
        <button @click="store.clearFilters()" class="mt-1 text-xs text-action-primary-600 dark:text-action-primary-400 hover:underline">Clear filters</button>
      </div>

      <!-- Rows -->
      <div v-else class="divide-y divide-gray-200 dark:divide-gray-700">
        <div
          v-for="row in store.rows"
          :key="row.id"
          class="p-4 cursor-pointer group transition-colors hover:bg-gray-50 dark:hover:bg-gray-700/40 hover:shadow-[inset_3px_0_0_0] hover:shadow-action-primary-400"
          :class="rowTintClass(row.status)"
          @click="goToDetail(row)"
        >
          <div class="flex items-start gap-3">
            <!-- Status badge -->
            <span class="mt-0.5 flex-shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium" :class="statusBadgeClass(row.status)">
              <span class="w-1.5 h-1.5 mr-1.5 rounded-full" :class="[statusDotClass(row.status), row.status === 'running' ? 'animate-pulse' : '']"></span>
              {{ row.status }}
            </span>

            <!-- Main content -->
            <div class="flex-1 min-w-0">
              <!-- Top line: agent + trigger + time -->
              <div class="flex flex-wrap items-center gap-2 mb-0.5">
                <router-link
                  :to="'/agents/' + row.agent_name"
                  @click.stop
                  class="text-sm font-medium text-gray-900 dark:text-white hover:text-action-primary-600 dark:hover:text-action-primary-400"
                >
                  {{ row.agent_name }}
                </router-link>
                <span class="px-1.5 py-0.5 rounded text-xs" :class="triggerLabelClass(row.triggered_by)">
                  {{ row.triggered_by }}
                </span>
                <span class="text-xs text-gray-400 dark:text-gray-500">{{ timeAgo(row.started_at) }}</span>
                <span class="font-mono text-xs text-gray-400 dark:text-gray-500 hidden sm:inline">{{ truncId(row.id) }}</span>
              </div>

              <!-- Message -->
              <p class="text-sm text-gray-700 dark:text-gray-300 truncate">{{ row.message }}</p>

              <!-- Meta row -->
              <div class="mt-1 flex flex-wrap items-center gap-3 text-xs text-gray-400 dark:text-gray-500">
                <span v-if="row.duration_ms">{{ formatDuration(row.duration_ms) }}</span>
                <span v-if="row.cost != null">{{ formatCost(row.cost) }}</span>
                <span v-if="row.context_used">{{ formatTokens(row.context_used) }}</span>
                <span v-if="row.error_summary" class="text-status-danger-600 dark:text-status-danger-400 truncate max-w-xs">{{ row.error_summary }}</span>
              </div>
            </div>

            <!-- Right side: stop button (running only) + detail arrow -->
            <div class="flex-shrink-0 flex items-center gap-2 ml-2">
              <button
                v-if="row.status === 'running'"
                @click.stop="stopExecution(row)"
                class="px-2 py-1 text-xs font-medium text-status-danger-600 dark:text-status-danger-400 border border-status-danger-300 dark:border-status-danger-700 rounded-md hover:bg-status-danger-50 dark:hover:bg-status-danger-900/20 transition-colors"
              >
                Stop
              </button>
              <svg class="w-4 h-4 text-gray-300 dark:text-gray-600 hidden sm:block transition-transform group-hover:translate-x-0.5 group-hover:text-action-primary-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
              </svg>
            </div>
          </div>
        </div>
      </div>

      <!-- Load more -->
      <div v-if="store.hasMore" class="px-4 py-3 border-t border-gray-200 dark:border-gray-700">
        <button
          @click="store.loadMore()"
          :disabled="store.loading"
          class="w-full text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 transition-colors disabled:opacity-50"
        >
          {{ store.loading ? 'Loading…' : 'Load more' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { formatCost, formatCostCompact } from '../composables/useFormatters'
import axios from 'axios'
import { useExecutionsStore } from '../stores/executions'
import { useAuthStore } from '../stores/auth'
import { useAgentsStore } from '../stores/agents'
import { useWebSocket } from '../utils/websocket'

const router = useRouter()
const store = useExecutionsStore()
const authStore = useAuthStore()
const agentsStore = useAgentsStore()
const { isConnected } = useWebSocket()

const agentNames = computed(() =>
  agentsStore.agents.map(a => a.name).sort()
)

const wsTooltip = computed(() =>
  isConnected.value ? 'Live updates connected' : 'Polling every 30s'
)

// Threshold ladder for success rate (DS §3.4, inverted: good rate = low danger)
const successRateClass = computed(() => {
  const r = store.stats?.success_rate
  if (r == null) return 'text-gray-900 dark:text-white'
  if (r >= 90) return 'text-status-success-600 dark:text-status-success-400'
  if (r >= 75) return 'text-status-warning-600 dark:text-status-warning-400'
  if (r >= 50) return 'text-status-urgent-600 dark:text-status-urgent-400'
  return 'text-status-danger-600 dark:text-status-danger-400'
})

// --- display helpers ---
function statusBadgeClass(status) {
  const map = {
    success:   'bg-status-success-100 dark:bg-status-success-900/30 text-status-success-800 dark:text-status-success-300',
    failed:    'bg-status-danger-100 dark:bg-status-danger-900/30 text-status-danger-800 dark:text-status-danger-300',
    error:     'bg-status-danger-100 dark:bg-status-danger-900/30 text-status-danger-800 dark:text-status-danger-300',
    running:   'bg-status-warning-100 dark:bg-status-warning-900/30 text-status-warning-800 dark:text-status-warning-300',
    queued:    'bg-status-warning-100 dark:bg-status-warning-900/30 text-status-warning-800 dark:text-status-warning-300',
    cancelled: 'bg-status-urgent-100 dark:bg-status-urgent-900/30 text-status-urgent-800 dark:text-status-urgent-300',
    skipped:   'bg-accent-purple-100 dark:bg-accent-purple-900/30 text-accent-purple-800 dark:text-accent-purple-300',
  }
  return map[status] || 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300'
}

function statusDotClass(status) {
  const map = {
    success:   'bg-status-success-500',
    failed:    'bg-status-danger-500',
    error:     'bg-status-danger-500',
    running:   'bg-status-warning-500',
    queued:    'bg-status-warning-500',
    cancelled: 'bg-status-urgent-500',
    skipped:   'bg-accent-purple-500',
  }
  return map[status] || 'bg-gray-400'
}

function rowTintClass(status) {
  if (status === 'running') return 'bg-status-warning-50/50 dark:bg-status-warning-900/10'
  if (status === 'failed' || status === 'error') return 'bg-status-danger-50/30 dark:bg-status-danger-900/10'
  if (status === 'cancelled') return 'bg-status-urgent-50/30 dark:bg-status-urgent-900/10'
  return ''
}

function triggerLabelClass(trigger) {
  const map = {
    schedule: 'bg-accent-purple-100 dark:bg-accent-purple-900/30 text-accent-purple-700 dark:text-accent-purple-300',
    manual:   'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300',
    chat:     'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300',
    session:  'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300',
    agent:    'bg-state-autonomous-100 dark:bg-state-autonomous-900/30 text-state-autonomous-700 dark:text-state-autonomous-300',
    mcp:      'bg-state-autonomous-100 dark:bg-state-autonomous-900/30 text-state-autonomous-700 dark:text-state-autonomous-300',
    public:   'bg-teal-100 dark:bg-teal-900/30 text-teal-700 dark:text-teal-300',
    webhook:  'bg-sky-100 dark:bg-sky-900/30 text-sky-700 dark:text-sky-300',
    fan_out:  'bg-sky-100 dark:bg-sky-900/30 text-sky-700 dark:text-sky-300',
    loop:     'bg-fuchsia-100 dark:bg-fuchsia-900/30 text-fuchsia-700 dark:text-fuchsia-300',
  }
  return map[trigger] || 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300'
}

function timeAgo(iso) {
  if (!iso) return ''
  const diff = Math.floor((Date.now() - new Date(iso)) / 1000)
  if (diff < 60) return 'just now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

function truncId(id) {
  if (!id || id.length < 12) return id
  return id.slice(0, 6) + '…' + id.slice(-4)
}

function formatDuration(ms) {
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60000)}m ${Math.floor((ms % 60000) / 1000)}s`
}

function formatTokens(n) {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K tokens`
  return `${n} tokens`
}

// --- navigation ---
function goToDetail(row) {
  router.push(`/agents/${row.agent_name}/executions/${row.id}`)
}

// --- stop execution (navigates to agent detail where stop is handled) ---
async function stopExecution(row) {
  try {
    await axios.post(
      `/api/agents/${row.agent_name}/schedules/stop-execution/${row.id}`,
      {},
      { headers: authStore.authHeader }
    )
    store.refresh()
  } catch {
    // stop endpoint may not exist; fall back to opening detail page
    goToDetail(row)
  }
}

// --- search debounce ---
let _searchTimer = null
function onSearchInput(e) {
  clearTimeout(_searchTimer)
  _searchTimer = setTimeout(() => store.setFilter('search', e.target.value), 300)
}

onMounted(() => {
  if (agentsStore.agents.length === 0) agentsStore.fetchAgents()
  store.startPolling(30000)
})

onUnmounted(() => {
  store.stopPolling()
})
</script>
