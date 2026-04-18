<template>
  <div :class="activeTab === 'chat' ? 'h-screen overflow-hidden flex flex-col bg-gray-100 dark:bg-gray-900' : 'min-h-screen bg-gray-100 dark:bg-gray-900'">
    <NavBar />

    <main :class="['max-w-[1400px] mx-auto py-2 sm:px-6 lg:px-8', activeTab === 'chat' ? 'flex-1 flex flex-col overflow-hidden' : 'overflow-visible']">
      <div :class="['px-4 sm:px-0', activeTab === 'chat' ? 'flex-1 flex flex-col overflow-hidden' : 'py-2 overflow-visible']">
        <div v-if="loading" class="text-center py-8">
          <div class="animate-spin rounded-full h-12 w-12 border-b-2 border-indigo-500 mx-auto"></div>
        </div>

        <!-- Notification Toast -->
        <div v-if="notification"
          :class="[
            'fixed top-20 right-4 z-50 px-4 py-3 rounded-lg shadow-lg transition-all duration-300',
            notification.type === 'success' ? 'bg-green-100 dark:bg-green-900/50 border border-green-400 dark:border-green-700 text-green-700 dark:text-green-300' : 'bg-red-100 dark:bg-red-900/50 border border-red-400 dark:border-red-700 text-red-700 dark:text-red-300'
          ]"
        >
          {{ notification.message }}
        </div>

        <div v-if="error && !agent" class="bg-red-100 dark:bg-red-900/50 border border-red-400 dark:border-red-700 text-red-700 dark:text-red-300 px-4 py-3 rounded mb-4">
          {{ error }}
        </div>

        <div v-if="agent" :class="['ml-16', activeTab === 'chat' ? 'flex-1 flex flex-col min-h-0' : '']">
          <!-- Agent Header Component -->
          <AgentHeader
            :agent="agent"
            :auth-status="authStatus"
            :subscriptions="availableSubscriptions"
            :subscription-changing="subscriptionChanging"
            :action-loading="actionLoading"
            :autonomy-loading="autonomyLoading"
            :read-only-loading="readOnlyLoading"
            :agent-stats="agentStats"
            :stats-loading="statsLoading"
            :cpu-history="cpuHistory"
            :memory-history="memoryHistory"
            :resource-limits="resourceLimits"
            :has-git-sync="hasGitSync"
            :git-status="gitStatus"
            :git-loading="gitLoading"
            :git-syncing="gitSyncing"
            :git-pulling="gitPulling"
            :git-has-changes="gitHasChanges"
            :git-changes-count="gitChangesCount"
            :git-behind="gitBehind"
            :tags="agentTags"
            :all-tags="allTags"
            @toggle="toggleRunning"
            @delete="deleteAgent"
            @toggle-autonomy="toggleAutonomy"
            @toggle-read-only="toggleReadOnly"
            @open-resource-modal="showResourceModal = true"
            @git-pull="pullFromGithub"
            @git-push="syncToGithub"
            @git-refresh="refreshGitStatus"
            @update-tags="updateTags"
            @add-tag="addTag"
            @remove-tag="removeTag"
            @rename="renameAgent"
            @open-avatar-modal="showAvatarModal = true"
            @cycle-emotion="cycleEmotion"
            @change-subscription="changeSubscription"
            :has-avatar-prompt="!!avatarIdentityPrompt"
            :emotion-avatar-url="emotionAvatarUrl"
          />

          <!-- Tabs -->
          <div :class="['bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg', activeTab === 'chat' ? 'flex-1 flex flex-col overflow-hidden' : '']">
            <div class="border-b border-gray-200 dark:border-gray-700 overflow-x-auto overflow-y-hidden">
              <nav class="-mb-px flex whitespace-nowrap">
                <button
                  v-for="tab in visibleTabs"
                  :key="tab.id"
                  @click="activeTab = tab.id"
                  :class="[
                    'px-4 py-3 border-b-2 font-medium text-sm transition-colors whitespace-nowrap',
                    activeTab === tab.id
                      ? 'border-indigo-500 text-indigo-600 dark:text-indigo-400'
                      : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
                  ]"
                >
                  {{ tab.label }}
                  <span v-if="tab.badge" class="ml-1.5 px-1.5 py-0.5 text-[10px] font-semibold bg-green-100 dark:bg-green-900/50 text-green-700 dark:text-green-300 rounded-full leading-none">
                    {{ tab.badge }}
                  </span>
                </button>
              </nav>
            </div>

            <!-- Info Tab Content -->
            <div v-if="activeTab === 'info'" class="p-6">
              <InfoPanel :agent-name="agent.name" :agent-status="agent.status" @item-click="handleInfoItemClick" />
            </div>

            <!-- Tasks Tab Content -->
            <div v-if="activeTab === 'tasks'" class="p-6">
              <TasksPanel :agent-name="agent.name" :agent-status="agent.status" :highlight-execution-id="route.query.execution" :initial-message="taskPrefillMessage" @create-schedule="handleCreateSchedule" />
            </div>

            <!-- Chat Tab Content (v-show keeps component mounted so state/polling survives tab switches) -->
            <div v-show="activeTab === 'chat'" class="flex-1 overflow-hidden">
              <ChatPanel
                :agent-name="agent.name"
                :agent-status="agent.status"
                :resume-session-id="resumeSessionId"
                :resume-execution-id="resumeExecutionId"
              />
            </div>

            <!-- Dashboard Tab Content -->
            <div v-if="activeTab === 'dashboard'" class="p-6">
              <DashboardPanel :agent-name="agent.name" :agent-status="agent.status" />
            </div>

            <!-- DEPRECATED: Terminal tab hidden for all users (candidate for removal) -->

            <!-- Logs Tab Content -->
            <div v-if="activeTab === 'logs'">
              <LogsPanel :agent-name="agent.name" :agent-status="agent.status" />
            </div>

            <!-- Credentials Tab Content -->
            <div v-if="activeTab === 'credentials'">
              <CredentialsPanel :agent-name="agent.name" :agent-status="agent.status" />
            </div>

            <!-- Nevermined Payments Tab Content -->
            <div v-if="activeTab === 'nevermined'">
              <NeverminedPanel :agent-name="agent.name" :can-edit="agent.can_share" />
            </div>

            <!-- Sharing Tab Content -->
            <div v-if="activeTab === 'sharing' && agent.can_share">
              <SharingPanel
                :agent-name="agent.name"
                :shares="agent.shares"
                @agent-updated="loadAgent"
              />
            </div>

            <!-- Permissions Tab Content -->
            <div v-if="activeTab === 'permissions' && agent.can_share">
              <PermissionsPanel :agent-name="agent.name" :agent-status="agent.status" />
            </div>

            <!-- Schedules Tab Content -->
            <div v-if="activeTab === 'schedules'" class="p-6">
              <SchedulesPanel :agent-name="agent.name" :initial-message="schedulePrefillMessage" />
            </div>

            <!-- Playbooks Tab Content -->
            <div v-if="activeTab === 'playbooks'" class="p-6">
              <PlaybooksPanel
                :agent-name="agent.name"
                :agent-status="agent.status"
                @run-with-instructions="handlePlaybookRunWithInstructions"
              />
            </div>

            <!-- Git Tab Content -->
            <div v-if="activeTab === 'git'" class="p-6">
              <GitPanel :agent-name="agent.name" :agent-status="agent.status" />
            </div>

            <!-- Files Tab Content -->
            <div v-if="activeTab === 'files'">
              <FilesPanel :agent-name="agent.name" :agent-status="agent.status" />
            </div>

            <!-- Skills Tab Content -->
            <div v-if="activeTab === 'skills'">
              <SkillsPanel :agent-name="agent.name" :agent-status="agent.status" />
            </div>

            <!-- Shared Folders Tab Content -->
            <div v-if="activeTab === 'folders'" class="p-6">
              <FoldersPanel :agent-name="agent.name" :agent-status="agent.status" :can-share="agent.can_share" />
            </div>

          </div>
        </div>
      </div>
    </main>

    <!-- Resource Modal -->
    <ResourceModal
      :show="showResourceModal"
      :resource-limits="resourceLimits"
      :loading="resourceLimitsLoading"
      @update:show="showResourceModal = $event"
      @update:memory="resourceLimits.memory = $event"
      @update:cpu="resourceLimits.cpu = $event"
      @save="saveResourceLimits"
    />

    <!-- Avatar Generate Modal (AVATAR-001) -->
    <AvatarGenerateModal
      :show="showAvatarModal"
      :agent-name="agent?.name || ''"
      :initial-prompt="avatarIdentityPrompt"
      :current-avatar-url="agent?.avatar_url || null"
      :has-reference="avatarHasReference"
      @close="showAvatarModal = false"
      @updated="onAvatarUpdated"
    />

    <!-- Confirm Dialog -->
    <ConfirmDialog
      v-model:visible="confirmDialog.visible"
      :title="confirmDialog.title"
      :message="confirmDialog.message"
      :confirm-text="confirmDialog.confirmText"
      :variant="confirmDialog.variant"
      @confirm="confirmDialog.onConfirm"
    />

    <!-- Git Conflict Resolution Modal -->
    <GitConflictModal
      :show="showConflictModal"
      :conflict="gitConflict"
      :is-parallel-history="isParallelHistory"
      :pull-branch="pullBranch"
      @resolve="resolveConflict"
      @dismiss="dismissConflict"
    />
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, onActivated, onDeactivated, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import axios from 'axios'
import { useAgentsStore } from '../stores/agents'
import { useAuthStore } from '../stores/auth'
import NavBar from '../components/NavBar.vue'

