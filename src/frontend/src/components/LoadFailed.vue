<template>
  <!--
    #1926 — the "failed" member of the loading / empty / failed triad
    (design-system principles 15 + 25).

    A failed fetch must never borrow the empty state's copy: "No templates
    found" on a network error points the user at the wrong remedy (create one)
    instead of the right one (retry). This block names what happened, offers
    the retry, and keeps the raw technical detail behind a disclosure so the
    headline stays in user vocabulary.

    Shares the footprint of the sibling loading/empty blocks (same centered
    `py-12` column) so nothing shifts when the state resolves (principle 4).
  -->
  <div
    class="text-center px-4"
    :class="dense ? 'py-6' : 'py-12'"
    role="alert"
    data-testid="load-failed"
  >
    <svg
      class="mx-auto text-status-danger-500 dark:text-status-danger-400"
      :class="dense ? 'w-8 h-8 mb-2' : 'w-12 h-12 mb-4'"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      aria-hidden="true"
    >
      <path
        stroke-linecap="round"
        stroke-linejoin="round"
        stroke-width="2"
        d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
      />
    </svg>

    <p class="font-medium text-gray-900 dark:text-gray-100">{{ title }}</p>
    <p class="text-sm mt-1 text-gray-600 dark:text-gray-400">{{ message }}</p>

    <button
      v-if="showRetry"
      type="button"
      class="mt-4 text-sm font-medium text-action-primary-600 dark:text-action-primary-400 hover:text-action-primary-800 dark:hover:text-action-primary-300 focus:outline-none focus:ring-2 focus:ring-action-primary-500/40 rounded px-2 py-1 disabled:opacity-45 disabled:cursor-wait"
      :disabled="retrying"
      @click="$emit('retry')"
    >
      {{ retrying ? 'Retrying…' : retryLabel }}
    </button>

    <!-- Principle 25: codes and traces live behind a disclosure, not in the headline. -->
    <details v-if="detail" class="mt-3 text-left max-w-md mx-auto">
      <summary class="text-xs text-gray-500 dark:text-gray-400 cursor-pointer select-none">
        Technical detail
      </summary>
      <p class="mt-1 text-xs font-mono break-words text-gray-600 dark:text-gray-400">
        {{ detail }}
      </p>
    </details>
  </div>
</template>

<script setup>
defineProps({
  // Headline: what happened, in user vocabulary.
  title: {
    type: String,
    default: "Couldn't load"
  },
  // What it means / what to do next.
  message: {
    type: String,
    default: 'The request failed. Check your connection and try again.'
  },
  // Raw error text (status code, server message) — disclosed, never the headline.
  detail: {
    type: String,
    default: ''
  },
  retryLabel: {
    type: String,
    default: 'Try again'
  },
  // True while the retry is in flight, so the control can't be double-fired.
  retrying: {
    type: Boolean,
    default: false
  },
  // `:show-retry="false"` for the rare surface with nothing to retry.
  // NOT named `onRetry`: Vue compiles the parent's `@retry` into an `onRetry`
  // prop, so a prop of that name collides with this component's own emit.
  showRetry: {
    type: Boolean,
    default: true
  },
  // Compact variant for small panels and rows.
  dense: {
    type: Boolean,
    default: false
  }
})

defineEmits(['retry'])
</script>
