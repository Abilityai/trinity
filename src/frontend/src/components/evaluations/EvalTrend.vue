<template>
  <!--
    Operations › Evaluations › trend (trinity-enterprise#938 C6). The stored
    daily `eval.checks_passed_rate` series the platform records (ent#937
    scorecard), read through the trend route — never recomputed here. One
    slot per UTC day of the window: a day with no point is a gap, not 0 %.
    With no agent selected it plots the fleet series (C7), which the nightly
    pass writes, so it runs through yesterday; a reader the API does not hand
    the fleet series to gets no points and the "pick an agent" state.
  -->
  <BaseCard data-testid="eval-trend">
    <div class="flex items-baseline justify-between mb-3">
      <h3 class="text-sm font-semibold text-gray-900 dark:text-white">Checks passed per day</h3>
      <span v-if="agent || fleetShown" class="text-xs text-gray-500 dark:text-gray-400" data-testid="eval-trend-scope">{{ agent || 'Fleet' }}</span>
    </div>

    <div class="min-h-[10rem]">
      <!-- The API answers [] both to a reader it does not hand the fleet
           series and to an admin before the first nightly pass (review I2). -->
      <div v-if="!agent && loaded && !chart.points" class="py-10 text-center" data-testid="eval-trend-pick">
        <template v-if="fleetReader">
          <p class="text-sm font-medium text-gray-700 dark:text-gray-300">No fleet points yet</p>
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            The fleet line is written nightly for each completed day — the first nightly pass is pending, or nothing in this period was graded. Pick an agent in the table above to see its trend now.
          </p>
        </template>
        <template v-else>
          <p class="text-sm font-medium text-gray-700 dark:text-gray-300">The fleet series is shown to admins</p>
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">Pick an agent in the table above to see its trend.</p>
        </template>
        <p v-if="chart.clipped" class="mt-1 text-xs text-gray-500 dark:text-gray-400">The trend keeps the last 92 days; this period starts before that.</p>
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
          <div v-if="!chart.points && agent" class="py-10 text-center" data-testid="eval-trend-empty">
            <p class="text-sm font-medium text-gray-700 dark:text-gray-300">No graded days recorded for {{ agent }} in this period</p>
            <p v-if="chart.clipped" class="mt-1 text-xs text-gray-500 dark:text-gray-400">The trend keeps the last 92 days; this period starts before that.</p>
            <p v-else class="mt-1 text-xs text-gray-500 dark:text-gray-400">A day appears once one of its runs has a check verdict.</p>
          </div>
          <div v-else-if="chart.points" data-testid="eval-trend-chart">
            <TrendLineChart
              :dates="chart.dates"
              :series="series"
              :y-min="0"
              :y-max="100"
              :value-format="(v) => (v == null ? '—' : v + '%')"
              :axis-format="(v) => v + '%'"
            />
            <p v-if="fleetShown || chart.startsOn || chart.clipped" class="mt-2 text-[11px] text-gray-500 dark:text-gray-400" data-testid="eval-trend-note">
              <template v-if="fleetShown">Fleet series, recorded nightly — runs through yesterday.</template>
              <template v-if="chart.startsOn"> Series starts {{ chart.startsOn }}.</template>
              <template v-if="chart.clipped"> The trend keeps the last 92 days.</template>
            </p>
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
import { tokenColor } from '../../utils/tokenColor'
import { CANVAS_PALETTE } from '../../utils/canvasPalette'

const props = defineProps({
  agent: { type: String, default: '' },
  // { dates: string[], values: (number|null)[], points: number } — store.trendChart
  chart: { type: Object, required: true },
  loading: { type: Boolean, default: false },
  loaded: { type: Boolean, default: false },
  error: { type: String, default: null },
  // Whether the API hands this reader the fleet series (an admin): decides
  // the empty state's copy only — the API decides what is returned.
  fleetReader: { type: Boolean, default: false },
})
const emit = defineEmits(['retry'])

// uPlot strokes a canvas, which takes no class: the status-success-500
// token's value, resolved from the token itself (review I4). Where nothing is
// computed, the canvas palette's status-success entry — an undefined stroke
// draws nothing, which would read as "no data".
const SUCCESS_STROKE = tokenColor('text-status-success-500') ?? CANVAS_PALETTE[1]

// The fleet line is on screen: no agent picked and the API handed us points.
const fleetShown = computed(() => !props.agent && props.loaded && props.chart.points > 0)

const series = computed(() => [{
  label: 'Checks passed',
  color: SUCCESS_STROKE,
  fill: true,
  data: props.chart.values,
}])
</script>
