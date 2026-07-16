<template>
  <div>
    <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Expose via A2A</h3>
    <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
      Make this agent reachable over the open
      <a href="https://a2a-protocol.org" target="_blank" rel="noopener" class="text-action-primary-600 hover:underline">A2A protocol</a>
      so external orchestrators (Google ADK, LangChain, Bedrock, another Trinity) can discover its
      Agent Card and task it. Callers authenticate with a Trinity MCP API key; only identities you
      allow-list below may task it.
    </p>

    <!-- Toggle -->
    <div class="flex items-start gap-3 mb-4">
      <label class="relative inline-flex items-center cursor-pointer mt-1">
        <input
          type="checkbox"
          class="sr-only peer"
          :checked="config.a2a_exposed"
          :disabled="toggleLoading || loading"
          @change="onToggle($event.target.checked)"
        />
        <div class="w-11 h-6 bg-gray-200 dark:bg-gray-700 peer-focus:outline-none peer-focus:ring-2 peer-focus:ring-action-primary-500 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-0.5 after:left-0.5 after:bg-white after:border after:border-gray-300 after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-action-primary-600"></div>
      </label>
      <div class="flex-1">
        <div class="text-sm font-medium text-gray-900 dark:text-gray-100">
          {{ config.a2a_exposed ? 'Exposed over A2A' : 'Not exposed' }}
        </div>
        <div class="text-xs text-gray-500 dark:text-gray-400">
          {{ config.a2a_exposed
            ? 'External orchestrators can discover and task this agent (subject to the allow-list below).'
            : 'Off by default. Enable to publish the public Agent Card and accept inbound A2A tasks.' }}
        </div>
      </div>
    </div>

    <!-- Not-exposed explainer (no dead empty state) -->
    <div
      v-if="!config.a2a_exposed"
      class="mt-4 p-4 rounded-lg bg-gray-50 dark:bg-gray-900/50 border border-gray-200 dark:border-gray-700 text-sm text-gray-600 dark:text-gray-400"
    >
      While off, the public routes return <code class="font-mono text-xs">404</code> — the agent is
      invisible to the A2A ecosystem. Toggle <span class="font-medium">Expose over A2A</span> above to
      publish its card and start accepting tasks.
    </div>

    <template v-else>
      <!-- Agent Card URL (one-click copy, #1575 idiom) -->
      <div class="mt-5 pt-5 border-t border-gray-200 dark:border-gray-700">
        <h4 class="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-1">Agent Card URL</h4>
        <p class="text-xs text-gray-500 dark:text-gray-400 mb-2">
          Give this discovery URL to an external A2A client — its SDK fetches the card, then tasks the
          agent over JSON-RPC. Auth is a Trinity MCP API key as a Bearer token.
        </p>
        <div class="flex items-center gap-2">
          <code class="flex-1 font-mono text-xs bg-gray-100 dark:bg-gray-800 px-2 py-1.5 rounded overflow-x-auto whitespace-nowrap">{{ cardUrl }}</code>
          <button
            type="button"
            @click="copyText(cardUrl, 'card')"
            class="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm font-semibold rounded-md transition-all duration-300 shrink-0"
            :class="copied === 'card'
              ? 'bg-status-success-600 text-white ring-2 ring-status-success-400'
              : 'text-gray-700 dark:text-gray-200 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600'"
          >
            <svg v-if="copied === 'card'" class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="3"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>
            {{ copied === 'card' ? 'Copied!' : 'Copy' }}
          </button>
        </div>
      </div>

      <!-- Advertised skills (read-only) -->
      <div class="mt-5 pt-5 border-t border-gray-200 dark:border-gray-700">
        <h4 class="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-1">Advertised skills</h4>
        <p class="text-xs text-gray-500 dark:text-gray-400 mb-2">
          What an external caller sees on the card (derived from the agent's <code class="font-mono">template.yaml</code>).
        </p>
        <div v-if="cardLoading" class="text-sm text-gray-500 dark:text-gray-400">Loading card…</div>
        <div v-else-if="skills.length" class="flex flex-wrap gap-2">
          <span
            v-for="s in skills"
            :key="s.id || s.name"
            class="px-2 py-0.5 text-xs rounded-full bg-action-primary-50 dark:bg-action-primary-900/30 text-action-primary-700 dark:text-action-primary-300 border border-action-primary-200 dark:border-action-primary-800"
          >{{ s.name || s.id }}</span>
        </div>
        <div v-else class="text-sm text-gray-500 dark:text-gray-400">
          No skills advertised — the agent's template declares no capabilities.
        </div>
      </div>

      <!-- Inbound allow-list -->
      <div class="mt-5 pt-5 border-t border-gray-200 dark:border-gray-700">
        <h4 class="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-1">Inbound allow-list</h4>
        <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">
          Trinity accounts permitted to task this agent inbound — the caller's account
          <strong>email</strong> (or username, when the account has no email). Empty =
          any authenticated owner/shared caller. Non-empty = only these identities.
        </p>
        <div v-if="config.inbound_allowlist.length" class="space-y-2 mb-3">
          <div
            v-for="id in config.inbound_allowlist"
            :key="id"
            class="flex items-center justify-between gap-2 px-3 py-1.5 rounded-md bg-gray-50 dark:bg-gray-900/50 border border-gray-200 dark:border-gray-700"
          >
            <code class="font-mono text-xs text-gray-800 dark:text-gray-200 overflow-x-auto">{{ id }}</code>
            <button
              type="button"
              @click="removeIdentity(id)"
              :disabled="busy"
              class="text-status-danger-600 hover:text-status-danger-700 text-xs font-medium disabled:opacity-50"
            >Remove</button>
          </div>
        </div>
        <div v-else class="text-sm text-gray-500 dark:text-gray-400 mb-3">
          No restriction — any authenticated owner/shared caller may task the agent.
        </div>
        <form class="flex items-center gap-2" @submit.prevent="addIdentity">
          <input
            v-model.trim="newIdentity"
            type="text"
            placeholder="caller@example.com"
            class="flex-1 px-3 py-1.5 text-sm rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-action-primary-500 focus:outline-none"
          />
          <button
            type="submit"
            :disabled="busy || !newIdentity"
            class="px-3 py-1.5 text-sm font-medium rounded-md text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50"
          >Add</button>
        </form>
      </div>

      <!-- Outbound endpoint registry -->
      <div class="mt-5 pt-5 border-t border-gray-200 dark:border-gray-700">
        <h4 class="text-sm font-semibold text-gray-900 dark:text-gray-100 mb-1">Outbound endpoints</h4>
        <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">
          External A2A endpoints this agent may call (for the outbound <code class="font-mono">call_a2a_agent</code> tool).
          Credentials are stored encrypted and never shown again.
        </p>
        <div v-if="config.outbound_endpoints.length" class="space-y-2 mb-3">
          <div
            v-for="ep in config.outbound_endpoints"
            :key="ep.id"
            class="flex items-center justify-between gap-2 px-3 py-2 rounded-md bg-gray-50 dark:bg-gray-900/50 border border-gray-200 dark:border-gray-700"
          >
            <div class="min-w-0">
              <div class="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{{ ep.name }}</div>
              <div class="text-xs text-gray-500 dark:text-gray-400 font-mono truncate">{{ ep.url }}</div>
            </div>
            <div class="flex items-center gap-2 shrink-0">
              <span
                v-if="ep.has_credentials"
                class="px-1.5 py-0.5 text-xs rounded bg-status-success-100 dark:bg-status-success-900/40 text-status-success-700 dark:text-status-success-300"
              >🔒 credentialed</span>
              <button
                type="button"
                @click="removeEndpoint(ep.id)"
                :disabled="busy"
                class="text-status-danger-600 hover:text-status-danger-700 text-xs font-medium disabled:opacity-50"
              >Remove</button>
            </div>
          </div>
        </div>
        <div v-else class="text-sm text-gray-500 dark:text-gray-400 mb-3">No outbound endpoints registered.</div>
        <form class="grid grid-cols-1 sm:grid-cols-2 gap-2" @submit.prevent="addEndpoint">
          <input
            v-model.trim="newEndpoint.name"
            type="text"
            placeholder="Label (e.g. acme-orchestrator)"
            class="px-3 py-1.5 text-sm rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-action-primary-500 focus:outline-none"
          />
          <input
            v-model.trim="newEndpoint.url"
            type="url"
            placeholder="https://partner.example/a2a"
            class="px-3 py-1.5 text-sm rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-action-primary-500 focus:outline-none"
          />
          <input
            v-model="newEndpoint.credentials"
            type="password"
            placeholder="Credential / token (optional)"
            autocomplete="new-password"
            class="px-3 py-1.5 text-sm rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:ring-2 focus:ring-action-primary-500 focus:outline-none"
          />
          <button
            type="submit"
            :disabled="busy || !newEndpoint.name || !newEndpoint.url"
            class="px-3 py-1.5 text-sm font-medium rounded-md text-white bg-action-primary-600 hover:bg-action-primary-700 disabled:opacity-50"
          >Register endpoint</button>
        </form>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { copyToClipboard } from '../utils/clipboard'

const props = defineProps({
  agentName: { type: String, required: true },
  // Positional (message, type) notification host — same contract as the other
  // Settings panels. null when rendered standalone.
  notify: { type: Function, default: null },
})

const agentsStore = useAgentsStore()
function notifyUser(message, type = 'success') {
  if (props.notify) props.notify(message, type)
}

const config = ref({ a2a_exposed: false, inbound_allowlist: [], outbound_endpoints: [] })
const loading = ref(false)
const toggleLoading = ref(false)
const busy = ref(false)

const skills = ref([])
const cardLoading = ref(false)

const newIdentity = ref('')
const newEndpoint = ref({ name: '', url: '', credentials: '' })

// The canonical A2A discovery URL an external orchestrator fetches (the public,
// unauthenticated well-known route served by the inbound server, ent#157).
const cardUrl = computed(
  () => `${window.location.origin}/a2a/${props.agentName}/.well-known/agent-card.json`
)

// Transient "Copied!" affordance.
const copied = ref('')
let copiedTimer = null
function flashCopied(id) {
  copied.value = id
  if (copiedTimer) clearTimeout(copiedTimer)
  copiedTimer = setTimeout(() => { copied.value = '' }, 1600)
}
onUnmounted(() => { if (copiedTimer) clearTimeout(copiedTimer) })

async function load() {
  loading.value = true
  try {
    config.value = await agentsStore.getA2aConfig(props.agentName)
    if (config.value.a2a_exposed) loadSkills()
  } catch (e) {
    notifyUser(e.response?.data?.detail || `Failed to load A2A config: ${e.message}`, 'error')
  } finally {
    loading.value = false
  }
}

async function loadSkills() {
  cardLoading.value = true
  try {
    const card = await agentsStore.getA2aCard(props.agentName)
    skills.value = Array.isArray(card?.skills) ? card.skills : []
  } catch {
    skills.value = []  // best-effort — the card is a read-only convenience here
  } finally {
    cardLoading.value = false
  }
}

async function onToggle(enabled) {
  toggleLoading.value = true
  try {
    config.value = await agentsStore.setA2aExposure(props.agentName, enabled)
    notifyUser(enabled ? 'Agent exposed over A2A.' : 'No longer exposed over A2A.', 'success')
    if (enabled) loadSkills()
    else skills.value = []
  } catch (e) {
    notifyUser(e.response?.data?.detail || `Failed to toggle A2A exposure: ${e.message}`, 'error')
    await load()  // reflect actual state
  } finally {
    toggleLoading.value = false
  }
}

async function addIdentity() {
  if (!newIdentity.value) return
  busy.value = true
  try {
    config.value = await agentsStore.updateA2aAllowlist(props.agentName, { add: [newIdentity.value] })
    newIdentity.value = ''
    notifyUser('Identity added to the inbound allow-list.', 'success')
  } catch (e) {
    notifyUser(e.response?.data?.detail || `Failed to add identity: ${e.message}`, 'error')
  } finally {
    busy.value = false
  }
}

async function removeIdentity(id) {
  busy.value = true
  try {
    config.value = await agentsStore.updateA2aAllowlist(props.agentName, { remove: [id] })
    notifyUser('Identity removed.', 'success')
  } catch (e) {
    notifyUser(e.response?.data?.detail || `Failed to remove identity: ${e.message}`, 'error')
  } finally {
    busy.value = false
  }
}

async function addEndpoint() {
  if (!newEndpoint.value.name || !newEndpoint.value.url) return
  busy.value = true
  try {
    await agentsStore.registerA2aEndpoint(props.agentName, { ...newEndpoint.value })
    newEndpoint.value = { name: '', url: '', credentials: '' }
    await load()
    notifyUser('Outbound endpoint registered.', 'success')
  } catch (e) {
    notifyUser(e.response?.data?.detail || `Failed to register endpoint: ${e.message}`, 'error')
  } finally {
    busy.value = false
  }
}

async function removeEndpoint(id) {
  busy.value = true
  try {
    await agentsStore.removeA2aEndpoint(props.agentName, id)
    await load()
    notifyUser('Endpoint removed.', 'success')
  } catch (e) {
    notifyUser(e.response?.data?.detail || `Failed to remove endpoint: ${e.message}`, 'error')
  } finally {
    busy.value = false
  }
}

async function copyText(text, id = '') {
  const ok = await copyToClipboard(text)
  if (ok && id) flashCopied(id)
  notifyUser(ok ? 'Copied to clipboard.' : 'Copy failed — select and copy manually.', ok ? 'success' : 'error')
}

onMounted(load)
</script>
