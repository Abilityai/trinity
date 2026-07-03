<template>
  <div class="p-6 space-y-8">
    <!-- Framing: Google-Docs-style "share this agent" (trinity-enterprise#18) -->
    <div>
      <h3 class="text-lg font-medium text-gray-900 dark:text-white">Share this agent</h3>
      <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
        Let external clients reach this agent through channels — Slack, Telegram, WhatsApp, voice, and public links.
        Trinity <strong>operators</strong> (your teammates) are managed on the <span class="font-medium">Access</span> tab.
      </p>
    </div>

    <!-- External access policy: single Restricted ↔ Open control (#18) -->
    <div>
      <h4 class="text-sm font-medium text-gray-900 dark:text-gray-100">Who can chat with this agent?</h4>

      <div
        class="mt-3 inline-flex rounded-lg border border-gray-300 dark:border-gray-600 p-1 bg-gray-100 dark:bg-gray-800"
        role="group"
        aria-label="External access policy"
      >
        <button
          type="button"
          :disabled="policyLoading"
          :aria-pressed="accessMode === 'restricted'"
          @click="setAccessMode('restricted')"
          :class="['flex items-center gap-2 px-4 py-1.5 rounded-md text-sm font-medium transition-colors disabled:opacity-50',
            accessMode === 'restricted'
              ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-white shadow-sm'
              : 'text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white']"
        >
          <span aria-hidden="true">🔒</span> Restricted
        </button>
        <button
          type="button"
          :disabled="policyLoading"
          :aria-pressed="accessMode === 'open'"
          @click="setAccessMode('open')"
          :class="['flex items-center gap-2 px-4 py-1.5 rounded-md text-sm font-medium transition-colors disabled:opacity-50',
            accessMode === 'open'
              ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-white shadow-sm'
              : 'text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white']"
        >
          <span aria-hidden="true">🌐</span> Open
        </button>
      </div>

      <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">
        <template v-if="accessMode === 'open'">
          <strong>Open</strong> — anyone with a verified email can chat without approval.
        </template>
        <template v-else>
          <strong>Restricted</strong> — only people you approve can chat. Everyone else lands in Pending requests below.
        </template>
        Either way, clients must verify their email first (Telegram users <code>/login</code>; Slack uses workspace email; web requires verification).
      </p>

      <!-- Dead-end heads-up: Restricted with nobody approved yet (#446) -->
      <div
        v-if="accessMode === 'restricted' && (!shares || shares.length === 0)"
        class="mt-4 rounded-lg bg-state-autonomous-50 dark:bg-state-autonomous-900/20 border border-state-autonomous-200 dark:border-state-autonomous-800/40 p-3 text-sm text-state-autonomous-900 dark:text-state-autonomous-200"
      >
        <strong>Heads up:</strong> access is Restricted and no one's approved yet — every verified client will wait in
        Pending requests until you approve them. Approve requests below, or switch to <strong>Open</strong>.
      </div>

      <!-- Pending access requests (external clients) -->
      <div v-if="pendingRequests.length > 0" class="mt-6">
        <h4 class="text-sm font-medium text-gray-900 dark:text-gray-100 mb-2">
          Pending requests ({{ pendingRequests.length }})
        </h4>
        <p class="text-xs text-gray-500 dark:text-gray-400 mb-2">
          External clients who verified their identity but aren't approved yet. Approving lets them chat.
        </p>
        <ul class="divide-y divide-gray-200 dark:divide-gray-700 border border-gray-200 dark:border-gray-700 rounded-lg">
          <li v-for="req in pendingRequests" :key="req.id" class="px-4 py-3 flex items-center justify-between">
            <div>
              <p class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ req.email }}</p>
              <p class="text-xs text-gray-500 dark:text-gray-400">
                via {{ req.channel || 'unknown' }} · {{ formatRequestedAt(req.requested_at) }}
              </p>
            </div>
            <div class="flex items-center gap-2">
              <button
                @click="decideRequest(req, true)"
                :disabled="decisionLoading === req.id"
                class="px-3 py-1 text-sm font-medium rounded-md text-white bg-status-success-600 hover:bg-status-success-700 disabled:opacity-50"
              >Approve</button>
              <button
                @click="decideRequest(req, false)"
                :disabled="decisionLoading === req.id"
                class="px-3 py-1 text-sm font-medium rounded-md text-gray-700 dark:text-gray-200 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 disabled:opacity-50"
              >Deny</button>
            </div>
          </li>
        </ul>
      </div>
    </div>

    <!-- Public chat model (#894): governs the model for ALL public-facing
         conversations (public link, channels, x402) — not the owner's own chats. -->
    <div>
      <h4 class="text-sm font-medium text-gray-900 dark:text-gray-100 mb-1">Public chat model</h4>
      <p class="text-xs text-gray-500 dark:text-gray-400 mb-2">
        The Claude model used for public-facing chats (public link, Slack/Telegram/WhatsApp, paid).
        Your own authenticated chats and scheduled runs are unaffected.
      </p>
      <select
        :value="publicChannelModel"
        @change="setPublicChannelModel($event.target.value)"
        :disabled="pcmSaving"
        class="block w-full sm:w-80 text-sm border border-gray-300 dark:border-gray-600 rounded-md px-3 py-2 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-action-primary-500 disabled:opacity-50"
      >
        <option value="">Use platform default{{ pcmDefault ? ` (${pcmDefault})` : '' }}</option>
        <option v-for="m in pcmAvailable" :key="m" :value="m">{{ m }}</option>
      </select>
    </div>

    <!-- Additional Instructions for public & channel chats (#1205) -->
    <div>
      <h4 class="text-sm font-medium text-gray-900 dark:text-gray-100 mb-1">Additional instructions <span class="font-normal text-gray-500 dark:text-gray-400">— public &amp; channel chats only</span></h4>
      <p class="text-xs text-gray-500 dark:text-gray-400 mb-2">
        Extra instructions injected into the agent's system prompt for <strong>outside audiences only</strong> — public links, Slack / Telegram / WhatsApp, and paid chat.
        Use it for persona, scope limits, disclaimers, or guardrails like "you're talking to an external customer, never reveal internal project names."
      </p>
      <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">
        Does <em>not</em> affect your own authenticated chats, scheduled runs, loops, or agent-to-agent calls. Leave empty to disable (no behavior change). Voice/VoIP has its own prompt.
      </p>

      <textarea
        v-model="publicPrompt"
        :maxlength="PUBLIC_PROMPT_MAX"
        rows="5"
        :disabled="publicPromptLoading"
        placeholder="e.g. Always answer in the visitor's language. Never mention internal codenames. Add the disclaimer: 'Responses are informational only.'"
        class="w-full px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-action-primary-500 disabled:bg-gray-100 dark:disabled:bg-gray-900 font-mono text-sm"
      ></textarea>

      <div class="mt-2 flex items-center justify-between">
        <span class="text-xs text-gray-400 dark:text-gray-500">{{ (publicPrompt || '').length }} / {{ PUBLIC_PROMPT_MAX }}</span>
        <div class="flex items-center gap-2">
          <button
            type="button"
            @click="clearPublicPrompt"
            :disabled="publicPromptLoading || !publicPrompt"
            class="px-3 py-1.5 text-sm font-medium rounded-md text-gray-700 dark:text-gray-200 bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 disabled:opacity-50"
          >Clear</button>
          <button
            type="button"
            @click="savePublicPrompt"
            :disabled="publicPromptLoading || !publicPromptDirty"
            class="inline-flex items-center px-4 py-1.5 text-sm font-medium rounded-md text-white bg-action-primary-600 hover:bg-action-primary-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-action-primary-500 dark:focus:ring-offset-gray-800 disabled:bg-gray-400 dark:disabled:bg-gray-600 disabled:cursor-not-allowed"
          >{{ publicPromptLoading ? 'Saving…' : 'Save' }}</button>
        </div>
      </div>
    </div>

    <!-- Channels: compact summary rows; config opens in a dialog (#19) -->
    <div>
      <h4 class="text-sm font-medium text-gray-900 dark:text-gray-100 mb-1">Channels</h4>
      <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">
        Connect this agent to messaging channels. Use <strong>Configure</strong> to set one up.
      </p>
      <div class="space-y-2">
        <ChannelConfigRow
          title="Slack"
          icon="💬"
          :agent-name="agentName"
          :status-url="`/api/agents/${agentName}/slack/channel`"
          :derive-status="slackStatus"
        >
          <SlackChannelPanel :agent-name="agentName" />
        </ChannelConfigRow>

        <ChannelConfigRow
          title="Telegram"
          icon="✈️"
          :agent-name="agentName"
          :status-url="`/api/agents/${agentName}/telegram`"
          :derive-status="telegramStatus"
        >
          <TelegramChannelPanel :agent-name="agentName" />
        </ChannelConfigRow>

        <ChannelConfigRow
          title="WhatsApp"
          icon="📱"
          :agent-name="agentName"
          :status-url="`/api/agents/${agentName}/whatsapp`"
          :derive-status="whatsappStatus"
        >
          <WhatsAppChannelPanel :agent-name="agentName" />
        </ChannelConfigRow>

        <ChannelConfigRow
          v-if="sessionsStore.voipAvailable"
          title="Voice calls"
          icon="📞"
          :agent-name="agentName"
          :status-url="`/api/agents/${agentName}/voip`"
          :derive-status="voipStatus"
        >
          <VoipChannelPanel :agent-name="agentName" />
        </ChannelConfigRow>

        <!-- MCP connector (ent#46) — gated on the mcp_connector entitlement -->
        <ChannelDisclosure
          v-if="enterpriseStore.isEntitled('mcp_connector')"
          title="MCP connector"
          subtitle="Add this agent to an AI client; playbooks become tools"
          icon="🔌"
        >
          <ConnectorChannelPanel :agent-name="agentName" />
        </ChannelDisclosure>
      </div>
    </div>

    <!-- Client Roster (#20) — external channel users (read-only) -->
    <div>
      <h4 class="text-sm font-medium text-gray-900 dark:text-gray-100 mb-1">
        Client roster <span class="font-normal text-gray-500 dark:text-gray-400">— who's reaching this agent</span>
      </h4>
      <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">
        External users who have messaged this agent through a channel (Telegram, WhatsApp). Read-only.
      </p>

      <div v-if="clients.length === 0" class="text-center py-6 text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-900/50 rounded-lg border border-dashed border-gray-300 dark:border-gray-700">
        <p class="text-sm">No external clients yet</p>
        <p class="text-xs mt-1">Users who message via Telegram or WhatsApp will appear here.</p>
      </div>

      <div v-else class="overflow-x-auto border border-gray-200 dark:border-gray-700 rounded-lg">
        <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700 text-sm">
          <thead class="bg-gray-50 dark:bg-gray-900/50">
            <tr class="text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
              <th class="px-4 py-2">Client</th>
              <th class="px-4 py-2">Channel</th>
              <th class="px-4 py-2">Verified email</th>
              <th class="px-4 py-2 text-right">Messages</th>
              <th class="px-4 py-2">Last active</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-200 dark:divide-gray-700">
            <tr v-for="client in clients" :key="`${client.channel}:${client.identity}`">
              <td class="px-4 py-2 text-gray-900 dark:text-gray-100">
                <span class="font-medium">{{ client.display_name || client.identity }}</span>
                <span v-if="client.display_name" class="block text-xs text-gray-500 dark:text-gray-400">{{ client.identity }}</span>
              </td>
              <td class="px-4 py-2">
                <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200 capitalize">{{ client.channel }}</span>
              </td>
              <td class="px-4 py-2 text-gray-600 dark:text-gray-300">
                {{ client.verified_email || '—' }}
              </td>
              <td class="px-4 py-2 text-right tabular-nums text-gray-700 dark:text-gray-200">{{ client.message_count }}</td>
              <td class="px-4 py-2 text-gray-500 dark:text-gray-400">{{ formatLastActive(client.last_active) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Distribution: content/links sharing — not client access (#18 nudge) -->
    <div>
      <h4 class="text-sm font-medium text-gray-900 dark:text-gray-100 mb-1">Distribution</h4>
      <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">
        Share generated files and public chat links. This is about distributing output, not granting client access.
      </p>
      <div class="space-y-2">
        <ChannelDisclosure title="Public links" subtitle="Shareable public chat URLs" icon="🔗">
          <PublicLinksPanel :agent-name="agentName" />
        </ChannelDisclosure>

        <ChannelDisclosure title="File sharing" subtitle="Outbound shared files" icon="📂">
          <FileSharingPanel :agent-name="agentName" />
        </ChannelDisclosure>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import { useAgentsStore } from '../stores/agents'
import { useNotification } from '../composables'
import { useSessionsStore } from '../stores/sessions'
import { useEnterpriseStore } from '../stores/enterprise'
import ChannelDisclosure from './ChannelDisclosure.vue'
import ChannelConfigRow from './ChannelConfigRow.vue'
import PublicLinksPanel from './PublicLinksPanel.vue'
import SlackChannelPanel from './SlackChannelPanel.vue'
import TelegramChannelPanel from './TelegramChannelPanel.vue'
import WhatsAppChannelPanel from './WhatsAppChannelPanel.vue'
import VoipChannelPanel from './VoipChannelPanel.vue'
import ConnectorChannelPanel from './ConnectorChannelPanel.vue'
import FileSharingPanel from './FileSharingPanel.vue'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  },
  shares: {
    type: Array,
    default: () => []
  }
})

const emit = defineEmits(['agent-updated'])

const { showNotification } = useNotification()

// VoIP channel visibility (#28) — gated on the platform `voip_available` flag.
const sessionsStore = useSessionsStore()
const agentsStore = useAgentsStore()
sessionsStore.loadFeatureFlags()

// MCP connector visibility (ent#46) — gated on the `mcp_connector` entitlement.
const enterpriseStore = useEnterpriseStore()
enterpriseStore.loadFeatureFlags()

const loadAgent = () => {
  emit('agent-updated')
}

// ---------------------------------------------------------------------------
// Channel summary-row status derivations (#19). Each maps a channel's GET
// response to { connected, label, warn } for the compact row; the full config
// panel renders in the row's Configure dialog.
// ---------------------------------------------------------------------------
const slackStatus = (d) => ({
  connected: !!d?.bound,
  label: d?.bound ? `#${d.channel_name}` : '',
})
const telegramStatus = (d) => ({
  connected: !!d?.configured,
  label: d?.configured && d.bot_username ? `@${d.bot_username}` : '',
  warn: !!d?.configured && !d?.webhook_url,
})
const whatsappStatus = (d) => ({
  connected: !!d?.configured,
  label: d?.configured ? (d.from_number || '') : '',
})
const voipStatus = (d) => ({
  connected: !!d?.configured,
  label: d?.configured ? `${d.from_number || ''}${d.enabled ? '' : ' (disabled)'}` : '',
  warn: !!d?.configured && !d?.enabled,
})

// ---------------------------------------------------------------------------
// External access policy + pending requests (Issue #311, reframed by #18)
// ---------------------------------------------------------------------------
const authStore = useAuthStore()
const policy = ref({ require_email: false, open_access: false })
const policyLoading = ref(false)
const pendingRequests = ref([])
const decisionLoading = ref(null)

// The two backend flags collapse into one Restricted ↔ Open control:
//   Restricted → require_email: true, open_access: false  (approval-gated)
//   Open       → require_email: true, open_access: true   (anyone verified)
// Identity proof (require_email) is always on for external sharing — a legacy
// agent with require_email=false is shown by its open_access flag and upgraded
// to identity-required the next time the operator picks a mode.
const accessMode = computed(() => (policy.value.open_access ? 'open' : 'restricted'))

// ---------------------------------------------------------------------------
// Additional instructions for public & channel chats (#1205)
// ---------------------------------------------------------------------------
const PUBLIC_PROMPT_MAX = 4000
const publicPrompt = ref('')
const publicPromptSaved = ref('')   // last persisted value, for dirty tracking
const publicPromptLoading = ref(false)
const publicPromptDirty = computed(
  () => (publicPrompt.value || '') !== (publicPromptSaved.value || '')
)

const loadPublicPrompt = async () => {
  try {
    const value = await agentsStore.fetchPublicChannelPrompt(props.agentName)
    publicPrompt.value = value || ''
    publicPromptSaved.value = value || ''
  } catch (err) {
    console.error('Failed to load public instructions:', err)
  }
}

const savePublicPrompt = async () => {
  publicPromptLoading.value = true
  try {
    const value = await agentsStore.savePublicChannelPrompt(
      props.agentName,
      publicPrompt.value
    )
    publicPrompt.value = value || ''
    publicPromptSaved.value = value || ''
    showNotification('Additional instructions saved', 'success')
  } catch (err) {
    console.error('Failed to save public instructions:', err)
    showNotification(err.response?.data?.detail || 'Failed to save instructions', 'error')
  } finally {
    publicPromptLoading.value = false
  }
}

const clearPublicPrompt = async () => {
  publicPrompt.value = ''
  await savePublicPrompt()
}

// ---------------------------------------------------------------------------
// External client roster (#20)
// ---------------------------------------------------------------------------
const clients = ref([])

const loadClients = async () => {
  try {
    const { data } = await axios.get(
      `/api/agents/${props.agentName}/clients`,
      { headers: authStore.authHeader }
    )
    clients.value = data
  } catch (err) {
    console.error('Failed to load client roster:', err)
    clients.value = []
  }
}

const formatLastActive = (iso) => {
  if (!iso) return 'never'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

const loadPolicy = async () => {
  try {
    const { data } = await axios.get(
      `/api/agents/${props.agentName}/access-policy`,
      { headers: authStore.authHeader }
    )
    policy.value = data
  } catch (err) {
    console.error('Failed to load access policy:', err)
  }
}

const setAccessMode = async (mode) => {
  const next = { require_email: true, open_access: mode === 'open' }
  if (next.require_email === policy.value.require_email && next.open_access === policy.value.open_access) {
    return
  }
  policyLoading.value = true
  try {
    const { data } = await axios.put(
      `/api/agents/${props.agentName}/access-policy`,
      next,
      { headers: authStore.authHeader }
    )
    policy.value = data
    showNotification('Access policy updated', 'success')
  } catch (err) {
    console.error('Failed to update access policy:', err)
    showNotification(err.response?.data?.detail || 'Failed to update policy', 'error')
  } finally {
    policyLoading.value = false
  }
}

const loadAccessRequests = async () => {
  try {
    const { data } = await axios.get(
      `/api/agents/${props.agentName}/access-requests`,
      { headers: authStore.authHeader, params: { status: 'pending' } }
    )
    pendingRequests.value = data
  } catch (err) {
    console.error('Failed to load access requests:', err)
  }
}

const decideRequest = async (req, approve) => {
  decisionLoading.value = req.id
  try {
    await axios.post(
      `/api/agents/${props.agentName}/access-requests/${req.id}/decide`,
      { approve },
      { headers: authStore.authHeader }
    )
    showNotification(
      approve ? `Approved ${req.email}` : `Denied ${req.email}`,
      'success'
    )
    await loadAccessRequests()
    if (approve) await loadAgent()
  } catch (err) {
    console.error('Failed to decide request:', err)
    showNotification(err.response?.data?.detail || 'Failed to update request', 'error')
  } finally {
    decisionLoading.value = null
  }
}

const formatRequestedAt = (iso) => {
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

// ---------------------------------------------------------------------------
// Public-channel model override (#894)
// ---------------------------------------------------------------------------
const publicChannelModel = ref('')      // '' = inherit platform default
const pcmAvailable = ref([])
const pcmDefault = ref('')
const pcmSaving = ref(false)

const loadPublicChannelModel = async () => {
  try {
    const { data } = await axios.get(
      `/api/agents/${props.agentName}/public-channel-model`,
      { headers: authStore.authHeader }
    )
    publicChannelModel.value = data.public_channel_model || ''
    pcmAvailable.value = data.available_models || []
    pcmDefault.value = data.platform_default || ''
  } catch (err) {
    console.error('Failed to load public-channel model:', err)
  }
}

const setPublicChannelModel = async (value) => {
  pcmSaving.value = true
  try {
    const { data } = await axios.put(
      `/api/agents/${props.agentName}/public-channel-model`,
      { model: value || null },
      { headers: authStore.authHeader }
    )
    publicChannelModel.value = data.public_channel_model || ''
    showNotification(
      data.is_overridden ? `Public chat model set to ${data.public_channel_model}` : 'Public chat model reset to platform default',
      'success'
    )
  } catch (err) {
    console.error('Failed to update public-channel model:', err)
    showNotification(err.response?.data?.detail?.message || err.response?.data?.detail || 'Failed to update model', 'error')
    await loadPublicChannelModel()  // re-sync the select to the persisted value
  } finally {
    pcmSaving.value = false
  }
}

watch(() => props.agentName, async (name) => {
  if (!name) return
  await Promise.all([loadPolicy(), loadAccessRequests(), loadPublicChannelModel(), loadClients(), loadPublicPrompt()])
}, { immediate: true })
</script>
