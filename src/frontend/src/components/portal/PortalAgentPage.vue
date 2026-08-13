<template>
  <div class="flex-1 min-w-0 flex flex-col min-h-0">
    <!-- Header -->
    <header class="shrink-0 border-b border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900">
      <div class="flex items-start gap-3 px-3 sm:px-6 pt-4">
        <button class="sm:hidden -ml-1 p-2 text-gray-500" aria-label="Menu" @click="$emit('open-menu')">
          <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" /></svg>
        </button>

        <PortalAvatar :name="agentName" :avatar-url="page?.header?.avatar_url" :size="52" />

        <div class="min-w-0 flex-1">
          <h1 class="text-lg font-semibold truncate">{{ agentName }}</h1>
          <p v-if="page?.header?.description" class="text-sm text-gray-500 dark:text-gray-400 line-clamp-2">
            {{ page.header.description }}
          </p>
          <div class="mt-1 flex items-center gap-3 text-xs text-gray-500 dark:text-gray-400">
            <span class="inline-flex items-center gap-1.5">
              <span class="w-2 h-2 rounded-full" :class="healthDot"></span>{{ healthLabel }}
            </span>
            <span v-if="page?.header?.last_active">Last active {{ relative(page.header.last_active) }}</span>
          </div>
        </div>

        <button
          class="shrink-0 rounded-xl bg-action-primary-600 hover:bg-action-primary-700 text-white text-sm font-medium px-3.5 py-2 transition"
          @click="$emit('start-chat', agentName)"
        >Start a chat</button>
      </div>

      <!-- Stats strip -->
      <div class="px-3 sm:px-6 pt-4">
        <div class="flex flex-wrap items-end gap-x-8 gap-y-3">
          <div>
            <div class="text-xl font-semibold tabular-nums" :class="{ 'opacity-40': loading && !page }">{{ loading && !page ? '—' : stats.total_executions }}</div>
            <div class="text-xs text-gray-500 dark:text-gray-400">tasks · last {{ windowLabel }}</div>
          </div>
          <div>
            <div class="text-xl font-semibold tabular-nums">{{ pct(stats.success_rate) }}</div>
            <div class="text-xs text-gray-500 dark:text-gray-400">completed</div>
          </div>
          <div>
            <div class="text-xl font-semibold tabular-nums">{{ pct(stats.first_try?.rate) }}</div>
            <div class="text-xs text-gray-500 dark:text-gray-400" title="Succeeded without needing a retry">first try</div>
          </div>

          <!-- The activity chart used to live here as a bespoke full-bleed bar
               strip. It is now a bounded card on the Overview tab using the same
               component the operator surface uses (#2161). -->
          <div class="flex-1"></div>

          <!-- Only shown where it changes something: the window drives the chart
               and the headline numbers, not the asks or the chat list. -->
          <select
            v-if="WINDOWED_TABS.includes(tab)"
            v-model="timeWindow"
            class="text-xs rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-2 py-1"
            aria-label="Time window"
          >
            <option value="7d">7 days</option>
            <option value="14d">14 days</option>
            <option value="30d">30 days</option>
          </select>
        </div>
      </div>

      <!-- Tabs -->
      <nav class="px-3 sm:px-6 mt-3 flex gap-1 overflow-x-auto" role="tablist">
        <button
          v-for="t in TABS"
          :key="t.id"
          role="tab"
          :aria-selected="tab === t.id"
          class="shrink-0 px-3 py-2 text-sm border-b-2 -mb-px transition"
          :class="tab === t.id
            ? 'border-action-primary-600 text-action-primary-600 dark:text-action-primary-400 font-medium'
            : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200'"
          @click="tab = t.id"
        >
          {{ t.label }}
          <span v-if="t.id === 'overview' && asks.length" class="ml-1 px-1.5 rounded-full bg-action-primary-600 text-white text-[10px]">{{ asksBadge }}</span>
        </button>
      </nav>
    </header>

    <div class="flex-1 min-h-0 overflow-y-auto px-3 sm:px-6 py-5">
      <!-- AC4 (#2160): section-shaped skeletons, not one "Loading…" line. The
           payload arrives in one response, so this is honesty about WHERE
           content will appear rather than progressive arrival — the header and
           tabs above are already rendered from the route, so the page never
           looks blank or hung while this fills in. -->
      <div v-if="loading && !page" class="space-y-6" aria-busy="true">
        <div v-for="sec in 2" :key="sec" class="animate-pulse">
          <div class="h-3 w-32 rounded bg-gray-200 dark:bg-gray-800 mb-3"></div>
          <div class="space-y-2">
            <div v-for="row in 3" :key="row" class="h-9 rounded-lg bg-gray-100 dark:bg-gray-800/60"></div>
          </div>
        </div>
        <span class="sr-only">Loading this agent's activity…</span>
      </div>
      <p v-else-if="error" class="text-sm text-status-danger-600 dark:text-status-danger-400">{{ error }}</p>

      <!-- ---------------------------- OVERVIEW ---------------------------- -->
      <template v-else-if="tab === 'overview'">
        <!-- Activity, in the same visual language as the operator Overview
             (#1107): bounded, stacked by what triggered the work, both themes.
             An empty window and an unreadable one are different sentences — a
             blank chart frame for either would be the dead empty state. -->
        <section class="mb-6 max-w-2xl">
          <h2 class="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-2">
            Activity · last {{ windowLabel }}
          </h2>
          <div class="rounded-xl border border-gray-200 dark:border-gray-800 px-3.5 py-3">
            <p v-if="stats.unavailable" class="text-sm text-gray-400">Stats are unavailable right now.</p>
            <p v-else-if="!hasActivity" class="text-sm text-gray-400">No activity in this window.</p>
            <StackedBarChart
              v-else
              :data="stats.timeline || []"
              :buckets="chartBuckets"
              :colors="BUCKET_COLORS"
              :labels="PORTAL_BUCKET_LABELS"
              :height="110"
            />
          </div>
        </section>

        <!-- Asks lead — that is the reason the page exists, it is where the
             agent reaches the user when no chat is open. On a wide screen they
             sit beside recent work rather than pushing it below the fold; they
             stay FIRST in DOM order so the mobile stack keeps the priority. -->
        <div class="mb-6 grid gap-6" :class="{ 'lg:grid-cols-2': asks.length }">
          <section v-if="asks.length">
            <h2 class="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-2">Waiting on you</h2>
            <div
              v-for="a in visibleAsks"
              :key="a.id"
              class="mb-2 rounded-xl border border-amber-300/70 dark:border-amber-700/50 bg-amber-50/60 dark:bg-amber-900/10 px-3 py-2.5"
            >
              <div class="text-sm font-medium">{{ a.title || 'The agent needs a decision' }}</div>
              <p v-if="a.question" class="mt-0.5 text-xs text-gray-600 dark:text-gray-300 line-clamp-3">{{ a.question }}</p>
              <div v-if="a.options?.length" class="mt-1.5 flex flex-wrap gap-1">
                <span v-for="o in a.options" :key="o" class="text-[11px] rounded-full bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 px-1.5 py-0.5">{{ o }}</span>
              </div>
              <!-- Answering writes to the operator queue, an operator surface
                   with its own auth. Rather than render a control that 403s for
                   a client, the answer path is the conversation. -->
              <button class="mt-1.5 text-xs text-action-primary-600 hover:underline" @click="$emit('start-chat', agentName)">
                Reply in chat →
              </button>
            </div>
            <!-- In place, not a nested scroll region: this page has one scroll
                 axis (#2101), and a pane that scrolls inside a page that scrolls
                 traps the gesture on touch. -->
            <button
              v-if="asks.length > ASKS_PREVIEW"
              class="text-xs text-action-primary-600 hover:underline"
              @click="allAsks = !allAsks"
            >
              {{ allAsks ? 'Show fewer' : `Show all ${asksBadge}` }}
            </button>
          </section>

          <section>
            <h2 class="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-2">Recent work</h2>
            <p v-if="!recentWork.length" class="text-sm text-gray-400">Nothing yet.</p>
            <ul v-else class="divide-y divide-gray-100 dark:divide-gray-800">
              <li v-for="w in recentWork.slice(0, 8)" :key="w.id" class="py-2 flex items-center gap-3 text-sm">
                <span class="w-2 h-2 rounded-full shrink-0" :class="statusDot(w.status)"></span>
                <span class="flex-1 min-w-0 truncate text-gray-600 dark:text-gray-300" :title="workLabel(w)">{{ workLabel(w) }}</span>
                <span class="text-xs text-gray-400 tabular-nums">{{ duration(w.duration_ms) }}</span>
                <span class="text-xs text-gray-400 shrink-0">{{ relative(w.started_at) }}</span>
              </li>
            </ul>
          </section>
        </div>

        <section>
          <h2 class="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-2">Your chats with {{ agentName }}</h2>
          <p v-if="!chats.length" class="text-sm text-gray-400">No conversations yet.</p>
          <ul v-else class="divide-y divide-gray-100 dark:divide-gray-800">
            <li v-for="c in chats.slice(0, 8)" :key="c.id">
              <button class="w-full py-2 flex items-center gap-3 text-left text-sm hover:opacity-80" @click="$emit('open-thread', c)">
                <span class="flex-1 truncate">{{ c.title || 'New chat' }}</span>
                <span v-if="c.unread" class="shrink-0 min-w-[1.125rem] px-1 h-[1.125rem] rounded-full bg-action-primary-600 text-white text-[10px] font-semibold flex items-center justify-center">{{ c.unread }}</span>
                <span class="text-xs text-gray-400 shrink-0">{{ relative(c.last_message_at) }}</span>
              </button>
            </li>
          </ul>
        </section>
      </template>

      <!-- ---------------------------- REPORTS ----------------------------- -->
      <!-- #2162: reports render through the SHARED components/reports/ set, the
           same dispatch Agent Detail uses. This dumped JSON.stringify(payload)
           at external clients — a disclosure defect, not only an ugly one: the
           payload is free-form agent JSON of the same class as an ask's
           `context`, which this page refuses to expose at all. A typed renderer
           reads only the keys its hint declares, so this narrows what crosses.
           `:allow-raw="false"` is the one place this surface diverges from the
           operator ones: no raw payload is reachable behind the fallback. -->
      <template v-else-if="tab === 'reports'">
        <div v-if="!reportsLoaded && !reportsError" class="space-y-2" aria-busy="true">
          <div v-for="row in 3" :key="row" class="animate-pulse h-14 rounded-xl bg-gray-100 dark:bg-gray-800/60"></div>
          <span class="sr-only">Loading this agent's reports…</span>
        </div>
        <LoadFailed
          v-else-if="reportsError"
          dense
          title="Couldn't load reports"
          :message="reportsError"
          @retry="loadReports"
        />
        <p v-else-if="!reports.length" class="text-sm text-gray-500 dark:text-gray-400">This agent hasn't published any reports.</p>
        <div v-for="r in reports" :key="r.id" class="mb-2 rounded-xl border border-gray-200 dark:border-gray-800">
          <button class="w-full px-3.5 py-3 flex items-center gap-3 text-left" @click="toggleReport(r.id)">
            <span class="min-w-0 flex-1">
              <span class="block text-sm font-medium truncate">{{ r.title || r.report_type }}</span>
              <span class="block text-xs text-gray-400">{{ r.report_type }} · {{ relative(r.created_at) }}</span>
            </span>
            <svg class="w-4 h-4 text-gray-400 shrink-0 transition" :class="{ 'rotate-180': openReport === r.id }" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" /></svg>
          </button>
          <div v-if="openReport === r.id" class="px-3.5 pb-3 border-t border-gray-100 dark:border-gray-800">
            <InlineError
              v-if="reportErrors[r.id]"
              class="mt-3"
              :message="reportErrors[r.id]"
              retryable
              @retry="retryReport(r.id)"
              @dismiss="dismissReportError(r.id)"
            />
            <div v-else-if="!reportPayloads[r.id]" class="pt-3 space-y-2" aria-busy="true">
              <div v-for="row in 2" :key="row" class="animate-pulse h-8 rounded-lg bg-gray-100 dark:bg-gray-800/60"></div>
              <span class="sr-only">Loading this report…</span>
            </div>
            <div v-else class="pt-3">
              <ReportRenderer
                :report-type="r.report_type"
                :display-hint="r.display_hint"
                :payload="reportPayloads[r.id]"
                :meta="reportRowMeta[r.id]"
                :load-more="reportRowMeta[r.id] ? () => loadMoreRows(r.id) : null"
                :allow-raw="false"
              />
            </div>
          </div>
        </div>
      </template>

      <!-- ----------------------------- FILES ------------------------------ -->
      <template v-else-if="tab === 'files'">
        <h2 class="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-2">Shared with you</h2>
        <p v-if="!documents.length" class="text-sm text-gray-400 mb-5">No files yet.</p>
        <ul v-else class="mb-5 divide-y divide-gray-100 dark:divide-gray-800">
          <li v-for="d in documents" :key="d.name || d.filename" class="py-2 flex items-center gap-3 text-sm">
            <span class="flex-1 truncate">{{ d.filename || d.name }}</span>
            <span class="text-xs text-gray-400">{{ size(d.size_bytes ?? d.size) }}</span>
          </li>
        </ul>

        <h2 class="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-2">You sent</h2>
        <p v-if="!uploads.length" class="text-sm text-gray-400">Nothing uploaded.</p>
        <ul v-else class="divide-y divide-gray-100 dark:divide-gray-800">
          <li v-for="u in uploads" :key="u.name || u.filename" class="py-2 flex items-center gap-3 text-sm">
            <span class="flex-1 truncate">{{ u.filename || u.name }}</span>
            <span class="text-xs text-gray-400">{{ size(u.size_bytes ?? u.size) }}</span>
          </li>
        </ul>
      </template>

      <!-- ------------------------ WHAT IT CAN DO -------------------------- -->
      <template v-else-if="tab === 'capabilities'">
        <p v-if="!capabilities.length" class="text-sm text-gray-400">
          This agent hasn't published anything it can do yet.
        </p>
        <div v-else class="grid gap-2 sm:grid-cols-2">
          <button
            v-for="c in capabilities"
            :key="c.title"
            class="text-left rounded-xl border border-gray-200 dark:border-gray-800 px-3.5 py-3 hover:bg-gray-50 dark:hover:bg-gray-800/50 transition"
            @click="$emit('start-chat', agentName, c.starter_prompt)"
          >
            <div class="text-sm font-medium">{{ c.title }}</div>
            <div v-if="c.description" class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">{{ c.description }}</div>
          </button>
        </div>
      </template>

      <!-- ---------------------------- ACTIVITY ---------------------------- -->
      <template v-else-if="tab === 'activity'">
        <p v-if="!recentWork.length" class="text-sm text-gray-400">No activity in this window.</p>
        <ul v-else class="divide-y divide-gray-100 dark:divide-gray-800">
          <li v-for="w in recentWork" :key="w.id" class="py-2.5 flex items-center gap-3 text-sm">
            <span class="w-2 h-2 rounded-full shrink-0" :class="statusDot(w.status)"></span>
            <span class="flex-1 min-w-0 truncate text-gray-600 dark:text-gray-300" :title="workLabel(w)">{{ workLabel(w) }}</span>
            <span class="text-xs text-gray-400 tabular-nums">{{ duration(w.duration_ms) }}</span>
            <span class="text-xs text-gray-400 shrink-0">{{ relative(w.started_at) }}</span>
          </li>
        </ul>
      </template>
    </div>
  </div>
</template>

<script setup>
/**
 * The Workspace agent page (ent#360).
 *
 * An agent had no home: a roster row started a chat, so there was nowhere to
 * see what it has been doing, nowhere for it to ask you something when no chat
 * is open, and nowhere to show what it can do.
 *
 * It REPORTS; it does not configure (AC #7) — no schedules, no skill editing,
 * no logs, no costs, no model. That is enforced in the service, which never
 * sends those fields, rather than here: a field that does not arrive cannot be
 * rendered by a later edit.
 *
 * Everything is DB-sourced, so the page renders for a stopped agent (AC #6) —
 * degraded (health "unknown", empty sections), never blank.
 */
import { ref, computed, watch, onMounted } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import { BUCKET_COLORS, bucketsForChart, hasChartActivity } from '@/utils/executionBuckets'
import StackedBarChart from '@/components/StackedBarChart.vue'
import InlineError from '@/components/InlineError.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import ReportRenderer from '@/components/reports/ReportRenderer.vue'
import PortalAvatar from './PortalAvatar.vue'
import { PORTAL_BUCKET_LABELS } from './portalUtils'

const props = defineProps({
  agentName: { type: String, required: true },
  // Chats are already loaded by the shell; re-fetching them here would show a
  // different list from the sidebar for a moment.
  threads: { type: Array, default: () => [] },
})
defineEmits(['start-chat', 'open-thread', 'open-menu'])

const store = useClientPortalStore()

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'reports', label: 'Reports' },
  { id: 'files', label: 'Files' },
  { id: 'capabilities', label: 'What it can do' },
  { id: 'activity', label: 'Activity' },
]

// Tabs the time window actually changes. Reports, Files and the capability list
// are not windowed, and a selector that redraws nothing is a broken control.
const WINDOWED_TABS = ['overview', 'activity']

// How many asks show before the toggle. Enough to see there is a queue, few
// enough that the rest of the Overview stays on screen.
const ASKS_PREVIEW = 5

// Mirrors `agent_page.MAX_ASKS`. The service truncates there, so a full-length
// list means "at least this many" — named here rather than inlined so the
// duplication across the API boundary is visible to whoever changes the cap.
const ASKS_CAP = 20

const tab = ref('overview')
const timeWindow = ref('7d')
const page = ref(null)
const loading = ref(false)
const error = ref(null)
// Only the "which card is open" bit is local (#2162). Everything else about
// reports — the list, its loaded/failed flags, per-report payloads, row meta,
// per-report errors — lives in the store (design contract #21), which is also
// what makes the agent-switch race testable: a reset there invalidates requests
// already in flight, which clearing a ref here cannot do.
const openReport = ref(null)
// The store is a singleton and outlives this component, so a fresh MOUNT for a
// different agent would otherwise read the previous one's reports as "already
// loaded" and never refetch — the props watcher below only fires on a change
// within one instance, not on a remount. Every read is therefore gated on the
// state actually belonging to the agent on screen; the store's generation
// counter covers the in-flight half, this covers the at-rest half.
const reportsMine = computed(() => store.reportsAgent === props.agentName)
const reports = computed(() => (reportsMine.value ? store.reports : []))
const reportsLoaded = computed(() => reportsMine.value && store.reportsLoaded)
const reportsError = computed(() => (reportsMine.value ? store.reportsError : null))
const reportPayloads = computed(() => (reportsMine.value ? store.reportPayloads : {}))
const reportRowMeta = computed(() => (reportsMine.value ? store.reportRowMeta : {}))
const reportErrors = computed(() => (reportsMine.value ? store.reportErrors : {}))
const documents = ref([])
const uploads = ref([])

const allAsks = ref(false)

const stats = computed(() => page.value?.stats || { total_executions: 0, timeline: [] })
const asks = computed(() => page.value?.asks || [])
const visibleAsks = computed(() => (allAsks.value ? asks.value : asks.value.slice(0, ASKS_PREVIEW)))
// The service caps asks at MAX_ASKS, so a full list is a floor, not a count —
// rendering a bare "20" against 50 pending would be a wrong number, not a
// rounded one.
const asksBadge = computed(() => (
  asks.value.length >= ASKS_CAP ? `${ASKS_CAP}+` : String(asks.value.length)
))

const chartBuckets = computed(() => bucketsForChart(stats.value))
const hasActivity = computed(() => hasChartActivity(stats.value))
const recentWork = computed(() => page.value?.recent_work || [])
const capabilities = computed(() => page.value?.capabilities || [])
const chats = computed(() => props.threads.filter(
  (t) => !t.is_room && t.agent_name === props.agentName,
))

const windowLabel = computed(() => ({ '7d': '7 days', '14d': '14 days', '30d': '30 days' }[timeWindow.value]))

const healthLabel = computed(() => ({
  healthy: 'Healthy', unhealthy: 'Unhealthy', degraded: 'Degraded',
}[page.value?.header?.health?.status] || 'Status unknown'))
const healthDot = computed(() => ({
  healthy: 'bg-status-success-500', unhealthy: 'bg-status-danger-500', degraded: 'bg-status-warning-500',
}[page.value?.header?.health?.status] || 'bg-gray-300 dark:bg-gray-600'))

// AC5 (#2160): `${name}:${window}` cache, the convention `stores/executions.js`
// already uses. Flipping 7d → 30d → 7d refetched an identical payload every
// time; the window is a view of a fixed past, so the second look is free.
// Stale-while-revalidate: a cached page renders instantly AND refreshes behind
// it, so returning to a tab never shows a spinner over data we already have,
// and never shows yesterday's numbers either.
const pageCache = new Map()

async function load() {
  const key = `${props.agentName}:${timeWindow.value}`
  const cached = pageCache.get(key)
  if (cached) page.value = cached
  loading.value = !cached
  error.value = null
  try {
    const fresh = await store.fetchAgentPage(props.agentName, timeWindow.value)
    pageCache.set(key, fresh)
    page.value = fresh
  } catch (e) {
    error.value = e?.response?.status === 404
      ? "You don't have access to this agent."
      : "Couldn't load this agent right now."
  } finally {
    loading.value = false
  }
}

// Each tab fetches only when first opened: the page is one call, and Reports /
// Files are separate surfaces that most visits never look at.
watch(tab, async (t) => {
  try {
    // Gated on the LOADED FLAG, not on list length: an agent with genuinely
    // zero reports would otherwise refetch on every entry to the tab, and a
    // failed fetch would look identical to an empty one (contract #15).
    if (t === 'reports' && !reportsLoaded.value) {
      await loadReports()
    } else if (t === 'files' && !documents.value.length && !uploads.value.length) {
      const [d, u] = await Promise.all([
        store.fetchDocuments(props.agentName).catch(() => []),
        store.fetchUploads(props.agentName).catch(() => []),
      ])
      documents.value = d || []
      uploads.value = u || []
    }
  } catch { /* the empty state is the honest answer */ }
})

// The agent can change without a remount (sidebar → another agent), so reset
// every per-agent cache. Leaving them would show one agent's reports under
// another's name — the class of bug ent#359's review found twice.
watch(() => props.agentName, () => {
  pageCache.clear()   // #2160: keyed by name, but never serve one agent's page for another
  page.value = null
  // #2162: the store owns report state AND the generation counter, so this also
  // invalidates any report request already in flight for the previous agent —
  // the half a plain ref-clear cannot do, and the reason `reportsLoaded` is safe
  // to add at all (it would otherwise make a transient wrong-render permanent).
  store.resetAgentReports(props.agentName)
  openReport.value = null
  documents.value = []
  uploads.value = []
  allAsks.value = false
  tab.value = 'overview'
  load()
})
watch(timeWindow, load)
onMounted(load)

function loadReports() {
  return store.loadAgentReports(props.agentName)
}

async function toggleReport(id) {
  if (openReport.value === id) { openReport.value = null; return }
  openReport.value = id
  // The store owns the already-loaded / already-in-flight guards, so a rapid
  // expand-collapse-expand cannot fire duplicate requests — which matters here
  // because each one re-reads the whole blob server-side.
  await store.loadAgentReport(props.agentName, id)
}

function retryReport(id) {
  return store.loadAgentReport(props.agentName, id)
}

function dismissReportError(id) {
  store.clearReportError(id)
}

function loadMoreRows(id) {
  return store.loadMoreReportRows(props.agentName, id)
}

const pct = (v) => (v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`)
const size = (b) => (b === null || b === undefined ? '' : b > 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`)
const duration = (ms) => (!ms ? '' : ms < 1000 ? `${ms}ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms / 60000)}m`)

const statusDot = (s) => ({
  success: 'bg-status-success-500', failed: 'bg-status-danger-500', error: 'bg-status-danger-500',
  running: 'bg-action-primary-500', queued: 'bg-gray-300 dark:bg-gray-600',
}[s] || 'bg-gray-300 dark:bg-gray-600')

// Trigger names are internal; the page is for someone in the driver seat.
const triggerLabel = (t) => ({
  manual: 'Chat', chat: 'Chat', schedule: 'Scheduled run', webhook: 'Webhook',
  loop: 'Loop', reminder: 'Reminder', event: 'Event', voip: 'Phone call',
  voice: 'Voice', mcp: 'Tool call', fan_out: 'Fan-out',
}[t] || (t ? t.replace(/_/g, ' ') : 'Task'))

// What a row of work actually says. The trigger alone repeats "Scheduled run"
// down the whole list and tells the reader nothing, so a resolved schedule name
// is appended when the service could find one (#2161). It is deliberately the
// only content on this row: the task's message is a prompt, and this page is
// read by external clients as well as operators.
const workLabel = (w) => (
  w.schedule_name
    ? `${triggerLabel(w.triggered_by)} · ${w.schedule_name}`
    : triggerLabel(w.triggered_by)
)

function relative(iso) {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const mins = Math.round((Date.now() - then) / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.round(hrs / 24)
  return days < 30 ? `${days}d ago` : new Date(iso).toLocaleDateString()
}
</script>
