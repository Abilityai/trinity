<script setup>
/**
 * Executions-per-day stacked-by-type bar chart (#1107).
 *
 * Deliberately CSS/flexbox, NOT uPlot bars: ≤30 days × ≤8 buckets is
 * trivial DOM, and this gives correct-by-construction per-segment
 * tooltips, theme-aware colors, and no cumulative-stacking math (the
 * documented uPlot-bars failure mode). One column per day; segments sized
 * by count / max-day-total; hover shows the per-bucket breakdown.
 */
import { ref, computed } from 'vue'

const props = defineProps({
  // timeline points: [{ date, total, by_type: { bucket: count } }]
  data: { type: Array, required: true },
  // ordered bucket names (stack + legend order)
  buckets: { type: Array, required: true },
  // bucket -> hex color
  colors: { type: Object, required: true },
  height: { type: Number, default: 150 },
})

const hover = ref(null)

const maxTotal = computed(() =>
  Math.max(1, ...props.data.map((d) => d.total || 0))
)

const bucketTotals = computed(() => {
  const t = {}
  for (const b of props.buckets) t[b] = 0
  for (const d of props.data) {
    for (const b of props.buckets) t[b] += (d.by_type?.[b] || 0)
  }
  return t
})

// buckets present in a given day, in stack order (bottom -> top)
function bucketsForDay(d) {
  return props.buckets.filter((b) => d.by_type?.[b])
}

function segHeight(d, b) {
  const n = d.by_type?.[b] || 0
  return (n / maxTotal.value) * props.height
}

// Slate fallback so a bucket missing from the colors map (e.g. a stale
// cached bundle against a newer backend) renders gray, not invisible.
function colorFor(b) {
  return props.colors[b] || '#94a3b8'
}

function fmtDate(iso) {
  const dt = new Date(iso + 'T00:00:00Z')
  return dt.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' })
}
function fmtDayShort(iso) {
  const dt = new Date(iso + 'T00:00:00Z')
  return dt.toLocaleDateString(undefined, { month: 'numeric', day: 'numeric', timeZone: 'UTC' })
}

// Sparse x labels: ~6 evenly spaced ticks regardless of window size.
function showLabel(i) {
  const step = Math.max(1, Math.ceil(props.data.length / 6))
  return i % step === 0
}
</script>

<template>
  <div>
    <!-- bars -->
    <div class="flex items-end gap-px" :style="{ height: height + 'px' }">
      <div
        v-for="(d, i) in data"
        :key="d.date"
        class="relative flex-1 flex flex-col-reverse justify-start items-center min-w-0"
        @mouseenter="hover = i"
        @mouseleave="hover = null"
      >
        <!-- baseline tick for empty days so the axis reads as continuous -->
        <div
          v-if="!d.total"
          class="w-full max-w-[56px] rounded-sm bg-gray-200 dark:bg-gray-700"
          style="height: 2px"
        ></div>
        <!-- cap rounding by index, not :last-child — the hover tooltip is a
             later sibling, so last: would drop the cap's rounding mid-hover -->
        <div
          v-for="(b, bi) in bucketsForDay(d)"
          :key="b"
          class="w-full max-w-[56px]"
          :class="{ 'rounded-t-md': bi === bucketsForDay(d).length - 1 }"
          :style="{
            height: segHeight(d, b) + 'px',
            backgroundColor: colorFor(b),
            boxShadow: 'inset 0 -1px 0 rgba(17,24,39,0.28)',
          }"
        ></div>

        <!-- hover tooltip -->
        <div
          v-if="hover === i && d.total"
          class="absolute bottom-full left-1/2 -translate-x-1/2 mb-1 z-20 w-max max-w-[200px] px-2.5 py-1.5 rounded-md shadow-lg text-[11px] bg-gray-900 text-gray-100 dark:bg-gray-700 pointer-events-none"
        >
          <div class="font-semibold mb-1 whitespace-nowrap">{{ fmtDate(d.date) }}</div>
          <div v-for="b in bucketsForDay(d)" :key="b" class="flex items-center justify-between gap-3 whitespace-nowrap">
            <span class="flex items-center">
              <span class="inline-block w-2 h-2 rounded-sm mr-1.5" :style="{ backgroundColor: colorFor(b) }"></span>{{ b }}
            </span>
            <span class="font-mono">{{ d.by_type[b] }}</span>
          </div>
          <div class="flex items-center justify-between gap-3 mt-1 pt-1 border-t border-gray-700 dark:border-gray-600">
            <span>Total</span><span class="font-mono">{{ d.total }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- x labels (sparse) -->
    <div class="flex gap-px mt-1">
      <div
        v-for="(d, i) in data"
        :key="d.date"
        class="flex-1 text-center text-[9px] text-gray-400 dark:text-gray-500 truncate"
      >
        {{ showLabel(i) ? fmtDayShort(d.date) : '' }}
      </div>
    </div>

    <!-- legend with per-bucket window totals -->
    <div class="flex flex-wrap gap-x-3 gap-y-1 mt-3">
      <span
        v-for="b in buckets"
        :key="b"
        class="inline-flex items-center text-xs text-gray-600 dark:text-gray-300"
      >
        <span class="w-2.5 h-2.5 rounded-sm mr-1" :style="{ backgroundColor: colorFor(b) }"></span>
        {{ b }}
        <span class="ml-1 font-mono text-gray-400 dark:text-gray-500">{{ bucketTotals[b] }}</span>
      </span>
    </div>
  </div>
</template>
