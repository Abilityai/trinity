<!--
  Activation launcher (ent#238 presentation, follow-up to ent#581).

  The checklist itself is unchanged — this only decides WHERE it lives. It used
  to render inline in the dashboard flow, which meant the card could not appear
  without pushing the fleet grid down: `ActivationChecklist` fetches in its own
  `onMounted` and renders under `v-if="store.visible"`, so the answer that
  decides whether there is a card arrives one round-trip AFTER the dashboard has
  painted. Nothing could reserve the space, because nothing knew whether a card
  was coming — and reserving it unconditionally would leave a permanent gap on
  every unentitled build, where the endpoint 404s and the card never renders.

  Moving it behind a launcher removes the question rather than answering it.
  The pill sits in the header's controls cluster, which is a fixed-height row,
  so the checklist arriving late changes nothing below it. Layout stability by
  construction instead of by timing (contract principles 4 and 6).

  A popover, deliberately not a modal: ent#238's rule is that this is ambient —
  dismissible, gating nothing, hiding itself once the last milestone is done.
  A blocking dialog would turn a progress marker into the tour it must not be.

  Renders nothing at all when the store says so (OSS 404, unentitled 403,
  dismissed, or complete), and its absence costs no space in the header.
-->
<template>
  <div v-if="store.visible" ref="rootRef" class="relative">
    <button
      type="button"
      data-testid="activation-launcher"
      class="flex items-center space-x-1.5 px-2 py-0.5 rounded text-xs font-medium transition-all whitespace-nowrap
             bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300
             hover:bg-gray-200 dark:hover:bg-gray-600
             focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
      :aria-expanded="open ? 'true' : 'false'"
      aria-haspopup="dialog"
      :aria-label="`Getting started — ${store.completedCount} of ${store.totalCount} done`"
      @click="open = !open"
    >
      <!-- The meter IS the label at narrow widths, so the control still says
           how far along you are when the words are gone. -->
      <span class="flex items-center space-x-0.5" aria-hidden="true">
        <span
          v-for="n in store.totalCount"
          :key="n"
          class="block w-1.5 h-1.5 rounded-full"
          :class="n <= store.completedCount
            ? 'bg-status-success-500'
            : 'bg-gray-300 dark:bg-gray-500'"
        ></span>
      </span>
      <span class="hidden md:inline">Getting started</span>
      <span class="tabular-nums text-gray-500 dark:text-gray-400">
        {{ store.completedCount }}/{{ store.totalCount }}
      </span>
    </button>

    <!-- Anchored to the trigger, right-aligned like the Tags dropdown beside
         it, so the two read as one family of header controls. -->
    <div
      v-if="open"
      class="absolute right-0 top-full mt-1 z-50 w-[19rem] max-w-[calc(100vw-2rem)]"
      role="dialog"
      aria-label="Getting started"
      data-testid="activation-launcher-panel"
    >
      <ActivationChecklist embedded @dismissed="open = false" />
    </div>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useOnboardingStore } from '../../stores/onboarding'
import ActivationChecklist from './ActivationChecklist.vue'

const store = useOnboardingStore()
const open = ref(false)
const rootRef = ref(null)

// The launcher owns the read, so the pill knows whether to exist before the
// panel is ever opened. `fetchChecklist` early-returns once loaded, so the
// checklist's own mounted call stays a harmless no-op.
onMounted(() => {
  store.fetchChecklist()
  document.addEventListener('click', onDocClick, true)
  document.addEventListener('keydown', onKey)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick, true)
  document.removeEventListener('keydown', onKey)
})

function onDocClick(e) {
  if (open.value && !rootRef.value?.contains(e.target)) open.value = false
}
function onKey(e) {
  if (e.key === 'Escape' && open.value) open.value = false
}

// Finishing the last milestone (or dismissing) retires the whole control; an
// open panel would otherwise be left anchored to a trigger that no longer
// exists.
watch(() => store.visible, (v) => { if (!v) open.value = false })
</script>
