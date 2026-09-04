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
            <svg class="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
            </svg>
            <span class="max-w-32 truncate">{{ currentSessionLabel }}</span>
            <svg class="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
            </svg>
          </button>

          <!-- Dropdown menu -->
          <div
            v-if="showSessionDropdown"
            class="absolute left-0 mt-2 w-72 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg z-20"
          >
            <div class="py-2 max-h-64 overflow-y-auto">
              <div v-if="sessionsLoading" class="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
                Loading sessions...
              </div>
              <div v-else-if="sessions.length === 0" class="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
                No previous sessions
              </div>
              <button
                v-else
                v-for="session in sessions"
                :key="session.id"
                @click="selectSession(session)"
                class="w-full text-left px-4 py-2 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                :class="{ 'bg-action-primary-50 dark:bg-action-primary-900/30': currentSessionId === session.id }"
              >
                <div class="flex items-center justify-between">
                  <span class="text-sm font-medium text-gray-700 dark:text-gray-200">
                    {{ formatSessionDate(session.started_at) }}
                  </span>
                  <span class="text-xs text-gray-400">
                    {{ session.message_count }} msg{{ session.message_count !== 1 ? 's' : '' }}
                  </span>
                </div>
                <p v-if="session.last_message" class="text-xs text-gray-500 dark:text-gray-400 truncate mt-0.5">
                  {{ session.last_message }}
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

        <!-- New Chat button -->
        <button
          @click="startNewChat"
          :disabled="loading"
          class="inline-flex items-center px-3 py-1.5 text-sm font-medium text-action-primary-600 dark:text-action-primary-400 hover:text-action-primary-700 dark:hover:text-action-primary-300 hover:bg-action-primary-50 dark:hover:bg-action-primary-900/30 rounded-lg transition-colors"
        >
          <svg class="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
          </svg>
          New Chat
        </button>
      </div>
    </div>

    <!-- Agent not running message -->
    <div v-if="agentStatus !== 'running'" class="flex-1 flex items-center justify-center">
      <div class="text-center p-8">
        <div class="w-16 h-16 bg-status-warning-100 dark:bg-status-warning-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
          <svg class="w-8 h-8 text-status-warning-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
          </svg>
        </div>
        <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Agent Not Running</h3>
        <p class="text-gray-500 dark:text-gray-400 text-sm">
          Start the agent to begin chatting.
        </p>
      </div>
    </div>

    <!-- Chat interface -->
    <template v-else>
      <!-- Resume mode banner (EXEC-023) -->
      <div
        v-if="isResumeMode"
        class="mx-6 mt-3 px-4 py-3 bg-action-primary-50 dark:bg-action-primary-900/30 border border-action-primary-200 dark:border-action-primary-800 rounded-lg flex items-center justify-between"
      >
        <div class="flex items-center space-x-2">
          <svg class="w-5 h-5 text-action-primary-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span class="text-sm text-action-primary-700 dark:text-action-primary-300">
            Continuing from execution
            <span class="font-mono text-xs bg-action-primary-100 dark:bg-action-primary-800 px-1.5 py-0.5 rounded">
              {{ resumeExecutionIdLocal?.substring(0, 8) }}...
            </span>
            - The agent has full context from that execution.
          </span>
        </div>
        <button
          @click="dismissResumeMode"
          class="text-action-primary-500 hover:text-action-primary-700 dark:hover:text-action-primary-300"
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
          <ChatEmptyState
            :playbooks="playbooks"
            subheading="Pick a quick action below or type your own message. All activity is tracked in the Dashboard timeline."
            @select="onEmptyStateSelect"
          />
        </template>
      </ChatMessages>

      <!-- Error message -->
      <div v-if="error" class="mx-6 mb-2 p-3 rounded-lg" :class="isRateLimitError ? 'bg-state-autonomous-100 dark:bg-state-autonomous-900/30 border border-state-autonomous-200 dark:border-state-autonomous-800' : 'bg-status-danger-100 dark:bg-status-danger-900/30 border border-status-danger-200 dark:border-status-danger-800'">
        <p v-if="isRateLimitError" class="text-sm font-medium text-state-autonomous-700 dark:text-state-autonomous-400 mb-1">Subscription Usage Limit</p>
        <p class="text-sm" :class="isRateLimitError ? 'text-state-autonomous-600 dark:text-state-autonomous-400' : 'text-status-danger-600 dark:text-status-danger-400'">{{ error }}</p>
      </div>

      <!-- Voice overlay (VOICE-004) -->
      <VoiceOverlay
        :voice="voice"
        @end="endVoice"
      />

      <!-- Input area -->
      <div class="px-6 pb-6">
        <ChatInput
          ref="chatInputRef"
          v-model="message"
          :disabled="loading || voice.isActive.value"
          :agent-name="agentName"
          :agent-status="agentStatus"
          :playbooks="playbooks"
          :voice-available="voiceAvailable"
          :voice-active="voice.isActive.value"
          :cancellable="canCancelTurn"
          :cancelling="cancelling"
          @submit="sendMessage"
          @voice="startVoice"
          @cancel="cancelTurn"
        />
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, onMounted, onUnmounted, onActivated, onDeactivated, watch } from 'vue'
import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import { ChatMessages, ChatInput, ChatEmptyState } from './chat'
import { shouldCancelOnEscape, restoreDraft, cancelOutcome, isNoopCancel } from '../utils/turnCancel'
import VoiceOverlay from './chat/VoiceOverlay.vue'
import ModelSelector from './ModelSelector.vue'
import { getStatusFromStreamEvent, MIN_LABEL_DISPLAY_MS, HEARTBEAT_TIMEOUT_MS } from '../utils/execution-status'
import { useVoiceSession } from '../composables/useVoiceSession'

