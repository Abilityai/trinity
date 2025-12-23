<template>
  <div class="min-h-screen bg-gray-100 dark:bg-gray-900">
    <NavBar />

    <main class="max-w-7xl mx-auto py-6 sm:px-6 lg:px-8">
      <div class="px-4 py-6 sm:px-0">
        <!-- Notification Toast -->
        <div v-if="notification"
          :class="[
            'fixed top-20 right-4 z-50 px-4 py-3 rounded-lg shadow-lg transition-all duration-300',
            notification.type === 'success' ? 'bg-green-100 dark:bg-green-900/50 border border-green-400 dark:border-green-700 text-green-700 dark:text-green-300' : 'bg-red-100 dark:bg-red-900/50 border border-red-400 dark:border-red-700 text-red-700 dark:text-red-300'
          ]"
        >
          {{ notification.message }}
        </div>

        <div class="flex justify-between items-center mb-8">
          <h1 class="text-3xl font-bold text-gray-900 dark:text-white">Agents</h1>

          <div class="flex items-center space-x-4">
            <!-- Sort dropdown -->
            <select
              v-model="agentsStore.sortBy"
              class="block rounded-md border-gray-300 dark:border-gray-600 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 text-sm py-2 px-3 bg-white dark:bg-gray-700 dark:text-gray-200 border"
            >
              <option value="created_desc">Newest First</option>
              <option value="created_asc">Oldest First</option>
              <option value="name_asc">Name (A-Z)</option>
              <option value="name_desc">Name (Z-A)</option>
              <option value="status">Running First</option>
              <option value="context_desc">Context Usage</option>
            </select>

            <button
              @click="showCreateModal = true"
              class="bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-2 px-4 rounded"
            >
              Create Agent
            </button>
          </div>
        </div>

        <!-- Agents List -->
        <div class="bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 overflow-hidden sm:rounded-md">
          <ul class="divide-y divide-gray-200 dark:divide-gray-700">
            <li v-for="agent in agentsStore.sortedAgents" :key="agent.name">
              <div class="px-4 py-4 sm:px-6">
                <div class="flex items-center justify-between">
                  <router-link :to="`/agents/${agent.name}`" class="flex-1">
                    <div class="flex items-center">
                      <!-- Agent Icon with Activity Dot -->
                      <div class="flex-shrink-0 relative">
                        <ServerIcon class="h-10 w-10 text-gray-400 dark:text-gray-500" />
                        <!-- Activity state indicator dot -->
                        <div
                          :class="[
                            'absolute -bottom-0.5 -right-0.5 w-3 h-3 rounded-full border-2 border-white dark:border-gray-800',
                            getActivityDotClass(agent.name)
                          ]"
                        ></div>
                      </div>

                      <div class="ml-4 flex-1">
                        <div class="flex items-center space-x-2">
                          <p class="text-sm font-medium text-indigo-600 dark:text-indigo-400">{{ agent.name }}</p>
                          <span v-if="agent.is_shared" class="px-2 py-0.5 text-xs font-medium bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300 rounded-full">
                            Shared by {{ agent.owner }}
                          </span>
                          <!-- Activity state label -->
                          <span :class="getActivityLabelClass(agent.name)" class="text-xs font-medium">
                            {{ getActivityState(agent.name) }}
                          </span>
                        </div>

                        <p class="text-sm text-gray-500 dark:text-gray-400">Type: {{ agent.type }} | Port: {{ agent.port }}</p>

                        <!-- Context progress bar (only for running agents) -->
                        <div v-if="agent.status === 'running'" class="mt-2 max-w-xs">
                          <div class="flex items-center justify-between text-xs text-gray-500 dark:text-gray-400 mb-1">
                            <span>Context</span>
                            <span>{{ getContextPercent(agent.name) }}%</span>
                          </div>
                          <div class="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-1.5">
                            <div
                              :class="getProgressBarColor(agent.name)"
                              :style="{ width: getContextPercent(agent.name) + '%' }"
                              class="h-full rounded-full transition-all duration-500"
                            ></div>
                          </div>
                        </div>

                        <!-- Task progress (if has active plan) -->
                        <div v-if="hasActivePlan(agent.name)" class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                          <span class="inline-flex items-center">
                            <ClipboardDocumentCheckIcon class="h-3 w-3 mr-1 text-purple-500 dark:text-purple-400" />
                            {{ getTaskProgress(agent.name) }}
                            <span v-if="getCurrentTask(agent.name)" class="ml-2 truncate max-w-[150px] text-purple-600 dark:text-purple-400" :title="getCurrentTask(agent.name)">
                              • {{ getCurrentTask(agent.name) }}
                            </span>
                          </span>
                        </div>
                      </div>
                    </div>
                  </router-link>

                  <div class="flex items-center space-x-2">
                    <span :class="[
                      'px-2 inline-flex text-xs leading-5 font-semibold rounded-full',
                      agent.status === 'running' ? 'bg-green-100 dark:bg-green-900/50 text-green-800 dark:text-green-300' : 'bg-gray-100 dark:bg-gray-700 text-gray-800 dark:text-gray-300'
                    ]">
                      {{ agent.status }}
                    </span>
                    <button
                      v-if="agent.status === 'stopped'"
                      @click="startAgent(agent.name)"
                      :disabled="actionInProgress === agent.name"
                      class="text-green-600 dark:text-green-400 hover:text-green-900 dark:hover:text-green-300 disabled:opacity-50"
                    >
                      <svg v-if="actionInProgress === agent.name" class="animate-spin h-5 w-5" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      <PlayIcon v-else class="h-5 w-5" />
                    </button>
                    <button
                      v-else
                      @click="stopAgent(agent.name)"
                      :disabled="actionInProgress === agent.name"
                      class="text-red-600 dark:text-red-400 hover:text-red-900 dark:hover:text-red-300 disabled:opacity-50"
                    >
                      <svg v-if="actionInProgress === agent.name" class="animate-spin h-5 w-5" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      <StopIcon v-else class="h-5 w-5" />
                    </button>
                  </div>
                </div>
              </div>
            </li>
          </ul>

          <!-- Empty state -->
          <div v-if="agentsStore.sortedAgents.length === 0" class="text-center py-12">
            <ServerIcon class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" />
            <h3 class="mt-2 text-sm font-medium text-gray-900 dark:text-gray-100">No agents</h3>
            <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Get started by creating a new agent.</p>
            <div class="mt-6">
              <button
                @click="showCreateModal = true"
                class="inline-flex items-center px-4 py-2 border border-transparent shadow-sm text-sm font-medium rounded-md text-white bg-indigo-600 hover:bg-indigo-700"
              >
                Create Agent
              </button>
            </div>
          </div>
        </div>

        <!-- Create Agent Modal -->
        <CreateAgentModal v-if="showCreateModal" @close="showCreateModal = false" />
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { useAgentsStore } from '../stores/agents'
import NavBar from '../components/NavBar.vue'
import CreateAgentModal from '../components/CreateAgentModal.vue'
import { ServerIcon, PlayIcon, StopIcon, ClipboardDocumentCheckIcon } from '@heroicons/vue/24/outline'

