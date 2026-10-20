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
import { ref, computed, nextTick, onBeforeUnmount } from 'vue'

const props = defineProps({
  // timeline points: [{ date, total, by_type: { bucket: count } }]
  data: { type: Array, required: true },
  // ordered bucket names (stack + legend order)
  buckets: { type: Array, required: true },
  // bucket -> hex color
  colors: { type: Object, required: true },
  // OPTIONAL bucket -> display label (#2161). Presentation only: entries in
  // `buckets` are the KEYS used to index `by_type`, so a caller wanting
  // client-facing wording must translate here and never in the array itself —
  // a renamed bucket would look up `by_type['Tool call']`, find nothing, and
  // render an empty chart. Unmapped buckets show their own name.
  labels: { type: Object, default: () => ({}) },
  height: { type: Number, default: 150 },
  // ent#523: where the legend sits. 'below' (default) is every existing
  // caller's layout, unchanged. 'side' puts it in a column BEFORE the bars —
  // what board A3 draws for the Workspace band, where the chart shares one
  // short row with the stat figures and a legend underneath would double the
  // band's height.
  //
  // ent#547 adds 'none': no legend at all, series identity carried by the hover
  // tooltip, which already names every bucket with its swatch and count. Read
  // the two `v-if`s below as a pair — the second was `legend !== 'side'`, so a
  // third value would have rendered the BELOW legend rather than no legend, and
  // "add a value to an enum, then grep the readers" is exactly the class this
  // repo's ledger keeps recording. Both branches now name their value.
  //
  // Why 'side' is a height problem worth a third value rather than a smaller
  // font: it lays out `flex-col`, one row per bucket, so it grows ~13px per
  // bucket — ~29px at one bucket and ~133px at nine. In the Workspace band that
  // made a busy agent's header nearly twice a quiet one's.
  legend: { type: String, default: 'below' },
  // ent#547: the sparse x-axis day labels under the bars. On by default (every
  // existing caller). The Workspace band turns them off: at 7 columns they are
  // four truncated dates, and the hover tooltip carries each bar's full date
  // already — so they cost height the compact band does not have and say
  // nothing the chart does not.
  axis: { type: Boolean, default: true },
  // OPTIONAL x-label formatter (ent#536). Null = the UTC-day formatters every
  // existing caller relies on (`date` is an ISO day); the canvas passes its own
  // so a category column is not parsed as a date.
  labelFormat: { type: Function, default: null },
})

const hover = ref(null)

// #3264: the tooltip renders in <body> at fixed coordinates taken from the
// hovered bar. As an `absolute bottom-full` child of the bar it always opened
// upward, so on the Workspace band — a 23px chart just under the header, inside
// ScanlineReveal's clip-path — its top rows were covered and the date and top
// buckets unreadable. Out of every parent, nothing can clip or cover it.
const TIP_GAP = 4 // px between bar and tooltip (the old `mb-1`)
const VIEWPORT_MARGIN = 8 // px the tooltip keeps from every viewport edge
const barEls = []
const tipEl = ref(null)
const tipPos = ref(null) // { top, left, side } once measured; hidden until then

const hoverDay = computed(() => (hover.value == null ? null : props.data[hover.value]))

// Above when it fits, else below when that fits, else whichever side has more
// room (clamped). Horizontally centred on the bar, clamped into the viewport.
function placeTooltip(bar, tip, vw, vh) {
  const roomAbove = bar.top - TIP_GAP - VIEWPORT_MARGIN
  const roomBelow = vh - bar.bottom - TIP_GAP - VIEWPORT_MARGIN
  const side = tip.height <= roomAbove || (tip.height > roomBelow && roomAbove >= roomBelow)
    ? 'above'
    : 'below'
  const rawTop = side === 'above' ? bar.top - TIP_GAP - tip.height : bar.bottom + TIP_GAP
  const top = Math.max(VIEWPORT_MARGIN, Math.min(rawTop, vh - VIEWPORT_MARGIN - tip.height))
  const rawLeft = bar.left + bar.width / 2 - tip.width / 2
  const left = Math.max(VIEWPORT_MARGIN, Math.min(rawLeft, vw - VIEWPORT_MARGIN - tip.width))
  return { top, left, side }
}

function positionTooltip() {
  const bar = hover.value == null ? null : barEls[hover.value]
  if (!bar || !tipEl.value) return
  const t = tipEl.value.getBoundingClientRect()
  tipPos.value = placeTooltip(
    bar.getBoundingClientRect(),
    { width: t.width, height: t.height },
    window.innerWidth,
    window.innerHeight,
  )
}

// While a tooltip is up, follow the bar through any scroll (capture: an inner
// scroller moves it too) and through a resize.
function listen(on) {
  const fn = on ? window.addEventListener : window.removeEventListener
  fn('scroll', positionTooltip, true)
  fn('resize', positionTooltip)
}

async function onEnter(i) {
  hover.value = i
  tipPos.value = null
  listen(true)
  await nextTick() // the tooltip must exist before it can be measured
  if (hover.value === i) positionTooltip()
}