const props = defineProps({
  agentName: {
    type: String,
    required: true
  },
  agentStatus: {
    type: String,
    default: 'stopped'
  },
  // Resume mode props (EXEC-023)
  resumeSessionId: {
    type: String,
    default: null
  },
  resumeExecutionId: {
    type: String,
    default: null
  }
})

const authStore = useAuthStore()

// Voice chat (VOICE-004)
const voice = useVoiceSession(props.agentName)
const voiceAvailable = ref(false)

// Check voice availability
const checkVoiceAvailability = async () => {
  try {
    const response = await axios.get(
      `/api/agents/${props.agentName}/voice/status`,
      { headers: authStore.authHeader }
    )
    voiceAvailable.value = response.data.enabled && response.data.available
  } catch {
    voiceAvailable.value = false
  }
}

const startVoice = () => {
  // ent#438 — canvas tools ON. They used to be reachable only from the retired
  // per-agent workspace page, which is why deleting that page would otherwise
  // have removed the capability rather than moved it. The panel now writes the
  // agent's durable `voice` canvas (operator audience), so what the agent draws
  // while you talk to it is still there tomorrow, on the Canvas tab.
  voice.start(currentSessionId.value, null, true)
}

const endVoice = async () => {
  await voice.stop()
  // Refresh messages to show the saved transcript
  if (currentSessionId.value) {
    try {
      const response = await axios.get(
        `/api/agents/${props.agentName}/chat/sessions/${currentSessionId.value}`,
        { headers: authStore.authHeader }
      )
      messages.value = (response.data.messages || []).map(msg => ({
        role: msg.role,
        content: msg.content,
        timestamp: msg.timestamp,
        source: msg.source || 'text',
      }))
    } catch (err) {
      console.error('Failed to reload messages after voice session:', err)
    }
  }
  // If voice session created a new chat session, refresh sessions list
  if (voice.chatSessionId.value && !currentSessionId.value) {
    currentSessionId.value = voice.chatSessionId.value
  }
  await loadSessions(false)
}

// State
const message = ref('')
const messages = ref([])
const loading = ref(false)
// ent#155 — stopping an in-flight turn.
// `activeExecutionId` is the id the Stop control acts on; `pendingUserMessage`
// is the text to give back. Both are set together at dispatch and cleared
// together at every exit, because a Stop with an id but no text (or the
// reverse) is a control that half-works.
const activeExecutionId = ref(null)
const pendingUserMessage = ref('')
const cancelling = ref(false)
const canCancelTurn = computed(() => loading.value && !!activeExecutionId.value)
const loadingText = ref('Thinking...')
const error = ref(null)
const isRateLimitError = computed(() => {
  if (!error.value) return false
  const lower = error.value.toLowerCase()
  return lower.includes('usage limit') || lower.includes('rate limit') || lower.includes('out of extra usage') || lower.includes('out of usage')
})
const messagesRef = ref(null)
const chatInputRef = ref(null)

