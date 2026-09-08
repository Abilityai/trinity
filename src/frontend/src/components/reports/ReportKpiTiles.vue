<template>
  <!-- `report-kpi-grid` names the grid for the canvas kit, whose container
       rules re-flow it by the COLUMN's width (a 24rem rail on a 1920px screen
       is `lg:` to these viewport breakpoints, which put four 80px tiles side
       by side, #2583). Off the canvas the viewport classes stay as they were. -->
  <div class="report-kpi-grid grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
    <div
      v-for="(tile, idx) in tiles"
      :key="idx"
      class="min-w-0 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2 [overflow-wrap:anywhere]"
    >
      <p class="text-[10px] uppercase tracking-wide text-gray-500 dark:text-gray-400">{{ tile.label }}</p>
      <p class="text-base font-semibold text-gray-900 dark:text-gray-100">
        {{ tile.value }}<span v-if="tile.unit" class="text-xs text-gray-400 ml-0.5">{{ tile.unit }}</span>
      </p>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

// Expected payload shape: { tiles: Array<{label, value, unit?}> }
const props = defineProps({ payload: { type: Object, default: () => ({}) } })

const tiles = computed(() => (Array.isArray(props.payload?.tiles) ? props.payload.tiles : []))
</script>
