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

  It now lives as a row in the systems sidebar, which is a fixed-width column
  outside the dashboard's own flow: a late answer adds a row to a scrolling
  list and moves nothing in the grid. Layout stability by construction rather
  than by timing (contract principles 4 and 6).

  A popover, deliberately not a modal: ent#238's rule is that this is ambient —
  dismissible, gating nothing, hiding itself once the last milestone is done.
  A blocking dialog would turn a progress marker into the tour it must not be.
  It opens to the RIGHT of the rail, because the panel is wider than the rail.

  Renders nothing at all when the store says so (OSS 404, unentitled 403,
  dismissed, or complete), and its absence costs no space.
-->
<template>
  <div v-if="store.visible" ref="rootRef" class="relative">
    <button
      type="button"
      data-testid="activation-launcher"
      :class="[
        'w-full flex items-center px-3 py-2 text-sm transition-colors',
        open
          ? 'bg-gray-100 dark:bg-gray-700/60 text-gray-900 dark:text-gray-100'
          : 'text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/50',
      ]"
      :aria-expanded="open ? 'true' : 'false'"
      aria-haspopup="dialog"
      :aria-label="`Getting started — ${store.completedCount} of ${store.totalCount} done`"
      :title="collapsed ? `Getting started — ${store.completedCount} of ${store.totalCount} done` : ''"
      @click="open = !open"
    >
      <!-- The meter takes the icon slot the other rows use, so collapsed the
           row still says how far along you are rather than going mute. -->
      <span class="w-5 h-5 flex items-center justify-center mr-2 flex-shrink-0" aria-hidden="true">
        <span class="grid grid-cols-2 gap-0.5">
          <span
            v-for="n in store.totalCount"
            :key="n"
            class="block w-1.5 h-1.5 rounded-full"
            :class="n <= store.completedCount ? 'bg-status-success-500' : 'bg-gray-300 dark:bg-gray-500'"
          ></span>
        </span>
      </span>

      <template v-if="!collapsed">
        <span class="truncate flex-1 text-left">Getting started</span>
        <span class="ml-2 text-xs tabular-nums text-gray-400 dark:text-gray-400">
          {{ store.completedCount }}/{{ store.totalCount }}
        </span>
      </template>
    </button>

    <!-- Right of the rail, not below it: the rail is 224px and the panel is
         304px, so opening downward would either clip or force a narrower
         second rendering of a card that already exists. -->
    <div
      v-if="open"
      class="absolute left-full bottom-0 ml-1 z-50 w-[19rem] max-w-[calc(100vw-4rem)]"
      role="dialog"
      aria-label="Getting started"
      data-testid="activation-launcher-panel"
    >
      <ActivationChecklist embedded @close="open = false" />
    </div>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useOnboardingStore } from '../../stores/onboarding'
import ActivationChecklist from './ActivationChecklist.vue'

defineProps({
  // The rail's own state. Collapsed, the row keeps its meter and drops the
  // label, exactly as the view rows beside it do.
  collapsed: { type: Boolean, default: false },
})

const store = useOnboardingStore()
const open = ref(false)
const rootRef = ref(null)

// The launcher owns the read, so the row knows whether to exist before the
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
// open panel would otherwise be left anchored to a row that no longer exists.
watch(() => store.visible, (v) => { if (!v) open.value = false })
</script>