// Focus the chat input
const focusChatInput = () => {
  nextTick(() => {
    chatInputRef.value?.focus()
  })
}

// Model selection
const selectedModel = ref(localStorage.getItem('trinity_chat_model') || '')

// Playbooks (for empty-state quick actions)
const playbooks = ref([])
const loadPlaybooks = async () => {
  if (!props.agentName || props.agentStatus !== 'running') return
  try {
    const response = await axios.get(
      `/api/agents/${props.agentName}/playbooks`,
      { headers: authStore.authHeader }
    )
    playbooks.value = response.data.skills || []
  } catch {
    playbooks.value = []
  }
}
const onEmptyStateSelect = ({ text, sendImmediately }) => {
  if (sendImmediately) {
    sendMessage(text)
  } else {
    message.value = text
    nextTick(() => chatInputRef.value?.focus())
  }
}

// SSE state (THINK-001)
let heartbeatTimer = null
let labelTimer = null
let lastLabelTime = 0

// Sessions
const sessions = ref([])
const sessionsLoading = ref(false)
const currentSessionId = ref(null)
const showSessionDropdown = ref(false)
const dropdownRef = ref(null)

// Resume mode state (EXEC-023)
const resumeSessionIdLocal = ref(null)
const resumeExecutionIdLocal = ref(null)
const resumeBannerDismissed = ref(false)  // Separate flag for banner visibility
// Show banner only if in resume mode AND banner not dismissed
const isResumeMode = computed(() => !!resumeSessionIdLocal.value && !resumeBannerDismissed.value)

// Computed
const currentSessionLabel = computed(() => {
  if (!currentSessionId.value) return 'New Conversation'
  const session = sessions.value.find(s => s.id === currentSessionId.value)
  if (session) {
    return formatSessionDate(session.started_at)
  }
  return 'Current Session'
})

// Format session date
const formatSessionDate = (dateStr) => {
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

  return date.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit'
  })
}

// Load sessions
const loadSessions = async (autoSelect = true) => {
  sessionsLoading.value = true
  try {
    const response = await axios.get(`/api/agents/${props.agentName}/chat/sessions`, {
      headers: authStore.authHeader
    })
    sessions.value = response.data.sessions || []

    // If we have sessions and no current session, select the most recent active one
    // BUT only if:
    // - autoSelect is true
    // - we don't already have messages (unsaved new conversation)
    // - resume is NOT active (EXEC-023: don't auto-select when continuing from an
    //   execution — selectSession() would null resumeSessionIdLocal and the next
    //   message would drop --resume). Gate on resumeSessionIdLocal, NOT isResumeMode:
    //   isResumeMode also goes false when the user merely DISMISSES the banner, which
    //   deliberately keeps resume active (#1672) — testing it here silently lost the
    //   execution context on the first auto-select after a dismiss.
    if (autoSelect && sessions.value.length > 0 && !currentSessionId.value && messages.value.length === 0 && !resumeSessionIdLocal.value) {
      const activeSession = sessions.value.find(s => s.status === 'active')
      if (activeSession) {
        await selectSession(activeSession, false)
      }
    }
  } catch (err) {
    console.error('Failed to load sessions:', err)
  } finally {
    sessionsLoading.value = false
  }
}

// Select a session and load its messages
const selectSession = async (session, closeDropdown = true) => {
  if (closeDropdown) {
    showSessionDropdown.value = false
  }

  if (currentSessionId.value === session.id) return

  // Clear resume mode when selecting a different session
  // (user is explicitly choosing a different conversation context)
  resumeSessionIdLocal.value = null
  resumeExecutionIdLocal.value = null
  resumeBannerDismissed.value = false

  currentSessionId.value = session.id
  loading.value = true
  error.value = null

  try {
    const response = await axios.get(`/api/agents/${props.agentName}/chat/sessions/${session.id}`, {
      headers: authStore.authHeader
    })

    // Load messages from session (already ordered ASC by backend)
    messages.value = (response.data.messages || []).map(msg => ({
      role: msg.role,
      content: msg.content,
      timestamp: msg.timestamp,
      source: msg.source || 'text',
    }))
  } catch (err) {
    console.error('Failed to load session:', err)
    error.value = 'Failed to load conversation history'
  } finally {
    loading.value = false
    focusChatInput()
  }
}

