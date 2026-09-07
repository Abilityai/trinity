<script setup>
/**
 * Labeled line/area trend chart (#1107). Built on the existing uPlot dep
 * (same engine as SparklineChart) but with axes, a hover cursor, and a
 * legend enabled — for the Overview success-rate / duration / context
 * trends. One or more series over a shared UTC-day x-axis. `null` points
 * render as gaps (no false zero). Dark-mode aware: axis/grid strokes are
 * re-resolved from the theme and the chart rebuilds on theme toggle.
 */
import { ref, onMounted, onUnmounted, watch, nextTick, toRaw } from 'vue'
import uPlot from 'uplot'
import 'uplot/dist/uPlot.min.css'
import { useThemeStore } from '../stores/theme'

const props = defineProps({
  // x-axis labels (ISO date strings, one per point)
  dates: { type: Array, required: true },
  // [{ label, data: number|null[], color, fill?: bool }]
  series: { type: Array, required: true },
  height: { type: Number, default: 160 },
  yMin: { type: Number, default: null },
  yMax: { type: Number, default: null },
  // formats a raw value for the legend/tooltip (e.g. "92%", "1.4s")
  valueFormat: { type: Function, default: (v) => (v == null ? '—' : String(v)) },
  // formats a y-axis tick
  axisFormat: { type: Function, default: (v) => String(v) },
  // formats an x label (ent#536). Null = the UTC-day formatter every existing
  // caller relies on; the canvas passes its own so a category axis is not
  // forced through a date parser.
  labelFormat: { type: Function, default: null },
})

function fmtLabel(v) {
  return props.labelFormat ? props.labelFormat(v) : fmtDay(v)
}

const themeStore = useThemeStore()
const wrapEl = ref(null)
let chart = null
let ro = null

// Custom cursor tooltip (replaces uPlot's built-in legend, which reflows
// the layout on hover and makes labels jump). Absolutely positioned inside
// the uPlot root → zero layout impact. Dark chip in both themes, matching
// StackedBarChart's tooltip.
let tooltipEl = null
function ensureTooltip() {
  if (!tooltipEl) {
    tooltipEl = document.createElement('div')
    tooltipEl.style.cssText = [
      'position:absolute', 'pointer-events:none', 'z-index:20',
      'display:none', 'white-space:nowrap', 'font-size:11px',
      'padding:6px 9px', 'border-radius:6px', 'background:#111827',
      'color:#f3f4f6', 'box-shadow:0 2px 10px rgba(0,0,0,0.35)',
      'transform:translate(-50%, calc(-100% - 8px))',
    ].join(';')
  }
  return tooltipEl
}

function mountTooltip(u) {
  u.root.style.position = 'relative'
  u.root.style.overflow = 'visible'
  u.root.appendChild(ensureTooltip())
}

function moveTooltip(u) {
  const tip = ensureTooltip()
  const idx = u.cursor.idx
  if (idx == null || u.cursor.left < 0) {
    tip.style.display = 'none'
    return
  }
  // Built with DOM APIs, never string-concatenated markup: series labels and
  // colours are caller data — on the agent canvas they are AGENT-authored and
  // reach a customer's browser — so they must only ever land in textContent
  // and CSSOM properties, where no string can become an element (ent#536).
  tip.replaceChildren()
  const head = document.createElement('div')
  head.style.cssText = 'font-weight:600;margin-bottom:3px'
  head.textContent = props.dates[idx] ? fmtLabel(props.dates[idx]) : ''
  tip.appendChild(head)
  props.series.forEach((s, si) => {
    const v = u.data[si + 1] ? u.data[si + 1][idx] : null
    const row = document.createElement('div')
    row.style.cssText = 'display:flex;align-items:center;gap:8px'
    const swatch = document.createElement('span')
    swatch.style.cssText = 'display:inline-block;width:8px;height:8px;border-radius:2px'
    swatch.style.background = s.color
    const name = document.createElement('span')
    name.textContent = s.label
    const val = document.createElement('span')
    val.style.cssText = 'margin-left:auto;font-variant-numeric:tabular-nums;font-weight:600'
    val.textContent = props.valueFormat(v)
    row.append(swatch, name, val)
    tip.appendChild(row)
  })
  tip.style.display = 'block'
  // Follow the cursor horizontally; pin just above the plot top so the
  // tooltip itself never jumps vertically.
  tip.style.left = (u.over.offsetLeft + u.cursor.left) + 'px'
  tip.style.top = u.over.offsetTop + 'px'
}

