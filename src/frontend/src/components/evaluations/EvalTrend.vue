<template>
  <!--
    Operations › Evaluations › trend (trinity-enterprise#938 C6). The stored
    daily `eval.checks_passed_rate` series the platform records (ent#937
    scorecard), read through the trend route — never recomputed here. One
    slot per UTC day of the window: a day with no point is a gap, not 0 %.
  -->
  <BaseCard data-testid="eval-trend">
    <div class="flex items-baseline justify-between mb-3">
      <h3 class="text-sm font-semibold text-gray-900 dark:text-white">Checks passed per day</h3>
      <span v-if="agent" class="text-xs text-gray-500 dark:text-gray-400" data-testid="eval-trend-scope">{{ agent }}</span>
    </div>

    <div class="min-h-[10rem]">
      <div v-if="!agent" class="py-10 text-center" data-testid="eval-trend-pick">
        <p class="text-sm font-medium text-gray-700 dark:text-gray-300">Pick an agent to see its trend</p>
        <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">Click a row in the table above.</p>
      </div>

      <LoadFailed
        v-else-if="error && !loaded"
        title="Couldn't load the trend"
        :detail="error"
        :retrying="loading"
        data-testid="eval-trend-failed"
        @retry="emit('retry')"
      />

      <ScanlineReveal v-else :loading="loading && !loaded" :reveal="chart.points > 0">
        <template v-if="loaded">
          <div v-if="!chart.points" class="py-10 text-center" data-testid="eval-trend-empty">
            <p class="text-sm font-medium text-gray-700 dark:text-gray-300">No graded days recorded for {{ agent }} in this period</p>
            <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">A day appears once one of its runs has a check verdict.</p>
          </div>
          <div v-else data-testid="eval-trend-chart">
            <TrendLineChart
              :dates="chart.dates"
              :series="series"
              :y-min="0"
              :y-max="100"
              :value-format="(v) => (v == null ? '—' : v + '%')"
              :axis-format="(v) => v + '%'"
            />
          </div>
        </template>
        <div v-else class="h-40" aria-hidden="true"></div>
      </ScanlineReveal>
    </div>
  </BaseCard>
</template>

<script setup>
import { computed } from 'vue'
import BaseCard from '../base/BaseCard.vue'
import LoadFailed from '../LoadFailed.vue'
import ScanlineReveal from '../ScanlineReveal.vue'
import TrendLineChart from '../TrendLineChart.vue'

const props = defineProps({
  agent: { type: String, default: '' },
  // { dates: string[], values: (number|null)[], points: number } — store.trendChart
  chart: { type: Object, required: true },
  loading: { type: Boolean, default: false },
  loaded: { type: Boolean, default: false },
  error: { type: String, default: null },
})
const emit = defineEmits(['retry'])

// uPlot strokes a canvas, which takes no CSS variable: the status-success-500
// token's value, as OverviewPanel's completion chart uses it.
const SUCCESS_STROKE = '#22c55e'

const series = computed(() => [{
  label: 'Checks passed',
  color: SUCCESS_STROKE,
  fill: true,
  data: props.chart.values,
}])
</script>
