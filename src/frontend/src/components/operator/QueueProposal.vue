<!--
  trinity-enterprise#611 — the exact action an approval asks a person to approve
  (`proposal`). The prompt tells agents to put the action and its parameters
  there "so the operator can verify what they are approving", so every card that
  offers the decision shows it, expanded, never behind "Show details" the way
  `context` is. Agent-authored: text interpolation only, never v-html. Bounded:
  the sink caps a proposal at 8 KB, and the list scrolls inside its own box.
-->
<template>
  <div v-if="rows.length" data-testid="queue-proposal">
    <p class="text-[11px] font-mono font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
      Proposed action
    </p>
    <dl class="mt-1 max-h-48 overflow-y-auto rounded-lg bg-gray-50 dark:bg-gray-900/50 px-3 py-2 space-y-1">
      <div
        v-for="row in rows"
        :key="row.key"
        class="flex items-start gap-3 text-[12.5px]"
        data-testid="queue-proposal-row"
      >
        <dt class="flex-shrink-0 min-w-[96px] max-w-[200px] truncate font-mono text-gray-500 dark:text-gray-400" :title="row.key">{{ row.key }}</dt>
        <dd class="min-w-0 font-mono break-all whitespace-pre-wrap text-gray-900 dark:text-gray-100">{{ row.value }}</dd>
      </div>
    </dl>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { proposalRows } from '../../utils/operatorQueue'

const props = defineProps({
  proposal: { type: Object, default: null },
})

const rows = computed(() => proposalRows(props.proposal))
</script>
