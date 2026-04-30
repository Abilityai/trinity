<template>
  <div class="p-6">
    <div class="mb-6">
      <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Agent Collaboration Permissions</h3>
      <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
        Control which other agents this agent can communicate with via the Trinity MCP tools.
      </p>

      <!-- Loading State -->
      <div v-if="permissionsLoading" class="text-center py-4">
        <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-500 mx-auto"></div>
        <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">Loading permissions...</p>
      </div>

      <!-- Permissions List -->
      <div v-else-if="availableAgents.length > 0">
        <!-- Bulk Actions -->
        <div class="flex items-center space-x-3 mb-4">
          <button
            @click="allowAllAgents"
            :disabled="permissionsSaving"
            class="text-sm text-indigo-600 dark:text-indigo-400 hover:text-indigo-800 dark:hover:text-indigo-300 font-medium disabled:opacity-50"
          >
            Allow All
          </button>
          <span class="text-gray-300 dark:text-gray-600">|</span>
          <button
            @click="allowNoAgents"
            :disabled="permissionsSaving"
            class="text-sm text-status-danger-600 dark:text-status-danger-400 hover:text-status-danger-800 dark:hover:text-status-danger-300 font-medium disabled:opacity-50"
          >
            Allow None
          </button>
          <span v-if="permissionsDirty" class="text-state-autonomous-600 dark:text-state-autonomous-400 text-xs ml-4">
            Unsaved changes
          </span>
        </div>

        <!-- Agent Checkboxes -->
        <ul class="divide-y divide-gray-200 dark:divide-gray-700 border border-gray-200 dark:border-gray-700 rounded-lg mb-4">
          <li v-for="targetAgent in availableAgents" :key="targetAgent.name" class="px-4 py-3 flex items-center justify-between hover:bg-gray-50 dark:hover:bg-gray-700">
            <label :for="'perm-' + targetAgent.name" class="flex items-center space-x-3 cursor-pointer flex-1">
              <input
                :id="'perm-' + targetAgent.name"
                type="checkbox"
                v-model="targetAgent.permitted"
                @change="markDirty"
                :disabled="permissionsSaving"
                class="h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-gray-300 dark:border-gray-600 rounded dark:bg-gray-700"
              />
              <div class="flex-1">
                <span class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ targetAgent.name }}</span>
                <span v-if="targetAgent.type" class="ml-2 text-xs text-gray-500 dark:text-gray-400">[{{ targetAgent.type }}]</span>
              </div>
              <span :class="[
                'px-2 py-0.5 text-xs font-medium rounded-full',
                targetAgent.status === 'running' ? 'bg-status-success-100 dark:bg-status-success-900/50 text-status-success-800 dark:text-status-success-300' : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400'
              ]">
                {{ targetAgent.status }}
              </span>
            </label>
          </li>
        </ul>

        <!-- Save Button -->
        <div class="flex items-center space-x-3">
          <button
            @click="savePermissions"
            :disabled="permissionsSaving || !permissionsDirty"
            class="inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md shadow-sm text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 dark:focus:ring-offset-gray-800 disabled:bg-gray-400 dark:disabled:bg-gray-600 disabled:cursor-not-allowed"
          >
            <svg v-if="permissionsSaving" class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
              <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
              <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            {{ permissionsSaving ? 'Saving...' : 'Save Permissions' }}
          </button>

          <!-- Status Message -->
          <div v-if="permissionsMessage" :class="[
            'text-sm',
            permissionsMessage.type === 'success' ? 'text-status-success-600 dark:text-status-success-400' : 'text-status-danger-600 dark:text-status-danger-400'
          ]">
            {{ permissionsMessage.text }}
          </div>
        </div>
      </div>

      <!-- No Other Agents -->
      <div v-else class="text-center py-8 text-gray-500 dark:text-gray-400">
        <svg class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
        </svg>
        <p class="mt-2">No other agents available</p>
        <p class="text-xs">Create more agents to enable collaboration permissions.</p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, watch } from 'vue'
import { useAgentsStore } from '../stores/agents'
import { useAgentPermissions } from '../composables/useAgentPermissions'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  },
  agentStatus: {
    type: String,
    default: 'stopped'
  }
})

const agentsStore = useAgentsStore()

// Create agent ref for composable
const agent = computed(() => ({
  name: props.agentName,
  status: props.agentStatus
}))

const {
  availableAgents,
  permissionsLoading,
  permissionsSaving,
  permissionsDirty,
  permissionsMessage,
  loadPermissions,
  savePermissions,
  allowAllAgents,
  allowNoAgents,
  markDirty
} = useAgentPermissions(agent, agentsStore)

// Load permissions on mount
onMounted(() => {
  loadPermissions()
})

// Reload when agent changes
watch(() => props.agentName, () => {
  loadPermissions()
})
</script>
