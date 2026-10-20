<!--
  GitModeNotice.vue (trinity-enterprise#704)

  After a GitHub-backed create: what the platform decided about the git binding
  and why — the create response's `git_mode` ({kind, source_mode, pushes,
  reason}, ent#705). Keyed on `pushes`, never on `source_mode` alone: a
  fork-to-own agent is source-mode AND pushes to its own repo (PR #3022 review).
  The case that matters most is the honest one: you asked for an
  agent, but there was no token of your own that can push (the platform-wide
  token never counts, ent#705), so it was created pull-only.
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
  // {kind: 'agent'|'deployment', source_mode: bool, pushes: bool, reason: string}
  gitMode: { type: Object, required: true },
})

// A response without `pushes` (an older backend) leaves the binding unknown:
// guessing it from `source_mode` is the misread this component exists to fix.
const pushes = computed(() => props.gitMode.pushes ?? null)

// Asked for an agent, got pull-only: say so plainly.
const fellBack = computed(() => props.gitMode.kind === 'agent' && pushes.value === false)

const headline = computed(() => {
  if (pushes.value === null) return 'Created from GitHub'
  if (pushes.value) {
    return props.gitMode.source_mode
      ? 'An agent that owns its repository — its work is saved to GitHub'
      : 'An agent with its own branch — its work is saved to GitHub'
  }
  if (fellBack.value) return 'Created pull-only'
  return 'A deployment — pulls updates, never pushes'
})
</script>
