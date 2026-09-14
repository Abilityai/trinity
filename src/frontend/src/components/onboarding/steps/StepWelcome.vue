<!--
  Welcome (ent#581 AC 2): says what is about to be configured and roughly how
  long it takes, before the operator agrees to any of it — no surprise chain of
  dialogs. The constellation's four nodes are those capabilities (hollow =
  still to do); the manifest beside it is the rail's own set, in order.
-->
<template>
  <div data-testid="first-run-step-welcome">
    <FirstRunStepHeader kicker="Welcome" :title="title" :lead="lead" />

    <div class="mt-6 flex items-center gap-8 max-sm:flex-col max-sm:items-start max-sm:gap-4">
      <FirstRunConstellation :lit="lit" />

      <ul class="flex min-w-0 flex-1 flex-col gap-2.5" data-testid="first-run-manifest">
        <li v-for="s in steps" :key="s.key" class="flex items-start gap-2.5">
          <span
            class="mt-0.5 flex h-4 w-4 flex-none items-center justify-center rounded-full border-[1.5px]"
            :class="states[s.key] === 'done'
              ? 'border-status-success-500 bg-status-success-500 text-white'
              : 'border-gray-300 dark:border-gray-600'"
            aria-hidden="true"
          >
            <svg v-if="states[s.key] === 'done'" class="h-2.5 w-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="3.5" d="M5 13l4 4L19 7" />
            </svg>
          </span>
          <span class="min-w-0">
            <span class="block text-sm text-gray-900 dark:text-gray-100">{{ s.name }}</span>
            <span class="block text-[12.5px] text-gray-500 dark:text-gray-400">
              {{ states[s.key] === 'done' ? 'Done' : `${s.tag} · about ${s.minutes} min` }}
            </span>
          </span>
        </li>
      </ul>
    </div>

    <p class="mt-6 text-[12.5px] text-gray-500 dark:text-gray-400">
      Governed · Auditable · Your infrastructure
    </p>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import FirstRunStepHeader from '../FirstRunStepHeader.vue'
import FirstRunConstellation from '../FirstRunConstellation.vue'
import { estimateMinutes } from '../firstRunSteps'

const props = defineProps({
  steps: { type: Array, required: true },
  states: { type: Object, required: true },
  lit: { type: Array, default: () => [] },
})

const todo = computed(() => props.steps.filter((s) => props.states[s.key] !== 'done'))

const title = computed(() =>
  todo.value.length ? 'Set up this instance' : 'This instance is set up'
)

const lead = computed(() => {
  const n = todo.value.length
  if (!n) return 'Everything below is already configured. Walk through it again to change anything.'
  const things = n === 1 ? 'One thing' : `${n} things`
  const required = todo.value.some((s) => s.required)
    ? ' Connecting Claude is required — agents cannot think without it.'
    : ''
  return `${things} to set up, about ${estimateMinutes(todo.value)} minutes.${required} Everything else can be skipped, and each step says where to find it later.`
})
</script>
