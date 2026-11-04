<template>
  <!-- #3460: the server bounds a message, so the composer says so FIRST —
       a count for the last stretch, then a named refusal with Send held.
       One line, mutually exclusive ink arms. Shared by the 1:1 and room
       composers (#3706). -->
  <p
    v-if="limit.near"
    class="mb-2 text-xs tabular-nums"
    :class="limit.over ? 'text-status-danger-700 dark:text-status-danger-400' : 'text-gray-500 dark:text-gray-400'"
    :role="limit.over ? 'alert' : undefined"
    data-testid="portal-message-limit"
  >{{ limit.message }}</p>
</template>

<script setup>
// `limit` is a `messageLimitState(...)` result from `./portalMessageLimit`.
defineProps({ limit: { type: Object, required: true } })
</script>
