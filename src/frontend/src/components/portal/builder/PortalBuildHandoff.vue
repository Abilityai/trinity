<template>
  <!-- One of their agents did a job another asked for: said once, with the way to see it. -->
  <div
    class="rounded-lg p-3 ring-1 bg-action-primary-100 dark:bg-action-primary-500/16 ring-action-primary-200 dark:ring-action-primary-500/30"
    role="status"
    data-testid="build-handoff"
  >
    <p class="text-sm font-semibold text-gray-900 dark:text-gray-100">{{ toLabel || handoff.to }} just did a job for {{ fromLabel || handoff.from }}</p>
    <p class="mt-1 text-xs text-gray-700 dark:text-gray-200">
      {{ fromLabel || handoff.from }} asked, and {{ toLabel || handoff.to }} did the work. Want to see where that shows up?
    </p>
    <div class="mt-2 flex flex-wrap items-baseline gap-x-4 gap-y-1">
      <!-- A link, not a button: it opens the real page in a new tab, where the walk runs. -->
      <a
        :href="link"
        target="_blank"
        rel="noopener"
        class="inline-flex items-center gap-1.5 rounded-sm text-sm font-semibold text-action-primary-700 dark:text-action-primary-300 underline decoration-2 underline-offset-4 hover:no-underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:focus-visible:ring-action-primary-400/40"
        data-testid="build-handoff-show-me"
        @click="$emit('shown', $event, link)"
      >Show me<svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M13.5 6H5.25A2.25 2.25 0 003 8.25v10.5A2.25 2.25 0 005.25 21h10.5A2.25 2.25 0 0018 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25" /></svg></a>
      <button
        type="button"
        class="rounded-sm text-xs text-gray-600 dark:text-gray-300 underline underline-offset-2 hover:no-underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:focus-visible:ring-action-primary-400/40"
        data-testid="build-handoff-dismiss"
        @click="$emit('dismiss')"
      >Not now</button>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { handoffLink } from './handoff'

const props = defineProps({
  // `{ from, to, at }` (`handoff.js`): who asked, who did the job.
  handoff: { type: Object, required: true },
  fromLabel: { type: String, default: '' },
  toLabel: { type: String, default: '' },
})
// `shown`: its Show me link was opened (the click and the link). `dismiss`: put away.
defineEmits(['shown', 'dismiss'])

const link = computed(() => handoffLink(props.handoff))
</script>
