<script setup>
/**
 * Overview › Reported by this agent (trinity-enterprise#1317).
 *
 * The agent's own declared metrics as plain tiles — the numbers that describe
 * its job, read once. The Dashboard tab owns the full surface (charts, windows,
 * declaring and retiring); this is the glance, and it links there.
 *
 * With nothing declared the section keeps its row: one dashed card saying what
 * will appear, beside dashed placeholder tiles.
 */
import { computed } from 'vue'
import BaseCard from '../base/BaseCard.vue'
import LoadFailed from '../LoadFailed.vue'
import SkeletonLoader from '../SkeletonLoader.vue'
import { formatMetricValue, metricUnitSuffix } from '@/utils/metricFormat'

const props = defineProps({
  /** 'loading' | 'failed' | 'empty' | 'ready' — from `viewState`. */
  state: { type: String, required: true },
  /** The metrics read's `metrics` list. */
  metrics: { type: Array, default: () => [] },
  /** Whether this viewer can declare metrics (owners; the Dashboard tab is their door). */
  canOpenDashboard: { type: Boolean, default: true },
})
const emit = defineEmits(['navigate', 'retry'])

const MAX_TILES = 8
const live = computed(() => props.metrics.filter((m) => m?.status !== 'retired'))
const shown = computed(() => live.value.slice(0, MAX_TILES).map((m) => {
  const value = m.latest?.value
  return {
    key: m.name,
    label: m.label || m.name,
    value: value == null ? '—' : m.type === 'status' ? String(value) : formatMetricValue(value, m.type),
    unit: value == null || m.type === 'status' ? '' : metricUnitSuffix(value, m.type, m.unit) || '',
    measured: value != null,
    description: m.description || '',
  }
}))
const hidden = computed(() => Math.max(0, live.value.length - MAX_TILES))
</script>

<template>
  <section data-testid="overview-reported">
    <div class="mb-3 mt-2 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
      <h3 class="text-sm font-semibold text-gray-900 dark:text-gray-100">Reported by this agent</h3>
      <button
        v-if="state === 'ready'"
        type="button"
        class="rounded text-[12.5px] font-medium text-action-primary-600 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:text-action-primary-400"
        @click="emit('navigate', 'dashboard')"
      >{{ hidden ? `All ${live.length} on Dashboard` : 'Open Dashboard' }}</button>
    </div>

    <div v-if="state === 'loading'" class="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-4" aria-busy="true">
      <span class="sr-only">Loading the agent's metrics…</span>
      <BaseCard v-for="i in 4" :key="i" class="min-h-[98px]"><SkeletonLoader :count="2" height="1.25rem" gap="0.75rem" /></BaseCard>
    </div>

    <LoadFailed
      v-else-if="state === 'failed'"
      dense
      title="Couldn't load the agent's metrics"
      message="The metric store did not answer. This is not the same as an agent with no metrics."
      @retry="emit('retry')"
    />

    <div v-else-if="state === 'empty'" class="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-4" data-testid="overview-reported-empty">
      <div class="min-h-[98px] rounded-lg border border-dashed border-gray-300 p-4 sm:col-span-2 dark:border-gray-600">
        <p class="text-sm font-medium text-gray-900 dark:text-gray-100">This agent reports no numbers of its own yet</p>
        <p class="mt-1 text-[12.5px] text-gray-500 dark:text-gray-400">
          An agent can publish the figures that describe its job, such as leads qualified or tickets closed. Once it does, they appear here as tiles.
        </p>
        <p v-if="canOpenDashboard" class="mt-2">
          <button
            type="button"
            class="rounded text-sm font-medium text-action-primary-600 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:text-action-primary-400"
            @click="emit('navigate', 'dashboard')"
          >Declare a metric on Dashboard</button>
        </p>
      </div>
      <div v-for="i in 2" :key="i" class="hidden min-h-[98px] rounded-lg border border-dashed border-gray-300 p-4 sm:block dark:border-gray-600" aria-hidden="true">
        <p class="text-[11px] font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">Metric</p>
        <p class="mt-1 text-2xl font-bold text-gray-500 dark:text-gray-400">—</p>
      </div>
    </div>

    <div v-else class="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-4">
      <BaseCard v-for="m in shown" :key="m.key" class="min-h-[98px]" data-testid="overview-reported-tile" :title="m.description || undefined">
        <p class="truncate text-[11px] font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">{{ m.label }}</p>
        <p
          class="mt-1 truncate text-2xl font-bold tabular-nums"
          :class="m.measured ? 'text-gray-900 dark:text-gray-100' : 'text-gray-500 dark:text-gray-400'"
        >{{ m.value }}<span v-if="m.unit" class="ml-1 text-sm font-medium text-gray-500 dark:text-gray-400">{{ m.unit }}</span></p>
        <p v-if="!m.measured" class="text-[12.5px] text-gray-500 dark:text-gray-400">not reported yet</p>
      </BaseCard>
    </div>
  </section>
</template>
