<template>
  <div class="space-y-6">
    <!-- Loading State -->
    <div v-if="loading" class="flex items-center justify-center py-8">
      <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-500"></div>
    </div>

    <!-- Not Owner State -->
    <div v-else-if="!canShare" class="text-center py-8">
      <div class="mx-auto w-16 h-16 bg-gray-100 rounded-full flex items-center justify-center mb-4">
        <svg class="w-8 h-8 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
        </svg>
      </div>
      <h3 class="text-lg font-medium text-gray-900">Owner Access Required</h3>
      <p class="mt-2 text-sm text-gray-500">
        Only the agent owner can manage shared folders.
      </p>
    </div>

    <!-- Main Content -->
    <div v-else>
      <!-- Restart Required Banner -->
      <div v-if="foldersData?.restart_required" class="mb-4 p-4 bg-amber-50 border border-amber-200 rounded-lg">
        <div class="flex items-start">
          <svg class="w-5 h-5 text-amber-500 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
          </svg>
          <div class="ml-3">
            <h3 class="text-sm font-medium text-amber-800">Restart Required</h3>
            <p class="mt-1 text-sm text-amber-700">
              Configuration has changed. Restart the agent to apply shared folder mounts.
            </p>
          </div>
        </div>
      </div>

      <!-- Configuration Section -->
      <div class="bg-white rounded-lg border border-gray-200 p-6">
        <h3 class="text-lg font-medium text-gray-900 mb-4">Shared Folder Configuration</h3>

        <div class="space-y-6">
          <!-- Expose Toggle -->
          <div class="flex items-start justify-between">
            <div class="flex-1">
              <h4 class="text-sm font-medium text-gray-900">Expose Shared Folder</h4>
              <p class="text-sm text-gray-500 mt-1">
                Make files in <code class="bg-gray-100 px-1 py-0.5 rounded text-xs">/home/developer/shared-out</code>
                available to other permitted agents.
              </p>
            </div>
            <button
              @click="toggleExpose"
              :disabled="saving"
              :class="[
                'relative inline-flex h-6 w-11 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2',
                foldersData?.expose_enabled ? 'bg-indigo-600' : 'bg-gray-200'
              ]"
            >
              <span
                :class="[
                  'pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out',
                  foldersData?.expose_enabled ? 'translate-x-5' : 'translate-x-0'
                ]"
              />
            </button>
          </div>

          <!-- Consume Toggle -->
          <div class="flex items-start justify-between">
            <div class="flex-1">
              <h4 class="text-sm font-medium text-gray-900">Mount Shared Folders</h4>
              <p class="text-sm text-gray-500 mt-1">
                Mount shared folders from other permitted agents at
                <code class="bg-gray-100 px-1 py-0.5 rounded text-xs">/home/developer/shared-in/{agent}</code>
              </p>
            </div>
            <button
              @click="toggleConsume"
              :disabled="saving"
              :class="[
                'relative inline-flex h-6 w-11 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2',
                foldersData?.consume_enabled ? 'bg-indigo-600' : 'bg-gray-200'
              ]"
            >
              <span
                :class="[
                  'pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out',
                  foldersData?.consume_enabled ? 'translate-x-5' : 'translate-x-0'
                ]"
              />
            </button>
          </div>
        </div>
      </div>

      <!-- Exposed Folder Info (if exposing) -->
      <div v-if="foldersData?.expose_enabled" class="bg-white rounded-lg border border-gray-200 p-6">
        <h3 class="text-lg font-medium text-gray-900 mb-4">Exposed Folder</h3>

        <div class="space-y-4">
          <div class="flex items-center space-x-2 text-sm">
            <span class="text-gray-500">Volume:</span>
            <code class="bg-gray-100 px-2 py-1 rounded text-xs font-mono">{{ foldersData?.exposed_volume }}</code>
          </div>
          <div class="flex items-center space-x-2 text-sm">
            <span class="text-gray-500">Path:</span>
            <code class="bg-gray-100 px-2 py-1 rounded text-xs font-mono">{{ foldersData?.exposed_path }}</code>
          </div>

          <!-- Consumers List -->
          <div v-if="consumers.length > 0" class="mt-4">
            <h4 class="text-sm font-medium text-gray-700 mb-2">Consumers</h4>
            <p class="text-xs text-gray-500 mb-2">These agents can mount this folder:</p>
            <div class="space-y-2">
              <div
                v-for="consumer in consumers"
                :key="consumer.agent_name"
                class="flex items-center justify-between p-2 bg-gray-50 rounded text-sm"
              >
                <div class="flex items-center space-x-2">
                  <span :class="[
                    'w-2 h-2 rounded-full',
                    consumer.status === 'running' ? 'bg-green-500' : 'bg-gray-400'
                  ]"></span>
                  <span class="font-medium">{{ consumer.agent_name }}</span>
                </div>
                <code class="text-xs text-gray-500">{{ consumer.mount_path }}</code>
              </div>
            </div>
          </div>
          <div v-else class="text-sm text-gray-500 italic">
            No agents are configured to consume this folder yet.
          </div>
        </div>
      </div>

      <!-- Consumed Folders (if consuming) -->
      <div v-if="foldersData?.consume_enabled" class="bg-white rounded-lg border border-gray-200 p-6">
        <h3 class="text-lg font-medium text-gray-900 mb-4">Mounted Folders</h3>

        <div v-if="foldersData?.consumed_folders?.length > 0" class="space-y-3">
          <div
            v-for="folder in foldersData.consumed_folders"
            :key="folder.source_agent"
            class="flex items-center justify-between p-3 border border-gray-200 rounded-lg"
          >
            <div class="flex items-center space-x-3">
              <span :class="[
                'w-3 h-3 rounded-full',
                folder.currently_mounted ? 'bg-green-500' : 'bg-amber-500'
              ]" :title="folder.currently_mounted ? 'Mounted' : 'Pending restart'"></span>
              <div>
                <div class="font-medium text-gray-900">{{ folder.source_agent }}</div>
                <code class="text-xs text-gray-500">{{ folder.mount_path }}</code>
              </div>
            </div>
            <span :class="[
              'px-2 py-1 text-xs rounded-full',
              folder.currently_mounted ? 'bg-green-100 text-green-700' : 'bg-amber-100 text-amber-700'
            ]">
              {{ folder.currently_mounted ? 'Mounted' : 'Pending' }}
            </span>
          </div>
        </div>

        <!-- Available Folders -->
        <div v-if="availableFolders.length > 0" class="mt-6">
          <h4 class="text-sm font-medium text-gray-700 mb-2">Available Folders</h4>
          <p class="text-xs text-gray-500 mb-3">
            Folders from agents you have permission to access. These will be mounted on restart.
          </p>
          <div class="grid grid-cols-2 gap-2">
            <div
              v-for="folder in availableFolders"
              :key="folder.source_agent"
              class="flex items-center space-x-2 p-2 bg-gray-50 rounded text-sm"
            >
              <span :class="[
                'w-2 h-2 rounded-full',
                folder.source_status === 'running' ? 'bg-green-500' : 'bg-gray-400'
              ]"></span>
              <span>{{ folder.source_agent }}</span>
            </div>
          </div>
        </div>

        <div v-else-if="foldersData?.consumed_folders?.length === 0" class="text-sm text-gray-500 italic">
          No shared folders available. Grant permissions to other agents that expose folders.
        </div>
      </div>

      <!-- How It Works -->
      <div class="bg-gray-50 rounded-lg p-4 border border-gray-200">
        <h4 class="text-sm font-medium text-gray-700 mb-2">How Shared Folders Work</h4>
        <ul class="text-xs text-gray-600 space-y-1">
          <li class="flex items-start">
            <span class="text-indigo-500 mr-2">1.</span>
            Enable "Expose" on Agent A to create a shared volume at <code class="bg-gray-200 px-1 rounded">/home/developer/shared-out</code>
          </li>
          <li class="flex items-start">
            <span class="text-indigo-500 mr-2">2.</span>
            Grant Agent B permission to access Agent A (via Permissions tab)
          </li>
          <li class="flex items-start">
            <span class="text-indigo-500 mr-2">3.</span>
            Enable "Mount" on Agent B to mount Agent A's folder at <code class="bg-gray-200 px-1 rounded">/home/developer/shared-in/agent-a</code>
          </li>
          <li class="flex items-start">
            <span class="text-indigo-500 mr-2">4.</span>
            Restart both agents to apply the volume mounts
          </li>
        </ul>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, watch } from 'vue'
