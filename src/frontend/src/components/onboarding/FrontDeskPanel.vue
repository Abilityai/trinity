<!--
  First-run front desk (ent#319, epic ent#54).

  The three doors into Trinity, shown on a seed-only install: **Show me**
  (watch a seeded agent work — zero commitment), **Make me one** (the ent#52
  one-question wizard), and **Bring mine** (an existing fleet), which is
  deliberately a de-emphasised secondary line rather than a button. That last
  placement is the issue's own instruction: fleet migration is our strongest
  capability and our worst first impression, asking the most from the person
  who knows us least.

  The front desk is a SURFACE, not an agent. ent#319 was filed proposing an
  agent that replaced the seeded fleet; that premise was reversed when ent#322
  was closed not-planned — a fresh install does roll out an agentic system, and
  which agents it contains belongs to ent#137. What survives is the routing:
  the user meets a running fleet with no way in, which is what this fixes.
-->
<template>
  <div
    v-if="store.visible"
    data-testid="front-desk"
    class="mx-4 mt-3 mb-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 shadow-sm"
  >
    <div class="flex items-start justify-between px-4 pt-3">
      <div class="min-w-0">
        <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">Start here</h3>
        <p class="text-xs text-gray-500 dark:text-gray-400">
          Trinity runs agents. Here are three ways in — none of them commit you to anything.
        </p>
      </div>
      <button
        @click="store.dismiss()"
        data-testid="front-desk-dismiss"
        class="ml-3 p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 rounded"
        title="Dismiss"
        aria-label="Dismiss the getting-started panel"
      >
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </div>

    <div class="px-4 py-3 flex flex-wrap items-center gap-2">
      <!-- Show me — only when the install actually seeded something to show.
           A door that opens onto nothing is worse than one door fewer. -->
      <button
        v-if="store.demoAgent"
        @click="showMe"
        data-testid="front-desk-show-me"
        class="inline-flex items-center px-3 py-1.5 text-sm font-medium rounded-md text-white bg-action-primary-600 hover:bg-action-primary-700"
      >
        Show me
        <span class="ml-1.5 font-normal opacity-80">watch {{ store.demoAgent }} work</span>
      </button>

      <button
        @click="$emit('make-one')"
        data-testid="front-desk-make-one"
        class="inline-flex items-center px-3 py-1.5 text-sm font-medium rounded-md border border-gray-300 dark:border-gray-600 text-gray-800 dark:text-gray-100 hover:bg-gray-50 dark:hover:bg-gray-700"
      >
        Make me one
        <span class="ml-1.5 font-normal text-gray-500 dark:text-gray-400">one question → an agent</span>
      </button>
    </div>

    <!-- Bring mine: reachable, never on the first screen as a peer of the
         other two (ent#319 AC). Until an in-app migration surface exists it
         points at the docs, which is honest about where that path starts. -->
    <div class="px-4 pb-3">
      <a
        href="https://docs.ability.ai"
        target="_blank"
        rel="noopener noreferrer"
        data-testid="front-desk-bring-mine"
        class="text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 underline underline-offset-2"
      >
        Already run a fleet? Bring it over →
      </a>
    </div>
  </div>
</template>

<script setup>
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useFirstRunStore } from '../../stores/firstRun'

defineEmits(['make-one'])

const store = useFirstRunStore()
const router = useRouter()

// Chat is where an agent stops being an abstraction, so "Show me" lands there
// rather than on a detail page the user would then have to navigate out of.
const showMe = () => {
  if (store.demoAgent) router.push(`/agents/${store.demoAgent}?tab=chat`)
}

onMounted(() => {
  store.fetchState()
})
</script>
