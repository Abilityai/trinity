<template>
  <div>
    <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">MCP Connector</h3>
    <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
      Let someone add this agent to their AI client (Claude Code, Cursor, Claude Desktop) in one line.
      The agent's playbooks become tools they can run; the agent keeps all credentials server-side.
      Only a scoped, revocable key reaches the client.
    </p>

    <!-- Loading -->
    <div v-if="loading" class="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
      <svg class="animate-spin h-4 w-4" fill="none" viewBox="0 0 24 24">
        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
      </svg>
      Loading…
    </div>

    <div v-else-if="accessDenied" class="text-sm text-gray-500 dark:text-gray-400">
      Only the agent owner can manage the connector.
    </div>

    <div v-else class="space-y-5">
      <!-- No key yet -->
      <div v-if="!status.has_key" class="p-4 bg-gray-50 dark:bg-gray-900/50 rounded-lg border border-gray-200 dark:border-gray-700">
        <p class="text-sm text-gray-700 dark:text-gray-200 mb-3">
          No connector key yet. Generate one to get copy-paste setup instructions for each client.
        </p>
        <button
          type="button"
          @click="regenerateKey"
          :disabled="busy"
          class="inline-flex items-center px-4 py-2 text-sm font-medium rounded-md text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50"
        >{{ busy ? 'Generating…' : 'Generate connector key' }}</button>
      </div>

      <!-- Active connector -->
      <div v-else class="space-y-5">
        <!-- Status row -->
        <div class="p-4 bg-gray-50 dark:bg-gray-900/50 rounded-lg border border-gray-200 dark:border-gray-700">
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-3">
              <span class="inline-block w-2.5 h-2.5 rounded-full" :class="status.enabled ? 'bg-status-success-500' : 'bg-gray-400 dark:bg-gray-600'"></span>
              <div>
                <p class="text-sm font-medium text-gray-900 dark:text-white">
                  Key <code class="text-xs">{{ status.key_prefix }}…</code>
                  <span v-if="!status.enabled" class="ml-2 px-1.5 py-0.5 text-xs rounded bg-gray-200 dark:bg-gray-700 text-gray-600 dark:text-gray-300">Disabled</span>
                </p>
                <p class="text-xs text-gray-500 dark:text-gray-400">The full key is shown only once, at generation.</p>
              </div>
            </div>
            <div class="flex items-center gap-2">
              <button type="button" @click="toggleEnabled" :disabled="busy"
                class="px-3 py-1.5 text-sm font-medium rounded-md text-gray-700 dark:text-gray-200 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 disabled:opacity-50">
                {{ status.enabled ? 'Disable' : 'Enable' }}
              </button>
              <button type="button" @click="regenerateKey" :disabled="busy"
                class="px-3 py-1.5 text-sm font-medium rounded-md text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50">
                Regenerate
              </button>
              <button type="button" @click="revokeKey" :disabled="busy"
                class="px-3 py-1.5 text-sm font-medium rounded-md text-white bg-status-danger-600 hover:bg-status-danger-700 disabled:opacity-50">
                Revoke
              </button>
            </div>
          </div>
        </div>

        <!-- Exposed-playbooks picker — extracted to a standalone, decoupled
             component (trinity-enterprise#55) so it can be reused across
             client-sharing channels and this key block can be swapped for the
             SSO/OAuth variant without reworking the picker. -->
        <ExposedToolsPanel :agent-name="agentName" />
      </div>

      <!-- One-time secret + per-client snippets (after generate/regenerate) -->
      <div v-if="freshSnippets.length" class="p-4 rounded-lg border border-state-autonomous-200 dark:border-state-autonomous-800/40 bg-state-autonomous-50 dark:bg-state-autonomous-900/20">
        <div class="flex items-center justify-between mb-2">
          <p class="text-sm font-medium text-state-autonomous-900 dark:text-state-autonomous-200">
            Copy your setup — the key is shown only once.
          </p>
          <button type="button" @click="freshSnippets = []" class="text-xs text-gray-500 dark:text-gray-400 hover:underline">Dismiss</button>
        </div>
        <div class="space-y-3">
          <div v-for="s in freshSnippets" :key="s.client">
            <div class="flex items-center justify-between mb-1">
              <span class="text-xs font-medium text-gray-700 dark:text-gray-200">{{ s.label }}</span>
              <button type="button" @click="copy(s.content)" class="text-xs text-action-primary-600 hover:underline">Copy</button>
            </div>
            <pre class="text-xs bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded p-2 overflow-x-auto"><code>{{ s.content }}</code></pre>
            <p v-if="s.note" class="text-xs text-gray-500 dark:text-gray-400 mt-1">{{ s.note }}</p>
          </div>
        </div>
      </div>

      <p v-if="message" class="text-xs" :class="messageError ? 'text-status-danger-600' : 'text-status-success-600'">{{ message }}</p>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, watch } from 'vue'
import api from '../api'
import ExposedToolsPanel from './ExposedToolsPanel.vue'

const props = defineProps({
  agentName: { type: String, required: true },
})

const loading = ref(true)
const busy = ref(false)
const accessDenied = ref(false)
const status = ref({ has_key: false, enabled: false, key_prefix: null, mcp_url: null })
const freshSnippets = ref([])
const message = ref('')
const messageError = ref(false)

const notify = (text, isError = false) => {
  message.value = text
  messageError.value = isError
}

const loadStatus = async () => {
  loading.value = true
  accessDenied.value = false
  try {
    const { data } = await api.get(`/api/agents/${props.agentName}/connector`)
    status.value = data
  } catch (e) {
    if (e.response?.status === 403) accessDenied.value = true
    else notify(e.response?.data?.detail || 'Failed to load connector', true)
  } finally {
    loading.value = false
  }
}

const regenerateKey = async () => {
  busy.value = true
  try {
    const { data } = await api.post(`/api/agents/${props.agentName}/connector/key`)
    freshSnippets.value = data.snippets || []
    notify('Connector key generated — copy your setup below.')
    await loadStatus()
  } catch (e) {
    notify(e.response?.data?.detail || 'Failed to generate key', true)
  } finally {
    busy.value = false
  }
}

const revokeKey = async () => {
  busy.value = true
  try {
    await api.delete(`/api/agents/${props.agentName}/connector/key`)
    freshSnippets.value = []
    notify('Connector key revoked.')
    await loadStatus()
  } catch (e) {
    notify(e.response?.data?.detail || 'Failed to revoke key', true)
  } finally {
    busy.value = false
  }
}

const toggleEnabled = async () => {
  busy.value = true
  try {
    const { data } = await api.put(`/api/agents/${props.agentName}/connector`, { enabled: !status.value.enabled })
    status.value = data
  } catch (e) {
    notify(e.response?.data?.detail || 'Failed to update connector', true)
  } finally {
    busy.value = false
  }
}

const copy = async (text) => {
  try {
    await navigator.clipboard.writeText(text)
    notify('Copied to clipboard.')
  } catch {
    notify('Copy failed — select and copy manually.', true)
  }
}

watch(() => props.agentName, () => loadStatus())
onMounted(() => loadStatus())
</script>
