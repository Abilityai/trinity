<template>
  <!-- Operations › Evaluations KPI row (trinity-enterprise#938 C2): the
       window's headline counts from the ent#937 aggregate, each with its
       change against the previous window of the same length. No judge or
       agreement tile until the calibration placement is decided. -->
  <div class="grid grid-cols-1 sm:grid-cols-3 gap-3" data-testid="eval-kpis">
    <div
      v-for="tile in kpis"
      :key="tile.id"
      class="min-w-0 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg px-4 py-3"
      :data-testid="`eval-kpi-${tile.id}`"
    >
      <p class="text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400">{{ tile.label }}</p>
      <p class="mt-1 text-2xl font-semibold text-gray-900 dark:text-gray-100 tabular-nums" data-testid="eval-kpi-value">
        {{ tile.rate ? fmtRate(tile.value) : tile.value }}
      </p>
      <p class="mt-0.5 text-xs tabular-nums" :class="tile.rate ? deltaTone(tile.delta) : 'text-gray-500 dark:text-gray-400'" data-testid="eval-kpi-delta">
        {{ fmtDelta(tile.delta, tile.rate ? 'pts' : '') }}
        <span class="text-gray-500 dark:text-gray-400">vs previous period</span>
      </p>
    </div>
  </div>
</template>

<script setup>
import { fmtRate, fmtDelta, deltaTone } from './format'

defineProps({
  /** `store.kpis` — `[{id, label, value, rate, delta}]`. */
  kpis: { type: Array, required: true },
})
</script>
