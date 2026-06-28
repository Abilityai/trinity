<template>
  <ol class="relative border-l border-gray-200 dark:border-gray-700 ml-2">
    <li v-for="(ev, idx) in events" :key="idx" class="mb-4 ml-4">
      <div class="absolute -left-1.5 w-3 h-3 rounded-full bg-blue-500 border border-white dark:border-gray-900"></div>
      <time v-if="ev.ts" class="text-[11px] text-gray-400">{{ ev.ts }}</time>
      <p class="text-sm font-medium text-gray-800 dark:text-gray-200">{{ ev.label }}</p>
      <p v-if="ev.detail" class="text-xs text-gray-500 dark:text-gray-400">{{ ev.detail }}</p>
    </li>
    <li v-if="events.length === 0" class="ml-4 text-xs text-gray-400">No events.</li>
  </ol>
</template>

<script setup>
import { computed } from 'vue'

// Expected payload shape: { events: Array<{ts?, label, detail?}> }
const props = defineProps({ payload: { type: Object, default: () => ({}) } })

const events = computed(() => (Array.isArray(props.payload?.events) ? props.payload.events : []))
</script>