// Start a new chat
const startNewChat = () => {
  currentSessionId.value = null
  messages.value = []
  error.value = null
  showSessionDropdown.value = false
  // Clear resume mode when starting new chat
  resumeSessionIdLocal.value = null
  resumeExecutionIdLocal.value = null
  resumeBannerDismissed.value = false
  // Close any active SSE connection
  closeSSE()
  // Focus input
  focusChatInput()
}

// Dismiss resume mode banner (EXEC-023)
// Note: This only hides the banner, but keeps resumeSessionIdLocal so
// subsequent messages continue using --resume for context continuity
const dismissResumeMode = () => {
  resumeBannerDismissed.value = true
}

// Build context from conversation history
const buildContextPrompt = (userMessage) => {
  if (messages.value.length === 0) {
    return userMessage
  }

  // Build conversation context (last 10 exchanges)
  const recentMessages = messages.value.slice(-20) // Last 20 messages (10 exchanges)
  let context = '### Previous conversation:\n\n'

  for (const msg of recentMessages) {
    const role = msg.role === 'user' ? 'User' : 'Assistant'
    context += `${role}: ${msg.content}\n\n`
  }

  context += `### Current message:\n\nUser: ${userMessage}`

  return context
}

// THINK-001: Update loading text with minimum display time to prevent flicker
const updateLoadingText = (newText) => {
  if (!newText) return

  const now = Date.now()
  const elapsed = now - lastLabelTime

  if (elapsed < MIN_LABEL_DISPLAY_MS) {
    // Schedule the update after remaining time
    clearTimeout(labelTimer)
    labelTimer = setTimeout(() => {
      loadingText.value = newText
      lastLabelTime = Date.now()
    }, MIN_LABEL_DISPLAY_MS - elapsed)
  } else {
    loadingText.value = newText
    lastLabelTime = now
  }

  // Reset heartbeat timer
  resetHeartbeat()
}

// THINK-001: Reset heartbeat timer
const resetHeartbeat = () => {
  clearTimeout(heartbeatTimer)
  heartbeatTimer = setTimeout(() => {
    if (loading.value) {
      loadingText.value = 'Working...'
    }
  }, HEARTBEAT_TIMEOUT_MS)
}

// THINK-001: Close SSE connection and cleanup timers
let streamReader = null

const closeSSE = () => {
  if (streamReader) {
    streamReader.cancel().catch(() => {})
    streamReader = null
  }
  clearTimeout(heartbeatTimer)
  clearTimeout(labelTimer)
  heartbeatTimer = null
  labelTimer = null
}

// THINK-001: Subscribe to execution SSE stream for status updates
const subscribeToStream = (executionId) => {
  closeSSE() // Clean up any existing connection
  lastLabelTime = 0
  resetHeartbeat()

  // Use fetch with ReadableStream (EventSource doesn't support custom headers)
  const url = `/api/agents/${props.agentName}/executions/${executionId}/stream`

  fetch(url, {
    headers: {
      'Authorization': `Bearer ${authStore.token}`,
      'Accept': 'text/event-stream'
    }
  }).then(response => {
    if (!response.ok) return

    const reader = response.body.getReader()
    streamReader = reader
    const decoder = new TextDecoder()
    let buffer = ''

    function processStream() {
      reader.read().then(({ done, value }) => {
        if (done) {
          closeSSE()
          return
        }

        buffer += decoder.decode(value, { stream: true })

        // Process complete SSE messages
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6))

              if (data.type === 'stream_end') {
                closeSSE()
                return
              }

              if (data.type === 'error') {
                console.warn('SSE stream error:', data.message)
                continue
              }

              // Map event to status label
              const status = getStatusFromStreamEvent(data)
              if (status) {
                updateLoadingText(status)
              }
            } catch (e) {
              // Ignore parse errors for comments/keepalives
            }
          }
        }

        processStream()
      }).catch(() => {
        closeSSE()
      })
    }

    processStream()
  }).catch(() => {
    // Stream failed - polling will handle completion
    closeSSE()
  })
}

