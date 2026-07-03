<template>
  <div class="flex items-center gap-3 px-4 py-3 border border-gray-200 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-800">
    <!-- Channel glyph -->
    <span class="shrink-0 text-xl leading-none" aria-hidden="true">{{ icon }}</span>

    <!-- Title + status -->
    <div class="min-w-0 flex-1">
      <div class="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{{ title }}</div>
      <div class="mt-0.5 flex items-center gap-1.5 text-xs">
        <template v-if="loading">
          <span class="inline-block shrink-0 w-2 h-2 rounded-full bg-gray-300 dark:bg-gray-600 animate-pulse"></span>
          <span class="text-gray-400 dark:text-gray-500">Checking…</span>
        </template>
        <template v-else-if="status.connected">
          <span
            class="inline-block shrink-0 w-2 h-2 rounded-full"
            :class="status.warn ? 'bg-status-warning-500' : 'bg-status-success-500'"
          ></span>
          <span class="min-w-0 truncate text-gray-600 dark:text-gray-300">{{ status.label || 'Connected' }}</span>
          <span v-if="status.warn" class="shrink-0 text-status-warning-600 dark:text-status-warning-400">· setup needed</span>
        </template>
        <template v-else>
          <span class="inline-block shrink-0 w-2 h-2 rounded-full bg-gray-300 dark:bg-gray-600"></span>
          <span class="text-gray-400 dark:text-gray-500">Not connected</span>
        </template>
      </div>
    </div>

    <!-- Configure -->
    <button
      type="button"
      class="shrink-0 inline-flex items-center px-3 py-1.5 text-sm font-medium rounded-md border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 bg-white dark:bg-gray-800 hover:bg-gray-50 dark:hover:bg-gray-700 focus:outline-none focus:ring-1 focus:ring-action-primary-500"
      @click="dialogOpen = true"
    >
      {{ status.connected ? 'Manage' : 'Configure' }}
    </button>

    <!-- Config dialog: mounted only while open (single gate) so the closed
         dialog and its slotted panel never participate in this row's flex
         layout. The channel panel renders inside the modal, untouched. -->
    <ChannelConfigDialog v-if="dialogOpen" :title="title" :icon="icon" @close="onDialogClose">
      <slot />
    </ChannelConfigDialog>
  </div>
</template>

<script setup>
import { ref, reactive, watch } from 'vue'
import api from '../api'

const props = defineProps({
  title: { type: String, required: true },
  icon: { type: String, default: '🔗' },
  agentName: { type: String, required: true },
  // Channel status endpoint (e.g. `/api/agents/{name}/slack/channel`).
  statusUrl: { type: String, required: true },
  // (responseData) => ({ connected: boolean, label?: string, warn?: boolean })
  deriveStatus: { type: Function, required: true },
})

const loading = ref(true)
const dialogOpen = ref(false)
const status = reactive({ connected: false, label: '', warn: false })

async function fetchStatus() {
  loading.value = true
  try {
    const { data } = await api.get(props.statusUrl)
    const s = props.deriveStatus(data) || {}
    status.connected = !!s.connected
    status.label = s.label || ''
    status.warn = !!s.warn
  } catch {
    // Not configured / not permitted / feature off — show "Not connected".
    status.connected = false
    status.label = ''
    status.warn = false
  } finally {
    loading.value = false
  }
}

// Refetch after the dialog closes so the row reflects edits made inside it.
function onDialogClose() {
  dialogOpen.value = false
  fetchStatus()
}

watch(() => [props.agentName, props.statusUrl], fetchStatus, { immediate: true })
</script>
