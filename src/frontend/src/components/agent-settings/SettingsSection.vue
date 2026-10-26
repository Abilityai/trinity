<template>
  <!--
    One titled group inside a Settings pane (trinity-enterprise#887): the
    section heading above, the rows inside one BaseCard, divided by a hairline.
    `danger` is the Danger zone's treatment — the heading and the card border
    take the danger token so the destructive group reads as one at a glance.
    Rows are `SettingRow`s, or any composed block (a moved panel) that owns its
    own padding; `flush` hands the card's padding to those children.
  -->
  <section :aria-labelledby="headingId" class="space-y-2">
    <div>
      <h3
        :id="headingId"
        class="text-lg font-semibold"
        :class="danger ? 'text-status-danger-700 dark:text-status-danger-400' : 'text-gray-900 dark:text-gray-100'"
      >{{ title }}</h3>
      <p v-if="description" class="mt-0.5 text-[12.5px] text-gray-600 dark:text-gray-400">{{ description }}</p>
    </div>
    <slot v-if="bare" />
    <div
      v-else
      class="bg-white dark:bg-gray-800 border rounded-lg shadow-sm divide-y divide-gray-200 dark:divide-gray-750"
      :class="danger ? 'border-status-danger-200 dark:border-status-danger-500/40' : 'border-gray-200 dark:border-gray-750'"
    >
      <slot />
    </div>
  </section>
</template>

<script setup>
import { useId } from 'vue'

defineProps({
  title: { type: String, required: true },
  description: { type: String, default: '' },
  danger: { type: Boolean, default: false },
  bare: { type: Boolean, default: false },
})

const headingId = useId()
</script>
