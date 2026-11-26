<script setup>
/**
 * Overview › Now (trinity-enterprise#1317).
 *
 * One fixed-height row: what the agent is doing, when it next runs on a
 * schedule, how many things are waiting on a person, and the view's single
 * primary action. The same height for every agent and every state, so nothing
 * below it moves (design principle 30).
 */
import BaseCard from '../base/BaseCard.vue'
import BaseButton from '../base/BaseButton.vue'

defineProps({
  /** `nowState()` output, or null while nothing is known yet. */
  now: { type: Object, default: null },
  /** `nextRun()` output, or null. */
  next: { type: Object, default: null },
  /** 'loading' | 'failed' | 'empty' | 'ready' for the schedules read. */
  nextState: { type: String, default: 'loading' },
  attention: { type: Number, default: 0 },
})
const emit = defineEmits(['new-task', 'navigate', 'open-task'])

const GLYPH = {
  working: 'border-2 border-action-primary-500 dark:border-action-primary-400',
  queued: 'bg-status-warning-500',
  idle: 'bg-status-success-500',
  first: 'border-2 border-gray-300 dark:border-gray-600',
  stopped: 'bg-gray-400 dark:bg-gray-500',
}
</script>

<template>
  <BaseCard class="flex min-h-[72px] flex-wrap items-center gap-x-6 gap-y-3" data-testid="overview-now">
    <div class="flex min-w-0 flex-[2_1_320px] items-center gap-3">
      <span
        class="inline-block h-2.5 w-2.5 flex-none rounded-full"
        :class="now ? GLYPH[now.kind] : 'bg-gray-200 dark:bg-gray-700'"
        aria-hidden="true"
      ></span>
      <div class="min-w-0">
        <p class="text-[11px] font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">Now</p>
        <p v-if="now" class="truncate text-sm font-medium text-gray-900 dark:text-gray-100" data-testid="overview-now-state">
          <button
            v-if="now.executionId"
            type="button"
            class="max-w-full truncate rounded align-bottom hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
            :title="now.full || undefined"
            @click="emit('open-task', now.executionId)"
          >{{ now.title }}</button>
          <template v-else>{{ now.title }}</template>
          <span v-if="now.detail" class="text-[12.5px] font-normal tabular-nums text-gray-500 dark:text-gray-400"> · {{ now.detail }}</span>
        </p>
        <p v-else class="text-sm font-medium text-gray-500 dark:text-gray-400">—</p>
      </div>
    </div>

    <div class="min-w-0 flex-[1_1_200px]">
      <p class="text-[11px] font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">Next run</p>
      <p class="truncate text-sm text-gray-900 dark:text-gray-100" data-testid="overview-now-next">
        <template v-if="next">
          <span class="font-medium tabular-nums" :title="next.absolute">{{ next.label }}</span>
          <span class="text-[12.5px] text-gray-500 dark:text-gray-400"> · {{ next.name }}</span>
        </template>
        <template v-else-if="nextState === 'empty' || nextState === 'ready'">
          <span class="text-[12.5px] text-gray-500 dark:text-gray-400">Nothing scheduled · </span>
          <button
            type="button"
            class="rounded text-sm font-medium text-action-primary-600 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:text-action-primary-400"
            @click="emit('navigate', 'schedules')"
          >Add a schedule</button>
        </template>
        <span v-else class="text-gray-500 dark:text-gray-400">—</span>
      </p>
    </div>

    <router-link
      v-if="attention > 0"
      :to="{ path: '/operations' }"
      class="inline-flex h-8 items-center gap-2 whitespace-nowrap rounded-md border border-status-warning-200 bg-status-warning-50 px-3 text-[13.5px] font-medium text-status-warning-800 hover:bg-status-warning-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:border-status-warning-800 dark:bg-status-warning-900/30 dark:text-status-warning-300 dark:hover:bg-status-warning-900/50"
      data-testid="needs-attention"
    >
      <span class="inline-block h-2 w-2 rounded-full bg-status-warning-500" aria-hidden="true"></span>
      {{ attention }} {{ attention === 1 ? 'item needs' : 'items need' }} attention
    </router-link>

    <BaseButton type="button" variant="primary" data-testid="overview-new-task" @click="emit('new-task')">New task</BaseButton>
  </BaseCard>
</template>