// THINK-001: Poll execution status until complete
const pollExecution = async (executionId) => {
  const maxAttempts = 360 // 30 minutes at 5s intervals
  let attempts = 0

  while (attempts < maxAttempts && loading.value) {
    attempts++
    await new Promise(resolve => setTimeout(resolve, 5000))

    try {
      const response = await axios.get(
        `/api/agents/${props.agentName}/executions/${executionId}`,
        { headers: authStore.authHeader }
      )

      const execution = response.data
      if (execution.status === 'success' || execution.status === 'failed' || execution.status === 'cancelled') {
        return execution
      }
    } catch (err) {
      console.error('Poll error:', err)
    }
  }

  return null
}

// Send message
const sendMessage = async (userMessage, files = []) => {
  if ((!userMessage && files.length === 0) || loading.value || props.agentStatus !== 'running') return

  error.value = null

  // Add user message to chat immediately
  messages.value.push({
    role: 'user',
    content: userMessage,
    timestamp: new Date().toISOString()
  })

  // Clear input
  message.value = ''

  loading.value = true
  loadingText.value = 'Thinking...'

  try {
    // Build context with conversation history
    const contextPrompt = buildContextPrompt(userMessage)

    // Build request payload - use async_mode for SSE streaming (THINK-001)
    const payload = {
      message: contextPrompt,
      save_to_session: true,
      user_message: userMessage,
      create_new_session: !currentSessionId.value,
      chat_session_id: currentSessionId.value || undefined,
      async_mode: true,
      model: selectedModel.value || undefined,
      files: files.length > 0 ? files : undefined,
    }

    // EXEC-023: Include resume_session_id for ALL messages in resume mode
    // The /task endpoint is stateless - it doesn't use --continue.
    // We must keep passing resume_session_id so Claude Code uses --resume for every message.
    if (resumeSessionIdLocal.value) {
      payload.resume_session_id = resumeSessionIdLocal.value
      // Note: We intentionally do NOT clear resumeSessionIdLocal here.
      // All messages in a resumed session need --resume to maintain context.
    }

    // POST with async_mode=true returns immediately with execution_id
    const submitResponse = await axios.post(`/api/agents/${props.agentName}/task`, payload, {
      headers: authStore.authHeader
    })

    const executionId = submitResponse.data.execution_id
    if (!executionId) {
      throw new Error('No execution_id returned from async task submission')
    }

    // ent#155: the Stop control can only act once we have an id, and the words
    // can only be restored if we kept them. Set together, cleared together.
    activeExecutionId.value = executionId
    pendingUserMessage.value = userMessage

    // Subscribe to SSE stream for real-time status updates
    subscribeToStream(executionId)

    // Poll for completion (SSE handles status labels, polling handles result)
    const execution = await pollExecution(executionId)

    closeSSE()

    if (execution) {
      if (execution.status === 'success' && execution.response) {
        messages.value.push({
          role: 'assistant',
          content: execution.response,
          timestamp: new Date().toISOString()
        })
      } else if (execution.status === 'failed') {
        error.value = execution.error || 'Task execution failed'
      } else if (execution.status === 'cancelled') {
        error.value = 'Task was cancelled'
      }

      // Update session ID from the execution
      // The background task saves to chat_sessions and broadcasts chat_session_id
      // We need to refresh sessions to pick up the new session
      await loadSessions(false)

      // If we didn't have a session before, pick up the one just created
      if (!currentSessionId.value && sessions.value.length > 0) {
        const latestSession = sessions.value.find(s => s.status === 'active')
        if (latestSession) {
          currentSessionId.value = latestSession.id
        }
      }
    } else {
      error.value = 'Request timed out. Please try again.'
    }
  } catch (err) {
    console.error('Chat error:', err)
    closeSSE()
    error.value = err.response?.data?.detail || 'Failed to send message. Please try again.'
    // Remove the user message if send failed
    messages.value.pop()
  } finally {
    loading.value = false
    loadingText.value = 'Thinking...'
    closeSSE()
    // Cleared on EVERY exit, including the cancel path — a stale id would let
    // a later Escape terminate an execution that already ended, and a stale
    // pending message would be restored over a draft the user has moved on to.
    activeExecutionId.value = null
    pendingUserMessage.value = ''
    cancelling.value = false
  }
}

