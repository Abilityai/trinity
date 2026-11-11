<!--
  A short walk around the Workspace, the first time someone lands on the Build page.
  It dims the page, lights up one part at a time and says what that part is for. A part
  that isn't on screen is left out rather than pointed at, and a phone, with little more
  than the chat on screen, gets no tour. What each step says lives in `portalBuildTour.js`.
-->
<template>
  <Teleport to="body">
    <div v-if="open && step" class="fixed inset-0 z-50" :data-testid="testid" role="dialog" aria-modal="true" aria-labelledby="build-tour-title" @keydown.esc="$emit('close', 'escape')">
      <!-- The light is the hole left by one huge shadow, so everything else stays dim. -->
      <div
        class="absolute rounded-xl ring-2 ring-action-primary-500 transition-all duration-200 motion-reduce:transition-none"
        :style="{ ...spot, boxShadow: '0 0 0 9999px rgb(3 7 18 / 0.62)' }"
        aria-hidden="true"
      ></div>
      <div
        ref="card"
        class="absolute w-[340px] max-w-[calc(100vw-32px)] rounded-[10px] border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800 p-4 shadow-2xl transition-all duration-200 motion-reduce:transition-none"
        :style="cardPlace"
        tabindex="-1"
      >
        <p v-if="steps.length > 1" class="text-[11px] tabular-nums text-gray-500 dark:text-gray-400" data-testid="build-tour-progress">{{ index + 1 }} of {{ steps.length }}</p>
        <h2 id="build-tour-title" class="mt-1 text-base font-semibold text-gray-900 dark:text-gray-100">{{ step.title }}</h2>
        <p class="mt-1 text-sm text-gray-600 dark:text-gray-300" data-testid="build-tour-body">{{ step.body }}</p>
        <div class="mt-4 flex items-center gap-2">
          <BaseButton v-if="skippable && index < steps.length - 1" variant="ghost" size="sm" data-testid="build-tour-skip" @click="$emit('close', 'skipped')">Skip the tour</BaseButton>
          <span class="flex-1"></span>
          <BaseButton v-if="index > 0" variant="secondary" size="sm" data-testid="build-tour-back" @click="index -= 1">Back</BaseButton>
          <BaseButton size="sm" data-testid="build-tour-next" @click="next">{{ index < steps.length - 1 ? 'Next' : doneLabel }}</BaseButton>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import BaseButton from '@/components/base/BaseButton.vue'
import { tourCardPosition, tourSteps } from './portalBuildTour'

const props = defineProps({
  open: { type: Boolean, default: false },
  guideName: { type: String, default: '' },
  // Another page's stops (the Dashboard's feature tour). None means the Workspace tour.
  steps: { type: Array, default: null },
  // A tour needs two stops to be worth it; Show me lights up just one.
  minSteps: { type: Number, default: 2 },
  // A level moment is one or two stops, with nothing to skip.
  skippable: { type: Boolean, default: true },
  testid: { type: String, default: 'build-tour' },
  // What the last stop's button says: the way to the Dashboard opens it.
  doneLabel: { type: String, default: 'Got it' },
})
const emit = defineEmits(['close'])

const PAD = 6
const card = ref(null)
const index = ref(0)
const rect = ref(null)
const steps = ref([])
const step = computed(() => steps.value[index.value] || null)

// A stop names its part by tour name, or by a selector (a level moment's sidebar agent row).
const target = (s) => document.querySelector(s.selector || `[data-tour="${s.target}"]`)
const visible = (el) => {
  const r = el?.getBoundingClientRect?.()
  return Boolean(r && r.width > 0 && r.height > 0)
}

const spot = computed(() => (rect.value ? {
  left: `${rect.value.left - PAD}px`,
  top: `${rect.value.top - PAD}px`,
  width: `${rect.value.width + PAD * 2}px`,
  height: `${rect.value.height + PAD * 2}px`,
} : {}))

const cardPlace = ref({})

async function place() {
  const el = step.value && target(step.value)
  if (!visible(el)) { dropHidden(); return }
  el.scrollIntoView?.({ block: 'nearest' })
  const r = el.getBoundingClientRect()
  rect.value = { left: r.left, top: r.top, width: r.width, height: r.height }
  await nextTick()
  const box = card.value?.getBoundingClientRect?.()
  cardPlace.value = tourCardPosition(rect.value, { width: box?.width || 340, height: box?.height || 200 }, { width: window.innerWidth, height: window.innerHeight })
}

// A part that left the screen after the stops were picked (a nav link folding into
// "N more", a resize) is left out rather than pointed at. Closing with nothing left to
// show is "dropped", so a level moment can come back once its card has drawn.
function dropHidden() {
  if (!step.value) return
  steps.value = steps.value.filter((s) => visible(target(s)))
  if (steps.value.length < props.minSteps) { emit('close', 'dropped'); return }
  index.value = Math.min(index.value, steps.value.length - 1)
  place()
}

function next() {
  if (index.value < steps.value.length - 1) index.value += 1
  else emit('close', 'done')
}

async function begin() {
  await nextTick()
  // One frame, so parts that measure themselves after mounting (the nav) have settled.
  if (typeof requestAnimationFrame === 'function') await new Promise((done) => requestAnimationFrame(() => done()))
  steps.value = (props.steps || tourSteps(props.guideName)).filter((s) => visible(target(s)))
  index.value = 0
  // On a phone the chat is about all that's on screen, so there's nothing to walk round.
  if (steps.value.length < props.minSteps) { emit('close', 'dropped'); return }
  await place()
  card.value?.focus?.()
}

// Closed, it forgets its stops, so reopening for the next moment never shows the last one.
watch(() => props.open, (now) => {
  if (now) { begin(); return }
  steps.value = []
  index.value = 0
  rect.value = null
}, { immediate: true })
watch(index, place)

const onResize = () => { if (props.open) place() }
window.addEventListener('resize', onResize)
onBeforeUnmount(() => window.removeEventListener('resize', onResize))
</script>
