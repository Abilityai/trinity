<template>
  <!--
    A Flight Deck card (trinity-enterprise#1307): the surface primitive with a
    quiet title row — the title on the left, an optional note on the right.
  -->
  <BaseCard flush class="min-w-0 flex flex-col">
    <header class="flex-none flex items-baseline justify-between gap-3 px-4 pt-3">
      <h3 class="min-w-0 truncate text-[12.5px] font-medium text-gray-600 dark:text-gray-300">{{ title }}</h3>
      <span v-if="note" class="flex-none text-[12.5px] tabular-nums text-gray-500 dark:text-gray-400">{{ note }}</span>
    </header>
    <div class="flex-1 min-h-0 px-4 pb-3 pt-2">
      <slot />
    </div>
  </BaseCard>
</template>

<script setup>
import BaseCard from '@/components/base/BaseCard.vue'

defineProps({
  title: { type: String, required: true },
  note: { type: String, default: '' },
})
</script>
