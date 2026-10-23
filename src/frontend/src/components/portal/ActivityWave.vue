<script setup>
/**
 * ActivityWave (trinity-enterprise#968) — the Workspace agent band's activity
 * chart as a waveform signature.
 *
 * One row of rounded beats, four per day, mirrored around the centre line.
 * The envelope is a smooth monotone curve through the daily totals; every beat
 * is a clipped pill filled with that day's execution sources stacked bottom-up
 * in bucket order, the seams between them melted by a vertical blur. It reads
 * as the agent's signature rather than a plot, and it looks right whatever the
 * week holds: a quiet week murmurs, one big day blooms, an empty week is a
 * soft dotted line rather than a sentence.
 *
 * All geometry is `utils/activityWave.js` (pure, node-tested); this file binds
 * it, owns hover + tooltip, and owns the chart's loading motion.
 *
 * LOADING MOTION — a sanctioned exception to design-system §6 (operator ruling
 * 2026-10-22, trinity-enterprise#968): this chart owns its loading face instead
 * of riding `ScanlineReveal`. While `loading` ("no data yet", never "fetch in
 * flight") the resting beats breathe as a slow travelling wave while a glint
 * sweeps across them; when the data lands each beat grows out of the centre
 * line left to right with a touch of overshoot. The PHASE rules are still the
 * scanline's (`utils/scanlinePhase.js`): a cache hit mounts straight into
 * `loaded` and never animates — the band is rebuilt on every chat-tab switch
 * with its numbers already in hand — a background refresh never re-enters
 * `loading`, an empty terminal snaps (`reveal=false`) rather than celebrates,
 * and `prefers-reduced-motion` skips both the breathing and the reveal. The
 * one difference is the fallback timer: the reveal is ~1.5s end to end for a
 * week, not 550ms, so a lost `animationend` is forced closed by
 * `revealFallbackMs(beatCount)`, not the scanline's 700ms.
 *
 * HONESTY — the payload is one datum per UTC day. The four beats of a day are
 * texture on that datum, not sub-day samples: the DAY is the unit the person
 * hovers (one full-height hit rect per day, the beats take no pointer events),
 * the tooltip names, and the accessible label counts.
 *
 * RATCHETS — every colour here is bound from script (`BUCKET_COLORS` and the
 * module's constants), never written in the template; SVG ids start with a
 * letter that is not a hex digit, so `url(#…)` never reads as a colour; the
 * `loading` prop drives a phase, and the faces branch on the PHASE, never on a
 * bare loading flag (the #1927 gate ratchet counts `v-if="isLoading"` too).
 */
import { ref, computed, watch, nextTick, onBeforeUnmount } from 'vue'
import {
  WAVE_W, WAVE_H, SOFT, FALLBACK_COLOR, IDLE_COLORS, revealFallbackMs,
  layoutWave, idleBeats, nextWaveId,
} from '@/utils/activityWave'
import {
  PHASE_LOADING, PHASE_REVEALING, PHASE_LOADED,
  initialPhase, onLoadingChange, onRevealEnd,
} from '@/utils/scanlinePhase'
import { placeTooltip } from '@/utils/chartTooltip'

const props = defineProps({
  // timeline points: [{ date, total, by_type: { bucket: count } }]
  data: { type: Array, default: () => [] },
  // ordered bucket names (stack order) — the KEYS into `by_type`, never translated
  buckets: { type: Array, default: () => [] },
  // bucket -> hex colour
  colors: { type: Object, required: true },
  // OPTIONAL bucket -> display label (the #2161 contract: presentation only)
  labels: { type: Object, default: () => ({}) },
  // "No data yet" — never "fetch in flight". Drives the phase, never a v-if.
  loading: { type: Boolean, default: false },
  // False when loading ends on an empty week: snap, no celebratory grow-in.
  reveal: { type: Boolean, default: true },
  // Rendered height in px. The band budgets 23 (ent#547).
  height: { type: Number, default: 23 },
  // Accessible name for the chart; the total is appended.
  label: { type: String, default: 'Activity' },
})

// Per-instance id prefix: two bands coexist during an agent switch, and SVG
// defs are document-global (why it is a module counter: `nextWaveId`).
const uid = nextWaveId()
const gid = (k) => `${uid}-${k}`

const prefersReducedMotion =
  typeof window !== 'undefined' &&
  typeof window.matchMedia === 'function' &&
  !!window.matchMedia('(prefers-reduced-motion: reduce)')?.matches

// ---------------------------------------------------------------- geometry
const wave = computed(() => layoutWave({ data: props.data, buckets: props.buckets, colors: props.colors }))
const idle = computed(() => idleBeats(Array.isArray(props.data) && props.data.length ? { n: props.data.length } : {}))
const total = computed(() => wave.value.days.reduce((a, d) => a + d.total, 0))
const ariaLabel = computed(() => (
  phase.value === PHASE_LOADING
    ? `${props.label}: loading`
    : wave.value.empty ? `${props.label}: no runs` : `${props.label}: ${total.value} runs`
))

