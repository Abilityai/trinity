<!--
  The levels on the Company page (core-agent AC-15 to AC-17): seven new abilities, each
  ticked from what is really on the instance (`levels.js`). The next one says what it
  means and why it matters, with one button that puts a request to the guide in the
  composer. Level 3 (keys) shows as not needed when no agent needs keys. Reaching one
  gets a short celebration here; the page also tells the guide.
-->
<template>
  <BaseCard data-testid="build-levels" data-tour="levels">
    <div class="flex items-center justify-between gap-2">
      <p class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Your levels</p>
      <p class="text-xs font-medium tabular-nums text-gray-600 dark:text-gray-300" data-testid="build-levels-count">{{ reachedCount }} of {{ levels.length }}</p>
    </div>

    <Transition
      enter-active-class="motion-safe:transition motion-safe:duration-300 motion-safe:ease-out"
      enter-from-class="motion-safe:opacity-0 motion-safe:scale-95"
    >
      <div
        v-if="celebrate"
        role="status"
        class="mt-3 flex items-start gap-3 rounded-lg border border-status-success-200 dark:border-status-success-900/50 bg-status-success-50 dark:bg-status-success-900/20 p-3"
        data-testid="build-level-celebrate"
      >
        <span class="shrink-0 w-7 h-7 rounded-full bg-status-success-600 text-white flex items-center justify-center text-xs font-semibold tabular-nums" aria-hidden="true">{{ celebrate.id }}</span>
        <div class="min-w-0 flex-1">
          <p class="text-sm font-semibold text-status-success-800 dark:text-status-success-300">Level {{ celebrate.id }} reached: {{ celebrate.name }}</p>
          <p class="mt-0.5 text-xs text-status-success-700 dark:text-status-success-300">{{ celebrate.means }}</p>
        </div>
        <button
          type="button"
          class="shrink-0 -m-1 p-1 rounded text-status-success-700 dark:text-status-success-300 hover:bg-status-success-100 dark:hover:bg-status-success-900/30"
          aria-label="Close"
          data-testid="build-level-celebrate-dismiss"
          @click="$emit('dismiss')"
        >
          <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" /></svg>
        </button>
      </div>
    </Transition>

    <ol class="mt-3 space-y-1.5">
      <li
        v-for="level in levels"
        :key="level.id"
        :data-testid="`build-level-${level.id}`"
        :data-reached="String(level.reached)"
        :data-skipped="String(Boolean(level.skipped))"
        :aria-current="next && next.id === level.id ? 'step' : undefined"
      >
        <div class="flex items-center gap-2.5">
          <span
            class="shrink-0 w-5 h-5 rounded-full flex items-center justify-center text-[11px] font-semibold tabular-nums"
            :class="level.reached
              ? 'bg-status-success-600 text-white'
              : level.skipped
                ? 'bg-gray-200 dark:bg-gray-750 text-gray-500 dark:text-gray-400'
                : next && next.id === level.id
                  ? 'bg-action-primary-600 dark:bg-action-primary-500 text-white'
                  : 'border border-gray-300 dark:border-gray-700 text-gray-500 dark:text-gray-400'"
          >
            <svg v-if="level.reached || level.skipped" class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" /></svg>
            <template v-else>{{ level.id }}</template>
          </span>
          <span
            class="flex-1 min-w-0 truncate text-sm"
            :class="level.reached || (next && next.id === level.id) ? 'font-medium text-gray-900 dark:text-gray-100' : 'text-gray-500 dark:text-gray-400'"
          >{{ level.name }}</span>
          <span v-if="level.reached" class="shrink-0 text-xs text-status-success-700 dark:text-status-success-400">Reached</span>
          <span v-else-if="level.skipped" class="shrink-0 text-xs text-gray-500 dark:text-gray-400" title="None of your agents needs keys">Not needed</span>
        </div>

        <div v-if="next && next.id === level.id" class="mt-2 ml-7 rounded-lg bg-gray-50 dark:bg-gray-900 p-3" data-testid="build-level-next">
          <p class="text-xs text-gray-700 dark:text-gray-200">{{ level.means }}</p>
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">{{ level.why }}</p>
          <BaseButton size="sm" class="mt-2.5" data-testid="build-level-ask" @click="$emit('ask', level.prompt)">Do it with {{ guideLabel }}</BaseButton>
        </div>
      </li>
    </ol>
  </BaseCard>
</template>

<script setup>
import { computed } from 'vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseCard from '@/components/base/BaseCard.vue'
import { nextLevel } from './levels'

const props = defineProps({
  levels: { type: Array, required: true },
  celebrate: { type: Object, default: null },
  guideLabel: { type: String, default: 'Atlas' },
})
defineEmits(['ask', 'dismiss'])

// A level that isn't needed (Level 3 with no keys to set) counts as done.
const reachedCount = computed(() => props.levels.filter((l) => l.reached || l.skipped).length)
const next = computed(() => nextLevel(props.levels))
</script>
