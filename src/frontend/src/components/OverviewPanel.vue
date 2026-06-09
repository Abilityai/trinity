<template>
  <div class="space-y-5" data-testid="overview-panel">
    <!-- ============================================================ -->
    <!-- 1. ABOUT (lead) — header has no description at all (#1107)   -->
    <!-- ============================================================ -->
    <div class="bg-gradient-to-r from-action-primary-50 to-accent-purple-50 dark:from-action-primary-900/30 dark:to-accent-purple-900/30 rounded-lg p-5 border border-action-primary-100 dark:border-action-primary-800">
      <div v-if="loading.info" class="animate-pulse space-y-2">
        <div class="h-4 bg-gray-200 dark:bg-gray-700 rounded w-1/3"></div>
        <div class="h-3 bg-gray-200 dark:bg-gray-700 rounded w-2/3"></div>
      </div>
      <template v-else>
        <div class="flex items-start justify-between gap-4">
          <div class="min-w-0">
            <p v-if="info?.tagline" class="text-sm text-action-primary-600 dark:text-action-primary-400 font-medium">
              {{ info.tagline }}
            </p>
            <p
              v-if="info?.description"
              class="mt-1 text-sm text-gray-600 dark:text-gray-300 whitespace-pre-line line-clamp-4"
            >{{ info.description }}</p>
            <p v-else class="mt-1 text-sm text-gray-400 dark:text-gray-500 italic">
              No description provided in this agent's template.
            </p>
          </div>
          <button
            class="flex-shrink-0 text-xs font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline whitespace-nowrap"
            @click="$emit('navigate', { tab: 'info' })"
          >Full details →</button>
        </div>

        <!-- Task-entry shim: preserves the action surface that the default-swap
             would otherwise demote (#1107 coherence fix). Deep-links into Tasks. -->
        <form class="mt-4 flex items-center gap-2" @submit.prevent="submitQuickTask">
          <input
            v-model="quickTask"
            type="text"
            :placeholder="`Give ${agentName} a task…`"
            class="flex-1 min-w-0 text-sm px-3 py-2 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-800 dark:text-gray-200 focus:outline-none focus:ring-1 focus:ring-action-primary-500"
          />
          <button
            type="submit"
            :disabled="!quickTask.trim()"
            class="flex-shrink-0 inline-flex items-center gap-1 text-sm font-medium py-2 px-3 rounded-md bg-action-primary-600 hover:bg-action-primary-700 disabled:bg-gray-300 dark:disabled:bg-gray-700 text-white transition-colors"
            title="Open the Tasks tab with this message prefilled"
          >
            Run →
          </button>
        </form>
      </template>
    </div>

    <!-- ============================================================ -->
    <!-- 2. NEEDS ATTENTION — only shown when non-empty (#1107)       -->
    <!-- ============================================================ -->
    <div
      v-if="hasAttention"
      class="bg-status-warning-50 dark:bg-status-warning-900/20 rounded-lg p-4 border border-status-warning-300 dark:border-status-warning-700"
    >
      <h3 class="text-xs font-semibold text-status-warning-800 dark:text-status-warning-300 uppercase tracking-wider mb-3 flex items-center">
        <svg class="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
        </svg>
        Needs attention
      </h3>
      <ul class="space-y-2 text-sm">
        <!-- Circuit breaker open: point to the header badge, do NOT re-render it (#1107) -->
        <li v-if="circuitOpen" class="flex items-center gap-2 text-status-danger-700 dark:text-status-danger-300">
          <span class="w-1.5 h-1.5 rounded-full bg-status-danger-500 flex-shrink-0"></span>
          <span>Dispatch circuit breaker is <strong>open</strong> — see the <span class="font-medium">⚡ circuit open</span> badge in the header above; new tasks fast-fail until it recovers.</span>
        </li>
        <!-- Sync failing -->
        <li v-if="syncFailing" class="flex items-center gap-2 text-status-warning-800 dark:text-status-warning-300">
          <span class="w-1.5 h-1.5 rounded-full bg-status-warning-500 flex-shrink-0"></span>
          <span>Git sync failing — {{ syncState.consecutive_failures }} consecutive failure(s).
            <button class="underline hover:no-underline" @click="$emit('navigate', { tab: 'git' })">View Git</button>
          </span>
        </li>
        <!-- Operator-queue items -->
        <li
          v-for="item in operatorItems.slice(0, 5)"
          :key="item.id"
          class="flex items-start gap-2 text-gray-700 dark:text-gray-300"
        >
          <span class="w-1.5 h-1.5 mt-1.5 rounded-full bg-action-primary-500 flex-shrink-0"></span>
          <span>
            <span class="px-1.5 py-0.5 text-[10px] font-semibold rounded uppercase mr-1"
              :class="opPriorityClass(item.priority)">{{ item.type }}</span>
            {{ item.title || item.question }}
          </span>
        </li>
        <!-- Pending notifications -->
        <li
          v-for="note in pendingNotifications.slice(0, 5)"
          :key="note.id"
          class="flex items-start gap-2 text-gray-700 dark:text-gray-300"
        >
          <span class="w-1.5 h-1.5 mt-1.5 rounded-full bg-accent-purple-500 flex-shrink-0"></span>
          <span>{{ note.title || note.message }}</span>
        </li>
      </ul>
    </div>

    <!-- ============================================================ -->
    <!-- 3. PERFORMANCE — net-new breakdowns the header lacks (#1107) -->
    <!-- ============================================================ -->
    <div>
      <h3 class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">Performance</h3>
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <!-- Tasks (24h / 7d) -->
        <div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <p class="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide">Tasks · 24h</p>
          <div v-if="loading.stats24" class="animate-pulse h-7 mt-1 bg-gray-200 dark:bg-gray-700 rounded w-16"></div>
          <template v-else>
            <p class="text-2xl font-bold text-gray-900 dark:text-white mt-0.5">{{ stats24?.total ?? 0 }}</p>
            <div class="mt-1 flex items-center gap-2 text-[11px]">
              <span :class="successRateClass(stats24?.success_rate)">{{ fmtPct(stats24?.success_rate) }} ok</span>
              <span v-if="(stats24?.failed_count ?? 0) > 0" class="text-status-danger-600 dark:text-status-danger-400">{{ stats24.failed_count }} failed</span>
            </div>
            <p class="text-[11px] text-gray-400 dark:text-gray-500 mt-0.5">7d: {{ stats7?.total ?? 0 }} · {{ fmtPct(stats7?.success_rate) }} ok</p>
          </template>
        </div>

        <!-- Running + queued (live) -->
        <div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <p class="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide">Running · Queued</p>
          <div v-if="loading.stats24" class="animate-pulse h-7 mt-1 bg-gray-200 dark:bg-gray-700 rounded w-16"></div>
          <template v-else>
            <p class="text-2xl font-bold mt-0.5" :class="(stats24?.running_count ?? 0) > 0 ? 'text-status-warning-600 dark:text-status-warning-400' : 'text-gray-900 dark:text-white'">
              {{ stats24?.running_count ?? 0 }}<span class="text-gray-400 dark:text-gray-500 text-lg font-normal"> · {{ stats24?.queued_count ?? 0 }}</span>
            </p>
            <p class="text-[11px] text-gray-400 dark:text-gray-500 mt-1">live now</p>
          </template>
        </div>

        <!-- Context window utilization (container-gated) -->
        <div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <p class="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide">Context window</p>
          <p v-if="!isRunning" class="text-sm text-gray-400 dark:text-gray-500 mt-2">Offline</p>
          <div v-else-if="loading.context" class="animate-pulse h-7 mt-1 bg-gray-200 dark:bg-gray-700 rounded w-16"></div>
          <template v-else>
            <p class="text-2xl font-bold text-gray-900 dark:text-white mt-0.5">{{ Math.round(context?.contextPercent ?? 0) }}%</p>
            <div class="mt-1.5 h-1.5 w-full bg-gray-100 dark:bg-gray-700 rounded-full overflow-hidden">
              <div class="h-full rounded-full" :class="contextBarClass" :style="{ width: Math.min(100, context?.contextPercent ?? 0) + '%' }"></div>
            </div>
            <p class="text-[11px] text-gray-400 dark:text-gray-500 mt-1">{{ fmtTokens(context?.contextUsed) }} / {{ fmtTokens(context?.contextMax) }}</p>
          </template>
        </div>

        <!-- Schedules + capacity (combined card) -->
        <div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
          <button class="text-left w-full" @click="$emit('navigate', { tab: 'schedules' })" title="Open Schedules">
            <p class="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide">Schedules</p>
            <div v-if="loading.schedules" class="animate-pulse h-7 mt-1 bg-gray-200 dark:bg-gray-700 rounded w-16"></div>
            <template v-else>
              <p class="text-2xl font-bold text-gray-900 dark:text-white mt-0.5">
                {{ schedulesEnabled }}<span class="text-gray-400 dark:text-gray-500 text-lg font-normal">/{{ schedules.length }}</span>
              </p>
              <p class="text-[11px] text-gray-400 dark:text-gray-500 mt-1 truncate">
                <template v-if="nextRunAt">next {{ formatRelativeTime(nextRunAt) }}</template>
                <template v-else>none enabled</template>
              </p>
            </template>
          </button>
          <div class="mt-2 pt-2 border-t border-gray-100 dark:border-gray-700 flex items-center justify-between text-[11px]">
            <span class="text-gray-500 dark:text-gray-400">Slots</span>
            <span v-if="!isRunning" class="text-gray-400 dark:text-gray-500">offline</span>
            <span v-else-if="loading.capacity" class="text-gray-400 dark:text-gray-500">…</span>
            <span v-else class="font-mono text-gray-700 dark:text-gray-300">{{ capacity?.active_slots ?? 0 }} / {{ capacity?.max_parallel_tasks ?? '—' }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- ============================================================ -->
    <!-- 4. HEALTH & RELIABILITY — aggregate rollup (#1107)           -->
    <!-- Header shows instantaneous CPU/MEM/uptime-since-start only.  -->
    <!-- ============================================================ -->
    <div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
      <div class="flex items-center justify-between mb-3">
        <h3 class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider">Health &amp; reliability</h3>
        <span v-if="health?.aggregate_status" class="px-2 py-0.5 text-xs font-medium rounded-full" :class="healthStatusClass(health.aggregate_status)">
          {{ health.aggregate_status }}
        </span>
      </div>
      <p v-if="!isRunning" class="text-sm text-gray-400 dark:text-gray-500">Agent is stopped — start it to see live health.</p>
      <div v-else-if="loading.health" class="animate-pulse grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div v-for="n in 4" :key="n" class="h-12 bg-gray-100 dark:bg-gray-700 rounded"></div>
      </div>
      <div v-else class="grid grid-cols-2 sm:grid-cols-4 gap-y-3 gap-x-4 text-sm">
        <!-- Liveness (heartbeat / business status). Distinct from the header's
             instantaneous status badge — this is the monitored health-probe state. -->
        <div>
          <p class="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide">Liveness</p>
          <p class="font-medium" :class="health?.business?.status === 'healthy' ? 'text-status-success-600 dark:text-status-success-400' : 'text-gray-700 dark:text-gray-300'">
            {{ health?.business?.status || 'unknown' }}
          </p>
        </div>
        <!-- 24h uptime — explicitly labeled "24h" so it does NOT collide with the
             header's "uptime since start" (#1107 coherence fix). -->
        <div>
          <p class="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide">24h uptime</p>
          <p class="font-medium text-gray-700 dark:text-gray-300">{{ health?.uptime_percent_24h != null ? health.uptime_percent_24h + '%' : '—' }}</p>
        </div>
        <!-- Avg latency 24h -->
        <div>
          <p class="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide">Avg latency</p>
          <p class="font-medium text-gray-700 dark:text-gray-300">{{ health?.avg_latency_24h_ms != null ? Math.round(health.avg_latency_24h_ms) + 'ms' : '—' }}</p>
        </div>
        <!-- Restarts + OOM -->
        <div>
          <p class="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide">Restarts</p>
          <p class="font-medium" :class="(health?.docker?.restart_count ?? 0) > 0 ? 'text-status-warning-600 dark:text-status-warning-400' : 'text-gray-700 dark:text-gray-300'">
            {{ health?.docker?.restart_count ?? 0 }}
            <span v-if="health?.docker?.oom_killed" class="ml-1 px-1 py-0.5 text-[9px] font-semibold rounded bg-status-danger-100 dark:bg-status-danger-900/50 text-status-danger-700 dark:text-status-danger-300">OOM</span>
          </p>
        </div>
      </div>
    </div>

    <!-- ============================================================ -->
    <!-- 5. RECENT ACTIVITY — net-new (#1107)                         -->
    <!-- ============================================================ -->
    <div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
      <div class="flex items-center justify-between mb-3">
        <h3 class="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider">Recent activity</h3>
        <span class="flex items-center gap-1.5 text-xs">
          <span class="w-2 h-2 rounded-full" :class="activityDotClass"></span>
          <span class="text-gray-500 dark:text-gray-400">{{ activityLabel }}</span>
        </span>
      </div>
      <div v-if="loading.recent" class="animate-pulse space-y-2">
        <div v-for="n in 3" :key="n" class="h-6 bg-gray-100 dark:bg-gray-700 rounded"></div>
      </div>
      <p v-else-if="recentExecutions.length === 0" class="text-sm text-gray-400 dark:text-gray-500">No executions yet.</p>
      <ul v-else class="divide-y divide-gray-100 dark:divide-gray-700">
        <li
          v-for="exec in recentExecutions"
          :key="exec.id"
          class="flex items-center gap-3 py-2 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700/50 -mx-2 px-2 rounded transition-colors"
          @click="$emit('navigate', { tab: 'tasks', executionId: exec.id })"
        >
          <span class="w-2 h-2 rounded-full flex-shrink-0" :class="execDotClass(exec.status)"></span>
          <span class="flex-1 min-w-0 text-sm text-gray-700 dark:text-gray-300 truncate">{{ exec.message || exec.triggered_by || exec.status }}</span>
          <span class="flex-shrink-0 text-[11px] text-gray-400 dark:text-gray-500">{{ formatRelativeTime(exec.started_at) }}</span>
        </li>
      </ul>
      <button
        v-if="recentExecutions.length > 0"
        class="mt-2 text-xs font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline"
        @click="$emit('navigate', { tab: 'tasks' })"
      >View all tasks →</button>
    </div>

    <!-- ============================================================ -->
    <!-- 6. FOOTPRINT — compact, net-new (#1107)                      -->
    <!-- ============================================================ -->
    <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
      <!-- Sharing (owner-gated: /shares + /access-policy are owner-scoped) -->
      <div v-if="agent?.can_share" class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
        <h4 class="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1.5">Sharing</h4>
        <div v-if="loading.sharing" class="animate-pulse h-5 bg-gray-100 dark:bg-gray-700 rounded w-20"></div>
        <template v-else>
          <p class="text-sm text-gray-700 dark:text-gray-300">
            {{ shares.length }} {{ shares.length === 1 ? 'person' : 'people' }}
          </p>
          <p class="text-[11px] text-gray-400 dark:text-gray-500 mt-0.5 truncate">
            <span v-if="accessPolicy?.open_access">Open access</span>
            <span v-else-if="accessPolicy?.require_email">Email required</span>
            <span v-else>Invite only</span>
          </p>
        </template>
      </div>

      <!-- Sync health (controls live in header; this is the health readout) -->
      <div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
        <h4 class="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1.5">Sync health</h4>
        <div v-if="loading.sync" class="animate-pulse h-5 bg-gray-100 dark:bg-gray-700 rounded w-20"></div>
        <template v-else>
          <p class="text-sm font-medium capitalize" :class="syncStatusClass">{{ syncState?.last_sync_status || 'never' }}</p>
          <p class="text-[11px] text-gray-400 dark:text-gray-500 mt-0.5">
            <template v-if="syncState?.last_sync_at">{{ formatRelativeTime(syncState.last_sync_at) }}</template>
            <template v-else-if="(syncState?.consecutive_failures ?? 0) > 0">{{ syncState.consecutive_failures }} failure(s)</template>
            <template v-else>not configured</template>
          </p>
        </template>
      </div>

      <!-- Skills & playbooks -->
      <div class="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
        <h4 class="text-[10px] text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1.5">Skills &amp; playbooks</h4>
        <div v-if="loading.skills" class="animate-pulse h-5 bg-gray-100 dark:bg-gray-700 rounded w-20"></div>
        <template v-else>
          <p class="text-sm text-gray-700 dark:text-gray-300">
            {{ skills.length }} skill{{ skills.length === 1 ? '' : 's' }}<span v-if="isRunning"> · {{ playbooks.length }} playbook{{ playbooks.length === 1 ? '' : 's' }}</span>
          </p>
          <p class="text-[11px] text-gray-400 dark:text-gray-500 mt-0.5 truncate">{{ topSkillNames || '—' }}</p>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, reactive, onMounted, watch } from 'vue'
import axios from 'axios'
import { useAgentsStore } from '../stores/agents'
import { useMonitoringStore } from '../stores/monitoring'
import { useAuthStore } from '../stores/auth'
import { useFormatters } from '../composables'

const props = defineProps({
  agentName: { type: String, required: true },
  // Full agent object passed from parent so we don't re-fetch status/can_share/
  // circuit_breaker_state (eng review: pass props down to halve mount fan-out).
  agent: { type: Object, default: null }
})

const emit = defineEmits(['navigate', 'item-click'])

const agentsStore = useAgentsStore()
const monitoringStore = useMonitoringStore()
const authStore = useAuthStore()
const { formatRelativeTime } = useFormatters()

// --- Per-card state (local refs — NEVER mutate the shared executions/
// monitoring/notifications/operatorQueue store singletons, which back the
// NavBar badges; see eng review finding #1). -------------------------------
const info = ref(null)
const stats24 = ref(null)
const stats7 = ref(null)
const context = ref(null)
const schedules = ref([])
const capacity = ref(null)
const health = ref(null)
const recentExecutions = ref([])
const operatorItems = ref([])
const pendingNotifications = ref([])
const syncState = ref(null)
const shares = ref([])
const accessPolicy = ref(null)
const skills = ref([])
const playbooks = ref([])

const quickTask = ref('')

const loading = reactive({
  info: true, stats24: true, context: true, schedules: true, capacity: true,
  health: true, recent: true, sync: true, sharing: true, skills: true
})

const isRunning = computed(() => props.agent?.status === 'running')
const circuitOpen = computed(() => props.agent?.circuit_breaker_state === 'open')
const syncFailing = computed(() => (syncState.value?.consecutive_failures ?? 0) >= 3)

const hasAttention = computed(() =>
  circuitOpen.value ||
  syncFailing.value ||
  operatorItems.value.length > 0 ||
  pendingNotifications.value.length > 0
)

const schedulesEnabled = computed(() => schedules.value.filter(s => s.enabled).length)
const nextRunAt = computed(() => {
  const times = schedules.value
    .filter(s => s.enabled && s.next_run_at)
    .map(s => s.next_run_at)
    .sort()
  return times[0] || null
})

const topSkillNames = computed(() =>
  skills.value.slice(0, 3).map(s => s.name || s.skill_name || s).filter(Boolean).join(', ')
)

// --- Activity state (from context-stats activityState) ---------------------
const activityLabel = computed(() => {
  if (!isRunning.value) return 'offline'
  return context.value?.activityState || 'idle'
})
const activityDotClass = computed(() => {
  const s = activityLabel.value
  if (s === 'active') return 'bg-status-success-500 animate-pulse'
  if (s === 'idle') return 'bg-gray-400'
  return 'bg-gray-300 dark:bg-gray-600'
})

// --- Style helpers ---------------------------------------------------------
const contextBarClass = computed(() => {
  const p = context.value?.contextPercent ?? 0
  if (p > 85) return 'bg-status-danger-500'
  if (p > 60) return 'bg-status-warning-500'
  return 'bg-status-success-500'
})
const syncStatusClass = computed(() => {
  const s = syncState.value?.last_sync_status
  if (s === 'success') return 'text-status-success-600 dark:text-status-success-400'
  if (s === 'failed') return 'text-status-danger-600 dark:text-status-danger-400'
  return 'text-gray-500 dark:text-gray-400'
})

function successRateClass(rate) {
  const r = rate ?? 0
  if (r >= 90) return 'text-status-success-600 dark:text-status-success-400'
  if (r >= 70) return 'text-status-warning-600 dark:text-status-warning-400'
  return 'text-status-danger-600 dark:text-status-danger-400'
}
function healthStatusClass(status) {
  if (status === 'healthy') return 'bg-status-success-100 dark:bg-status-success-900/50 text-status-success-700 dark:text-status-success-300'
  if (status === 'degraded') return 'bg-status-warning-100 dark:bg-status-warning-900/50 text-status-warning-700 dark:text-status-warning-300'
  if (status === 'unhealthy') return 'bg-status-danger-100 dark:bg-status-danger-900/50 text-status-danger-700 dark:text-status-danger-300'
  return 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300'
}
function execDotClass(status) {
  if (status === 'success') return 'bg-status-success-500'
  if (status === 'failed' || status === 'error') return 'bg-status-danger-500'
  if (status === 'running') return 'bg-status-warning-500 animate-pulse'
  if (status === 'queued') return 'bg-action-primary-400'
  return 'bg-gray-300 dark:bg-gray-600'
}
function opPriorityClass(priority) {
  if (priority === 'critical' || priority === 'high') return 'bg-status-danger-100 dark:bg-status-danger-900/50 text-status-danger-700 dark:text-status-danger-300'
  return 'bg-action-primary-100 dark:bg-action-primary-900/50 text-action-primary-700 dark:text-action-primary-300'
}

function fmtPct(rate) {
  return `${Math.round(rate ?? 0)}%`
}
function fmtTokens(n) {
  if (!n) return '0'
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k`
  return String(n)
}

function submitQuickTask() {
  const text = quickTask.value.trim()
  if (!text) return
  // Reuse InfoPanel's item-click contract → parent prefills Tasks + switches tab.
  emit('item-click', { type: 'overview-task', text })
  quickTask.value = ''
}

// --- Fetchers (local axios; stateless store methods where safe) ------------
const hdr = () => ({ headers: authStore.authHeader })

async function loadInfo() {
  loading.info = true
  try { info.value = await agentsStore.getAgentInfo(props.agentName) }
  catch { info.value = null }
  finally { loading.info = false }
}

async function loadTaskStats() {
  loading.stats24 = true
  try {
    const [r24, r7] = await Promise.allSettled([
      axios.get(`/api/executions/stats?agent=${encodeURIComponent(props.agentName)}&hours=24`, hdr()),
      axios.get(`/api/executions/stats?agent=${encodeURIComponent(props.agentName)}&hours=168`, hdr())
    ])
    if (r24.status === 'fulfilled') stats24.value = r24.value.data
    if (r7.status === 'fulfilled') stats7.value = r7.value.data
  } finally { loading.stats24 = false }
}

async function loadRecentExecutions() {
  loading.recent = true
  try {
    const res = await axios.get(`/api/executions?agent=${encodeURIComponent(props.agentName)}&limit=5&hours=168`, hdr())
    recentExecutions.value = Array.isArray(res.data) ? res.data : []
  } catch { recentExecutions.value = [] }
  finally { loading.recent = false }
}

async function loadSchedules() {
  loading.schedules = true
  try {
    const res = await axios.get(`/api/agents/${props.agentName}/schedules`, hdr())
    schedules.value = Array.isArray(res.data) ? res.data : (res.data?.schedules || [])
  } catch { schedules.value = [] }
  finally { loading.schedules = false }
}

async function loadContext() {
  if (!isRunning.value) { loading.context = false; return }
  loading.context = true
  try {
    const res = await axios.get('/api/agents/context-stats', hdr())
    const list = res.data?.agents || []
    context.value = list.find(a => a.name === props.agentName) || null
  } catch { context.value = null }
  finally { loading.context = false }
}

async function loadCapacity() {
  if (!isRunning.value) { loading.capacity = false; return }
  loading.capacity = true
  try {
    const res = await axios.get(`/api/agents/${props.agentName}/capacity`, hdr())
    capacity.value = res.data
  } catch { capacity.value = null }
  finally { loading.capacity = false }
}

async function loadHealth() {
  if (!isRunning.value) { loading.health = false; return }
  loading.health = true
  try {
    // Stateless cache-only store method — safe to reuse (eng review).
    health.value = await monitoringStore.fetchAgentHealth(props.agentName)
  } catch { health.value = null }
  finally { loading.health = false }
}

async function loadAttention() {
  // operator-queue + pending notifications (local axios — do NOT touch the
  // notifications/operatorQueue store singletons that feed the NavBar badges).
  try {
    const res = await axios.get(`/api/operator-queue/agents/${props.agentName}?status=pending`, hdr())
    operatorItems.value = res.data?.items || []
  } catch { operatorItems.value = [] }
  try {
    const res = await axios.get(`/api/agents/${props.agentName}/notifications?status=pending`, hdr())
    pendingNotifications.value = res.data?.notifications || []
  } catch { pendingNotifications.value = [] }
}

async function loadSync() {
  loading.sync = true
  try {
    const res = await axios.get(`/api/agents/${props.agentName}/git/sync-state`, hdr())
    syncState.value = res.data
  } catch { syncState.value = null }
  finally { loading.sync = false }
}

async function loadSharing() {
  if (!props.agent?.can_share) { loading.sharing = false; return }
  loading.sharing = true
  try {
    const [s, p] = await Promise.allSettled([
      axios.get(`/api/agents/${props.agentName}/shares`, hdr()),
      axios.get(`/api/agents/${props.agentName}/access-policy`, hdr())
    ])
    if (s.status === 'fulfilled') shares.value = s.value.data?.shares || (Array.isArray(s.value.data) ? s.value.data : [])
    if (p.status === 'fulfilled') accessPolicy.value = p.value.data
  } finally { loading.sharing = false }
}

async function loadSkills() {
  loading.skills = true
  try {
    const res = await axios.get(`/api/agents/${props.agentName}/skills`, hdr())
    skills.value = Array.isArray(res.data) ? res.data : (res.data?.skills || [])
  } catch { skills.value = [] }
  finally { loading.skills = false }
  if (isRunning.value) {
    try {
      const res = await axios.get(`/api/agents/${props.agentName}/playbooks`, hdr())
      playbooks.value = Array.isArray(res.data) ? res.data : (res.data?.skills || res.data?.playbooks || [])
    } catch { playbooks.value = [] }
  }
}

// DB-sourced cards always load; container-gated cards load only when running.
function loadAll() {
  loadInfo()
  loadTaskStats()
  loadRecentExecutions()
  loadSchedules()
  loadAttention()
  loadSync()
  loadSharing()
  loadSkills()
  loadContext()
  loadCapacity()
  loadHealth()
}

// Re-fetch container-gated cards when the agent transitions to running
// (mirrors InfoPanel's status watcher).
watch(() => props.agent?.status, (s, prev) => {
  if (s === 'running' && prev !== 'running') {
    loadContext(); loadCapacity(); loadHealth()
    // playbooks live in the container too
    loadSkills()
  }
})

onMounted(loadAll)
</script>
