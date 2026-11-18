<template>
  <!--
    "This schedule run did not run at owner tier" (#4281 slice C/F). The fact is
    the run's `turn_integrity.restricted_turn` record, codes only; its status
    does not change, so a `success` that ran without the shell needs this mark.
    `badge` for a list row, `line` for a detail view. `testid` is required so
    two surfaces on one screen never share an address.
  -->
  <BaseBadge
    v-if="record && variant === 'badge'"
    variant="warning"
    size="sm"
    :title="sentence"
    :data-testid="testid"
  >ran without shell</BaseBadge>
  <p
    v-else-if="record"
    class="flex items-start gap-1.5 text-[12.5px] text-status-warning-700 dark:text-status-warning-400"
    :data-testid="testid"
  >
    <svg class="mt-0.5 h-3 w-3 flex-none" viewBox="0 0 16 16" aria-hidden="true">
      <path d="M8 1.5 15 14H1z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" />
      <path d="M8 6v3.5M8 11.5v.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
    </svg>
    <span>{{ sentence }}</span>
  </p>
</template>

<script setup>
import { computed } from 'vue'
import BaseBadge from './base/BaseBadge.vue'
import { restrictedTurnOf, restrictedRunSentence } from '../utils/scheduleRunTier'

const props = defineProps({
  // The execution's `turn_integrity` (JSON text, or an object).
  turnIntegrity: { type: [String, Object], default: null },
  variant: { type: String, default: 'badge', validator: (v) => ['badge', 'line'].includes(v) },
  testid: { type: String, required: true },
})

const record = computed(() => restrictedTurnOf(props.turnIntegrity))
const sentence = computed(() => restrictedRunSentence(record.value))
</script>
