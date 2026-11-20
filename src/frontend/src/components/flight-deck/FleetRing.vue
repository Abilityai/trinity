<template>
  <!--
    The fleet at a glance (trinity-enterprise#1307): one dot per agent around a
    ring, the working count in the middle. Deliberately plain — neutral ink, one
    accent for "working", no glow. Shape carries state as well as colour:
    working is a filled dot, idle a ring, stopped a dashed ring.

    The only motion: a dot changes state with a short fade, and one thin ring
    spreads from an agent when a run of its starts or ends.
  -->
  <svg viewBox="0 0 200 200" class="block h-full w-full" role="img" :aria-label="summary" data-testid="flight-deck-ring">
    <circle cx="100" cy="100" :r="RADIUS" class="fill-none stroke-gray-200 dark:stroke-gray-750" stroke-width="1" />
    <g v-for="d in dots" :key="d.name">
      <circle
        v-if="pulses[d.name]"
        :key="pulses[d.name].key"
        :cx="d.x" :cy="d.y" :r="d.r"
        class="deck-pulse fill-none"
        :class="pulses[d.name].kind === 'failed' ? 'stroke-status-danger-500' : 'stroke-gray-500 dark:stroke-gray-400'"
        stroke-width="1"
        @animationend="clearPulse(d.name)"
      />
      <circle
        :cx="d.x" :cy="d.y" :r="d.r"
        class="deck-dot"
        :class="d.working
          ? 'fill-action-primary-500 stroke-action-primary-500'
          : 'fill-transparent stroke-gray-400 dark:stroke-gray-500'"
        stroke-width="1.25"
        :stroke-dasharray="d.stopped ? '1.5 1.5' : undefined"
        :opacity="d.stopped ? 0.6 : 1"
      >
        <title>{{ d.label }} — {{ d.state }}</title>
      </circle>
    </g>
    <text x="100" y="110" text-anchor="middle" class="fill-gray-900 dark:fill-gray-100" font-size="44" font-weight="600">{{ counts.working }}</text>
    <text x="100" y="128" text-anchor="middle" class="fill-gray-500 dark:fill-gray-400" font-size="11.5">of {{ counts.total }} working</text>
  </svg>
</template>

<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({
  // Ordered rows from `deckAgents`.
  rows: { type: Array, default: () => [] },
  counts: { type: Object, default: () => ({ working: 0, total: 0 }) },
  // Newest-first events from `deckFeed`; a new key draws one ring.
  feed: { type: Array, default: () => [] },
})

const RADIUS = 82
const MAX_DOTS = 60

const summary = computed(() => `${props.counts.working} of ${props.counts.total} agents working`)

// A dot keeps its place: positions follow the name order, not the working-first
// order of the list, so an agent starting a run does not make the ring shuffle.
const dots = computed(() => {
  const shown = [...props.rows].sort((a, b) => a.name.localeCompare(b.name)).slice(0, MAX_DOTS)
  const r = shown.length > 36 ? 3 : shown.length > 20 ? 4.25 : 6
  return shown.map((row, i) => {
    const a = (i / shown.length) * Math.PI * 2 - Math.PI / 2
    return {
      name: row.name,
      label: row.label,
      working: row.working,
      stopped: row.stopped,
      state: row.working ? 'working' : row.stopped ? 'stopped' : 'idle',
      x: +(100 + Math.cos(a) * RADIUS).toFixed(2),
      y: +(100 + Math.sin(a) * RADIUS).toFixed(2),
      r,
    }
  })
})

const pulses = ref({})
let seen = null
watch(() => props.feed, (feed) => {
  const keys = new Set(feed.map((e) => e.key))
  if (seen) {
    const next = { ...pulses.value }
    for (const e of feed) if (!seen.has(e.key)) next[e.agent] = { key: e.key, kind: e.kind }
    pulses.value = next
  }
  seen = keys // the first feed only primes: history is not replayed as rings
}, { immediate: true })

function clearPulse(name) {
  const next = { ...pulses.value }
  delete next[name]
  pulses.value = next
}
</script>

<style scoped>
.deck-dot {
  transition: fill 400ms ease, stroke 400ms ease;
}
.deck-pulse {
  transform-box: fill-box;
  transform-origin: center;
  animation: deck-pulse 1400ms ease-out 1 forwards;
}
@keyframes deck-pulse {
  from { transform: scale(1); opacity: 0.7; }
  to { transform: scale(3.2); opacity: 0; }
}
@media (prefers-reduced-motion: reduce) {
  .deck-dot { transition: none; }
  .deck-pulse { display: none; }
}
</style>
