<template>
  <!--
    abilityai/trinity-enterprise#662 — what each edge of this agent exposes.

    A peer may run what an agent declares; what an admin assigned needs a grant.
    For every agent this one may call, this lists the skills that agent holds
    from the skills library, and whether THIS agent may run each. An admin turns
    one on per skill; everyone else sees the state. It is also the upgrade
    notice: an edge that used to reach a library skill shows it "Not granted".
  -->
  <section class="mt-8" data-testid="peer-skill-grants">
    <h4 class="text-sm font-medium text-gray-900 dark:text-gray-100">Library skills on agents this agent can call</h4>
    <p class="mt-1 mb-3 text-sm text-gray-600 dark:text-gray-400">
      This agent can run the skills an agent declares itself. A skill assigned from the skills library needs a grant per skill.
    </p>

    <div v-if="view.state === 'loading'" class="h-16 rounded-lg bg-gray-100 dark:bg-gray-750 animate-pulse" aria-hidden="true"></div>

    <LoadFailed
      v-else-if="view.state === 'failed'"
      dense
      title="Couldn't load library skills"
      message="The permission edges above still apply. Try again."
      :detail="errorText"
      :retrying="loading"
      @retry="reload"
    />

    <p v-else-if="view.state === 'empty'" class="text-sm text-gray-600 dark:text-gray-400" data-testid="peer-skill-grants-empty">
      None of the agents this agent can call holds a library skill, so there is nothing to grant.
    </p>

    <ul v-else class="space-y-3">
      <li
        v-for="t in targetsWithSkills"
        :key="t.target"
        class="border border-gray-200 dark:border-gray-750 rounded-lg px-4 py-3"
      >
        <div class="flex items-center justify-between gap-3">
          <span class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t.target }}</span>
          <BaseBadge v-if="t.spawned" size="sm" variant="info">Spawned by this agent: every skill runs</BaseBadge>
          <BaseBadge v-else-if="t.not_granted" size="sm" variant="warning">{{ t.not_granted }} not granted</BaseBadge>
        </div>
        <ul class="mt-2 divide-y divide-gray-200 dark:divide-gray-750">
          <li
            v-for="s in t.skills"
            :key="s.name"
            class="py-2 flex items-center justify-between gap-3"
            :data-testid="`peer-skill-${t.target}-${s.name}`"
          >
            <div class="min-w-0 flex items-center gap-2">
              <span class="text-sm font-mono text-gray-900 dark:text-gray-100 truncate" :title="s.name">{{ s.name }}</span>
              <BaseBadge v-if="!s.individual" size="sm">via skill set</BaseBadge>
              <span
                v-if="s.last_used_at"
                class="text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap"
                :title="s.last_used_at"
              >used {{ formatDay(s.last_used_at) }}</span>
            </div>
            <BaseToggle
              v-if="isAdmin && !t.spawned"
              :model-value="s.granted"
              :disabled="busyKey === `${t.target}/${s.name}`"
              :aria-label="`Let ${agentName} run ${s.name} on ${t.target}`"
              @update:model-value="(on) => toggle(t.target, s.name, on)"
            />
            <BaseBadge v-else-if="!t.spawned" size="sm" :variant="s.granted ? 'success' : 'neutral'">
              {{ s.granted ? 'Granted' : 'Not granted' }}
            </BaseBadge>
          </li>
        </ul>
      </li>
    </ul>

    <p v-if="isAdmin && view.state === 'ready'" class="mt-2 text-xs text-gray-500 dark:text-gray-400">
      Turning a skill on lets {{ agentName }} run it on that agent, with that agent's identity, credentials and write access.
    </p>
    <p
      v-if="notice"
      class="mt-2 text-sm"
      :class="notice.type === 'success' ? 'text-status-success-700 dark:text-status-success-400' : 'text-status-danger-700 dark:text-status-danger-400'"
      role="status"
    >
      {{ notice.text }}
    </p>
  </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import BaseBadge from './base/BaseBadge.vue'
import BaseToggle from './base/BaseToggle.vue'
import LoadFailed from './LoadFailed.vue'
import { useAgentsStore } from '../stores/agents'
import { useAuthStore } from '../stores/auth'
import { viewState } from '../utils/loadingState'

// The day a skill was last used over the edge (the upgrade evidence).
function formatDay(iso) {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString()
}

const props = defineProps({
  agentName: { type: String, required: true }
})

const agentsStore = useAgentsStore()
const authStore = useAuthStore()

const targets = ref([])
const loading = ref(false)
const hasLoaded = ref(false)
const error = ref(null)
const busyKey = ref(null)
const notice = ref(null)

// Granting is admin-only on the server (require_admin + interactive); the
// toggle is shown only where the server would accept it.
const isAdmin = computed(() => authStore.user?.role === 'admin')
const targetsWithSkills = computed(() => targets.value.filter((t) => t.skills.length > 0))
const view = computed(() => viewState({
  loading: loading.value, hasLoaded: hasLoaded.value, error: error.value,
  count: targetsWithSkills.value.length,
}))
const errorText = computed(() => {
  const d = error.value?.response?.data?.detail
  return typeof d === 'string' ? d : (error.value?.message || '')
})

async function reload() {
  const forAgent = props.agentName
  loading.value = true
  try {
    const data = await agentsStore.getAgentPeerSkills(forAgent)
    if (forAgent !== props.agentName) return
    targets.value = Array.isArray(data?.targets) ? data.targets : []
    hasLoaded.value = true
    error.value = null
  } catch (e) {
    if (forAgent === props.agentName) error.value = e
  } finally {
    if (forAgent === props.agentName) loading.value = false
  }
}

async function toggle(target, skill, on) {
  busyKey.value = `${target}/${skill}`
  notice.value = null
  try {
    const res = await agentsStore.setAgentPeerSkillGrant(props.agentName, target, skill, on)
    const row = targets.value.find((t) => t.target === target)?.skills.find((s) => s.name === skill)
    if (row) row.granted = on
    const t = targets.value.find((x) => x.target === target)
    if (t) t.not_granted = t.skills.filter((s) => !s.granted).length
    notice.value = {
      type: 'success',
      text: on ? (res?.consent || `${props.agentName} may now run ${skill} on ${target}.`)
               : `${props.agentName} can no longer run ${skill} on ${target}.`,
    }
  } catch (e) {
    const d = e?.response?.data?.detail
    notice.value = { type: 'error', text: d?.message || (typeof d === 'string' ? d : "Couldn't change the grant. Try again.") }
  } finally {
    busyKey.value = null
  }
}

defineExpose({ reload })

onMounted(reload)
watch(() => props.agentName, () => {
  targets.value = []
  hasLoaded.value = false
  error.value = null
  notice.value = null
  reload()
})
</script>