// Component name for KeepAlive matching
defineOptions({
  name: 'AgentDetail'
})

// UI Components
import ConfirmDialog from '../components/ConfirmDialog.vue'
import GitConflictModal from '../components/GitConflictModal.vue'

// Panel Components (existing)
import SchedulesPanel from '../components/SchedulesPanel.vue'
import TasksPanel from '../components/TasksPanel.vue'
import GitPanel from '../components/GitPanel.vue'
import InfoPanel from '../components/InfoPanel.vue'
import DashboardPanel from '../components/DashboardPanel.vue'
import FoldersPanel from '../components/FoldersPanel.vue'

// Panel Components (newly extracted)
import AgentHeader from '../components/AgentHeader.vue'
import ResourceModal from '../components/ResourceModal.vue'
import AvatarGenerateModal from '../components/AvatarGenerateModal.vue'
import LogsPanel from '../components/LogsPanel.vue'
import CredentialsPanel from '../components/CredentialsPanel.vue'
import SharingPanel from '../components/SharingPanel.vue'
import PermissionsPanel from '../components/PermissionsPanel.vue'
import FilesPanel from '../components/FilesPanel.vue'
import TerminalPanelContent from '../components/TerminalPanelContent.vue'
import SkillsPanel from '../components/SkillsPanel.vue'
import PlaybooksPanel from '../components/PlaybooksPanel.vue'
import ChatPanel from '../components/ChatPanel.vue'
import NeverminedPanel from '../components/NeverminedPanel.vue'

