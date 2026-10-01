<!--
  trinity-enterprise#610 sign-off round 8: "replying to" — the Codex-style tab
  that sits on top of the composer (and, `placement="message"`, above the sent
  message). It shows a one-line excerpt; the agent is given the stored message
  itself, quoted server-side from its id. A sent message's chip is removable only
  while that message failed (#3054 review): a refused reply target must be
  droppable, or Retry re-sends the same id into the same 422.
-->
<template>
  <div
    class="flex items-center gap-2 min-w-0 text-xs text-gray-600 dark:text-gray-300"
    :class="placement === 'composer'
      ? 'mx-3 px-3 py-1.5 rounded-t-xl border border-b-0 border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900'
      : 'justify-end'"
    data-testid="portal-reply-chip"
  >
    <svg class="w-3.5 h-3.5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 10h10a8 8 0 018 8v2M3 10l6 6m-6-6l6-6" /></svg>
    <span class="sr-only">Replying to:</span>
    <span class="min-w-0 truncate" :title="excerpt">{{ excerpt }}</span>
    <button
      v-if="removable"
      type="button"
      class="ml-auto shrink-0 p-0.5 rounded hover:text-gray-900 dark:hover:text-gray-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
      title="Don't reply to this message"
      aria-label="Don't reply to this message"
      data-testid="portal-reply-chip-remove"
      @click="$emit('remove')"
    >
      <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" /></svg>
    </button>
  </div>
</template>

<script setup>
defineProps({
  excerpt: { type: String, default: '' },
  removable: { type: Boolean, default: true },
  placement: { type: String, default: 'composer' },
})
defineEmits(['remove'])
</script>
