<template>
  <!--
    "Guardrails (24 h)" — Operations › Health (trinity-enterprise#165 S4,
    GUARD-005). Admin surface: the Health tab is admin-gated by its parent, and
    the read behind it (`GET /api/ops/guardrail-stats`) answers an admin with
    every live agent.

    Silence is kept honest across the fleet. The total is a number only when
    some agent is reporting; "No guardrail events in the last 24 h" is said only
    then. Every agent that is not reporting is named with its own words — never
    reported (an older image), stopped reporting since a time, or a runtime with
    no hooks — in one bounded list beside the agents that had events.

    The card keeps one footprint across loading / failed / loaded (principle 4)
    and a refresh swaps values in place: the skeleton is the first load only.
  -->
  <BaseCard flush data-testid="guardrail-stats-card">
    <div class="px-4 py-3 border-b border-gray-200 dark:border-gray-750 flex items-baseline justify-between gap-3">
      <h2 class="text-lg font-semibold text-gray-900 dark:text-gray-100">Guardrails ({{ hours }} h)</h2>
      <p class="text-[12.5px] text-gray-600 dark:text-gray-400">What the agents' guardrail hooks refused or hid, as reported</p>
    </div>

    <div
      v-if="view.state === 'loading'"
      class="p-4 space-y-3 animate-pulse motion-reduce:animate-none"
      aria-busy="true"
      data-testid="guardrail-stats-loading"
    >
      <div class="h-8 w-20 rounded bg-gray-100 dark:bg-gray-750"></div>
      <div class="h-3 w-2/3 rounded bg-gray-100 dark:bg-gray-750"></div>
      <div class="h-3 w-1/2 rounded bg-gray-100 dark:bg-gray-750"></div>
      <span class="sr-only">Loading guardrail counts…</span>
    </div>

    <div v-else-if="view.state === 'failed'" data-testid="guardrail-stats-failed">
      <LoadFailed
        dense
        title="Couldn't load the guardrail counts"
        message="We can't tell what the guardrails did across the fleet. Check your connection and try again."
        :detail="store.fleetError || ''"
        :retrying="store.fleetLoading"
        @retry="store.loadFleet()"
      />
    </div>

    <p
      v-else-if="agents.length === 0"
      class="px-4 py-6 text-center text-[12.5px] text-gray-600 dark:text-gray-400"
      data-testid="guardrail-stats-no-agents"
    >No agents yet. Guardrail events appear here once an agent runs.</p>

    <div v-else class="p-4 space-y-3">
      <div v-if="view.stale && !staleDismissed" data-testid="guardrail-stats-stale">
        <InlineError
          :message="staleMessage"
          :detail="store.fleetError || ''"
          retryable
          @retry="store.loadFleet()"
          @dismiss="staleDismissed = true"
        />
      </div>

      <div class="flex flex-wrap items-baseline gap-x-6 gap-y-2">
        <div>
          <div class="text-2xl font-bold tabular-nums text-gray-900 dark:text-gray-100" data-testid="guardrail-stats-total">
            {{ anyReporting ? stats.events : '—' }}
          </div>
          <div class="text-[12.5px] text-gray-600 dark:text-gray-400">events</div>
        </div>
        <ul v-if="kinds.length" class="flex flex-wrap gap-2" aria-label="Events by kind">
          <li v-for="k in kinds" :key="k.kind" data-testid="guardrail-stats-kind">
            <BaseBadge :variant="kindVariant(k.kind)" size="sm">{{ k.label }} <span class="tabular-nums">{{ k.count }}</span></BaseBadge>
          </li>
        </ul>
      </div>

      <p class="text-[12.5px] text-gray-600 dark:text-gray-400 tabular-nums" data-testid="guardrail-stats-coverage">
        {{ coverageTally }}
      </p>

      <p
        v-if="!anyReporting"
        class="text-[12.5px] text-gray-700 dark:text-gray-300"
        data-testid="guardrail-stats-none-reporting"
      >No agent is reporting guardrail events, so no count can be given. The agents below say why.</p>
      <p
        v-else-if="stats.events === 0"
        class="text-[12.5px] text-gray-700 dark:text-gray-300"
        data-testid="guardrail-stats-empty"
      >{{ NO_EVENTS_COPY }}</p>

      <ul
        v-if="rows.length"
        class="max-h-72 overflow-y-auto -mx-4 border-t border-gray-200 dark:border-gray-750 divide-y divide-gray-200 dark:divide-gray-750 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:focus-visible:ring-action-primary-400/40"
        tabindex="0"
        aria-label="Agents with guardrail events or not reporting"
      >
        <li
          v-for="row in rows"
          :key="row.agent_name"
          class="px-4 py-2.5 flex items-center gap-3"
          :data-agent="row.agent_name"
          data-testid="guardrail-stats-agent"
        >
          <button
            type="button"
            class="min-w-0 flex-1 text-left text-sm font-medium text-gray-900 dark:text-gray-100 truncate hover:text-action-primary-700 dark:hover:text-action-primary-400 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 rounded"
            :title="`Open ${row.agent_name}'s guardrail settings`"
            @click="openAgent(row.agent_name)"
          >{{ row.agent_name }}</button>
          <span v-if="row.notice" class="min-w-0 text-[12.5px] text-gray-600 dark:text-gray-400 truncate" :title="row.notice.text">
            {{ row.notice.text }}
          </span>
          <BaseBadge v-if="row.notice" :variant="row.notice.variant" size="sm" dot class="shrink-0">{{ COVERAGE_LABELS[row.coverage] }}</BaseBadge>
          <span v-if="row.events" class="shrink-0 text-sm tabular-nums text-gray-900 dark:text-gray-100">{{ row.events }}</span>
        </li>
      </ul>
    </div>
  </BaseCard>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useGuardrailEventsStore, FLEET_WINDOW_HOURS } from '../stores/guardrailEvents'
