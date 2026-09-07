<template>
  <div>
    <!-- line / area: the shared trend chart, with a label formatter so a
         category axis is not forced through the UTC-day formatter. -->
    <TrendLineChart
      v-if="model.type === 'line' || model.type === 'area'"
      :dates="trend.dates"
      :series="trend.series"
      :label-format="trend.labelFormat"
      :value-format="valueFormat"
    />

    <!-- bar / stacked bar: the shared execution chart. A single-point-per-series
         payload renders one bar per series (categories are series, as they are
         dims in the metric store); anything else stacks the series per label. -->
    <StackedBarChart
      v-else-if="model.type === 'bar' || model.type === 'stacked_bar'"
      :data="bars.data"
      :buckets="bars.buckets"
      :colors="bars.colors"
      :labels="bars.labels"
      :label-format="bars.labelFormat"
    />

    <CanvasPieChart
      v-else-if="(model.type === 'pie' || model.type === 'donut') && slices"
      :slices="slices"
      :donut="model.type === 'donut'"
    />

    <!-- A pie whose values are all zero is not a chart; say so rather than
         drawing an empty ring. -->
    <p v-else class="text-xs text-gray-500 dark:text-gray-400">No positive values to chart.</p>

    <!-- Freshness travels with the series (#479's shape): the point time is
         always shown when known, and a stale mark is an ADDITION to it. -->
    <p
      v-if="model.asOf || model.stale"
      class="mt-1.5 flex flex-wrap items-center gap-2 text-[11px] text-gray-500 dark:text-gray-400"
    >
      <span v-if="model.asOf" :title="model.asOf">as of {{ relativeTime(model.asOf) }}</span>
      <span
        v-if="model.stale"
        class="rounded-full bg-status-warning-100 px-2 py-0.5 font-medium text-status-warning-700 dark:bg-status-warning-500/16 dark:text-status-warning-300"
      >metric may be stale</span>
    </p>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import TrendLineChart from '../TrendLineChart.vue'
import StackedBarChart from '../StackedBarChart.vue'
import CanvasPieChart from './CanvasPieChart.vue'
import { pieSlices, relativeTime, stackedBarProps, trendChartProps } from './canvasUtils'

const props = defineProps({
  // The normalised model from `canvasUtils.chartModel` — never a raw payload.
  model: { type: Object, required: true },
})

const trend = computed(() => trendChartProps(props.model))
const bars = computed(() => stackedBarProps(props.model))
const slices = computed(() => pieSlices(props.model))

// One unit across the chart when every series agrees on it; else bare numbers.
const unit = computed(() => {
  const units = new Set(props.model.series.map((s) => s.unit).filter(Boolean))
  return units.size === 1 ? [...units][0] : ''
})
const valueFormat = (v) => (v == null ? '—' : `${Number(v).toLocaleString()}${unit.value ? ` ${unit.value}` : ''}`)
</script>
