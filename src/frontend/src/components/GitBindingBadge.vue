<!--
  GitBindingBadge.vue (trinity-enterprise#704)

  On the agent's Git panel: the binding the create-time choice produced —
  its own branch (an agent that saves its work) or pull-only (a deployment, or
  an agent whose token could not push). `sourceMode` is `db_config.source_mode`.
-->
<template>
  <span
    v-if="sourceMode !== null && sourceMode !== undefined"
    class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300"
    :title="sourceMode
      ? 'Pull-only: this agent tracks the branch and never pushes to it.'
      : 'This agent writes its own branch and saves its work there.'"
    data-testid="git-binding-badge"
  >{{ sourceMode ? 'Pull-only' : 'Agent · own branch' }}</span>
</template>

<script setup>
defineProps({
  sourceMode: { type: Boolean, default: null },
})
</script>
