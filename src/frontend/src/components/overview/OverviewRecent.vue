<script setup>
/**
 * Overview › Recent work (trinity-enterprise#1317).
 *
 * The last five runs: outcome (shape and colour, never colour alone), what was
 * asked, when, how long, and what started it. Empty names what will appear and
 * the one next action.
 */
import BaseCard from '../base/BaseCard.vue'
import BaseBadge from '../base/BaseBadge.vue'
import LoadFailed from '../LoadFailed.vue'
import SkeletonLoader from '../SkeletonLoader.vue'

defineProps({
  /** 'loading' | 'failed' | 'empty' | 'ready' — from `viewState`. */
  state: { type: String, required: true },
  /** `recentRows()` output. */
  rows: { type: Array, default: () => [] },
})
const emit = defineEmits(['open-task', 'navigate', 'retry'])

// Outcome is carried by shape as well as hue: dot, diamond, ring, hollow dot.
const GLYPH = {
  finished: 'rounded-full bg-status-success-500',
  failed: 'rotate-45 rounded-[1px] bg-status-danger-500',
  running: 'rounded-full border-2 border-action-primary-500 dark:border-action-primary-400',
  queued: 'rounded-full border-2 border-status-warning-500',
}
const glyph = (status) => GLYPH[status] || 'rounded-full bg-gray-400 dark:bg-gray-500'
</script>

<template>
  <BaseCard class="flex flex-col" data-testid="overview-recent">
    <div class="flex items-center justify-between gap-3">
      <h3 class="text-sm font-semibold text-gray-900 dark:text-gray-100">Recent work</h3>
      <button
        v-if="state === 'ready'"
        type="button"
        class="rounded text-[12.5px] font-medium text-action-primary-600 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:text-action-primary-400"
        @click="emit('navigate', 'tasks')"
      >All tasks</button>
    </div>

    <div v-if="state === 'loading'" class="mt-3" aria-busy="true">
      <span class="sr-only">Loading recent work…</span>
      <SkeletonLoader :count="5" height="2.25rem" gap="0.5rem" />
    </div>

    <LoadFailed
      v-else-if="state === 'failed'"
      class="mt-3"
      dense
      title="Couldn't load recent work"
      message="The run list did not load. This is not the same as an agent with no runs."
      @retry="emit('retry')"
    />

    <div v-else-if="state === 'empty'" class="flex flex-1 flex-col justify-center gap-1 py-6" data-testid="overview-recent-empty">
      <p class="text-sm font-medium text-gray-900 dark:text-gray-100">No work yet</p>
      <p class="text-[12.5px] text-gray-500 dark:text-gray-400">
        The last five things this agent did will be listed here, each with what started it and how long it took.
      </p>
      <p class="mt-2">
        <button
          type="button"
          class="rounded text-sm font-medium text-action-primary-600 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:text-action-primary-400"
          @click="emit('navigate', 'tasks')"
        >Give it a first task</button>
      </p>
    </div>

    <ul v-else class="mt-1 divide-y divide-gray-200 dark:divide-gray-750">
      <li v-for="row in rows" :key="row.id">
        <button
          type="button"
          class="-mx-1 flex w-[calc(100%+0.5rem)] items-center gap-3 rounded px-1 py-2.5 text-left hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:hover:bg-gray-750"
          data-testid="overview-recent-row"
          @click="emit('open-task', row.id)"
        >
          <span class="inline-block h-2.5 w-2.5 flex-none" :class="glyph(row.status)" role="img" :aria-label="row.status"></span>
          <span class="min-w-0 flex-1">
            <span class="block truncate text-sm text-gray-900 dark:text-gray-100">{{ row.title }}</span>
            <span class="block text-[12.5px] tabular-nums text-gray-500 dark:text-gray-400" :title="row.absolute">{{ row.when }} · {{ row.took }}</span>
          </span>
          <BaseBadge v-if="row.via" variant="neutral" size="sm" class="flex-none">{{ row.via }}</BaseBadge>
        </button>
      </li>
    </ul>
  </BaseCard>
</template>
