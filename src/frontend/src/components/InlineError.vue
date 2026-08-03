<template>
  <!--
    #1926 — the failure surface for a VERB (toggle, acknowledge, respond, stop),
    as opposed to a failed list fetch (see LoadFailed.vue).

    Design-system principle 18: toasts announce completed verbs, so a failed
    verb may not be a toast — it must persist next to the control the user just
    pressed, until they dismiss it or retry. Principle 25: name what happened
    and what to do; keep the raw error behind a disclosure.
  -->
  <div
    v-if="message"
    class="flex items-start gap-2 rounded-md border border-status-danger-200 dark:border-status-danger-500/30 bg-status-danger-50 dark:bg-status-danger-500/10 px-3 py-2 text-sm text-status-danger-700 dark:text-status-danger-300"
    role="alert"
    data-testid="inline-error"
  >
    <svg class="w-4 h-4 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
      <path
        stroke-linecap="round"
        stroke-linejoin="round"
        stroke-width="2"
        d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
      />
    </svg>

    <div class="min-w-0 flex-1">
      <p class="break-words">{{ message }}</p>
      <!-- Action before disclosure: the next step the user should take must
           come before the optional trace, not after it. -->
      <button
        v-if="retryable"
        type="button"
        class="mt-1 text-xs font-medium underline hover:no-underline focus:outline-none focus:ring-2 focus:ring-status-danger-500/40 rounded"
        @click="$emit('retry')"
      >
        {{ retryLabel }}
      </button>
      <details v-if="detail" class="mt-1">
        <summary class="text-xs opacity-80 cursor-pointer select-none">Technical detail</summary>
        <p class="mt-1 text-xs font-mono break-words opacity-90">{{ detail }}</p>
      </details>
    </div>

    <button
      type="button"
      class="flex-shrink-0 opacity-70 hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-status-danger-500/40 rounded"
      :aria-label="`Dismiss error: ${message}`"
      @click="$emit('dismiss')"
    >
      <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
      </svg>
    </button>
  </div>
</template>

<script setup>
defineProps({
  // What happened + what to do. Empty string renders nothing.
  message: {
    type: String,
    default: ''
  },
  detail: {
    type: String,
    default: ''
  },
  retryable: {
    type: Boolean,
    default: false
  },
  retryLabel: {
    type: String,
    default: 'Try again'
  }
})

defineEmits(['dismiss', 'retry'])
</script>
