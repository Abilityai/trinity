<template>
  <!-- ent#523: the band under the conversation header — the agent's numbers,
       ALWAYS visible (operator, 2026-09-06: "I don't see on the top panel for
       the agent the details we have on the Workspace right now, like the chart
       with the executions"). It is the half of the old agent page that answers
       "how is this agent doing"; the half that answers "what has it got"
       moved to the Agent-details panel.

       Deliberately not collapsible. A control that hides the one thing the
       operator asked to always see would re-create the state they objected
       to, one click deeper. -->
  <div
    class="shrink-0 border-b border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 px-3 sm:px-4 py-2.5"
    data-testid="portal-agent-band"
  >
    <div class="flex flex-wrap items-center gap-x-6 gap-y-2">
      <!-- #2540/#1927: gated on the VERDICT (`loaded`), never on a request
           being open, so a window change or a background refresh leaves the
           numbers on screen instead of flashing back to placeholders. -->
      <template v-if="!loaded">
        <div class="flex items-center gap-6" aria-busy="true">
          <div v-for="i in 3" :key="i" class="animate-pulse">
            <div class="h-5 w-10 rounded bg-gray-200 dark:bg-gray-800"></div>
            <div class="mt-1 h-2.5 w-16 rounded bg-gray-100 dark:bg-gray-800/60"></div>
          </div>
        </div>
        <span class="sr-only">Loading this agent's activity…</span>
      </template>

      <template v-else>
        <div>
          <div class="text-lg font-semibold tabular-nums leading-tight">{{ stats.total_executions }}</div>
          <div class="text-[11px] text-gray-500 dark:text-gray-400">tasks · last {{ windowLabel }}</div>
        </div>
        <div>
          <div class="text-lg font-semibold tabular-nums leading-tight">{{ pct(stats.success_rate) }}</div>
          <div class="text-[11px] text-gray-500 dark:text-gray-400">completed</div>
        </div>
        <div>
          <div class="text-lg font-semibold tabular-nums leading-tight">{{ pct(stats.first_try?.rate) }}</div>
          <div class="text-[11px] text-gray-500 dark:text-gray-400" title="Succeeded without needing a retry">first try</div>
        </div>
        <!-- ent#366: a RAW TALLY, never a percentage — one thumbs-down out of
             one rating renders as "100% negative", a number that looks like
             evidence and is not. Carried over from the agent page unchanged. -->
        <div v-if="ratings.total || ratings.unavailable">
          <div class="text-lg font-semibold tabular-nums leading-tight">
            <span class="text-status-success-600 dark:text-status-success-400">{{ ratings.up }}</span>
            <span class="text-gray-300 dark:text-gray-600"> / </span>
            <span class="text-status-warning-600 dark:text-status-warning-400">{{ ratings.down }}</span>
          </div>
          <div class="text-[11px] text-gray-500 dark:text-gray-400">{{ ratingsCaption }}</div>
        </div>
      </template>

      <!-- The chart. #2540: the scanline beam is the CHART-loading motion and
           this is the only place on this page entitled to it — every other
           first load here and in the conversation is a skeleton. -->
      <div class="flex-1 min-w-[12rem] max-w-full">
        <ScanlineReveal :loading="!loaded">
          <div class="h-[52px] flex items-center">
            <p v-if="stats.unavailable" class="text-xs text-gray-400">Stats are unavailable right now.</p>
            <p v-else-if="!hasActivity" class="text-xs text-gray-400">No activity in the last {{ windowLabel }}.</p>
            <StackedBarChart
              v-else
              class="w-full"
              :data="stats.timeline || []"
              :buckets="chartBuckets"
              :colors="BUCKET_COLORS"
              :labels="PORTAL_BUCKET_LABELS"
              :height="52"
            />
          </div>
        </ScanlineReveal>
      </div>

      <select
        v-model="timeWindow"
        class="shrink-0 text-xs rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-2 py-1"
        aria-label="Time window"
      >
        <option value="7d">7 days</option>
        <option value="14d">14 days</option>
        <option value="30d">30 days</option>
      </select>

      <button
        type="button"
        class="shrink-0 text-xs font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline"
        data-testid="portal-open-agent-details"
        @click="$emit('open-details')"
      >Agent details</button>
    </div>

    <!-- ent#253: a failed REFRESH keeps the data and says so beside it; it does
         not replace a band that is already reading correctly. -->
    <InlineError v-if="error && loaded" class="mt-2" :message="error" retryable @retry="reload" />
  </div>
</template>

<script setup>
/**
 * The agent's numbers, above its conversation (ent#523).
 *
 * Reads `usePortalAgentPage`, the shared payload the Agent-details panel also
 * reads — one fetch, two surfaces, because the two are on screen at different
 * times and each issuing its own would double every page load.
 */
import { ref, computed, toRef } from 'vue'
import StackedBarChart from '@/components/StackedBarChart.vue'
import ScanlineReveal from '@/components/ScanlineReveal.vue'
import InlineError from '@/components/InlineError.vue'
import { BUCKET_COLORS, bucketsForChart, hasChartActivity } from '@/utils/executionBuckets'
import { PORTAL_BUCKET_LABELS } from './portalUtils'
import { usePortalAgentPage } from '@/composables/usePortalAgentPage'

const props = defineProps({
  agentName: { type: String, required: true },
})
defineEmits(['open-details'])

const timeWindow = ref('7d')
const { stats, ratings, loaded, error, reload } = usePortalAgentPage(
  toRef(props, 'agentName'), timeWindow,
)

const windowLabel = computed(() => ({ '7d': '7 days', '14d': '14 days', '30d': '30 days' }[timeWindow.value]))
const chartBuckets = computed(() => bucketsForChart(stats.value))
const hasActivity = computed(() => hasChartActivity(stats.value))
const ratingsCaption = computed(() => (
  ratings.value.unavailable ? 'ratings unavailable' : 'helpful / not helpful'
))

// A rate that has never been measured is `—`, not `0%`: zero is a claim about
// performance, and no runs is a claim about nothing.
function pct(v) {
  return typeof v === 'number' && Number.isFinite(v) ? `${Math.round(v)}%` : '—'
}
</script>
