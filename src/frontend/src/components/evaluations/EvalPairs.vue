<template>
  <!-- Operations › Evaluations delegated pairs (trinity-enterprise#938 C3,
       numbers from ent#940): caller → callee from the aggregate `by=pair` —
       the EDGE's graded count, its pair-check pass rate and the check that
       fails it most (the card is a third of the page: round trips and cost per
       verified outcome stay on the store for the matrix). A row narrows the
       failed list to that edge. The full delegation matrix is a later issue. -->
  <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg" data-testid="eval-pairs">
    <div class="flex items-center justify-between px-4 py-2 border-b border-gray-200 dark:border-gray-700">
      <h3 class="text-sm font-medium text-gray-900 dark:text-white">Delegated pairs</h3>
      <span class="text-xs text-gray-500 dark:text-gray-400 tabular-nums" data-testid="eval-pairs-total">
        {{ rows.length }} {{ rows.length === 1 ? 'pair' : 'pairs' }}
      </span>
    </div>
    <InlineError v-if="error" class="m-3" :message="error" retryable @retry="$emit('retry')" />
    <p v-if="!rows.length && !error" class="px-4 py-6 text-sm text-center text-gray-500 dark:text-gray-400" data-testid="eval-pairs-empty">
      No delegated runs graded in this period.
    </p>
    <div v-else-if="rows.length" class="max-h-[20rem] overflow-y-auto">
      <table class="w-full text-sm">
        <thead class="sticky top-0 bg-gray-50 dark:bg-gray-800 text-xs text-gray-500 dark:text-gray-400">
          <tr>
            <th scope="col" class="px-4 py-2 text-left font-medium">Caller → callee</th>
            <th scope="col" class="px-4 py-2 text-right font-medium">Graded</th>
            <th scope="col" class="px-4 py-2 text-right font-medium">Pair checks passed</th>
            <th scope="col" class="px-4 py-2 text-left font-medium">Top failed check</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-100 dark:divide-gray-700">
          <tr v-for="row in rows" :key="`${row.caller}>${row.callee}`" data-testid="eval-pair-row">
            <td class="px-4 py-2 whitespace-nowrap">
              <button
                type="button"
                class="text-action-primary-600 dark:text-action-primary-400 hover:underline"
                :aria-label="`Show failed runs from ${row.caller} to ${row.callee}`"
                data-testid="eval-pair-select"
                @click="$emit('select', { caller: row.caller, callee: row.callee })"
              >{{ row.caller }} → {{ row.callee }}</button>
            </td>
            <td class="px-4 py-2 text-right tabular-nums text-gray-700 dark:text-gray-300" data-testid="eval-pair-graded">{{ row.graded }}</td>
            <td class="px-4 py-2 text-right tabular-nums text-gray-700 dark:text-gray-300" data-testid="eval-pair-passed">{{ fmtRate(row.checksPassedRate) }}</td>
            <td class="px-4 py-2 text-xs text-gray-700 dark:text-gray-300" data-testid="eval-pair-top-check">
              <template v-if="row.topFailedCheck">
                <span class="font-mono">{{ row.topFailedCheck.id }}</span>
                <span class="ml-1 tabular-nums text-gray-500 dark:text-gray-400">×{{ row.topFailedCheck.count }}</span>
              </template>
              <span v-else class="text-gray-500 dark:text-gray-400">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import InlineError from '../InlineError.vue'
import { fmtRate } from './format'

defineProps({
  /** `store.pairRows` — `[{caller, callee, graded, checksPassedRate, topFailedCheck, roundTrips, costPerVerified}]`. */
  rows: { type: Array, required: true },
  error: { type: String, default: null },
})
defineEmits(['select', 'retry'])
</script>
