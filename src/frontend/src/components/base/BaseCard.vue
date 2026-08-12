<template>
  <!--
    The surface primitive (docs/memory/design-system.md §5, #2122).
    ONE surface recipe everywhere: surface bg, 1px border, radius 8px,
    padding 16, shadow-sm. Stat cards, tiles, and list panels are
    COMPOSITIONS of it — pass `flush` when the composition owns its own
    internal padding (header/body splits), and override nothing else.
  -->
  <div
    class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-750 rounded-lg shadow-sm"
    :class="flush ? '' : 'p-4'"
  >
    <slot />
  </div>
</template>

<script setup>
defineProps({
  // For header/body compositions that own their internal padding.
  flush: {
    type: Boolean,
    default: false,
  },
})
</script>