// Import composables
import { useNotification } from '../composables'
import { useAgentLifecycle } from '../composables/useAgentLifecycle'
import { useAgentStats } from '../composables/useAgentStats'
import { useAgentTerminal } from '../composables/useAgentTerminal'
import { useGitSync } from '../composables/useGitSync'
import { useAgentSettings } from '../composables/useAgentSettings'
import { useSessionActivity } from '../composables/useSessionActivity'

// Setup
const route = useRoute()
const router = useRouter()
const agentsStore = useAgentsStore()
const authStore = useAuthStore()

// Minimal local state
const agent = ref(null)
const loading = ref(true)
const error = ref('')
const activeTab = ref('tasks')
const showResourceModal = ref(false)
const showAvatarModal = ref(false)
const avatarIdentityPrompt = ref('')
const avatarHasReference = ref(false)
// Emotion avatar cycling state (AVATAR-002)
const availableEmotions = ref([])
const emotionAvatarUrl = ref(null)
const emotionCycleTimer = ref(null)

const taskPrefillMessage = ref('')
const schedulePrefillMessage = ref('')
const hasDashboard = ref(false)

// Tags state (ORG-001)
const agentTags = ref([])
const allTags = ref([])

// Auth status state
const authStatus = ref(null)
const availableSubscriptions = ref(null)
const subscriptionChanging = ref(false)

// Resume mode state (EXEC-023)
const resumeSessionId = computed(() => route.query.resumeSessionId || null)
const resumeExecutionId = computed(() => route.query.executionId || null)

// Initialize composables
const { notification, showNotification } = useNotification()

// Agent lifecycle composable
const {
  actionLoading,
  confirmDialog,
  startAgent,
  stopAgent,
  deleteAgent
} = useAgentLifecycle(agent, agentsStore, router, showNotification)

// Stats composable
const {
  agentStats,
  statsLoading,
  cpuHistory,
  memoryHistory,
  startStatsPolling,
  stopStatsPolling
} = useAgentStats(agent, agentsStore)

// Terminal composable
const {
  isTerminalFullscreen,
  terminalRef,
  toggleTerminalFullscreen,
  handleTerminalKeydown,
  onTerminalConnected,
  onTerminalDisconnected,
  onTerminalError
} = useAgentTerminal(showNotification)

