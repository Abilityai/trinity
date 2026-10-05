<!--
  GitBindingBadge.vue (trinity-enterprise#704)

  On the agent's Git panel: the binding the create-time choice produced —
  whether the agent saves its work to GitHub (its own branch, or its own repo
  after fork-to-own) or is pull-only (a deployment, or an agent whose token
  could not push). Keyed on `pushes` (`db_config.pushes`), never on
  `source_mode` alone: a fork-to-own agent is source-mode AND pushes (PR #3022
  review). `sourceMode` only names which of the two pushing shapes it is.
-->
<template>
  <span
    v-if="pushes !== null && pushes !== undefined"
    class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300"
    :title="title"
    data-testid="git-binding-badge"
  >{{ label }}</span>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  pushes: { type: Boolean, default: null },
  sourceMode: { type: Boolean, default: null },
})

const label = computed(() => {
  if (!props.pushes) return 'Pull-only'
  return props.sourceMode ? 'Agent · own repo' : 'Agent · own branch'
})

const title = computed(() => {
  if (!props.pushes) return 'Pull-only: this agent tracks the branch, and auto-sync does not push its work.'
  return props.sourceMode
    ? 'This agent owns its repository and saves its work to its default branch.'
    : 'This agent writes its own branch and saves its work there.'
})
</script>
