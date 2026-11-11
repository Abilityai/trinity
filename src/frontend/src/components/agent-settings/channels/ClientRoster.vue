<template>
  <SettingsSection title="Who has talked to it">
    <SettingRow label="Client roster" hint="External people who have messaged this agent through a channel. Approve, revoke or block one from the roster." testid="channels-roster">
      <template #value>
        <template v-if="failed">
          <span class="text-xs text-status-danger-700 dark:text-status-danger-400">Couldn't load the roster</span>
          <BaseButton size="sm" variant="ghost" @click="load">Retry</BaseButton>
        </template>
        <span v-else-if="loaded" class="text-xs text-gray-600 dark:text-gray-300" data-testid="channels-roster-summary">{{ summary }}</span>
      </template>
      <template #action>
        <BaseButton v-if="clients.length" size="sm" variant="secondary" data-testid="channels-roster-open" @click="open = true">Open roster</BaseButton>
      </template>
    </SettingRow>
    <ChannelConfigDialog v-if="open" title="Client roster" @close="open = false">
      <!-- trinity-enterprise#21: what each control does, said once above the table. -->
      <p class="mb-3 text-xs text-gray-600 dark:text-gray-300" data-testid="channels-roster-controls-note">
        <strong>Revoke</strong> removes an approved client's access; <strong>Block</strong> stops a client
        reaching this agent at all, whatever the access policy, with no reply. Their history is kept.
      </p>
      <!-- #1925: a bounded wide table. Horizontal scroll INSIDE the container is the
           right treatment for unbounded column width (design-system principle 7). -->
      <div class="overflow-x-auto border border-gray-200 dark:border-gray-700 rounded-lg">
        <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700 text-sm">
          <thead class="bg-gray-50 dark:bg-gray-900/50">
            <tr class="text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
              <th class="px-4 py-2">Client</th>
              <th class="px-4 py-2">Channel</th>
              <th class="px-4 py-2">Verified email</th>
              <th
                class="px-4 py-2 text-right"
                title="Direct messages received from this client. Counting starts when this feature is deployed."
              >Messages</th>
              <th class="px-4 py-2">Last active</th>
              <th class="px-4 py-2">Access</th>
              <th class="px-4 py-2"><span class="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-200 dark:divide-gray-700">
            <tr v-for="client in clients" :key="`${client.channel}:${client.client_id || client.identity}`">
              <td class="px-4 py-2 text-gray-900 dark:text-gray-100">
                <span class="font-medium">{{ client.display_name || client.identity }}</span>
                <span v-if="client.display_name" class="block text-xs text-gray-500 dark:text-gray-400">{{ client.identity }}</span>
              </td>
              <td class="px-4 py-2">
                <BaseBadge size="sm" variant="neutral">{{ client.channel }}</BaseBadge>
              </td>
              <td class="px-4 py-2 text-gray-600 dark:text-gray-300">{{ client.verified_email || '—' }}</td>
              <td class="px-4 py-2 text-right tabular-nums text-gray-700 dark:text-gray-200">{{ client.message_count }}</td>
              <td class="px-4 py-2 text-gray-500 dark:text-gray-400">{{ formatLastActive(client.last_active) }}</td>
              <td class="px-4 py-2 whitespace-nowrap">
                <BaseBadge
                  size="sm"
                  :variant="ACCESS_BADGE[client.access]?.variant || 'neutral'"
                  :title="blockedTitle(client)"
                  :data-testid="`client-access-${client.channel}-${client.client_id}`"
                >{{ ACCESS_BADGE[client.access]?.label || 'By policy' }}</BaseBadge>
              </td>
              <td class="px-4 py-2">
                <div class="flex items-center justify-end gap-2 whitespace-nowrap">
                  <BaseButton
                    v-if="client.access === 'pending' && client.access_request_id"
                    size="sm"
                    :loading="busy === clientKey(client)"
                    @click="approveClient(client)"
                  >Approve</BaseButton>
                  <BaseButton
                    v-if="client.access === 'granted'"
                    size="sm"
                    variant="secondary"
                    :disabled="busy === clientKey(client)"
                    @click="askControl(client, 'revoke')"
                  >Revoke</BaseButton>
                  <BaseButton
                    v-if="client.access === 'blocked'"
                    size="sm"
                    variant="secondary"
                    :loading="busy === clientKey(client)"
                    @click="runControl(client, 'unblock')"
                  >Unblock</BaseButton>
                  <BaseButton
                    v-else
                    size="sm"
                    variant="danger"
                    :disabled="busy === clientKey(client)"
                    @click="askControl(client, 'block')"
                  >Block</BaseButton>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </ChannelConfigDialog>
    <ConfirmDialog
      :visible="!!confirm"
      :title="confirm?.title"
      :message="confirm?.message || ''"
      :confirm-text="confirm?.confirmText"
      variant="warning"
      @confirm="confirmControl"
      @cancel="confirm = null"
    />
  </SettingsSection>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import api from '../../../api'
