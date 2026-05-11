<template>
  <div class="flex flex-col h-full relative">
    <!-- Header with session selector -->
    <div class="flex items-center justify-between px-6 py-3 border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50">
      <div class="flex items-center space-x-3">
        <!-- Session selector dropdown -->
        <div class="relative" ref="dropdownRef">
          <button
            @click="showSessionDropdown = !showSessionDropdown"
            class="flex items-center space-x-2 px-3 py-1.5 bg-white dark:bg-gray-700 border border-gray-300 dark:border-gray-600 rounded-lg text-sm text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-600 transition-colors"
          >
            <svg class="w-4 h-4 text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <span class="max-w-32 truncate">{{ currentSessionLabel }}</span>
            <svg class="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
            </svg>
          </button>

          <!-- Dropdown menu -->
          <div
            v-if="showSessionDropdown"
            class="absolute left-0 mt-2 w-80 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg z-20"
          >
            <div class="py-2 max-h-72 overflow-y-auto">
              <div v-if="sessionsLoading" class="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
                Loading sessions...
              </div>
              <div v-else-if="sessions.length === 0" class="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
                No sessions yet — start one with “+ New Session”.
              </div>
              <button
                v-else
                v-for="session in sessions"
                :key="session.id"
                @click="selectSession(session)"
                class="w-full text-left px-4 py-2 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                :class="{ 'bg-indigo-50 dark:bg-indigo-900/30': currentSessionId === session.id }"
              >
                <div class="flex items-center justify-between">
                  <span class="text-sm font-medium text-gray-700 dark:text-gray-200">
                    {{ formatSessionDate(session.last_message_at || session.started_at) }}
                  </span>
                  <span class="text-xs text-gray-400">
                    {{ session.message_count }} turn{{ session.message_count !== 1 ? 's' : '' }}
                  </span>
                </div>
                <!-- Per-session subtitle (Phase 3.5) -->
                <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5 flex items-center gap-2">
                  <span>{{ formatContextPercent(session) }}</span>
                  <span v-if="session.cached_claude_session_id" class="text-emerald-500" title="Has working memory">●</span>
                  <span v-else class="text-gray-300 dark:text-gray-600" title="Cold (memory cleared)">●</span>
                  <span v-if="session.consecutive_resume_failures > 0" class="text-amber-500">
                    {{ session.consecutive_resume_failures }} resume failure(s)
                  </span>
                </p>
              </button>
            </div>
          </div>
        </div>
      </div>

      <div class="flex items-center space-x-2">
        <!-- Model selector -->
        <div class="w-44">
          <ModelSelector v-model="selectedModel" compact placeholder="Default model" />
        </div>

        <!-- Reset memory button (Phase 3.4) — only when an active session exists -->
        <div v-if="currentSessionId" class="flex flex-col items-end">
          <button
            @click="confirmReset"
            :disabled="loading"
            class="inline-flex items-center px-3 py-1.5 text-sm font-medium text-amber-600 dark:text-amber-400 hover:text-amber-700 dark:hover:text-amber-300 hover:bg-amber-50 dark:hover:bg-amber-900/30 rounded-lg transition-colors"
            title="Clear working memory (keeps message history)"
          >
            <svg class="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
            Reset memory
          </button>
          <p
            v-if="showCompactHint"
            class="text-xs italic text-gray-500 dark:text-gray-400 mt-1 max-w-xs text-right"
          >
            Compacted {{ currentSession.compact_count }} times — consider starting fresh for sharper responses.
          </p>
        </div>

        <!-- New Session button -->
        <button
          @click="startNewSession"
          :disabled="loading || creatingSession"
          class="inline-flex items-center px-3 py-1.5 text-sm font-medium text-indigo-600 dark:text-indigo-400 hover:text-indigo-700 dark:hover:text-indigo-300 hover:bg-indigo-50 dark:hover:bg-indigo-900/30 rounded-lg transition-colors"
        >
          <svg class="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
          </svg>
          New Session
        </button>
      </div>
    </div>

    <!-- Agent not running message -->
    <div v-if="agentStatus !== 'running'" class="flex-1 flex items-center justify-center">
      <div class="text-center p-8">
        <div class="w-16 h-16 bg-yellow-100 dark:bg-yellow-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
          <svg class="w-8 h-8 text-yellow-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
          </svg>
        </div>
        <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Agent Not Running</h3>
        <p class="text-gray-500 dark:text-gray-400 text-sm">
          Start the agent to begin a session.
        </p>
      </div>
    </div>

    <!-- Session interface -->
    <template v-else>
      <!-- Fallback notice (E2/E3): the resume failed and we recovered -->
      <div
        v-if="fallbackNotice"
        class="mx-6 mt-3 px-4 py-3 bg-amber-50 dark:bg-amber-900/30 border border-amber-200 dark:border-amber-800 rounded-lg flex items-center justify-between"
      >
        <div class="flex items-center space-x-2">
          <svg class="w-5 h-5 text-amber-500 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M5.07 19h13.86c1.54 0 2.5-1.67 1.73-3L13.73 4a2 2 0 00-3.46 0L3.34 16c-.77 1.33.19 3 1.73 3z" />
          </svg>
          <span class="text-sm text-amber-700 dark:text-amber-300">
            This session’s working memory expired. Starting fresh — past messages are still visible.
          </span>
        </div>
        <button
          @click="dismissFallbackNotice"
          class="text-amber-500 hover:text-amber-700 dark:hover:text-amber-300"
          title="Dismiss"
        >
          <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>

      <!-- Messages area -->
      <ChatMessages
        ref="messagesRef"
        :messages="messages"
        :loading="loading"
        :loading-text="loadingText"
        class="flex-1 px-6"
      >
        <template #empty>
          <div class="text-center py-12">
            <div class="w-16 h-16 bg-indigo-100 dark:bg-indigo-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
              <svg class="w-8 h-8 text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.86 9.86 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
              </svg>
            </div>
            <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">
              {{ currentSessionId ? 'Session ready' : 'Start a session' }}
            </h3>
            <p class="text-gray-500 dark:text-gray-400 text-sm max-w-md mx-auto">
              Conversation, tool results, and reasoning state persist between turns.
              Each new message reattaches to the same Claude memory.
            </p>
          </div>
        </template>
      </ChatMessages>

      <!-- Error message -->
      <div v-if="error" class="mx-6 mb-2 p-3 rounded-lg bg-red-100 dark:bg-red-900/30 border border-red-200 dark:border-red-800">
        <p class="text-sm text-red-600 dark:text-red-400">{{ error }}</p>
      </div>

      <!-- Input area -->
      <div class="px-6 pb-6">
        <ChatInput
          ref="chatInputRef"
          v-model="message"
          :disabled="loading"
          :agent-name="agentName"
          :agent-status="agentStatus"
          :voice-available="false"
          :voice-active="false"
          placeholder="Send a message — your conversation memory will persist between turns."
          @submit="onSubmit"
        />
      </div>
    </template>

    <!-- Reset confirm modal (Phase 3.4) -->
    <div
      v-if="showResetModal"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      @click.self="showResetModal = false"
    >
      <div class="bg-white dark:bg-gray-800 rounded-lg shadow-xl max-w-md w-full p-6">
        <h3 class="text-lg font-semibold text-gray-900 dark:text-white mb-2">Reset session memory?</h3>
        <p class="text-sm text-gray-600 dark:text-gray-400 mb-4">
          The agent will forget the working state of this session — but the message history
          stays visible. Useful when you want to start a clean line of thought. Your next
          message becomes a cold turn under a new Claude session.
        </p>
        <div class="flex justify-end space-x-2">
          <button
            @click="showResetModal = false"
            class="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg"
          >
            Cancel
          </button>
          <button
            @click="performReset"
            class="px-4 py-2 text-sm font-medium text-white bg-amber-600 hover:bg-amber-700 rounded-lg"
          >
            Reset memory
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, onMounted, onUnmounted, onActivated, onDeactivated, watch } from 'vue'
import { ChatMessages, ChatInput } from './chat'
import ModelSelector from './ModelSelector.vue'
import { useSessionsStore } from '../stores/sessions'