// ent#155: stop the turn, give the words back.
const cancelTurn = async () => {
  const executionId = activeExecutionId.value
  if (!executionId || cancelling.value) return
  cancelling.value = true
  const restoreText = pendingUserMessage.value
  try {
    const res = await axios.post(
      `/api/agents/${props.agentName}/executions/${executionId}/terminate?task_execution_id=${executionId}`,
      {},
      { headers: authStore.authHeader }
    )
    const alreadyTerminal = isNoopCancel(res.data?.status)
    const outcome = cancelOutcome({ ok: true, alreadyTerminal })
    if (outcome.kind === 'cancelled') {
      // The poll sees the CANCELLED terminal and renders it; the words come
      // back here so the user can edit and resend. Prepended, never replacing
      // a draft typed while waiting.
      message.value = restoreDraft(restoreText, message.value)
    }
  } catch (err) {
    // Review finding: this route has no DB pre-check, so a turn that finished
    // between the click and the terminate reaches the agent, which answers 404
    // ("Execution not found in agent") — and reporting "it's still running"
    // there is exactly backwards. A 404 IS the lost race; say nothing.
    if (err?.response?.status === 404) {
      cancelling.value = false
      return
    }
    // Anything else: the turn is still running and still spending — say so, and
    // leave the input alone. Restoring the text here would imply a stop that
    // did not happen (AC: honest status).
    error.value = cancelOutcome({ ok: false, alreadyTerminal: false }).message
    cancelling.value = false
  }
}

// Escape stops the turn — and does nothing at all otherwise. The overlay list
// is what keeps it from hijacking the Escape that closes a picker or exits the
// voice overlay; the rule itself lives in `utils/turnCancel.js` so it can be
// tested (vitest has no mount harness).
const onEscapeKeydown = (event) => {
  if (!shouldCancelOnEscape(event, {
    inFlight: canCancelTurn.value,
    cancelling: cancelling.value,
    overlays: [voice.isActive.value, showSessionDropdown.value],
  })) return
  event.preventDefault()
  cancelTurn()
}

// Click outside to close dropdown
const handleClickOutside = (event) => {
  if (dropdownRef.value && !dropdownRef.value.contains(event.target)) {
    showSessionDropdown.value = false
  }
}

// Persist model selection
watch(selectedModel, (val) => {
  if (val) {
    localStorage.setItem('trinity_chat_model', val)
  } else {
    localStorage.removeItem('trinity_chat_model')
  }
})

// Watch for agent becoming available
watch(() => props.agentStatus, (newStatus) => {
  if (newStatus === 'running') {
    loadSessions()
    checkVoiceAvailability()
    loadPlaybooks()
  }
})

// Watch for resume mode props (EXEC-023)
watch(() => props.resumeSessionId, (newSessionId) => {
  if (newSessionId) {
    // Start fresh for resume - don't load existing session
    messages.value = []
    currentSessionId.value = null
    error.value = null
    resumeSessionIdLocal.value = newSessionId
    resumeExecutionIdLocal.value = props.resumeExecutionId
    resumeBannerDismissed.value = false  // Show banner when entering resume mode
  }
}, { immediate: true })

// Watch for agent name change
watch(() => props.agentName, () => {
  messages.value = []
  currentSessionId.value = null
  error.value = null
  closeSSE()
  if (props.agentStatus === 'running') {
    loadSessions()
    loadPlaybooks()
  }
})

// Initialize
// Review finding: `App.vue` wraps the router view in
// `<KeepAlive :include="['AgentDetail']">`, so `onUnmounted` does NOT fire when
// the user navigates away — only `onDeactivated` does. Left on `onMounted`
// alone, the document listener outlived the page while a turn was still in
// flight (the send closure outlives the navigation, so `loading` and
// `activeExecutionId` stay set), and an Escape pressed on the Dashboard
// silently cancelled a turn from a page showing no chat at all.
function bindEscape() { document.addEventListener('keydown', onEscapeKeydown) }
function unbindEscape() { document.removeEventListener('keydown', onEscapeKeydown) }

onActivated(bindEscape)
onDeactivated(unbindEscape)

onMounted(() => {
  document.addEventListener('click', handleClickOutside)
  bindEscape()
  if (props.agentStatus === 'running') {
    loadSessions()
    checkVoiceAvailability()
    loadPlaybooks()
  }
})

onUnmounted(() => {
  document.removeEventListener('click', handleClickOutside)
  unbindEscape()
  closeSSE()
  // End voice session on unmount
  if (voice.isActive.value) {
    voice.stop()
  }
})
</script>