// ---------------------------------------------------------------- phase
const phase = ref(initialPhase(props.loading))
let fallbackTimer = null
function clearFallback() {
  if (fallbackTimer) { clearTimeout(fallbackTimer); fallbackTimer = null }
}
function endReveal() {
  clearFallback()
  phase.value = onRevealEnd(phase.value)
}
watch(() => props.loading, (loading) => {
  phase.value = onLoadingChange(phase.value, loading, { reveal: props.reveal, reducedMotion: prefersReducedMotion })
  clearFallback()
  if (phase.value === PHASE_REVEALING) fallbackTimer = setTimeout(endReveal, revealFallbackMs(wave.value.beats.length))
})
const isRevealing = computed(() => phase.value === PHASE_REVEALING)
const lastBeat = computed(() => wave.value.beats.length - 1)

// ---------------------------------------------------------------- hover + tooltip
const hover = ref(null)
const dayEls = []
const tipEl = ref(null)
const tipPos = ref(null) // { top, left, side } once measured; hidden until then
const hoverDay = computed(() => (hover.value == null ? null : wave.value.days[hover.value]))

function positionTooltip() {
  const el = hover.value == null ? null : dayEls[hover.value]
  if (!el || !tipEl.value) return
  const t = tipEl.value.getBoundingClientRect()
  tipPos.value = placeTooltip(
    el.getBoundingClientRect(),
    { width: t.width, height: t.height },
    window.innerWidth,
    window.innerHeight,
  )
}
// While a tooltip is up, follow the anchor through any scroll (capture: an
// inner scroller moves it too) and through a resize.
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
onBeforeUnmount(() => { listen(false); clearFallback() })

// Slate fallback so a bucket missing from the colour map renders gray, not
// invisible — the same rule the geometry applies to the beats.
const colorFor = (b) => props.colors[b] || FALLBACK_COLOR
const labelFor = (b) => props.labels[b] || b
function fmtDate(iso) {
  if (!iso) return ''
  const dt = new Date(iso + 'T00:00:00Z')
  return dt.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' })
}

const glint = IDLE_COLORS
const softDeviation = `0 ${SOFT}`
</script>

