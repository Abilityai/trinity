<template>
  <!-- ent#875: below the owner tier a list is the viewer's own executions; say so,
       so a shorter list than the owner's reads as scoping, not as data loss.
       Spacing comes from the caller's class (it lands on this root). -->
  <p class="text-xs text-gray-500 dark:text-gray-400" data-testid="own-rows-note">
    This agent is shared with you: you see the {{ what }} you started.
  </p>
</template>

<script setup>
defineProps({
  what: { type: String, default: 'executions' },
})
</script>
