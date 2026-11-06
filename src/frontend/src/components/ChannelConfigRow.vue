<template>
  <!-- One channel in Settings › Channels (trinity-enterprise#340): a SettingRow
       with its state on the right, and its config in a dialog. -->
  <SettingRow :label="title" :hint="hint" :testid="`channel-row-${channelId}`">
    <template #value>
      <span v-if="loading" class="text-xs text-gray-500 dark:text-gray-400">Checking…</span>
      <template v-else-if="failed">
        <BaseBadge size="sm" variant="warning">Couldn't check</BaseBadge>
        <BaseButton size="sm" variant="ghost" @click="fetchStatus">Retry</BaseButton>
      </template>
      <span v-else-if="!badge" class="text-xs text-gray-600 dark:text-gray-300">{{ status.label }}</span>
      <BaseBadge v-else-if="status.connected" size="sm" :variant="status.warn ? 'warning' : 'success'">
        {{ status.warn ? 'Needs attention' : 'Connected' }}
      </BaseBadge>
      <BaseBadge v-else size="sm" variant="neutral">Not connected</BaseBadge>
    </template>
    <template #action>
      <BaseButton size="sm" variant="secondary" @click="dialogOpen = true">
        {{ status.connected ? 'Manage' : 'Configure' }}
      </BaseButton>
    </template>
    <template #hint>
      <p v-if="!loading && !failed && badge && status.connected && status.label" class="mt-0.5 text-[12.5px] text-gray-600 dark:text-gray-400 truncate">{{ status.label }}</p>
    </template>
  </SettingRow>
  <!-- Mounted only while open, so a closed panel never loads or lays out. -->
  <ChannelConfigDialog v-if="dialogOpen" :title="title" :icon="icon" @close="onDialogClose">
    <slot />
  </ChannelConfigDialog>
</template>

<script setup>
import { computed, ref, reactive, watch } from 'vue'
import api from '../api'
import BaseBadge from './base/BaseBadge.vue'
import BaseButton from './base/BaseButton.vue'
import ChannelConfigDialog from './ChannelConfigDialog.vue'
import SettingRow from './agent-settings/SettingRow.vue'

const props = defineProps({
  title: { type: String, required: true },
  icon: { type: String, default: '' },
  hint: { type: String, default: '' },
  agentName: { type: String, required: true },
  // Channel status endpoint (e.g. `/api/agents/{name}/slack/channel`).
  statusUrl: { type: String, required: true },
  // (responseData) => ({ connected: boolean, label?: string, warn?: boolean })
  deriveStatus: { type: Function, required: true },
  // Show Connected / Not connected. Off for a row whose state is a count.
  badge: { type: Boolean, default: true },
  // Open the dialog from outside: `&channel=slack` (trinity-enterprise#340).
  open: { type: Boolean, default: false },
})
const emit = defineEmits(['status', 'closed'])

const channelId = computed(() => props.title.toLowerCase().replace(/[^a-z]+/g, '-'))
const loading = ref(true)
const failed = ref(false)
const dialogOpen = ref(false)
const status = reactive({ connected: false, label: '', warn: false })

async function fetchStatus() {
  loading.value = true
  failed.value = false
  try {
    const { data } = await api.get(props.statusUrl)
    const s = props.deriveStatus(data) || {}
    status.connected = !!s.connected
    status.label = s.label || ''
    status.warn = !!s.warn
  } catch (e) {
    status.connected = false
    status.label = ''
    status.warn = false
    // A 4xx is an answer: not configured, not permitted, feature off. A 5xx or
    // no response isn't, so it must not read as "Not connected" (ent#340).
    const code = e?.response?.status
    failed.value = !code || code >= 500
  } finally {
    loading.value = false
    emit('status', { connected: failed.value ? null : status.connected })
  }
}

// Refetch after the dialog closes so the row reflects edits made inside it.
function onDialogClose() {
  dialogOpen.value = false
  emit('closed')
  fetchStatus()
}

watch(() => props.open, (open) => { if (open) dialogOpen.value = true }, { immediate: true })
watch(() => [props.agentName, props.statusUrl], fetchStatus, { immediate: true })
</script>
