<template>
  <SettingsSection title="Who can chat">
    <SettingRow label="Access policy" :hint="policyHint" testid="channels-policy">
      <template #action>
        <span v-if="policyFailed" class="text-xs text-status-danger-700 dark:text-status-danger-400">Couldn't load the policy</span>
        <BaseButton v-if="policyFailed" size="sm" variant="ghost" @click="loadPolicy">Retry</BaseButton>
        <div
          v-else
          class="inline-flex rounded-lg border border-gray-300 dark:border-gray-600 p-1 bg-gray-100 dark:bg-gray-800"
          role="group"
          aria-label="External access policy"
        >
          <button
            v-for="m in MODES"
            :key="m.id"
            type="button"
            :disabled="policyLoading || !policyLoaded"
            :aria-pressed="policyLoaded && accessMode === m.id"
            :class="['px-3 py-1 rounded-md text-sm font-medium transition-colors disabled:opacity-50',
              policyLoaded && accessMode === m.id
                ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-white shadow-sm'
                : 'text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-white']"
            @click="setAccessMode(m.id)"
          >{{ m.label }}</button>
        </div>
      </template>
    </SettingRow>

    <!-- Dead-end heads-up: Restricted with nobody approved yet (#446) -->
    <div
      v-if="policyLoaded && accessMode === 'restricted' && !shares.length"
      class="px-4 py-3 text-sm text-state-autonomous-800 dark:text-state-autonomous-200 bg-state-autonomous-50 dark:bg-state-autonomous-900/20"
      data-testid="channels-restricted-heads-up"
    >
      <strong>Heads up:</strong> access is Restricted and no one's approved yet, so every verified client waits in
      Pending requests until you approve them. Approve requests, or switch to <strong>Open</strong>.
    </div>

    <SettingRow label="Pending requests" hint="Outsiders who verified their identity and are asking to be let in." testid="channels-pending">
      <template #value>
        <template v-if="requestsFailed">
          <span class="text-xs text-status-danger-700 dark:text-status-danger-400">Couldn't load requests</span>
          <BaseButton size="sm" variant="ghost" @click="loadAccessRequests">Retry</BaseButton>
        </template>
        <span v-else-if="!requestsLoaded" class="text-xs text-gray-500 dark:text-gray-400">Checking…</span>
        <BaseBadge v-else-if="pendingRequests.length" size="sm" variant="warning">{{ pendingRequests.length }} waiting</BaseBadge>
        <span v-else class="text-xs text-gray-500 dark:text-gray-400">No one waiting</span>
      </template>
      <template #action>
        <BaseButton v-if="pendingRequests.length" size="sm" variant="secondary" data-testid="channels-review" @click="reviewOpen = true">Review</BaseButton>
      </template>
    </SettingRow>

    <ChannelConfigDialog v-if="reviewOpen" title="Pending requests" @close="reviewOpen = false">
      <p v-if="!pendingRequests.length" class="text-sm text-gray-600 dark:text-gray-300">Nobody's waiting now.</p>
      <ul v-else class="divide-y divide-gray-200 dark:divide-gray-700 border border-gray-200 dark:border-gray-700 rounded-lg">
        <!-- trinity-enterprise#737: the requester as a person — name, handle,
             channel and its own id — with the email as one attribute. -->
        <li v-for="req in pendingRequests" :key="req.id" class="px-4 py-3 flex items-center justify-between gap-3" :data-testid="`access-request-${req.id}`">
          <div class="min-w-0">
            <p class="text-sm font-medium text-gray-900 dark:text-gray-100" data-testid="access-request-who">{{ requesterTitle(req) }}</p>
            <p class="text-xs text-gray-500 dark:text-gray-400" data-testid="access-request-facts">
              {{ requesterFacts(req) }} · {{ formatRequestedAt(req.requested_at) }}
            </p>
            <div v-if="!req.identity_captured || req.known_as?.length" class="mt-1 flex flex-wrap gap-1">
              <BaseBadge
                v-if="!req.identity_captured"
                size="sm"
                variant="neutral"
                :title="req.channel === 'web' ? 'A public-link request carries a verified email and nothing else.' : 'This request predates identity capture.'"
                data-testid="access-request-no-identity"
              >{{ req.channel === 'web' ? 'Email only — public link' : 'Identity not captured' }}</BaseBadge>
              <BaseBadge
                v-for="k in (req.known_as || []).slice(0, 3)"
                :key="`${k.agent_name}:${k.status}:${k.email}`"
                size="sm"
                :variant="KNOWN_AS_VARIANT[k.status] || 'neutral'"
                :title="k.email ? `as ${k.email}` : undefined"
                data-testid="access-request-known-as"
              >{{ KNOWN_AS_LABEL[k.status] || k.status }} on {{ k.agent_name }}</BaseBadge>
              <BaseBadge
                v-if="(req.known_as || []).length > 3"
                size="sm"
                variant="neutral"
                :title="req.known_as.slice(3).map(k => `${KNOWN_AS_LABEL[k.status] || k.status} on ${k.agent_name}`).join(', ')"
              >+{{ req.known_as.length - 3 }} more</BaseBadge>
            </div>
          </div>
          <div class="flex items-center gap-2">
            <BaseButton size="sm" :disabled="decisionLoading === req.id" @click="decideRequest(req, true)">Approve</BaseButton>
            <BaseButton size="sm" variant="secondary" :disabled="decisionLoading === req.id" @click="decideRequest(req, false)">Deny</BaseButton>
          </div>
        </li>
      </ul>
    </ChannelConfigDialog>
  </SettingsSection>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import api from '../../../api'
import { useNotification } from '../../../composables'
import BaseBadge from '../../base/BaseBadge.vue'
import BaseButton from '../../base/BaseButton.vue'
import ChannelConfigDialog from '../../ChannelConfigDialog.vue'
import SettingRow from '../SettingRow.vue'
import SettingsSection from '../SettingsSection.vue'

const props = defineProps({
  agentName: { type: String, required: true },
  shares: { type: Array, default: () => [] },
  // Bumped by the pane when the roster approved a request (trinity-enterprise#21),
  // so the pending list here follows without a reload.
  refreshKey: { type: Number, default: 0 },
})
// `requests-changed`: a pending request was decided here, so the roster re-reads.
const emit = defineEmits(['agent-updated', 'summary', 'requests-changed'])

const MODES = [{ id: 'restricted', label: 'Restricted' }, { id: 'open', label: 'Open' }]

const { showNotification } = useNotification()

const policy = ref({ require_email: false, open_access: false })
const policyLoaded = ref(false)
const policyFailed = ref(false)
const policyLoading = ref(false)
const pendingRequests = ref([])
const requestsLoaded = ref(false)
const requestsFailed = ref(false)
const decisionLoading = ref(null)
const reviewOpen = ref(false)

// The two backend flags collapse into one Restricted ↔ Open control:
//   Restricted → require_email: true, open_access: false  (approval-gated)
//   Open       → require_email: true, open_access: true   (anyone verified)
// Identity proof (require_email) is always on for external sharing — a legacy
// agent with require_email=false is shown by its open_access flag and upgraded
// to identity-required the next time the operator picks a mode.
const accessMode = computed(() => (policy.value.open_access ? 'open' : 'restricted'))

const policyHint = computed(() => (accessMode.value === 'open'
  ? 'Open: anyone with a verified email can chat without approval. Telegram users verify with /login, Slack uses the workspace email, and the web asks for a code.'
  : 'Restricted: only people you approve can chat. Everyone else waits in Pending requests. Telegram users verify with /login, Slack uses the workspace email, and the web asks for a code.'))

const report = () => emit('summary', {
  mode: policyLoaded.value ? accessMode.value : null,
  pending: requestsLoaded.value ? pendingRequests.value.length : null,
})

const loadPolicy = async () => {
  policyFailed.value = false
  const name = props.agentName   // a late answer for the agent we left is dropped
  try {
    const { data } = await api.get(`/api/agents/${name}/access-policy`)
    if (name !== props.agentName) return
    policy.value = data
    policyLoaded.value = true
  } catch (err) {
    if (name !== props.agentName) return
    console.error('Failed to load access policy:', err)
    policyLoaded.value = false
    policyFailed.value = true
  }
  report()
}

const setAccessMode = async (mode) => {
  const next = { require_email: true, open_access: mode === 'open' }
  if (next.require_email === policy.value.require_email && next.open_access === policy.value.open_access) return
  policyLoading.value = true
  try {
    const { data } = await api.put(`/api/agents/${props.agentName}/access-policy`, next)
    policy.value = data
    showNotification('Access policy updated', 'success')
  } catch (err) {
    console.error('Failed to update access policy:', err)
    showNotification(err.response?.data?.detail || 'Failed to update policy', 'error')
  } finally {
    policyLoading.value = false
  }
  report()
}

const loadAccessRequests = async () => {
  requestsFailed.value = false
  const name = props.agentName   // a late answer for the agent we left is dropped
  try {
    const { data } = await api.get(`/api/agents/${name}/access-requests`, { params: { status: 'pending' } })
    if (name !== props.agentName) return
    pendingRequests.value = data
    requestsLoaded.value = true
  } catch (err) {
    if (name !== props.agentName) return
    console.error('Failed to load access requests:', err)
    requestsLoaded.value = false
    requestsFailed.value = true
  }
  report()
}

const decideRequest = async (req, approve) => {
  decisionLoading.value = req.id
  try {
    await api.post(`/api/agents/${props.agentName}/access-requests/${req.id}/decide`, { approve })
    showNotification(approve ? `Approved ${req.email}` : `Denied ${req.email}`, 'success')
    await loadAccessRequests()
    emit('requests-changed')
    if (approve) emit('agent-updated')
  } catch (err) {
    console.error('Failed to decide request:', err)
    showNotification(err.response?.data?.detail || 'Failed to update request', 'error')
  } finally {
    decisionLoading.value = null
  }
}

// trinity-enterprise#737 — who asked, as the channel saw them.
const CHANNEL_LABEL = { telegram: 'Telegram', whatsapp: 'WhatsApp', slack: 'Slack', web: 'Public link' }
const KNOWN_AS_LABEL = { approved: 'Approved', denied: 'Denied', pending: 'Asked', verified: 'Verified' }
const KNOWN_AS_VARIANT = { approved: 'success', denied: 'danger', pending: 'warning', verified: 'info' }
const handleOf = (req) => (req.requester_handle ? `@${req.requester_handle.replace(/^@/, '')}` : '')
const requesterTitle = (req) => req.requester_name || handleOf(req) || req.email
const requesterFacts = (req) => {
  const channel = CHANNEL_LABEL[req.channel] || req.channel || 'unknown channel'
  const parts = []
  if (req.requester_name && handleOf(req)) parts.push(handleOf(req))
  parts.push(req.requester_channel_id ? `${channel} id ${req.requester_channel_id}` : `via ${channel}`)
  if (requesterTitle(req) !== req.email) parts.push(req.email)
  return parts.join(' · ')
}
const formatRequestedAt = (iso) => {
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

// A different agent: the previous one's policy and requests must not stay on
// screen or clickable while the new ones load — `setAccessMode`'s early return
// would otherwise compare against the old agent's policy (#4310 review).
watch(() => props.agentName, (name) => {
  policy.value = { require_email: false, open_access: false }
  policyLoaded.value = false
  policyFailed.value = false
  pendingRequests.value = []
  requestsLoaded.value = false
  requestsFailed.value = false
  reviewOpen.value = false
  report()
  if (!name) return
  loadPolicy()
  loadAccessRequests()
}, { immediate: true })
watch(() => props.refreshKey, () => { if (props.agentName) loadAccessRequests() })
</script>