function fmtDay(iso) {
  // "2026-06-09" -> "Jun 9"
  const d = new Date(iso + 'T00:00:00Z')
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })
}

function buildOpts(width) {
  const dark = themeStore.isDark
  const axisStroke = dark ? '#9ca3af' : '#6b7280'
  const gridStroke = dark ? 'rgba(75,85,99,0.35)' : 'rgba(209,213,219,0.6)'
  const dates = props.dates

  return {
    width,
    height: props.height,
    padding: [8, 8, 0, 0],
    cursor: { y: false, points: { size: 6 } },
    legend: { show: false },
    hooks: {
      init: [mountTooltip],
      setCursor: [moveTooltip],
    },
    scales: {
      x: { time: false },
      y: {
        range: (u, dataMin, dataMax) => {
          const lo = props.yMin != null ? props.yMin : dataMin
          const hi = props.yMax != null ? props.yMax : dataMax
          return [lo, hi]
        },
      },
    },
    axes: [
      {
        stroke: axisStroke,
        grid: { show: false },
        ticks: { show: false },
        font: '10px sans-serif',
        values: (u, splits) => splits.map((i) => (dates[i] ? fmtLabel(dates[i]) : '')),
      },
      {
        stroke: axisStroke,
        grid: { stroke: gridStroke, width: 1 },
        ticks: { show: false },
        font: '10px sans-serif',
        size: 38,
        values: (u, splits) => splits.map((v) => props.axisFormat(v)),
      },
    ],
    series: [
      { label: '', value: (u, i) => (dates[i] ? fmtLabel(dates[i]) : '') },
      ...props.series.map((s) => ({
        label: s.label,
        stroke: s.color,
        width: 2,
        fill: s.fill ? s.color + '22' : undefined,
        spanGaps: false,
        points: { show: false },
        value: (u, v) => props.valueFormat(v),
      })),
    ],
  }
}

function chartData() {
  const x = props.dates.map((_, i) => i)
  return [x, ...props.series.map((s) => toRaw(s.data))]
}

function render() {
  if (!wrapEl.value) return
  if (chart) {
    chart.destroy()
    chart = null
  }
  wrapEl.value.innerHTML = ''
  const width = wrapEl.value.clientWidth || 320
  chart = new uPlot(buildOpts(width), chartData(), wrapEl.value)
}

onMounted(async () => {
  await nextTick()
  render()
  ro = new ResizeObserver(() => {
    if (chart && wrapEl.value) chart.setSize({ width: wrapEl.value.clientWidth, height: props.height })
  })
  if (wrapEl.value) ro.observe(wrapEl.value)
})

watch(() => [props.dates, props.series], () => render(), { deep: true })
watch(() => themeStore.isDark, () => render())

onUnmounted(() => {
  if (ro) { ro.disconnect(); ro = null }
  if (chart) { chart.destroy(); chart = null }
  if (tooltipEl) { tooltipEl.remove(); tooltipEl = null }
})
</script>

<template>
  <div ref="wrapEl" class="trend-line-chart w-full"></div>
</template>

<style scoped>
.trend-line-chart :deep(.uplot),
.trend-line-chart :deep(.u-wrap) {
  width: 100% !important;
}
/* Legend is disabled (custom cursor tooltip replaces it — see script).
   Nothing to style here; axis strokes are set theme-aware in buildOpts. */
</style>
