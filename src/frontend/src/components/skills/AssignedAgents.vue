<template>
  <div class="text-[11px]">
    <!-- Three distinct states, never collapsed into one another (principle 15):
         couldn't-find-out, nobody-holds-it, and the list. The first two look
         identical if you only inspect an empty map, and rendering "could not
         load" as "assigned to nobody" is the confident wrong zero this whole
         read exists to avoid. -->
    <p v-if="store.assignmentsError" class="text-gray-600 dark:text-gray-400">
      <span class="font-[550]">Assigned to</span>
      — unavailable
      <button
        type="button"
        class="ml-1 text-action-primary-600 dark:text-action-primary-400 hover:underline"
        @click="store.loadAssignments()"
      >retry</button>
    </p>

    <p v-else-if="!store.assignmentsLoaded" class="text-gray-600 dark:text-gray-400">
      <span class="font-[550]">Assigned to</span>
      —
    </p>

    <template v-else>
      <!-- Spacing is real whitespace, not `ml-*`: a margin is invisible to a
           screen reader, which would read "Assigned to1 agent". -->
      <p class="text-gray-600 dark:text-gray-400">
        <span class="font-[550]">Assigned to</span>
        <template v-if="agents.length">
          {{ ' ' }}<span class="tabular-nums">{{ agents.length }}</span>
          {{ ' ' }}agent{{ agents.length === 1 ? '' : 's' }}
        </template>
        <!-- Wording follows the payload's `scope`, so a non-admin is never told
             a skill has NO holders when they simply cannot see the ones it
             has. "None of your agents" is true whether the caller has zero
             agents or zero assignments among them. -->
        <template v-else>{{ ' ' }}{{ emptyText }}</template>
      </p>

      <ul v-if="agents.length" class="mt-1 flex flex-wrap items-center gap-1">
        <li v-for="a in shownAgents" :key="a.name">
          <router-link
            :to="{ path: `/agents/${a.name}`, query: { tab: 'skills' } }"
            class="inline-flex items-center rounded-full px-[9px] py-[2.5px] text-[11.5px] font-[550] leading-[1.4] bg-gray-100 text-gray-600 dark:bg-gray-750 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700"
            :title="a.display_label && a.display_label !== a.name ? a.name : undefined"
          >{{ a.display_label || a.name }}</router-link>
        </li>
        <!-- Counted overflow rather than truncation (principle 10/28): a skill
             on sixty agents must not grow the card without limit. -->
        <li v-if="hiddenCount > 0">
          <button
            type="button"
            class="text-[11px] text-action-primary-600 dark:text-action-primary-400 hover:underline"
            @click="expanded = true"
          >+{{ hiddenCount }} more</button>
        </li>
        <li v-else-if="expanded && agents.length > COLLAPSED_LIMIT">
          <button
            type="button"
            class="text-[11px] text-action-primary-600 dark:text-action-primary-400 hover:underline"
            @click="expanded = false"
          >show fewer</button>
        </li>
      </ul>
    </template>
  </div>
</template>

<script setup>
/**
 * The agents currently holding one skill (ent#384).
 *
 * Read-only by design: assignment is a WRITE that lives on each agent's Skills
 * tab (ent#182 — one skill model, no parallel mechanisms). The Library gained
 * this read, not a second write path; the assign/unassign controls are
 * ent#386.
 *
 * Reads the fleet-scoped `stores/skillsLibrary` store, which is deliberately
 * separate from the agent-scoped `stores/skills` — do not "unify" them, see
 * that store's header for why (KeepAlive'd AgentDetail).
 */
import { computed, ref } from 'vue'
import { useSkillsLibraryStore } from '../../stores/skillsLibrary'

const props = defineProps({
  skillName: { type: String, required: true },
})

const COLLAPSED_LIMIT = 4

const store = useSkillsLibraryStore()
const expanded = ref(false)

const agents = computed(() => store.agentsFor(props.skillName))
const shownAgents = computed(() =>
  expanded.value ? agents.value : agents.value.slice(0, COLLAPSED_LIMIT)
)
const hiddenCount = computed(() =>
  expanded.value ? 0 : Math.max(0, agents.value.length - COLLAPSED_LIMIT)
)

const emptyText = computed(() =>
  store.assignmentsScope === 'all' ? 'no agents yet' : 'none of your agents'
)
</script>
