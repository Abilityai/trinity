<template>
  <!--
    Exposed-playbooks allow-list picker (trinity-enterprise#55).

    Carved out of ConnectorChannelPanel.vue as a standalone, reusable surface so
    the same picker can govern any client-sharing channel and the connector's
    key/config block stays decoupled (the SSO tier can later swap the key block
    for an OAuth variant without touching this component). Reads/writes the SAME
    allow-list the connector consumes: enterprise_connectors.exposed_playbooks via
    GET/PUT /api/agents/{name}/connector. No separate storage.
  -->
  <div>
    <div class="flex items-center justify-between mb-2">
      <h4 class="text-sm font-medium text-gray-900 dark:text-gray-100">Exposed playbooks</h4>
      <label class="flex items-center gap-2 text-xs text-gray-600 dark:text-gray-300 cursor-pointer">
        <input type="checkbox" v-model="exposeAll" @change="saveAllowList" :disabled="busy" />
        Expose all
      </label>
    </div>
    <p class="text-xs text-gray-500 dark:text-gray-400 mb-2">
      Choose which playbooks shared consumers can call as tools. Approval-gated playbooks still
      require operator sign-off; non-invocable playbooks can never be exposed.
    </p>

    <!-- Playbooks need a running agent -->
    <div v-if="playbooksError" class="text-xs text-gray-500 dark:text-gray-400">{{ playbooksError }}</div>

    <!-- Empty state: guidance, not a blank panel -->
    <div v-else-if="playbooks.length === 0" class="rounded-lg border border-dashed border-gray-300 dark:border-gray-700 px-3 py-4 text-center">
      <p class="text-xs font-medium text-gray-700 dark:text-gray-300">No playbooks to expose</p>
      <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
        This agent has no skills under
        <code class="font-mono text-xs bg-gray-100 dark:bg-gray-800 px-1 py-0.5 rounded">.claude/skills/</code>.
      </p>
    </div>

    <ul v-else class="divide-y divide-gray-200 dark:divide-gray-700 border border-gray-200 dark:border-gray-700 rounded-lg">
      <li
        v-for="pb in playbooks"
        :key="pb.name"
        class="px-3 py-2 flex items-center gap-2"
        :class="{ 'opacity-60': !pb.user_invocable }"
      >
        <input
          type="checkbox"
          :value="pb.name"
          v-model="selected"
          :disabled="exposeAll || busy || !pb.user_invocable"
          @change="debouncedSaveAllowList"
        />
        <span class="text-sm text-gray-900 dark:text-gray-100 font-mono">/{{ pb.name }}</span>
        <span
          v-if="pb.automation === 'gated'"
          class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-state-autonomous-100 dark:bg-state-autonomous-900/40 text-state-autonomous-800 dark:text-state-autonomous-200"
          title="Runs require operator approval"
        >approval-gated</span>
        <span
          v-if="!pb.user_invocable"
          class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-gray-200 dark:bg-gray-700 text-gray-600 dark:text-gray-300"
          title="Marked user-invocable: false in its SKILL.md — cannot be exposed"
        >not invocable</span>
        <span v-if="pb.description" class="text-xs text-gray-500 dark:text-gray-400 truncate">— {{ pb.description }}</span>
      </li>
    </ul>

    <p v-if="message" class="mt-2 text-xs" :class="messageError ? 'text-status-danger-600' : 'text-status-success-600'">{{ message }}</p>
  </div>
</template>

<script setup>
import { ref, onMounted, watch } from 'vue'
import api from '../api'

const props = defineProps({
  agentName: { type: String, required: true },
})

const playbooks = ref([])
const playbooksError = ref('')
const selected = ref([])
const exposeAll = ref(true)
const busy = ref(false)
const message = ref('')
const messageError = ref(false)

const notify = (text, isError = false) => {
  message.value = text
  messageError.value = isError
}

// exposed_playbooks === null ⇒ all exposed; else the explicit set.
const syncFromConnector = (exposedPlaybooks) => {
  exposeAll.value = exposedPlaybooks == null
  selected.value = Array.isArray(exposedPlaybooks) ? [...exposedPlaybooks] : []
}

const loadConnectorState = async () => {
  try {
    const { data } = await api.get(`/api/agents/${props.agentName}/connector`)
    syncFromConnector(data.exposed_playbooks)
  } catch {
    // 403 (not owner) / not entitled — parent gates visibility; default to all.
    syncFromConnector(null)
  }
}

const loadPlaybooks = async () => {
  playbooksError.value = ''
  try {
    const { data } = await api.get(`/api/agents/${props.agentName}/playbooks`)
    playbooks.value = data?.skills || []
    // Prune stale names: an allow-list entry that's no longer a live
    // user-invocable playbook would otherwise linger and be re-sent forever.
    if (!exposeAll.value) {
      const invocable = new Set(playbooks.value.filter((p) => p.user_invocable !== false).map((p) => p.name))
      selected.value = selected.value.filter((n) => invocable.has(n))
    }
  } catch (e) {
    playbooks.value = []
    playbooksError.value = e.response?.status === 503
      ? 'Start the agent to choose which playbooks to expose.'
      : 'Could not load this agent’s playbooks.'
  }
}

const load = async () => {
  await loadConnectorState()
  await loadPlaybooks()
}

const saveAllowList = async () => {
  busy.value = true
  try {
    const body = exposeAll.value
      ? { expose_all_playbooks: true }
      : { exposed_playbooks: selected.value }
    const { data } = await api.put(`/api/agents/${props.agentName}/connector`, body)
    syncFromConnector(data.exposed_playbooks)
    notify('Exposed playbooks updated.')
  } catch (e) {
    notify(e.response?.data?.detail || 'Failed to update playbooks', true)
  } finally {
    busy.value = false
  }
}

// Coalesce a burst of checkbox toggles into one PUT (each send is the full set,
// so last-write-wins is safe). The "expose all" toggle saves immediately.
let saveTimer = null
const debouncedSaveAllowList = () => {
  clearTimeout(saveTimer)
  saveTimer = setTimeout(saveAllowList, 400)
}

watch(() => props.agentName, load)
onMounted(load)
</script>
