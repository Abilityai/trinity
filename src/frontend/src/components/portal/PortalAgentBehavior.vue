<template>
  <!--
    "How it's been working lately" (trinity-enterprise#939) — the agent's
    behavioral indicators in plain words for the people it serves. No numbers
    or scores: a tone per indicator, keyed to a sentence here. Rides on the role
    card read (PortalAgentRole loads it), so it costs no request of its own.
    Unreadable → nothing rendered, never "all good".
  -->
  <section v-if="behavior" data-testid="portal-agent-behavior">
    <h2 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">How it's been working lately</h2>
    <div class="rounded-xl border border-gray-200 dark:border-gray-800 px-3 py-2.5">
      <p v-if="!behavior.runs" class="text-[12.5px] text-gray-600 dark:text-gray-300" data-testid="portal-behavior-none">
        No tasks in the last {{ weeksText }} yet, so there is nothing to say about how it works.
      </p>
      <template v-else>
        <p class="text-[12.5px] text-gray-500 dark:text-gray-400 mb-2">Based on {{ behavior.runs }} {{ behavior.runs === 1 ? 'task' : 'tasks' }} over the last {{ weeksText }}</p>
        <ul class="space-y-2 text-sm">
          <li v-for="l in behavior.lines" :key="l.indicator" class="flex gap-2.5 items-start" :data-testid="`portal-behavior-${l.indicator}`" :data-tone="l.tone">
            <svg v-if="l.tone === 'good'" class="w-[18px] h-[18px] shrink-0 mt-px text-status-success-700 dark:text-status-success-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M5 12l5 5L20 7" /></svg>
            <svg v-else-if="l.tone === 'watch'" class="w-[18px] h-[18px] shrink-0 mt-px text-status-warning-700 dark:text-status-warning-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M12 8v5M12 16.5v.5" /></svg>
            <svg v-else class="w-[18px] h-[18px] shrink-0 mt-px text-gray-500 dark:text-gray-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M8 12h8" /></svg>
            <span :class="l.tone === 'unknown' ? 'text-gray-600 dark:text-gray-300' : 'text-gray-900 dark:text-gray-100'">{{ words(l) }}</span>
          </li>
        </ul>
      </template>
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import { useClientPortalStore } from '../../stores/clientPortal'
import { PLAIN_WORDS } from '../indicators/format'

const props = defineProps({
  agentName: { type: String, required: true },
})

const store = useClientPortalStore()
const behavior = computed(() =>
  (store.roleAgent === props.agentName ? store.role?.behavior : null) || null)
const weeksText = computed(() => {
  const d = behavior.value?.days || 14
  return d === 7 ? 'week' : d === 14 ? 'two weeks' : `${d} days`
})
const words = (l) => PLAIN_WORDS[l.indicator]?.[l.tone] || ''
</script>
