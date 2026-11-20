<template>
  <!--
    Flight Deck (abilityai/trinity-enterprise#1307) — the fleet at work, on one
    screen. A props-driven pane, like the Dashboard's other modes: `agents` is
    the store's `visibleAgents` (access-scoped, filters applied). The host —
    the Dashboard, or the tabless window page — owns the fleet store's socket
    and polls; this pane owns only the host-load and history reads, and stops
    them while the page is hidden.

    Colour discipline (it is a dashboard, read for its data): ink is neutral
    gray; ONE accent marks "working"; red and amber appear only on a failure or
    a reading past its limit. Nothing is coloured for identity or decoration.
  -->
  <div class="h-full flex flex-col" data-testid="flight-deck">
    <div class="flex-none flex flex-wrap items-center gap-x-4 gap-y-2 px-4 sm:px-6 py-2">
      <p class="text-sm tabular-nums text-gray-600 dark:text-gray-300" data-testid="flight-deck-clock">{{ clock }} · {{ dateLine }}</p>
      <BaseBadge v-if="demo" variant="neutral" data-testid="flight-deck-demo">Demo data — not your fleet</BaseBadge>
      <p
        v-if="stale"
        class="text-[12.5px] font-medium text-status-warning-700 dark:text-status-warning-400"
        role="status"
        data-testid="flight-deck-stale"
      >
        ⚠ Not updating — showing the last values received
      </p>
      <div class="ml-auto flex items-center gap-3">
        <BaseToggle
          :model-value="deck.showText"
          label="Show task text"
          data-testid="flight-deck-privacy"
          @update:model-value="deck.setShowText"
        />
        <BaseButton
          v-if="!windowMode"
          variant="secondary"
          size="sm"
          data-testid="flight-deck-popout"
          @click="openWindow"
        >
          Open in window
        </BaseButton>
      </div>
    </div>

    <InlineError
      v-if="popupBlocked"
      class="flex-none mx-4 sm:mx-6 mb-2"
      :message="`Your browser blocked the new window. Allow pop-ups for this site, or open ${windowHref} yourself.`"
      @dismiss="popupBlocked = false"
    />

    <div class="flex-1 min-h-0 overflow-y-auto xl:overflow-hidden">
      <div v-if="view.state === 'loading'" class="p-6" aria-busy="true">
        <span class="sr-only">Loading the fleet</span>
        <SkeletonLoader variant="rows" :count="8" height="2.5rem" gap="0.5rem" />
      </div>

      <LoadFailed
        v-else-if="view.state === 'failed'"
        title="Couldn't load the fleet"
        message="The agent list did not load. Check that the backend is running, then try again."
        @retry="$emit('retry')"
      />

      <div v-else-if="view.state === 'empty'" class="text-center px-4 py-12" data-testid="flight-deck-empty">
        <p class="font-medium text-gray-900 dark:text-gray-100">No agents to show yet</p>
        <p class="text-sm mt-1 text-gray-600 dark:text-gray-400">
          The Flight Deck shows your agents at work. Create one and it appears here.
        </p>
        <BaseButton class="mt-4" @click="router.push({ path: '/', query: { view: 'list' } })">Open the agent list</BaseButton>
      </div>

      <div v-else class="h-full flex flex-col gap-3 px-4 sm:px-6 pb-4">
        <!-- 1. the headline numbers, one row -->
        <BaseCard flush class="flex-none">
          <dl class="grid grid-cols-2 sm:grid-cols-4 xl:grid-cols-[repeat(7,minmax(0,1fr))_minmax(0,2.4fr)] divide-gray-200 dark:divide-gray-750 xl:divide-x">
            <div v-for="s in headline" :key="s.label" class="px-4 py-3" :data-testid="`flight-deck-kpi-${s.id}`">
              <dt class="text-[12.5px] text-gray-600 dark:text-gray-300">{{ s.label }}</dt>
              <dd class="mt-0.5 text-2xl font-semibold" :class="s.tone">{{ s.value }}</dd>
            </div>
            <div class="col-span-2 sm:col-span-4 xl:col-span-1 px-4 py-3" data-testid="flight-deck-host">
              <dt class="text-[12.5px] text-gray-600 dark:text-gray-300">Host</dt>
              <dd
                v-if="!host"
                class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-300"
                data-testid="flight-deck-host-missing"
              >
                {{ hostError ? 'Host load is unavailable right now. The rest of the page still updates.' : 'Reading host load…' }}
              </dd>
              <dd v-else class="mt-1.5 grid grid-cols-3 gap-4">
                <div v-for="m in hostMetrics" :key="m.name" :title="m.note">
                  <div class="flex items-baseline justify-between text-[12.5px]">
                    <span class="text-gray-600 dark:text-gray-300">{{ m.name }}</span>
                    <span class="tabular-nums" :class="m.hot ? 'font-semibold text-status-danger-700 dark:text-status-danger-400' : 'text-gray-900 dark:text-gray-100'">{{ m.value }}</span>
                  </div>
                  <span class="mt-1 block h-1 rounded-full bg-gray-200 dark:bg-gray-750 overflow-hidden" aria-hidden="true">
                    <span
                      class="block h-full rounded-full transition-[width] duration-700 motion-reduce:transition-none"
                      :class="m.hot ? 'bg-status-danger-500' : 'bg-gray-500 dark:bg-gray-400'"
                      :style="{ width: `${m.pct}%` }"
                    ></span>
                  </span>
                </div>
              </dd>
            </div>
          </dl>
        </BaseCard>

        <div class="flex-1 min-h-0 grid gap-3 grid-cols-1 xl:grid-cols-[minmax(0,1fr)_20rem]">
          <!-- 2. the agents: the main stage -->
          <BaseCard flush class="min-h-[18rem] xl:min-h-0 flex flex-col">
            <header class="flex-none flex items-baseline justify-between px-4 pt-3 pb-2">
              <h3 class="text-[12.5px] font-medium text-gray-600 dark:text-gray-300">Agents</h3>
              <span class="text-[12.5px] tabular-nums text-gray-500 dark:text-gray-400">{{ counts.total }} · working first · last 24 hours</span>
            </header>
            <div class="flex-1 min-h-0 overflow-auto">
              <table class="w-full text-sm" data-testid="flight-deck-agents">
                <thead class="sticky top-0 bg-white dark:bg-gray-800 text-left text-[12.5px] text-gray-500 dark:text-gray-400">
                  <tr class="border-b border-gray-200 dark:border-gray-750">
                    <th scope="col" class="font-normal py-1.5 pl-4 pr-2">Agent</th>
                    <th scope="col" class="font-normal py-1.5 px-2">State</th>
                    <th scope="col" class="font-normal py-1.5 px-2 w-[26%]">Doing</th>
                    <th scope="col" class="font-normal py-1.5 px-2">Success</th>
                    <th scope="col" class="font-normal py-1.5 px-2 text-right">Runs</th>
                    <th scope="col" class="font-normal py-1.5 px-2 text-right">Cost</th>
                    <th scope="col" class="font-normal py-1.5 pl-2 pr-4 text-right">Context</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="row in rows"
                    :key="row.name"
                    class="border-b border-gray-100 dark:border-gray-750 last:border-0"
                    :class="row.stopped ? 'text-gray-500 dark:text-gray-400' : 'text-gray-600 dark:text-gray-300'"
                  >
                    <td class="py-2 pl-4 pr-2 max-w-0 w-[24%]">
                      <span class="flex items-center gap-2">
                        <span aria-hidden="true" class="flex-none text-[11px]" :class="row.working ? 'text-action-primary-600 dark:text-action-primary-400' : ''">{{ row.working ? '●' : row.stopped ? '◌' : '○' }}</span>
                        <router-link
                          :to="`/agents/${row.name}`"
                          class="truncate font-medium hover:underline"
                          :class="row.stopped ? '' : 'text-gray-900 dark:text-gray-100'"
                          :title="row.label"
                        >{{ row.label }}</router-link>
                        <span v-if="row.autonomous" class="flex-none text-[12.5px] text-gray-500 dark:text-gray-400" title="Autonomy is on">auto</span>
                      </span>
                    </td>
                    <td class="py-2 px-2 whitespace-nowrap tabular-nums" :class="row.working ? 'font-medium text-gray-900 dark:text-gray-100' : ''" data-testid="flight-deck-state">{{ stateLine(row) }}</td>
                    <td class="py-2 px-2 max-w-0">
                      <span v-if="row.task" class="block truncate" :title="row.task" data-testid="flight-deck-task">{{ row.task }}</span>
                    </td>
                    <td class="py-2 px-2 whitespace-nowrap" data-testid="flight-deck-facts">
                      <span v-if="row.successRate != null" class="flex items-center gap-2">
                        <span class="h-1 w-12 flex-none rounded-full bg-gray-200 dark:bg-gray-750 overflow-hidden" aria-hidden="true">
                          <span class="block h-full rounded-full" :class="rateBar(row.successRate)" :style="{ width: `${row.successRate}%` }"></span>
                        </span>
                        <span class="tabular-nums" :class="rateInk(row.successRate)">{{ row.successRate }}%</span>
                      </span>
                      <span v-else title="No runs in the last 24 hours">—</span>
                    </td>
                    <td class="py-2 px-2 text-right tabular-nums">{{ row.runs || '—' }}</td>
                    <td class="py-2 px-2 text-right tabular-nums">{{ row.runs ? costLabel(row.cost) : '—' }}</td>
                    <td class="py-2 pl-2 pr-4 text-right tabular-nums" :class="row.contextPercent >= 80 ? 'font-medium text-status-warning-700 dark:text-status-warning-400' : ''">{{ row.contextPercent == null ? '—' : `${row.contextPercent}%` }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </BaseCard>

          <!-- 3. the side: the fleet at a glance, what is next, what just happened -->
          <div class="min-w-0 min-h-0 flex flex-col gap-3 xl:overflow-hidden">
            <BaseCard class="flex-none">
              <div class="mx-auto h-44 w-44">
                <FleetRing :rows="rows" :counts="counts" :feed="feed" />
              </div>
            </BaseCard>

            <DeckCard title="Up next" note="schedules" class="flex-none">
              <ul class="text-[12.5px] min-h-[3.75rem]" data-testid="flight-deck-next">
                <li v-if="!next.length" class="text-gray-600 dark:text-gray-300">No scheduled runs coming up.</li>
                <li v-for="n in next" :key="n.key" class="grid grid-cols-[3.75rem_minmax(0,1fr)] gap-x-2 py-0.5">
                  <span class="tabular-nums text-gray-500 dark:text-gray-400">in {{ agoLabel(n.inSec) }}</span>
                  <span class="truncate text-gray-900 dark:text-gray-100">{{ labelOf(n.agent) }}<span v-if="n.name" class="text-gray-600 dark:text-gray-300"> · {{ n.name }}</span></span>
                </li>
              </ul>
            </DeckCard>

            <DeckCard title="Latest" :note="`last ${networkStore.timeRangeHours}h`" class="flex-1 min-h-[6rem]">
              <ul class="h-full overflow-hidden text-[12.5px]" data-testid="flight-deck-feed">
                <li v-if="!feed.length" class="text-gray-600 dark:text-gray-300">Nothing has run in this window.</li>
                <li v-for="e in feed" :key="e.key" class="grid grid-cols-[2.75rem_minmax(0,1fr)_auto] gap-x-2 py-0.5">
                  <span class="tabular-nums text-gray-500 dark:text-gray-400">{{ hhmm(e.at) }}</span>
                  <span class="truncate text-gray-900 dark:text-gray-100">{{ labelOf(e.agent) }}</span>
                  <span :class="e.kind === 'failed' ? 'font-medium text-status-danger-700 dark:text-status-danger-400' : 'text-gray-600 dark:text-gray-300'">{{ FEED_WORD[e.kind] }}</span>
                </li>
              </ul>
            </DeckCard>
          </div>
        </div>

        <!-- 4. history -->
        <div class="flex-none grid gap-3 grid-cols-1 md:grid-cols-2">
          <DeckCard title="Runs per day" :note="`30 days · ${historyNote(dayTotal)}`">
            <div class="h-12 grid grid-flow-col auto-cols-fr gap-0.5 items-end" data-testid="flight-deck-days">
              <span
                v-for="(b, i) in days"
                :key="i"
                class="rounded-t-sm min-h-px bg-gray-400 dark:bg-gray-500"
                :style="{ height: `${dayMax ? Math.max(2, (b.total / dayMax) * 100) : 2}%` }"
                :title="`${b.bucket}: ${b.total} runs`"
              ></span>
            </div>
          </DeckCard>

          <DeckCard title="Runs per hour" :note="`24 hours · ${historyNote(hourTotal)}${hourFailed ? ` · ${hourFailed} failed` : ''}`">
            <div class="h-12 grid grid-flow-col auto-cols-fr gap-0.5 items-end" data-testid="flight-deck-hours">
              <span
                v-for="(b, i) in hours"
                :key="i"
                class="relative rounded-t-sm min-h-px bg-gray-400 dark:bg-gray-500 overflow-hidden"
                :style="{ height: `${hourMax ? Math.max(2, (b.total / hourMax) * 100) : 2}%` }"
                :title="`${b.bucket}: ${b.total} runs, ${b.failed} failed`"
              >
                <!-- the failed share of the hour, from the top of its bar -->
                <span
                  v-if="b.failed && b.total"
                  class="absolute inset-x-0 top-0 bg-status-danger-500"
                  :style="{ height: `${Math.min(100, (b.failed / b.total) * 100)}%` }"
                ></span>
              </span>
            </div>
          </DeckCard>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseCard from '@/components/base/BaseCard.vue'
import BaseToggle from '@/components/base/BaseToggle.vue'
import DeckCard from '@/components/flight-deck/DeckCard.vue'
import FleetRing from '@/components/flight-deck/FleetRing.vue'
import { useNetworkStore } from '@/stores/network'
import { useFlightDeckStore } from '@/stores/flightDeck'
import { viewState } from '@/utils/loadingState'
import {
  agoLabel, costLabel, deckAgents, deckFeed, fleetCounts, fleetTotals,
  isAwake, isStale, popoutFeatures, upNext,
} from '@/utils/flightDeck'
import { createDemo, stepDemo } from '@/utils/flightDeckDemo'

const props = defineProps({
  // The store's `visibleAgents` — never a second list.
  agents: { type: Array, default: () => [] },
  // True on the tabless window page: no second "Open in window".
  windowMode: { type: Boolean, default: false },
})
defineEmits(['retry'])

const FEED_WORD = { started: 'started', completed: 'finished', failed: 'failed' }
const INK = 'text-gray-900 dark:text-gray-100'
const QUIET = 'text-gray-500 dark:text-gray-400'
const BAD = 'text-status-danger-700 dark:text-status-danger-400'
const WARN = 'text-status-warning-700 dark:text-status-warning-400'
// Below these a success rate stops being plain ink. Set low on purpose: a
// column that is mostly amber says nothing.
const RATE_OK = 75
const RATE_POOR = 50

const route = useRoute()
const router = useRouter()
const networkStore = useNetworkStore()
const deck = useFlightDeckStore()
const { workingState, contextStats, executionStats, historicalCollaborations, schedules, loading, loadError } = storeToRefs(networkStore)

// `?demo=1`: a simulated fleet in place of every input, labelled on screen.
// Nothing is fetched and nothing is written while it is on.
const demo = computed(() => route.query.demo === '1')
const sim = ref(null)
const src = computed(() => (demo.value && sim.value) ? sim.value : {
  agents: props.agents,
  workingState: workingState.value,
  contextStats: contextStats.value,
  executionStats: executionStats.value,
  activities: historicalCollaborations.value,
  schedules: schedules.value,
  host: deck.host,
  days: deck.days,
  hours: deck.hours,
})
const host = computed(() => src.value.host)
const days = computed(() => src.value.days)
const hours = computed(() => src.value.hours)
const hostError = computed(() => !demo.value && deck.hostError)

const windowHref = computed(() => router.resolve({ path: '/flight-deck', query: demo.value ? { demo: '1' } : {} }).href)

const now = ref(Date.now())
const awake = ref(typeof document === 'undefined' ? true : isAwake(document.visibilityState))
const popupBlocked = ref(false)

watch(demo, (on) => {
  sim.value = on ? createDemo(Date.now()) : null
  if (on) deck.stop()
  else if (awake.value) deck.start()
}, { immediate: true })

const rows = computed(() => deckAgents({
  agents: src.value.agents,
  workingState: src.value.workingState,
  contextStats: src.value.contextStats,
  executionStats: src.value.executionStats,
  activities: src.value.activities,
  now: now.value,
  showText: deck.showText,
}))
const names = computed(() => new Set(src.value.agents.map((a) => a.name)))
const counts = computed(() => fleetCounts(rows.value))
const totals = computed(() => fleetTotals(rows.value))
const feed = computed(() => deckFeed(src.value.activities, { names: names.value, limit: 8 }))
const next = computed(() => upNext(src.value.schedules, { names: names.value, now: now.value, limit: 3, showText: deck.showText }))
const labels = computed(() => new Map(rows.value.map((r) => [r.name, r.label])))
const labelOf = (name) => labels.value.get(name) || name

// "No data yet", never "fetch in flight": a background refresh changes nothing
// on screen, and a failed one leaves the agents where they are.
const view = computed(() => viewState({
  loading: loading.value,
  hasLoaded: demo.value || props.agents.length > 0 || (!loading.value && !loadError.value),
  error: demo.value ? null : loadError.value,
  count: src.value.agents.length,
}))
const stale = computed(() => !demo.value && awake.value && (isStale(deck.lastOkAt, now.value) || view.value.stale))

const clock = computed(() => new Date(now.value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }))
const dateLine = computed(() => new Date(now.value).toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long' }))
const hhmm = (at) => new Date(at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })

// One accent, for "working". Red and amber only when a reading is bad.
const headline = computed(() => {
  const c = counts.value
  const t = totals.value
  return [
    { id: 'working', label: 'Working', value: c.working, tone: c.working ? 'text-action-primary-600 dark:text-action-primary-400' : QUIET },
    { id: 'idle', label: 'Idle', value: c.idle, tone: INK },
    { id: 'stopped', label: 'Stopped', value: c.stopped, tone: c.stopped ? INK : QUIET },
    { id: 'runs', label: 'Runs, 24h', value: t.runs.toLocaleString(), tone: t.runs ? INK : QUIET },
    { id: 'success', label: 'Success', value: t.successRate == null ? '—' : `${t.successRate}%`, tone: t.successRate == null ? QUIET : rateInk(t.successRate) || INK },
    { id: 'failed', label: 'Failed', value: t.failed.toLocaleString(), tone: t.failed ? BAD : QUIET },
    { id: 'cost', label: 'Cost', value: costLabel(t.cost), tone: t.cost ? INK : QUIET },
  ]
})

// A success rate is plain ink while it is healthy; colour means "look here".
function rateInk(rate) {
  if (rate >= RATE_OK) return ''
  return rate >= RATE_POOR ? WARN : BAD
}
function rateBar(rate) {
  if (rate >= RATE_OK) return 'bg-gray-500 dark:bg-gray-400'
  return rate >= RATE_POOR ? 'bg-status-warning-500' : 'bg-status-danger-500'
}

const hostMetrics = computed(() => {
  const h = host.value || {}
  const metric = (name, part, note) => ({
    name, note,
    value: `${Math.round(part?.percent ?? 0)}%`,
    pct: Math.max(0, Math.min(100, part?.percent ?? 0)),
    hot: (part?.percent ?? 0) > 90,
  })
  return [
    metric('CPU', h.cpu, h.cpu?.count ? `${h.cpu.count} cores` : ''),
    metric('Memory', h.memory, h.memory ? `${h.memory.used_gb} of ${h.memory.total_gb} GB` : ''),
    metric('Disk', h.disk, h.disk ? `${h.disk.used_gb} of ${h.disk.total_gb} GB` : ''),
  ]
})

const dayTotal = computed(() => days.value.reduce((n, b) => n + b.total, 0))
const dayMax = computed(() => Math.max(0, ...days.value.map((b) => b.total)))
const hourTotal = computed(() => hours.value.reduce((n, b) => n + b.total, 0))
const hourFailed = computed(() => hours.value.reduce((n, b) => n + (b.failed || 0), 0))
const hourMax = computed(() => Math.max(0, ...hours.value.map((b) => b.total)))
function historyNote(total) {
  if (demo.value || deck.historyLoaded) return `${total.toLocaleString()} runs`
  return deck.historyError ? 'unavailable' : '—'
}

function stateLine(row) {
  if (row.stopped) return 'stopped'
  const state = row.working ? 'working' : 'idle'
  return row.sinceSec == null ? state : `${state} ${agoLabel(row.sinceSec)}`
}

function openWindow() {
  const win = window.open(windowHref.value, 'trinity-flight-deck', popoutFeatures(window.screen))
  popupBlocked.value = !win
  win?.focus?.()
}

let ticker = null
function onVisibility() {
  const visible = isAwake(document.visibilityState)
  if (visible === awake.value) return
  awake.value = visible
  if (visible) {
    now.value = Date.now()
    if (!demo.value) deck.start()
  } else {
    deck.stop()
  }
}

onMounted(() => {
  document.addEventListener('visibilitychange', onVisibility)
  ticker = setInterval(() => {
    if (!awake.value) return
    now.value = Date.now()
    if (demo.value && sim.value) sim.value = { ...stepDemo(sim.value, now.value) }
  }, 1000)
  if (awake.value && !demo.value) deck.start()
})

onUnmounted(() => {
  document.removeEventListener('visibilitychange', onVisibility)
  if (ticker) clearInterval(ticker)
  deck.stop()
})
</script>
