<template>
  <section v-if="store.setsError || store.sets.length" class="mb-6" data-testid="library-skill-sets">
    <h3 class="text-[14px] font-[550] text-gray-900 dark:text-gray-100">
      Skill sets
      <span class="ml-1 text-[12.5px] font-[400] text-gray-600 dark:text-gray-400 tabular-nums">{{ store.sets.length }}</span>
    </h3>
    <p class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-400">
      A set assigns a family of library skills in one act and keeps it current — a member added
      upstream arrives on the next re-inject, one removed upstream is pruned.
    </p>

    <!-- Couldn't-find-out is not "no sets" (principle 15). -->
    <p v-if="store.setsError" class="mt-2 text-[12.5px] text-gray-600 dark:text-gray-400" data-testid="library-sets-error">
      Skill sets are unavailable — {{ store.setsError }}
      <button
        type="button"
        class="ml-1 text-action-primary-600 dark:text-action-primary-400 hover:underline"
        @click="store.loadSets()"
      >retry</button>
    </p>

    <!-- Bounded: a catalog may declare up to 50 sets per source (principle 28). -->
    <ul v-else class="mt-3 max-h-[32rem] overflow-y-auto space-y-2 pr-1">
      <li v-for="set in store.sets" :key="set.name">
        <BaseCard :data-testid="`library-set-${set.name}`">
          <div class="flex items-center gap-2 flex-wrap">
            <button
              type="button"
              class="inline-flex items-center gap-1 text-[14px] font-[550] text-gray-900 dark:text-gray-100 hover:underline"
              :aria-expanded="isOpen(set.name) ? 'true' : 'false'"
              :aria-controls="`set-members-${set.name}`"
              :data-testid="`library-set-toggle-${set.name}`"
              @click="toggle(set.name)"
            >
              <svg
                class="w-3 h-3 transition-transform motion-reduce:transition-none"
                :class="isOpen(set.name) ? 'rotate-90' : ''"
                viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"
              ><path d="M7 5l6 5-6 5V5z" /></svg>
              {{ set.name }}
            </button>
            <BaseBadge variant="purple">set · {{ set.members.length }}</BaseBadge>
            <BaseBadge v-if="set.source_name" variant="info">{{ set.source_name }}</BaseBadge>
            <BaseBadge
              v-if="set.status !== 'ok'"
              :variant="set.status === 'invalid' ? 'danger' : 'warning'"
              dot
              :data-testid="`library-set-partial-${set.name}`"
            >{{ set.status === 'invalid' ? 'invalid' : 'partial' }}</BaseBadge>
            <BaseBadge
              v-if="set.shadowed_by?.length"
              variant="warning"
              :title="`Also declared by: ${set.shadowed_by.map(x => x.source_name).join(', ')}`"
            >shadowed</BaseBadge>
          </div>

          <p
            v-if="set.status === 'invalid'"
            class="mt-1 text-[12.5px] text-status-danger-700 dark:text-status-danger-400"
            :data-testid="`library-set-invalid-${set.name}`"
          >
            Its definition in the source's <span class="font-mono">catalog.yaml</span> is invalid
            ({{ set.problems.join(', ') }}) — it cannot be assigned until the source is fixed.
          </p>
          <p v-else-if="set.status !== 'ok'" class="mt-1 text-[12.5px] text-status-warning-700 dark:text-status-warning-400">
            Names {{ missingNames(set).join(', ') }}, which its source does not ship — it cannot be assigned until the
            source is fixed.
          </p>
          <p v-if="set.requires?.env?.length" class="mt-1 text-[11px] text-gray-600 dark:text-gray-400">
            Requires <span class="font-mono">{{ set.requires.env.join(', ') }}</span>
          </p>

          <div v-show="isOpen(set.name)" :id="`set-members-${set.name}`" class="mt-2">
            <ul class="space-y-0.5">
              <li
                v-for="m in set.members"
                :key="m.name"
                class="flex items-center gap-2 flex-wrap text-[12.5px] text-gray-700 dark:text-gray-300"
              >
                <span>{{ m.name }}</span>
                <span v-if="m.version" class="font-mono text-[11px] text-gray-600 dark:text-gray-400">{{ m.version.slice(0, 7) }}</span>
                <BaseBadge v-if="!m.present" variant="warning">missing</BaseBadge>
                <BaseBadge
                  v-else-if="m.shadowed_source"
                  variant="neutral"
                  title="The copy that runs comes from another source, by library precedence"
                >from another source</BaseBadge>
              </li>
            </ul>
            <div v-if="set.schedules?.length" class="mt-2 text-[12.5px] text-gray-600 dark:text-gray-400">
              Suggested schedules — shown, never created:
              <ul class="mt-0.5">
                <li v-for="sc in set.schedules" :key="sc.name">
                  {{ sc.name }} · <span class="font-mono text-[11px]">{{ sc.cron }}</span>
                </li>
              </ul>
            </div>
          </div>

          <div
            v-if="set.status === 'ok' && store.assignableAgents.length"
            class="mt-3 flex items-end gap-2"
          >
            <BaseSelect
              v-model="picked[set.name]"
              :label="`Assign ${set.name} to`"
              class="min-w-[12rem]"
              :data-testid="`library-set-picker-${set.name}`"
            >
              <option value="">Choose an agent…</option>
              <option v-for="a in agentsSorted" :key="a.name" :value="a.name">{{ a.display_label || a.name }}</option>
            </BaseSelect>
            <BaseButton
              size="sm"
              :disabled="!picked[set.name] || busy !== null"
              :loading="busy === set.name"
              loading-label="Assigning…"
              :data-testid="`library-set-assign-${set.name}`"
              @click="onAssign(set.name)"
            >Assign set</BaseButton>
          </div>

          <InlineError
            v-if="errors[set.name]"
            :message="errors[set.name]"
            class="mt-2"
            @dismiss="errors[set.name] = null"
          />
          <p
            v-else-if="store.setResults[set.name]"
            class="mt-2 text-[12.5px]"
            :class="missingEnv(set.name).length
              ? 'text-status-warning-700 dark:text-status-warning-400'
              : 'text-status-success-700 dark:text-status-success-400'"
            :data-testid="`library-set-note-${set.name}`"
          >
            {{ store.setResults[set.name].agent }}: assigned<template v-if="missingEnv(set.name).length">
              — missing credentials {{ missingEnv(set.name).join(', ') }}; add them on the agent's Credentials tab</template>.
          </p>
        </BaseCard>
      </li>
    </ul>
  </section>
