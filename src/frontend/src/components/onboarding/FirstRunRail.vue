<!--
  The first-run rail (ent#581 spec comment 2). It renders the ACTUAL step set
  for this install — steps are conditional, so "Step 3 of 6" alone would be a
  lie where two do not apply — with required-vs-optional legible from the start,
  so the size of the commitment is visible before the operator agrees to it.

  Under `sm` the list and "Finish later" hide; the segments and the progress
  label remain (the overlay footer carries "Finish later" there).
-->
<template>
  <aside
    class="flex flex-col border-b sm:border-b-0 sm:border-r border-gray-200 dark:border-gray-750
           bg-gray-100 dark:bg-gray-750 px-4 pb-4 pt-5 max-sm:py-3"
  >
    <div class="mb-1 flex items-center gap-2.5">
      <TrinityMark class="h-[26px] w-[26px] flex-none text-action-primary-600 dark:text-action-primary-500" />
      <span class="text-sm font-[550] text-gray-900 dark:text-gray-100">Set up Trinity</span>
    </div>

    <p class="mb-4 text-[12.5px] tabular-nums text-gray-500 dark:text-gray-400 max-sm:mb-2" data-testid="first-run-progress">
      {{ progressLabel }}
    </p>

    <!-- Segments, one per APPLICABLE step — so the bar cannot promise a step
         this install does not have. Lit = done. -->
    <div class="mb-5 flex gap-[3px] max-sm:mb-0" aria-hidden="true">
      <i
        v-for="s in steps"
        :key="s.key"
        class="h-[3px] flex-1 rounded-sm"
        :class="states[s.key] === 'done'
          ? 'bg-action-primary-600 dark:bg-action-primary-500'
          : 'bg-gray-300 dark:bg-gray-700'"
      />
    </div>

    <nav aria-label="Setup steps" class="max-sm:hidden">
      <ul class="flex flex-col gap-0.5">
        <li v-for="s in steps" :key="s.key">
          <!-- Colour lives on mutually exclusive arms, never a static class
               plus an override (design-system contract, #2662). -->
          <button
            type="button"
            class="flex w-full items-start gap-2.5 rounded-md px-2 py-2 text-left text-[13px] leading-[1.35]
                   focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40
                   dark:focus-visible:ring-action-primary-400/40 disabled:cursor-default"
            :class="states[s.key] === 'current'
              ? 'bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 shadow-sm'
              : 'text-gray-500 dark:text-gray-400 enabled:hover:bg-gray-200/60 dark:enabled:hover:bg-gray-700/50'"
            :disabled="!reachable.includes(s.key)"
            :aria-current="states[s.key] === 'current' ? 'step' : undefined"
            :data-state="states[s.key]"
            :data-testid="`first-run-rail-${s.key}`"
            @click="$emit('go', s.key)"
          >
            <!-- Identity is shape AND colour, never hue alone (contract p.24):
                 filled check = done, filled dot = current, hollow = upcoming,
                 dashed = skipped. -->
            <span
              class="mt-px flex h-4 w-4 flex-none items-center justify-center rounded-full border-[1.5px]"
              :class="{
                'border-status-success-500 bg-status-success-500 text-white': states[s.key] === 'done',
                'border-action-primary-600 bg-action-primary-600 dark:border-action-primary-500 dark:bg-action-primary-500': states[s.key] === 'current',
                'border-dashed border-gray-400 dark:border-gray-600': states[s.key] === 'skipped',
                'border-gray-300 dark:border-gray-600': states[s.key] === 'upcoming',
              }"
              aria-hidden="true"
            >
              <svg v-if="states[s.key] === 'done'" class="h-2.5 w-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="3.5" d="M5 13l4 4L19 7" />
              </svg>
              <span v-else-if="states[s.key] === 'current'" class="h-1.5 w-1.5 rounded-full bg-white"></span>
            </span>

            <span class="min-w-0">
              <span class="block" :class="states[s.key] === 'current' ? 'font-[550]' : ''">{{ s.name }}</span>
              <span class="mt-px block text-[11px] text-gray-500 dark:text-gray-400">
                {{ subline(s) }}
              </span>
            </span>
          </button>
        </li>
      </ul>
    </nav>

    <div class="mt-auto pt-4 max-sm:hidden">
      <button
        type="button"
        class="rounded text-[12.5px] text-gray-500 underline underline-offset-2
               hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100
               focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
        data-testid="first-run-finish-later"
        @click="$emit('close')"
      >
        Finish later
      </button>
    </div>
  </aside>
</template>

<script setup>
import { computed } from 'vue'
import TrinityMark from '../TrinityMark.vue'

const props = defineProps({
  steps: { type: Array, required: true },
  // { [key]: 'current' | 'done' | 'skipped' | 'upcoming' } — from `stepState`.
  states: { type: Object, required: true },
  // Keys the operator may jump to: already visited, never past a required step
  // that is not satisfied yet.
  reachable: { type: Array, default: () => [] },
})
defineEmits(['go', 'close'])

const doneCount = computed(() => props.steps.filter((s) => props.states[s.key] === 'done').length)
const progressLabel = computed(() => `${doneCount.value} of ${props.steps.length} done`)

const subline = (s) => {
  if (props.states[s.key] === 'skipped') return `Skipped — ${s.settingsPath}`
  if (props.states[s.key] === 'done') return 'Done'
  return s.tag
}
</script>