import { staleBannerMessage, viewState } from '../utils/loadingState'
import {
  COVERAGE_LABELS,
  NO_EVENTS_COPY,
  coverageNotice,
  kindRows,
  kindVariant,
} from '../utils/guardrailEvents'
import BaseCard from './base/BaseCard.vue'
import BaseBadge from './base/BaseBadge.vue'
import LoadFailed from './LoadFailed.vue'
import InlineError from './InlineError.vue'

const store = useGuardrailEventsStore()
const router = useRouter()
const staleDismissed = ref(false)

const hours = FLEET_WINDOW_HOURS
const stats = computed(() => store.fleet || { events: 0, by_kind: {}, coverage: {}, agents: [] })
const agents = computed(() => (Array.isArray(stats.value.agents) ? stats.value.agents : []))

// `count` 1: an empty fleet is its own branch below, not the "no events" copy.
const view = computed(() => viewState({ hasLoaded: store.fleetHasLoaded, error: store.fleetError, count: 1 }))
const staleMessage = computed(() => staleBannerMessage('the guardrail counts', store.fleetLoadedAt))

const anyReporting = computed(() => (stats.value.coverage?.reporting || 0) > 0)
const kinds = computed(() => kindRows(stats.value.by_kind))

const coverageTally = computed(() => {
  const c = stats.value.coverage || {}
  const n = (k) => c[k] || 0
  const parts = [`${n('reporting')} reporting`]
  if (n('stale')) parts.push(`${n('stale')} stopped reporting`)
  if (n('never')) parts.push(`${n('never')} not reporting`)
  if (n('no_hooks')) parts.push(`${n('no_hooks')} no hooks`)
  return `Agents: ${parts.join(' · ')}`
})

// The agents worth a line: those with events, and those whose silence means
// something other than "nothing happened". The server sorts loudest first.
const rows = computed(() => agents.value
  .map((a) => ({ ...a, notice: coverageNotice(a.coverage, a.reporting) }))
  .filter((a) => a.events > 0 || a.notice))

function openAgent(name) {
  router.push({ path: `/agents/${encodeURIComponent(name)}`, query: { tab: 'settings', section: 'guardrails' } })
}

onMounted(() => { store.loadFleet() })

defineExpose({ refresh: () => store.loadFleet() })
</script>
