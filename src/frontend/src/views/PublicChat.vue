<template>
  <div class="min-h-screen bg-gray-50 dark:bg-gray-900 flex flex-col">
    <!-- Header with Trinity branding and agent metadata -->
    <header class="bg-white dark:bg-gray-800 shadow-sm py-3 px-4">
      <div class="max-w-3xl mx-auto">
        <!-- Trinity branding row -->
        <div class="flex items-center justify-between mb-2">
          <div class="flex items-center">
            <img src="../assets/trinity-logo.svg" alt="Trinity" class="h-6 w-6 mr-2 dark:hidden" />
            <img src="../assets/trinity-logo-white.svg" alt="Trinity" class="h-6 w-6 mr-2 hidden dark:block" />
            <span class="text-lg font-bold text-gray-900 dark:text-white">Trinity</span>
          </div>
          <!-- Header action buttons -->
          <div class="flex items-center gap-1">
            <!-- Chat history dropdown (logged-in users only) -->
            <ChatHistoryDropdown
              v-if="authStore.isAuthenticated && linkInfo?.valid"
              :token="token"
              @session-selected="handleHistorySessionSelected"
            />
            <!-- New Conversation button -->
            <button
              v-if="messages.length > 0 && isVerified && linkInfo?.valid"
              @click="confirmNewConversation"
              class="inline-flex items-center px-2 py-1 text-xs font-medium text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white hover:bg-gray-100 dark:hover:bg-gray-700 rounded transition-colors"
              :disabled="chatLoading"
            >
              <svg class="w-3.5 h-3.5 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
              New
            </button>
          </div>
        </div>

        <!-- Agent info row -->
        <div v-if="linkInfo && linkInfo.valid" class="space-y-1">
          <div class="flex items-center justify-between">
            <div class="flex items-center space-x-2">
              <div class="w-2 h-2 rounded-full bg-status-success-500 animate-pulse"></div>
              <span class="font-semibold text-gray-900 dark:text-white">
                {{ linkInfo.agent_display_name || 'Agent' }}
              </span>
            </div>
            <!-- Status badges -->
            <div class="flex items-center space-x-2">
              <span
                v-if="linkInfo.is_autonomous"
                class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400"
              >
                AUTO
              </span>
              <span
                v-if="linkInfo.is_read_only"
                class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-rose-100 text-rose-800 dark:bg-rose-900/30 dark:text-rose-400"
              >
                <svg class="w-3 h-3 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
                </svg>
                READ-ONLY
              </span>
            </div>
          </div>
          <!-- Description row -->
          <p
            v-if="linkInfo.agent_description"
            class="text-sm text-gray-500 dark:text-gray-400 line-clamp-2"
          >
            {{ linkInfo.agent_description }}
          </p>
        </div>
        <div v-else class="text-sm text-gray-500 dark:text-gray-400">
          Loading...
        </div>
      </div>
    </header>

    <!-- Main content -->
    <main class="flex-1 flex flex-col max-w-3xl mx-auto w-full p-4">
      <!-- Loading state -->
      <div v-if="loading" class="flex-1 flex items-center justify-center">
        <div class="text-center">
          <div class="animate-spin rounded-full h-10 w-10 border-b-2 border-indigo-500 mx-auto mb-4"></div>
          <p class="text-gray-500 dark:text-gray-400">Loading...</p>
        </div>
      </div>

      <!-- Invalid link state -->
      <div v-else-if="!linkInfo || !linkInfo.valid" class="flex-1 flex items-center justify-center">
        <div class="text-center bg-white dark:bg-gray-800 rounded-xl shadow-lg p-8 max-w-md">
          <div class="w-16 h-16 bg-status-danger-100 dark:bg-status-danger-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
            <svg class="w-8 h-8 text-status-danger-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
            </svg>
          </div>
          <h2 class="text-xl font-semibold text-gray-900 dark:text-white mb-2">Link Not Available</h2>
          <p class="text-gray-500 dark:text-gray-400">
            {{ linkError || 'This link is no longer valid or has expired.' }}
          </p>
        </div>
      </div>

      <!-- Agent not available -->
      <div v-else-if="!linkInfo.agent_available" class="flex-1 flex items-center justify-center">
        <div class="text-center bg-white dark:bg-gray-800 rounded-xl shadow-lg p-8 max-w-md">
          <div class="w-16 h-16 bg-status-warning-100 dark:bg-status-warning-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
            <svg class="w-8 h-8 text-status-warning-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          </div>
          <h2 class="text-xl font-semibold text-gray-900 dark:text-white mb-2">Agent Unavailable</h2>
          <p class="text-gray-500 dark:text-gray-400">
            The agent is currently offline. Please try again later.
          </p>
        </div>
      </div>

      <!-- Email verification required -->
      <div v-else-if="linkInfo.require_email && !isVerified" class="flex-1 flex items-center justify-center">
        <div class="bg-white dark:bg-gray-800 rounded-xl shadow-lg p-8 max-w-md w-full">
          <div class="text-center mb-6">
            <div class="w-16 h-16 bg-indigo-100 dark:bg-indigo-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
              <svg class="w-8 h-8 text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
              </svg>
            </div>
            <h2 class="text-xl font-semibold text-gray-900 dark:text-white mb-2">Verify Your Email</h2>
            <p class="text-gray-500 dark:text-gray-400 text-sm">
              {{ !codeSent ? 'Enter your email to continue' : 'Enter the code sent to your email' }}
            </p>
          </div>

          <!-- Email input form -->
          <form v-if="!codeSent" @submit.prevent="requestCode" class="space-y-4">
            <div>
              <input
                v-model="email"
                type="email"
                required
                placeholder="your@email.com"
                class="w-full px-4 py-3 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
                :disabled="verifyLoading"
              />
            </div>
            <button
              type="submit"
              :disabled="verifyLoading || !email"
              class="w-full py-3 px-4 bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-400 text-white font-medium rounded-lg transition-colors"
            >
              <span v-if="verifyLoading" class="flex items-center justify-center">
                <svg class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                  <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
                Sending...
              </span>
              <span v-else>Send Code</span>
            </button>
          </form>

          <!-- Code input form -->
          <form v-else @submit.prevent="verifyCode" class="space-y-4">
            <div>
              <input
                v-model="code"
                type="text"
                required
                maxlength="6"
                placeholder="123456"
                class="w-full px-4 py-3 text-center text-2xl tracking-widest border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white focus:ring-2 focus:ring-indigo-500 focus:border-transparent font-mono"
                :disabled="verifyLoading"
              />
              <p class="mt-2 text-xs text-gray-500 dark:text-gray-400 text-center">
                Code sent to {{ email }}
              </p>
            </div>
            <button
              type="submit"
              :disabled="verifyLoading || code.length !== 6"
              class="w-full py-3 px-4 bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-400 text-white font-medium rounded-lg transition-colors"
            >
              <span v-if="verifyLoading" class="flex items-center justify-center">
                <svg class="animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                  <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
                Verifying...
              </span>
              <span v-else>Verify</span>
            </button>
            <button
              type="button"
              @click="codeSent = false; code = ''"
              class="w-full py-2 text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
            >
              Use a different email
            </button>
          </form>

          <!-- Error message -->
          <div v-if="verifyError" class="mt-4 p-3 bg-status-danger-100 dark:bg-status-danger-900/30 border border-status-danger-200 dark:border-status-danger-800 rounded-lg">
            <p class="text-sm text-status-danger-600 dark:text-status-danger-400">{{ verifyError }}</p>
          </div>
        </div>
      </div>

      <!-- Chat interface -->
      <div v-else class="flex-1 flex flex-col">
        <!-- Read-only history banner -->
        <div
          v-if="viewingHistorySession"
          class="mb-3 px-3 py-2 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg flex items-center justify-between text-xs"
        >
          <span class="text-amber-700 dark:text-amber-400">
            Viewing past session — read only
          </span>
          <button
            @click="exitHistoryView"
            class="ml-2 text-amber-700 dark:text-amber-400 underline hover:no-underline"
          >
            Return to current chat
          </button>
        </div>

        <!-- Messages area using shared component -->
        <ChatMessages
          ref="messagesRef"
          :messages="messages"
          :loading="chatLoading"
          :loading-text="loadingText"
          class="flex-1"
        >
          <template #empty>
            <!-- Loading intro -->
            <div v-if="introLoading" class="text-center py-12">
              <div class="w-16 h-16 bg-indigo-100 dark:bg-indigo-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
                <svg class="w-8 h-8 text-indigo-500 animate-spin" fill="none" viewBox="0 0 24 24">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                  <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
              </div>
              <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Getting ready...</h3>
              <p class="text-gray-500 dark:text-gray-400 text-sm max-w-md mx-auto">
                The agent is preparing to assist you.
              </p>
            </div>

            <!-- Fallback welcome message (only if intro failed or not fetched yet) -->
            <div v-else class="text-center py-12">
              <div class="w-16 h-16 bg-indigo-100 dark:bg-indigo-900/30 rounded-full flex items-center justify-center mx-auto mb-4">
                <svg class="w-8 h-8 text-indigo-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
                </svg>
              </div>
              <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-2">Start a Conversation</h3>
              <p class="text-gray-500 dark:text-gray-400 text-sm max-w-md mx-auto">
                Type a message below to begin chatting.
              </p>
            </div>
          </template>
        </ChatMessages>

        <!-- Quick-action buttons (#363) — render alongside intro until first user message -->
        <ChatEmptyState
          v-if="showQuickActions"
          :playbooks="playbooks"
          :show-icon="false"
          heading=""
          subheading=""
          @select="onEmptyStateSelect"
          class="mb-2"
        />

        <!-- Error message -->
        <div v-if="chatError" class="mb-4 p-3 bg-status-danger-100 dark:bg-status-danger-900/30 border border-status-danger-200 dark:border-status-danger-800 rounded-lg">
          <p class="text-sm text-status-danger-600 dark:text-status-danger-400">{{ chatError }}</p>
        </div>

        <!-- Input area (hidden when viewing a read-only history session) -->
        <ChatInput
          v-if="!viewingHistorySession"
          v-model="message"
          :disabled="chatLoading"
          :public-token="token"
          placeholder="Type your message or / for playbooks..."
          @submit="sendMessage"
        />
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import axios from 'axios'
import { ChatMessages, ChatInput, ChatEmptyState, ChatHistoryDropdown } from '../components/chat'
import { getStatusFromStreamEvent, MIN_LABEL_DISPLAY_MS, HEARTBEAT_TIMEOUT_MS } from '../utils/execution-status'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const token = computed(() => route.params.token)
const authStore = useAuthStore()

// State
const loading = ref(true)
const linkInfo = ref(null)
const linkError = ref(null)

// Email verification
const email = ref('')
const code = ref('')
const codeSent = ref(false)
const verifyLoading = ref(false)
const verifyError = ref(null)
const sessionToken = ref(localStorage.getItem(`public_session_${token.value}`) || '')
const isVerified = computed(() => !linkInfo.value?.require_email || !!sessionToken.value)

// Chat session persistence (for anonymous links)
const chatSessionId = ref(localStorage.getItem(`public_chat_session_id_${token.value}`) || '')
const historyLoading = ref(false)

// Read-only history view (when a past session is loaded from ChatHistoryDropdown)
const viewingHistorySession = ref(null)

// Chat
const message = ref('')
const messages = ref([])
const chatLoading = ref(false)
const chatError = ref(null)
const messagesRef = ref(null)
const loadingText = ref('Thinking...')

// SSE state (THINK-001 for public chat)
let heartbeatTimer = null
let labelTimer = null
let lastLabelTime = 0
let streamReader = null

// Intro
const introLoading = ref(false)
const introError = ref(null)
const introFetched = ref(false)

// Playbooks (for empty-state quick actions)
const playbooks = ref([])
const userMessageCount = computed(() => messages.value.filter(m => m.role === 'user').length)
const showQuickActions = computed(() => userMessageCount.value === 0 && !introLoading.value)
const loadPlaybooks = async () => {
  try {
    const response = await axios.get(`/api/public/playbooks/${token.value}`)
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
  }
}

// Load link info
const loadLinkInfo = async () => {
  loading.value = true
  linkError.value = null

  try {
    const response = await axios.get(`/api/public/link/${token.value}`)
    linkInfo.value = response.data

    if (!response.data.valid) {
      linkError.value = getErrorMessage(response.data.reason)
    }
  } catch (err) {
    console.error('Failed to load link info:', err)
    if (err.response?.status === 404) {
      linkInfo.value = { valid: false }
      linkError.value = 'This link does not exist or has been removed.'
    } else {
      linkError.value = 'Failed to load link information. Please try again.'
    }
  } finally {
    loading.value = false
  }
}

const getErrorMessage = (reason) => {
  switch (reason) {
    case 'expired':
      return 'This link has expired.'
    case 'disabled':
      return 'This link has been disabled by the owner.'
    case 'not_found':
      return 'This link does not exist.'
    default:
      return 'This link is no longer valid.'
  }
}

// Request verification code
const requestCode = async () => {
  verifyLoading.value = true
  verifyError.value = null

  try {
    await axios.post('/api/public/verify/request', {
      token: token.value,
      email: email.value
    })
    codeSent.value = true
  } catch (err) {
    console.error('Failed to request code:', err)
    if (err.response?.status === 429) {
      verifyError.value = 'Too many requests. Please wait a few minutes and try again.'
    } else {
      verifyError.value = err.response?.data?.detail || 'Failed to send verification code.'
    }
  } finally {
    verifyLoading.value = false
  }
}

// Verify code
const verifyCode = async () => {
  verifyLoading.value = true
  verifyError.value = null

  try {
    const response = await axios.post('/api/public/verify/confirm', {
      token: token.value,
      email: email.value,
      code: code.value
    })

    if (response.data.verified) {
      sessionToken.value = response.data.session_token
      localStorage.setItem(`public_session_${token.value}`, response.data.session_token)

      // Try to load history first (returning user)
      const hasHistory = await loadHistory()

      // Only fetch intro if no history exists
      if (!hasHistory) {
        await fetchIntro()
      } else {
        introFetched.value = true
      }
    } else {
      verifyError.value = getVerifyErrorMessage(response.data.error)
    }
  } catch (err) {
    console.error('Failed to verify code:', err)
    verifyError.value = err.response?.data?.detail || 'Failed to verify code.'
  } finally {
    verifyLoading.value = false
  }
}

const getVerifyErrorMessage = (error) => {
  switch (error) {
    case 'invalid_code':
      return 'Invalid code. Please check and try again.'
    case 'code_expired':
      return 'Code has expired. Please request a new one.'
    default:
      return 'Verification failed. Please try again.'
  }
}

// Load chat history from server
const loadHistory = async () => {
  historyLoading.value = true

  try {
    // Build URL with appropriate session credentials
    let url = `/api/public/history/${token.value}`
    const params = []

    if (linkInfo.value?.require_email && sessionToken.value) {
      params.push(`session_token=${encodeURIComponent(sessionToken.value)}`)
    } else if (chatSessionId.value) {
      params.push(`session_id=${encodeURIComponent(chatSessionId.value)}`)
    }

    if (params.length > 0) {
      url += '?' + params.join('&')
    }

    const response = await axios.get(url)

    if (response.data.messages && response.data.messages.length > 0) {
      // Load history into messages array
      messages.value = response.data.messages.map(msg => ({
        role: msg.role,
        content: msg.content
      }))
      return true // History was loaded
    }

    return false // No history
  } catch (err) {
    console.error('Failed to load history:', err)
    return false
  } finally {
    historyLoading.value = false
  }
}

// Fetch agent introduction
const fetchIntro = async () => {
  if (introFetched.value || introLoading.value) return

  introLoading.value = true
  introError.value = null

  try {
    // Build URL with session token if needed
    let url = `/api/public/intro/${token.value}`
    if (linkInfo.value?.require_email && sessionToken.value) {
      url += `?session_token=${encodeURIComponent(sessionToken.value)}`
    }

    const response = await axios.get(url)

    if (response.data.intro) {
      // Add intro as first assistant message
      messages.value.push({
        role: 'assistant',
        content: response.data.intro
      })
    }

    introFetched.value = true
  } catch (err) {
    console.error('Failed to fetch intro:', err)
    // Don't block the user - just skip the intro on error
    introError.value = 'Could not load introduction.'
    introFetched.value = true
  } finally {
    introLoading.value = false
  }
}

// Confirm and start new conversation
const confirmNewConversation = async () => {
  if (!confirm('Start a new conversation? This will clear your chat history.')) {
    return
  }

  try {
    // Build URL with appropriate session credentials
    let url = `/api/public/session/${token.value}`
    const params = []

    if (linkInfo.value?.require_email && sessionToken.value) {
      params.push(`session_token=${encodeURIComponent(sessionToken.value)}`)
    } else if (chatSessionId.value) {
      params.push(`session_id=${encodeURIComponent(chatSessionId.value)}`)
    }

    if (params.length > 0) {
      url += '?' + params.join('&')
    }

    const response = await axios.delete(url)

    // Clear local messages
    messages.value = []
    introFetched.value = false

    // Update session_id for anonymous links
    if (response.data.new_session_id) {
      chatSessionId.value = response.data.new_session_id
      localStorage.setItem(`public_chat_session_id_${token.value}`, response.data.new_session_id)
    }

    // Fetch fresh intro
    await fetchIntro()
  } catch (err) {
    console.error('Failed to clear session:', err)
    chatError.value = 'Failed to start new conversation. Please refresh the page.'
  }
}

// Load a past session in read-only view (from ChatHistoryDropdown)
const handleHistorySessionSelected = ({ messages: sessionMessages, session }) => {
  viewingHistorySession.value = session
  messages.value = sessionMessages
  chatError.value = null
}

// Exit read-only history view and return to live chat
const exitHistoryView = async () => {
  viewingHistorySession.value = null
  messages.value = []
  introFetched.value = false
  const hasHistory = await loadHistory()
  if (!hasHistory) {
    await fetchIntro()
  } else {
    introFetched.value = true
  }
}

// THINK-001: Update loading text with minimum display time to prevent flicker
const updateLoadingText = (newText) => {
  if (!newText) return

  const now = Date.now()
  const elapsed = now - lastLabelTime

  if (elapsed < MIN_LABEL_DISPLAY_MS) {
    clearTimeout(labelTimer)
    labelTimer = setTimeout(() => {
      loadingText.value = newText
      lastLabelTime = Date.now()
    }, MIN_LABEL_DISPLAY_MS - elapsed)
  } else {
    loadingText.value = newText
    lastLabelTime = now
  }

  resetHeartbeat()
}

// THINK-001: Reset heartbeat timer
const resetHeartbeat = () => {
  clearTimeout(heartbeatTimer)
  heartbeatTimer = setTimeout(() => {
    if (chatLoading.value) {
      loadingText.value = 'Working...'
    }
  }, HEARTBEAT_TIMEOUT_MS)
}

// THINK-001: Close SSE connection and cleanup timers
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

// THINK-001: Subscribe to public execution SSE stream for status updates
const subscribeToStream = (executionId) => {
  closeSSE()
  lastLabelTime = 0
  resetHeartbeat()

  const url = `/api/public/executions/${token.value}/${executionId}/stream`

  fetch(url, {
    headers: { 'Accept': 'text/event-stream' }
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
    closeSSE()
  })
}

// THINK-001: Poll public execution status until complete
const pollExecution = async (executionId) => {
  const maxAttempts = 360 // 30 minutes at 5s intervals
  let attempts = 0

  while (attempts < maxAttempts && chatLoading.value) {
    attempts++
    await new Promise(resolve => setTimeout(resolve, 5000))

    try {
      const response = await axios.get(
        `/api/public/executions/${token.value}/${executionId}/status`
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

// Send chat message
const sendMessage = async (userMessage, files = []) => {
  if ((!userMessage && files.length === 0) || chatLoading.value) return

  chatError.value = null

  // Add user message to chat
  messages.value.push({
    role: 'user',
    content: userMessage
  })

  // Clear input
  message.value = ''

  chatLoading.value = true
  loadingText.value = 'Thinking...'

  try {
    const payload = {
      message: userMessage,
      async_mode: true,
      files: files.length > 0 ? files : undefined,
    }

    // Include session token if email verification was required
    if (linkInfo.value?.require_email && sessionToken.value) {
      payload.session_token = sessionToken.value
    } else if (chatSessionId.value) {
      // Include session_id for anonymous links
      payload.session_id = chatSessionId.value
    }

    // Submit with async_mode=true — returns execution_id immediately
    const submitResponse = await axios.post(`/api/public/chat/${token.value}`, payload)

    const executionId = submitResponse.data.execution_id
    if (!executionId) {
      throw new Error('No execution_id returned from async submission')
    }

    // Store session_id from response for anonymous links
    if (submitResponse.data.session_id && !linkInfo.value?.require_email) {
      chatSessionId.value = submitResponse.data.session_id
      localStorage.setItem(`public_chat_session_id_${token.value}`, submitResponse.data.session_id)
    }

    // Subscribe to SSE stream for real-time status updates
    subscribeToStream(executionId)

    // Poll for completion (SSE handles status labels, polling handles result)
    const execution = await pollExecution(executionId)

    closeSSE()

    if (execution) {
      if (execution.status === 'success' && execution.response) {
        messages.value.push({
          role: 'assistant',
          content: execution.response
        })
      } else if (execution.status === 'failed') {
        chatError.value = execution.error || 'Failed to process your request. Please try again.'
      } else if (execution.status === 'cancelled') {
        chatError.value = 'Request was cancelled.'
      }
    } else {
      chatError.value = 'Request timed out. Please try again.'
    }
  } catch (err) {
    console.error('Chat error:', err)
    closeSSE()
    if (err.response?.status === 401) {
      // Session expired, clear and show verification again
      sessionToken.value = ''
      localStorage.removeItem(`public_session_${token.value}`)
      chatError.value = 'Session expired. Please verify your email again.'
    } else if (err.response?.status === 429) {
      chatError.value = 'Too many requests. Please wait a moment.'
    } else {
      chatError.value = err.response?.data?.detail || 'Failed to send message. Please try again.'
    }
  } finally {
    chatLoading.value = false
    loadingText.value = 'Thinking...'
    closeSSE()
  }
}

// Initialize
onMounted(async () => {
  await loadLinkInfo()

  // If link is valid and chat is accessible (no email needed or already verified)
  if (linkInfo.value?.valid && linkInfo.value?.agent_available) {
    const needsEmail = linkInfo.value.require_email
    const hasSession = !!sessionToken.value

    if (!needsEmail || hasSession) {
      // Try to load history first
      const hasHistory = await loadHistory()

      // Only fetch intro if no history exists
      if (!hasHistory) {
        await fetchIntro()
      } else {
        introFetched.value = true // Mark intro as done since we have history
      }

      loadPlaybooks()
    }
  }
})

onUnmounted(() => {
  closeSSE()
})
</script>

<style scoped>
/* Custom bounce animation for loading dots */
@keyframes bounce {
  0%, 100% {
    transform: translateY(0);
  }
  50% {
    transform: translateY(-4px);
  }
}
</style>
