<template>
  <!-- Operations › Evaluations failed checks + failed runs
       (trinity-enterprise#938 C3). The window's failed-check counts, then the
       runs that did not complete or failed a gate check, newest first, each
       linking to its run page. Check ids and reason codes only — the ent#937
       failed route never carries evidence. -->
  <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg" data-testid="eval-failed">
    <div class="flex flex-wrap items-center gap-2 px-4 py-2 border-b border-gray-200 dark:border-gray-700">
      <h3 class="text-sm font-medium text-gray-900 dark:text-white">Failed runs</h3>
      <BaseBadge v-if="pair" variant="neutral" data-testid="eval-failed-pair-chip">
        {{ pair.caller }} → {{ pair.callee }}
        <button
          type="button"
          class="ml-1 text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
          aria-label="Clear pair filter"
          data-testid="eval-failed-pair-clear"
          @click="$emit('clear-pair')"
        >×</button>
      </BaseBadge>
      <span class="ml-auto text-xs text-gray-500 dark:text-gray-400 tabular-nums" data-testid="eval-failed-total">
        {{ loaded ? `${items.length} of ${total}` : '' }}
      </span>
    </div>

    <div v-if="perCheck.length && !pair" class="flex flex-wrap gap-2 px-4 py-2 border-b border-gray-100 dark:border-gray-700" data-testid="eval-failed-checks">
      <BaseBadge v-for="c in perCheck" :key="c.id" variant="danger" data-testid="eval-failed-check">
        <span class="font-mono">{{ c.id }}</span>&nbsp;<span class="tabular-nums">{{ c.count }}</span>
      </BaseBadge>
    </div>

    <InlineError v-if="error" class="m-3" :message="error" retryable @retry="$emit('retry')" />

    <div class="max-h-[24rem] overflow-y-auto min-h-[4rem]">
      <p v-if="loaded && !items.length" class="px-4 py-6 text-sm text-center text-gray-500 dark:text-gray-400" data-testid="eval-failed-empty">
        No failed runs in this period.
      </p>
      <ul v-else class="divide-y divide-gray-100 dark:divide-gray-700">
        <li v-for="run in items" :key="run.execution_id" class="px-4 py-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm" data-testid="eval-failed-row">
          <router-link
            :to="{ name: 'ExecutionDetail', params: { name: run.agent_name, executionId: run.execution_id } }"
            class="text-action-primary-600 dark:text-action-primary-400 hover:underline"
            data-testid="eval-failed-link"
          >{{ run.agent_name }}</router-link>
          <span v-if="run.caller_agent" class="text-xs text-gray-500 dark:text-gray-400">from {{ run.caller_agent }}</span>
          <span class="flex flex-wrap gap-1" data-testid="eval-failed-why">
            <span v-if="!run.completion" class="text-xs text-status-danger-700 dark:text-status-danger-300">did not complete</span>
            <span v-for="c in run.failed_checks" :key="c.id" class="font-mono text-xs text-gray-700 dark:text-gray-300">
              {{ c.id }}<span v-if="c.reason" class="text-gray-500 dark:text-gray-400"> · {{ c.reason }}</span>
            </span>
          </span>
          <span class="ml-auto text-xs text-gray-500 dark:text-gray-400 tabular-nums">{{ formatLocalDateTime(run.completed_at) }}</span>
        </li>
      </ul>
    </div>

    <div v-if="hasMore || truncated" class="flex items-center gap-3 px-4 py-2 border-t border-gray-100 dark:border-gray-700">
      <BaseButton v-if="hasMore" variant="ghost" size="sm" :loading="loading" data-testid="eval-failed-more" @click="$emit('more')">
        Load more
      </BaseButton>
      <span v-if="truncated" class="text-xs text-gray-500 dark:text-gray-400" data-testid="eval-failed-truncated">
        Window capped at 5,000 runs — narrow the period to see every failure.
      </span>
    </div>
  </div>
</template>

<script setup>
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import InlineError from '../InlineError.vue'
import { formatLocalDateTime } from '../../utils/timestamps'

defineProps({
  items: { type: Array, required: true },
  total: { type: Number, default: 0 },
  truncated: { type: Boolean, default: false },
  hasMore: { type: Boolean, default: false },
  loaded: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
  error: { type: String, default: null },
  perCheck: { type: Array, default: () => [] },
  pair: { type: Object, default: null },
})
defineEmits(['more', 'retry', 'clear-pair'])
</script>