<template>
  <div
    class="wave-root text-gray-500"
    :style="{ height: height + 'px' }"
    role="img"
    :aria-label="ariaLabel"
    :aria-busy="phase === PHASE_LOADING ? 'true' : undefined"
    data-activity-wave
    :data-phase="phase"
  >
    <svg
      :viewBox="`0 0 ${WAVE_W} ${WAVE_H}`"
      :height="height"
      width="100%"
      preserveAspectRatio="xMidYMid meet"
      aria-hidden="true"
      focusable="false"
    >
      <!-- LOADING FACE: resting dots breathing as a travelling wave, a glint
           sweeping across. No blur here — dots have no seams, and a filter
           under a continuous transform would re-rasterise every frame. -->
      <g v-if="phase === PHASE_LOADING" class="wave-idle" data-wave-idle>
        <defs>
          <linearGradient :id="gid('glint')" gradientUnits="userSpaceOnUse" x1="0" y1="0" :x2="WAVE_W" y2="0">
            <stop offset="0" :stop-color="glint.base" stop-opacity="0.35"></stop>
            <stop offset="0.5" :stop-color="glint.core" stop-opacity="0.95"></stop>
            <stop offset="1" :stop-color="glint.base" stop-opacity="0.35"></stop>
            <!-- SMIL is outside CSS's reach, so reduced motion gates it here. -->
            <animateTransform
              v-if="!prefersReducedMotion"
              attributeName="gradientTransform"
              type="translate"
              :from="`${-WAVE_W - 104} 0`"
              :to="`${WAVE_W + 104} 0`"
              dur="2.4s"
              repeatCount="indefinite"
            ></animateTransform>
          </linearGradient>
        </defs>
        <rect
          v-for="(b, i) in idle"
          :key="i"
          class="wave-idle-beat"
          :class="{ 'wave-still': prefersReducedMotion }"
          :style="{ '--j': i }"
          :x="b.x" :y="b.y" :width="b.w" :height="b.h" :rx="b.r"
          :fill="`url(#${gid('glint')})`"
        ></rect>
      </g>

      <!-- DATA FACE -->
      <g v-else :class="{ 'wave-reveal': isRevealing, 'wave-empty': wave.empty }" data-wave-data>
        <defs>
          <!-- Explicit region over the whole chart: the default bbox ±10% gives
               a dot-sized beat no headroom for the blur and clips it. -->
          <filter :id="gid('soft')" filterUnits="userSpaceOnUse" x="0" :y="-WAVE_H" :width="WAVE_W" :height="WAVE_H * 3">
            <feGaussianBlur :stdDeviation="softDeviation"></feGaussianBlur>
          </filter>
          <clipPath v-for="(b, i) in wave.beats" :id="gid('b' + i)" :key="'c' + i">
            <path :d="b.clip"></path>
          </clipPath>
        </defs>

        <!-- hover backing band behind the hovered day's four beats -->
        <rect
          v-if="hoverDay"
          :x="hoverDay.x" y="0" :width="hoverDay.w" :height="WAVE_H" rx="2"
          fill="currentColor" fill-opacity="0.12"
          data-wave-band
        ></rect>

        <!-- beats: clip outside, blur inside, so the pill edge stays crisp -->
        <g
          v-for="(b, i) in wave.beats"
          :key="'b' + i"
          class="wave-beat"
          :style="{ '--j': i }"
          :clip-path="`url(#${gid('b' + i)})`"
          data-wave-beat
          @animationend="i === lastBeat && endReveal()"
          @animationcancel="i === lastBeat && endReveal()"
        >
          <g :filter="`url(#${gid('soft')})`">
            <rect v-for="(s, k) in b.segs" :key="k" :x="b.x" :y="s.y" :width="b.w" :height="s.h" :fill="s.color"></rect>
          </g>
        </g>

        <!-- per-day hit areas, full height, on top: the day is the hover unit -->
        <rect
          v-for="d in wave.days"
          :key="'d' + d.i"
          :ref="(el) => (dayEls[d.i] = el)"
          :x="d.x" y="0" :width="d.w" :height="WAVE_H"
          fill="transparent"
          data-wave-day
          @mouseenter="onEnter(d.i)"
          @mouseleave="onLeave"
        ></rect>
      </g>
    </svg>

    <!-- hover tooltip (#3264): in <body>, fixed, placed by positionTooltip().
         Hidden until measured so it never flashes at 0,0. -->
    <Teleport to="body">
      <div
        v-if="hoverDay"
        ref="tipEl"
        data-wave-tooltip
        :data-side="tipPos?.side"
        class="fixed z-50 w-max max-w-[200px] px-2.5 py-1.5 rounded-md shadow-lg text-[11px] bg-gray-900 text-gray-100 dark:bg-gray-700 pointer-events-none"
        :style="tipPos
          ? { top: tipPos.top + 'px', left: tipPos.left + 'px' }
          : { top: '0px', left: '0px', visibility: 'hidden' }"
      >
        <div v-if="hoverDay.date" class="font-semibold mb-1 whitespace-nowrap">{{ fmtDate(hoverDay.date) }}</div>
        <template v-if="hoverDay.total">
          <div v-for="s in hoverDay.segments" :key="s.bucket" class="flex items-center justify-between gap-3 whitespace-nowrap">
            <span class="flex items-center">
              <span class="inline-block w-2 h-2 rounded-sm mr-1.5" :style="{ backgroundColor: colorFor(s.bucket) }"></span>{{ labelFor(s.bucket) }}
            </span>
            <span class="font-mono">{{ s.n }}</span>
          </div>
          <div class="flex items-center justify-between gap-3 mt-1 pt-1 border-t border-gray-700 dark:border-gray-600">
            <span>Total</span><span class="font-mono">{{ hoverDay.total }}</span>
          </div>
        </template>
        <div v-else class="text-gray-300 whitespace-nowrap">No runs</div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.wave-root { display: block; overflow: hidden; line-height: 0; }
.wave-root svg { display: block; }

/* loading: a slow travelling breath across the resting dots */
@keyframes wave-breathe {
  0%, 100% { transform: scaleY(1); }
  50% { transform: scaleY(2.8); }
}
.wave-idle-beat {
  transform-box: fill-box;
  transform-origin: center;
  animation: wave-breathe 1.9s cubic-bezier(0.45, 0, 0.55, 1) infinite;
  animation-delay: calc(var(--j) * -85ms);
}

/* reveal: each beat grows out of the centre line, left to right, slight overshoot */
@keyframes wave-in {
  0% { transform: scaleY(0.06); opacity: 0; }
  60% { opacity: 1; }
  100% { transform: scaleY(1); opacity: 1; }
}
.wave-reveal .wave-beat {
  transform-box: fill-box;
  transform-origin: center;
  animation: wave-in 750ms cubic-bezier(0.22, 1.2, 0.36, 1) both;
  animation-delay: calc(var(--j) * 24ms);
}

.wave-beat { pointer-events: none; }
.wave-empty { opacity: 0.45; }

.wave-still { animation: none; }
@media (prefers-reduced-motion: reduce) {
  .wave-idle-beat, .wave-reveal .wave-beat { animation: none; }
}
</style>
