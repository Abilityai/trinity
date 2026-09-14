<template>
  <div class="flex flex-wrap items-center gap-4">
    <svg viewBox="0 0 100 100" class="h-36 w-36 shrink-0" role="img" :aria-label="ariaLabel">
      <!-- A single full slice is a circle, not an arc that starts and ends at
           the same point (which SVG draws as nothing). -->
      <circle
        v-if="slices.length === 1"
        cx="50" cy="50" :r="outer"
        :fill="slices[0].color"
      />
      <path
        v-for="(s, i) in slices.length === 1 ? [] : slices"
        :key="i"
        :d="arcPath(s.startAngle, s.endAngle)"
        :fill="s.color"
      >
        <title>{{ s.label }}: {{ valueText(s) }}</title>
      </path>
      <!-- Donut hole in the surface colour, both themes. -->
      <circle v-if="donut" cx="50" cy="50" :r="inner" class="fill-white dark:fill-gray-900" />
    </svg>

    <!-- The legend keeps a readable minimum and wraps UNDER the pie in a
         narrow figure cell, rather than shrinking beside it until every
         label is an ellipsis (#2583). -->
    <ul class="min-w-[9rem] flex-1 space-y-1 text-xs">
      <li v-for="(s, i) in slices" :key="i" class="flex items-center gap-2">
        <span class="inline-block h-2.5 w-2.5 shrink-0 rounded-sm" :style="{ backgroundColor: s.color }"></span>
        <span class="truncate text-gray-700 dark:text-gray-200">{{ s.label }}</span>
        <span class="ml-auto font-mono tabular-nums text-gray-500 dark:text-gray-400">
          {{ valueText(s) }} · {{ Math.round(s.fraction * 100) }}%
        </span>
      </li>
    </ul>
  </div>
</template>

<script setup>
/**
 * Pie / donut for canvas `chart` blocks (ent#536). Pure SVG — uPlot has no
 * pie, and a second charting engine for one shape is not worth its weight.
 * Slice geometry comes from `canvasUtils.pieSlices` so it is testable without
 * a DOM; this file only turns angles into arc paths.
 */
import { computed } from 'vue'

const props = defineProps({
  // [{label, value, color, unit, fraction, startAngle, endAngle}]
  slices: { type: Array, required: true },
  donut: { type: Boolean, default: false },
})

const outer = 48
const inner = 28

function point(angleDeg) {
  const rad = ((angleDeg - 90) * Math.PI) / 180
  return [50 + outer * Math.cos(rad), 50 + outer * Math.sin(rad)]
}

function arcPath(start, end) {
  const [sx, sy] = point(start)
  const [ex, ey] = point(end)
  const large = end - start > 180 ? 1 : 0
  return `M50 50 L${sx.toFixed(3)} ${sy.toFixed(3)} A${outer} ${outer} 0 ${large} 1 ${ex.toFixed(3)} ${ey.toFixed(3)} Z`
}

function valueText(s) {
  return `${Number(s.value).toLocaleString()}${s.unit ? ` ${s.unit}` : ''}`
}

const ariaLabel = computed(() =>
  props.slices.map((s) => `${s.label} ${Math.round(s.fraction * 100)}%`).join(', '),
)
</script>
