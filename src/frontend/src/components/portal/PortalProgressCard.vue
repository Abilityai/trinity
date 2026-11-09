<template>
  <!-- Member onboarding progress card (ent#1175).
       Visible in the Main conversation from acceptance until all five steps are done.
       Card footprint is fixed by the five rows so the thread below never shifts. -->
  <div
    v-if="shouldShow"
    class="mb-6 rounded-lg border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800 shadow-sm overflow-hidden"
    data-testid="portal-progress-card"
    aria-label="Your setup progress"
    role="region"
  >
    <!-- Header -->
    <div class="px-4 py-3 border-b border-gray-200 dark:border-gray-750">
      <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Getting set up</p>
      <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
        {{ doneCount }} of {{ steps.length }} steps complete
      </p>
    </div>

    <!-- Step rows -->
    <ul class="divide-y divide-gray-100 dark:divide-gray-750" role="list">
      <li
        v-for="step in steps"
        :key="step.key"
        class="flex items-start gap-3 px-4 py-3"
        :data-testid="`progress-step-${step.key}`"
      >
        <!-- State indicator -->
        <span
          class="mt-0.5 w-4 h-4 shrink-0 flex items-center justify-center rounded-full"
          :class="indicatorClass(step.state)"
          aria-hidden="true"
        >
          <!-- Done: checkmark -->
          <svg v-if="step.state === 'done'" viewBox="0 0 12 12" fill="none" class="w-2.5 h-2.5">
            <path d="M2 6l3 3 5-5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          <!-- In progress: dot pulse -->
          <span v-else-if="step.state === 'in_progress'" class="w-1.5 h-1.5 rounded-full bg-current motion-safe:animate-pulse"></span>
          <!-- Failed: × -->
          <svg v-else-if="step.state === 'failed'" viewBox="0 0 12 12" fill="none" class="w-2.5 h-2.5">
            <path d="M3 3l6 6M9 3l-6 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
          </svg>
          <!-- Pending: empty circle handled by ring class -->
        </span>

        <div class="min-w-0 flex-1">
          <p
            class="text-sm"
            :class="labelClass(step.state)"
          >{{ step.label }}</p>
          <p
            v-if="step.note || step.state === 'failed'"
            class="text-xs mt-0.5 text-gray-500 dark:text-gray-400"
          >{{ step.note || 'Something went wrong here. Your assistant is looking into it and will tell you in the chat.' }}</p>
        </div>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  steps: {
    type: Array,
    default: () => [],
  },
  allDone: {
    type: Boolean,
    default: false,
  },
  active: {
    type: Boolean,
    default: false,
  },
})

// Show the card while there is at least one step and not everything is done yet.
const shouldShow = computed(() => props.active && props.steps.length > 0 && !props.allDone)

const doneCount = computed(() => props.steps.filter((s) => s.state === 'done').length)

function indicatorClass(state) {
  if (state === 'done') {
    return 'bg-status-success-500 text-white'
  }
  if (state === 'in_progress') {
    return 'bg-action-primary-500 dark:bg-action-primary-500 text-white'
  }
  if (state === 'failed') {
    return 'bg-status-danger-500 text-white'
  }
  // pending
  return 'ring-1 ring-gray-300 dark:ring-gray-600 text-transparent'
}

function labelClass(state) {
  if (state === 'done') return 'text-status-success-700 dark:text-status-success-400'
  if (state === 'failed') return 'text-status-danger-700 dark:text-status-danger-400'
  if (state === 'in_progress') return 'text-gray-900 dark:text-gray-100 font-medium'
  return 'text-gray-500 dark:text-gray-400'
}
</script>
