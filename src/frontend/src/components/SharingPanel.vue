<template>
  <div class="p-6 space-y-8">
    <!-- Framing: identity vs. authorization (Issue #446) -->
    <div class="rounded-lg bg-action-primary-50 dark:bg-action-primary-900/20 border border-action-primary-100 dark:border-action-primary-900/40 p-4 text-sm text-action-primary-900 dark:text-action-primary-200">
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
        Identity proof alone does <em>not</em> grant access — manage operators on the <strong>Access</strong> tab.
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
        class="mt-4 rounded-lg bg-state-autonomous-50 dark:bg-state-autonomous-900/20 border border-state-autonomous-200 dark:border-state-autonomous-800/40 p-3 text-sm text-state-autonomous-900 dark:text-state-autonomous-200"
      >
        <strong>Heads up:</strong> you've required verified email but haven't shared with anyone or enabled Open access.
        Every verified user will land in Pending Access Requests and stay locked out until you approve them one by one.
        Add operators on the <strong>Access</strong> tab, or enable Open access, to let people chat.
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
