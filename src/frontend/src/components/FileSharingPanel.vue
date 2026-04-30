<template>
  <div>
    <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">File Sharing</h3>
    <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
      Let the agent publish files from its
      <code class="font-mono text-xs bg-gray-100 dark:bg-gray-800 px-1 py-0.5 rounded">/home/developer/public/</code>
      directory via time-limited download URLs. Agents call the
      <code class="font-mono text-xs bg-gray-100 dark:bg-gray-800 px-1 py-0.5 rounded">share_file</code>
      MCP tool once this toggle is on.
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
        <div class="w-11 h-6 bg-gray-200 dark:bg-gray-700 peer-focus:outline-none peer-focus:ring-2 peer-focus:ring-indigo-500 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-0.5 after:left-0.5 after:bg-white after:border after:border-gray-300 after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-indigo-600"></div>
      </label>
      <div class="flex-1">
        <div class="text-sm font-medium text-gray-900 dark:text-gray-100">
          {{ status.enabled ? 'Enabled' : 'Disabled' }}
        </div>
        <div class="text-xs text-gray-500 dark:text-gray-400">
          Flipping this toggle requires restarting the agent before it takes effect.
        </div>
      </div>
    </div>

    <!-- Restart-required banner -->
    <div
      v-if="status.restart_required"
      class="mb-4 rounded-md bg-amber-50 dark:bg-amber-900/30 border border-amber-200 dark:border-amber-800 px-4 py-3 text-sm text-amber-800 dark:text-amber-200"
    >
      Configuration changed — restart the agent to mount or detach
      <code class="font-mono text-xs">/home/developer/public/</code>.
    </div>

    <!-- Quota + file count -->
    <div v-if="status.enabled" class="mb-4 text-sm text-gray-600 dark:text-gray-400">
      <span class="font-medium text-gray-900 dark:text-gray-100">{{ files.length }}</span>
      active file{{ files.length === 1 ? '' : 's' }} ·
      <span class="font-medium text-gray-900 dark:text-gray-100">{{ formatBytes(totalBytes) }}</span>
      of <span>{{ formatBytes(quotaBytes) }}</span>
      used
    </div>

    <!-- Files table -->
    <div
      v-if="status.enabled && files.length > 0"
      class="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700"
    >
      <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
        <thead class="bg-gray-50 dark:bg-gray-800">
          <tr>
            <th class="px-4 py-2 text-left text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Filename</th>
            <th class="px-4 py-2 text-left text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Size</th>
            <th class="px-4 py-2 text-left text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Expires</th>
            <th class="px-4 py-2 text-left text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Downloads</th>
            <th class="px-4 py-2 text-right text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Actions</th>
          </tr>
        </thead>
        <tbody class="bg-white dark:bg-gray-900 divide-y divide-gray-200 dark:divide-gray-800">
          <tr v-for="file in files" :key="file.file_id">
            <td class="px-4 py-2 text-sm text-gray-900 dark:text-gray-100 truncate max-w-[20ch]" :title="file.filename">
              {{ file.filename }}
            </td>
            <td class="px-4 py-2 text-sm text-gray-600 dark:text-gray-400">{{ formatBytes(file.size_bytes) }}</td>
            <td class="px-4 py-2 text-sm text-gray-600 dark:text-gray-400" :title="file.expires_at">
              {{ relativeTime(file.expires_at) }}
            </td>
            <td class="px-4 py-2 text-sm text-gray-600 dark:text-gray-400">{{ file.download_count }}</td>
            <td class="px-4 py-2 text-right whitespace-nowrap">
              <button
                @click="copyUrl(file)"
                class="px-2 py-1 text-xs font-medium rounded-md text-indigo-700 dark:text-indigo-300 bg-indigo-50 dark:bg-indigo-900/40 hover:bg-indigo-100 dark:hover:bg-indigo-900/70 mr-2"
                :title="file.url"
              >
                {{ copiedId === file.file_id ? 'Copied!' : 'Copy URL' }}
              </button>
              <button
                @click="revoke(file)"
                :disabled="revokingId === file.file_id"
                class="px-2 py-1 text-xs font-medium rounded-md text-status-danger-700 dark:text-status-danger-300 bg-status-danger-50 dark:bg-status-danger-900/40 hover:bg-status-danger-100 dark:hover:bg-status-danger-900/70 disabled:opacity-50"
              >
                {{ revokingId === file.file_id ? 'Revoking…' : 'Revoke' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Empty state -->
    <div
      v-else-if="status.enabled"
      class="text-sm text-gray-500 dark:text-gray-400 italic"
    >
      No active shares yet. The agent will publish files via the
      <code class="font-mono text-xs">share_file</code> MCP tool.
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { useNotification } from '../composables'

const props = defineProps({
  agentName: { type: String, required: true },
})

const agentsStore = useAgentsStore()
const { showNotification } = useNotification()

const status = ref({ enabled: false, restart_required: false, volume_attached: false })
const files = ref([])
const totalBytes = ref(0)
const quotaBytes = ref(500 * 1024 * 1024)

const statusLoading = ref(false)
const toggleLoading = ref(false)
const revokingId = ref(null)
const copiedId = ref(null)

async function loadStatus() {
  statusLoading.value = true
  try {
    status.value = await agentsStore.getFileSharingStatus(props.agentName)
    if (status.value.enabled) await loadFiles()
  } catch (e) {
    showNotification({ type: 'error', message: `Failed to load file-sharing status: ${e.message}` })
  } finally {
    statusLoading.value = false
  }
}

async function loadFiles() {
  try {
    const resp = await agentsStore.listSharedFiles(props.agentName)
    files.value = resp.files || []
    totalBytes.value = resp.total_bytes || 0
    quotaBytes.value = resp.quota_bytes || 500 * 1024 * 1024
  } catch (e) {
    showNotification({ type: 'error', message: `Failed to list shared files: ${e.message}` })
  }
}

async function onToggle(enabled) {
  toggleLoading.value = true
  try {
    const resp = await agentsStore.setFileSharingStatus(props.agentName, enabled)
    status.value = {
      ...status.value,
      enabled: resp.enabled,
      restart_required: resp.restart_required,
    }
    if (enabled) await loadFiles()
    else files.value = []
    showNotification({ type: 'success', message: resp.message })
  } catch (e) {
    showNotification({
      type: 'error',
      message: e.response?.data?.detail || `Failed to toggle file sharing: ${e.message}`,
    })
    // Reload status to reflect actual state
    await loadStatus()
  } finally {
    toggleLoading.value = false
  }
}

async function revoke(file) {
  if (!confirm(`Revoke the download link for "${file.filename}"? Existing URLs will stop working.`)) return
  revokingId.value = file.file_id
  try {
    await agentsStore.revokeSharedFile(props.agentName, file.file_id)
    await loadFiles()
    showNotification({ type: 'success', message: `Revoked ${file.filename}` })
  } catch (e) {
    showNotification({
      type: 'error',
      message: e.response?.data?.detail || `Revoke failed: ${e.message}`,
    })
  } finally {
    revokingId.value = null
  }
}

async function copyUrl(file) {
  try {
    await navigator.clipboard.writeText(file.url)
    copiedId.value = file.file_id
    setTimeout(() => {
      if (copiedId.value === file.file_id) copiedId.value = null
    }, 1500)
  } catch {
    showNotification({ type: 'error', message: 'Clipboard copy failed — use the title tooltip to copy manually.' })
  }
}

function formatBytes(n) {
  if (n == null) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`
}

function relativeTime(iso) {
  if (!iso) return '—'
  const then = new Date(iso).getTime()
  const now = Date.now()
  const diff = then - now
  if (diff <= 0) return 'expired'
  const days = diff / (1000 * 60 * 60 * 24)
  if (days >= 1) return `in ${Math.round(days)}d`
  const hours = diff / (1000 * 60 * 60)
  if (hours >= 1) return `in ${Math.round(hours)}h`
  const mins = Math.round(diff / (1000 * 60))
  return `in ${mins}m`
}

onMounted(loadStatus)
</script>
