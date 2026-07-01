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

    <!-- Channels: compact summary rows (detailed config moves to dialogs in #19) -->
    <div>
      <h4 class="text-sm font-medium text-gray-900 dark:text-gray-100 mb-1">Channels</h4>
      <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">
        Connect this agent to messaging channels. Expand a row to configure it.
      </p>
      <div class="space-y-2">
        <ChannelDisclosure title="Slack" subtitle="@mentions in a Slack channel" icon="💬">
          <SlackChannelPanel :agent-name="agentName" />
        </ChannelDisclosure>

        <ChannelDisclosure title="Telegram" subtitle="DMs and group chats via a bot" icon="✈️">
          <TelegramChannelPanel :agent-name="agentName" />
        </ChannelDisclosure>

        <ChannelDisclosure title="WhatsApp" subtitle="DMs via Twilio" icon="📱">
          <WhatsAppChannelPanel :agent-name="agentName" />
        </ChannelDisclosure>

        <ChannelDisclosure
          v-if="sessionsStore.voipAvailable"
          title="Voice calls"
          subtitle="Outbound phone calls via Twilio + Gemini Live"
          icon="📞"
        >
          <VoipChannelPanel :agent-name="agentName" />
        </ChannelDisclosure>

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
import { ref, computed, watch } from 'vue'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import { useNotification } from '../composables'
import { useSessionsStore } from '../stores/sessions'
import { useEnterpriseStore } from '../stores/enterprise'
import ChannelDisclosure from './ChannelDisclosure.vue'
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
sessionsStore.loadFeatureFlags()

// MCP connector visibility (ent#46) — gated on the `mcp_connector` entitlement.
const enterpriseStore = useEnterpriseStore()
enterpriseStore.loadFeatureFlags()

const loadAgent = () => {
  emit('agent-updated')
}

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
  await Promise.all([loadPolicy(), loadAccessRequests(), loadPublicChannelModel()])
}, { immediate: true })
</script>
