<template>
  <!--
    Settings › Guardrails › Recent events (trinity-enterprise#165 S4, GUARD-005).

    What this agent's guardrail hooks refused or hid in the last 24 hours, as
    the platform received it. The Settings tab is owner tier (`agent.can_share`)
    and so is the read (ruling D8): the rows can name owner-only paths a shared
    person tried.

    Silence is never shown as healthy. Before the rows, one notice says which
    kind of silence this is — no hooks on this runtime, never reported (an older
    image), or stopped reporting since a time — and "No guardrail events in the
    last 24 h" is said only while the agent is actually reporting. A failed read
    is a load error, never an empty state; a failed refresh keeps the rows and
    raises the stale banner.
  -->
  <SettingsSection
    title="Recent events"
    description="What the guardrails refused or hid in the last 24 hours. Events are reported by the agent; Codex and Gemini agents have no guardrail hooks."
  >
    <div data-testid="guardrail-events">
      <!-- First load: the list's own footprint, three rows. -->
      <div
        v-if="view.state === 'loading'"
        class="divide-y divide-gray-200 dark:divide-gray-750"
        aria-busy="true"
        data-testid="guardrail-events-loading"
      >
        <div v-for="n in 3" :key="n" class="px-4 py-3 flex items-center gap-3 animate-pulse motion-reduce:animate-none">
          <span class="h-4 w-16 rounded-full bg-gray-100 dark:bg-gray-750"></span>
          <span class="h-3 flex-1 rounded bg-gray-100 dark:bg-gray-750"></span>
          <span class="h-3 w-14 rounded bg-gray-100 dark:bg-gray-750"></span>
        </div>
        <span class="sr-only">Loading guardrail events…</span>
      </div>

      <div v-else-if="view.state === 'failed'" data-testid="guardrail-events-failed">
        <LoadFailed
          dense
          title="Couldn't load the guardrail events"
          message="We can't tell what the guardrails did on this agent. Check your connection and try again."
          :detail="store.agentError || ''"
          :retrying="store.agentLoading"
          @retry="refresh"
        />
      </div>

      <template v-else>
        <div v-if="view.stale && !staleDismissed" class="px-4 pt-3" data-testid="guardrail-events-stale">
          <InlineError
            :message="staleMessage"
            :detail="store.agentError || ''"
            retryable
            @retry="refresh"
            @dismiss="staleDismissed = true"
          />
        </div>

        <div
          v-if="notice"
          class="px-4 py-3 flex items-start gap-2"
          :data-coverage="notice.coverage"
          data-testid="guardrail-events-notice"
        >
          <BaseBadge :variant="notice.variant" size="sm" dot>{{ coverageLabel }}</BaseBadge>
          <p class="text-[12.5px] text-gray-700 dark:text-gray-300">{{ notice.text }}</p>
        </div>

        <template v-if="store.events.length">
          <p class="px-4 pt-3 pb-1 text-[11px] font-mono uppercase tracking-wide text-gray-600 dark:text-gray-400 tabular-nums" data-testid="guardrail-events-total">
            {{ totalText }}
          </p>
          <ul
            class="max-h-72 overflow-y-auto divide-y divide-gray-200 dark:divide-gray-750 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:focus-visible:ring-action-primary-400/40"
            tabindex="0"
            aria-label="Recent guardrail events"
          >
            <li
              v-for="event in store.events"
              :key="event.event_id"
              class="px-4 py-2.5 flex items-start gap-3"
              data-testid="guardrail-event-row"
            >
              <BaseBadge :variant="kindVariant(event.kind)" size="sm" class="mt-0.5 shrink-0">{{ kindLabel(event.kind) }}</BaseBadge>
              <div class="min-w-0 flex-1">
                <p class="font-mono text-[12.5px] text-gray-900 dark:text-gray-100 truncate" :title="sourceText(event)">{{ sourceText(event) }}</p>
                <p v-if="detailText(event)" class="text-[12.5px] text-gray-600 dark:text-gray-400 truncate" :title="detailText(event)">{{ detailText(event) }}</p>
              </div>
              <time
                :datetime="event.occurred_at"
                :title="eventTimes(event.occurred_at).absolute"
                class="shrink-0 text-[12.5px] tabular-nums text-gray-600 dark:text-gray-400 whitespace-nowrap"
              >{{ eventTimes(event.occurred_at).relative }}</time>
            </li>
          </ul>
        </template>

        <p
          v-else-if="coverage === 'reporting'"
          class="px-4 py-6 text-center text-[12.5px] text-gray-600 dark:text-gray-400"
          data-testid="guardrail-events-empty"
        >{{ NO_EVENTS_COPY }}</p>
      </template>
    </div>
  </SettingsSection>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useGuardrailEventsStore } from '../../stores/guardrailEvents'
import { viewState, staleBannerMessage } from '../../utils/loadingState'
import {
  COVERAGE_LABELS,
  NO_EVENTS_COPY,
  agentCoverage,
  coverageNotice,
  eventTimes,
  kindLabel,
  kindVariant,
  sourceText,
} from '../../utils/guardrailEvents'
import SettingsSection from './SettingsSection.vue'
import BaseBadge from '../base/BaseBadge.vue'
import LoadFailed from '../LoadFailed.vue'
import InlineError from '../InlineError.vue'

const props = defineProps({
  agentName: { type: String, required: true },
  // The agent's runtime as the page knows it (`agent.runtime`); the forwarder's
  // last report stands in when the page has none.
  runtime: { type: String, default: '' },
})

const store = useGuardrailEventsStore()
const staleDismissed = ref(false)
const lastLoadedAt = ref(null)

const view = computed(() => viewState({
  hasLoaded: store.agentHasLoaded && store.agentName === props.agentName,
  error: store.agentError,
  count: store.events.length,
}))

const coverage = computed(() => agentCoverage({ runtime: props.runtime || null, reporting: store.reporting }))
const notice = computed(() => coverageNotice(coverage.value, store.reporting))
const coverageLabel = computed(() => COVERAGE_LABELS[coverage.value] || '')
const staleMessage = computed(() => staleBannerMessage('the guardrail events', lastLoadedAt.value))

const totalText = computed(() => {
  const n = store.events.length
  if (store.nextCursor) return `Latest ${n} shown`
  return `${n} event${n === 1 ? '' : 's'}`
})

function detailText(event) {
  const d = event?.details || {}
  const parts = [event?.tool]
  if (d.path) parts.push(d.path)
  else if (d.rule) parts.push(d.rule)
  if (Array.isArray(d.patterns) && d.patterns.length) parts.push(d.patterns.join(', '))
  return parts.filter(Boolean).join(' · ')
}

async function refresh() {
  staleDismissed.value = false
  const ok = await store.loadAgent(props.agentName)
  if (ok) lastLoadedAt.value = new Date()
  return ok
}

onMounted(refresh)
watch(() => props.agentName, () => {
  lastLoadedAt.value = null
  refresh()
})

defineExpose({ refresh })
</script>
