<template>
  <div class="p-6 space-y-8">
    <!-- Framing: identity vs. authorization (Issue #446) -->
    <div class="rounded-lg bg-indigo-50 dark:bg-indigo-900/20 border border-indigo-100 dark:border-indigo-900/40 p-4 text-sm text-indigo-900 dark:text-indigo-200">
      Access to this agent has two layers:
      <ul class="mt-2 list-disc pl-5 space-y-1">
        <li><strong>Identity proof</strong> — "Require verified email" (below) forces every user to prove who they are via email verification before chatting. It is enforced on web, Slack, and Telegram.</li>
        <li><strong>Authorization</strong> — "Team Sharing" (below) is the allow-list of emails who skip the owner-approval queue. Everyone else lands in Pending Access Requests until you approve them.</li>
      </ul>
    </div>

    <!-- Channel Access Policy (Issue #311) -->
    <div>
      <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Identity Proof</h3>
      <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
        Require users to prove who they are before chatting, across web, Telegram, and Slack.
        Identity proof alone does <em>not</em> grant access — see Team Sharing below.
      </p>

      <div class="space-y-3">
        <label class="flex items-start gap-3">
          <input
            type="checkbox"
            class="mt-1"
            :checked="policy.require_email"
            :disabled="policyLoading"
            @change="updatePolicy({ require_email: $event.target.checked })"
          />
          <div>
            <div class="text-sm font-medium text-gray-900 dark:text-gray-100">Require verified email</div>
            <div class="text-xs text-gray-500 dark:text-gray-400">
              Telegram users must <code>/login</code>; Slack uses workspace email; web requires email verification.
              This only proves who the user is — it does not authorize them to chat. Use Team Sharing or Open access below for authorization.
            </div>
          </div>
        </label>

        <label class="flex items-start gap-3">
          <input
            type="checkbox"
            class="mt-1"
            :checked="policy.open_access"
            :disabled="policyLoading"
            @change="updatePolicy({ open_access: $event.target.checked })"
          />
          <div>
            <div class="text-sm font-medium text-gray-900 dark:text-gray-100">Open access (anyone verified)</div>
            <div class="text-xs text-gray-500 dark:text-gray-400">
              Anyone with a verified email may chat without owner approval.
              When off, only users in Team Sharing skip the pending-approval queue.
            </div>
          </div>
        </label>
      </div>

      <!-- Dead-end configuration warning (#446) -->
      <div
        v-if="policy.require_email && !policy.open_access && (!shares || shares.length === 0)"
        class="mt-4 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800/40 p-3 text-sm text-amber-900 dark:text-amber-200"
      >
        <strong>Heads up:</strong> you've required verified email but haven't shared with anyone or enabled Open access.
        Every verified user will land in Pending Access Requests and stay locked out until you approve them one by one.
        Add emails to Team Sharing below, or enable Open access, to let people chat.
      </div>

      <!-- Pending access requests -->
      <div v-if="pendingRequests.length > 0" class="mt-6">
        <h4 class="text-sm font-medium text-gray-900 dark:text-gray-100 mb-2">
          Pending access requests ({{ pendingRequests.length }})
        </h4>
        <p class="text-xs text-gray-500 dark:text-gray-400 mb-2">
          Users who verified their identity but aren't in Team Sharing. Approving moves them into Team Sharing.
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

    <div class="border-t border-gray-200 dark:border-gray-700"></div>

    <!-- Team Sharing Section -->
    <div>
      <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Team Sharing <span class="text-sm font-normal text-gray-500 dark:text-gray-400">— allow-list</span></h3>
      <p class="text-sm text-gray-500 dark:text-gray-400 mb-2">
        Pre-authorize team members by email. They still verify their identity on first chat, but they skip the owner-approval queue.
      </p>
      <p class="text-xs text-gray-500 dark:text-gray-400 mb-4">
        Applies uniformly across web, Slack, and Telegram. Entries are stored lowercase and matched case-insensitively.
      </p>

      <!-- Share Form -->
      <form @submit.prevent="shareWithUser" class="flex items-center space-x-3">
        <input
          v-model="shareEmail"
          type="email"
          placeholder="user@example.com"
          :disabled="shareLoading"
          class="flex-1 max-w-md px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:bg-gray-100 dark:disabled:bg-gray-900"
        />
        <button
          type="submit"
          :disabled="shareLoading || !shareEmail.trim()"
          class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 dark:focus:ring-offset-gray-800 disabled:bg-gray-400 dark:disabled:bg-gray-600 disabled:cursor-not-allowed"
        >
          <svg v-if="shareLoading" class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
          </svg>
          {{ shareLoading ? 'Sharing...' : 'Share' }}
        </button>
      </form>

      <!-- Share Error/Success Message -->
      <div v-if="shareMessage" :class="[
        'mt-3 p-3 rounded-lg text-sm',
        shareMessage.type === 'success' ? 'bg-status-success-50 dark:bg-status-success-900/30 text-status-success-700 dark:text-status-success-300' : 'bg-status-danger-50 dark:bg-status-danger-900/30 text-status-danger-700 dark:text-status-danger-300'
      ]">
        {{ shareMessage.text }}
      </div>

      <!-- Shared Users List -->
      <div class="mt-4">
        <div v-if="!shares || shares.length === 0" class="text-center py-6 text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-900/50 rounded-lg border border-dashed border-gray-300 dark:border-gray-700">
          <svg class="mx-auto h-10 w-10 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
          </svg>
          <p class="mt-2 text-sm">Not shared with anyone</p>
        </div>

        <ul v-else class="divide-y divide-gray-200 dark:divide-gray-700 border border-gray-200 dark:border-gray-700 rounded-lg">
          <li v-for="share in shares" :key="share.id" class="px-4 py-3 flex items-center justify-between">
            <div class="flex items-center space-x-3">
              <div class="flex-shrink-0 h-8 w-8 bg-gray-200 dark:bg-gray-700 rounded-full flex items-center justify-center">
                <span class="text-sm font-medium text-gray-600 dark:text-gray-300">
                  {{ (share.shared_with_name || share.shared_with_email || '?')[0].toUpperCase() }}
                </span>
              </div>
              <div>
                <p class="text-sm font-medium text-gray-900 dark:text-gray-100">
                  {{ share.shared_with_name || share.shared_with_email }}
                </p>
                <p v-if="share.shared_with_name" class="text-xs text-gray-500 dark:text-gray-400">
                  {{ share.shared_with_email }}
                </p>
              </div>
            </div>
            <div class="flex items-center gap-4">
              <!-- Proactive messaging toggle -->
              <label class="flex items-center gap-2 cursor-pointer" :title="share.allow_proactive ? 'Agent can send proactive messages' : 'Agent cannot send proactive messages'">
                <span class="text-xs text-gray-500 dark:text-gray-400">Proactive</span>
                <button
                  type="button"
                  role="switch"
                  :aria-checked="share.allow_proactive"
                  @click="toggleProactive(share)"
                  :disabled="proactiveLoading === share.shared_with_email"
                  :class="[
                    share.allow_proactive ? 'bg-indigo-600' : 'bg-gray-200 dark:bg-gray-600',
                    'relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2 disabled:opacity-50 disabled:cursor-not-allowed'
                  ]"
                >
                  <span
                    :class="[
                      share.allow_proactive ? 'translate-x-4' : 'translate-x-0',
                      'pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out'
                    ]"
                  />
                </button>
              </label>
              <button
                @click="removeShare(share.shared_with_email)"
                :disabled="unshareLoading === share.shared_with_email"
                class="text-status-danger-600 dark:text-status-danger-400 hover:text-status-danger-800 dark:hover:text-status-danger-300 text-sm font-medium disabled:opacity-50"
              >
                <span v-if="unshareLoading === share.shared_with_email">Removing...</span>
                <span v-else>Remove</span>
              </button>
            </div>
          </li>
        </ul>
      </div>
    </div>

    <!-- Divider -->
    <div class="border-t border-gray-200 dark:border-gray-700"></div>

    <!-- Slack Channel Section -->
    <SlackChannelPanel :agent-name="agentName" />

    <!-- Divider -->
    <div class="border-t border-gray-200 dark:border-gray-700"></div>

    <!-- Telegram Bot Section -->
    <TelegramChannelPanel :agent-name="agentName" />

    <!-- Divider -->
    <div class="border-t border-gray-200 dark:border-gray-700"></div>

    <!-- WhatsApp (Twilio) Section -->
    <WhatsAppChannelPanel :agent-name="agentName" />

    <!-- Divider -->
    <div class="border-t border-gray-200 dark:border-gray-700"></div>

    <!-- File Sharing Section (FILES-001) -->
    <FileSharingPanel :agent-name="agentName" />

    <!-- Divider -->
    <div class="border-t border-gray-200 dark:border-gray-700"></div>

    <!-- Public Links Section -->
    <PublicLinksPanel :agent-name="agentName" />
  </div>