</template>

<script setup>
/**
 * Library → Skills: the skill SETS every synced source declares
 * (trinity-enterprise#530). Expandable to members with each member's version;
 * a partial set (a member its source does not ship) is shown and cannot be
 * assigned. Assign goes through the per-agent set route — the same ent#596
 * fence as a skill assign; unassigning lives on the agent's Skills tab.
 */
import { computed, reactive, ref } from 'vue'
import { useSkillsLibraryStore } from '../../stores/skillsLibrary'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseCard from '../base/BaseCard.vue'
import BaseSelect from '../base/BaseSelect.vue'
import InlineError from '../InlineError.vue'

const store = useSkillsLibraryStore()
const open = ref(new Set())
const picked = reactive({})
const errors = reactive({})
const busy = ref(null)

const agentsSorted = computed(() =>
  [...store.assignableAgents].sort((a, b) =>
    (a.display_label || a.name).localeCompare(b.display_label || b.name))
)

function isOpen(name) {
  return open.value.has(name)
}

function toggle(name) {
  const next = new Set(open.value)
  if (next.has(name)) next.delete(name)
  else next.add(name)
  open.value = next
}

function missingNames(set) {
  return set.members.filter(m => !m.present).map(m => m.name)
}

function missingEnv(setName) {
  const status = store.setResults[setName]?.result?.status
  return status?.prerequisites?.state === 'missing' ? status.prerequisites.missing_env : []
}

async function onAssign(setName) {
  const agent = picked[setName]
  if (!agent) return
  busy.value = setName
  errors[setName] = null
  errors[setName] = await store.assignSet(setName, agent)
  busy.value = null
  if (!errors[setName]) picked[setName] = ''
}
</script>