const agentsStore = useAgentsStore()
const showCreateModal = ref(false)
const notification = ref(null)
const actionInProgress = ref(null)

const showNotification = (message, type = 'success') => {
  notification.value = { message, type }
  setTimeout(() => {
    notification.value = null
  }, 3000)
}

onMounted(() => {
  agentsStore.fetchAgents()
  agentsStore.startContextPolling()
})

onUnmounted(() => {
  agentsStore.stopContextPolling()
})

// Activity state helpers
const getActivityState = (agentName) => {
  const stats = agentsStore.contextStats[agentName]
  if (!stats) return 'Offline'
  const state = stats.activityState
  if (state === 'active') return 'Active'
  if (state === 'idle') return 'Idle'
  return 'Offline'
}

const getActivityDotClass = (agentName) => {
  const state = getActivityState(agentName)
  if (state === 'Active') return 'bg-green-500 active-pulse'
  if (state === 'Idle') return 'bg-green-500'
  return 'bg-gray-400'
}

const getActivityLabelClass = (agentName) => {
  const state = getActivityState(agentName)
  if (state === 'Active' || state === 'Idle') return 'text-green-600 dark:text-green-400'
  return 'text-gray-500 dark:text-gray-400'
}

// Context helpers
const getContextPercent = (agentName) => {
  const stats = agentsStore.contextStats[agentName]
  return stats ? Math.round(stats.contextPercent || 0) : 0
}

const getProgressBarColor = (agentName) => {
  const percent = getContextPercent(agentName)
  if (percent >= 90) return 'bg-red-500'
  if (percent >= 75) return 'bg-orange-500'
  if (percent >= 50) return 'bg-yellow-500'
  return 'bg-green-500'
}

const startAgent = async (name) => {
  if (actionInProgress.value === name) return
  actionInProgress.value = name
  try {
    const result = await agentsStore.startAgent(name)
    showNotification(result.message, 'success')
  } catch (error) {
    showNotification(error.message || 'Failed to start agent', 'error')
  } finally {
    actionInProgress.value = null
  }
}

const stopAgent = async (name) => {
  if (actionInProgress.value === name) return
  actionInProgress.value = name
  try {
    const result = await agentsStore.stopAgent(name)
    showNotification(result.message, 'success')
  } catch (error) {
    showNotification(error.message || 'Failed to stop agent', 'error')
  } finally {
    actionInProgress.value = null
  }
}
</script>

<style scoped>
/* More pronounced and faster pulsing for active agents */
.active-pulse {
  animation: active-pulse-animation 0.8s ease-in-out infinite;
  box-shadow: 0 0 8px 2px rgba(16, 185, 129, 0.6);
}

@keyframes active-pulse-animation {
  0%, 100% {
    transform: scale(1);
    opacity: 1;
    box-shadow: 0 0 8px 2px rgba(16, 185, 129, 0.6);
  }
  50% {
    transform: scale(1.3);
    opacity: 0.8;
    box-shadow: 0 0 16px 4px rgba(16, 185, 129, 0.9);
  }
}
</style>
