<template>
  <section class="mb-4 last:mb-0">
    <h4
      v-if="block.title"
      class="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400"
    >{{ block.title }}</h4>

    <!-- chart — the canvas's own kind. TrendLineChart is reused as-is; a
         payload that cannot make a chart falls through to the JSON renderer
         below rather than mounting an empty chart, which would read as
         "no data" — a claim we have not earned. -->
    <TrendLineChart
      v-if="block.kind === 'chart' && chart"
      :dates="chart.labels"
      :series="chart.series"
    />

    <!-- html — the kind the voice panel writes (ent#438 FR-7). Sanitised
         through the shared DOMPurify path, never raw: this is agent-authored
         markup and, on a `roster` canvas, it reaches a customer's browser. -->
    <div
      v-else-if="block.kind === 'html'"
      class="prose prose-sm dark:prose-invert max-w-none"
      v-html="safeHtml"
    ></div>

    <!-- Everything else delegates to the SHARED report dispatch — reused, not
         forked, because those renderer keys are CI-pinned as the canonical
         contract (test_1535_report_prompt_guidance.py). -->
    <ReportRenderer
      v-else
      :display-hint="delegatedHint"
      :payload="block.payload"
    />
  </section>
</template>

<script setup>
import { computed } from 'vue'
import ReportRenderer from '../reports/ReportRenderer.vue'
import TrendLineChart from '../TrendLineChart.vue'
import { sanitizeHtml } from '../../utils/markdown'
import { chartSeries, REPORT_DELEGATED_KINDS } from './canvasUtils'

const props = defineProps({
  block: { type: Object, required: true },
})

const chart = computed(() => chartSeries(props.block?.payload))

const safeHtml = computed(() => sanitizeHtml(props.block?.payload?.html || ''))

// A `chart` whose payload could not make one, and any kind the report dispatch
// does not know, both land on `json` — the reader still sees the data.
const delegatedHint = computed(() =>
  REPORT_DELEGATED_KINDS.includes(props.block?.kind) ? props.block.kind : 'json',
)
</script>