const props = defineProps({
  agentName: {
    type: String,
    required: true,
  },
  agentStatus: {
    type: String,
    default: 'stopped',
  },
})

const sessionsStore = useSessionsStore()

// -- Local UI state ----------------------------------------------------------
// AgentDetail is wrapped in <KeepAlive>, so this SessionPanel instance is
// reused across all `/agents/*` routes. Local refs survive deactivation
// and would otherwise bleed across agents. Turn-specific state (loading,
// error, fallback notice, in-flight tracking) lives in the Pinia store
// keyed by sessionId. The local refs below are intentionally UI-only:
// either ephemera that's safe to reset on agent change, or user prefs
// (selectedModel) that should persist.
const message = ref('')
const localLoading = ref(false)        // non-turn loading: select/create/reset
const loadingText = ref('Thinking...')
const localError = ref(null)           // non-turn errors: load/create/reset
const sessionsLoading = ref(false)
const showSessionDropdown = ref(false)
const showResetModal = ref(false)
const creatingSession = ref(false)
const dropdownRef = ref(null)
const messagesRef = ref(null)
const chatInputRef = ref(null)
const selectedModel = ref(localStorage.getItem('trinity_session_model') || '')

// -- Derived from store -----------------------------------------------------
const sessions = computed(() => sessionsStore.sessionsFor(props.agentName))
const currentSessionId = computed(() => sessionsStore.activeSessionId(props.agentName))
const messages = computed(() => {
  if (!currentSessionId.value) return []
  return (sessionsStore.messagesFor(currentSessionId.value) || []).map((m) => ({
    role: m.role,
    content: m.content,
    timestamp: m.timestamp,
    source: 'text',
  }))
})

