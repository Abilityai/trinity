<template>
  <!-- ent#547: the agent's context, as the rail's INFO TAB.
       ent#523 opened it from the header into the rail's place as a sibling
       (ruled 2026-09-05); the operator reversed that on 2026-09-07 after testing
       `dev` — a header-launched sibling panel "reads as one more top-level
       thing". `portalRail.js`'s `info` entry carries the full argument, including
       why the tab's door is SOLO_AGENT and what blocks a room form.

       Canvas and Files are deliberately absent: they are rail tabs since
       ent#475, and duplicating them here would give the same capability two
       homes that can disagree. That reasoning is now literal — this IS a rail
       tab, and the three sit in one strip.

       As a tab BODY this owns no chrome. `PortalRail` supplies the column, the
       scroll axis and the padding, so an `overflow-y-auto` here would nest two
       scrollers and a `p-4` would double the inset; the close control is the
       rail's own collapse. What the retired header DID own and must not be lost
       with it is the identity row below — the avatar, and #2196's deliberately
       separate health/availability pair. -->
  <div data-testid="portal-agent-details" class="space-y-6">
    <div class="flex items-center gap-2">
      <PortalAvatar :name="agentName" :avatar-url="header?.avatar_url" :size="28" />
      <div class="min-w-0 flex-1">
        <div class="text-sm font-medium truncate">{{ label }}</div>
        <!-- #2196: health and availability are their OWN labelled facts, never
             folded into one dot. They differ in freshness by construction —
             health is the last persisted `agent_health_checks` row (stale by
             design, and `unknown` on most installs because monitoring is
             default-OFF) while availability is read at request time. One widget
             carrying both would tell the viewer neither. Carried across from
             the retired agent page unchanged, and then across again from the
             panel header ent#547 retired. -->
        <div class="flex items-center gap-2.5 text-[11px] text-gray-400">
          <span class="inline-flex items-center gap-1">
            <span class="w-1.5 h-1.5 rounded-full" :class="healthDot"></span>{{ healthLabel }}
          </span>
          <span v-if="availability" class="inline-flex items-center gap-1" :title="availability.title">
            <span class="w-1.5 h-1.5 rounded-full" :class="availabilityDot"></span>{{ availability.label }}
          </span>
        </div>
      </div>
    </div>

    <!-- #2597: a failed REFRESH keeps everything on screen and says so beside
         it (ent#253) — the band's rule, applied to the panel that reads the
         same payload. Sits at the top of the panel body; ent#547 removed the
         `flex-1 … p-4 space-y-6` wrapper this used to live inside, so it is a
         direct child now and the rule is unchanged. -->
    <InlineError
      v-if="pageError && pageLoaded"
      :message="pageError"
      retryable
      @retry="reload"
    />

    <p v-if="header?.description" class="text-sm text-gray-600 dark:text-gray-300">{{ header.description }}</p>

    <!-- ---------------------------- CHATS ------------------------------ -->
    <!-- The FULL list, which is what this panel is for: the tab strip shows
         what fits and hides the rest, and the design pass puts the complete
         one here. Archived chats included — a retired Main is still a chat
         you can open, it just stopped being the one the agent reaches you
         in. -->
    <section>
      <h2 class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 mb-2">Your chats</h2>
      <p v-if="!chats.length" class="text-sm text-gray-400">No conversations yet.</p>
      <ul v-else class="divide-y divide-gray-100 dark:divide-gray-800">
        <li v-for="c in chats" :key="c.id || c.session_id">
          <button
            class="w-full py-2 flex items-center gap-2 text-left text-sm hover:opacity-80"
            @click="$emit('open-thread', c)"
          >
            <span
              v-if="c.is_main"
              class="shrink-0 text-[10px] font-semibold uppercase tracking-wide text-action-primary-600 dark:text-action-primary-400"
            >{{ MAIN_TAB_LABEL }}</span>
            <span class="flex-1 min-w-0 truncate" :class="{ 'text-gray-400': c.archived_at }">
              {{ c.is_main ? 'Current conversation' : chatTitle(c) }}
            </span>
            <span
              v-if="c.unread"
              class="shrink-0 min-w-[1.125rem] px-1 h-[1.125rem] rounded-full bg-action-primary-600 text-white text-[10px] font-semibold flex items-center justify-center"
            >{{ c.unread }}</span>
            <span class="text-[11px] text-gray-400 shrink-0">{{ relative(c.last_message_at) }}</span>
          </button>
        </li>
      </ul>
    </section>

    <!-- ------------------------ WHAT IT CAN DO ------------------------- -->
    <!-- ent#138's rule, unchanged by the move: a card PRE-FILLS the composer
         and never auto-sends. -->
    <section>
      <h2 class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 mb-2">What it can do</h2>
      <!-- #2597: the failure arm comes FIRST, because the empty arm below it
           is a positive claim ("hasn't published anything") that a failed
           fetch supplies no evidence for. `LoadFailed` is the "failed" member
           of the loading/empty/failed triad (#1926) precisely so a broken
           fetch never borrows the empty state's copy and points the reader at
           the wrong remedy. -->
      <LoadFailed
        v-if="pageFailed"
        dense
        title="Couldn't load this agent"
        :message="pageError"
        @retry="reload"
      />
      <p v-else-if="!capabilities.length" class="text-sm text-gray-400">
        This agent hasn't published anything it can do yet.
      </p>
      <div v-else class="space-y-2">
        <button
          v-for="c in capabilities"
          :key="c.title"
          class="w-full text-left rounded-xl border border-gray-200 dark:border-gray-800 px-3 py-2.5 hover:bg-gray-50 dark:hover:bg-gray-800/50 transition"
          @click="$emit('use-playbook', c.starter_prompt)"
        >
          <div class="text-sm font-medium">{{ c.title }}</div>
          <div v-if="c.description" class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">{{ c.description }}</div>
        </button>
      </div>
    </section>

    <!-- --------------------------- REPORTS ----------------------------- -->
    <!-- #2162: through the SHARED components/reports/ dispatch, with the
         client-side `ReportSummary` fallback rather than the operator
         surfaces' raw JSON viewer. Carried over verbatim — that split is a
         disclosure decision, not a style one. -->
    <section>
      <h2 class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 mb-2">Reports</h2>
      <div v-if="!reportsLoaded && !reportsError" class="space-y-2" aria-busy="true">
        <div v-for="row in 2" :key="row" class="animate-pulse h-12 rounded-xl bg-gray-100 dark:bg-gray-800/60"></div>
        <span class="sr-only">Loading this agent's reports…</span>
      </div>
      <LoadFailed
        v-else-if="reportsError"
        dense
        title="Couldn't load reports"
        :message="reportsError"
        @retry="loadReports"
      />
      <p v-else-if="!reports.length" class="text-sm text-gray-400">This agent hasn't published any reports.</p>
      <div v-for="r in reports" :key="r.id" class="mb-2 rounded-xl border border-gray-200 dark:border-gray-800">
        <button class="w-full px-3 py-2.5 flex items-center gap-2 text-left" @click="toggleReport(r.id)">
          <span class="min-w-0 flex-1">
            <span class="block text-sm font-medium truncate">{{ r.title || r.report_type }}</span>
            <span class="block text-[11px] text-gray-400">{{ r.report_type }} · {{ relative(r.created_at) }}</span>
          </span>
          <svg class="w-4 h-4 text-gray-400 shrink-0 transition" :class="{ 'rotate-180': openReport === r.id }" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" /></svg>
        </button>
        <div v-if="openReport === r.id" class="px-3 pb-3 border-t border-gray-100 dark:border-gray-800">
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
              :fallback-component="ReportSummary"
            />
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup>
/**
 * Agent details (ent#523) — the half of the retired agent page that answers
 * "what has this agent got": your chats with it, what it can do, what it has
 * published. The other half (the numbers) is always visible in the band.
 *
 * Opens into the rail's place from the band's "Agent details" control.
 */
import { ref, computed, toRef, watch, onMounted } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import InlineError from '@/components/InlineError.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import ReportRenderer from '@/components/reports/ReportRenderer.vue'
import ReportSummary from '@/components/reports/ReportSummary.vue'
import PortalAvatar from './PortalAvatar.vue'
import { agentDisplayName } from '@/utils/agentName'
import { availabilityChip, threadTitle, MAIN_TAB_LABEL } from './portalUtils'
import { usePortalAgentPage } from '@/composables/usePortalAgentPage'

const props = defineProps({
  agentName: { type: String, required: true },
  agent: { type: Object, default: null },
  threads: { type: Array, default: () => [] },
})
// ent#547: no `close`. As a rail tab the dismissal is the rail's own collapse
// control, not a button this body owns — a second X inside the column would
// be a second way to do one thing, and the two would disagree about whether
// the rail is shut or merely on another tab.
defineEmits(['open-thread', 'use-playbook'])

const store = useClientPortalStore()
const openReport = ref(null)

// The window is fixed here: this panel shows no windowed figure, and a second
// selector disagreeing with the band's would be two controls for one fact.
const timeWindow = ref('7d')
const { header, capabilities, loaded: pageLoaded, error: pageError, reload } =
  usePortalAgentPage(toRef(props, 'agentName'), timeWindow)

// #2597: the two faces of a failed `/page`, kept apart because the honest
// answer differs (ent#253).
//
//   pageFailed — the FIRST load failed, so there is nothing to show. Every
//     region fed by this payload must say so instead of rendering its own
//     fallback: `header` is null, so identity degrades to the slug and the
//     description disappears, and `capabilities` is `[]`, which renders "this
//     agent hasn't published anything it can do yet" — a positive claim about
//     the agent made on no evidence at all. That last one is the defect: it is
//     indistinguishable from a real answer.
//
//   a failed REFRESH (`pageError && pageLoaded`) keeps the data it has and says
//     so beside it, which is what the band already did.
//
// Gated on the VERDICT, never on `loading` (#2540/#1927): a background refresh
// must not blank a panel that is already reading correctly.
const pageFailed = computed(() => !!pageError.value && !pageLoaded.value)

// `agentDisplayName` is the shared rule (§1.3.1 FR-3), the same one the
// conversation header uses — not the sidebar's local `agentLabel` const,
// which is a third copy of it that this panel should not become a fourth of.
const label = computed(() => agentDisplayName(props.agent || { name: props.agentName }))

const healthLabel = computed(() => ({
  healthy: 'Healthy', unhealthy: 'Unhealthy', degraded: 'Degraded',
}[header.value?.health?.status] || 'Status unknown'))
const healthDot = computed(() => ({
  healthy: 'bg-status-success-500', unhealthy: 'bg-status-danger-500', degraded: 'bg-status-warning-500',
}[header.value?.health?.status] || 'bg-gray-300 dark:bg-gray-600'))

// #2196: the same pure rule the sidebar row and the composer notice use — one
// decision, several surfaces. `owner` comes off the header so the copy names
// who to ask.
const availability = computed(() => availabilityChip(
  { ...(props.agent || {}), owner: header.value?.owner ?? props.agent?.owner },
  { detailed: true },
))
const availabilityDot = computed(() => (
  availability.value?.state === 'stopped'
    ? 'bg-gray-400 dark:bg-gray-500'
    : 'bg-status-warning-500'
))

// Main first, then recency — the same order the tab strip uses, because the
// two are the same list at different lengths and a person reading both should
// not have to re-find Main. Unlike the strip, ARCHIVED chats are included:
// this panel is where the full history lives, and a retired Main is still a
// chat you can open.
const chats = computed(() => {
  const ts = (t) => {
    const iso = t.last_message_at || t.created_at
    const n = iso ? new Date(iso).getTime() : 0
    return Number.isNaN(n) ? 0 : n
  }
  return props.threads
    .filter((t) => !t.is_room && t.agent_name === props.agentName)
    .slice()
    .sort((a, b) => (b.is_main ? 1 : 0) - (a.is_main ? 1 : 0) || ts(b) - ts(a))
})
const chatTitle = threadTitle

// The store is a singleton and outlives this component, so every read is gated
// on the loaded state actually belonging to the agent on screen — the #2162
// rule, which a panel that mounts and unmounts repeatedly needs more, not less.
const reportsMine = computed(() => store.reportsAgent === props.agentName)
const reports = computed(() => (reportsMine.value ? store.reports : []))
const reportsLoaded = computed(() => reportsMine.value && store.reportsLoaded)
const reportsError = computed(() => (reportsMine.value ? store.reportsError : null))
const reportPayloads = computed(() => (reportsMine.value ? store.reportPayloads : {}))
const reportRowMeta = computed(() => (reportsMine.value ? store.reportRowMeta : {}))
const reportErrors = computed(() => (reportsMine.value ? store.reportErrors : {}))

function loadReports() {
  return store.loadAgentReports(props.agentName)
}

async function toggleReport(id) {
  if (openReport.value === id) { openReport.value = null; return }
  openReport.value = id
  await store.loadAgentReport(props.agentName, id)
}

function retryReport(id) { return store.loadAgentReport(props.agentName, id) }
function dismissReportError(id) { store.clearReportError(id) }
function loadMoreRows(id) { return store.loadMoreReportRows(props.agentName, id) }

watch(() => props.agentName, () => { openReport.value = null; loadReports() })
onMounted(loadReports)

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
