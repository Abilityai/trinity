<!--
  The models member agents run on (trinity-enterprise#1202). Read-only.

  Two models per member agent: the one a member's own messages run on (the
  member-agents model setting) and the one the agent's OWN turns run on —
  wakes and reminders — which follows the template's model. A template model
  this instance cannot run is named, with what runs instead.

  Reads the member-agents settings response the welcome panel already loads
  (one GET, shared through the store). Rendered only where the member-agents
  entitlement is on.
-->
<template>
  <div v-if="entitled" class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg" data-testid="member-model-panel">
    <div class="px-6 py-5">
      <h3 class="text-lg font-medium text-gray-900 dark:text-gray-100">Models member agents run on</h3>
      <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">
        A member's messages run on the member model. The agent's own turns — wakes and reminders — run on its stored model (from its template or a Settings pick).
      </p>

      <!-- #1927: gated on "no data yet", never on the in-flight flag. -->
      <div v-if="view.state === 'loading'" class="mt-4" aria-busy="true" data-testid="member-model-loading">
        <span class="sr-only">Loading the member agent models…</span>
        <div class="h-16 rounded-md bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" aria-hidden="true"></div>
      </div>

      <LoadFailed
        v-else-if="view.state === 'failed'"
        dense
        class="mt-4"
        title="Couldn't load the member agent models"
        message="The member settings could not be read. Try again."
        :retrying="store.loading"
        @retry="store.load()"
      />

      <div v-else class="mt-4 space-y-4">
        <dl class="grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
          <div>
            <dt class="text-gray-600 dark:text-gray-300">Member messages</dt>
            <dd class="mt-0.5 flex items-center gap-2 text-gray-900 dark:text-gray-100" data-testid="member-model-turn">
              <span class="font-mono break-all">{{ memberModel?.resolved || '—' }}</span>
              <BaseBadge v-if="memberModel" variant="neutral">{{ memberModel.source === 'configured' ? 'configured' : 'default' }}</BaseBadge>
            </dd>
          </div>
          <div>
            <dt class="text-gray-600 dark:text-gray-300">Agents' own turns</dt>
            <dd class="mt-0.5 text-gray-900 dark:text-gray-100" data-testid="member-model-summary">{{ wake?.summary || 'No member agents yet.' }}</dd>
          </div>
        </dl>

        <div
          v-for="a in refused"
          :key="`refused-${a.agent_name}`"
          role="status"
          class="rounded-md border border-status-warning-200 dark:border-status-warning-500/30 bg-status-warning-50 dark:bg-status-warning-500/10 px-4 py-3 text-sm text-status-warning-700 dark:text-status-warning-300"
          data-testid="member-model-refused"
        >
          {{ a.agent_name }}'s template names <span class="font-mono break-all">{{ a.template_model }}</span>, which this instance cannot run ({{ reasonText(a.refused_reason) }}). Its own turns run on <span class="font-mono">{{ a.resolved }}</span> instead. Fix the template's model to change it.
        </div>

        <div v-if="!allAgree" class="max-h-64 overflow-y-auto rounded-md border border-gray-200 dark:border-gray-750" data-testid="member-model-list">
          <table class="w-full text-sm">
            <thead class="bg-gray-50 dark:bg-gray-900 text-left text-gray-600 dark:text-gray-300">
              <tr>
                <th scope="col" class="px-3 py-2 font-medium">Agent</th>
                <th scope="col" class="px-3 py-2 font-medium">Own turns run on</th>
                <th scope="col" class="px-3 py-2 font-medium">Source</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="a in agents" :key="a.agent_name" class="border-t border-gray-200 dark:border-gray-750" data-testid="member-model-row">
                <td class="px-3 py-2 text-gray-900 dark:text-gray-100 break-all">{{ a.agent_name }}</td>
                <td class="px-3 py-2 font-mono text-gray-900 dark:text-gray-100 break-all">{{ a.resolved || '—' }}</td>
                <td class="px-3 py-2">
                  <BaseBadge :variant="a.refused_reason ? 'warning' : 'neutral'">{{ a.source === 'stored' || a.source === 'template' ? a.source : 'default' }}</BaseBadge>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="text-xs text-gray-600 dark:text-gray-300">
          Not verified against a subscription: a model shown here is the one the platform asks for.
        </p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, watch } from 'vue'
import BaseBadge from '../base/BaseBadge.vue'
import LoadFailed from '../LoadFailed.vue'
import { useEnterpriseStore } from '../../stores/enterprise'
import { useMemberWelcomeStore } from '../../stores/memberWelcome'
import { viewState } from '../../utils/loadingState'

const FEATURE_ID = 'member_agents'
const REASONS = {
  not_in_catalog: 'not in the model catalog',
  runtime_mismatch: "another runtime's model",
}

const enterprise = useEnterpriseStore()
const store = useMemberWelcomeStore()

const entitled = computed(() => enterprise.isEntitled(FEATURE_ID))
const view = computed(() => viewState({ hasLoaded: store.hasLoaded, error: store.error }))
const memberModel = computed(() => store.settings?.model || null)
const wake = computed(() => store.settings?.wake_model || null)
const agents = computed(() => wake.value?.agents || [])
const refused = computed(() => agents.value.filter((a) => a.refused_reason))
/** Collapsed to the summary when every agent runs on the same model from the same source. */
const allAgree = computed(() => new Set(agents.value.map((a) => `${a.resolved}|${a.source}`)).size <= 1)

function reasonText(code) {
  return REASONS[code] || code
}

// The welcome panel beside this one loads the shared view; load only if
// nothing has, so the Access tab issues one GET.
onMounted(() => {
  if (entitled.value && !store.hasLoaded && !store.loading) store.load()
})
watch(entitled, (on) => { if (on && !store.hasLoaded && !store.loading) store.load() })
</script>