import { useNotification } from '../../../composables/useNotification'
import { rosterSummary } from '../../../utils/channelsPane'
import BaseBadge from '../../base/BaseBadge.vue'
import BaseButton from '../../base/BaseButton.vue'
import ChannelConfigDialog from '../../ChannelConfigDialog.vue'
import ConfirmDialog from '../../ConfirmDialog.vue'
import SettingRow from '../SettingRow.vue'
import SettingsSection from '../SettingsSection.vue'

const props = defineProps({
  agentName: { type: String, required: true },
  // Bumped by the pane when a request was decided next door (Who can chat),
  // so an approval there shows on the roster without a reload.
  refreshKey: { type: Number, default: 0 },
})
// `agent-updated`: a share changed (approve, revoke). `requests-changed`: a
// pending request was decided here, so Who can chat re-reads its list.
const emit = defineEmits(['agent-updated', 'requests-changed'])

const { showNotification } = useNotification()
const clients = ref([])
const loaded = ref(false)
const failed = ref(false)
const open = ref(false)
const summary = computed(() => rosterSummary(clients.value))

// One read at a time counts: a slow answer for the agent we left must not land
// on the agent we are on now.
let ticket = 0
const load = async () => {
  const mine = ++ticket
  failed.value = false
  try {
    const { data } = await api.get(`/api/agents/${props.agentName}/clients`)
    if (mine !== ticket) return
    clients.value = data
    loaded.value = true
  } catch (err) {
    if (mine !== ticket) return
    // Keep what's on screen: a failed read isn't "no clients" (ent#253).
    console.error('Failed to load client roster:', err)
    failed.value = true
  }
}

// Per-client controls (trinity-enterprise#21). The row's `access` comes from the
// roster; approve is the existing access-request decision.
const ACCESS_BADGE = {
  blocked: { label: 'Blocked', variant: 'danger' },
  granted: { label: 'Approved', variant: 'success' },
  pending: { label: 'Pending', variant: 'warning' },
  none: { label: 'By policy', variant: 'neutral' },
}
const busy = ref(null)
const confirm = ref(null)
const clientKey = (c) => `${c.channel}:${c.client_id}`
const clientName = (c) => c.display_name || c.identity

const blockedTitle = (c) => {
  if (c.access !== 'blocked') return undefined
  const by = c.blocked_by_email ? ` by ${c.blocked_by_email}` : ''
  const why = c.block_reason ? ` — “${c.block_reason}”` : ''
  return `Blocked ${formatLastActive(c.blocked_at)}${by}${why}`
}

const CONFIRM = {
  block: (c) => ({
    title: `Block ${clientName(c)}?`,
    message: `Their ${c.channel} messages to this agent will be dropped with no reply, whatever the access policy. Their history and any approval are kept; unblocking restores them.`,
    confirmText: 'Block',
  }),
  revoke: (c) => ({
    title: `Revoke ${clientName(c)}'s access?`,
    message: `${c.verified_email} loses access to this agent. While access is Restricted, their next message asks for approval again; while it is Open, they can still chat — block them to stop that.`,
    confirmText: 'Revoke',
  }),
}

const askControl = (client, action) => {
  confirm.value = { client, action, ...CONFIRM[action](client) }
}

const confirmControl = async () => {
  const { client, action } = confirm.value || {}
  confirm.value = null
  if (client) await runControl(client, action)
}

const DONE = { block: 'Blocked', unblock: 'Unblocked', revoke: 'Revoked access for' }

const runControl = async (client, action) => {
  busy.value = clientKey(client)
  try {
    await api.post(`/api/agents/${props.agentName}/clients/${action}`,
      { channel: client.channel, client_id: client.client_id })
    showNotification(`${DONE[action]} ${clientName(client)}`, 'success')
    await load()
    if (action === 'revoke') emit('agent-updated')
  } catch (err) {
    console.error(`Failed to ${action} client:`, err)
    showNotification(err.response?.data?.detail || `Failed to ${action} client`, 'error')
  } finally {
    busy.value = null
  }
}

const approveClient = async (client) => {
  busy.value = clientKey(client)
  try {
    await api.post(`/api/agents/${props.agentName}/access-requests/${client.access_request_id}/decide`,
      { approve: true })
    showNotification(`Approved ${client.verified_email || clientName(client)}`, 'success')
    await load()
    emit('requests-changed')
    emit('agent-updated')
  } catch (err) {
    console.error('Failed to approve client:', err)
    showNotification(err.response?.data?.detail || 'Failed to update request', 'error')
  } finally {
    busy.value = null
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

// A different agent: nothing of the previous one stays on screen or clickable
// while the new roster loads (#4310 review).
watch(() => props.agentName, (name) => {
  clients.value = []
  loaded.value = false
  failed.value = false
  open.value = false
  confirm.value = null
  busy.value = null
  if (name) load()
}, { immediate: true })
watch(() => props.refreshKey, () => { if (props.agentName) load() })
</script>