function onLeave() {
  hover.value = null
  tipPos.value = null
  listen(false)
}

onBeforeUnmount(() => listen(false))

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

// Display text for a bucket. The bucket name is its own label unless the caller
// supplied a translation — see the `labels` prop.
function labelFor(b) {
  return props.labels[b] || b
}

function fmtDate(iso) {
  if (props.labelFormat) return props.labelFormat(iso)
  const dt = new Date(iso + 'T00:00:00Z')
  return dt.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' })
}
function fmtDayShort(iso) {
  if (props.labelFormat) return props.labelFormat(iso)
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
  <div :class="legend === 'side' ? 'flex items-end gap-3' : ''">
    <!-- legend, when it sits BESIDE the bars (ent#523 / board A3). Same markup
         as the block below; only the position differs, so the two cannot drift
         in what they say. Rendered first so it reads left-to-right. -->
    <div v-if="legend === 'side'" class="shrink-0 flex flex-col gap-0.5 pb-4">
      <span
        v-for="b in buckets"
        :key="`side-${b}`"
        class="inline-flex items-center text-[10px] leading-tight text-gray-600 dark:text-gray-300 whitespace-nowrap"
      >
        <span class="w-2 h-2 rounded-sm mr-1 shrink-0" :style="{ backgroundColor: colorFor(b) }"></span>
        {{ labelFor(b) }}
      </span>
    </div>

    <div :class="legend === 'side' ? 'flex-1 min-w-0' : ''">
    <!-- bars -->
    <div class="flex items-end gap-px" :style="{ height: height + 'px' }">
      <div
        v-for="(d, i) in data"
        :key="i"
        :ref="(el) => (barEls[i] = el)"
        data-chart-bar
        class="relative flex-1 flex flex-col-reverse justify-start items-center min-w-0"
        @mouseenter="onEnter(i)"
        @mouseleave="onLeave"
      >
        <!-- baseline tick for empty days so the axis reads as continuous -->
        <div
          v-if="!d.total"
          class="w-full max-w-[56px] rounded-sm bg-gray-200 dark:bg-gray-700"
          style="height: 2px"
        ></div>
        <!-- cap rounding by index: the topmost segment is the last bucket in
             stack order -->
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

      </div>
    </div>

    <!-- hover tooltip (#3264): in <body>, fixed, placed by positionTooltip().
         Hidden until measured so it never flashes at 0,0. -->
    <Teleport to="body">
      <div
        v-if="hoverDay && hoverDay.total"
        ref="tipEl"
        data-chart-tooltip
        :data-side="tipPos?.side"
        class="fixed z-50 w-max max-w-[200px] px-2.5 py-1.5 rounded-md shadow-lg text-[11px] bg-gray-900 text-gray-100 dark:bg-gray-700 pointer-events-none"
        :style="tipPos
          ? { top: tipPos.top + 'px', left: tipPos.left + 'px' }
          : { top: '0px', left: '0px', visibility: 'hidden' }"
      >
        <div class="font-semibold mb-1 whitespace-nowrap">{{ fmtDate(hoverDay.date) }}</div>
        <div v-for="b in bucketsForDay(hoverDay)" :key="b" class="flex items-center justify-between gap-3 whitespace-nowrap">
          <span class="flex items-center">
            <span class="inline-block w-2 h-2 rounded-sm mr-1.5" :style="{ backgroundColor: colorFor(b) }"></span>{{ labelFor(b) }}
          </span>
          <span class="font-mono">{{ hoverDay.by_type[b] }}</span>
        </div>
        <div class="flex items-center justify-between gap-3 mt-1 pt-1 border-t border-gray-700 dark:border-gray-600">
          <span>Total</span><span class="font-mono">{{ hoverDay.total }}</span>
        </div>
      </div>
    </Teleport>

    <!-- x labels (sparse) -->
    <div v-if="axis" class="flex gap-px mt-1">
      <div
        v-for="(d, i) in data"
        :key="i"
        class="flex-1 text-center text-[9px] text-gray-500 dark:text-gray-400 truncate"
      >
        {{ showLabel(i) ? fmtDayShort(d.date) : '' }}
      </div>
    </div>

    </div>

    <!-- legend with per-bucket window totals. Gated on the VALUE, not on
         `!== 'side'`: with the negated test ent#547's new 'none' would have
         rendered this block, i.e. the one thing it asks to remove. -->
    <div v-if="legend === 'below'" class="flex flex-wrap gap-x-3 gap-y-1 mt-3">
      <span
        v-for="b in buckets"
        :key="b"
        class="inline-flex items-center text-xs text-gray-600 dark:text-gray-300"
      >
        <span class="w-2.5 h-2.5 rounded-sm mr-1" :style="{ backgroundColor: colorFor(b) }"></span>
        {{ labelFor(b) }}
        <span class="ml-1 font-mono text-gray-500 dark:text-gray-400">{{ bucketTotals[b] }}</span>
      </span>
    </div>
  </div>
</template>
