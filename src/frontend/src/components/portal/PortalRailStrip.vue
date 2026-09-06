<!--
  The rail's mobile collapsed form (trinity-enterprise#474): a strip above the
  composer, the `PortalLoops` strip pattern, carrying the same signals as the
  desktop column — as words. `● Work · 1 running · ● Canvas updated`, and with
  nothing to report, the tab names, so it still says what a tap opens.

  Rendered by the conversation and the room through their `#rail-strip` slot,
  below `sm` only — above that the column beside the stage IS the collapsed
  rail. A tap opens the bottom sheet (`PortalRail` in `sheet` mode).

  Presentational: `stripSegments` decides the words; this file draws them.
-->
<template>
  <button
    v-if="tabs.length"
    type="button"
    class="sm:hidden w-full flex items-center gap-2 border-t border-gray-200 dark:border-gray-700 px-4 py-2 text-xs text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800"
    aria-label="Open the conversation rail"
    data-testid="portal-rail-strip"
    @click="$emit('open')"
  >
    <template v-for="(seg, i) in segments" :key="seg.id">
      <span v-if="i" class="text-gray-400" aria-hidden="true">·</span>
      <span class="inline-flex items-center gap-1.5 min-w-0">
        <span v-if="seg.shape" :class="dotClass(seg.shape)" aria-hidden="true"></span>
        <span class="truncate" :class="seg.shape ? 'font-medium' : ''">{{ seg.text }}</span>
      </span>
    </template>
    <svg class="ml-auto w-4 h-4 shrink-0 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 15l7-7 7 7" /></svg>
  </button>
</template>

<script setup>
import { computed } from 'vue'
import { RAIL_SIGNAL_LIVE, RAIL_SIGNAL_UPDATED, stripSegments } from './portalRail'

const props = defineProps({
  // The session's VISIBLE tabs (`visibleTabs`), never the registry.
  tabs: { type: Array, default: () => [] },
  signals: { type: Object, default: () => ({}) },
})
defineEmits(['open'])

const segments = computed(() => stripSegments(props.signals, props.tabs))

// The same two shapes as the column (portalRail.js / PortalRail.vue).
function dotClass(shape) {
  if (shape === RAIL_SIGNAL_LIVE) {
    return 'block w-2 h-2 rounded-full bg-action-primary-500 ring-[3px] ring-action-primary-500/[.28] motion-safe:animate-pulse'
  }
  if (shape === RAIL_SIGNAL_UPDATED) return 'block w-1.5 h-1.5 rounded-full bg-action-primary-500'
  return ''
}
</script>