// Git sync composable
const {
  hasGitSync,
  gitStatus,
  gitLoading,
  gitSyncing,
  gitPulling,
  gitHasChanges,
  gitChangesCount,
  gitBehind,
  gitConflict,
  showConflictModal,
  // S2 (issue #385) parallel-history detection
  isParallelHistory,
  pullBranch,
  refreshGitStatus,
  syncToGithub,
  pullFromGithub,
  resolveConflict,
  dismissConflict,
  startGitStatusPolling,
  stopGitStatusPolling
} = useGitSync(agent, agentsStore, showNotification)

// Autonomy mode state
const autonomyLoading = ref(false)

// Read-only mode state
const readOnlyLoading = ref(false)

async function toggleAutonomy() {
  if (!agent.value || autonomyLoading.value) return

  autonomyLoading.value = true
  const newState = !agent.value.autonomy_enabled

  try {
    const response = await fetch(`/api/agents/${agent.value.name}/autonomy`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem('token')}`
      },
      body: JSON.stringify({ enabled: newState })
    })

    if (!response.ok) {
      const error = await response.json()
      throw new Error(error.detail || 'Failed to update autonomy mode')
    }

    const result = await response.json()

    // Update local state
    agent.value.autonomy_enabled = newState

    showNotification(
      newState
        ? `Autonomy enabled. ${result.schedules_updated} schedule(s) activated.`
        : `Autonomy disabled. ${result.schedules_updated} schedule(s) paused.`,
      'success'
    )
  } catch (error) {
    console.error('Failed to toggle autonomy:', error)
    showNotification(error.message || 'Failed to update autonomy mode', 'error')
  } finally {
    autonomyLoading.value = false
  }
}

async function toggleReadOnly() {
  if (!agent.value || readOnlyLoading.value) return

  readOnlyLoading.value = true
  const newState = !agent.value.read_only_enabled

  try {
    const response = await fetch(`/api/agents/${agent.value.name}/read-only`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem('token')}`
      },
      body: JSON.stringify({ enabled: newState })
    })

    if (!response.ok) {
      const error = await response.json()
      throw new Error(error.detail || 'Failed to update read-only mode')
    }

    const result = await response.json()

    // Update local state
    agent.value.read_only_enabled = newState

    showNotification(
      newState
        ? `Read-only mode enabled. Agent cannot modify source files.${result.hooks_injected ? '' : ' Hooks will be applied on next agent start.'}`
        : 'Read-only mode disabled. Agent can modify all files.',
      'success'
    )
  } catch (error) {
    console.error('Failed to toggle read-only mode:', error)
    showNotification(error.message || 'Failed to update read-only mode', 'error')
  } finally {
    readOnlyLoading.value = false
  }
}

// Toggle running state (start/stop)
async function toggleRunning() {
  if (!agent.value || actionLoading.value) return

  if (agent.value.status === 'running') {
    await stopAgent()
  } else {
    await startAgent()
  }
}

// Rename agent (RENAME-001)
const renameLoading = ref(false)

