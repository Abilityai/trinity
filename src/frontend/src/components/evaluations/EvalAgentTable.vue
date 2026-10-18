<template>
  <!-- Operations › Evaluations per-agent table (trinity-enterprise#938 C2).
       Sortable, bounded viewport (principle: unbounded data never grows the
       page), stated total. Selecting an agent narrows the whole tab to it. -->
  <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg" data-testid="eval-agent-table">
    <div class="flex items-center justify-between px-4 py-2 border-b border-gray-200 dark:border-gray-700">
      <h3 class="text-sm font-medium text-gray-900 dark:text-white">By agent</h3>
      <span class="text-xs text-gray-500 dark:text-gray-400 tabular-nums" data-testid="eval-agent-total">
        {{ rows.length }} {{ rows.length === 1 ? 'agent' : 'agents' }}
      </span>
    </div>
    <div class="max-h-[28rem] overflow-y-auto">
      <table class="w-full text-sm">
        <thead class="sticky top-0 bg-gray-50 dark:bg-gray-800 text-xs text-gray-500 dark:text-gray-400">
          <tr>
            <th
              v-for="col in COLUMNS"
              :key="col.id"
              scope="col"
              class="px-4 py-2 font-medium"
              :class="col.numeric ? 'text-right' : 'text-left'"
              :aria-sort="sortKey === col.id ? (sortDir === 1 ? 'ascending' : 'descending') : 'none'"
            >
              <button
                v-if="col.sortable"
                type="button"
                class="hover:text-gray-700 dark:hover:text-gray-200"
                :data-testid="`eval-sort-${col.id}`"
                @click="sortBy(col.id)"
              >{{ col.label }}<span v-if="sortKey === col.id" aria-hidden="true">{{ sortDir === 1 ? ' ↑' : ' ↓' }}</span></button>
              <span v-else>{{ col.label }}</span>
            </th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-100 dark:divide-gray-700">
          <tr v-for="row in sorted" :key="row.agent" data-testid="eval-agent-row" :data-agent="row.agent">
            <td class="px-4 py-2">
              <button
                type="button"
                class="text-action-primary-600 dark:text-action-primary-400 hover:underline"
                :aria-label="`Show only ${row.agent}`"
                data-testid="eval-agent-select"
                @click="$emit('select', row.agent)"
              >{{ row.agent }}</button>
            </td>
            <td class="px-4 py-2 text-right tabular-nums text-gray-700 dark:text-gray-300">{{ row.evaluated }}</td>
            <td class="px-4 py-2 text-right tabular-nums text-gray-700 dark:text-gray-300">{{ fmtRate(row.completionRate) }}</td>
            <td class="px-4 py-2 text-right tabular-nums text-gray-700 dark:text-gray-300" data-testid="eval-agent-passed">{{ fmtRate(row.checksPassedRate) }}</td>
            <td class="px-4 py-2 text-right tabular-nums" :class="deltaTone(row.delta)" data-testid="eval-agent-delta">{{ fmtDelta(row.delta) }}</td>
            <td class="px-4 py-2 text-gray-700 dark:text-gray-300" data-testid="eval-agent-topcheck">
              <span v-if="row.topFailedCheck" class="font-mono text-xs">{{ row.topFailedCheck.id }} <span class="text-gray-500 dark:text-gray-400">×{{ row.topFailedCheck.count }}</span></span>
              <span v-else class="text-gray-500 dark:text-gray-400">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { fmtRate, fmtDelta, deltaTone } from './format'

const props = defineProps({
  /** `store.agentRows` — already in the default (worst-first) order. */
  rows: { type: Array, required: true },
})
defineEmits(['select'])

const COLUMNS = [
  { id: 'agent', label: 'Agent', sortable: true },
  { id: 'evaluated', label: 'Evaluated', sortable: true, numeric: true },
  { id: 'completionRate', label: 'Completed', sortable: true, numeric: true },
  { id: 'checksPassedRate', label: 'Checks passed', sortable: true, numeric: true },
  { id: 'delta', label: 'Δ vs previous', sortable: true, numeric: true },
  { id: 'topFailedCheck', label: 'Top failed check' },
]

// `null` sort key = the store's own order. A rate of `null` (nothing graded)
// always sorts last, whichever direction.
const sortKey = ref(null)
const sortDir = ref(1)

function sortBy(id) {
  if (sortKey.value === id) sortDir.value = -sortDir.value
  else { sortKey.value = id; sortDir.value = 1 }
}

const sorted = computed(() => {
  if (!sortKey.value) return props.rows
  const k = sortKey.value
  return [...props.rows].sort((a, b) => {
    const va = a[k]
    const vb = b[k]
    if (va == null && vb == null) return a.agent.localeCompare(b.agent)
    if (va == null) return 1
    if (vb == null) return -1
    const c = typeof va === 'string' ? va.localeCompare(vb) : va - vb
    return c * sortDir.value || a.agent.localeCompare(b.agent)
  })
})
</script>
