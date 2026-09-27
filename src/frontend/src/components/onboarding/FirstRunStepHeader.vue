<!--
  The one header every first-run step renders (ent#581). It owns
  `id="first-run-title"`, which the overlay's `aria-labelledby` points at, so
  exactly one step header is mounted at a time. Schematic beside the text on
  desktop, above it on phone (spec comment 3).
-->
<template>
  <div class="flex items-start gap-7 max-sm:flex-col-reverse max-sm:gap-3">
    <div class="min-w-0 flex-1">
      <div v-if="kicker || badge" class="mb-1.5 flex flex-wrap items-center gap-2">
        <span
          v-if="kicker"
          class="font-mono text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400"
        >{{ kicker }}</span>
        <BaseBadge v-if="badge" :variant="badgeVariant">{{ badge }}</BaseBadge>
      </div>
      <h2
        id="first-run-title"
        class="text-lg font-[650] leading-snug text-gray-900 dark:text-gray-100"
      >
        {{ title }}
      </h2>
      <p v-if="lead" class="mt-1.5 text-sm leading-[1.55] text-gray-600 dark:text-gray-300">
        {{ lead }}
      </p>
    </div>
    <Schematic v-if="schematic" :name="schematic" />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import BaseBadge from '../base/BaseBadge.vue'
import Schematic from './schematics/Schematic.vue'

const props = defineProps({
  kicker: { type: String, default: '' },
  title: { type: String, required: true },
  lead: { type: String, default: '' },
  // The registry tag ('Required' / 'Recommended' / 'Optional') or 'Done'.
  badge: { type: String, default: '' },
  // A `Schematic` name: secure · email · claude · keys · agent · sharing.
  schematic: { type: String, default: '' },
})

// One fact per badge. Required is information, not an alarm; Done is the one
// state that earned success.
const badgeVariant = computed(
  () => ({ Required: 'info', Recommended: 'info', Done: 'success' })[props.badge] || 'neutral'
)
</script>