import { useAgentsStore } from '../stores/agents'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  },
  agentStatus: {
    type: String,
    default: 'stopped'
  },
  canShare: {
    type: Boolean,
    default: false
  }
})

const agentsStore = useAgentsStore()
const loading = ref(true)
const saving = ref(false)
const foldersData = ref(null)
const consumers = ref([])
const availableFolders = ref([])

async function loadFoldersConfig() {
  loading.value = true
  try {
    foldersData.value = await agentsStore.getAgentFolders(props.agentName)

    // Load consumers if exposing
    if (foldersData.value?.expose_enabled) {
      consumers.value = await agentsStore.getFolderConsumers(props.agentName)
    }

    // Load available folders if consuming
    if (foldersData.value?.consume_enabled) {
      availableFolders.value = await agentsStore.getAvailableFolders(props.agentName)
    }
  } catch (error) {
    console.error('Failed to load folders config:', error)
  } finally {
    loading.value = false
  }
}

async function toggleExpose() {
  saving.value = true
  try {
    const newValue = !foldersData.value?.expose_enabled
    await agentsStore.updateAgentFolders(props.agentName, { expose_enabled: newValue })
    await loadFoldersConfig()
  } catch (error) {
    console.error('Failed to toggle expose:', error)
  } finally {
    saving.value = false
  }
}

async function toggleConsume() {
  saving.value = true
  try {
    const newValue = !foldersData.value?.consume_enabled
    await agentsStore.updateAgentFolders(props.agentName, { consume_enabled: newValue })
    await loadFoldersConfig()
  } catch (error) {
    console.error('Failed to toggle consume:', error)
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  if (props.canShare) {
    loadFoldersConfig()
  } else {
    loading.value = false
  }
})

watch(() => props.agentName, () => {
  if (props.canShare) {
    loadFoldersConfig()
  }
})
</script>
