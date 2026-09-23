<template>
  <!--
    Skill managers (trinity-enterprise#596) — the agents an instance admin has
    let change agents' skills.

    The ruling this panel is the grant side of: changing an agent's skills —
    another agent's OR ITS OWN — is its own permission. Every agent NOT listed
    here is refused, on a sibling and on itself; people and the system agent
    never needed it. So the empty state is not "nothing to see": it is "no agent
    can change skills", which is exactly what an orchestrator running the skill
    map hits the day this ships — the copy says so and names the next step.
  -->
  <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg" data-testid="skill-managers-panel">
    <div class="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
      <h2 class="text-lg font-medium text-gray-900 dark:text-white">Skill managers</h2>
      <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
        Agents allowed to change which skills agents hold — their own included. Every
        other agent is refused. People, and the system agent, are not affected.
      </p>
    </div>

    <div class="px-6 py-4 space-y-4">
      <!-- #1927: gate on "no data yet", never on the in-flight flag. -->
      <p v-if="view.state === 'loading'" class="text-sm text-gray-500 dark:text-gray-400">Loading…</p>

      <div
        v-else-if="view.state === 'failed'"
        class="rounded-md bg-status-danger-50 dark:bg-status-danger-900/30 p-3 text-sm text-status-danger-700 dark:text-status-danger-300"
        data-testid="skill-managers-load-error"
      >
        {{ store.loadError }}
        <button type="button" class="ml-2 underline font-medium" @click="store.fetch()">Retry</button>
      </div>

      <template v-else>
        <!-- The backend's named refusal, verbatim: a grant to an ephemeral or
             system agent says why, which a generic message would throw away. -->
        <div
          v-if="store.actionError"
          class="rounded-md bg-status-danger-50 dark:bg-status-danger-900/30 p-3 text-sm text-status-danger-700 dark:text-status-danger-300"
          data-testid="skill-managers-action-error"
        >
          {{ store.actionError }}
        </div>

        <p
          v-if="view.state === 'empty'"
          class="text-sm text-gray-600 dark:text-gray-300"
          data-testid="skill-managers-empty"
        >
          No agent can change skills yet. A fleet orchestrator that applies a skill map
          needs this — grant it below.
        </p>

        <ul v-else class="divide-y divide-gray-200 dark:divide-gray-700" data-testid="skill-managers-list">
          <li
            v-for="h in store.holders"
            :key="h.agent_name"
            class="py-3 flex items-center justify-between gap-4"
            :data-testid="'skill-manager-' + h.agent_name"
          >
            <div class="min-w-0">
              <div class="text-sm font-medium text-gray-900 dark:text-white truncate">{{ h.agent_name }}</div>
              <div class="text-xs text-gray-500 dark:text-gray-400">
                Granted by {{ h.granted_by }} · {{ formatDate(h.granted_at) }}
              </div>
            </div>
            <BaseButton
              variant="secondary"
              size="sm"
              :loading="store.busyAgent === h.agent_name"
              loading-label="Revoking…"
              :disabled="!!store.busyAgent"
              @click="store.setGranted(h.agent_name, false)"
            >Revoke</BaseButton>
          </li>
        </ul>

        <form class="flex items-end gap-3" @submit.prevent="grant">
          <BaseSelect
            v-model="choice"
            label="Allow another agent"
            class="flex-1 min-w-0"
            :disabled="!candidates.length || !!store.busyAgent"
            data-testid="skill-managers-picker"
          >
            <option value="">{{ candidates.length ? 'Choose an agent…' : 'Every agent already holds it' }}</option>
            <option v-for="a in candidates" :key="a" :value="a">{{ a }}</option>
          </BaseSelect>
          <BaseButton
            type="submit"
            size="md"
            :disabled="!choice || !!store.busyAgent"
            :loading="!!choice && store.busyAgent === choice"
            loading-label="Granting…"
            data-testid="skill-managers-grant"
          >Grant</BaseButton>
        </form>
      </template>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import BaseButton from './base/BaseButton.vue'
import BaseSelect from './base/BaseSelect.vue'
import { useSkillManagersStore } from '../stores/skillManagers'
import { useAgentsStore } from '../stores/agents'
import { viewState } from '../utils/loadingState'

const store = useSkillManagersStore()
const agents = useAgentsStore()
const choice = ref('')

const view = computed(() => viewState({
  hasLoaded: store.hasLoaded,
  error: store.loadError,
  count: store.holders.length,
}))

// Every agent the admin can see that does not already hold the grant. The
// system agent is left out — it holds every capability by its scope, and the
// backend refuses a grant to it. An ephemeral agent is not filtered here: the
// list does not carry the flag, and the backend's named refusal is the honest
// answer if one is picked.
const candidates = computed(() => {
  const held = new Set(store.holders.map(h => h.agent_name))
  return (agents.agents || [])
    .filter(a => !a.is_system && !held.has(a.name))
    .map(a => a.name)
    .sort()
})

async function grant () {
  if (!choice.value) return
  const ok = await store.setGranted(choice.value, true)
  if (ok) choice.value = ''
}

function formatDate (iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString()
}

onMounted(() => {
  store.fetch()
  if (!agents.agents?.length) agents.fetchAgents?.()
})
</script>