// -- Turn state from store, scoped by current sessionId ---------------------
// These computed wrappers read store state keyed by `currentSessionId`.
// Switching agents/sessions automatically follows the active session id,
// so the template never sees stale state from another session.
const turnInFlight = computed(() =>
  currentSessionId.value ? sessionsStore.isInFlight(currentSessionId.value) : false,
)
const turnError = computed(() =>
  currentSessionId.value ? sessionsStore.errorForSession(currentSessionId.value) : null,
)
const fallbackNotice = computed(() =>
  currentSessionId.value ? sessionsStore.fallbackNoticeForSession(currentSessionId.value) : false,
)

// Unified `loading` for the template: turn in flight OR any UI loading.
const loading = computed(
  () => turnInFlight.value || localLoading.value || creatingSession.value || sessionsLoading.value,
)
// Unified `error` for the template: turn error takes precedence (it's
// what the user just acted on); fall back to non-turn errors.
const error = computed(() => turnError.value || localError.value)

const currentSessionLabel = computed(() => {
  if (!currentSessionId.value) return 'No session'
  const s = sessions.value.find((x) => x.id === currentSessionId.value)
  if (!s) return 'Current session'
  return formatSessionDate(s.last_message_at || s.started_at)
})

const currentSession = computed(() => {
  if (!currentSessionId.value) return null
  return sessions.value.find((x) => x.id === currentSessionId.value) || null
})

// Inline reset-memory hint threshold. Claude Code auto-compacts at ~85% of
// the model window; each compact compresses ~170k of history into ~10k of
// summary, and stacked compacts degrade response quality. Surfacing the
// "consider starting fresh" suggestion at >5 keeps the hint quiet for
// normal use and visible only when fidelity is genuinely at risk.
const COMPACT_HINT_THRESHOLD = 5
const showCompactHint = computed(() => {
  return (currentSession.value?.compact_count ?? 0) > COMPACT_HINT_THRESHOLD
})