async function renameAgent(newName) {
  if (!agent.value || renameLoading.value) return

  renameLoading.value = true

  try {
    const response = await fetch(`/api/agents/${agent.value.name}/rename`, {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem('token')}`
      },
      body: JSON.stringify({ new_name: newName })
    })

    if (!response.ok) {
      const error = await response.json()
      throw new Error(error.detail || 'Failed to rename agent')
    }

    const result = await response.json()

    // Navigate to new URL with new agent name
    showNotification(`Agent renamed to '${result.new_name}'${result.note ? `. ${result.note}` : ''}`, 'success')

    // Navigate to the new agent URL
    router.replace({ name: 'AgentDetail', params: { name: result.new_name } })

  } catch (error) {
    console.error('Failed to rename agent:', error)
    showNotification(error.message || 'Failed to rename agent', 'error')
  } finally {
    renameLoading.value = false
  }
}

// Default model based on runtime
const defaultModel = computed(() => {
  const runtime = agent.value?.runtime || 'claude-code'
  if (runtime === 'gemini-cli' || runtime === 'gemini') {
    return 'gemini-2.5-flash'
  }
  return 'sonnet' // Claude default
})

// Agent settings composable
const {
  apiKeySetting,
  apiKeySettingLoading,
  loadApiKeySetting,
  updateApiKeySetting,
  currentModel,
  resourceLimits,
  resourceLimitsLoading,
  loadResourceLimits,
  updateResourceLimits
} = useAgentSettings(agent, agentsStore, showNotification)

// Save resource limits and restart agent if needed
async function saveResourceLimits() {
  // Check if values actually changed
  const newMemory = resourceLimits.value.memory || resourceLimits.value.current_memory
  const newCpu = resourceLimits.value.cpu || resourceLimits.value.current_cpu
  const oldMemory = resourceLimits.value.current_memory
  const oldCpu = resourceLimits.value.current_cpu
  const valuesChanged = newMemory !== oldMemory || newCpu !== oldCpu

  await updateResourceLimits()
  showResourceModal.value = false

  // If values changed and agent is running, restart it
  if (valuesChanged && agent.value?.status === 'running') {
    showNotification('Restarting agent to apply new resource limits...', 'info')
    await stopAgent()
    // Wait a moment for container to fully stop
    await new Promise(resolve => setTimeout(resolve, 1000))
    await startAgent()
  }
}

// Session activity composable
const {
  sessionInfo,
  startActivityPolling,
  stopActivityPolling,
  loadSessionInfo,
  resetSessionActivity
} = useSessionActivity(agent, agentsStore)

// Computed tabs based on agent permissions and system agent status
// Tab order optimized for workflow: primary actions first, configuration/reference last
const visibleTabs = computed(() => {
  const isSystem = agent.value?.is_system

  // Primary tabs - most frequently used
  const tabs = [
    { id: 'tasks', label: 'Tasks' },
    { id: 'chat', label: 'Chat' }
  ]

  // Dashboard tab - only show if agent has a dashboard.yaml file (insert after Tasks)
  if (hasDashboard.value) {
    tabs.push({ id: 'dashboard', label: 'Dashboard' })
  }

  tabs.push(
    { id: 'schedules', label: 'Schedules' },
    { id: 'playbooks', label: 'Playbooks' },
    { id: 'credentials', label: 'Credentials' },
    { id: 'nevermined', label: 'Payments' }
  )

  // Access control tabs - hide for system agent (system agent has full access)
  if (agent.value?.can_share && !isSystem) {
    tabs.push({ id: 'sharing', label: 'Sharing' })
    tabs.push({ id: 'permissions', label: 'Permissions' })
  }

  // Git and Files tabs together
  if (hasGitSync.value) {
    tabs.push({ id: 'git', label: 'Git' })
  }
  // DEPRECATED: Terminal tab hidden for all users (candidate for removal)
  // tabs.push({ id: 'terminal', label: 'Terminal' })
  tabs.push({ id: 'files', label: 'Files' })

  // Folders - hide for system agent
  if (agent.value?.can_share && !isSystem) {
    tabs.push({ id: 'folders', label: 'Folders' })
  }

  // Info at the end (reference/metadata)
  tabs.push({ id: 'info', label: 'Info' })

  return tabs
})

// Load agent
async function loadAgent() {
  loading.value = true
  error.value = ''
  try {
    agent.value = await agentsStore.fetchAgent(route.params.name)
  } catch (err) {
    error.value = 'Failed to load agent details'
  } finally {
    loading.value = false
  }
}

// Avatar management (AVATAR-001)
async function loadAvatarIdentity() {
  if (!agent.value?.name) return
  try {
    const response = await axios.get(`/api/agents/${agent.value.name}/avatar/identity`, {
      headers: authStore.authHeader
    })
    avatarIdentityPrompt.value = response.data.identity_prompt || ''
    avatarHasReference.value = response.data.has_reference || false
  } catch (err) {
    // Not critical
  }
}

async function onAvatarUpdated() {
  await loadAgent()
  await loadAvatarIdentity()
  // Stop current cycling — new avatar means old emotions are deleted
  stopEmotionCycling()
  availableEmotions.value = []
  // Poll for new emotions to appear (generated in background, ~15s each)
  let attempts = 0
  const pollInterval = setInterval(async () => {
    attempts++
    await loadAvailableEmotions()
    if (availableEmotions.value.length > 0) {
      startEmotionCycling()
    }
    if (attempts >= 12 || availableEmotions.value.length >= 8) {
      clearInterval(pollInterval)
    }
  }, 15000)
}

// Emotion avatar cycling (AVATAR-002)
async function loadAvailableEmotions() {
  if (!agent.value?.name) return
  try {
    const response = await axios.get(`/api/agents/${agent.value.name}/avatar/emotions`)
    availableEmotions.value = response.data.emotions || []
  } catch (err) {
    availableEmotions.value = []
  }
}

function cycleEmotion() {
  if (availableEmotions.value.length === 0) {
    emotionAvatarUrl.value = null
    return
  }
  const emotion = availableEmotions.value[Math.floor(Math.random() * availableEmotions.value.length)]
  const avatarVersion = agent.value.avatar_url?.split('v=')[1] || '1'
  emotionAvatarUrl.value = `/api/agents/${agent.value.name}/avatar/emotion/${emotion}?v=${avatarVersion}`
}

function startEmotionCycling() {
  stopEmotionCycling()
  if (availableEmotions.value.length === 0) return
  cycleEmotion()
  emotionCycleTimer.value = setInterval(cycleEmotion, 30000)
}

function stopEmotionCycling() {
  if (emotionCycleTimer.value) {
    clearInterval(emotionCycleTimer.value)
    emotionCycleTimer.value = null
  }
  emotionAvatarUrl.value = null
}

// Tags management (ORG-001)
async function loadTags() {
  if (!agent.value?.name) return
  try {
    const response = await axios.get(`/api/agents/${agent.value.name}/tags`, {
      headers: authStore.authHeader
    })
    agentTags.value = response.data.tags || []
  } catch (err) {
    console.error('Failed to load tags:', err)
  }
}

async function loadAllTags() {
  try {
    const response = await axios.get('/api/tags', {
      headers: authStore.authHeader
    })
    allTags.value = (response.data.tags || []).map(t => t.tag)
  } catch (err) {
    console.error('Failed to load all tags:', err)
  }
}

async function updateTags(newTags) {
  if (!agent.value?.name) return
  try {
    const response = await axios.put(`/api/agents/${agent.value.name}/tags`, { tags: newTags }, {
      headers: authStore.authHeader
    })
    agentTags.value = response.data.tags || []
    showNotification('Tags updated', 'success')
  } catch (err) {
    console.error('Failed to update tags:', err)
    showNotification('Failed to update tags', 'error')
  }
}

async function addTag(tag) {
  if (!agent.value?.name) return
  try {
    const response = await axios.post(`/api/agents/${agent.value.name}/tags/${tag}`, {}, {
      headers: authStore.authHeader
    })
    agentTags.value = response.data.tags || []
    // Refresh all tags to show new tag in autocomplete
    await loadAllTags()
  } catch (err) {
    console.error('Failed to add tag:', err)
    showNotification(err.response?.data?.detail || 'Failed to add tag', 'error')
  }
}

async function removeTag(tag) {
  if (!agent.value?.name) return
  try {
    const response = await axios.delete(`/api/agents/${agent.value.name}/tags/${tag}`, {
      headers: authStore.authHeader
    })
    agentTags.value = response.data.tags || []
  } catch (err) {
    console.error('Failed to remove tag:', err)
    showNotification('Failed to remove tag', 'error')
  }
}

// Check if agent has a dashboard.yaml file.
// Uses lightweight DB-backed /exists endpoint first (no agent call needed).
// On failure, retries with backoff since agents need time to boot.
async function checkDashboardExists() {
  if (!agent.value?.name) {
    hasDashboard.value = false
    return
  }

  // Fast path: check DB cache (no agent container call)
  try {
    const exists = await agentsStore.checkDashboardExists(agent.value.name)
    if (exists) {
      hasDashboard.value = true
      return
    }
  } catch {
    // DB check failed — fall through to live check
  }

  // Slow path: try live fetch with retries (agent may still be booting)
  const delays = [0, 3000, 6000]
  for (const delay of delays) {
    if (delay > 0) await new Promise(r => setTimeout(r, delay))
    if (agent.value?.status !== 'running') return
    try {
      const response = await agentsStore.getAgentDashboard(agent.value.name)
      if (response?.has_dashboard === true || response?.stale === true) {
        hasDashboard.value = true
        return
      }
    } catch {
      // Continue to next retry
    }
  }
  // All retries exhausted — no dashboard found
  hasDashboard.value = false
}

// Load auth status (subscription vs API key)
async function loadAuthStatus() {
  if (!agent.value?.name) return
  try {
    const response = await axios.get(`/api/subscriptions/agents/${agent.value.name}/auth`, {
      headers: authStore.authHeader
    })
    authStatus.value = response.data
  } catch (err) {
    // Non-critical - just don't show badge
    authStatus.value = null
  }
}

async function loadAvailableSubscriptions() {
  try {
    const response = await axios.get('/api/subscriptions', {
      headers: authStore.authHeader
    })
    availableSubscriptions.value = response.data || []
  } catch (err) {
    // Non-admin users get 403 - just hide the dropdown
    availableSubscriptions.value = null
  }
}

async function changeSubscription(subscriptionName) {
  if (!agent.value?.name) return
  subscriptionChanging.value = true
  try {
    if (subscriptionName) {
      await axios.put(
        `/api/subscriptions/agents/${encodeURIComponent(agent.value.name)}?subscription_name=${encodeURIComponent(subscriptionName)}`,
        {},
        { headers: authStore.authHeader }
      )
    } else {
      await axios.delete(`/api/subscriptions/agents/${encodeURIComponent(agent.value.name)}`, {
        headers: authStore.authHeader
      })
    }
    await loadAuthStatus()
  } catch (err) {
    showNotification(err.response?.data?.detail || 'Failed to update subscription', 'error')
  } finally {
    subscriptionChanging.value = false
  }
}

// Watch for route changes (when navigating to a different agent)
watch(() => route.params.name, async (newName, oldName) => {
  if (newName && newName !== oldName) {
    // Stop polling for old agent
    stopAllPolling()
    // Reset dashboard state for new agent
    hasDashboard.value = false
    // Reset tags for new agent
    agentTags.value = []
    // Reset auth status for new agent
    authStatus.value = null
    // DEPRECATED: Terminal tab hidden (candidate for removal)
    // if (terminalRef.value?.disconnect) {
    //   terminalRef.value.disconnect()
    // }
    // Load new agent data
    await loadAgent()
    await loadSessionInfo()
    await loadApiKeySetting()
    await loadResourceLimits()
    await loadTags()
    await loadAuthStatus()
    // Load avatar identity for new agent (AVATAR-001)
    await loadAvatarIdentity()
    // Check if new agent has dashboard (only when running)
    if (agent.value?.status === 'running') {
      await checkDashboardExists()
    }
    // Reset activeTab if current tab is not valid for new agent
    // Must use nextTick to ensure visibleTabs has recomputed
    nextTick(() => {
      const validTabIds = visibleTabs.value.map(t => t.id)
      if (!validTabIds.includes(activeTab.value)) {
        activeTab.value = 'tasks'
      }
    })
    startAllPolling()
    // DEPRECATED: Terminal tab hidden (candidate for removal)
    // if (activeTab.value === 'terminal' && agent.value?.status === 'running') {
    //   nextTick(() => {
    //     if (terminalRef.value?.connect) {
    //       terminalRef.value.connect()
    //     }
    //   })
    // }
  }
})

// Watch agent status for stats, activity, git polling, and dashboard check
watch(() => agent.value?.status, async (newStatus) => {
  if (newStatus === 'running') {
    startStatsPolling()
    startActivityPolling()
    if (hasGitSync.value) {
      startGitStatusPolling()
    }
    // Check for dashboard when agent starts running
    await checkDashboardExists()
  } else {
    stopStatsPolling()
    stopActivityPolling()
    stopGitStatusPolling()
    resetSessionActivity()
    // Don't reset hasDashboard — DB cache keeps the tab visible
    // so users can still see the last known dashboard state
  }
})

// Initialize model to default when agent is loaded and model is not set
watch(() => agent.value?.runtime, (newRuntime) => {
  if (newRuntime && !currentModel.value) {
    currentModel.value = defaultModel.value
  }
}, { immediate: true })

// Start all polling (used on mount and activation)
function startAllPolling() {
  if (agent.value?.status === 'running') {
    startStatsPolling()
    startActivityPolling()
    if (hasGitSync.value) {
      startGitStatusPolling()
    }
  }
}

// Stop all polling (used on deactivation and unmount)
function stopAllPolling() {
  stopStatsPolling()
  stopActivityPolling()
  stopGitStatusPolling()
}

// Initialize on mount
onMounted(async () => {
  // Load agent first (other calls may depend on agent data)
  await loadAgent()

  // PERF-269: Parallelize independent mount calls
  await Promise.allSettled([
    loadSessionInfo(),
    loadApiKeySetting(),
    loadResourceLimits(),
    loadTags(),
    loadAllTags(),
    loadAvatarIdentity(),
    loadAvailableEmotions(),
    loadAuthStatus(),
    loadAvailableSubscriptions(),
    ...(agent.value?.status === 'running' ? [checkDashboardExists()] : [])
  ])
  startEmotionCycling()
  startAllPolling()

  // Handle tab query param (from Timeline click navigation)
  if (route.query.tab) {
    const requestedTab = route.query.tab
    const validTabs = ['tasks', 'chat', 'dashboard', 'logs', 'files', 'schedules', 'credentials', 'skills', 'sharing', 'permissions', 'git', 'folders', 'info']
    if (validTabs.includes(requestedTab)) {
      activeTab.value = requestedTab
    }
  }
})

// onActivated fires when component is shown (after being cached by KeepAlive)
onActivated(async () => {
  // Restart polling when returning to this view
  startAllPolling()
  // Refresh agent data
  await loadAgent()
  // Reload emotions and restart cycling (AVATAR-002)
  await loadAvailableEmotions()
  startEmotionCycling()
  // Re-check for dashboard if agent is running
  if (agent.value?.status === 'running') {
    await checkDashboardExists()
  }

  // Handle tab query param (EXEC-023: Continue as Chat navigation)
  // Must also check here since onMounted doesn't fire for cached components
  if (route.query.tab) {
    const requestedTab = route.query.tab
    const validTabs = ['tasks', 'chat', 'dashboard', 'logs', 'files', 'schedules', 'credentials', 'skills', 'sharing', 'permissions', 'git', 'folders', 'info']
    if (validTabs.includes(requestedTab)) {
      activeTab.value = requestedTab
    }
  }

  // DEPRECATED: Terminal tab hidden (candidate for removal)
  // if (activeTab.value === 'terminal') {
  //   nextTick(() => {
  //     if (terminalRef.value?.fit) {
  //       terminalRef.value.fit()
  //     }
  //   })
  // }
})

// onDeactivated fires when navigating away (component is cached, not destroyed)
onDeactivated(() => {
  // Stop polling when navigating away (but keep WebSocket connection alive)
  stopAllPolling()
  stopEmotionCycling()
})

// onUnmounted fires when component is actually destroyed
onUnmounted(() => {
  stopAllPolling()
  stopEmotionCycling()
})

// Handle item click from Info tab - switch to Tasks tab with prefilled message
const handleInfoItemClick = ({ type, text }) => {
  // Set the prefill message and switch to Tasks tab
  taskPrefillMessage.value = text
  activeTab.value = 'tasks'
  // Clear the prefill after a short delay so it can be used again
  nextTick(() => {
    setTimeout(() => {
      taskPrefillMessage.value = ''
    }, 100)
  })
}

// Handle create schedule from Tasks tab - switch to Schedules tab with prefilled message
const handleCreateSchedule = (message) => {
  // Set the prefill message and switch to Schedules tab
  schedulePrefillMessage.value = message
  activeTab.value = 'schedules'
  // Clear the prefill after a short delay so it can be used again
  nextTick(() => {
    setTimeout(() => {
      schedulePrefillMessage.value = ''
    }, 100)
  })
}

// Handle run-with-instructions from Playbooks tab
const handlePlaybookRunWithInstructions = (prefillText) => {
  // Check if this is a navigation request (one-click run completed)
  if (prefillText.startsWith('__NAVIGATE_TASKS__:')) {
    const executionId = prefillText.replace('__NAVIGATE_TASKS__:', '')
    // Navigate to Tasks tab with execution highlighted via query param
    activeTab.value = 'tasks'
    // The TasksPanel will pick up the execution via the highlight-execution-id prop
    router.replace({ query: { ...route.query, execution: executionId } })
    return
  }

  // Normal case: prefill the task input and switch to Tasks tab
  taskPrefillMessage.value = prefillText
  activeTab.value = 'tasks'
  // Clear the prefill after a short delay so it can be used again
  nextTick(() => {
    setTimeout(() => {
      taskPrefillMessage.value = ''
    }, 100)
  })
}
</script>

<style scoped>
/* Animated progress bar pulse effect */
@keyframes progress-pulse {
  0%, 100% {
    opacity: 1;
  }
  50% {
    opacity: 0.7;
  }
}

.animate-progress-pulse {
  animation: progress-pulse 2s ease-in-out infinite;
}

/* Shimmer effect for progress bars */
@keyframes shimmer {
  0% {
    background-position: -200% 0;
  }
  100% {
    background-position: 200% 0;
  }
}

.animate-shimmer {
  background: linear-gradient(
    90deg,
    transparent 0%,
    rgba(255, 255, 255, 0.3) 50%,
    transparent 100%
  );
  background-size: 200% 100%;
  animation: shimmer 2s ease-in-out infinite;
}
</style>
