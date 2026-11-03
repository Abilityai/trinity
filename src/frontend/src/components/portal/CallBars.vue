<!--
  The Direct call's live mark (trinity-enterprise#1046): four rounded bars in a tinted
  disc, the plainest "a call is on" signal there is. Green bars follow the person's mic
  while it listens, amber bars run a slow wave while the turn is worked on, blue bars
  follow the agent's voice while it speaks, and muted is four flat gray dots.

  Lightweight on purpose: four elements whose height is set once a frame, no canvas and
  no reactivity in the loop. It sits on the theme's own surface, tinted with the status
  tokens. Under `prefers-reduced-motion: reduce` it holds one still frame per state and
  never starts the loop.
-->
<template>
  <div
    class="shrink-0 rounded-full flex items-center justify-center transition-colors duration-300 motion-reduce:transition-none"
    :class="tint.ground"
    :style="{ width: `${size}px`, height: `${size}px`, gap: `${gap}px` }"
    :data-state="shown"
    aria-hidden="true"
    data-testid="call-bars"
  >
    <span
      v-for="i in BARS"
      :key="i"
      ref="bars"
      class="block rounded-full transition-colors duration-300 motion-reduce:transition-none"
      :class="tint.bar"
      :style="{ width: `${barWidth}px`, height: `${floor}px` }"
      data-testid="call-bar"
    />
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  // listening · working · speaking · connecting · idle (portalDirectCall.directBarsState).
  state: { type: String, default: 'idle' },
  // 0–1, the sound right now: the agent's voice while it speaks, the person's while it listens.
  amplitude: { type: Number, default: 0 },
  muted: { type: Boolean, default: false },
  size: { type: Number, default: 48 },
})

const BARS = 4
// Each bar's share of the sound, and its own flicker, so four bars never move as one.
const WEIGHT = [0.75, 1, 0.9, 0.65]
const FLICKER = [5.1, 7.3, 6.2, 8.9]
const PHASE = [0, 1.7, 3.1, 4.4]

// Full class strings, so the build sees every one (design-system contract: semantic
// tokens only; status text tiers do not apply, these are solid marks).
const TINTS = {
  listening: { ground: 'bg-status-success-100 dark:bg-status-success-500/16', bar: 'bg-status-success-600 dark:bg-status-success-400' },
  working: { ground: 'bg-state-autonomous-100 dark:bg-state-autonomous-500/16', bar: 'bg-state-autonomous-600 dark:bg-state-autonomous-400' },
  speaking: { ground: 'bg-status-info-100 dark:bg-status-info-500/16', bar: 'bg-status-info-600 dark:bg-status-info-400' },
  neutral: { ground: 'bg-gray-200 dark:bg-gray-800', bar: 'bg-gray-400 dark:bg-gray-500' },
}

const bars = ref([])
const shown = computed(() => (props.muted ? 'muted' : props.state))
const tint = computed(() => TINTS[shown.value] || TINTS.neutral)
const unit = computed(() => props.size / 48)
const barWidth = computed(() => Math.max(2, Math.round(4 * unit.value)))
const gap = computed(() => Math.max(2, Math.round(3 * unit.value)))
const floor = computed(() => Math.max(2, Math.round(4 * unit.value)))

// One bar's height for a state at time t (seconds), in px.
function target(state, i, t, amp) {
  const u = unit.value
  switch (state) {
    case 'listening':
    case 'speaking': {
      const reach = state === 'speaking' ? 24 : 18
      const level = Math.min(1, amp * 1.5)
      return u * (5 + reach * level * WEIGHT[i] * (0.55 + 0.45 * Math.abs(Math.sin(t * FLICKER[i] + PHASE[i]))))
    }
    case 'working': return u * (6 + 7 * (0.5 + 0.5 * Math.sin(t * 4.2 - i * 0.9)))
    case 'connecting': return u * (5 + 4 * (0.5 + 0.5 * Math.sin(t * 2.5 - i * 0.9)))
    default: return floor.value
  }
}

const heights = new Array(BARS).fill(0)
let raf = null

function paint() {
  bars.value.forEach((el, i) => { if (el) el.style.height = `${heights[i].toFixed(1)}px` })
}

function frame(now) {
  const t = (now ?? performance.now()) / 1000
  for (let i = 0; i < BARS; i++) heights[i] += (target(shown.value, i, t, props.amplitude) - heights[i]) * 0.35
  paint()
  raf = requestAnimationFrame(frame)
}

// The still frame: listening and speaking at a mid level, the wave caught mid-run.
function still() {
  for (let i = 0; i < BARS; i++) heights[i] = target(shown.value, i, 1.2, 0.4)
  paint()
}

const reducedMotion = () => (
  typeof window !== 'undefined' && typeof window.matchMedia === 'function'
  && !!window.matchMedia('(prefers-reduced-motion: reduce)')?.matches
)

onMounted(() => {
  heights.fill(floor.value)
  if (reducedMotion() || typeof requestAnimationFrame !== 'function') { still(); return }
  paint()
  raf = requestAnimationFrame(frame)
})
onBeforeUnmount(() => {
  if (raf !== null) { cancelAnimationFrame(raf); raf = null }
})
watch([shown, () => props.size], () => { if (raf === null) still() }, { flush: 'post' })
</script>