function formatSessionDate(dateStr) {
  if (!dateStr) return 'Unknown'
  const date = new Date(dateStr)
  const now = new Date()
  const diffMs = now - date
  const diffMins = Math.floor(diffMs / 60000)
  const diffHours = Math.floor(diffMs / 3600000)
  const diffDays = Math.floor(diffMs / 86400000)
  if (diffMins < 1) return 'Just now'
  if (diffMins < 60) return `${diffMins}m ago`
  if (diffHours < 24) return `${diffHours}h ago`
  if (diffDays < 7) return `${diffDays}d ago`
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

// Per-session subtitle. Shows the size of the *last* assistant turn's
// cache (cache_read + cache_creation) as a fraction of the model
// window. NOT a session-wide watermark — Claude Code auto-compacts at
// ~85% so a watermark would asymptote near the compact threshold and
// stop conveying anything useful.
function formatContextPercent(session) {
  const used = session.total_context_used || 0
  const max = session.total_context_max || 200000
  if (!used || !max) return 'last cache: —'
  const pct = Math.min(100, Math.round((used / max) * 100))
  return `${pct}% last cache`
}

const focusInput = () => {
  nextTick(() => chatInputRef.value?.focus?.())
}

// ----- session list / selection ---------------------------------------------

async function loadSessions(autoSelect = true) {
  sessionsLoading.value = true
  try {
    await sessionsStore.listSessions(props.agentName)
    // Pick the most recent session if nothing selected.
    if (autoSelect && !currentSessionId.value && sessions.value.length > 0) {
      await selectSession(sessions.value[0], false)
    }
  } catch (e) {
    localError.value = 'Failed to load sessions'
  } finally {
    sessionsLoading.value = false
  }
}

async function selectSession(session, closeDropdown = true) {
  if (closeDropdown) showSessionDropdown.value = false
  if (currentSessionId.value === session.id) return
  sessionsStore.selectSession(props.agentName, session.id)
  localLoading.value = true
  localError.value = null
  try {
    await sessionsStore.loadSession(props.agentName, session.id)
  } catch {
    localError.value = 'Failed to load session messages'
  } finally {
    localLoading.value = false
    focusInput()
  }
}

async function startNewSession() {
  if (creatingSession.value) return
  creatingSession.value = true
  showSessionDropdown.value = false
  localError.value = null
  try {
    await sessionsStore.createSession(props.agentName)
    focusInput()
  } catch (e) {
    localError.value = e.response?.data?.detail || 'Failed to create session'
  } finally {
    creatingSession.value = false
  }
}

function dismissFallbackNotice() {
  const sid = currentSessionId.value
  if (!sid) return
  // Store owns fallback notice state per session; clear just that flag
  // without touching in-flight/error state.
  sessionsStore.fallbackNoticeBySession[sid] = false
}

// ----- reset / delete --------------------------------------------------------

function confirmReset() {
  if (!currentSessionId.value) return
  showResetModal.value = true
}

async function performReset() {
  showResetModal.value = false
  const sid = currentSessionId.value
  if (!sid) return
  try {
    await sessionsStore.resetSession(props.agentName, sid)
    // Clear any stale turn state (error, fallback notice, polling) on
    // the now-reset session. Store-side cleanup is idempotent.
    sessionsStore.clearSessionTurnState(sid)
  } catch (e) {
    localError.value = e.response?.data?.detail || 'Failed to reset memory'
  }
}

// ----- the turn --------------------------------------------------------------

async function onSubmit(text, files = []) {
  if ((!text && (!files || files.length === 0)) || loading.value || props.agentStatus !== 'running') return

  // Lazy session creation — first turn from the empty state creates a row.
  if (!currentSessionId.value) {
    try {
      await sessionsStore.createSession(props.agentName)
    } catch (e) {
      localError.value = e.response?.data?.detail || 'Failed to create session'
      return
    }
  }

  message.value = ''
  loadingText.value = 'Thinking...'
  const sid = currentSessionId.value

  try {
    // sendMessage manages inFlight + error + fallbackNotice on the store,
    // keyed by sessionId. The template's computed `loading`/`error`/
    // `fallbackNotice` follow automatically.
    await sessionsStore.sendMessage(props.agentName, sid, text, {
      model: selectedModel.value || undefined,
      files: files && files.length > 0 ? files : undefined,
    })
    // Refresh the session list so the dropdown reflects new turn count + last_message_at.
    await sessionsStore.listSessions(props.agentName)
  } catch {
    // Store has already populated errorBySession[sid] with a friendly message.
  } finally {
    loadingText.value = 'Thinking...'
  }
}

// ----- click-outside dropdown ------------------------------------------------

function handleClickOutside(event) {
  if (dropdownRef.value && !dropdownRef.value.contains(event.target)) {
    showSessionDropdown.value = false
  }
}

// ----- model persistence -----------------------------------------------------

watch(selectedModel, (val) => {
  if (val) localStorage.setItem('trinity_session_model', val)
  else localStorage.removeItem('trinity_session_model')
})

// ----- agent prop changes ----------------------------------------------------

watch(
  () => [props.agentName, props.agentStatus],
  ([newName, status], oldVals) => {
    if (status === 'running') {
      loadSessions()
    }
    const oldName = Array.isArray(oldVals) ? oldVals[0] : null
    if (newName !== oldName) {
      // SessionPanel instance is shared across agents via <KeepAlive>.
      // Reset local UI ephemera so it doesn't bleed across agents (#759).
      // Per-session turn state lives in the Pinia store keyed by sessionId
      // and naturally scopes — but local refs survive deactivation.
      message.value = ''
      showSessionDropdown.value = false
      showResetModal.value = false
      localError.value = null
      localLoading.value = false
      creatingSession.value = false
      sessionsLoading.value = false
    }
  },
)

// ----- KeepAlive lifecycle (Issue #759) -------------------------------------

// onActivated fires when SessionPanel is reactivated after a KeepAlive
// deactivation — i.e., the user navigated to another route and came back.
// If a turn is still running on the backend (visible via `turn_in_progress`
// on the latest loadSession response), start polling so the UI eventually
// catches up. Backend persists user + assistant rows itself; we just need
// to re-fetch them.
onActivated(async () => {
  const sid = currentSessionId.value
  if (!sid || props.agentStatus !== 'running') return
  try {
    const data = await sessionsStore.loadSession(props.agentName, sid)
    if (data?.session?.turn_in_progress) {
      sessionsStore.startPolling(props.agentName, sid)
    }
  } catch {
    // Re-sync failure is non-fatal — the next user action will retry.
  }
})

// Stop polling while the panel isn't visible. We're using ref-counted
// start/stop so this won't kill a concurrent watcher (today there's only
// ever one instance — but the contract is honoured).
onDeactivated(() => {
  const sid = currentSessionId.value
  if (sid) sessionsStore.stopPolling(sid)
})

onMounted(() => {
  document.addEventListener('click', handleClickOutside)
  if (props.agentStatus === 'running') loadSessions()
})

onUnmounted(() => {
  document.removeEventListener('click', handleClickOutside)
  // Best-effort cleanup if the panel ever fully unmounts (KeepAlive cache
  // eviction or a future refactor). Stops the timer for the current sid.
  const sid = currentSessionId.value
  if (sid) sessionsStore.stopPolling(sid)
})
</script>
