<template>
  <!-- Operations › Outcomes weekly trend (trinity-enterprise#941): the fleet's
       stored weekly records, oldest first — tasks done, done-claim rejections
       and failed checks as rates on one axis. Read from the records the
       weekly pass wrote, never recomputed. -->
  <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-750 rounded-lg" data-testid="outcomes-trend">
    <div class="flex items-center justify-between px-4 py-2 border-b border-gray-200 dark:border-gray-750">
      <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">Week by week</h3>
      <span class="text-xs text-gray-500 dark:text-gray-400 tabular-nums" data-testid="outcomes-trend-weeks">{{ records.length }} weeks · fleet</span>
    </div>
    <div class="px-4 py-3">
      <TrendLineChart
        :dates="dates"
        :series="series"
        :y-min="0"
        :y-max="100"
        :value-format="(v) => (v == null ? '—' : v + '%')"
        :axis-format="(v) => v + '%'"
      />
      <div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-600 dark:text-gray-300" data-testid="outcomes-trend-legend">
        <span v-for="s in series" :key="s.label" class="flex items-center gap-1.5">
          <span class="inline-block w-3 h-0.5 rounded" :style="{ background: s.color }" aria-hidden="true"></span>{{ s.label }}
        </span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import TrendLineChart from '../TrendLineChart.vue'
import { tokenColor } from '../../utils/tokenColor'
import { CANVAS_PALETTE } from '../../utils/canvasPalette'

const props = defineProps({
  /** Fleet records, oldest first (`store.fleetHistory`). */
  records: { type: Array, required: true },
})

// uPlot strokes a canvas, which takes no class: the status tokens' values,
// with the canvas palette where a token cannot be resolved (as EvalTrend).
const STROKE = {
  success: tokenColor('text-status-success-500') ?? CANVAS_PALETTE[1],
  danger: tokenColor('text-status-danger-500') ?? CANVAS_PALETTE[2],
  warning: tokenColor('text-status-warning-500') ?? CANVAS_PALETTE[3],
}

const dates = computed(() => props.records.map((r) => String(r.window_start || '').slice(0, 10)))

const pick = (path) => props.records.map((r) => {
  const v = path.reduce((n, k) => (n && typeof n === 'object' ? n[k] : undefined), r.signals)
  return typeof v === 'number' ? Math.round(v * 1000) / 10 : null
})

const series = computed(() => [
  { label: 'Tasks done', color: STROKE.success, data: pick(['completion', 'rate']) },
  { label: 'Done claims rejected', color: STROKE.danger, data: pick(['done_claims', 'rejection_rate']) },
  { label: 'Checks failed', color: STROKE.warning, data: pick(['runs', 'checks_failed_rate']) },
])
</script>
