<!--
  Done (ent#581). The constellation turns into a progress report: completed
  capabilities lit, skipped ones still hollow. Anything left undone is listed
  with where it lives, so a skip is never a dead end (AC 4 / AC 10).
-->
<template>
  <div data-testid="first-run-step-done">
    <FirstRunStepHeader kicker="Done" :title="title" :lead="lead" />

    <div class="mt-6 flex items-center gap-8 max-sm:flex-col max-sm:items-start max-sm:gap-4">
      <FirstRunConstellation :lit="lit" />

      <div class="min-w-0 flex-1 space-y-3">
        <ul v-if="left.length" class="space-y-2" data-testid="first-run-left-for-later">
          <li v-for="s in left" :key="s.key" class="text-sm text-gray-600 dark:text-gray-300">
            <span class="text-gray-900 dark:text-gray-100">{{ s.name }}</span>
            &mdash; later in {{ s.settingsPath }}
          </li>
        </ul>
        <p v-if="nextLabel" class="text-sm text-gray-600 dark:text-gray-300" data-testid="first-run-next-step">
          {{ nextLabel }}
        </p>
        <p class="text-[12.5px] text-gray-500 dark:text-gray-400">
          Settings → General → Re-run setup brings this sequence back any time.
        </p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import FirstRunStepHeader from '../FirstRunStepHeader.vue'
import FirstRunConstellation from '../FirstRunConstellation.vue'

const props = defineProps({
  steps: { type: Array, required: true },
  states: { type: Object, required: true },
  lit: { type: Array, default: () => [] },
  // What "Done" does next, when a step chose it (e.g. "Finishing opens cornelius's chat.").
  nextLabel: { type: String, default: '' },
})

const left = computed(() => props.steps.filter((s) => props.states[s.key] !== 'done'))

const title = computed(() =>
  left.value.length ? `Set up — ${left.value.length} left for later` : 'Trinity is set up'
)

const lead = computed(() => {
  const done = props.steps.length - left.value.length
  return `${done} of ${props.steps.length} configured. Governed, auditable, and running on your own infrastructure.`
})
</script>
