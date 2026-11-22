<script setup>
/**
 * Overview › Schedules (trinity-enterprise#1317).
 *
 * One row per schedule: what it runs and when, its next run, how its runs in
 * the window ended, and how long they take. The outcome bar is a share of the
 * window's FINISHED runs with the count beside it — the API has no per-run
 * history here, so the row never implies "the last ten".
 */
import BaseCard from '../base/BaseCard.vue'
import LoadFailed from '../LoadFailed.vue'
import SkeletonLoader from '../SkeletonLoader.vue'

defineProps({
  /** 'loading' | 'failed' | 'empty' | 'ready' — from `viewState`. */
  state: { type: String, required: true },
  /** `scheduleRows()` output. */
  rows: { type: Array, default: () => [] },
})
const emit = defineEmits(['navigate', 'retry'])
</script>

<template>
  <BaseCard data-testid="overview-schedules">
    <div class="flex items-center justify-between gap-3">
      <h3 class="text-sm font-semibold text-gray-900 dark:text-gray-100">Schedules</h3>
      <button
        v-if="state === 'ready'"
        type="button"
        class="rounded text-[12.5px] font-medium text-action-primary-600 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:text-action-primary-400"
        @click="emit('navigate', 'schedules')"
      >{{ rows.length === 1 ? 'Open schedule' : `All ${rows.length} schedules` }}</button>
    </div>

    <div v-if="state === 'loading'" class="mt-3" aria-busy="true">
      <span class="sr-only">Loading schedules…</span>
      <SkeletonLoader :count="3" height="2.5rem" gap="0.5rem" />
    </div>

    <LoadFailed
      v-else-if="state === 'failed'"
      class="mt-3"
      dense
      title="Couldn't load schedules"
      message="The schedule list did not load. This is not the same as an agent with no schedules."
      @retry="emit('retry')"
    />

    <div v-else-if="state === 'empty'" class="flex flex-col gap-1 pb-2 pt-6" data-testid="overview-schedules-empty">
      <p class="text-sm font-medium text-gray-900 dark:text-gray-100">No schedules yet</p>
      <p class="text-[12.5px] text-gray-500 dark:text-gray-400">
        A schedule runs one of this agent's playbooks on a timer. Each one gets a row here with its next run and how its runs have gone.
      </p>
      <p class="mt-2">
        <button
          type="button"
          class="rounded text-sm font-medium text-action-primary-600 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:text-action-primary-400"
          @click="emit('navigate', 'schedules')"
        >Add a schedule</button>
      </p>
    </div>

    <!-- Bounded: an agent with many schedules scrolls inside the card. -->
    <ul v-else class="mt-1 max-h-[340px] divide-y divide-gray-200 overflow-y-auto dark:divide-gray-750">
      <li v-for="row in rows" :key="row.id">
        <button
          type="button"
          class="flex w-full flex-wrap items-center gap-x-4 gap-y-1 rounded px-1 py-2.5 text-left hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:hover:bg-gray-750"
          :title="`Open ${row.name} in the Schedules tab`"
          data-testid="overview-schedule-row"
          @click="emit('navigate', 'schedules')"
        >
          <span class="min-w-0 flex-[1_1_200px]">
            <span class="block truncate text-sm font-medium text-gray-900 dark:text-gray-100">{{ row.command || row.name }}</span>
            <span class="block truncate font-mono text-[12.5px] text-gray-500 dark:text-gray-400">{{ row.name }} · {{ row.cron }}</span>
          </span>
          <span class="w-24 flex-none">
            <span class="block text-sm font-medium tabular-nums text-gray-900 dark:text-gray-100" :title="row.nextAbsolute || undefined">{{ row.next }}</span>
            <span class="block text-[11px] text-gray-500 dark:text-gray-400">next run</span>
          </span>
          <span class="w-36 flex-none">
            <!-- Finished vs failed, split by a surface gap; a dashed track when nothing has finished. -->
            <span
              v-if="row.hasOutcome"
              class="flex h-2 gap-[2px]"
              role="img"
              :aria-label="row.score"
            >
              <i v-if="row.okPct > 0" class="block h-2 rounded-sm bg-status-success-500" :style="{ width: row.okPct + '%' }"></i>
              <i v-if="row.failed" class="block h-2 min-w-[4px] flex-1 rounded-sm bg-status-danger-500"></i>
            </span>
            <span v-else class="block h-2 rounded-sm border border-dashed border-gray-300 dark:border-gray-600" aria-hidden="true"></span>
            <span class="mt-1 block text-[11px] tabular-nums text-gray-500 dark:text-gray-400">{{ row.score }}</span>
          </span>
          <span class="hidden w-16 flex-none text-right sm:block">
            <span class="block text-sm tabular-nums text-gray-900 dark:text-gray-100">{{ row.avg }}</span>
            <span class="block text-[11px] text-gray-500 dark:text-gray-400">avg</span>
          </span>
        </button>
      </li>
    </ul>
  </BaseCard>
</template>
