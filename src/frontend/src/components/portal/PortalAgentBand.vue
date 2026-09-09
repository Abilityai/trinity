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
  <!-- ent#547: COMPACT. `py-2` (8px) rather than `py-2.5`, because the band's
       height is now the stats strip's and nothing else's — see the block
       comment on the chart below. Measured in Chromium at 1440px: 99px → 56px,
       against the issue's ≤60% target. -->
  <div
    class="shrink-0 border-b border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 px-3 sm:px-4 py-2"
    data-testid="portal-agent-band"
  >
    <div class="flex flex-wrap items-center gap-x-6 gap-y-2">
      <!-- #2597: a failed FIRST load is the first ARM of this chain, not a
           banner above it and not a replacement for the whole row.

           An arm, because the `v-else` branch renders `stats`, which falls back
           to `{ total_executions: 0 }` — zeros are a claim about performance
           where "no data" is a claim about nothing, so anything that let them
           render here would be the very defect this fixes.

           Inside the row, because the window `<select>` below is a sibling of
           this chain rather than part of it: replacing the whole row took the
           selector away with the numbers, and changing the window is a second
           way out of a failure (`watch(timeWindow)` re-fetches) alongside the
           retry. `InlineError` is row-shaped, so it costs no layout — the same
           is NOT true of `LoadFailed`, whose centred `py-6` column in a
           `py-2.5` band would roughly triple its height and shift the
           conversation column (contract principle 4). It is also the primitive
           the failed-REFRESH case at the bottom of this file already uses, so
           the band has one failure language rather than two. -->
      <InlineError
        v-if="error && !loaded"
        :message="error"
        retryable
        @retry="reload"
      />

      <!-- #2540/#1927: gated on the VERDICT (`loaded`), never on a request
           being open, so a window change or a background refresh leaves the
           numbers on screen instead of flashing back to placeholders. -->
      <template v-else-if="!loaded">
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
          <div class="text-[11px] text-gray-500 dark:text-gray-400">tasks · last {{ WINDOW_LABEL }}</div>
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
      <!-- Bounded, not flexed to fill. Stretched across the whole band a 7-day
           window gives ~150px-wide columns, so a single execution renders as a
           slab rather than a bar — board A3's chart is a compact block beside
           the figures, not a full-width plot. -->
      <!-- ent#547 — THE HEIGHT RULE, and the thing to preserve if you edit this
           block. A stat block above is 39px (an 18px figure over an 11px
           caption), so with the band's 8px padding the floor is 55px + 1px
           border WHATEVER the chart does. This column is therefore budgeted at
           exactly 39px — title 10 + `mb-1.5` 6 + a 23px row — which makes the
           chart free: it can never lengthen the band, and shrinking it further
           would buy nothing. Go over 39 and every pixel is one the band grows.
           The `min-h` matches the bar height rather than the old 52px so the
           chart, its scanline, "no activity" and "unavailable" all share ONE
           footprint (contract principle 4) instead of the row resizing per
           state.
           What used to make this column tall was the LEGEND, not the bars: laid
           out `flex-col` it grew ~13px per bucket, so a nine-bucket agent's band
           ran to ~153px. `legend="none"` is most of the compaction; the tooltip
           carries series identity, and it names every bucket with its swatch,
           its count and the day's total. -->
      <div class="w-[26rem] max-w-[45%] shrink-0">
        <!-- Board A3 names the chart rather than leaving a bare plot beside a
             row of numbers — without it the bars read as another statistic. -->
        <div class="text-[10px] font-semibold uppercase tracking-wide text-gray-400 leading-none mb-1.5">
          Activity · last {{ WINDOW_LABEL }}
        </div>
        <ScanlineReveal :loading="!loaded">
          <div class="min-h-[23px] flex items-center">
            <p v-if="stats.unavailable" class="text-xs text-gray-400">Stats are unavailable right now.</p>
            <p v-else-if="!hasActivity" class="text-xs text-gray-400">No activity in the last {{ WINDOW_LABEL }}.</p>
            <!-- `axis="false"`: at 7 columns the day labels are four truncated
                 dates, and the tooltip gives each bar's full date. Dropping them
                 is what buys the title its 16px inside the 39px budget — the
                 chart can afford one of the two, and the title is the one board
                 A3 argued for ("without it the bars read as another
                 statistic"). -->
            <StackedBarChart
              v-else
              class="w-full"
              :data="stats.timeline || []"
              :buckets="chartBuckets"
              :colors="BUCKET_COLORS"
              :labels="PORTAL_BUCKET_LABELS"
              :height="23"
              legend="none"
              :axis="false"
            />
          </div>
        </ScanlineReveal>
      </div>

      <!-- ent#547: the 7d/14d/30d selector is gone and the window is fixed at 7
           days. The spacer that pushed it to the right went with it — it existed
           only to hold that control against the edge, and left behind it would
           be an invisible flex child nobody could account for. `PortalAgentDetails`
           already pinned '7d' for the same reason this now does: two controls
           for one fact is how the band and the panel came to disagree. -->
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

// ent#547: FIXED at 7 days — the selector is gone. Still a `ref`, because
// `usePortalAgentPage` takes the window as a reactive source and keys its cache
// on it; a constant ref simply never changes, which also means the band and
// `PortalAgentDetails` (which has always pinned '7d') now share one cache entry
// instead of paying for two windows of the same agent.
const timeWindow = ref('7d')
const WINDOW_LABEL = '7 days'
const { stats, ratings, loaded, error, reload } = usePortalAgentPage(
  toRef(props, 'agentName'), timeWindow,
)
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
