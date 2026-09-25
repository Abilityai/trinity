<!--
  GitModeNotice.vue (trinity-enterprise#704)

  After a GitHub-backed create: what the platform decided about the git binding
  and why — the create response's `git_mode` ({kind, source_mode, reason},
  ent#705). The case that matters most is the honest one: you asked for an
  agent, but the token cannot push, so it was created pull-only.
-->
<template>
  <div
    :class="[
      'rounded-md border p-3 text-sm',
      fellBack
        ? 'border-status-warning-300 bg-status-warning-50 dark:border-status-warning-700 dark:bg-status-warning-900/30'
        : 'border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-800'
    ]"
    data-testid="git-mode-notice"
  >
    <p
      :class="fellBack
        ? 'font-medium text-status-warning-700 dark:text-status-warning-300'
        : 'font-medium text-gray-900 dark:text-gray-100'"
    >{{ headline }}</p>
    <p class="mt-0.5 text-xs text-gray-600 dark:text-gray-300">{{ gitMode.reason }}</p>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  // {kind: 'agent'|'deployment', source_mode: bool, reason: string}
  gitMode: { type: Object, required: true },
})

// Asked for an agent, got pull-only: say so plainly.
const fellBack = computed(() => props.gitMode.kind === 'agent' && props.gitMode.source_mode)

const headline = computed(() => {
  if (!props.gitMode.source_mode) return 'An agent with its own branch — its work is saved to GitHub'
  if (fellBack.value) return 'Created pull-only'
  return 'A deployment — pulls updates, never pushes'
})
</script>