</template>

<script setup>
import { ref, watch, onMounted } from 'vue'
import axios from 'axios'
import { useAgentsStore } from '../stores/agents'
import { useAuthStore } from '../stores/auth'
import { useAgentSharing } from '../composables/useAgentSharing'
import { useNotification } from '../composables'
import PublicLinksPanel from './PublicLinksPanel.vue'
import SlackChannelPanel from './SlackChannelPanel.vue'
import TelegramChannelPanel from './TelegramChannelPanel.vue'
import WhatsAppChannelPanel from './WhatsAppChannelPanel.vue'
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

const agentsStore = useAgentsStore()
const { showNotification } = useNotification()

// Create agent ref for composable
const agent = ref({ name: props.agentName, shares: props.shares })

// Update agent ref when props change
watch(() => [props.agentName, props.shares], () => {
  agent.value = { name: props.agentName, shares: props.shares }
}, { deep: true })

// Reload agent function for composable
const loadAgent = () => {
  emit('agent-updated')
}

const {
  shareEmail,
  shareLoading,
  shareMessage,
  unshareLoading,
  shareWithUser,
  removeShare
} = useAgentSharing(agent, agentsStore, loadAgent, showNotification)

// Proactive messaging toggle
const proactiveLoading = ref(null)

const toggleProactive = async (share) => {
  proactiveLoading.value = share.shared_with_email
  try {
    await axios.put(
      `/api/agents/${props.agentName}/shares/proactive`,
      { email: share.shared_with_email, allow_proactive: !share.allow_proactive },
      { headers: authStore.authHeader }
    )
    share.allow_proactive = !share.allow_proactive
    showNotification(
      share.allow_proactive ? 'Proactive messaging enabled' : 'Proactive messaging disabled',
      'success'
    )
  } catch (err) {
    console.error('Failed to update proactive setting:', err)
    showNotification(err.response?.data?.detail || 'Failed to update setting', 'error')
  } finally {
    proactiveLoading.value = null
  }
}

// ---------------------------------------------------------------------------
// Access policy + access requests (Issue #311)
// ---------------------------------------------------------------------------
const authStore = useAuthStore()
const policy = ref({ require_email: false, open_access: false })
const policyLoading = ref(false)
const pendingRequests = ref([])
const decisionLoading = ref(null)

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

const updatePolicy = async (changes) => {
  policyLoading.value = true
  try {
    const next = { ...policy.value, ...changes }
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

watch(() => props.agentName, async (name) => {
  if (!name) return
  await Promise.all([loadPolicy(), loadAccessRequests()])
}, { immediate: true })
</script>
