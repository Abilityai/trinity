<template>
  <!--
    Compact channel summary row with a collapsible body (trinity-enterprise#18).
    Collapsed by default: the row IS the "compact summary"; expanding reveals the
    existing channel config panel. This is an interim seam — #19 ("Move channel
    configuration into dialogs") replaces the expandable body with a modal dialog.
  -->
  <div class="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
    <button
      type="button"
      class="w-full flex items-center gap-3 px-4 py-3 text-left bg-white dark:bg-gray-800 hover:bg-gray-50 dark:hover:bg-gray-700/50 focus:outline-none focus:ring-1 focus:ring-action-primary-500"
      :aria-expanded="open"
      @click="open = !open"
    >
      <!-- Channel glyph -->
      <span class="shrink-0 text-xl leading-none" aria-hidden="true">{{ icon }}</span>

      <span class="min-w-0 flex-1">
        <span class="block text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{{ title }}</span>
        <span v-if="subtitle" class="block text-xs text-gray-500 dark:text-gray-400 truncate">{{ subtitle }}</span>
      </span>

      <!-- Chevron -->
      <svg
        class="shrink-0 h-4 w-4 text-gray-400 transition-transform duration-150"
        :class="open ? 'rotate-180' : ''"
        fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"
        aria-hidden="true"
      >
        <path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7" />
      </svg>
    </button>

    <div v-if="open" class="px-4 py-4 border-t border-gray-200 dark:border-gray-700 bg-gray-50/50 dark:bg-gray-900/30">
      <slot />
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'

defineProps({
  title: { type: String, required: true },
  subtitle: { type: String, default: '' },
  // A small emoji/text glyph for the channel (kept dependency-free; the dialogs
  // issue #19 can swap these for proper brand icons).
  icon: { type: String, default: '🔗' },
})

const open = ref(false)
</script>
