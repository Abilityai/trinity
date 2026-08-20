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
        :disabled="store.assignmentsFetching"
        class="ml-1 text-action-primary-600 dark:text-action-primary-400 hover:underline disabled:opacity-45 disabled:cursor-not-allowed disabled:no-underline"
        @click="store.loadAssignments()"
      >{{ store.assignmentsFetching ? 'retrying…' : 'retry' }}</button>
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
        <li v-for="a in shownAgents" :key="a.name" class="inline-flex items-center">
          <router-link
            :to="{ path: `/agents/${a.name}`, query: { tab: 'skills' } }"
            class="inline-flex items-center rounded-full px-[9px] py-[2.5px] text-[11.5px] font-[550] leading-[1.4] bg-gray-100 text-gray-600 dark:bg-gray-750 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700"
            :class="store.canModify(a.name) ? 'rounded-r-none pr-[6px]' : ''"
            :title="a.display_label && a.display_label !== a.name ? a.name : undefined"
          >{{ a.display_label || a.name }}</router-link>
          <!-- Unassign is offered ONLY where the write would actually be
               allowed. A shared agent is a holder the caller cannot modify, so
               showing it an × would produce a 404 from the owner gate — a
               control that exists to fail (principle: no dead affordances). -->
          <button
            v-if="store.canModify(a.name)"
            type="button"
            :disabled="busy === a.name"
            class="inline-flex items-center rounded-full rounded-l-none pl-[3px] pr-[7px] py-[2.5px] text-[11.5px] leading-[1.4] bg-gray-100 text-gray-500 dark:bg-gray-750 dark:text-gray-400 hover:bg-red-100 hover:text-red-700 dark:hover:bg-red-900/40 dark:hover:text-red-300 disabled:opacity-45 disabled:cursor-not-allowed"
            :title="`Unassign from ${a.display_label || a.name}`"
            :aria-label="`Unassign ${skillName} from ${a.display_label || a.name}`"
            @click="onUnassign(a.name)"
          >×</button>
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

      <!-- Assign (ent#386). Rendered for a zero-holder skill too — that case
           used to link out to an agent's Skills tab, which made the Library
           the one place that could tell you a skill was unused and the one
           place that could not act on it. -->
      <div v-if="assignable.length" class="mt-1.5 flex items-center gap-1.5">
        <label class="sr-only" :for="selectId">Assign {{ skillName }} to an agent</label>
        <select
          :id="selectId"
          v-model="picked"
          :disabled="busy !== null"
          class="rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-[11px] text-gray-700 dark:text-gray-200 py-[2px] pl-1.5 pr-5 disabled:opacity-45 disabled:cursor-not-allowed"
        >
          <option value="">Assign to…</option>
          <option v-for="a in assignable" :key="a.name" :value="a.name">
            {{ a.display_label || a.name }}
          </option>
        </select>
        <button
          type="button"
          :disabled="!picked || busy !== null"
          class="rounded px-2 py-[2.5px] text-[11px] font-[550] bg-action-primary-600 text-white hover:bg-action-primary-700 disabled:opacity-45 disabled:cursor-not-allowed"
          @click="onAssign"
        >{{ busy === picked && picked ? 'assigning…' : 'Assign' }}</button>
      </div>

      <!-- The server's own reason, next to the control that caused it. Not a
           toast: the Library renders one of these per skill card, and a toast
           detaches the failure from the block it belongs to. -->
      <p v-if="writeError" class="mt-1 text-[11px] text-red-700 dark:text-red-300">
        {{ writeError }}
      </p>
    </template>
  </div>
</template>

<script setup>
/**
 * The agents currently holding one skill (ent#384).
 *
 * ent#386 added the write half. It is not a parallel mechanism: the controls
 * call the SAME per-agent routes the agent's Skills tab calls
 * (`POST`/`DELETE /api/agents/{name}/skills/{skill}`), which is where the
 * owner gate lives. No skill-keyed writer exists, deliberately — a second
 * write path is a second place for that gate to drift (ent#182: one skill
 * model).
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

// `busy` holds the agent name being written, not a boolean: the block can show
// several controls at once and a shared boolean would grey out all of them.
const busy = ref(null)
const picked = ref('')
const writeError = ref(null)
// Unique per card — the <label for> would otherwise point at the first card's
// select for every skill on the page.
const selectId = computed(() => `assign-${props.skillName.replace(/[^a-zA-Z0-9_-]/g, '-')}`)

const assignable = computed(() => store.assignableFor(props.skillName))

async function onAssign() {
  if (!picked.value) return
  const target = picked.value
  busy.value = target
  writeError.value = null
  writeError.value = await store.assignSkill(props.skillName, target)
  busy.value = null
  // Only clear the choice on success, so a failed attempt leaves the target
  // selected and the reason visible beside it — retry is one click, not a
  // re-hunt through the dropdown.
  if (!writeError.value) picked.value = ''
}

async function onUnassign(agentName) {
  busy.value = agentName
  writeError.value = null
  writeError.value = await store.unassignSkill(props.skillName, agentName)
  busy.value = null
}

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
