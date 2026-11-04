<template>
  <!--
    abilityai/trinity-enterprise#662 — the honest upgrade.

    A peer may run what an agent declares; what an admin assigned needs a grant.
    Calls between agents that used a library skill in the last 30 days are
    refused from this release on until an admin grants them. This notice lists
    exactly those (caller, target, skill) pairs, read off the execution ledger,
    with a grant per pair. It renders nothing while there is nothing to grant.
  -->
  <BaseCard v-if="pairs.length > 0 || notice" class="mt-6" data-testid="peer-skill-drift">
    <div class="flex items-start justify-between gap-3">
      <div>
        <h3 class="text-base font-medium text-gray-900 dark:text-gray-100">Library skills agents can no longer run on each other</h3>
        <p class="mt-1 text-sm text-gray-600 dark:text-gray-400">
          {{ pairs.length }} agent-to-agent skill call{{ pairs.length === 1 ? '' : 's' }} used a library skill in the last {{ windowDays }} days
          and {{ pairs.length === 1 ? 'is' : 'are' }} now refused. An agent may run the skills another agent declares itself;
          a skill assigned from the library needs a grant. Grant the ones that should keep working.
        </p>
      </div>
      <BaseBadge v-if="pairs.length" variant="warning">{{ pairs.length }} refused</BaseBadge>
    </div>

    <ul v-if="pairs.length" class="mt-3 max-h-80 overflow-y-auto divide-y divide-gray-200 dark:divide-gray-750 border border-gray-200 dark:border-gray-750 rounded-lg">
      <li
        v-for="p in pairs"
        :key="`${p.caller}/${p.target}/${p.skill}`"
        class="px-4 py-2 flex items-center justify-between gap-3"
        :data-testid="`drift-${p.caller}-${p.target}-${p.skill}`"
      >
        <div class="min-w-0 text-sm text-gray-900 dark:text-gray-100">
          <span class="font-medium">{{ p.caller }}</span>
          <span class="text-gray-500 dark:text-gray-400"> runs </span>
          <span class="font-mono">{{ p.skill }}</span>
          <span class="text-gray-500 dark:text-gray-400"> on </span>
          <span class="font-medium">{{ p.target }}</span>
          <span class="ml-2 text-xs text-gray-500 dark:text-gray-400">{{ p.uses }}× · last {{ formatDay(p.last_used_at) }}</span>
        </div>
        <BaseButton
          size="sm"
          variant="secondary"
          :disabled="busyKey === keyOf(p)"
          @click="grant(p)"
        >{{ busyKey === keyOf(p) ? 'Granting…' : 'Grant' }}</BaseButton>
      </li>
    </ul>
    <p
      v-if="notice"
      class="mt-2 text-sm"
      :class="notice.type === 'success' ? 'text-status-success-700 dark:text-status-success-400' : 'text-status-danger-700 dark:text-status-danger-400'"
      role="status"
    >{{ notice.text }}</p>
  </BaseCard>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import BaseBadge from './base/BaseBadge.vue'
import BaseButton from './base/BaseButton.vue'
import BaseCard from './base/BaseCard.vue'
import { useAgentsStore } from '../stores/agents'

const agentsStore = useAgentsStore()
const pairs = ref([])
const windowDays = ref(30)
const busyKey = ref(null)
const notice = ref(null)

const keyOf = (p) => `${p.caller}/${p.target}/${p.skill}`

function formatDay(iso) {
  if (!iso) return 'unknown'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString()
}

async function load() {
  try {
    const report = await agentsStore.getPeerSkillDrift()
    pairs.value = Array.isArray(report?.pairs) ? report.pairs : []
    windowDays.value = report?.window_days || 30
  } catch (e) {
    // A notice, not a page: an unreadable report hides itself (the per-agent
    // Permissions tab still shows every edge's library skills).
    pairs.value = []
  }
}

async function grant(p) {
  busyKey.value = keyOf(p)
  notice.value = null
  try {
    const res = await agentsStore.setAgentPeerSkillGrant(p.caller, p.target, p.skill, true)
    pairs.value = pairs.value.filter((x) => keyOf(x) !== keyOf(p))
    notice.value = { type: 'success', text: res?.consent || `${p.caller} may now run ${p.skill} on ${p.target}.` }
  } catch (e) {
    const d = e?.response?.data?.detail
    notice.value = { type: 'error', text: d?.message || (typeof d === 'string' ? d : "Couldn't grant it. Try again.") }
  } finally {
    busyKey.value = null
  }
}

onMounted(load)
</script>
