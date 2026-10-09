<template>
  <!--
    "This run went through without approval because its requester is the
    approver" (trinity-enterprise#754, the 10-03 eyeball: an approver's own
    gated run gave no sign a gate applied — its only trace was an audit row).
    The facts arrive decided by the server (`gate_self_approved`,
    `gate_self_approved_by_viewer`, derived from ent#752's self-approved
    record); no email reaches the page. `badge` for a list row, `line` for a
    detail view and the Workspace turn.
  -->
  <BaseBadge
    v-if="selfApproved && variant === 'badge'"
    variant="locked"
    :title="sentence"
    data-testid="execution-gate-marker"
  >ran without approval</BaseBadge>
  <p
    v-else-if="selfApproved"
    class="flex items-center gap-1.5 text-[12.5px] text-state-locked-700 dark:text-state-locked-400"
    data-testid="execution-gate-marker"
  >
    <svg class="h-3 w-3 flex-none" viewBox="0 0 16 16" aria-hidden="true">
      <rect x="3.5" y="7" width="9" height="7" rx="1.5" fill="currentColor" />
      <path d="M5.5 7V5.2a2.5 2.5 0 0 1 5 0V7" fill="none" stroke="currentColor" stroke-width="1.7" />
    </svg>
    <span>{{ sentence }}</span>
  </p>
</template>

<script setup>
import { computed } from 'vue'
import BaseBadge from '../base/BaseBadge.vue'

const props = defineProps({
  selfApproved: { type: Boolean, default: false },
  byViewer: { type: Boolean, default: false },
  variant: { type: String, default: 'badge', validator: (v) => ['badge', 'line'].includes(v) },
})

const sentence = computed(() => (props.byViewer
  ? 'Ran without approval: you are the approver.'
  : 'Ran without approval: its approver started it.'))
</script>
