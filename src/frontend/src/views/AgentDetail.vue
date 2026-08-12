<template>
  <div :class="isFullscreenTab ? 'h-screen overflow-hidden flex flex-col bg-gray-100 dark:bg-gray-900' : 'min-h-screen bg-gray-100 dark:bg-gray-900'">
    <NavBar />

    <!-- #954: w-full keeps <main> at its max width in BOTH layout modes. Without it,
         the fullscreen (Chat) mode makes the root a flex column, and `mx-auto`'s auto
         inline margins override align-items:stretch on the flex item — collapsing
         <main> to content width and shifting/narrowing the whole card on tab switch. -->
    <main :class="['w-full max-w-[1400px] mx-auto py-2 sm:px-6 lg:px-8', isFullscreenTab ? 'flex-1 flex flex-col overflow-hidden' : 'overflow-visible']">
      <div :class="['px-4 sm:px-0 py-2', isFullscreenTab ? 'flex-1 flex flex-col overflow-hidden' : 'overflow-visible']">
        <div v-if="loading" class="text-center py-8">
          <div class="animate-spin rounded-full h-12 w-12 border-b-2 border-action-primary-500 mx-auto"></div>
        </div>

        <!-- Notification Toast.
             #1953: anchored bottom-right, NOT `top-20 right-4`. `top-20` is exactly
             where AgentHeader's row-1 right cluster starts (NavBar h-16 + main py-2 +
             inner py-2 = 80px), so the toast covered the Running toggle and Delete.
             `bottom-24` clears the global HelpChatWidget FAB (bottom-6 + h-14 = 80px),
             which is why this is not the `bottom-4 right-4` used elsewhere. -->
        <div v-if="notification"
          :class="[
            'fixed bottom-24 right-6 z-50 px-4 py-3 rounded-lg shadow-lg transition-all duration-300',
            notification.type === 'success' ? 'bg-status-success-100 dark:bg-status-success-900/50 border border-status-success-400 dark:border-status-success-700 text-status-success-700 dark:text-status-success-300' : 'bg-status-danger-100 dark:bg-status-danger-900/50 border border-status-danger-400 dark:border-status-danger-700 text-status-danger-700 dark:text-status-danger-300'
          ]"
        >
          <span>{{ notification.message }}</span>
          <button
            v-if="notification.type === 'error'"
            type="button"
            class="ml-3 font-medium opacity-70 hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-status-danger-500/40 rounded"
            aria-label="Dismiss notification"
            @click="dismissNotification"
          >✕</button>
        </div>

        <!-- #1914: agent 404'd. Deliberately does NOT say whether the agent
             exists — the backend's 404 is uniform for missing vs. inaccessible
             (Invariant #8 / #186) and this copy must not undo that. -->
        <div v-if="notFound && !loading" data-testid="agent-not-found" class="max-w-lg mx-auto text-center py-16">
          <svg class="mx-auto h-12 w-12 text-gray-400 dark:text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round" d="M9.75 9.75l4.5 4.5m0-4.5l-4.5 4.5M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <h2 class="mt-4 text-lg font-semibold text-gray-900 dark:text-gray-100">Agent not found</h2>
          <p class="mt-2 text-sm text-gray-600 dark:text-gray-400">
            <span class="font-mono text-gray-800 dark:text-gray-200">{{ route.params.name }}</span>
            doesn't exist, or you don't have access to it.
          </p>
          <router-link
            to="/"
            class="mt-6 inline-block px-4 py-2 rounded-lg bg-action-primary-600 hover:bg-action-primary-700 text-white text-sm font-medium transition-colors"
          >
            Back to Dashboard
          </router-link>
        </div>

        <div v-if="error && !agent" data-testid="agent-load-error" class="bg-status-danger-100 dark:bg-status-danger-900/50 border border-status-danger-400 dark:border-status-danger-700 text-status-danger-700 dark:text-status-danger-300 px-4 py-3 rounded mb-4 flex items-center justify-between gap-4">
          <span>{{ error }}</span>
          <button
            @click="loadAgent"
            data-testid="agent-load-retry"
            class="shrink-0 px-3 py-1 rounded border border-status-danger-400 dark:border-status-danger-700 text-sm font-medium hover:bg-status-danger-200 dark:hover:bg-status-danger-900 transition-colors"
          >
            Retry
          </button>
        </div>

        <div v-if="agent" :class="['ml-16', isFullscreenTab ? 'flex-1 flex flex-col min-h-0' : '']">
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
            :token-stats="tokenStats"
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
            @set-label="setAgentLabel"
            @open-avatar-modal="showAvatarModal = true"
            @cycle-emotion="cycleEmotion"
            @change-subscription="changeSubscription"
            :has-avatar-prompt="!!avatarIdentityPrompt"
            :emotion-avatar-url="emotionAvatarUrl"
            :voice-available="sessionsStore.voiceAvailable"
            :workspace-available="sessionsStore.workspaceAvailable"
            :brain-available="sessionsStore.brainOrbAvailable && hasBrainOrb"
          />

          <!-- Tabs -->
          <div :class="['bg-white dark:bg-gray-800 shadow dark:shadow-gray-900 rounded-lg', isFullscreenTab ? 'flex-1 flex flex-col overflow-hidden' : '']">
            <!-- #1114: tabs overflow into a "More ▾" dropdown instead of horizontal scroll -->
            <OverflowTabs :tabs="visibleTabs" v-model="activeTab" />

            <!-- Overview Tab Content (#1107 — default landing tab) -->
            <div v-if="activeTab === 'overview'" class="p-6">
              <OverviewPanel :agent="agent" @navigate-tab="handleOverviewNavigate" @open-task="handleOpenTask" />
            </div>

            <!-- Info Tab Content -->
            <div v-if="activeTab === 'info'" class="p-6">
              <InfoPanel :agent-name="agent.name" :agent-status="agent.status" @item-click="handleInfoItemClick" />
            </div>

            <!-- Brain Tab Content (#60) — settings + launch, not an auto-jump -->
            <div v-if="activeTab === 'brain'" class="p-6">
              <BrainPanel :name="agent.name" :running="agent.status === 'running'" />
            </div>

            <!-- Tasks Tab Content (#1500: fullscreen flex-fill like Chat; padding
                 lives on TasksPanel's scroll root so content scrolls under it) -->
            <div v-if="activeTab === 'tasks'" class="flex-1 min-h-0 flex flex-col overflow-hidden">
              <TasksPanel :agent-name="agent.name" :agent-status="agent.status" :highlight-execution-id="route.query.execution" :initial-message="taskPrefillMessage" @create-schedule="handleCreateSchedule" />
            </div>

            <!-- Chat Tab Content.
                 ent#358: the Session-mode toggle is gone — continuous
                 conversation lives in the Workspace now, which resumes the same
                 way this surface used to (see the "Continue in Workspace" link
                 below). What stays here is the stateless per-turn chat, which
                 the Workspace does NOT replace.
                 v-show keeps the surface mounted so state/polling survives tab switches. -->
            <div v-show="activeTab === 'chat'" class="flex-1 overflow-hidden flex flex-col">
              <div class="flex items-center justify-end gap-2 px-3 py-1.5 border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/40">
                <span class="text-xs text-gray-500 dark:text-gray-400">
                  Stateless chat — each message starts fresh.
                </span>
                <router-link
                  :to="{ path: '/workspace', query: { agent: agent.name } }"
                  class="text-xs font-medium text-action-primary-600 hover:text-action-primary-700 dark:text-action-primary-400 dark:hover:text-action-primary-300"
                  title="The Workspace keeps one continuous conversation — memory, tool results and reasoning carry across turns."
                >
                  Continue in Workspace →
                </router-link>
              </div>

              <div class="flex-1 overflow-hidden">
                <ChatPanel
                  :agent-name="agent.name"
                  :agent-status="agent.status"
                  :resume-session-id="resumeSessionId"
                  :resume-execution-id="resumeExecutionId"
                />
              </div>
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

            <!-- A2A Tab Content (trinity-enterprise#158) -->
            <div v-if="activeTab === 'a2a' && agent.can_share" class="p-6">
              <A2aPanel :agent-name="agent.name" :notify="showNotification" />
            </div>

            <!-- Sharing Tab Content -->
            <!-- Access Tab Content (#17 — Trinity operators) -->
            <div v-if="activeTab === 'access' && agent.can_share">
              <AccessPanel :agent-name="agent.name" />
            </div>

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
              <SchedulesPanel
                :agent-name="agent.name"
                :initial-message="schedulePrefillMessage"
                :autonomy-enabled="!!agent.autonomy_enabled"
                @enable-autonomy="toggleAutonomy"
              />
            </div>

            <!-- Reports Tab Content (#918) -->
            <div v-if="activeTab === 'reports'">
              <ReportsPanel :agent-name="agent.name" :can-delete="agent.can_share" />
            </div>

            <!-- Loops Tab Content (#1106 / #740 Phase 2) -->
            <div v-if="activeTab === 'loops'">
              <LoopsPanel :agent-name="agent.name" :agent-status="agent.status" />
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
            <div v-if="activeTab === 'skills'" class="p-6">
              <SkillsPanel
                :agent-name="agent.name"
                :can-manage="!!agent.can_share"
                :agent-running="agent.status === 'running'"
              />
            </div>

            <!-- Shared Folders Tab Content -->
            <div v-if="activeTab === 'folders'" class="p-6">
              <FoldersPanel :agent-name="agent.name" :agent-status="agent.status" :can-share="agent.can_share" />
            </div>

            <!-- Settings Tab Content (#1108) — sectioned home for per-agent
                 config; Guardrails (GUARD-001 UI, #967) is section #1 -->
            <div v-if="activeTab === 'settings' && agent.can_share" class="p-6 space-y-6">
              <SettingsPanel :agent-name="agent.name" :notify="showNotification" />
              <!-- ent#277: renders only when the entitlement is present. -->
              <CrossModelValidationPanel :agent-name="agent.name" />
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
import { useSessionsStore } from '../stores/sessions'  // SESSION_TAB_2026-04 Phase 3
import NavBar from '../components/NavBar.vue'

// Component name for KeepAlive matching
defineOptions({
  name: 'AgentDetail'
})

// UI Components
import ConfirmDialog from '../components/ConfirmDialog.vue'
import GitConflictModal from '../components/GitConflictModal.vue'

// Panel Components (existing)
import OverviewPanel from '../components/OverviewPanel.vue'
import SchedulesPanel from '../components/SchedulesPanel.vue'
import LoopsPanel from '../components/LoopsPanel.vue'
import ReportsPanel from '../components/ReportsPanel.vue'
import TasksPanel from '../components/TasksPanel.vue'
import GitPanel from '../components/GitPanel.vue'
import InfoPanel from '../components/InfoPanel.vue'
import DashboardPanel from '../components/DashboardPanel.vue'
import FoldersPanel from '../components/FoldersPanel.vue'
import SettingsPanel from '../components/settings/SettingsPanel.vue'
import CrossModelValidationPanel from '../components/CrossModelValidationPanel.vue'

// Panel Components (newly extracted)
import AgentHeader from '../components/AgentHeader.vue'
import BrainPanel from '../components/BrainPanel.vue'
import ResourceModal from '../components/ResourceModal.vue'
import AvatarGenerateModal from '../components/AvatarGenerateModal.vue'
import LogsPanel from '../components/LogsPanel.vue'
import CredentialsPanel from '../components/CredentialsPanel.vue'
import SharingPanel from '../components/SharingPanel.vue'
import AccessPanel from '../components/AccessPanel.vue'
import PermissionsPanel from '../components/PermissionsPanel.vue'
import FilesPanel from '../components/FilesPanel.vue'
import TerminalPanelContent from '../components/TerminalPanelContent.vue'
import SkillsPanel from '../components/SkillsPanel.vue'
import PlaybooksPanel from '../components/PlaybooksPanel.vue'
import ChatPanel from '../components/ChatPanel.vue'
import NeverminedPanel from '../components/NeverminedPanel.vue'
import A2aPanel from '../components/A2aPanel.vue'  // trinity-enterprise#158: A2A config tab
import OverflowTabs from '../components/OverflowTabs.vue'  // #1114: tab overflow dropdown

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
const sessionsStore = useSessionsStore()  // SESSION_TAB_2026-04 Phase 3

// Minimal local state
const agent = ref(null)
const loading = ref(true)
const error = ref('')
const notFound = ref(false)  // #1914: the agent 404'd (missing OR inaccessible — uniform by design)
const activeTab = ref('overview')  // #1107: Overview is the default landing tab
// Tabs reachable via ?tab= deep-link (Timeline / EXEC-023 navigation).
// Single source — referenced in onMounted + onActivated (#1107: dedupe + overview).
const DEEP_LINK_TABS = ['overview', 'tasks', 'chat', 'reports', 'dashboard', 'logs', 'files', 'schedules', 'credentials', 'skills', 'sharing', 'permissions', 'git', 'folders', 'settings', 'info']
// Legacy ?tab= ids that moved/renamed — keep old deep-links working (#1108).
// ent#358: `session` is no longer an alias — it REDIRECTS (see below). The
// surface it named lives in the Workspace now, so resolving it to a local tab
// would silently land the user on stateless chat while their link asked for a
// continuous conversation.
const TAB_ALIASES = { guardrails: 'settings' }
// Resolve a ?tab= value to a live tab id (applying aliases), or null if unknown.
function resolveDeepLinkTab(requested) {
  const resolved = TAB_ALIASES[requested] || requested
  return DEEP_LINK_TABS.includes(resolved) ? resolved : null
}
// Apply the ?tab= deep-link landing. Called from BOTH onMounted AND onActivated:
// AgentDetail is KeepAlive-cached (App.vue), so the common path — open agent
// (caches it), click into an execution, "Continue as Chat" back — hits
// onActivated, NOT onMounted. Handling it in onMounted alone silently dropped
// the landing with no error and no banner (EXEC-023 #1672). One shared fn so the
// two hooks can't drift again.
//
function applyDeepLinkRouting() {
  if (route.query.tab) {
    const resolvedTab = resolveDeepLinkTab(route.query.tab)
    if (resolvedTab) activeTab.value = resolvedTab
  }
}

// ent#358: a `?tab=session` link (or any older session deep link) asked for the
// continuous-conversation surface. That surface is the Workspace now, so send
// them there — query-preserving, minus the tab key that no longer names
// anything here. `replace`, not `push`: the retired URL should not sit in the
// back stack waiting to bounce them again.
//
// Called FIRST in both lifecycle hooks and returns true when it navigates, so
// the caller can skip setting up a view that is about to unmount.
function redirectRetiredSessionLink() {
  if (route.query.tab !== 'session') return false
  const { tab, ...rest } = route.query
  router.replace({ path: '/workspace', query: { ...rest, agent: route.params.name } })
  return true
}
// Tabs that flex-fill the viewport (page enters h-screen fullscreen layout).
// #1112: Chat (both session and legacy modes render ChatMessages, which depends
// on flex-1 grow). #1500: Tasks (list fills instead of a fixed max-h-96 cap).
// A tab joining this list must render a wrapper that is `flex-1 min-h-0 flex
// flex-col overflow-hidden` and a panel whose root fills it with its own
// `flex-1 min-h-0 overflow-y-auto` scroll region (see TasksPanel).
const FULLSCREEN_TABS = ['chat', 'tasks']
const isFullscreenTab = computed(() => FULLSCREEN_TABS.includes(activeTab.value))
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
// #58 (trinity-enterprise) — Brain Orb: per-agent half of the gate. True when the
// agent's template.yaml declares the generalizable `brain-orb` capability token.
const hasBrainOrb = ref(false)

// Tags state (ORG-001)
const agentTags = ref([])
const allTags = ref([])

// Auth status state
const authStatus = ref(null)
const availableSubscriptions = ref(null)
const subscriptionChanging = ref(false)

// Token usage stats (issue #250) — DB-sourced, persists across restarts
const tokenStats = ref(null)

// Resume mode state (EXEC-023)
const resumeSessionId = computed(() => route.query.resumeSessionId || null)
const resumeExecutionId = computed(() => route.query.executionId || null)

// ent#358: the Chat tab no longer forks between a session surface and a
// stateless one. Continuous conversation moved to the Workspace, which runs the
// same --resume engine; what remains here is the stateless per-turn chat that
// ExecutionDetail's "Continue as Chat" resumes into (EXEC-023 #1672), so the
// mode toggle, its localStorage preference and the transient routing override
// all went with the surface they selected.

// Initialize composables
const { notification, showNotification, dismissNotification } = useNotification()

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

    // #1945: the server authors this line — the toggle no longer "activates"
    // schedules, it gates them, and the message names the case (no schedules /
    // all disabled / N of M will run) that the raw count used to hide.
    showNotification(
      result.message ||
        `Autonomy ${newState ? 'enabled' : 'disabled'}.`,
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
// ent#181: the pencil edits the LABEL — one column, no restart, no re-key.
// `label === null` clears it and the agent renders under its slug again.
async function setAgentLabel(label) {
  if (!agent.value) return
  try {
    const res = await agentsStore.setAgentLabel(agent.value.name, label)
    // Reflect it locally so the header updates without a refetch; the slug
    // (agent.value.name) is deliberately untouched.
    agent.value.display_label = res.label
  } catch (e) {
    console.error('Failed to set agent label:', e)
    error.value = e.response?.data?.detail || 'Failed to update the label'
  }
}

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
  if (runtime === 'codex') {
    return 'gpt-5.1-codex' // OpenAI Codex default (#1187)
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
// #1126: poll the agent's real status until it reaches one of `targets`, or
// timeout. Returns true if reached. Tolerates transient fetch errors mid-stop.
async function waitForAgentStatus(targets, timeoutMs = 30000, intervalMs = 1000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const a = await agentsStore.fetchAgent(agent.value.name)
      if (a) {
        agent.value = a
        if (targets.includes(a.status)) return true
      }
    } catch (_) {
      // transient (container mid-teardown) — keep polling
    }
    await new Promise(resolve => setTimeout(resolve, intervalMs))
  }
  return false
}

async function saveResourceLimits() {
  // Check if values actually changed. Compare against the effective (current_*)
  // values; an empty override means "inherit", which equals current.
  const newMemory = resourceLimits.value.memory || resourceLimits.value.current_memory
  const newCpu = resourceLimits.value.cpu || resourceLimits.value.current_cpu
  const oldMemory = resourceLimits.value.current_memory
  const oldCpu = resourceLimits.value.current_cpu
  const valuesChanged = newMemory !== oldMemory || newCpu !== oldCpu

  // #1126: don't restart if the save didn't actually persist.
  const saved = await updateResourceLimits()
  if (!saved) return  // composable already surfaced the error
  showResourceModal.value = false

  // If values changed and agent is running, restart it to apply the new limits.
  if (valuesChanged && agent.value?.status === 'running') {
    showNotification('Restarting agent to apply new resource limits...', 'info')
    try {
      await stopAgent()
      // #1126: gate the start on the container actually being stopped rather
      // than a fixed 1s sleep (insufficient under load → "sometimes works").
      const stopped = await waitForAgentStatus(['stopped', 'exited', 'created'])
      if (!stopped) {
        showNotification(
          'Agent did not stop within 30s — not restarting automatically. Start it manually to apply the new limits.',
          'error',
        )
        return
      }
      await startAgent()
      // Refresh effective values so the header/dialog reflect the applied limits.
      await loadAgent()
      await loadResourceLimits()
      showNotification('Agent restarted with new resource limits.', 'success')
    } catch (err) {
      showNotification(
        `Restart failed: ${err?.message || err}. The agent may be stopped — start it manually.`,
        'error',
      )
    }
  } else {
    // No restart needed — still refresh the effective values shown in the UI.
    await loadResourceLimits()
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

  // Primary tabs - most frequently used. Overview leads (#1107).
  const tabs = [
    { id: 'overview', label: 'Overview' },
    { id: 'tasks', label: 'Tasks' },
    { id: 'chat', label: 'Chat' }
  ]

  // #1112 collapsed the Session tab into the Chat tab above; ent#358 retired
  // the surface entirely — continuous conversation is the Workspace's job now.
  // The Chat tab keeps stateless per-turn chat and links across.

  // Dashboard tab - only show if agent has a dashboard.yaml file (insert after Tasks)
  if (hasDashboard.value) {
    tabs.push({ id: 'dashboard', label: 'Dashboard' })
  }

  // Brain Orb tab (#58) — platform flag AND per-agent capability. Selecting it
  // navigates to the dedicated /agents/:name/brain route (handled by a watcher).
  if (sessionsStore.brainOrbAvailable && hasBrainOrb.value) {
    tabs.push({ id: 'brain', label: 'Brain' })
  }

  tabs.push(
    { id: 'reports', label: 'Reports' },  // #918 agent-published reports
    { id: 'schedules', label: 'Schedules' },
    { id: 'loops', label: 'Loops' },
    { id: 'playbooks', label: 'Playbooks' },
    { id: 'credentials', label: 'Credentials' },
    { id: 'nevermined', label: 'Payments' }
  )

  // Access control tabs - hide for system agent (system agent has full access)
  if (agent.value?.can_share && !isSystem) {
    tabs.push({ id: 'access', label: 'Access' })  // #17 operators (Trinity users)
    tabs.push({ id: 'sharing', label: 'Sharing' })
    tabs.push({ id: 'permissions', label: 'Permissions' })
  }

  // A2A tab (trinity-enterprise#158) — owner-only, non-system, and only when the
  // enterprise A2A module is entitled (never a blank tab in OSS/unentitled).
  if (sessionsStore.a2aAvailable && agent.value?.can_share && !isSystem) {
    tabs.push({ id: 'a2a', label: 'A2A' })
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

  // Skills (#235) — unhidden. Was kept out of `visibleTabs` per requirements
  // §22.2 ("component preserved for potential admin-only access") while
  // assignment stayed REST/MCP-only, so the #182/#183 machinery had no product
  // surface at all. Owner/admin and non-system, matching the other management
  // tabs; OverflowTabs absorbs the extra entry.
  if (agent.value?.can_share && !isSystem) {
    tabs.push({ id: 'skills', label: 'Skills' })
  }

  // Settings - owner-only (#1108); sectioned config home, Guardrails is section #1
  if (agent.value?.can_share && !isSystem) {
    tabs.push({ id: 'settings', label: 'Settings' })
  }

  // Info at the end (reference/metadata)
  tabs.push({ id: 'info', label: 'Info' })

  return tabs
})

// Load agent
//
// #1914: a failure here has to reach the template, or the page renders blank —
// `v-if="agent"` and the error banner are both false when `agent` is null and
// `error` is ''. Two distinct failure states:
//   404      -> not-found panel. The backend returns a UNIFORM 404 for both a
//               non-existent agent and one this caller cannot access (an
//               enumeration oracle otherwise — Invariant #8 / #186), so the
//               copy must not claim which of the two it is.
//   anything -> generic "couldn't load" banner with a retry (network blip,
//   else      500, expired token). Retrying a 404 is pointless, so only this
//               branch offers it.
async function loadAgent() {
  loading.value = true
  error.value = ''
  notFound.value = false
  try {
    agent.value = await agentsStore.fetchAgent(route.params.name)
  } catch (err) {
    if (err.response?.status === 404) {
      notFound.value = true
    } else {
      error.value = 'Failed to load agent details'
    }
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

// #58 — detect the per-agent Brain Orb capability from template.yaml's
// `capabilities:` list (surfaced by GET /api/agents/{name}/info). Generalizable:
// any agent declaring the `brain-orb` token qualifies — never a hardcoded name.
async function checkBrainOrbCapability() {
  if (!agent.value?.name || agent.value?.status !== 'running') {
    hasBrainOrb.value = false
    return
  }
  try {
    const info = await agentsStore.getAgentInfo(agent.value.name)
    const caps = Array.isArray(info?.capabilities) ? info.capabilities : []
    hasBrainOrb.value = caps.includes('brain-orb')
  } catch {
    hasBrainOrb.value = false
  }
}

// #60: the Brain tab is now an in-page settings panel (BrainPanel) with a launch
// button — no longer an auto-jump to the full-page orb (that was confusing). The
// header brain logo and the panel's "Open Brain Orb" button do the route hop.

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

async function loadTokenStats() {
  if (!agent.value?.name) return
  try {
    tokenStats.value = await agentsStore.getAgentTokenStats(agent.value.name)
  } catch (err) {
    // Non-critical — don't block render
    tokenStats.value = null
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
    tokenStats.value = null
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
    await loadTokenStats()
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
        activeTab.value = 'overview'
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
  // ent#358: a retired session deep-link navigates away — don't spend a mount's
  // worth of requests on a view that is unmounting.
  if (redirectRetiredSessionLink()) return

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
    sessionsStore.loadFeatureFlags(),  // SESSION_TAB_2026-04 Phase 3
    loadTokenStats(),
    ...(agent.value?.status === 'running' ? [checkDashboardExists(), checkBrainOrbCapability()] : [])
  ])
  startEmotionCycling()
  startAllPolling()

  // Handle tab / resume deep-link (from Timeline or ExecutionDetail navigation)
  applyDeepLinkRouting()
})

// onActivated fires when component is shown (after being cached by KeepAlive)
onActivated(async () => {
  // ent#358: same guard as onMounted — AgentDetail is KeepAlive-cached, so a
  // session deep-link opened on an already-visited agent lands here instead.
  if (redirectRetiredSessionLink()) return

  // Restart polling when returning to this view
  startAllPolling()
  // Refresh agent data
  await loadAgent()
  // Reload emotions and restart cycling (AVATAR-002)
  await loadAvailableEmotions()
  startEmotionCycling()
  // Re-check for dashboard + brain-orb capability if agent is running
  if (agent.value?.status === 'running') {
    await checkDashboardExists()
    await checkBrainOrbCapability()
  }

  // EXEC-023 (#1672): re-apply the full tab + resume landing here, not just the
  // tab. onMounted does NOT fire for a KeepAlive-cached instance, so this is the
  // path the common "Continue as Chat" navigation actually takes.
  applyDeepLinkRouting()

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

// Overview tab (#1107): navigate to another tab, or open a specific execution
// in the Tasks tab (deep-link via ?execution= which TasksPanel consumes).
const handleOverviewNavigate = (tabId) => {
  if (visibleTabs.value.some(t => t.id === tabId)) {
    activeTab.value = tabId
  }
}

const handleOpenTask = (executionId) => {
  activeTab.value = 'tasks'
  router.replace({ query: { ...route.query, execution: executionId } })
}

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
