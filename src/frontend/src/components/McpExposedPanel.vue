<template>
  <div>
    <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Expose via MCP</h3>
    <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
      Publish this agent as a dedicated MCP tool. When enabled, connected MCP
      clients get a first-class
      <code class="font-mono text-xs bg-gray-100 dark:bg-gray-800 px-1 py-0.5 rounded">{{ status.tool_name || 'chat_with_…' }}</code>
      tool — functionally identical to
      <code class="font-mono text-xs bg-gray-100 dark:bg-gray-800 px-1 py-0.5 rounded">chat_with_agent</code>
      with this agent pre-filled. Access is unchanged: callers still need
      ownership or a share to actually chat.
    </p>

    <!-- Toggle -->
    <div class="flex items-start gap-3 mb-4">
      <label class="relative inline-flex items-center cursor-pointer mt-1">
        <input
          type="checkbox"
          class="sr-only peer"
          :checked="status.enabled"
          :disabled="toggleLoading || statusLoading"
          @change="onToggle($event.target.checked)"
        />
        <div class="w-11 h-6 bg-gray-200 dark:bg-gray-700 peer-focus:outline-none peer-focus:ring-2 peer-focus:ring-action-primary-500 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-0.5 after:left-0.5 after:bg-white after:border after:border-gray-300 after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-action-primary-600"></div>
      </label>
      <div class="flex-1">
        <div class="text-sm font-medium text-gray-900 dark:text-gray-100">
          {{ status.enabled ? 'Exposed' : 'Not exposed' }}
        </div>
        <div class="text-xs text-gray-500 dark:text-gray-400">
          No agent restart needed — the change appears in connected MCP clients
          within a few seconds (the MCP server polls and refreshes their tool list).
        </div>
      </div>
    </div>

    <!-- Tool name -->
    <div
      v-if="status.tool_name"
      class="mb-2 text-sm text-gray-600 dark:text-gray-400"
    >
      Tool name:
      <code class="font-mono text-xs bg-gray-100 dark:bg-gray-800 px-1 py-0.5 rounded">{{ status.tool_name }}</code>
    </div>

    <!-- Connect an external client (#1575) — one-click, ready-to-paste config
         with a least-privilege, agent-scoped, revocable key already embedded.
         Reuses the per-agent MCP connector (scope='connector' key); the config
         exposes this agent's chat plus the owner-selected playbooks as tools. -->
    <div
      v-if="status.enabled && !connectorAccessDenied"
      class="mt-5 pt-5 border-t border-gray-200 dark:border-gray-700"
    >
      <h4 class="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-1">
        Connect an external client
      </h4>
      <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">
        Copy a ready-to-paste config (Claude Code, Cursor, Claude Desktop) with a
        scoped API key already embedded — no separate key steps. The key reaches
        only this agent and is revocable below.
      </p>

      <!-- Loading connector state -->
      <div
        v-if="connectorLoading"
        class="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400"
      >
        <svg class="animate-spin h-4 w-4" fill="none" viewBox="0 0 24 24">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
        </svg>
        Loading connection…
      </div>

      <template v-else>
        <!-- No key yet: one click mints + copies a live config -->
        <div v-if="!connector.has_key">
          <button
            type="button"
            @click="mintAndCopy"
            :disabled="connectorBusy"
            class="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-md text-white disabled:opacity-50 transition-colors duration-300"
            :class="copied === 'config' ? 'bg-status-success-600 hover:bg-status-success-600' : 'bg-action-primary-600 hover:bg-action-primary-700'"
          >
            <svg v-if="copied === 'config'" class="h-4 w-4 copied-pop" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>
            <svg v-else-if="!connectorBusy" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 4h8a2 2 0 012 2v6a2 2 0 01-2 2h-8a2 2 0 01-2-2v-6a2 2 0 012-2z"/></svg>
            {{ copied === 'config' ? 'Copied!' : (connectorBusy ? 'Generating…' : 'Copy connection config') }}
          </button>
          <p class="text-xs text-gray-500 dark:text-gray-400 mt-2">
            Generates a scoped key and copies a ready-to-paste
            <code class="font-mono">.mcp.json</code>. The key is shown only once.
          </p>
        </div>

        <!-- Key exists: reuse; secret is copy-once, so re-copy is placeholder + regenerate -->
        <div v-else class="space-y-3">
          <div class="p-3 bg-gray-50 dark:bg-gray-900/50 rounded-lg border border-gray-200 dark:border-gray-700">
            <div class="flex items-center justify-between gap-2 flex-wrap">
              <div class="flex items-center gap-2 text-sm">
                <span
                  class="inline-block w-2.5 h-2.5 rounded-full"
                  :class="connector.enabled ? 'bg-status-success-500' : 'bg-gray-400 dark:bg-gray-600'"
                ></span>
                <span class="text-gray-900 dark:text-gray-100">
                  Connection key
                  <code class="text-xs">{{ connector.key_prefix }}…</code>
                </span>
                <span
                  v-if="!connector.enabled"
                  class="px-1.5 py-0.5 text-xs rounded bg-gray-200 dark:bg-gray-700 text-gray-600 dark:text-gray-300"
                >Disabled</span>
              </div>
              <div class="flex items-center gap-2">
                <button
                  type="button"
                  @click="copyExistingConfig"
                  :disabled="connectorBusy"
                  class="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-md disabled:opacity-50 transition-colors duration-300"
                  :class="copied === 'existing' ? 'bg-status-success-100 dark:bg-status-success-900/40 text-status-success-700 dark:text-status-success-300' : 'text-gray-700 dark:text-gray-200 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600'"
                >
                  <svg v-if="copied === 'existing'" class="h-3.5 w-3.5 copied-pop" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>
                  {{ copied === 'existing' ? 'Copied!' : 'Copy config' }}
                </button>
                <button
                  type="button"
                  @click="mintAndCopy"
                  :disabled="connectorBusy"
                  class="px-3 py-1.5 text-sm font-medium rounded-md text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50"
                >{{ connectorBusy ? 'Working…' : 'Regenerate & copy' }}</button>
                <button
                  type="button"
                  @click="revokeKey"
                  :disabled="connectorBusy"
                  class="px-3 py-1.5 text-sm font-medium rounded-md text-white bg-status-danger-600 hover:bg-status-danger-700 disabled:opacity-50"
                >Revoke</button>
              </div>
            </div>
            <p class="text-xs text-gray-500 dark:text-gray-400 mt-2">
              The live key is shown only when generated. “Copy config” copies the
              config with a placeholder — use “Regenerate &amp; copy” to embed a
              fresh live key (this invalidates the old one). Revoking severs any
              connected client.
            </p>
          </div>

          <!-- Owner-selected playbooks exposed as tools (reuses the connector allow-list) -->
          <ExposedToolsPanel :agent-name="agentName" />
        </div>

        <!-- One-time secret + per-client snippets after mint/regenerate -->
        <div
          v-if="freshSnippets.length"
          class="mt-3 p-4 rounded-lg border border-state-autonomous-200 dark:border-state-autonomous-800/40 bg-state-autonomous-50 dark:bg-state-autonomous-900/20"
        >
          <div class="flex items-center justify-between mb-2">
            <p class="text-sm font-medium text-state-autonomous-900 dark:text-state-autonomous-200">
              Copied to clipboard — this config contains a live key, shown only once.
            </p>
            <button
              type="button"
              @click="freshSnippets = []"
              class="text-xs text-gray-500 dark:text-gray-400 hover:underline"
            >Dismiss</button>
          </div>
          <div class="space-y-3">
            <div v-for="s in freshSnippets" :key="s.client">
              <div class="flex items-center justify-between mb-1">
                <span class="text-xs font-medium text-gray-700 dark:text-gray-200">{{ s.label }}</span>
                <button
                  type="button"
                  @click="copyText(s.content, s.client)"
                  class="inline-flex items-center gap-1 text-xs hover:underline transition-colors duration-300"
                  :class="copied === s.client ? 'text-status-success-600 dark:text-status-success-400' : 'text-action-primary-600'"
                >
                  <svg v-if="copied === s.client" class="h-3 w-3 copied-pop" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="3"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>
                  {{ copied === s.client ? 'Copied!' : 'Copy' }}
                </button>
              </div>
              <pre class="text-xs bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded p-2 overflow-x-auto"><code>{{ s.content }}</code></pre>
              <p v-if="s.note" class="text-xs text-gray-500 dark:text-gray-400 mt-1">{{ s.note }}</p>
            </div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { useAgentsStore } from '../stores/agents'
import api from '../api'
import { copyToClipboard } from '../utils/clipboard'
import ExposedToolsPanel from './ExposedToolsPanel.vue'

const props = defineProps({
  agentName: { type: String, required: true },
  // Notification host passed down from SettingsPanel — positional (message, type),
  // same contract as GuardrailsPanel. null when rendered standalone.
  notify: { type: Function, default: null },
})

const agentsStore = useAgentsStore()
function notifyUser(message, type = 'success') {
  if (props.notify) props.notify(message, type)
}

const status = ref({ enabled: false, tool_name: '' })
const statusLoading = ref(false)
const toggleLoading = ref(false)

// --- Connect-an-external-client state (#1575) ---
const connector = ref({ has_key: false, enabled: false, key_prefix: null, mcp_url: null, snippets: [] })
const connectorLoading = ref(false)
const connectorBusy = ref(false)
const connectorAccessDenied = ref(false)
const freshSnippets = ref([])

// Transient "Copied!" affordance — holds the id of the last-copied target
// ('config' | 'existing' | a snippet client) for ~1.6s, then clears. One shared
// timer so rapid clicks across buttons don't overlap.
const copied = ref('')
let copiedTimer = null
function flashCopied(id) {
  copied.value = id
  if (copiedTimer) clearTimeout(copiedTimer)
  copiedTimer = setTimeout(() => { copied.value = '' }, 1600)
}
onUnmounted(() => { if (copiedTimer) clearTimeout(copiedTimer) })

// The `.mcp.json` block is the canonical one-click paste target; fall back to
// the first snippet if the client shape ever changes.
function pickConfigSnippet(snippets) {
  if (!Array.isArray(snippets) || !snippets.length) return null
  return snippets.find((s) => s.client === 'claude-code-json') || snippets[0]
}

async function loadStatus() {
  statusLoading.value = true
  try {
    status.value = await agentsStore.getMcpExposedStatus(props.agentName)
    if (status.value.enabled) await loadConnector()
  } catch (e) {
    notifyUser(`Failed to load MCP exposure status: ${e.message}`, 'error')
  } finally {
    statusLoading.value = false
  }
}

async function loadConnector() {
  connectorLoading.value = true
  connectorAccessDenied.value = false
  try {
    const { data } = await api.get(`/api/agents/${props.agentName}/connector`)
    connector.value = data
  } catch (e) {
    if (e.response?.status === 403) connectorAccessDenied.value = true
    else notifyUser(e.response?.data?.detail || `Failed to load connection config: ${e.message}`, 'error')
  } finally {
    connectorLoading.value = false
  }
}

async function onToggle(enabled) {
  toggleLoading.value = true
  try {
    const resp = await agentsStore.setMcpExposed(props.agentName, enabled)
    status.value = {
      ...status.value,
      enabled: resp.enabled,
      tool_name: resp.tool_name,
    }
    notifyUser(
      enabled
        ? `Exposed as MCP tool "${resp.tool_name}" — appears in clients shortly.`
        : 'No longer exposed via MCP.',
      'success'
    )
    if (enabled) await loadConnector()
    else freshSnippets.value = []
  } catch (e) {
    notifyUser(
      e.response?.data?.detail || `Failed to toggle MCP exposure: ${e.message}`,
      'error'
    )
    // Reload to reflect actual state
    await loadStatus()
  } finally {
    toggleLoading.value = false
  }
}

// Mint (or regenerate) the scoped key and copy the ready-to-paste config in one
// click — this is the moment the live secret exists, so it's the only path that
// yields a paste-and-it-works config.
async function mintAndCopy() {
  connectorBusy.value = true
  try {
    const { data } = await api.post(`/api/agents/${props.agentName}/connector/key`)
    const snippets = data.snippets || []
    freshSnippets.value = snippets
    const cfg = pickConfigSnippet(snippets)
    const didCopy = cfg ? await copyToClipboard(cfg.content) : false
    if (didCopy) flashCopied('config')
    await loadConnector()
    notifyUser(
      didCopy
        ? 'Connection config copied — contains a live key, shown only once.'
        : 'Connection key generated — copy the config below.',
      didCopy ? 'success' : 'info'
    )
  } catch (e) {
    notifyUser(e.response?.data?.detail || `Failed to generate connection config: ${e.message}`, 'error')
  } finally {
    connectorBusy.value = false
  }
}

// Re-copy for an existing key: the live secret is copy-once, so this copies the
// placeholder config (correct URL/transport) and points the user at Regenerate.
async function copyExistingConfig() {
  const cfg = pickConfigSnippet(connector.value.snippets)
  if (!cfg) {
    notifyUser('No connection config available — regenerate the key.', 'error')
    return
  }
  const didCopy = await copyToClipboard(cfg.content)
  if (didCopy) flashCopied('existing')
  notifyUser(
    didCopy
      ? 'Config copied (key placeholder). Use “Regenerate & copy” to embed a live key.'
      : 'Copy failed — select and copy manually.',
    didCopy ? 'success' : 'error'
  )
}

async function revokeKey() {
  connectorBusy.value = true
  try {
    await api.delete(`/api/agents/${props.agentName}/connector/key`)
    freshSnippets.value = []
    await loadConnector()
    notifyUser('Connection key revoked — any connected client is now severed.', 'success')
  } catch (e) {
    notifyUser(e.response?.data?.detail || `Failed to revoke key: ${e.message}`, 'error')
  } finally {
    connectorBusy.value = false
  }
}

async function copyText(text, id = '') {
  const ok = await copyToClipboard(text)
  if (ok && id) flashCopied(id)
  notifyUser(ok ? 'Copied to clipboard.' : 'Copy failed — select and copy manually.', ok ? 'success' : 'error')
}

onMounted(loadStatus)
</script>

<style scoped>
/* Checkmark "pop" when a copy succeeds — scales/fades in, respects
   reduced-motion. */
.copied-pop {
  animation: copied-pop 0.35s cubic-bezier(0.34, 1.56, 0.64, 1);
}
@keyframes copied-pop {
  0% { transform: scale(0.4); opacity: 0; }
  60% { transform: scale(1.15); opacity: 1; }
  100% { transform: scale(1); opacity: 1; }
}
@media (prefers-reduced-motion: reduce) {
  .copied-pop { animation: none; }
}
</style>
