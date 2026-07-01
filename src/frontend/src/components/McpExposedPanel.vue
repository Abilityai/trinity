<template>
  <div>
    <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Expose via MCP</h3>
    <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
      Publish this agent as a dedicated MCP tool. When enabled, connected MCP
      clients get a first-class
      <code class="font-mono text-xs bg-gray-100 dark:bg-gray-800 px-1 py-0.5 rounded">{{ status.tool_name || 'chat_with_…' }}</code>
      tool — functionally identical to
      <code class="font-mono text-xs bg-gray-100 dark:bg-gray-800 px-1 py-0.5 rounded">chat_with_agent</code>
      with this agent pre-filled. Access is unchanged: callers still need
      ownership or a share to actually chat.
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
        <div class="w-11 h-6 bg-gray-200 dark:bg-gray-700 peer-focus:outline-none peer-focus:ring-2 peer-focus:ring-action-primary-500 rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-0.5 after:left-0.5 after:bg-white after:border after:border-gray-300 after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-action-primary-600"></div>
      </label>
      <div class="flex-1">
        <div class="text-sm font-medium text-gray-900 dark:text-gray-100">
          {{ status.enabled ? 'Exposed' : 'Not exposed' }}
        </div>
        <div class="text-xs text-gray-500 dark:text-gray-400">
          No agent restart needed — the change appears in connected MCP clients
          within a few seconds (the MCP server polls and refreshes their tool list).
        </div>
      </div>
    </div>

    <!-- Tool name -->
    <div
      v-if="status.tool_name"
      class="mb-2 text-sm text-gray-600 dark:text-gray-400"
    >
      Tool name:
      <code class="font-mono text-xs bg-gray-100 dark:bg-gray-800 px-1 py-0.5 rounded">{{ status.tool_name }}</code>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useAgentsStore } from '../stores/agents'

const props = defineProps({
  agentName: { type: String, required: true },
  // Notification host passed down from SettingsPanel — positional (message, type),
  // same contract as GuardrailsPanel. null when rendered standalone.
  notify: { type: Function, default: null },
})

const agentsStore = useAgentsStore()
function notifyUser(message, type = 'success') {
  if (props.notify) props.notify(message, type)
}

const status = ref({ enabled: false, tool_name: '' })
const statusLoading = ref(false)
const toggleLoading = ref(false)

async function loadStatus() {
  statusLoading.value = true
  try {
    status.value = await agentsStore.getMcpExposedStatus(props.agentName)
  } catch (e) {
    notifyUser(`Failed to load MCP exposure status: ${e.message}`, 'error')
  } finally {
    statusLoading.value = false
  }
}

async function onToggle(enabled) {
  toggleLoading.value = true
  try {
    const resp = await agentsStore.setMcpExposed(props.agentName, enabled)
    status.value = {
      ...status.value,
      enabled: resp.enabled,
      tool_name: resp.tool_name,
    }
    notifyUser(
      enabled
        ? `Exposed as MCP tool "${resp.tool_name}" — appears in clients shortly.`
        : 'No longer exposed via MCP.',
      'success'
    )
  } catch (e) {
    notifyUser(
      e.response?.data?.detail || `Failed to toggle MCP exposure: ${e.message}`,
      'error'
    )
    // Reload to reflect actual state
    await loadStatus()
  } finally {
    toggleLoading.value = false
  }
}

onMounted(loadStatus)
</script>
