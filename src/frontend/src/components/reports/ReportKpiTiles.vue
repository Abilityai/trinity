<template>
  <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
    <div
      v-for="(tile, idx) in tiles"
      :key="idx"
      class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2"
    >
      <p class="text-[10px] uppercase tracking-wide text-gray-500">{{ tile.label }}</p>
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
